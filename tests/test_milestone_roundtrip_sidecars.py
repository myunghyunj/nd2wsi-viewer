"""Milestone 1 completeness tests: TIFF/OME-TIFF/QPTIFF sidecars, compound stems,
ROI export -> reopen -> annotate -> Astromap -> re-export round-trips, multi-scene
stack plane isolation, and packaged-source / README factual checks.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile
from http_fixture import serving

from nd2wsi import astromap_export as am
from nd2wsi.annotation_sidecars import (
    SLIDE_SUFFIXES,
    annotations_sidecar,
    confocal_annotations_sidecar,
    slide_stem,
)
from nd2wsi.cache import cache_container, source_tag
from nd2wsi.cache_removal import rescue_annotations
from nd2wsi.region_export import prepare_export
from nd2wsi.server import _frame_args, create_server, server_url
from nd2wsi.window_sessions import create_window_session

ROOT = Path(__file__).resolve().parents[1]
TIFF_VARIANTS = (".tif", ".tiff", ".ome.tif", ".ome.tiff", ".qptiff")


@pytest.fixture(autouse=True)
def _isolate_app_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Guarantee zero reads/writes to real ~/Library/Application Support/nd2wsi-viewer."""
    support = tmp_path / "isolated-app-support"
    support.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ND2WSI_APP_SUPPORT_DIR", str(support))
    monkeypatch.setenv("ND2WSI_UI_SETTINGS_ROOT", str(support))
    monkeypatch.setenv("ND2WSI_LUT_SETTINGS_ROOT", str(support / "lut-settings"))
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(support / "window-sessions"))
    return support


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        return err.code, json.loads(body) if body.strip().startswith("{") else {"raw": body}


def _http_download(url: str) -> tuple[int, bytes, dict[str, str]]:
    with urllib.request.urlopen(url, timeout=30) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, resp.read(), headers


def _disposition_filename(headers: dict[str, str]) -> str:
    cd = headers.get("content-disposition", "")
    m = re.search(r'filename="([^"]+)"', cd)
    assert m is not None, f"missing filename in Content-Disposition: {cd!r}"
    return m.group(1)


# ---------------------------------------------------------------------------
# 1. Suffix Registry, Compound Stems, Cache Tags & Annotation Rescue
# ---------------------------------------------------------------------------


def test_slide_suffixes_and_compound_stem_stripping() -> None:
    for ext in (".nd2", ".czi", ".lif", ".tif", ".tiff", ".qptiff", ".svs", ".sdpc"):
        assert ext in SLIDE_SUFFIXES
    for variant in TIFF_VARIANTS:
        assert Path(f"acquisition{variant}").suffix.lower() in SLIDE_SUFFIXES

    assert slide_stem("panel.ome.tif") == "panel"
    assert slide_stem("panel.ome.tiff") == "panel"
    assert slide_stem("PANEL.OME.TIF") == "PANEL"
    assert slide_stem("PANEL.OME.TIFF") == "PANEL"
    assert slide_stem("multiplex.qptiff") == "multiplex"
    assert slide_stem("crop.tif") == "crop"
    assert slide_stem("crop.tiff") == "crop"
    assert slide_stem("my.sample.v2.ome.tif") == "my.sample.v2"
    assert slide_stem("my.sample.v2.tif") == "my.sample.v2"
    assert slide_stem("slide.nd2.ome.zarr") == "slide"
    assert slide_stem("panel.ome.tif.ome.zarr") == "panel"
    assert slide_stem(r"C:\data\lab\panel.ome.tif") == "panel"


def test_cache_tags_and_rescue_annotations_cover_all_tiff_variants(tmp_path: Path) -> None:
    slides = [tmp_path / f"sample{ext}" for ext in (*TIFF_VARIANTS, ".nd2")]
    tags = {source_tag(p) for p in slides}
    containers = {cache_container(p) for p in slides}
    assert len(tags) == len(slides)
    assert len(containers) == len(slides)

    doomed_cache = tmp_path / "old_store.nd2wsi-cache"
    doomed_cache.mkdir()
    expected_names = set()
    for ext in TIFF_VARIANTS:
        ann_name = f"annotations_sample{ext}.json"
        (doomed_cache / ann_name).write_text(json.dumps({"items": [{"id": ext}]}), encoding="utf-8")
        expected_names.add(ann_name)
    plane_name = "annotations_sample.ome.tif--t1-p0-z2.json"
    (doomed_cache / plane_name).write_text(json.dumps({"items": [{"id": "plane"}]}), encoding="utf-8")
    expected_names.add(plane_name)

    rescue_dest = tmp_path / "nd2wsi" / "annotations"
    saved = rescue_annotations(doomed_cache, rescue_dest)
    assert {p.name for p in saved} == expected_names


