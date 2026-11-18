"""Phase 1 Track 3 tests: native OME-TIFF/TIFF reader, multi-series/scene API,
bounded HTTP thread pool, nd2Export dtype gating, col pass-through, Z-prefetch,
and UI settings / recent files endpoints with Agent window isolation.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import plistlib
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile
from http_fixture import running_server
from PIL import Image

from nd2wsi import app, ometiff, server
from nd2wsi.server import SlideRegistry, create_server, server_url
from nd2wsi.window_sessions import create_window_session

ROOT = Path(__file__).resolve().parents[1]


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


def _http_bytes(url: str) -> tuple[int, bytes, str]:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.status, resp.read(), resp.headers.get("Content-Type", "")


# ---------------------------------------------------------------------------
# Synthetic LIF & Multi-Scene CZI Builders
# ---------------------------------------------------------------------------


def _build_synthetic_lif(path: Path, series_specs: list[dict[str, Any]]) -> Path:
    elem_xmls: list[str] = []
    mem_payloads: list[tuple[str, bytes]] = []
    for idx, spec in enumerate(series_specs):
        name = spec.get("name", f"Series{idx:03d}")
        mem_id = f"MemBlock_{idx + 1}"
        payload: bytes = spec["payload"]
        dims_xml = "".join(spec["dims_xml"])
        chans_xml = "".join(spec["chans_xml"])
        elem_xmls.append(
            f'<Element Name="{name}">'
            f"<Data><Image><ImageDescription>"
            f"<Channels>{chans_xml}</Channels>"
            f"<Dimensions>{dims_xml}</Dimensions>"
            f"</ImageDescription></Image></Data>"
            f'<Memory Size="{len(payload)}" MemoryBlockID="{mem_id}"/>'
            f"</Element>"
        )
        mem_payloads.append((mem_id, payload))

    xml_str = (
        '<?xml version="1.0" encoding="utf-16"?>'
        f'<LMSDataContainerHeader Version="2">{"".join(elem_xmls)}</LMSDataContainerHeader>'
    )
    xml_utf16 = xml_str.encode("utf-16-le")
    buf = bytearray()
    buf.extend(struct.pack("<iIBI", 0x70, 5 + len(xml_utf16), 0x2A, len(xml_utf16) // 2))
    buf.extend(xml_utf16)
    for mem_id, payload in mem_payloads:
        id_utf16 = mem_id.encode("utf-16-le")
        chunk_len = 1 + 8 + 1 + 4 + len(id_utf16)
        buf.extend(
            struct.pack("<iIBQBI", 0x70, chunk_len, 0x2A, len(payload), 0x2A, len(id_utf16) // 2)
        )
        buf.extend(id_utf16)
        buf.extend(payload)
    path.write_bytes(bytes(buf))
    return path


def _build_multiscene_czi(
    path: Path,
    scenes: list[dict[str, Any]],
    *,
    pixel_size_um: tuple[float, float] = (0.325, 0.325),
) -> Path:
    """Write a synthetic multi-scene ZISRAW CZI where each scene dict has:
    {"s": int, "name": str, "planes": {(t, z, c): 2D uint16 array}}.
    """
    dir_records: list[tuple[int, ...]] = []
    buf = bytearray(512)
    scene_xml_nodes = ""

    for sc in scenes:
        s_idx = int(sc["s"])
        s_name = sc.get("name", f"Scene_{s_idx}")
        scene_xml_nodes += f'<Scene Index="{s_idx}" Name="{s_name}"/>'
        for (t, z, c), arr in sc["planes"].items():
            h, w = arr.shape
            payload = np.ascontiguousarray(arr, dtype=np.uint16).tobytes()
            fpos = len(buf)
            ndims = 6  # X, Y, C, Z, T, S
            hdr_len = max(256, 16 + 32 + 20 * ndims)
            used_size = hdr_len + len(payload)
            alloc_size = (used_size + 31) // 32 * 32

            seg_hdr = struct.pack("<16sQQ", b"ZISRAWSUBBLOCK\x00\x00", alloc_size, used_size)
            sb_prefix = bytearray(hdr_len)
            struct.pack_into("<IIQ", sb_prefix, 0, 0, 0, len(payload))
            struct.pack_into(
                "<2siqiiBB4si", sb_prefix, 16, b"DV", 1, fpos, 0, 0, 0, 0, b"\x00" * 4, ndims
            )
            dims = [
                (b"X\x00\x00\x00", s_idx * 2000, w, float(s_idx * 2000), w),
                (b"Y\x00\x00\x00", 0, h, 0.0, h),
                (b"C\x00\x00\x00", c, 1, 0.0, 1),
                (b"Z\x00\x00\x00", z, 1, 0.0, 1),
                (b"T\x00\x00\x00", t, 1, 0.0, 1),
                (b"S\x00\x00\x00", s_idx, 1, 0.0, 1),
            ]
            for idx_d, (dname, st_i, sz_i, st_f, st_sz) in enumerate(dims):
                struct.pack_into(
                    "<4siifi", sb_prefix, 48 + idx_d * 20, dname, st_i, sz_i, st_f, st_sz
                )
            buf.extend(seg_hdr)
            buf.extend(sb_prefix)
            buf.extend(payload)
            if alloc_size > used_size:
                buf.extend(b"\x00" * (alloc_size - used_size))
            dir_records.append((fpos, s_idx * 2000, w, 0, h, t, z, c, s_idx, 1))

    dir_pos = len(buf)
    dir_payload = bytearray(128)
    struct.pack_into("<i", dir_payload, 0, len(dir_records))
    for fpos, xs, w, ys, h, t, z, c, s_idx, sb_ptype in dir_records:
        ndims = 6
        dir_payload.extend(
            struct.pack("<2siqiiBB4si", b"DV", sb_ptype, fpos, 0, 0, 0, 0, b"\x00" * 4, ndims)
        )
        for dname, st_i, sz_i in (
            (b"X\x00\x00\x00", xs, w),
            (b"Y\x00\x00\x00", ys, h),
            (b"C\x00\x00\x00", c, 1),
            (b"Z\x00\x00\x00", z, 1),
            (b"T\x00\x00\x00", t, 1),
            (b"S\x00\x00\x00", s_idx, 1),
        ):
            dir_payload.extend(struct.pack("<4siifi", dname, st_i, sz_i, float(st_i), sz_i))
    dir_used = len(dir_payload)
    dir_alloc = (dir_used + 31) // 32 * 32
    buf.extend(struct.pack("<16sQQ", b"ZISRAWDIRECTORY\x00", dir_alloc, dir_used))
    buf.extend(dir_payload)
    if dir_alloc > dir_used:
        buf.extend(b"\x00" * (dir_alloc - dir_used))

    meta_pos = len(buf)
    py_m, px_m = pixel_size_um[0] * 1e-6, pixel_size_um[1] * 1e-6
    xml_str = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<ImageDocument><Metadata>"
        f"<Information><Image><Dimensions><S><Scenes>{scene_xml_nodes}</Scenes></S></Dimensions></Image></Information>"
        "<Scaling><Items>"
        f'<Distance Id="X"><Value>{px_m}</Value></Distance>'
        f'<Distance Id="Y"><Value>{py_m}</Value></Distance>'
        '<Distance Id="Z"><Value>1.5e-6</Value></Distance>'
        "</Items></Scaling>"
        "<DisplaySetting><Channels>"
        '<Channel Id="Channel:0" Name="DAPI"><ShortName>DAPI</ShortName><Color>#FF00A0FF</Color></Channel>'
        '<Channel Id="Channel:1" Name="GFP"><ShortName>GFP</ShortName><Color>#FF00FF00</Color></Channel>'
        '<Channel Id="Channel:2" Name="CY5"><ShortName>CY5</ShortName><Color>#FFFF00FF</Color></Channel>'
        "</Channels></DisplaySetting>"
        "</Metadata></ImageDocument>"
    )
    xml_bytes = xml_str.encode("utf-8")
    meta_hdr = bytearray(256)
    struct.pack_into("<II", meta_hdr, 0, len(xml_bytes), 0)
    meta_used = 256 + len(xml_bytes)
    meta_alloc = (meta_used + 31) // 32 * 32
    buf.extend(struct.pack("<16sQQ", b"ZISRAWMETADATA\x00\x00", meta_alloc, meta_used))
    buf.extend(meta_hdr)
    buf.extend(xml_bytes)
    if meta_alloc > meta_used:
        buf.extend(b"\x00" * (meta_alloc - meta_used))

    struct.pack_into("<16sQQ", buf, 0, b"ZISRAWFILE\x00\x00\x00\x00\x00\x00", 480, 80)
    struct.pack_into(
        "<iiii16s16siQQiQ",
        buf,
        32,
        1,
        0,
        0,
        0,
        b"\x01" * 16,
        b"\x02" * 16,
        0,
        dir_pos,
        meta_pos,
        0,
        0,
    )
    path.write_bytes(bytes(buf))
    return path


# ---------------------------------------------------------------------------
# 1. Native OME-TIFF / TIFF / QPTIFF Open, Metadata, Calibration & Tile Serving
# ---------------------------------------------------------------------------


def test_app_and_mac_packaging_declare_tiff_extensions(tmp_path: Path):
    for ext in (".tif", ".tiff", ".qptiff"):
        assert ext in app.OPEN_SUFFIXES
        assert f"*{ext}" in app.OPEN_FILE_TYPES[0]
        assert ometiff.is_ometiff(f"sample{ext}")
    assert ometiff.is_ometiff("multiplex.ome.tif")
    assert ometiff.is_ometiff("multiplex.ome.tiff")
    assert not ometiff.is_ometiff("aperio.svs")

    ome_file = tmp_path / "direct_check.ome.tif"
    tifffile.imwrite(ome_file, np.zeros((32, 32), dtype=np.uint16))
    assert app.open_or_convert(ome_file) == ome_file

    script = (ROOT / "packaging" / "build_mac_app.sh").read_text(encoding="utf-8")
    plist_src = script.split("<< 'PLISTEOF'\n", 1)[1].split("\nPLISTEOF", 1)[0]
    plist_path = tmp_path / "Info.plist"
    plist_path.write_bytes(plistlib.dumps({}))
    subprocess.run(
        [sys.executable, "-", str(plist_path), str(ROOT / "pyproject.toml")],
        input=plist_src,
        text=True,
        check=True,
        timeout=30,
    )
    info = plistlib.loads(plist_path.read_bytes())
    imported = {item["UTTypeIdentifier"]: item for item in info["UTImportedTypeDeclarations"]}
    assert "org.openmicroscopy.ome-tiff" in imported
    assert "com.perkinelmer.qptiff" in imported
    doc_types = [ct for item in info["CFBundleDocumentTypes"] for ct in item["LSItemContentTypes"]]
    assert "org.openmicroscopy.ome-tiff" in doc_types
    assert "public.tiff" in doc_types


def test_2d_multichannel_calibrated_ometiff_open_tile_pixel_and_col(tmp_path: Path):
    h, w = 144, 192
    ch0 = np.full((h, w), 1200, dtype=np.uint16)
    ch1 = np.zeros((h, w), dtype=np.uint16)
    ch1[:72, :96] = 18000
    ch2 = np.linspace(0, 8000, h * w, dtype=np.uint16).reshape(h, w)
    data = np.stack([ch0, ch1, ch2], axis=0)

    # Signed 32-bit RGBA colors:
    # Cyan (0x00FFFFFF = 16777215), Red (0xFF0000FF = -16776961), Yellow (0xFFFF00FF = -65281)
    ome_path = tmp_path / "tissue_panel.ome.tif"
    tifffile.imwrite(
        ome_path,
        data,
        ome=True,
        photometric="minisblack",
        metadata={
            "axes": "CYX",
            "PhysicalSizeX": 325.0,
            "PhysicalSizeXUnit": "nm",
            "PhysicalSizeY": 0.325,
            "PhysicalSizeYUnit": "µm",
            "Channel": {
                "Name": ["Hoechst", "TRITC", "EYFP"],
                "Color": [16777215, -16776961, -65281],
            },
        },
    )

    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")
        code, opened = _http_json(f"{base}/api/open", method="POST", payload={"path": str(ome_path)})
        assert code == 200
        sid = opened["sid"]

        code, info = _http_json(f"{base}/s/{sid}/api/info")
        assert code == 200
        assert info["width"] == w and info["height"] == h
        assert info["dtype"] == "uint16"
        assert info["rgb"] is False
        assert info["direct"] is True
        assert info["calibrated"] is True
        assert np.allclose(info["pixelSizeUm"], [0.325, 0.325], rtol=1e-5)
        assert [c["label"] for c in info["channels"]] == ["Hoechst", "TRITC", "EYFP"]
        assert [c["color"] for c in info["channels"]] == ["00FFFF", "FF0000", "FFFF00"]
        assert not (tmp_path / "nd2wsi").exists()

        # Raw pixel probe
        code, px = _http_json(f"{base}/s/{sid}/api/pixel?x=10&y=10")
        assert code == 200
        assert [v["value"] for v in px["values"]] == [1200, 18000, int(ch2[10, 10])]

        # Tile render + custom ?col= override
        status_default, tile_default, ctype = _http_bytes(
            f"{base}/s/{sid}/api/tile/0/0/0.png?c=1&win=,0:18000,"
        )
        assert status_default == 200 and ctype == "image/png"
        arr_default = np.asarray(Image.open(io.BytesIO(tile_default)))
        # Default TRITC color is FF0000 -> top-left bright red, green == 0
        assert int(arr_default[10, 10, 0]) > 240 and int(arr_default[10, 10, 1]) == 0

        status_col, tile_col, _ = _http_bytes(
            f"{base}/s/{sid}/api/tile/0/0/0.png?c=1&win=,0:18000,&col=,00FF00,"
        )
        assert status_col == 200
        arr_col = np.asarray(Image.open(io.BytesIO(tile_col)))
        # Overridden TRITC color is 00FF00 -> top-left bright green, red == 0
        assert int(arr_col[10, 10, 1]) > 240 and int(arr_col[10, 10, 0]) == 0

        # Region / ROI PNG render with ?col=
        status_reg, reg_bytes, _ = _http_bytes(
            f"{base}/s/{sid}/api/region?level=0&x=0&y=0&w=32&h=32&format=png&c=1&win=,0:18000,&col=,0000FF,"
        )
        assert status_reg == 200
        arr_reg = np.asarray(Image.open(io.BytesIO(reg_bytes)))
        assert int(arr_reg[5, 5, 2]) > 240 and int(arr_reg[5, 5, 0]) == 0


def test_uncalibrated_tiff_never_invents_one_micron_and_imagej_units_work(tmp_path: Path):
    uncal = tmp_path / "uncalibrated_raw.tif"
    tifffile.imwrite(uncal, np.full((64, 80), 500, dtype=np.uint16))

    reg = SlideRegistry()
    try:
        sid = reg.open_path(uncal)
        st = reg.get(sid)
        assert st.attrs["nd2wsi"]["pixel_size_um"] is None
        assert st.attrs["omero"]["channels"][0]["label"] == "Ch1"
        assert st.attrs["omero"]["channels"][0]["color"] == "FFFFFF"

        # ImageJ calibrated 2-channel TIFF with LUTs and micron unit
        ij_path = tmp_path / "ij_calibrated.tif"
        lut_g = np.zeros((3, 256), dtype=np.uint8)
        lut_g[1] = np.arange(256, dtype=np.uint8)
        lut_m = np.zeros((3, 256), dtype=np.uint8)
        lut_m[0] = np.arange(256, dtype=np.uint8)
        lut_m[2] = np.arange(256, dtype=np.uint8)
        tifffile.imwrite(
            ij_path,
            np.zeros((1, 2, 64, 64), dtype=np.uint16),
            imagej=True,
            resolution=(1.0 / 0.5, 1.0 / 0.5),
            metadata={"axes": "ZCYX", "unit": "micron", "Labels": ["GFP-Ch", "Cy5-Ch"], "LUTs": [lut_g, lut_m]},
        )
        sid_ij = reg.open_path(ij_path)
        st_ij = reg.get(sid_ij)
        assert np.allclose(st_ij.attrs["nd2wsi"]["pixel_size_um"], [0.5, 0.5], rtol=1e-5)
        assert [c["label"] for c in st_ij.attrs["omero"]["channels"]] == ["GFP-Ch", "Cy5-Ch"]
        assert [c["color"] for c in st_ij.attrs["omero"]["channels"]] == ["00FF00", "FF00FF"]
    finally:
        reg.close_all(immediate=True)


# ---------------------------------------------------------------------------
# 2. 16-bit Exported ROI .tif Re-Open Roundtrip (Instant, Zero Cache)
# ---------------------------------------------------------------------------


def test_16bit_exported_roi_tiff_reopen_roundtrip_preserves_values_and_calibration(tmp_path: Path):
    src_ome = tmp_path / "source_scan.ome.tif"
    rng = np.random.default_rng(7)
    raw_pixels = rng.integers(50, 45000, size=(2, 200, 240), dtype=np.uint16)
    tifffile.imwrite(
        src_ome,
        raw_pixels,
        ome=True,
        tile=(128, 128),
        photometric="minisblack",
        metadata={
            "axes": "CYX",
            "PhysicalSizeX": 0.25,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.25,
            "PhysicalSizeYUnit": "µm",
            "Channel": {"Name": ["DAPI", "FITC"]},
        },
    )

    roi_dir = tmp_path / "exported_roi_dir"
    roi_dir.mkdir()
    exported_tif = roi_dir / "roi_crop.tif"

    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")
        _, opened = _http_json(f"{base}/api/open", method="POST", payload={"path": str(src_ome)})
        sid_src = opened["sid"]

        status, tiff_bytes, ctype = _http_bytes(
            f"{base}/s/{sid_src}/api/roi?level=0&x=20&y=30&w=96&h=80&format=tiff&c=0,1"
        )
        assert status == 200 and ctype == "image/tiff"
        exported_tif.write_bytes(tiff_bytes)

        t0 = time.perf_counter()
        code, reopened = _http_json(
            f"{base}/api/open", method="POST", payload={"path": str(exported_tif)}
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert code == 200
        assert elapsed_ms < 500.0  # fast in-process HTTP roundtrip without conversion
        sid_roi = reopened["sid"]

        _, roi_info = _http_json(f"{base}/s/{sid_roi}/api/info")
        assert roi_info["width"] == 96 and roi_info["height"] == 80
        assert roi_info["dtype"] == "uint16"
        assert roi_info["calibrated"] is True
        assert np.allclose(roi_info["pixelSizeUm"], [0.25, 0.25], rtol=1e-4)
        assert len(roi_info["channels"]) == 2
        assert not (roi_dir / "nd2wsi").exists()

        st_roi = httpd.registry.get(sid_roi)
        reopened_arr = np.asarray(st_roi.root["0"][:, :, :])
        assert np.array_equal(reopened_arr, raw_pixels[:, 30 : 30 + 80, 20 : 20 + 96])


# ---------------------------------------------------------------------------
# 3. Pyramidal Tiled TIFF Zero-Cache Direct Open (SubIFDs & Multi-Page + SVS)
# ---------------------------------------------------------------------------


def test_pyramidal_tiled_tiff_subifds_and_multipage_zero_cache_direct_open(tmp_path: Path):
    # (A) SubIFD multi-channel uint16 pyramidal OME-TIFF
    pyr_ome = tmp_path / "pyr_subifd.ome.tif"
    base_mc = np.arange(2 * 1024 * 1024, dtype=np.uint32).reshape(2, 1024, 1024) % 50000
    base_mc = base_mc.astype(np.uint16)
    with tifffile.TiffWriter(pyr_ome, bigtiff=True, ome=True) as tw:
        tw.write(
            base_mc,
            subifds=2,
            tile=(256, 256),
            compression="zlib",
            photometric="minisblack",
            metadata={
                "axes": "CYX",
                "PhysicalSizeX": 0.5,
                "PhysicalSizeY": 0.5,
                "Channel": {"Name": ["Ch_Cyan", "Ch_Mag"]},
            },
        )
        tw.write(
            base_mc[:, ::2, ::2],
            subfiletype=1,
            tile=(256, 256),
            compression="zlib",
            photometric="minisblack",
        )
        tw.write(
            base_mc[:, ::4, ::4],
            subfiletype=1,
            tile=(256, 256),
            compression="zlib",
            photometric="minisblack",
        )

    # (B) Multi-page decreasing-resolution RGB pyramidal TIFF without SubIFDs
    pyr_rgb = tmp_path / "pyr_multipage.tif"
    rgb_base = np.zeros((1024, 1024, 3), dtype=np.uint8)
    rgb_base[:, :512, 0] = 220
    rgb_base[:, 512:, 2] = 200
    with tifffile.TiffWriter(pyr_rgb) as tw:
        tw.write(rgb_base, tile=(256, 256), photometric="rgb", resolution=(20000, 20000), resolutionunit="CENTIMETER")
        tw.write(rgb_base[::2, ::2], tile=(256, 256), photometric="rgb")
        tw.write(rgb_base[::4, ::4], tile=(256, 256), photometric="rgb")

    # (C) Ensure .svs still routes through svs.py (Aperio MPP metadata)
    svs_dir = tmp_path / "svs_sub"
    svs_dir.mkdir()
    svs_file = svs_dir / "aperio_check.svs"
    with tifffile.TiffWriter(svs_file) as tw:
        tw.write(
            rgb_base,
            subifds=1,
            tile=(256, 256),
            photometric="rgb",
            description="Aperio Image Library|MPP = 0.499|AppMag = 20",
        )
        tw.write(rgb_base[::2, ::2], subfiletype=1, tile=(256, 256), photometric="rgb")

    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")
        for p, exp_mpp, exp_rgb in ((pyr_ome, 0.5, False), (pyr_rgb, 0.5, True), (svs_file, 0.499, True)):
            code, res = _http_json(f"{base}/api/open", method="POST", payload={"path": str(p)})
            assert code == 200
            sid = res["sid"]
            _, info = _http_json(f"{base}/s/{sid}/api/info")
            assert info["direct"] is True
            assert info["storage"] == "direct"
            assert info["rgb"] is exp_rgb
            assert len(info["levels"]) == 2  # 1024 -> 512
            assert np.allclose(info["pixelSizeUm"], [exp_mpp, exp_mpp], rtol=1e-4)
            st_code, tile_jpg, _ = _http_bytes(f"{base}/s/{sid}/api/tile/1/0/0.jpg")
            assert st_code == 200 and len(tile_jpg) > 100

    assert not (tmp_path / "nd2wsi").exists()


# ---------------------------------------------------------------------------
# 4. 3D Z-Stack .ome.tif Interactive Z Planes, Autofocus & Z-Prefetch
# ---------------------------------------------------------------------------


def test_3d_zstack_ometiff_interactive_z_planes_autofocus_and_prefetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    n_z, n_c, h, w = 5, 2, 96, 128
    stack = np.zeros((n_z, n_c, h, w), dtype=np.uint16)
    for z in range(n_z):
        stack[z, 0] = (z + 1) * 1000
        stack[z, 1] = (z + 1) * 500
    # Make z=3 sharply patterned at 8x8 block scale so 8x-reduced autofocus clearly selects z=3
    yy, xx = np.indices((h, w))
    stack[3, 0] = np.where(((yy // 8) + (xx // 8)) % 2 == 0, 40000, 1000).astype(np.uint16)

    z_ome = tmp_path / "confocal_zstack.ome.tif"
    tifffile.imwrite(
        z_ome,
        stack,
        ome=True,
        photometric="minisblack",
        metadata={
            "axes": "ZCYX",
            "PhysicalSizeX": 0.2,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.2,
            "PhysicalSizeYUnit": "µm",
            "PhysicalSizeZ": 1.25,
            "PhysicalSizeZUnit": "µm",
            "Channel": {"Name": ["DAPI", "GFP"]},
        },
    )

    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")
        code, opened = _http_json(f"{base}/api/open", method="POST", payload={"path": str(z_ome)})
        assert code == 200
        sid = opened["sid"]

        _, info = _http_json(f"{base}/s/{sid}/api/info")
        assert info["kind"] == "plate"
        assert info["plate"]["Z"] == n_z
        assert info["plate"]["T"] == 1
        assert info["plate"]["P"] == 1
        assert np.isclose(info["plate"]["zStepUm"], 1.25)

        # Verify distinct Z planes via pixel endpoint
        _, px_z0 = _http_json(f"{base}/s/{sid}/api/pixel?x=8&y=8&t=0&p=0&z=0")
        _, px_z4 = _http_json(f"{base}/s/{sid}/api/pixel?x=8&y=8&t=0&p=0&z=4")
        assert px_z0["values"][0]["value"] == 1000
        assert px_z4["values"][0]["value"] == 5000

        # Verify autofocus selects z=3
        _, focus = _http_json(f"{base}/s/{sid}/api/plate/focus")
        assert focus["best"][0][0] == 3

        # Verify tile serving triggers prefetch_neighbors(t, p, z)
        plate_src = httpd.registry.get(sid).plate
        prefetch_calls: list[tuple[int, int, int]] = []
        orig_prefetch = plate_src.prefetch_neighbors

        def _spy_prefetch(t: int, p: int, z: int) -> None:
            prefetch_calls.append((t, p, z))
            orig_prefetch(t, p, z)

        monkeypatch.setattr(plate_src, "prefetch_neighbors", _spy_prefetch)
        st_tile, _, _ = _http_bytes(f"{base}/s/{sid}/api/tile/0/0/0.jpg?t=0&p=0&z=2")
        assert st_tile == 200
        assert (0, 0, 2) in prefetch_calls

        # Verify reduced frame endpoint + ?col= override
        st_fr, fr_bytes, _ = _http_bytes(
            f"{base}/s/{sid}/api/plate/frame/0/0/3.png?k=8&c=0&col=FF00FF,"
        )
        assert st_fr == 200 and len(fr_bytes) > 50


# ---------------------------------------------------------------------------
# 5. Multi-Series .lif & Multi-Scene .czi seriesList + /api/select-scene
# ---------------------------------------------------------------------------


def test_multiseries_lif_and_multiscene_czi_series_list_and_select_scene(tmp_path: Path):
    # (A) Heterogeneous 2-series LIF: Series 0 is 2D (80x96, 1ch), Series 1 is 3D Z-stack (64x64, Z=3, 2ch)
    s0_arr = np.full((80, 96), 140, dtype=np.uint8)
    s1_arr = np.zeros((3, 2, 64, 64), dtype=np.uint16)
    for zi in range(3):
        s1_arr[zi, 0] = (zi + 1) * 2000
        s1_arr[zi, 1] = (zi + 1) * 3000

    lif_path = _build_synthetic_lif(
        tmp_path / "hetero_container.lif",
        [
            {
                "name": "Overview_10x",
                "payload": s0_arr.tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="96" BytesInc="1" Length="9.5e-5" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="80" BytesInc="96" Length="7.9e-5" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
                ],
            },
            {
                "name": "Zoom_63x_ZStack",
                "payload": s1_arr.tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="64" BytesInc="2" Length="1.6e-5" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="64" BytesInc="128" Length="1.6e-5" Unit="m"/>',
                    f'<DimensionDescription DimID="3" NumberOfElements="3" BytesInc="{64 * 64 * 4}" Length="4.0e-6" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Cyan" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Magenta" BytesInc="{64 * 64 * 2}"/>',
                ],
            },
        ],
    )

    # (B) Multi-scene CZI: Scene 0 (128x96, 1ch, Z=1), Scene 1 (96x112, 2ch, Z=2)
    czi_path = _build_multiscene_czi(
        tmp_path / "two_regions.czi",
        [
            {
                "s": 0,
                "name": "Cortex_ROI1",
                "planes": {(0, 0, 0): np.full((96, 128), 1111, dtype=np.uint16)},
            },
            {
                "s": 1,
                "name": "Hippocampus_ROI2",
                "planes": {
                    (0, 0, 0): np.full((112, 96), 2222, dtype=np.uint16),
                    (0, 0, 1): np.full((112, 96), 3333, dtype=np.uint16),
                    (0, 1, 0): np.full((112, 96), 4444, dtype=np.uint16),
                    (0, 1, 1): np.full((112, 96), 5555, dtype=np.uint16),
                },
            },
        ],
    )

    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")

        # --- LIF checks ---
        code, lif_open = _http_json(f"{base}/api/open", method="POST", payload={"path": str(lif_path)})
        assert code == 200
        lif_sid0 = lif_open["sid"]
        _, lif_info0 = _http_json(f"{base}/s/{lif_sid0}/api/info")
        assert lif_info0["seriesCount"] == 2
        assert lif_info0["seriesIndex"] == 0
        assert lif_info0["seriesList"] == [
            {
                "index": 0,
                "name": "Overview_10x",
                "width": 96,
                "height": 80,
                "channels": 1,
                "zCount": 1,
                "tCount": 1,
            },
            {
                "index": 1,
                "name": "Zoom_63x_ZStack",
                "width": 64,
                "height": 64,
                "channels": 2,
                "zCount": 3,
                "tCount": 1,
            },
        ]

        # Select series 1 via /api/select-scene
        code, sel_res = _http_json(
            f"{base}/s/{lif_sid0}/api/select-scene", method="POST", payload={"series": 1}
        )
        assert code == 200
        assert sel_res["ok"] is True
        lif_sid1 = sel_res["sid"]
        assert lif_sid1 != lif_sid0
        assert sel_res["url"] == f"/s/{lif_sid1}/"
        assert "Zoom_63x_ZStack" in sel_res["name"]

        _, lif_info1 = _http_json(f"{base}/s/{lif_sid1}/api/info")
        assert lif_info1["seriesCount"] == 2
        assert lif_info1["seriesIndex"] == 1
        assert lif_info1["width"] == 64 and lif_info1["height"] == 64
        assert lif_info1["kind"] == "plate" and lif_info1["plate"]["Z"] == 3
        _, px_lif1_z2 = _http_json(f"{base}/s/{lif_sid1}/api/pixel?x=5&y=5&t=0&p=0&z=2")
        assert [v["value"] for v in px_lif1_z2["values"]] == [6000, 9000]

        # Out-of-range series -> 400
        err_code, _ = _http_json(
            f"{base}/s/{lif_sid0}/api/select-scene", method="POST", payload={"series": 5}
        )
        assert err_code == 400

        # --- CZI checks ---
        code, czi_open = _http_json(f"{base}/api/open", method="POST", payload={"path": str(czi_path)})
        assert code == 200
        czi_sid0 = czi_open["sid"]
        _, czi_info0 = _http_json(f"{base}/s/{czi_sid0}/api/info")
        assert czi_info0["seriesCount"] == 2
        assert czi_info0["seriesIndex"] == 0
        assert [s["name"] for s in czi_info0["seriesList"]] == ["Cortex_ROI1", "Hippocampus_ROI2"]
        assert czi_info0["width"] == 128 and czi_info0["height"] == 96

        # Open CZI scene 1 directly via POST /api/open {"path": ..., "series": 1}
        code, czi_open1 = _http_json(
            f"{base}/api/open", method="POST", payload={"path": str(czi_path), "series": 1}
        )
        assert code == 200
        czi_sid1 = czi_open1["sid"]
        _, czi_info1 = _http_json(f"{base}/s/{czi_sid1}/api/info")
        assert czi_info1["seriesIndex"] == 1
        assert czi_info1["width"] == 96 and czi_info1["height"] == 112
        assert len(czi_info1["channels"]) == 2
        _, px_czi1 = _http_json(f"{base}/s/{czi_sid1}/api/pixel?x=4&y=4&t=0&p=0&z=1")
        assert [v["value"] for v in px_czi1["values"]] == [4444, 5555]


# ---------------------------------------------------------------------------
# 6. nd2Export Dtype Gating (float32 vs uint16 vs Raster vs Missing limnd2)
# ---------------------------------------------------------------------------


def test_nd2_export_gating_and_reason_for_float32_raster_and_uint16(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    f32_path = tmp_path / "lightning_deconv.ome.tif"
    tifffile.imwrite(
        f32_path,
        np.linspace(0.0, 1.0, 64 * 64, dtype=np.float32).reshape(1, 64, 64),
        ome=True,
        metadata={"axes": "CYX", "PhysicalSizeX": 0.1, "PhysicalSizeY": 0.1},
    )
    u16_path = tmp_path / "raw_16bit.ome.tif"
    tifffile.imwrite(u16_path, np.full((64, 64), 1000, dtype=np.uint16))
    png_path = tmp_path / "snapshot.png"
    Image.fromarray(np.zeros((64, 64, 3), dtype=np.uint8)).save(png_path)

    monkeypatch.setattr(server, "_limnd2_available", lambda: True)
    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")

        _, op_f32 = _http_json(f"{base}/api/open", method="POST", payload={"path": str(f32_path)})
        _, info_f32 = _http_json(f"{base}/s/{op_f32['sid']}/api/info")
        assert info_f32["dtype"] == "float32"
        assert info_f32["nd2Export"] is False
        assert "float32" in info_f32["nd2ExportReason"]
        assert "TIFF" in info_f32["nd2ExportReason"]

        _, op_u16 = _http_json(f"{base}/api/open", method="POST", payload={"path": str(u16_path)})
        _, info_u16 = _http_json(f"{base}/s/{op_u16['sid']}/api/info")
        assert info_u16["nd2Export"] is True
        assert info_u16["nd2ExportReason"] == ""

        _, op_png = _http_json(f"{base}/api/open", method="POST", payload={"path": str(png_path)})
        _, info_png = _http_json(f"{base}/s/{op_png['sid']}/api/info")
        assert info_png["nd2Export"] is False
        assert "PNG" in info_png["nd2ExportReason"]

        # When limnd2 is absent, uint16 is also gated with a clear reason
        monkeypatch.setattr(server, "_limnd2_available", lambda: False)
        _, info_no_lim = _http_json(f"{base}/s/{op_u16['sid']}/api/info")
        assert info_no_lim["nd2Export"] is False
        assert "limnd2" in info_no_lim["nd2ExportReason"]


# ---------------------------------------------------------------------------
# 7. UI Settings, Recent Files, Agent Window Isolation & Bounded Thread Pool
# ---------------------------------------------------------------------------


def test_ui_settings_and_recent_files_endpoints_and_agent_isolation(tmp_path: Path):
    user_slide = tmp_path / "user_opened.ome.tif"
    agent_slide = tmp_path / "agent_private.ome.tif"
    tifffile.imwrite(user_slide, np.full((48, 48), 200, dtype=np.uint16))
    tifffile.imwrite(agent_slide, np.full((48, 48), 400, dtype=np.uint16))

    user_session = create_window_session("user", tmp_path / "sessions")
    agent_session = create_window_session("agent", tmp_path / "sessions")

    # 1. User window records recent files and persists UI settings
    with running_server([], port=0, window_session=user_session) as user_httpd:
        ubase = server_url(user_httpd).rstrip("/")

        code, ui0 = _http_json(f"{ubase}/api/ui-settings")
        assert code == 200 and ui0["theme"] == "auto"

        code, ui1 = _http_json(
            f"{ubase}/api/ui-settings",
            method="POST",
            payload={"theme": "dark", "shellToolsExpanded": True, "panels": {"channels": {"x": 24, "y": 60}}},
        )
        assert code == 200
        assert ui1["theme"] == "dark"
        assert ui1["shellToolsExpanded"] is True
        assert ui1["panels"]["channels"] == {"x": 24, "y": 60}

        bad_code, _ = _http_json(
            f"{ubase}/api/ui-settings", method="POST", payload={"theme": "neon"}
        )
        assert bad_code == 400

        # Opening a slide in User window auto-records recent file
        code, opened = _http_json(
            f"{ubase}/api/open", method="POST", payload={"path": str(user_slide)}
        )
        assert code == 200
        sid = opened["sid"]

        # Slide-relative /s/<sid>/api/ui-settings also works
        code, ui_rel = _http_json(f"{ubase}/s/{sid}/api/ui-settings")
        assert code == 200 and ui_rel["theme"] == "dark"

        code, recents = _http_json(f"{ubase}/api/recent-files")
        assert code == 200
        assert len(recents["items"]) == 1
        assert Path(recents["items"][0]["path"]) == user_slide.resolve()
        assert recents["items"][0]["available"] is True

    # 2. Agent window MUST NOT pollute recent files or overwrite user UI settings
    with running_server([], port=0, window_session=agent_session) as agent_httpd:
        abase = server_url(agent_httpd).rstrip("/")

        code, a_open = _http_json(
            f"{abase}/api/open", method="POST", payload={"path": str(agent_slide)}
        )
        assert code == 200
        assert a_open["sid"]

        code, _ = _http_json(
            f"{abase}/api/recent-files", method="POST", payload={"path": str(agent_slide)}
        )
        assert code == 200

        code, _ = _http_json(
            f"{abase}/api/ui-settings",
            method="POST",
            payload={"theme": "light", "shellToolsExpanded": False},
        )
        assert code == 200

    # Verify user's persisted state is completely untouched by the Agent session
    with running_server([], port=0, window_session=user_session) as user_httpd2:
        ubase2 = server_url(user_httpd2).rstrip("/")
        _, ui_after = _http_json(f"{ubase2}/api/ui-settings")
        assert ui_after["theme"] == "dark"
        assert ui_after["shellToolsExpanded"] is True

        _, recents_after = _http_json(f"{ubase2}/api/recent-files")
        paths = [Path(item["path"]) for item in recents_after["items"]]
        assert paths == [user_slide.resolve()]
        assert agent_slide.resolve() not in paths


def test_bounded_http_thread_pool_caps_worker_threads(tmp_path: Path):
    slide = tmp_path / "storm.ome.tif"
    tifffile.imwrite(slide, np.zeros((512, 512), dtype=np.uint16), tile=(128, 128))

    httpd = create_server(slide, port=0)
    assert httpd.max_workers == 16
    peak_http_threads = 0
    lock = threading.Lock()

    with running_server(slide, port=0) as live:
        base = server_url(live).rstrip("/")
        sid = live.registry.default_sid()

        def _fetch_tile(i: int) -> int:
            nonlocal peak_http_threads
            tx, ty = i % 1, 0
            status, body, _ = _http_bytes(f"{base}/s/{sid}/api/tile/0/{tx}/{ty}.jpg")
            active_pool = sum(
                1 for t in threading.enumerate() if t.name.startswith("nd2wsi-http")
            )
            with lock:
                peak_http_threads = max(peak_http_threads, active_pool)
            assert status == 200
            return len(body)

        with concurrent.futures.ThreadPoolExecutor(max_workers=48) as client_pool:
            sizes = list(client_pool.map(_fetch_tile, range(72)))
        assert all(sz > 50 for sz in sizes)
        assert 1 <= peak_http_threads <= 16

    httpd.server_close()
    assert httpd._pool_closed is True


def test_2d_lif_scene_selection_and_homogeneous_grid_exclusion_plus_track2_col_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # 1. Heterogeneous 2D-only LIF (Series 0: 64x64, Series 1: 96x80) -> ensure_cache p=1 path
    lif_2d = _build_synthetic_lif(
        tmp_path / "two_2d_scans.lif",
        [
            {
                "name": "ScanA",
                "payload": np.full((64, 64), 100, dtype=np.uint8).tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="64" BytesInc="1" Length="6.3e-5" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="64" BytesInc="64" Length="6.3e-5" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
                ],
            },
            {
                "name": "ScanB",
                "payload": np.full((80, 96), 210, dtype=np.uint8).tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="96" BytesInc="1" Length="9.5e-5" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="80" BytesInc="96" Length="7.9e-5" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Red" BytesInc="0"/>',
                ],
            },
        ],
    )

    # 2. Homogeneous 2D grid LIF (both series identical 64x64) -> opened as Plate grid -> seriesList == []
    lif_grid = _build_synthetic_lif(
        tmp_path / "homogeneous_grid.lif",
        [
            {
                "name": f"Well_{i}",
                "payload": np.full((64, 64), 50 + i * 20, dtype=np.uint8).tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="64" BytesInc="1" Length="6.3e-5" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="64" BytesInc="64" Length="6.3e-5" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
                ],
            }
            for i in range(2)
        ],
    )

    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")

        _, open_a = _http_json(f"{base}/api/open", method="POST", payload={"path": str(lif_2d)})
        code, sel_b = _http_json(
            f"{base}/s/{open_a['sid']}/api/select-scene", method="POST", payload={"series": 1}
        )
        assert code == 200 and sel_b["ok"] is True
        _, info_b = _http_json(f"{base}/s/{sel_b['sid']}/api/info")
        assert info_b["seriesIndex"] == 1
        assert info_b["width"] == 96 and info_b["height"] == 80
        assert info_b["kind"] == "slide"

        # Homogeneous grid LIF has seriesList == [] and seriesCount == 1
        _, open_grid = _http_json(f"{base}/api/open", method="POST", payload={"path": str(lif_grid)})
        _, info_grid = _http_json(f"{base}/s/{open_grid['sid']}/api/info")
        assert info_grid["kind"] == "plate" and info_grid["plate"]["P"] == 2
        assert info_grid["seriesCount"] == 1 and info_grid["seriesList"] == []

        # Verify Track 2 col= signature forwarding activates automatically when render_tile has col=
        seen_cols: list[str | None] = []
        orig_render_tile = server.render.render_tile

        def _track2_render_tile(
            root, attrs, level, tx, ty, channels, fmt="jpg", win=None, col=None
        ):
            seen_cols.append(col)
            return orig_render_tile(root, attrs, level, tx, ty, channels, fmt, win)

        monkeypatch.setattr(server.render, "render_tile", _track2_render_tile)
        st_code, _, _ = _http_bytes(f"{base}/s/{sel_b['sid']}/api/tile/0/0/0.jpg?col=00FFFF")
        assert st_code == 200
        assert seen_cols == ["00FFFF"]


def test_cross_track_ometiff_roi_export_cli_info_view_and_custom_color_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from nd2wsi import cli, render

    # 1. 3-channel fluorescence OME-TIFF ROI export -> native ometiff.open_direct round-trip
    rng = np.random.default_rng(2026)
    raw3 = rng.integers(0, 50000, size=(3, 96, 128), dtype=np.uint16)
    attrs3 = {
        "nd2wsi": {
            "source": "fluor3.nd2",
            "selection": {"t": 0, "p": 0, "z": 0},
            "levels": [{"path": "0", "width": 128, "height": 96, "downsample": 1}],
            "tile": 64,
            "dtype": "uint16",
            "rgb": False,
            "pixel_size_um": (0.325, 0.325),
        },
        "omero": {
            "channels": [
                {"label": "DAPI", "color": "00FFFF", "window": {"start": 0.0, "end": 40000.0}},
                {"label": "FITC", "color": "00FF00", "window": {"start": 0.0, "end": 40000.0}},
                {"label": "CY3", "color": "FF00FF", "window": {"start": 0.0, "end": 40000.0}},
            ]
        },
    }
    roi_tif = tmp_path / "exported_3ch_fluor.ome.tif"
    render.export_roi_tiff({"0": raw3}, attrs3, roi_tif, 0, 0, 0, 128, 96, [0, 1, 2])

    root_rt, attrs_rt = ometiff.open_direct(roi_tif)
    try:
        assert attrs_rt["nd2wsi"]["rgb"] is False
        assert attrs_rt["nd2wsi"]["pixel_size_um"] == pytest.approx([0.325, 0.325])
        assert [(c["label"], c["color"]) for c in attrs_rt["omero"]["channels"]] == [
            ("DAPI", "00FFFF"),
            ("FITC", "00FF00"),
            ("CY3", "FF00FF"),
        ]
        np.testing.assert_array_equal(root_rt["0"][:, :, :], raw3)
    finally:
        root_rt.close()

    # 2. CLI `nd2wsi info` (--json and human-readable) & `nd2wsi view` direct open routing
    assert cli.main(["info", str(roi_tif), "--json"]) == 0
    info_json = json.loads(capsys.readouterr().out)
    assert info_json["format"] == "OME-TIFF"
    assert info_json["channels"] == 3 and info_json["rgb"] is False

    assert cli.main(["info", str(roi_tif)]) == 0
    info_txt = capsys.readouterr().out
    assert "OME-TIFF" in info_txt and "DAPI (#00FFFF)" in info_txt

    served_stores: list[list[Path]] = []
    monkeypatch.setattr(server, "serve", lambda stores, **kw: served_stores.append(list(stores)))
    assert cli.main(["view", str(roi_tif)]) == 0
    capsys.readouterr()
    assert served_stores == [[roi_tif]]

    # 3. End-to-end HTTP custom `col` compositing + LUT color persistence on exported OME-TIFF
    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")
        code, opened = _http_json(f"{base}/api/open", method="POST", payload={"path": str(roi_tif)})
        assert code == 200
        sid = opened["sid"]

        t_code, t_png, _ = _http_bytes(
            f"{base}/s/{sid}/api/tile/0/0/0.png?c=0&win=0:1&col=FF0000,,"
        )
        assert t_code == 200
        im = np.asarray(Image.open(io.BytesIO(t_png)))
        assert im.shape[:2] == (96, 128)

        lut_code, lut_saved = _http_json(
            f"{base}/s/{sid}/api/lut-settings",
            method="POST",
            payload={
                "format": "nd2wsi-lut-settings/1",
                "luts": [
                    {"lo": 0, "hi": 40000, "gamma": 1.0, "color": "FF3B30"},
                    {"lo": 0, "hi": 40000, "gamma": 1.0},
                    {"lo": 0, "hi": 40000, "gamma": 1.0},
                ],
            },
        )
        assert lut_code == 200 and lut_saved["ok"] is True
        _, lut_loaded = _http_json(f"{base}/s/{sid}/api/lut-settings")
        assert lut_loaded["settings"]["luts"][0]["color"] == "FF3B30"


def test_nis_elements_ometiff_instrument_ref_and_rgb24_colors(tmp_path: Path) -> None:
    nis_xml = """<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2015-01">
  <Instrument ID="Instrument:0">
    <!-- backward compatibility with NIS elements 4.30 -->
    <Objective ID="Objective:0" NominalMagnification="1" CalibratedMagnification="1.0"/>
  </Instrument>
  <Instrument ID="Instrument:1">
    <Objective ID="Objective:1" NominalMagnification="10" Model="Plan Fluor 10x"/>
  </Instrument>
  <Image ID="Image:0" Name="widefield_scan.tif">
    <InstrumentRef ID="Instrument:1"/>
    <Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16"
            SizeX="64" SizeY="48" SizeC="3" SizeZ="1" SizeT="1"
            PhysicalSizeX="0.660367" PhysicalSizeY="0.660367">
      <Channel ID="Channel:0" Name="FITC" Color="2424576"/>
      <Channel ID="Channel:1" Name="TxRed" Color="16711680"/>
      <Channel ID="Channel:2" Name="DAPI" Color="158463"/>
      <TiffData/>
    </Pixels>
  </Image>
