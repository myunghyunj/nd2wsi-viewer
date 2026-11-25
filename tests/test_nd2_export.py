"""ND2 export round-trips (need limnd2, from Laboratory Imaging's index)."""
from pathlib import Path

import numpy as np
import pytest
from http_fixture import running_server
from nd2_fixture import write_synthetic_nd2

from nd2wsi.server import server_url

pytest.importorskip("limnd2")
nd2 = pytest.importorskip("nd2")


@pytest.fixture()
def fluor_nd2(tmp_path):
    """A small 2-channel uint16 ND2 written with limnd2 (the fixture writer)."""
    rng = np.random.default_rng(11)
    img = rng.integers(0, 4000, size=(700, 900, 2), dtype=np.uint16)  # (Y, X, C)
    channels = [("CY5", "#FF0000"), ("DAPI", "#0000FF")]
    path = write_synthetic_nd2(tmp_path / "fluor.nd2", img, channels, pixel_um=0.66)
    return path, img


def test_crop_nd2_to_nd2_roundtrip(fluor_nd2, tmp_path):
    from nd2wsi.export_nd2 import crop_nd2_to_nd2

    src_path, img = fluor_nd2
    out = tmp_path / "roi.nd2"
    # unaligned, odd-sized region spanning several 512-tiles
    res = crop_nd2_to_nd2(src_path, out, x=123, y=77, w=601, h=555)
    assert (res["w"], res["h"]) == (601, 555)

    with nd2.ND2File(str(out)) as f:
        back = np.moveaxis(np.array(f.read_frame(0)), 0, -1)  # (Y, X, C)
        assert np.array_equal(back, img[77 : 77 + 555, 123 : 123 + 601])
        assert f.voxel_size().x == pytest.approx(0.66)
        chs = f.metadata.channels
        assert [c.channel.name for c in chs] == ["CY5", "DAPI"]
        assert (chs[0].channel.color.r, chs[0].channel.color.g) == (255, 0)
        assert (f.attributes.compressionType or "none") == "none"


def test_crop_channel_subset(fluor_nd2, tmp_path):
    from nd2wsi.export_nd2 import crop_nd2_to_nd2

    src_path, img = fluor_nd2
    out = tmp_path / "roi_c1.nd2"
    crop_nd2_to_nd2(src_path, out, x=10, y=20, w=100, h=80, channels=[1])
    with nd2.ND2File(str(out)) as f:
        back = np.array(f.read_frame(0))
        assert back.shape == (80, 100)  # single channel -> plain 2D frame
        assert np.array_equal(back, img[20:100, 10:110, 1])
        assert [c.channel.name for c in f.metadata.channels] == ["DAPI"]


def test_three_channel_roundtrip(tmp_path):
    """Files are not always 2-channel: full 3-channel convert -> nd2 export."""
    from nd2wsi.convert import convert, open_store
    from nd2wsi.export_nd2 import export_roi_nd2
    from nd2wsi.render import compute_histograms

    rng = np.random.default_rng(5)
    img = rng.integers(0, 3000, size=(600, 800, 3), dtype=np.uint16)
    channels = [("CY5", "FF0000"), ("FITC", "00FF00"), ("DAPI", "0000FF")]
    src = write_synthetic_nd2(tmp_path / "three.nd2", img, channels, pixel_um=0.33,
                              magnification=None)

    with nd2.ND2File(str(src)) as f:
        assert f.sizes.get("C") == 3 and not f.is_rgb  # channels, not RGB comps

    store = tmp_path / "three.ome.zarr"
    convert(src, store, progress=False)
    root, sattrs = open_store(store)
    assert root["0"].shape == (3, 600, 800)
    assert [c["label"] for c in sattrs["omero"]["channels"]] == ["CY5", "FITC", "DAPI"]
    assert len(compute_histograms(root, sattrs, min_pixels=1000)) == 3

    out = tmp_path / "three_roi.nd2"
    export_roi_nd2(root, sattrs, out, level=0, x=101, y=53, w=333, h=222,
                   channels=[0, 1, 2])
    with nd2.ND2File(str(out)) as f:
        back = np.moveaxis(np.array(f.read_frame(0)), 0, -1)
        assert np.array_equal(back, img[53 : 53 + 222, 101 : 101 + 333])
        assert [c.channel.name for c in f.metadata.channels] == ["CY5", "FITC", "DAPI"]