# ---------------------------------------------------------------------------
# 2. Legacy Sidecar Migration & Same-Stem Sibling Ambiguity Across TIFFs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", TIFF_VARIANTS)
def test_legacy_exported_annotations_json_migrates_for_unambiguous_tiff_variants(
    tmp_path: Path, ext: str
) -> None:
    slide_dir = tmp_path / ext.lstrip(".").replace(".", "_")
    slide_dir.mkdir()
    slide = slide_dir / f"specimen{ext}"
    tifffile.imwrite(slide, np.zeros((32, 40), dtype=np.uint16))

    # Matches the exact <stem>.annotations.json filename written by app.js exportAnnotationsJSON()
    legacy = slide_dir / "specimen.annotations.json"
    legacy_payload = {"items": [{"id": f"legacy-{ext}", "type": "pin", "x": 5, "y": 6}]}
    legacy.write_text(json.dumps(legacy_payload), encoding="utf-8")

    # Agent window snapshots legacy marks privately without migrating or creating nd2wsi/
    agent_session = create_window_session("agent", tmp_path / "sessions")
    agent_srv = create_server([], port=0, window_session=agent_session)
    with serving(agent_srv):
        base_agent = server_url(agent_srv).rstrip("/")
        code, opened = _http_json(f"{base_agent}/api/open", method="POST", payload={"path": str(slide)})
        assert code == 200
        code_get, ann_agent = _http_json(f"{base_agent}/s/{opened['sid']}/api/annotations")
        assert code_get == 200
        assert ann_agent["items"] == legacy_payload["items"]
    assert not (slide_dir / "nd2wsi").exists()
    assert legacy.is_file()

    # User window migrates unambiguous legacy sidecar into nd2wsi/annotations/
    user_session = create_window_session("user", tmp_path / "sessions")
    user_srv = create_server([], port=0, window_session=user_session)
    with serving(user_srv):
        base_user = server_url(user_srv).rstrip("/")
        code, opened_u = _http_json(f"{base_user}/api/open", method="POST", payload={"path": str(slide)})
        assert code == 200
        code_get_u, ann_user = _http_json(f"{base_user}/s/{opened_u['sid']}/api/annotations")
        assert code_get_u == 200
        assert ann_user["items"] == legacy_payload["items"]

    canonical = slide_dir / "nd2wsi" / "annotations" / f"annotations_specimen{ext}.json"
    assert canonical.is_file()
    assert not legacy.exists()


@pytest.mark.parametrize("tiff_ext", TIFF_VARIANTS)
def test_sibling_nd2_and_tiff_refuse_unscoped_legacy_sidecar_until_provenanced(
    tmp_path: Path, tiff_ext: str
) -> None:
    folder = tmp_path / f"pair_{tiff_ext.lstrip('.').replace('.', '_')}"
    folder.mkdir()
    nd2_slide = folder / "shared_stem.nd2"
    tiff_slide = folder / f"shared_stem{tiff_ext}"
    nd2_slide.write_bytes(b"nd2-placeholder")
    tifffile.imwrite(tiff_slide, np.zeros((32, 32), dtype=np.uint16))

    unscoped = folder / "shared_stem.annotations.json"
    unscoped.write_text(json.dumps({"items": [{"id": "ambiguous-mark"}]}), encoding="utf-8")

    nd2_attrs: dict[str, Any] = {"nd2wsi": {"source": nd2_slide.name, "notes": []}}
    tiff_attrs: dict[str, Any] = {"nd2wsi": {"source": tiff_slide.name, "notes": []}}

    nd2_canon = annotations_sidecar(nd2_slide, nd2_attrs)
    tiff_canon = annotations_sidecar(tiff_slide, tiff_attrs)
    assert nd2_canon != tiff_canon
    assert not nd2_canon.exists()
    assert not tiff_canon.exists()
    assert unscoped.is_file()
    assert any("ambiguous" in n for n in nd2_attrs["nd2wsi"]["notes"])
    assert any("ambiguous" in n for n in tiff_attrs["nd2wsi"]["notes"])

    # Explicit source.name inside the sidecar resolves ownership unambiguously
    unscoped.write_text(
        json.dumps({"source": {"name": tiff_slide.name}, "items": [{"id": "provenanced-tiff"}]}),
        encoding="utf-8",
    )
    claimed = annotations_sidecar(tiff_slide, {"nd2wsi": {"source": tiff_slide.name}})
    assert claimed == tiff_canon
    assert claimed.is_file()
    assert not unscoped.exists()


