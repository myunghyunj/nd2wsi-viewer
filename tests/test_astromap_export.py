"""AstroMap geometry, bounded reads, file lifetime and HTTP window isolation."""
from __future__ import annotations

import copy
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from nd2wsi import astromap_export as am
from nd2wsi.server import ViewerState, _frame_args, create_server


def make_state(*, width=80, height=60, rgb=False, root=None, levels=None, plate=None):
    levels = levels or [{"path": "0", "width": width, "height": height}]
    channels = [
        {"label": name, "color": color, "window": {"start": 0, "end": 255}}
        for name, color in zip(("R", "G", "B"), ("FF0000", "00FF00", "0000FF"))
    ]
    attrs = {"nd2wsi": {"source": "example.nd2", "levels": levels, "rgb": rgb},
             "omero": {"channels": channels}}
    if root is None:
        root = {levels[0]["path"]: np.stack([
            np.full((height, width), value, np.uint8) for value in (40, 100, 160)
        ])}
    return ViewerState(root, attrs, generation="current", plate=plate)


def payload(**changes):
    return {"generation": "current", "rois": [{"x": 5, "y": 6, "w": 12, "h": 9, "note": "A"}],
            "channels": [0, 1, 2], "window": None, "frame": None} | changes


def prepare(st=None, **changes):
    return am.prepare_astromap(st or make_state(), payload(**changes), resolve_frame=_frame_args)


def test_target_size_and_inclusive_coordinates_match_astromapper_without_mutation():
    assert am.output_size(40_000, 20_000) == (8000, 4000)
    assert am.output_size(300, 700) == (300, 700)
    assert am.output_size(8001, 4001) == (8000, 4000)
    assert am.output_size(4001, 8001) == (4000, 8000)
    rois = [{"x": 10_000, "y": 5000, "w": 2000, "h": 1000, "note": "capsule"},
            {"x": 0, "y": 0, "w": 1, "h": 1, "note": "skip", "enabled": False},
            {"x": 39_999, "y": 19_999, "w": 100, "h": 100, "note": ""}]
    before = copy.deepcopy(rois)
    boxes = am.convert_boxes(rois, (40_000, 20_000), (8000, 4000))
    assert am.coordinates_text(boxes) == (
        "INDEX\tX1\tY1\tX2\tY2\tNOTE\n"
        "1\t2000\t1000\t2399\t1199\tcapsule\n"
        "2\t7999\t3999\t7999\t3999\t3\n"
    )
    assert rois == before
    assert am.convert_boxes(rois, (40_000, 20_000), (8000, 4000)) == boxes


def test_notes_are_unicode_single_tsv_fields_and_partial_bounds_clip_correctly():
    boxes = am.convert_boxes([{"x": -3, "y": -2, "w": 8, "h": 7, "note": "조직\tA\r\nB"}],
                             (10, 10), (10, 10))
    assert am.coordinates_text(boxes).splitlines()[1].split("\t") == [
        "1", "0", "0", "4", "4", "조직 A  B"
    ]


@pytest.mark.parametrize("change", [
    {"rois": []}, {"channels": [True]}, {"channels": [1, 1]}, {"channels": [3]},
    {"rois": [{"x": float("nan"), "y": 1, "w": 1, "h": 1}]},
    {"rois": [{"x": 1, "y": 1, "w": 0, "h": 1}]},
    {"rois": [{"x": 200, "y": 1, "w": 10, "h": 1}]},
    {"rois": [{"enabled": False}]}, {"frame": {"t": 0, "p": 0, "z": 0}},
])
def test_invalid_request_is_rejected_before_rendering(change):
    with pytest.raises(am.AstromapError):
        prepare(**change)


def test_stale_generation_rejected_before_reading_or_allocating():
    with pytest.raises(am.AstromapConflict, match="pyramid changed"):
        prepare(generation="old")