</OME>"""
    records = ometiff._parse_ome_xml(nis_xml)
    assert len(records) == 1
    rec = records[0]
    assert rec["magnification"] == pytest.approx(10.0)
    assert rec["pixel_size_um"] == pytest.approx((0.660367, 0.660367))
    assert [(ch["name"], ch["color"]) for ch in rec["channels"]] == [
        ("FITC", (36, 255, 0)),
        ("TxRed", (255, 0, 0)),
        ("DAPI", (2, 106, 255)),
    ]

    # Standard signed 32-bit RGBA OME-XML colors and ObjectiveSettings override remain intact
    std_xml = """<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">
  <Instrument ID="Instrument:0">
    <Objective ID="Objective:0" NominalMagnification="20"/>
    <Objective ID="Objective:1" NominalMagnification="63"/>
  </Instrument>
  <Image ID="Image:0" Name="confocal">
    <InstrumentRef ID="Instrument:0"/>
    <ObjectiveSettings ID="Objective:1"/>
    <Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16"
            SizeX="32" SizeY="32" SizeC="3" SizeZ="1" SizeT="1">
      <Channel ID="Channel:0" Name="Red" Color="-16776961"/>
      <Channel ID="Channel:1" Name="Green" Color="16711935"/>
      <Channel ID="Channel:2" Name="Blue" Color="65535"/>
    </Pixels>
  </Image>