# ---------------------------------------------------------------------------
# 3. 2D & Confocal (T, P, Z) Plane Sidecars + Astromap Across All 5 Suffixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", TIFF_VARIANTS)
def test_2d_sidecar_roundtrip_portability_and_astromap_naming_for_all_tiff_suffixes(
    tmp_path: Path, ext: str
) -> None:
    work = tmp_path / f"case_{ext.lstrip('.').replace('.', '_')}"
    work.mkdir()
    slide = work / f"biopsy_scan{ext}"
    pixels = np.arange(2 * 64 * 80, dtype=np.uint16).reshape(2, 64, 80) + 100
    if ext == ".qptiff":
        with tifffile.TiffWriter(slide) as tw:
            tw.write(
                pixels,
                photometric="minisblack",
                planarconfig="separate",
                description=(
                    '<?xml version="1.0"?><PerkinElmer-QPI-ImageDescription>'
                    "<Name>DAPI</Name><Biomarker>DAPI</Biomarker>"
                    "<Color>0,255,255</Color>"
                    "</PerkinElmer-QPI-ImageDescription>"
                ),
                resolution=(20000, 20000),
                resolutionunit="CENTIMETER",
            )
    else:
        tifffile.imwrite(
            slide,
            pixels,
            ome=True,
            photometric="minisblack",
            metadata={
                "axes": "CYX",
                "PhysicalSizeX": 0.5,
                "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": 0.5,
                "PhysicalSizeYUnit": "µm",
                "Channel": {"Name": ["DAPI", "FITC"]},
            },
        )

    session = create_window_session("user", tmp_path / "sessions")
    marks = [
        {"id": "box-1", "type": "box", "x": 8, "y": 10, "w": 24, "h": 18, "text": f"ROI {ext}"},
        {"id": "ruler-1", "type": "ruler", "x1": 4, "y1": 4, "x2": 44, "y2": 4, "text": "20 um"},
    ]

    srv = create_server([], port=0, window_session=session)
    with serving(srv):
        base = server_url(srv).rstrip("/")
        code, opened = _http_json(f"{base}/api/open", method="POST", payload={"path": str(slide)})
        assert code == 200
        sid = opened["sid"]

        # Read-only open does not create nd2wsi/ beside direct TIFFs
        assert not (work / "nd2wsi").exists()

        code_init, init_ann = _http_json(f"{base}/s/{sid}/api/annotations")
        assert code_init == 200
        assert init_ann["items"] == []
        assert init_ann["revision"] == "missing"

        code_save, saved = _http_json(
            f"{base}/s/{sid}/api/annotations",
            method="POST",
            payload={"items": marks, "expected_revision": init_ann["revision"]},
        )
        assert code_save == 200 and saved["ok"] is True
        assert saved["revision"].startswith("sha256:")

        # Stale expected_revision triggers 409 conflict with durable recovery draft
        code_conflict, conflict = _http_json(
            f"{base}/s/{sid}/api/annotations",
            method="POST",
            payload={"items": [{"id": "stale"}], "expected_revision": "missing"},
        )
        assert code_conflict == 409 and conflict["conflict"] is True
        assert Path(conflict["draft_path"]).is_file()

        # Astromap export strips compound .ome.tif / .ome.tiff cleanly -> biopsy_scan_astromap.zip
        _, info = _http_json(f"{base}/s/{sid}/api/info")
        code_am, am_res = _http_json(
            f"{base}/s/{sid}/api/astromap",
            method="POST",
            payload={
                "generation": info["generation"],
                "rois": [{"x": 8, "y": 10, "w": 24, "h": 18, "note": "Region1"}],
                "channels": [0, 1],
                "include_pdf": True,
            },
        )
        assert code_am == 200
        assert am_res["filename"] == "biopsy_scan_astromap.zip"
        dl_url = urllib.parse.urljoin(f"{base}/s/{sid}/", am_res["download_url"])
        dl_code, zip_bytes, _ = _http_download(dl_url)
        assert dl_code == 200
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert sorted(zf.namelist()) == [
                "biopsy_scan.jpg",
                "biopsy_scan.txt",
                "biopsy_scan_rois.pdf",
            ]

    expected_sidecar = work / "nd2wsi" / "annotations" / f"annotations_biopsy_scan{ext}.json"
    assert expected_sidecar.is_file()

    # Portability: move slide + nd2wsi/ folder together into a new directory and reopen
    moved_dir = tmp_path / f"relocated_{ext.lstrip('.').replace('.', '_')}"
    moved_dir.mkdir()
    moved_slide = moved_dir / slide.name
    shutil.move(str(slide), str(moved_slide))
    shutil.move(str(work / "nd2wsi"), str(moved_dir / "nd2wsi"))

    srv2 = create_server([], port=0, window_session=create_window_session("user", tmp_path / "sessions"))
    with serving(srv2):
        base2 = server_url(srv2).rstrip("/")
        code2, opened2 = _http_json(
            f"{base2}/api/open", method="POST", payload={"path": str(moved_slide)}
        )
        assert code2 == 200
        code_rel, reloaded = _http_json(f"{base2}/s/{opened2['sid']}/api/annotations")
        assert code_rel == 200
        assert reloaded["items"] == marks
        assert Path(reloaded["path"]) == (
            moved_dir / "nd2wsi" / "annotations" / f"annotations_biopsy_scan{ext}.json"
        )