def test_repeated_exports_preserve_source_and_coordinates_and_make_exact_pair(tmp_path):
    st = make_state()
    original = st.root["0"].copy()
    plan = prepare(st)
    bundles = []
    for index in range(2):
        folder = tmp_path / str(index)
        folder.mkdir()
        path = plan.write(folder)
        with zipfile.ZipFile(path) as bundle:
            assert bundle.namelist() == ["example.jpg", "example.txt"]
            assert bundle.read("example.txt") == b"INDEX\tX1\tY1\tX2\tY2\tNOTE\n1\t5\t6\t16\t14\tA\n"
            with Image.open(io.BytesIO(bundle.read("example.jpg"))) as jpg:
                assert jpg.size == (80, 60)
                assert jpg.mode == "RGB"
            bundles.append(bundle.read("example.jpg"))
        assert sorted(p.name for p in folder.iterdir()) == ["example_astromap.zip"]
    assert bundles[0] == bundles[1]
    np.testing.assert_array_equal(st.root["0"], original)


def test_lut_and_selected_rgb_components_match_viewer_before_overlay():
    plan = prepare(make_state(rgb=True), channels=[2], window="0:255,0:255,0:200:2")
    with plan.render_image() as image:
        expected = int(np.sqrt(160 / 200) * 255)
        assert image.getpixel((50, 40)) == (0, 0, expected)


def test_hidden_channels_need_no_source_reads_and_overlay_is_blended_in_place():
    plan = prepare(make_state(root={}), channels=[])
    with plan.render_image() as image:
        assert image.getpixel((40, 40)) == (0, 0, 0)
        assert image.getpixel((5, 6)) == (255, 255, 255)
        assert image.getpixel((8, 9)) == (100, 85, 40)


def test_large_virtual_odd_pyramid_reads_only_selected_channel_in_bounded_tiles(monkeypatch):
    monkeypatch.setattr(am, "MAX_EDGE", 257)
    calls = []

    class Virtual:
        dtype = np.dtype("uint16")

        def __init__(self, w, h, level):
            self.shape, self.level = (3, h, w), level

        def __getitem__(self, key):
            channel, ys, xs = key
            assert self.level == "2", "Full-resolution image must never be read"
            assert channel == 1, "Hidden channels must not be read"
            assert xs.stop - xs.start <= am.READ_EDGE + 4
            assert ys.stop - ys.start <= am.READ_EDGE + 4
            calls.append(key)
            return np.full((ys.stop - ys.start, xs.stop - xs.start), 100, self.dtype)

    levels = [{"path": "1", "width": 100_003, "height": 50_007},
              {"path": "2", "width": 523, "height": 263},
              {"path": "3", "width": 130, "height": 65}]
    root = {lv["path"]: Virtual(lv["width"], lv["height"], lv["path"]) for lv in levels}
    st = make_state(width=100_003, height=50_007, levels=levels, root=root)
    plan = prepare(st, channels=[1], rois=[{"x": 100_002, "y": 50_006, "w": 1, "h": 1}])
    assert plan.level_path == "2"
    assert plan.size == (257, 128)
    assert plan.boxes[0] == am.ExportBox(256, 127, 256, 127, "1")
    with plan.render_image() as image:
        assert image.getpixel((0, 0)) == (0, 100, 0)
        assert image.getpixel((254, 127)) == (0, 100, 0)
    assert len(calls) > 1


def test_plate_frame_is_explicit_and_export_uses_selected_site():
    calls = []
    root = make_state().root
    plate = SimpleNamespace(T=3, P=4, Z=5, z_home=2,
                            frame_shape=root["0"].shape, dtype=root["0"].dtype,
                            frame_view=lambda *frame: calls.append(frame) or root["0"])
    st = make_state(plate=plate)
    for frame in (None, {"p": 1}, {"t": 0, "p": False, "z": 2}, {"t": 0, "p": 7, "z": 2}):
        with pytest.raises(am.AstromapError):
            prepare(st, frame=frame)
    plan = prepare(st, frame={"t": 2, "p": 3, "z": 4})
    assert calls == [(2, 3, 4)]
    assert plan.root["0"] is root["0"]
    assert plan.stem == "example_t2_p3_z4"