</OME>"""
    std_rec = ometiff._parse_ome_xml(std_xml)[0]
    assert std_rec["magnification"] == pytest.approx(63.0)
    assert [ch["color"] for ch in std_rec["channels"]] == [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
    ]


def test_multipoint_ometiff_single_image_xml_expands_trailing_ifds(tmp_path: Path) -> None:
    mp_tif = tmp_path / "multipoint_nis_export.tif"
    ome_xml = """<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2015-01">
  <Instrument ID="Instrument:0"><Objective ID="Objective:0" NominalMagnification="1"/></Instrument>
  <Instrument ID="Instrument:1"><Objective ID="Objective:1" NominalMagnification="10"/></Instrument>
  <Image ID="Image:0" Name="multipoint.tif">
    <InstrumentRef ID="Instrument:1"/>
    <Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16"
            SizeX="64" SizeY="48" SizeC="3" SizeZ="1" SizeT="1"
            PhysicalSizeX="0.65" PhysicalSizeY="0.65">
      <Channel ID="Channel:0" Name="FITC" Color="2424576"/>
      <Channel ID="Channel:1" Name="TxRed" Color="16711680"/>
      <Channel ID="Channel:2" Name="DAPI" Color="158463"/>
      <TiffData/>
    </Pixels>
  </Image>