@pytest.mark.parametrize("ext", TIFF_VARIANTS)
def test_confocal_tpz_plane_sidecars_and_astromap_across_tiff_suffixes(
    tmp_path: Path, ext: str
) -> None:
    work = tmp_path / f"stack_{ext.lstrip('.').replace('.', '_')}"
    work.mkdir()
    stack_path = work / f"confocal_vol{ext}"
    # Shape (T=2, Z=3, C=2, Y=48, X=64)
    vol = np.zeros((2, 3, 2, 48, 64), dtype=np.uint16)
    for t in range(2):
        for z in range(3):
            vol[t, z, 0] = (t + 1) * 1000 + z * 100
            vol[t, z, 1] = (t + 1) * 2000 + z * 200
    tifffile.imwrite(
        stack_path,
        vol,
        ome=True,
        photometric="minisblack",
        metadata={
            "axes": "TZCYX",
            "PhysicalSizeX": 0.25,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.25,
            "PhysicalSizeYUnit": "µm",
            "PhysicalSizeZ": 1.0,
            "PhysicalSizeZUnit": "µm",
            "Channel": {"Name": ["DAPI", "GFP"]},
        },
    )

    plane_marks = {
        (0, 0, 0): [{"id": "m-t0-z0", "type": "pin", "x": 10, "y": 12}],
        (0, 0, 2): [{"id": "m-t0-z2", "type": "box", "x": 4, "y": 5, "w": 20, "h": 15}],
        (1, 0, 1): [{"id": "m-t1-z1", "type": "ruler", "x1": 2, "y1": 2, "x2": 30, "y2": 2}],
    }

    session = create_window_session("user", tmp_path / "sessions")
    srv = create_server([], port=0, window_session=session)
    with serving(srv):
        base = server_url(srv).rstrip("/")
        code, opened = _http_json(f"{base}/api/open", method="POST", payload={"path": str(stack_path)})
        assert code == 200
        sid = opened["sid"]
        _, info = _http_json(f"{base}/s/{sid}/api/info")
        assert info["kind"] == "plate"
        assert (info["plate"]["T"], info["plate"]["P"], info["plate"]["Z"]) == (2, 1, 3)

        for (t, p, z), items in plane_marks.items():
            _, cur = _http_json(f"{base}/s/{sid}/api/annotations?t={t}&p={p}&z={z}")
            assert cur["items"] == []
            code_post, res_post = _http_json(
                f"{base}/s/{sid}/api/annotations?t={t}&p={p}&z={z}",
                method="POST",
                payload={"items": items, "expected_revision": cur["revision"]},
            )
            assert code_post == 200 and res_post["ok"] is True

        # Unannotated plane (1, 0, 2) remains empty
        _, untouched = _http_json(f"{base}/s/{sid}/api/annotations?t=1&p=0&z=2")
        assert untouched["items"] == []

        # Astromap export on frame (1, 0, 2) strips compound stem cleanly
        code_am, am_res = _http_json(
            f"{base}/s/{sid}/api/astromap",
            method="POST",
            payload={
                "generation": info["generation"],
                "rois": [{"x": 4, "y": 5, "w": 20, "h": 15, "note": "ZPlane"}],
                "channels": [0, 1],
                "frame": {"t": 1, "p": 0, "z": 2},
            },
        )
        assert code_am == 200
        assert am_res["filename"] == "confocal_vol_t1_p0_z2_astromap.zip"

    # Reopen in a fresh server and verify each plane's sidecar reloads independently
    srv_reopen = create_server([], port=0, window_session=create_window_session("user", tmp_path / "sessions"))
    with serving(srv_reopen):
        base2 = server_url(srv_reopen).rstrip("/")
        _, op2 = _http_json(f"{base2}/api/open", method="POST", payload={"path": str(stack_path)})
        sid2 = op2["sid"]
        for (t, p, z), expected_items in plane_marks.items():
            code_g, got = _http_json(f"{base2}/s/{sid2}/api/annotations?t={t}&p={p}&z={z}")
            assert code_g == 200
            assert got["items"] == expected_items
            expected_file = (
                work / "nd2wsi" / "annotations" / f"annotations_confocal_vol{ext}--t{t}-p{p}-z{z}.json"
            )
            assert Path(got["path"]) == expected_file
            assert expected_file.is_file()


