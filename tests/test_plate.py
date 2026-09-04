"""Plate mode on a small written ND2 with T, P and Z loops."""

import io
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from nd2wsi import render

limnd2 = pytest.importorskip("limnd2")
nd2 = pytest.importorskip("nd2")

T, P, Z = 2, 3, 2
H, W = 96, 128


def value(t: int, p: int, z: int) -> int:
    return 1000 + 100 * t + 10 * p + z


def _write_plate(path, offset: int = 0) -> None:
    attrs = limnd2.ImageAttributes.create(
        width=W, height=H, component_count=1, bits=16, sequence_count=T * P * Z
    )
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = attrs
        f.experiment = limnd2.ExperimentFactory(
            t=T,
            m={"count": P, "xcoords": [100.0, 7000.0, 100.0], "ycoords": [0.0, 0.0, 6000.0]},
            z={"count": Z, "step": 5.0},
        ).createExperiment()
        seq = 0
        for t in range(T):
            for p in range(P):
                for z in range(Z):
                    f.setImage(seq, np.full((H, W, 1), value(t, p, z) + offset, np.uint16))
                    seq += 1
        mf = limnd2.MetadataFactory(objective_magnification=20.0, pixel_calibration=0.5)
        mf.addPlane(name="br", color="#FFFFFF")
        f.pictureMetadata = mf.createMetadata()