</OME>"""
    with tifffile.TiffWriter(str(mp_tif), ome=False) as tw:
        for pos_idx in range(3):
            for ch_idx in range(3):
                plane = np.full((48, 64), (pos_idx + 1) * 1000 + ch_idx * 100, dtype=np.uint16)
                desc = ome_xml if (pos_idx == 0 and ch_idx == 0) else None
                tw.write(plane, photometric="minisblack", description=desc, metadata=None)

    scenes = ometiff.inspect_ometiff_series(mp_tif)
    assert len(scenes) == 3
    assert [s["name"] for s in scenes] == ["Position 1", "Position 2", "Position 3"]

    with running_server([], port=0) as httpd:
        base = server_url(httpd).rstrip("/")
        code, opened = _http_json(f"{base}/api/open", method="POST", payload={"path": str(mp_tif)})
        assert code == 200
        sid0 = opened["sid"]
        _, info0 = _http_json(f"{base}/s/{sid0}/api/inspect")
        assert info0["seriesCount"] == 3
        assert info0["objective_magnification"] == pytest.approx(10.0)
        assert [(c["label"], c["color"]) for c in info0["channels"]] == [
            ("FITC", "24FF00"),
            ("TxRed", "FF0000"),
            ("DAPI", "026AFF"),
        ]
        _, pix0 = _http_json(f"{base}/s/{sid0}/api/pixel?x=8&y=8")
        assert [v["value"] for v in pix0["values"]] == [1000, 1100, 1200]

        sc_code, sc_res = _http_json(
            f"{base}/s/{sid0}/api/select-scene", method="POST", payload={"series": 2}
        )
        assert sc_code == 200
        sid2 = sc_res["sid"]
        _, pix = _http_json(f"{base}/s/{sid2}/api/pixel?x=8&y=8")
        assert [v["value"] for v in pix["values"]] == [3000, 3100, 3200]

    root_lazy0, _ = ometiff.open_direct(mp_tif, series=0, eager_max_bytes=1)
    try:
        assert root_lazy0["0"][:, 8:9, 8:9].ravel().tolist() == [1000, 1100, 1200]
    finally:
        root_lazy0.close()

    root_lazy2, _ = ometiff.open_direct(mp_tif, series=2, eager_max_bytes=1)
    try:
        assert root_lazy2["0"][:, 8:9, 8:9].ravel().tolist() == [3000, 3100, 3200]
    finally:
        root_lazy2.close()


def test_czi_startc_and_disabled_track_channel_mapping() -> None:
    import xml.etree.ElementTree as ET

    from nd2wsi.czi import _extract_czi_metadata

    # 1. Duplicate Id="Channel:1" disambiguated via StartC="0" / StartC="1"
    xml_startc = ET.fromstring(
        """<?xml version="1.0" encoding="utf-8"?>
        <ImageDocument><Metadata>
          <DisplaySetting><Channels>
            <Channel Id="Channel:1" Name="Brightfield" StartC="0">
              <ShortName>RhodB</ShortName>
            </Channel>
            <Channel Id="Channel:1" Name="C2" StartC="1">
              <DyeName>Dye2</DyeName>
            </Channel>
          </Channels></DisplaySetting>
        </Metadata></ImageDocument>"""
    )
    _, _, _, chs_sc = _extract_czi_metadata(xml_startc, 2, False, c_values=[0, 1])
    assert [c.name for c in chs_sc] == ["RhodB", "C2"]

    # 2. Disabled middle ZEN track: 3 XML channels (Channel:0, Channel:1, Channel:3) for SubBlocks [0, 1, 2]
    xml_axioscan = ET.fromstring(
        """<?xml version="1.0" encoding="utf-8"?>
        <ImageDocument><Metadata>
          <Information><Image><Dimensions><Channels>
            <Channel Id="Channel:0" Name="DNA"><Color>#FF0000FF</Color></Channel>
            <Channel Id="Channel:1" Name="AF488"><Color>#FFFFFFFF</Color></Channel>
            <Channel Id="Channel:3" Name="WGA"><Color>#FFFF00FF</Color></Channel>
          </Channels></Dimensions></Image></Information>
          <DisplaySetting><Channels>
            <Channel Id="Channel:0" Name="DNA"><ShortName>H3258</ShortName></Channel>
            <Channel Id="Channel:1" Name="AF488"><ShortName>EGFP</ShortName></Channel>
            <Channel Id="Channel:3" Name="WGA"><ShortName>AF647</ShortName></Channel>
          </Channels></DisplaySetting>
        </Metadata></ImageDocument>"""
    )
    _, _, _, chs_ax = _extract_czi_metadata(xml_axioscan, 3, False, c_values=[0, 1, 2])
    assert [(c.name, c.color) for c in chs_ax] == [
        ("H3258", (0, 0, 255)),
        ("EGFP", (255, 255, 255)),
        ("AF647", (255, 0, 255)),
    ]


