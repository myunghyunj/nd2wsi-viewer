"""ND2 export round-trips (need limnd2, from Laboratory Imaging's index)."""
import numpy as np
import pytest

limnd2 = pytest.importorskip("limnd2")
nd2 = pytest.importorskip("nd2")


@pytest.fixture()
def fluor_nd2(tmp_path):
    """A small 2-channel uint16 ND2 written with limnd2 (the fixture writer)."""
    rng = np.random.default_rng(11)
    img = rng.integers(0, 4000, size=(700, 900, 2), dtype=np.uint16)  # (Y, X, C)
    path = tmp_path / "fluor.nd2"
    attrs = limnd2.ImageAttributes.create(
        width=900, height=700, component_count=2, bits=16, sequence_count=1
    )
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = attrs
        f.setImage(0, img)
        mf = limnd2.MetadataFactory(
            objective_magnification=20.0, pixel_calibration=0.66
        )
        mf.addPlane(name="CY5", color="#FF0000")
        mf.addPlane(name="DAPI", color="#0000FF")
        f.pictureMetadata = mf.createMetadata()
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
    src = tmp_path / "three.nd2"
    attrs = limnd2.ImageAttributes.create(
        width=800, height=600, component_count=3, bits=16, sequence_count=1
    )
    with limnd2.Nd2Writer(str(src)) as f:
        f.imageAttributes = attrs
        f.setImage(0, img)
        mf = limnd2.MetadataFactory(pixel_calibration=0.33)
        for name, color in [("CY5", "FF0000"), ("FITC", "00FF00"), ("DAPI", "0000FF")]:
            mf.addPlane(name=name, color=color)
        f.pictureMetadata = mf.createMetadata()

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
    import threading
    import urllib.request

    from nd2wsi.convert import convert
    from nd2wsi.server import create_server

    src_path, _ = fluor_nd2
    store = tmp_path / "ann.ome.zarr"
    convert(src_path, store, progress=False)
    httpd = create_server(store, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        first = json.loads(urllib.request.urlopen(base + "/api/annotations").read())
        assert first["items"] == [] and first["path"].endswith("annotations_fluor.json")

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

        sidecar = tmp_path / "annotations_fluor.json"
        assert sidecar.exists()
        on_disk = json.loads(sidecar.read_text())
        assert on_disk["items"] == items and on_disk["source"] == "fluor.nd2"

        back = json.loads(urllib.request.urlopen(base + "/api/annotations").read())
        assert back["items"] == items  # auto-found on the next open
    finally:
        httpd.shutdown()


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
    import threading
    import urllib.request

    from nd2wsi.convert import convert
    from nd2wsi.server import create_server

    src_path, _ = fluor_nd2
    s1 = tmp_path / "one.ome.zarr"
    s2 = tmp_path / "two.ome.zarr"
    convert(src_path, s1, progress=False)
    convert(src_path, s2, progress=False)
    httpd = create_server([s1, s2], port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
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
    finally:
        httpd.shutdown()


def test_export_progress_endpoint(fluor_nd2, tmp_path):
    """A jobbed export leaves 100 percent and a done state behind."""
    import json
    import threading
    import urllib.request

    from nd2wsi.convert import convert
    from nd2wsi.server import create_server

    src_path, _ = fluor_nd2
    store = tmp_path / "prog.ome.zarr"
    convert(src_path, store, progress=False)
    httpd = create_server(store, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
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
    finally:
        httpd.shutdown()