@pytest.fixture()
def plate_nd2(tmp_path):
    path = tmp_path / "plate.nd2"
    _write_plate(path)
    return path


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from nd2wsi.server import create_server, server_url

    home = tmp_path_factory.mktemp("plate")
    path = home / "plate.nd2"
    _write_plate(path)
    httpd = create_server([path], host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, server_url(httpd).rstrip("/"), path
    httpd.shutdown()
    httpd.server_close()


def _get(url, method="GET", data=None):
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


# ---- detection -------------------------------------------------------------


def test_detection_accepts_loops_and_rejects_a_single_frame(plate_nd2, tmp_path):
    from nd2wsi.plate import is_plate_file

    assert is_plate_file(plate_nd2)
    single = tmp_path / "single.nd2"
    attrs = limnd2.ImageAttributes.create(
        width=W, height=H, component_count=1, bits=16, sequence_count=1
    )
    with limnd2.Nd2Writer(str(single)) as f:
        f.imageAttributes = attrs
        f.setImage(0, np.full((H, W, 1), 7, np.uint16))
    assert not is_plate_file(single)


# ---- PlateSource -----------------------------------------------------------


def test_source_reads_every_frame_where_it_belongs(plate_nd2):
    from nd2wsi.plate import PlateSource

    src = PlateSource(plate_nd2)
    try:
        assert (src.T, src.P, src.Z) == (T, P, Z)
        assert src.frame_shape == (1, H, W)
        assert src.dtype == np.uint16 and not src.rgb
        assert [ch.name for ch in src.channels] == ["br"]
        assert src.pixel_size_um == (0.5, 0.5)
        assert src.magnification == 20.0
        for t in range(T):
            for p in range(P):
                for z in range(Z):
                    view = src.frame_view(t, p, z)
                    assert view.shape == (1, H, W)
                    assert int(view[0, 5, 7]) == value(t, p, z)
        with pytest.raises(ValueError):
            src.seq(T, 0, 0)
        # sites, z and time
        assert [s["name"] for s in src.sites] == ["Site 1", "Site 2", "Site 3"]
        assert [(s["row"], s["col"]) for s in src.sites] == [(0, 0), (0, 1), (1, 0)]
        assert (src.rows, src.cols) == (2, 2)
        assert src.z_home == 0 and src.z_step_um == 5.0
        assert len(src.times_ms) == T
        plate = src.attrs["nd2wsi"]["plate"]
        assert plate["T"] == T and plate["P"] == P and plate["Z"] == Z
        assert plate["frameW"] == W and plate["frameH"] == H
        assert src.attrs["nd2wsi"]["kind"] == "plate"
        assert src.attrs["nd2wsi"]["direct"] is True
    finally:
        src.close()


def test_source_reduced_root_and_histogram(plate_nd2):
    from nd2wsi.plate import PlateSource

    src = PlateSource(plate_nd2)
    try:
        r = src.reduced(1, 2, 1, 2)
        assert r.shape == (1, H // 2, W // 2) and r.dtype == np.uint16
        assert float(r.mean()) == value(1, 2, 1)
        assert r.flags.owndata  # a fresh array, never the map itself
        assert src.reduced(1, 2, 1, 2) is r  # cached
        with pytest.raises(ValueError):
            src.reduced(0, 0, 0, 3)
        root = src.root_for(1, 2, 1)
        assert [lv["path"] for lv in src.levels] == list(root)
        assert root["0"].shape == (1, H, W)
        region = render._read_region(root, "0", 3, 4, 8, 8)
        assert region.shape == (1, 8, 8) and int(region.max()) == value(1, 2, 1)
        hist = src.histogram(1, 2, 1)
        assert len(hist) == 1 and len(hist[0]["bins"]) == 256
        assert src.histogram(1, 2, 1) is hist
    finally:
        src.close()
    with pytest.raises(ValueError):
        src.reduced(0, 0, 0, 2)  # closed: the door is barred


# ---- server ----------------------------------------------------------------


def test_info_describes_the_plate(served):
    _, base, _ = served
    status, body, _ = _get(base + "/api/info")
    assert status == 200
    info = json.loads(body)
    assert info["kind"] == "plate"
    plate = info["plate"]
    assert (plate["T"], plate["P"], plate["Z"]) == (T, P, Z)
    assert [s["i"] for s in plate["sites"]] == [0, 1, 2]
    assert plate["zHome"] == 0
    assert info["storage"] == "direct" and info["trashable"] is True
    assert info["plate"]["thumbsTotal"] == T * P * Z
    assert info["plate"]["cachePath"].endswith("--plate.nd2wsi-cache")
    status, body, _ = _get(base + "/api/inspect")
    assert status == 200 and json.loads(body)["kind"] == "plate"


def test_frame_route(served):
    _, base, _ = served
    status, body, headers = _get(base + "/api/plate/frame/1/2/1.jpg?k=2")
    assert status == 200 and body[:3] == b"\xff\xd8\xff"
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["Cache-Control"] == "no-store"
    assert _get(base + "/api/plate/frame/9/0/0.jpg")[0] == 404
    assert _get(base + "/api/plate/frame/0/0/0.jpg?k=3")[0] == 400
    gen = json.loads(_get(base + "/api/info")[1])["generation"]
    _, _, headers = _get(base + f"/api/plate/frame/0/0/0.png?k=4&g={gen}")
    assert "immutable" in headers["Cache-Control"]


def test_tile_pixel_histogram_and_roi_take_a_frame(served):
    from PIL import Image

    tifffile = pytest.importorskip("tifffile")
    _, base, _ = served
    status, body, _ = _get(base + "/api/tile/0/0/0.png?t=1&p=2&z=1")
    assert status == 200
    img = np.asarray(Image.open(io.BytesIO(body)))
    # the stored window is 1000..1020, so 1121 renders white
    assert img.shape == (H, W, 3) and img.min() == 255
    status, body, _ = _get(base + "/api/tile/0/0/0.png?t=0&p=0&z=0")
    assert status == 200 and np.asarray(Image.open(io.BytesIO(body))).max() == 0
    assert _get(base + "/api/tile/0/0/0.png?t=5")[0] == 400
    assert _get(base + "/api/tile/0/0/0.png?z=x")[0] == 400

    status, body, _ = _get(base + "/api/pixel?x=1&y=1&t=1&p=2&z=1")
    assert status == 200
    assert json.loads(body)["values"][0]["value"] == value(1, 2, 1)

    status, body, _ = _get(base + "/api/histogram?t=1&p=2&z=1")
    assert status == 200
    assert len(json.loads(body)["channels"][0]["bins"]) == 256

    status, body, headers = _get(
        base + "/api/roi?level=0&x=0&y=0&w=8&h=8&format=tiff&t=1&p=2&z=1"
    )
    assert status == 200
    assert "_t1_p2_z1_" in headers["Content-Disposition"]
    arr = tifffile.imread(io.BytesIO(body))
    assert arr.shape == (8, 8) and int(arr.min()) == int(arr.max()) == value(1, 2, 1)


def test_annotations_are_per_site(served):
    _, base, path = served
    status, body, _ = _get(base + "/api/annotations?p=1")
    assert status == 200 and json.loads(body)["items"] == []
    pin = {"type": "pin", "x": 10, "y": 20, "label": "a"}
    status, body, _ = _get(
        base + "/api/annotations?p=1", method="POST",
        data=json.dumps({"items": [pin]}).encode(),
    )
    assert status == 200
    saved = json.loads(body)["path"]
    assert saved.endswith("annotations_plate.nd2--site1.json")
    assert Path(saved).parent == path.parent / "nd2wsi" / "annotations"
    doc = json.loads(open(saved).read())
    assert doc["selection"] == {"p": 1} and doc["site"] == "Site 2"
    assert json.loads(_get(base + "/api/annotations?p=1")[1])["items"] == [pin]
    assert json.loads(_get(base + "/api/annotations?p=2")[1])["items"] == []
    assert json.loads(_get(base + "/api/annotations")[1])["items"] == []
    assert _get(base + "/api/annotations?p=9")[0] == 400


def test_new_route_is_behind_the_token(served):
    httpd, _, _ = served
    port = httpd.server_address[1]
    assert _get(f"http://127.0.0.1:{port}/api/plate/frame/0/0/0.jpg")[0] == 404


def test_focus_route_reports_a_plane_for_every_site(served):
    _, base, _ = served
    status, body, _ = _get(base + "/api/plate/focus")
    assert status == 200
    m = json.loads(body)
    assert m["total"] == T * P
    assert len(m["best"]) == T and all(len(row) == P for row in m["best"])
    assert all(0 <= z < Z for row in m["best"] for z in row)
    assert 0 <= m["zHome"] < Z


def test_open_path_registers_a_plate_and_writes_no_cache(plate_nd2):
    from nd2wsi.server import SlideRegistry

    reg = SlideRegistry()
    sid = reg.open_path(plate_nd2)
    st = reg.get(sid)
    assert st.plate is not None and st.store_path is None
    assert st.attrs["nd2wsi"]["kind"] == "plate"
    assert reg.open_path(plate_nd2) == sid  # same generation, reused
    caches = plate_nd2.parent / "nd2wsi" / "caches"
    names = sorted(d.name for d in caches.iterdir() if d.is_dir()) if caches.exists() else []
    assert names == [f"{plate_nd2.name}--plate.nd2wsi-cache"], names
    assert not (plate_nd2.parent / "pyramids").exists()
    assert st.trash_path == caches / f"{plate_nd2.name}--plate.nd2wsi-cache"
    source = st.plate
    reg.close_all(immediate=True)
    assert source._life.closed
    assert source._f.closed
    with pytest.raises(ValueError):
        source.reduced(0, 0, 0, 2)


def test_add_store_takes_a_plate_file_and_refuses_other_nd2(plate_nd2, tmp_path):
    from nd2wsi.server import SlideRegistry

    reg = SlideRegistry()
    sid = reg.add_store(plate_nd2)
    assert reg.get(sid).plate is not None
    reg.close_all(immediate=True)
    single = tmp_path / "single.nd2"
    attrs = limnd2.ImageAttributes.create(
        width=W, height=H, component_count=1, bits=16, sequence_count=1
    )
    with limnd2.Nd2Writer(str(single)) as f:
        f.imageAttributes = attrs
        f.setImage(0, np.full((H, W, 1), 7, np.uint16))
    with pytest.raises(ValueError):
        reg.add_store(single)


def test_open_or_convert_skips_the_cache_for_plates(plate_nd2):
    from nd2wsi.app import open_or_convert

    notes = []
    assert open_or_convert(plate_nd2, on_status=notes.append) == plate_nd2
    assert notes and "plate" in notes[0]
    # opening without registering builds nothing at all beside the file
    assert not (plate_nd2.parent / "nd2wsi" / "caches").exists()


# ---- the thumbnail store ---------------------------------------------------


def _wait_full(source, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if source.store is not None and source.store.count() >= source.store.total:
            return True
        time.sleep(0.05)
    return False


def test_store_fills_in_the_background_and_serves_the_second_open(plate_nd2, monkeypatch):
    from nd2wsi.plate import THUMB_K, PlateSource, plate_container

    src = PlateSource(plate_nd2)
    try:
        container = plate_container(plate_nd2)
        assert src.store is not None and src.store.container == container
        assert (container / "manifest.json").exists()
        assert _wait_full(src), src.store.status()
        status = src.status()
        assert status["done"] == T * P * Z and status["perT"] == [P * Z] * T
    finally:
        src.close()

    # a second open reads every reduction from the store, never the ND2
    again = PlateSource(plate_nd2)
    try:
        assert again.store.count() == T * P * Z

        def boom(self, seq):
            raise AssertionError("the store should have answered")

        monkeypatch.setattr(PlateSource, "_read_raw", boom)
        for t in range(T):
            for p in range(P):
                for z in range(Z):
                    r = again.reduced(t, p, z, THUMB_K)
                    assert r.shape == (1, H // THUMB_K, W // THUMB_K)
                    assert int(r[0, 0, 0]) == value(t, p, z)
    finally:
        again.close()


def test_store_is_rebuilt_when_the_file_changes(plate_nd2):
    from nd2wsi.plate import PlateSource, plate_container

    src = PlateSource(plate_nd2)
    first = src.store.manifest["generation"]
    src.close()
    # rewrite the file with other values: the fingerprint changes, the old
    # store is set aside and a fresh one starts empty
    _write_plate(plate_nd2, offset=7)
    src = PlateSource(plate_nd2)
    try:
        assert src.store.manifest["generation"] != first
        assert src.store.count() == 0
        r = src.reduced(0, 0, 0, 8)
        assert int(r[0, 0, 0]) == value(0, 0, 0) + 7
    finally:
        src.close()
    assert plate_container(plate_nd2).exists()


def test_status_route_and_trash_remove_the_store(served):
    httpd, base, path = served
    sid = httpd.registry.default_sid()
    status, body, _ = _get(f"{base}/s/{sid}/api/plate/status")
    assert status == 200
    d = json.loads(body)
    assert d["total"] == T * P * Z and len(d["perT"]) == T
    assert d["path"].endswith("--plate.nd2wsi-cache")
    bare = f"http://127.0.0.1:{httpd.server_address[1]}"
    assert _get(f"{bare}/s/{sid}/api/plate/status")[0] == 404

    st = httpd.registry.get(sid)
    container = st.trash_path
    assert container is not None and container.exists()
    freed = httpd.registry.trash_cache(sid)
    assert freed >= 0
    assert not container.exists()
    assert path.exists()  # the source is never touched
    assert (path.parent / "nd2wsi" / "annotations").exists()


def test_store_in_the_old_layout_is_rebuilt_not_set_aside(plate_nd2):
    import zarr

    from nd2wsi.plate import THUMBS_NAME, PlateSource, plate_container

    src = PlateSource(plate_nd2)
    assert _wait_full(src), src.store.status()
    src.close()
    # rewrite the array with one chunk per frame, the layout of the first
    # release candidate, keeping its data
    container = plate_container(plate_nd2)
    root = zarr.open_group(str(container / THUMBS_NAME), mode="r+", zarr_format=2)
    arr = root["thumbs"]
    data = arr[:]
    per_frame = (1, 1, 1) + tuple(arr.chunks[3:])
    del root["thumbs"]
    root.create_array("thumbs", shape=data.shape, chunks=per_frame, dtype=data.dtype)[:] = data
    src = PlateSource(plate_nd2)
    try:
        assert tuple(src.store._thumbs.chunks) == (1, P, 1) + per_frame[3:]
        assert src.store.count() == 0
        assert _wait_full(src), src.store.status()
    finally:
        src.close()
    # the old store was ours and superseded, so nothing is set aside
    assert not [p for p in container.parent.iterdir() if ".corrupt-" in p.name]


# ---- autofocus -------------------------------------------------------------


def _texture(amp: float) -> np.ndarray:
    """A coarse checkerboard whose contrast stands for how sharp a plane is.

    The pitch is 16 px so the pattern survives the store's 8x reduction,
    which is where the sharpness is measured.
    """
    yy, xx = np.mgrid[0:H, 0:W]
    pattern = (((yy // 16) + (xx // 16)) % 2).astype(np.float32)
    return (1000 + amp * pattern).astype(np.uint16).reshape(H, W, 1)


def best_plane(t: int, p: int) -> int:
    """The plane this site happens to be in focus on at this time point."""
    return (t + p) % Z


def _write_focus_plate(path) -> None:
    attrs = limnd2.ImageAttributes.create(
        width=W, height=H, component_count=1, bits=16, sequence_count=T * P * Z
    )
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = attrs
        f.experiment = limnd2.ExperimentFactory(
            t=T,
            m={"count": P, "xcoords": [100.0, 7000.0, 100.0], "ycoords": [0.0, 0.0, 6000.0]},
            z={"count": Z, "step": 5.0},
        ).createExperiment()
        seq = 0
        for t in range(T):
            for p in range(P):
                for z in range(Z):
                    amp = 400.0 if z == best_plane(t, p) else 20.0
                    f.setImage(seq, _texture(amp))
                    seq += 1
        mf = limnd2.MetadataFactory(objective_magnification=20.0, pixel_calibration=0.5)
        mf.addPlane(name="br", color="#FFFFFF")
        f.pictureMetadata = mf.createMetadata()


def test_sharpness_reads_detail_and_ignores_the_lamp():
    from nd2wsi.plate import _sharpness

    flat = np.full((24, 32), 1000, np.uint16)
    detail = _texture(400)[:, :, 0]
    assert _sharpness(detail) > _sharpness(flat)
    assert _sharpness(flat) == 0.0
    # a lamp twice as bright is not a change of focus
    assert _sharpness(detail.astype(np.float32) * 2) == pytest.approx(
        _sharpness(detail), rel=1e-3
    )
    # a single row or an empty frame is measured, not raised over
    assert _sharpness(np.zeros((1, 8), np.uint16)) == 0.0


def test_autofocus_picks_the_sharpest_plane_of_every_site(tmp_path):
    from nd2wsi.plate import PlateSource

    path = tmp_path / "focus.nd2"
    _write_focus_plate(path)
    src = PlateSource(path)
    try:
        assert _wait_full(src), src.store.status()
        src.store.flush_focus()
        m = src.focus_map()
        assert m["measured"] == m["total"] == T * P
        assert m["zHome"] == src.z_home
        assert m["best"] == [[best_plane(t, p) for p in range(P)] for t in range(T)]
    finally:
        src.close()


def test_focus_scores_are_read_back_from_the_store(tmp_path):
    from nd2wsi.plate import PlateSource

    path = tmp_path / "focus.nd2"
    _write_focus_plate(path)
    src = PlateSource(path)
    assert _wait_full(src), src.store.status()
    src.close()  # close flushes the scores

    src = PlateSource(path)
    try:
        # every plane is already measured, with no frame read at all
        m = src.focus_map()
        assert m["measured"] == m["total"]
        assert m["best"] == [[best_plane(t, p) for p in range(P)] for t in range(T)]
    finally:
        src.close()


def test_a_store_written_before_autofocus_gains_the_scores(tmp_path):
    import zarr

    from nd2wsi.plate import THUMBS_NAME, PlateSource, plate_container

    path = tmp_path / "focus.nd2"
    _write_focus_plate(path)
    src = PlateSource(path)
    assert _wait_full(src), src.store.status()
    src.close()

    # drop the scores the way a store from an earlier release has none
    root = zarr.open_group(str(plate_container(path) / THUMBS_NAME), mode="r+", zarr_format=2)
    del root["focus"]

    src = PlateSource(path)
    try:
        assert src.store.focus_map()["measured"] == 0
        # reading the frames back measures them again, from the store only
        for t in range(T):
            for p in range(P):
                for z in range(Z):
                    src.reduced(t, p, z, 8)
        m = src.focus_map()
        assert m["measured"] == m["total"]
        assert m["best"] == [[best_plane(t, p) for p in range(P)] for t in range(T)]
    finally:
        src.close()



def test_the_builder_gives_up_on_a_store_that_cannot_be_written(plate_nd2, monkeypatch):
    """A container that refuses writes must not send the builder round the
    same frame forever, reading it from the ND2 on every turn."""
    from nd2wsi import plate as plate_mod

    reads = {"n": 0}
    real_reduced = plate_mod.PlateSource.reduced

    def counting_reduced(self, t, p, z, k):
        reads["n"] += 1
        return real_reduced(self, t, p, z, k)

    def refuse(self, t, p, z, frame):
        return  # the write silently fails, as it does on a full drive

    monkeypatch.setattr(plate_mod.PlateStore, "put", refuse)
    monkeypatch.setattr(plate_mod.PlateSource, "reduced", counting_reduced)
    src = plate_mod.PlateSource(plate_nd2)
    try:
        thread = src.store._thread
        assert thread is not None
        thread.join(timeout=15)
        assert not thread.is_alive(), "the builder never stopped"
        assert src.store.count() == 0
        # it stopped early rather than walking the whole series over and over
        assert reads["n"] < T * P * Z, reads["n"]
    finally:
        src.close()


def test_only_one_viewer_writes_the_store(plate_nd2):
    """Sites share a chunk, so a second viewer of the same file reads the
    store and the ND2 but never writes: two writers would drop each other's
    sites while the done flags claimed the frames were there."""
    from nd2wsi.plate import PlateSource

    first = PlateSource(plate_nd2)
    try:
        assert first.store.writable
        second = PlateSource(plate_nd2)
        try:
            assert not second.store.writable
            # it still serves every frame, straight from the file
            assert second.reduced(0, 1, 1, 8).shape == first.reduced(0, 1, 1, 8).shape
            assert int(second.frame_view(1, 2, 0)[0, 0, 0]) == value(1, 2, 0)
        finally:
            second.close()
    finally:
        first.close()

    # once the first viewer lets go, the next one takes the role
    third = PlateSource(plate_nd2)
    try:
        assert third.store.writable
    finally:
        third.close()


def _write_two_channel_plate(path) -> None:
    """A plate whose frames carry two channels, each a flat level, so an
    interleaved read shows up as a channel that is not flat."""
    attrs = limnd2.ImageAttributes.create(
        width=W, height=H, component_count=2, bits=16, sequence_count=T * P * Z
    )
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = attrs
        f.experiment = limnd2.ExperimentFactory(
            t=T,
            m={"count": P, "xcoords": [100.0, 7000.0, 100.0], "ycoords": [0.0, 0.0, 6000.0]},
            z={"count": Z, "step": 5.0},
        ).createExperiment()
        seq = 0
        for t in range(T):
            for p in range(P):
                for z in range(Z):
                    frame = np.empty((H, W, 2), np.uint16)
                    frame[:, :, 0] = value(t, p, z)
                    frame[:, :, 1] = value(t, p, z) + 5000
                    f.setImage(seq, frame)
                    seq += 1
        mf = limnd2.MetadataFactory(objective_magnification=20.0, pixel_calibration=0.5)
        mf.addPlane(name="gfp", color="#00FF00")
        mf.addPlane(name="br", color="#FFFFFF")
        f.pictureMetadata = mf.createMetadata()


def test_two_channel_frames_are_not_interleaved(tmp_path):
    """The bytes on disk run (H, W, C); the fast read used to reshape them
    straight into (C, H, W), which put half of each channel in the other."""
    from nd2wsi.plate import PlateSource

    path = tmp_path / "twoch.nd2"
    _write_two_channel_plate(path)
    src = PlateSource(path)
    try:
        assert src.frame_shape[0] == 2, src.frame_shape
        seq = src.seq(1, 2, 0)
        fast = src._read_raw(seq)
        with nd2.ND2File(str(path)) as f:
            # nd2 hands back a view onto its memory map, which is gone
            # once the file closes, so the copy happens inside
            truth = np.array(f.read_frame(seq), copy=True)
        assert fast.shape == truth.shape == (2, H, W)
        assert np.array_equal(fast, truth)
        # each channel is one level, so interleaving could not hide here
        assert int(fast[0].min()) == int(fast[0].max()) == value(1, 2, 0)
        assert int(fast[1].min()) == int(fast[1].max()) == value(1, 2, 0) + 5000
        # and the frame the viewer serves agrees
        served = src.frame_view(1, 2, 0)
        assert int(served[0, 0, 0]) == value(1, 2, 0)
        assert int(served[1, 0, 0]) == value(1, 2, 0) + 5000
    finally:
        src.close()