def test_multiscene_ometiff_stack_isolates_plane_sidecars_and_exports_across_scenes(
    tmp_path: Path,
) -> None:
    """Two Z-stack scenes with different dimensions in one .ome.tif must never collide on p=0."""
    multi_path = tmp_path / "two_stacks.ome.tif"
    s0 = np.full((3, 2, 48, 64), 1200, dtype=np.uint16)
    s1 = np.full((3, 2, 80, 96), 3400, dtype=np.uint16)
    with tifffile.TiffWriter(multi_path, bigtiff=True, ome=True) as tw:
        tw.write(
            s0,
            photometric="minisblack",
            metadata={"axes": "ZCYX", "Name": "SceneA", "PhysicalSizeX": 0.4, "PhysicalSizeY": 0.4},
        )
        tw.write(
            s1,
            photometric="minisblack",
            metadata={"axes": "ZCYX", "Name": "SceneB", "PhysicalSizeX": 0.4, "PhysicalSizeY": 0.4},
        )

    session = create_window_session("user", tmp_path / "sessions")
    srv = create_server([], port=0, window_session=session)
    with serving(srv):
        base = server_url(srv).rstrip("/")
        code0, op0 = _http_json(f"{base}/api/open", method="POST", payload={"path": str(multi_path)})
        assert code0 == 200
        sid0 = op0["sid"]

        # Save annotation on Scene 0 (48x64), plane z=1
        _, init0 = _http_json(f"{base}/s/{sid0}/api/annotations?t=0&p=0&z=1")
        code_s0, _ = _http_json(
            f"{base}/s/{sid0}/api/annotations?t=0&p=0&z=1",
            method="POST",
            payload={"items": [{"id": "scene0-z1", "type": "pin", "x": 5, "y": 5}], "expected_revision": init0["revision"]},
        )
        assert code_s0 == 200

        # Switch to Scene 1 (80x96) via /api/select-scene
        code_sel, sel1 = _http_json(
            f"{base}/s/{sid0}/api/select-scene", method="POST", payload={"series": 1}
        )
        assert code_sel == 200
        sid1 = sel1["sid"]

        # Scene 1 plane z=1 must NOT collide with Scene 0 or fail geometry validation
        code_init1, init1 = _http_json(f"{base}/s/{sid1}/api/annotations?t=0&p=0&z=1")
        assert code_init1 == 200
        assert init1["items"] == []
        assert init1["path"].endswith("annotations_two_stacks.ome.tif--t0-p1-z1.json")

        code_s1, _ = _http_json(
            f"{base}/s/{sid1}/api/annotations?t=0&p=0&z=1",
            method="POST",
            payload={"items": [{"id": "scene1-z1", "type": "pin", "x": 70, "y": 60}], "expected_revision": init1["revision"]},
        )
        assert code_s1 == 200

        # Verify ROI export and Astromap export on Scene 1 include _p1
        _, info1 = _http_json(f"{base}/s/{sid1}/api/info")
        st1 = srv.registry.get(sid1)
        exp_plan1 = prepare_export(
            st1,
            {"x": ["0"], "y": ["0"], "w": ["32"], "h": ["32"], "format": ["tiff"], "t": ["0"], "p": ["0"], "z": ["1"]},
            resolve_frame=_frame_args,
        )
        assert exp_plan1.filename == "two_stacks_t0_p1_z1_L0_x0_y0_32x32"

        am_plan1 = am.prepare_astromap(
            st1,
            {
                "generation": info1["generation"],
                "rois": [{"x": 2, "y": 2, "w": 16, "h": 16, "note": "S1"}],
                "channels": [0, 1],
                "frame": {"t": 0, "p": 0, "z": 1},
            },
            resolve_frame=_frame_args,
        )
        assert am_plan1.stem == "two_stacks_t0_p1_z1"

    canon0, _ = confocal_annotations_sidecar(multi_path, {"nd2wsi": {"source": multi_path.name, "selection": {"p": 0}, "plate": {"P": 1}}}, (0, 0, 1))
    canon1, _ = confocal_annotations_sidecar(multi_path, {"nd2wsi": {"source": multi_path.name, "selection": {"p": 1}, "plate": {"P": 1}}}, (0, 0, 1))
    assert canon0 != canon1
    assert json.loads(canon0.read_text(encoding="utf-8"))["items"][0]["id"] == "scene0-z1"
    assert json.loads(canon1.read_text(encoding="utf-8"))["items"][0]["id"] == "scene1-z1"