def test_sparse_level_bilinear_halo_has_no_tile_seams(monkeypatch):
    monkeypatch.setattr(am, "MAX_EDGE", 600)
    pixels = np.random.default_rng(9).integers(0, 256, (3, 800, 4800), dtype=np.uint8)
    st = make_state(width=4800, height=800, rgb=True, root={"0": pixels})
    plan = prepare(st, rois=[{"x": 4799, "y": 799, "w": 1, "h": 1}])
    with plan.render_image() as image, Image.fromarray(np.moveaxis(pixels, 0, -1)) as source:
        with source.resize(plan.size, Image.Resampling.BILINEAR) as full_resize:
            actual, expected = np.asarray(image), np.asarray(full_resize)
            # Only the final pixel has the requested annotation overlay.
            np.testing.assert_array_equal(actual[:-1], expected[:-1])
            np.testing.assert_array_equal(actual[-1, :-1], expected[-1, :-1])


def test_large_plate_frame_rejected_before_decode_or_all_channel_cache():
    def forbidden(*args):
        pytest.fail("Oversized plate frames must be rejected before any read")

    plate = SimpleNamespace(T=1, P=1, Z=1, z_home=0, frame_shape=(3, 10_000, 10_000),
                            dtype=np.dtype("uint16"), frame_view=forbidden, root_for=forbidden)
    with pytest.raises(am.AstromapError, match="64 MiB"):
        prepare(make_state(plate=plate), frame={"t": 0, "p": 0, "z": 0})


def test_downloads_scope_retry_eviction_expiry_and_failed_build_cleanup(tmp_path, monkeypatch):
    store = am.AstromapDownloads(tmp_path, ttl=600, limit=1)
    owner, other = object(), object()
    result = store.create(prepare(), owner)
    key = urllib.parse.parse_qs(urllib.parse.urlparse(result["download_url"]).query)["token"][0]
    with pytest.raises(am.AstromapError):
        with store.download(key, other):
            pytest.fail("a different slide must not read the artifact")
    with pytest.raises(BrokenPipeError):
        with store.download(key, owner) as (_, source):
            assert source.read(2) == b"PK"
            raise BrokenPipeError("cancelled download")
    with store.download(key, owner):
        pass  # retry is permitted
    with store.download(key, owner):
        with pytest.raises(am.AstromapConflict):
            store.create(prepare(), owner)  # an in-use artifact cannot be evicted
    replacement = store.create(prepare(), owner)
    with pytest.raises(am.AstromapError):
        with store.download(key, owner):
            pass
    assert replacement["download_url"] != result["download_url"]
    assert len(list(tmp_path.iterdir())) == 1
    with store._lock:
        for item in store._items.values():
            item.expires = 0
        store._prune()
    assert not list(tmp_path.iterdir())

    def failed_write(self, folder):
        (folder / "incomplete.jpg").write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(am.AstromapPlan, "write", failed_write)
    with pytest.raises(OSError, match="disk full"):
        store.create(prepare(), owner)
    assert not list(tmp_path.iterdir())
    store.close()


@contextmanager
def http_server(st):
    server = create_server([], port=0)
    sid = "1234abcd"
    server.registry.slides[sid] = st
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/{server.token}/s/{sid}/"
    try:
        yield server, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_export_download_pair_stale_generation_and_lifecycle_lease(monkeypatch):
    st = make_state()
    original_write = am.AstromapPlan.write
    writes = []

    def verify_lease(self, folder):
        assert st.busy.active == 1
        writes.append(folder)
        return original_write(self, folder)

    monkeypatch.setattr(am.AstromapPlan, "write", verify_lease)
    with http_server(st) as (server, base):
        req = urllib.request.Request(base + "api/astromap", json.dumps(payload()).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as response:
            result = json.load(response)
        assert st.busy.active == 0
        assert (result["width"], result["height"], result["count"]) == (80, 60, 1)
        with urllib.request.urlopen(urllib.parse.urljoin(base, result["download_url"])) as response:
            assert response.headers["Content-Type"] == "application/zip"
            assert "attachment" in response.headers["Content-Disposition"]
            with zipfile.ZipFile(io.BytesIO(response.read())) as bundle:
                assert bundle.namelist() == ["example.jpg", "example.txt"]
        req = urllib.request.Request(base + "api/astromap", json.dumps(payload(generation="stale")).encode(),
                                     {"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        assert error.value.code == 409
        assert len(writes) == 1
    assert all(not folder.exists() for folder in writes)