def test_annotation_sidecar_roundtrip(fluor_nd2, tmp_path):
    """GET/POST /api/annotations persists a JSON sidecar next to the store."""
    import json
    import urllib.request

    from nd2wsi.convert import convert

    src_path, _ = fluor_nd2
    store = tmp_path / "ann.ome.zarr"
    convert(src_path, store, progress=False)
    with running_server(store) as httpd:
        base = server_url(httpd).rstrip("/")
        first = json.loads(urllib.request.urlopen(base + "/api/annotations").read())
        assert first["items"] == []
        reported_path = Path(first["path"])
        assert reported_path.parts[-3:] == (
            "nd2wsi", "annotations", "annotations_fluor.nd2--t0-p0-z0.json",
        )

        items = [
            {"id": "a1", "type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0,
             "text": "", "color": "#ffd60a"},
            {"id": "a2", "type": "pin", "x": 50, "y": 60, "text": "hello"},
        ]
        req = urllib.request.Request(
            base + "/api/annotations",
            data=json.dumps({"items": items}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req).read())
        assert resp["ok"] and resp["count"] == 2

        sidecar = (
            tmp_path
            / "nd2wsi"
            / "annotations"
            / "annotations_fluor.nd2--t0-p0-z0.json"
        )
        assert sidecar.exists()
        assert reported_path.samefile(sidecar)
        on_disk = json.loads(sidecar.read_text())
        assert on_disk["items"] == items
        assert on_disk["format"] == "nd2wsi-annotations/2"
        assert on_disk["coordinate_space"] == "level-0-pixels"
        assert on_disk["source"]["name"] == "fluor.nd2"
        assert on_disk["source"]["width"] == 900
        assert on_disk["selection"] == {"t": 0, "p": 0, "z": 0}
        assert on_disk["calibration"]["status"] == "calibrated"
        assert abs(on_disk["calibration"]["x_um_per_px"] - 0.66) < 1e-6

        back = json.loads(urllib.request.urlopen(base + "/api/annotations").read())
        assert back["items"] == items  # auto-found on the next open


def test_store_roi_export_matches_source(fluor_nd2, tmp_path):
    """convert -> export_roi_nd2 -> identical pixels + metadata carried over."""
    from nd2wsi.convert import convert, open_store
    from nd2wsi.export_nd2 import export_roi_nd2

    src_path, img = fluor_nd2
    store = tmp_path / "fluor.ome.zarr"
    convert(src_path, store, progress=False)
    root, attrs = open_store(store)
    assert attrs["nd2wsi"]["objective_magnification"] == pytest.approx(20.0)

    out = tmp_path / "store_roi.nd2"
    export_roi_nd2(root, attrs, out, level=0, x=333, y=41, w=512, h=300,
                   channels=[0, 1])
    with nd2.ND2File(str(out)) as f:
        back = np.moveaxis(np.array(f.read_frame(0)), 0, -1)
        assert np.array_equal(back, img[41 : 41 + 300, 333 : 333 + 512])
        assert f.voxel_size().x == pytest.approx(0.66)