# ---------------------------------------------------------------------------
# 4. Full Milestone 1 Round-Trip: Open -> Export ROI .tif -> Reopen -> Annotate -> Re-Export
# ---------------------------------------------------------------------------


def test_milestone1_full_roi_export_reopen_annotate_astromap_and_reexport_roundtrip(
    tmp_path: Path,
) -> None:
    lab_dir = tmp_path / "lab_session"
    lab_dir.mkdir()
    src_path = lab_dir / "acquisition_panel.ome.tif"

    rng = np.random.default_rng(42)
    raw = rng.integers(100, 50000, size=(3, 192, 224), dtype=np.uint16)
    tifffile.imwrite(
        src_path,
        raw,
        ome=True,
        tile=(128, 128),
        photometric="minisblack",
        metadata={
            "axes": "CYX",
            "PhysicalSizeX": 0.325,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.325,
            "PhysicalSizeYUnit": "µm",
            "Channel": {
                "Name": ["DAPI", "FITC", "CY5"],
                "Color": [16777215, 16711935, -65281],
            },
        },
    )

    session = create_window_session("user", tmp_path / "sessions")
    srv = create_server([], port=0, window_session=session)
    with serving(srv):
        base = server_url(srv).rstrip("/")

        # Step 1: Open source .ome.tif and save an annotation on the parent acquisition
        code, opened = _http_json(f"{base}/api/open", method="POST", payload={"path": str(src_path)})
        assert code == 200
        sid_src = opened["sid"]

        _, src_ann0 = _http_json(f"{base}/s/{sid_src}/api/annotations")
        src_mark = [{"id": "parent-roi", "type": "box", "x": 32, "y": 24, "w": 96, "h": 80, "text": "Cutout"}]
        code_sa, _ = _http_json(
            f"{base}/s/{sid_src}/api/annotations",
            method="POST",
            payload={"items": src_mark, "expected_revision": src_ann0["revision"]},
        )
        assert code_sa == 200

        # Step 2: Export native 16-bit OME-TIFF ROI into the SAME folder using server's suggested filename
        dl_code, roi1_bytes, roi1_headers = _http_download(
            f"{base}/s/{sid_src}/api/roi?level=0&x=32&y=24&w=96&h=80&format=tiff&c=0,2"
        )
        assert dl_code == 200
        roi1_name = _disposition_filename(roi1_headers)
        assert roi1_name == "acquisition_panel_L0_x32_y24_96x80.tif"
        roi1_path = lab_dir / roi1_name
        assert not roi1_path.exists()
        roi1_path.write_bytes(roi1_bytes)

        # Step 3: Reopen exported ROI .tif and verify exact pixels, calibration, and selected channels
        code_r1, opened_r1 = _http_json(
            f"{base}/api/open", method="POST", payload={"path": str(roi1_path)}
        )
        assert code_r1 == 200
        sid_r1 = opened_r1["sid"]
        assert sid_r1 != sid_src

        _, info_r1 = _http_json(f"{base}/s/{sid_r1}/api/info")
        assert (info_r1["width"], info_r1["height"]) == (96, 80)
        assert info_r1["dtype"] == "uint16"
        assert info_r1["calibrated"] is True
        assert np.allclose(info_r1["pixelSizeUm"], [0.325, 0.325], rtol=1e-4)
        assert [c["label"] for c in info_r1["channels"]] == ["DAPI", "CY5"]

        st_r1 = srv.registry.get(sid_r1)
        np.testing.assert_array_equal(
            np.asarray(st_r1.root["0"][:, :, :]),
            raw[[0, 2], 24 : 24 + 80, 32 : 32 + 96],
        )

        # Step 4: Save annotations on the reopened ROI .tif and verify distinct sidecar from parent
        _, r1_ann0 = _http_json(f"{base}/s/{sid_r1}/api/annotations")
        assert r1_ann0["items"] == []
        roi_marks = [
            {"id": "sub-cell-1", "type": "pin", "x": 14, "y": 18, "text": "Positive nucleus"},
            {"id": "sub-box-1", "type": "box", "x": 10, "y": 12, "w": 40, "h": 32, "text": "Sub-ROI"},
        ]
        code_r1_save, r1_saved = _http_json(
            f"{base}/s/{sid_r1}/api/annotations",
            method="POST",
            payload={"items": roi_marks, "expected_revision": r1_ann0["revision"]},
        )
        assert code_r1_save == 200 and r1_saved["ok"] is True

        # Step 5: Export Astromap on the reopened ROI .tif
        code_am, am_r1 = _http_json(
            f"{base}/s/{sid_r1}/api/astromap",
            method="POST",
            payload={
                "generation": info_r1["generation"],
                "rois": [{"x": 10, "y": 12, "w": 40, "h": 32, "note": "SubROI"}],
                "channels": [0, 1],
            },
        )
        assert code_am == 200
        assert am_r1["filename"] == "acquisition_panel_L0_x32_y24_96x80_astromap.zip"

        # Step 6: Re-export a second-generation sub-ROI (.tif and .png) from the reopened ROI .tif
        dl2_code, roi2_bytes, roi2_headers = _http_download(
            f"{base}/s/{sid_r1}/api/roi?level=0&x=10&y=12&w=40&h=32&format=tiff&c=0,1"
        )
        assert dl2_code == 200
        roi2_name = _disposition_filename(roi2_headers)
        assert roi2_name == "acquisition_panel_L0_x32_y24_96x80_L0_x10_y12_40x32.tif"
        assert roi2_name != roi1_name
        roi2_path = lab_dir / roi2_name
        roi2_path.write_bytes(roi2_bytes)

        png_code, png_bytes, png_headers = _http_download(
            f"{base}/s/{sid_r1}/api/roi?level=0&x=10&y=12&w=40&h=32&format=png&c=0,1"
        )
        assert png_code == 200 and len(png_bytes) > 50
        assert _disposition_filename(png_headers) == (
            "acquisition_panel_L0_x32_y24_96x80_L0_x10_y12_40x32.png"
        )

        # Reopen second-generation sub-ROI and verify exact pixel values
        code_r2, opened_r2 = _http_json(
            f"{base}/api/open", method="POST", payload={"path": str(roi2_path)}
        )
        assert code_r2 == 200
        st_r2 = srv.registry.get(opened_r2["sid"])
        np.testing.assert_array_equal(
            np.asarray(st_r2.root["0"][:, :, :]),
            raw[[0, 2], 24 + 12 : 24 + 12 + 32, 32 + 10 : 32 + 10 + 40],
        )

    # Step 7: Reopen both parent .ome.tif and ROI .tif in a brand-new server session
    srv_verify = create_server([], port=0, window_session=create_window_session("user", tmp_path / "sessions"))
    with serving(srv_verify):
        base_v = server_url(srv_verify).rstrip("/")
        _, op_parent = _http_json(f"{base_v}/api/open", method="POST", payload={"path": str(src_path)})
        _, op_child = _http_json(f"{base_v}/api/open", method="POST", payload={"path": str(roi1_path)})
        _, got_parent = _http_json(f"{base_v}/s/{op_parent['sid']}/api/annotations")
        _, got_child = _http_json(f"{base_v}/s/{op_child['sid']}/api/annotations")
        assert got_parent["items"] == src_mark
        assert got_child["items"] == roi_marks
        assert got_parent["path"] != got_child["path"]