def test_multi_slide_registry(fluor_nd2, tmp_path):
    """Two slides share one server: /api/slides, /s/<sid>/ routes, close."""
    import json
    import urllib.request

    from nd2wsi.convert import convert

    src_path, _ = fluor_nd2
    s1 = tmp_path / "one.ome.zarr"
    s2 = tmp_path / "two.ome.zarr"
    convert(src_path, s1, progress=False)
    convert(src_path, s2, progress=False)
    with running_server([s1, s2]) as httpd:
        base = server_url(httpd).rstrip("/")
        d = json.loads(urllib.request.urlopen(base + "/api/slides").read())
        assert len(d["slides"]) == 2
        sids = [s["sid"] for s in d["slides"]]
        for sid in sids:
            info = json.loads(
                urllib.request.urlopen(f"{base}/s/{sid}/api/info").read()
            )
            assert info["width"] == 900
        # bare /api/* keeps addressing the first slide
        bare = json.loads(urllib.request.urlopen(base + "/api/info").read())
        assert bare["width"] == 900
        # shell page at the root
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "tabbar" in page
        # close one
        req = urllib.request.Request(
            base + "/api/close",
            data=json.dumps({"sid": sids[1]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req).read())
        assert len(resp["slides"]) == 1


def test_export_progress_endpoint(fluor_nd2, tmp_path):
    """A jobbed export leaves 100 percent and a done state behind."""
    import json
    import urllib.request

    from nd2wsi.convert import convert

    src_path, _ = fluor_nd2
    store = tmp_path / "prog.ome.zarr"
    convert(src_path, store, progress=False)
    with running_server(store) as httpd:
        base = server_url(httpd).rstrip("/")
        body = urllib.request.urlopen(
            base + "/api/roi?level=0&x=0&y=0&w=300&h=200&format=tiff&job=t-1", timeout=60
        ).read()
        assert len(body) > 1000
        prog = json.loads(
            urllib.request.urlopen(base + "/api/roi/progress?job=t-1").read()
        )
        assert prog["state"] == "done" and prog["pct"] == 100
        unknown = json.loads(
            urllib.request.urlopen(base + "/api/roi/progress?job=nope").read()
        )
        assert unknown["state"] == "unknown"


def test_convert_progress_and_explicit_store_is_not_trashable(fluor_nd2, tmp_path):
    """Progress works, but a user-supplied OME-Zarr is never treated as cache."""
    import json
    import urllib.request

    from nd2wsi.convert import convert

    src_path, _ = fluor_nd2
    store = tmp_path / "prog.ome.zarr"
    seen = []
    convert(src_path, store, progress=False, on_progress=seen.append)
    assert seen and seen[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in seen)
    assert seen == sorted(seen)  # monotonic

    with running_server(store) as httpd:
        base = server_url(httpd).rstrip("/")
        sid = json.loads(
            urllib.request.urlopen(base + "/api/slides").read()
        )["slides"][0]["sid"]
        info = json.loads(urllib.request.urlopen(base + "/api/info").read())
        assert info["trashable"] is False
        req = urllib.request.Request(
            base + "/api/trash",
            data=json.dumps({"sid": sid}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected explicit store deletion to be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert "no cache managed" in e.read().decode()
        assert store.exists()  # user-owned portable store remains intact
        assert src_path.exists()
        # trashing an unknown sid is a clean 404
        req2 = urllib.request.Request(
            base + "/api/trash",
            data=json.dumps({"sid": "deadbeef"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req2)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404


def test_open_reports_convert_progress(fluor_nd2, tmp_path):
    """POST /api/open with a job id exposes converting->done via the progress route."""
    import json
    import shutil
    import urllib.request

    from nd2wsi.convert import convert

    src_path, _ = fluor_nd2
    # server needs at least one slide; give it a throwaway store
    seed = tmp_path / "seed.ome.zarr"
    convert(src_path, seed, progress=False)
    fresh = tmp_path / "fresh.nd2"
    shutil.copy(src_path, fresh)

    with running_server(seed) as httpd:
        base = server_url(httpd).rstrip("/")
        req = urllib.request.Request(
            base + "/api/open",
            data=json.dumps({"path": str(fresh), "job": "testjob1"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req).read())
        assert resp["sid"] and len(resp["slides"]) == 2
        prog = json.loads(
            urllib.request.urlopen(base + "/api/roi/progress?job=testjob1").read()
        )
        assert prog["state"] == "done" and prog["pct"] == 100
        from nd2wsi.cache import cache_container, container_store, read_manifest

        container = cache_container(tmp_path / "fresh.nd2")
        assert container_store(container).exists()
        manifest = read_manifest(container)
        # an eligible uncompressed ND2 now gets a compact overview cache
        assert manifest and manifest["complete"] and manifest["kind"] == "overview"


def test_failed_write_preserves_the_existing_output(fluor_nd2, tmp_path):
    """The old writer deleted the destination before writing; a failure
    then left neither the old file nor a valid new one."""
    from nd2wsi.export_nd2 import write_nd2

    out = tmp_path / "keep.nd2"
    out.write_bytes(b"precious previous export")

    def bomb(x, y, w, h):
        raise RuntimeError("disk on fire")

    with pytest.raises(RuntimeError, match="disk on fire"):
        write_nd2(out, bomb, height=100, width=100, dtype=np.dtype(np.uint16),
                  pixel_size_um=0.5, planes=[{"name": "A", "color": "FF0000"}])
    assert out.read_bytes() == b"precious previous export"
    assert not list(tmp_path.glob("*.partial-*"))


def test_multichannel_ome_tiff_writes_little_endian_multi_ifd_and_roundtrips_via_ometiff(tmp_path):
    """B5: Non-RGB multi-channel OME-TIFF uses C grayscale IFDs, BigEndian=false, and opens via nd2wsi.ometiff."""
    import xml.etree.ElementTree as ET

    import tifffile

    from nd2wsi import ometiff, render

    ome_ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
    rng = np.random.default_rng(73)
    raw = rng.integers(0, 50000, size=(3, 150, 170), dtype=np.uint16)
    attrs = {
        "nd2wsi": {
            "source": "multi_ifd.nd2",
            "selection": {"t": 0, "p": 0, "z": 0},
            "levels": [{"path": "0", "width": 170, "height": 150, "downsample": 1}],
            "tile": 64,
            "dtype": "uint16",
            "rgb": False,
            "pixel_size_um": [0.5, 0.25],
        },
        "omero": {
            "channels": [
                {"label": "DAPI", "color": "0000FF", "window": {"start": 0.0, "end": 50000.0}},
                {"label": "FITC", "color": "00FF00", "window": {"start": 0.0, "end": 50000.0}},
                {"label": "CY5", "color": "FF0000", "window": {"start": 0.0, "end": 50000.0}},
            ]
        },
    }
    progress: list[float] = []
    out = tmp_path / "roi_multi.ome.tif"
    render.export_roi_tiff(
        {"0": raw}, attrs, out, 0, 7, 5, 140, 130, [0, 2, 1], on_progress=progress.append
    )
    assert progress and progress[-1] == 1.0
    assert progress == sorted(progress)
    assert all(0.0 < p <= 1.0 for p in progress)

    with tifffile.TiffFile(out) as tf:
        assert tf.byteorder == "<"
        assert tf.is_ome is True
        assert tf.is_shaped is False
        assert len(tf.series) == 1
        assert tf.series[0].kind == "ome"
        assert tf.series[0].axes == "CYX"
        assert len(tf.pages) == 3
        for idx, raw_page in enumerate(tf.pages):
            page = raw_page.aspage()
            assert page.shape == (130, 140)
            assert page.samplesperpixel == 1
            assert page.is_tiled is True
            assert page.tags["ResolutionUnit"].value == 3  # CENTIMETER
            if idx == 0:
                assert page.description.startswith("<?xml")
            else:
                assert "ImageDescription" not in page.tags
        px_el = ET.fromstring(tf.ome_metadata).find(".//ome:Pixels", ome_ns)
        assert px_el is not None
        assert px_el.attrib["BigEndian"] == "false"
        assert px_el.attrib["SizeC"] == "3"
        assert px_el.attrib["Interleaved"] == "false"
        ch_els = px_el.findall("ome:Channel", ome_ns)
        assert [(c.attrib["ID"], c.attrib["SamplesPerPixel"], c.attrib["Name"]) for c in ch_els] == [
            ("Channel:0:0", "1", "DAPI"),
            ("Channel:0:1", "1", "CY5"),
            ("Channel:0:2", "1", "FITC"),
        ]
        td_els = px_el.findall("ome:TiffData", ome_ns)
        assert [
            (td.attrib["FirstC"], td.attrib["FirstZ"], td.attrib["FirstT"], td.attrib["IFD"], td.attrib["PlaneCount"])
            for td in td_els
        ] == [
            ("0", "0", "0", "0", "1"),
            ("1", "0", "0", "1", "1"),
            ("2", "0", "0", "2", "1"),
        ]
        np.testing.assert_array_equal(tf.asarray(), raw[[0, 2, 1], 5:135, 7:147])

    # Round-trip through nd2wsi.ometiff.open_direct
    root_rt, attrs_rt = ometiff.open_direct(out)
    try:
        assert attrs_rt["nd2wsi"]["rgb"] is False
        assert attrs_rt["nd2wsi"]["pixel_size_um"] == pytest.approx([0.5, 0.25])
        assert [ch["label"] for ch in attrs_rt["omero"]["channels"]] == ["DAPI", "CY5", "FITC"]
        np.testing.assert_array_equal(root_rt["0"][:, :, :], raw[[0, 2, 1], 5:135, 7:147])
    finally:
        root_rt.close()

    # RGB single-IFD export also declares BigEndian="false" and little-endian byteorder
    rgb_raw = rng.integers(0, 255, size=(3, 80, 90), dtype=np.uint8)
    rgb_attrs = {
        "nd2wsi": {
            "source": "rgb.svs",
            "selection": {"t": 0, "p": 0, "z": 0},
            "levels": [{"path": "0", "width": 90, "height": 80, "downsample": 1}],
            "tile": 64,
            "dtype": "uint8",
            "rgb": True,
            "pixel_size_um": [0.5, 0.5],
        },
        "omero": {
            "channels": [
                {"label": name, "color": col, "window": {"start": 0.0, "end": 255.0}}
                for name, col in (("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF"))
            ]
        },
    }
    rgb_out = tmp_path / "roi_rgb.ome.tif"
    render.export_roi_tiff({"0": rgb_raw}, rgb_attrs, rgb_out, 0, 0, 0, 90, 80, [0, 1, 2])
    with tifffile.TiffFile(rgb_out) as tf_rgb:
        assert tf_rgb.byteorder == "<"
        assert len(tf_rgb.pages) == 1
        px_rgb = ET.fromstring(tf_rgb.ome_metadata).find(".//ome:Pixels", ome_ns)
        assert px_rgb is not None
        assert px_rgb.attrib["BigEndian"] == "false"
        assert px_rgb.attrib["Interleaved"] == "true"


def test_pre_stream_roi_errors_record_job_error_state(fluor_nd2, tmp_path):
    """B7: Pre-stream 400 and 409 /api/roi rejections record error state in EXPORT_JOBS."""
    import json
    import urllib.error
    import urllib.request

    from nd2wsi.convert import convert
    from nd2wsi.region_export import parse_job_id
    from nd2wsi.server import _job_get

    assert parse_job_id({"job": ["valid_1-a"]}) == "valid_1-a"
    assert parse_job_id({"job": ["bad/job!"]}) is None
    assert parse_job_id({}) is None

    src_path, _ = fluor_nd2
    store = tmp_path / "job_err.ome.zarr"
    convert(src_path, store, progress=False)
    with running_server(store) as httpd:
        base = server_url(httpd).rstrip("/")
        # 400 pre-stream validation error (level out of range)
        with pytest.raises(urllib.error.HTTPError) as err_400:
            urllib.request.urlopen(
                base + "/api/roi?level=99&x=0&y=0&w=64&h=64&format=tiff&job=pre400"
            )
        assert err_400.value.code == 400
        prog_400 = json.loads(
            urllib.request.urlopen(base + "/api/roi/progress?job=pre400").read()
        )
        assert prog_400["state"] == "error"
        assert "level 99 out of range" in prog_400["error"]

        # 409 pre-stream busy lease closed
        st = next(iter(httpd.registry.slides.values()))
        st.busy.close()
        try:
            with pytest.raises(urllib.error.HTTPError) as err_409:
                urllib.request.urlopen(
                    base + "/api/roi?level=0&x=0&y=0&w=64&h=64&format=tiff&job=pre409"
                )
            assert err_409.value.code == 409
            prog_409 = _job_get("pre409")
            assert prog_409["state"] == "error"
            assert "closed" in prog_409["error"]
        finally:
            st.busy.reopen()