# ---------------------------------------------------------------------------
# 5. Packaging Bundling Verifiers & README / Signing Factual Invariants
# ---------------------------------------------------------------------------


def _load_script_module(name: str, rel_path: str, monkeypatch: pytest.MonkeyPatch | None = None):
    if monkeypatch is not None:
        monkeypatch.syspath_prepend(str((ROOT / rel_path).parent))
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_source_verifiers_enforce_ometiff_and_ui_settings_on_mac_and_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac_verifier = _load_script_module("verify_packaged_source_m1", "scripts/verify_packaged_source.py")
    win_builder = _load_script_module("build_windows_m1", "packaging/build_windows.py", monkeypatch)

    compiled: dict[str, Any] = {}
    for source in sorted((ROOT / "nd2wsi").rglob("*.py")):
        parts = list(source.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        compiled[".".join(parts)] = compile(
            source.read_text(encoding="utf-8"), str(source), "exec", dont_inherit=True
        )

    assert "nd2wsi.ometiff" in compiled
    assert "nd2wsi.ui_settings" in compiled

    mac_resources = tmp_path / "Contents" / "Resources"
    shutil.copytree(ROOT / "nd2wsi/static", mac_resources / "nd2wsi/static")
    info = {
        "CFBundleName": "nd2wsi-viewer",
        "CFBundleIdentifier": "com.nd2wsi.viewer",
        "ND2WSIPackageVersion": mac_verifier.project_version(),
    }

    # Simulate PyInstaller ZlibArchiveReader returning None when a module is absent
    report_mac = mac_verifier.verify(
        set(compiled), lambda mod: compiled.get(mod), mac_resources, info,
        expected_version=mac_verifier.project_version(),
    )
    assert report_mac["ok"] is True
    assert {"nd2wsi.ometiff", "nd2wsi.ui_settings"} <= set(report_mac["python_modules"])

    for missing_mod in ("nd2wsi.ometiff", "nd2wsi.ui_settings"):
        incomplete_toc = set(compiled) - {missing_mod}
        with pytest.raises(AssertionError, match=f"missing packaged module: {re.escape(missing_mod)}"):
            mac_verifier.verify(
                incomplete_toc, lambda mod: compiled.get(mod), mac_resources, info,
                expected_version=mac_verifier.project_version(),
            )

    # Windows bundle verifier checks PYZ modules + _internal/nd2wsi/static bytes
    win_internal = tmp_path / "_internal"
    shutil.copytree(ROOT / "nd2wsi/static", win_internal / "nd2wsi/static")
    report_win = win_builder.verify_bundled_source(
        set(compiled), lambda mod: compiled.get(mod), win_internal
    )
    assert {"nd2wsi.ometiff", "nd2wsi.ui_settings"} <= set(report_win["python_modules"])

    for missing_mod in ("nd2wsi.ometiff", "nd2wsi.ui_settings"):
        with pytest.raises(AssertionError, match=f"missing packaged module: {re.escape(missing_mod)}"):
            win_builder.verify_bundled_source(
                set(compiled) - {missing_mod}, lambda mod: compiled.get(mod), win_internal
            )

    # Windows source snapshot manifest requires ometiff.py, ui_settings.py, and THIRD_PARTY_NOTICES.md
    win_src = (ROOT / "packaging/build_windows.py").read_text(encoding="utf-8")
    assert "nd2wsi.ometiff" in win_src and "nd2wsi.ui_settings" in win_src
    assert "THIRD_PARTY_NOTICES.md" in win_src


def test_readme_and_signing_script_stay_strictly_factual() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sign_script = (ROOT / "packaging" / "sign_release.sh").read_text(encoding="utf-8")

    # packaging/sign_release.sh implements codesign only (no notarytool or stapler)
    assert "codesign" in sign_script
    assert "notarytool" not in sign_script
    assert "stapler" not in sign_script

    # README accurately describes ad-hoc signing and makes zero notarization or speed-multiplier claims
    assert "ad-hoc signed" in readme
    for overclaim in ("notarytool", "stapler", "notarized", "2.5x", "<300 ms", "60 fps"):
        assert overclaim not in readme.lower()

    # Zero AGENTS.md internal automation leakage in README.md
    for internal_token in ("--agent-window", 'new_window("agent")', "window_context", "exports_root"):
        assert internal_token not in readme
