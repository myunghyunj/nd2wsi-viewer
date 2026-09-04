"""Plate mode on a small written ND2 with T, P and Z loops."""

import io
import json
import os
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


def _write_plate(path, offset: int = 0, value_at=None) -> None:
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
                    level = value_at(t, p, z) if value_at is not None else value(t, p, z)
                    f.setImage(seq, np.full((H, W, 1), level + offset, np.uint16))
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


def test_registry_retries_when_plate_fingerprint_changes_during_open(
    plate_nd2, monkeypatch
):
    from nd2wsi import cache as cache_mod
    from nd2wsi import plate as plate_mod
    from nd2wsi.server import SlideRegistry

    actual = cache_mod.quick_fingerprint(plate_nd2)
    before = {**actual, "mtime_ns": int(actual["mtime_ns"]) - 1}
    fingerprints = iter((before, actual, actual))
    real_source = plate_mod.PlateSource
    opened = []

    def changing_fingerprint(_path):
        return next(fingerprints)

    def recording_source(path):
        source = real_source(path)
        opened.append(source)
        return source

    monkeypatch.setattr(cache_mod, "quick_fingerprint", changing_fingerprint)
    monkeypatch.setattr(plate_mod, "PlateSource", recording_source)
    registry = SlideRegistry()
    try:
        sid = registry.add_plate(plate_nd2)
        assert len(opened) == 2
        assert opened[0]._teardown_complete
        assert registry.get(sid).plate is opened[1]
        assert registry.get(sid).manifest["source"] == actual
    finally:
        registry.close_all(immediate=True)


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


def test_missing_nonzero_shared_chunk_falls_back_and_repairs(plate_nd2, monkeypatch):
    import zarr

    from nd2wsi.plate import THUMB_K, THUMBS_NAME, PlateSource, PlateStore, plate_container
    from nd2wsi.plate_integrity import zarr_v2_chunk_path

    first = PlateSource(plate_nd2)
    assert _wait_full(first), first.store.status()
    container = plate_container(plate_nd2)
    first.close()

    t, p, z = 1, 1, 1
    payload = zarr_v2_chunk_path(
        container / THUMBS_NAME / "thumbs", (t, 0, z, 0, 0, 0)
    )
    assert payload.is_file()  # this shared block contains nonzero source pixels
    payload.unlink()

    # Exercise foreground detection and repair without a background warm pass
    # winning the race first.
    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    reads = {"n": 0}
    real_read = PlateSource._read_raw

    def counting_read(self, seq):
        reads["n"] += 1
        return real_read(self, seq)

    monkeypatch.setattr(PlateSource, "_read_raw", counting_read)
    repaired = PlateSource(plate_nd2)
    try:
        before = reads["n"]  # opening samples source pixels for display windows
        frame = repaired.reduced(t, p, z, THUMB_K)
        assert int(frame[0, 0, 0]) == value(t, p, z)
        assert reads["n"] == before + 1
        assert repaired.store.is_committed(t, p, z)
        status = repaired.store.status()
        assert status["integrityErrors"] == 1
        assert status["repairPending"] == P - 1
    finally:
        repaired.close()

    root = zarr.open_group(str(container / THUMBS_NAME), mode="r", zarr_format=2)
    assert root["done"][t, p, z] == 1
    assert root["digest"][t, p, z] != 0
    for other in set(range(P)) - {p}:
        assert root["done"][t, other, z] == 0
        assert root["digest"][t, other, z] == 0

    # The repaired frame survives a reopen and no longer needs the ND2.
    verified = PlateSource(plate_nd2)
    try:
        before = reads["n"]
        frame = verified.reduced(t, p, z, THUMB_K)
        assert int(frame[0, 0, 0]) == value(t, p, z)
        assert reads["n"] == before
        assert verified.store.status()["integrityErrors"] == 0
    finally:
        verified.close()


def test_decodable_wrong_thumbnail_fails_its_digest_and_repairs(plate_nd2, monkeypatch):
    import zarr

    from nd2wsi.plate import THUMB_K, THUMBS_NAME, PlateSource, PlateStore, plate_container
    from nd2wsi.plate_integrity import digest_matches, zarr_v2_chunk_path

    first = PlateSource(plate_nd2)
    assert _wait_full(first), first.store.status()
    container = plate_container(plate_nd2)
    first.close()

    t, p, z = 0, 2, 1
    root = zarr.open_group(str(container / THUMBS_NAME), mode="r+", zarr_format=2)
    wrong = np.full(root["thumbs"].shape[3:], 42, dtype=np.uint16)
    root["thumbs"][t, p, z] = wrong
    assert np.array_equal(root["thumbs"][t, p, z], wrong)
    payload = zarr_v2_chunk_path(
        container / THUMBS_NAME / "thumbs", (t, 0, z, 0, 0, 0)
    )
    corrupt_payload = payload.read_bytes()

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    reads = {"n": 0}
    real_read = PlateSource._read_raw

    def counting_read(self, seq):
        reads["n"] += 1
        return real_read(self, seq)

    monkeypatch.setattr(PlateSource, "_read_raw", counting_read)
    repaired = PlateSource(plate_nd2)
    try:
        before = reads["n"]  # opening samples source pixels for display windows
        frame = repaired.reduced(t, p, z, THUMB_K)
        assert int(frame[0, 0, 0]) == value(t, p, z)
        assert reads["n"] == before + 1
        assert repaired.store.is_committed(t, p, z)
        status = repaired.store.status()
        assert status["integrityErrors"] == 1
        assert status["repairPending"] == P - 1
    finally:
        repaired.close()

    root = zarr.open_group(str(container / THUMBS_NAME), mode="r", zarr_format=2)
    stored = np.asarray(root["thumbs"][t, p, z])
    assert int(stored[0, 0, 0]) == value(t, p, z)
    assert digest_matches(stored, root["digest"][t, p, z])
    retained = list(payload.parent.glob(f"{payload.name}.corrupt-*"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == corrupt_payload
    for other in set(range(P)) - {p}:
        assert root["done"][t, other, z] == 0
        assert root["digest"][t, other, z] == 0


def test_shared_chunk_get_is_serialized_with_a_sibling_put(plate_nd2, monkeypatch):
    from nd2wsi.plate import THUMB_K, PlateSource, PlateStore

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    source = PlateSource(plate_nd2)
    t, z = 0, 0
    source.reduced(t, 0, z, THUMB_K)
    store = source.store
    real_thumbs = store._thumbs
    read_started = threading.Event()
    release_read = threading.Event()
    put_finished = threading.Event()
    result = {}

    class GatedThumbs:
        def __getitem__(self, index):
            block = real_thumbs[index]
            if index == (t, slice(None), z):
                read_started.set()
                assert release_read.wait(timeout=10)
            return block

        def __setitem__(self, index, frame):
            real_thumbs[index] = frame

    store._thumbs = GatedThumbs()
    reader = threading.Thread(
        target=lambda: result.setdefault("first", store.get(t, 0, z)), daemon=True
    )
    expected = np.full(real_thumbs.shape[3:], value(t, 1, z), dtype=np.uint16)

    def write_sibling():
        result["put"] = store.put(t, 1, z, expected)
        put_finished.set()

    writer = threading.Thread(target=write_sibling, daemon=True)
    try:
        reader.start()
        assert read_started.wait(timeout=5)
        writer.start()
        assert not put_finished.wait(timeout=0.1), "put raced a shared-chunk read"
        release_read.set()
        reader.join(timeout=5)
        writer.join(timeout=5)
        assert not reader.is_alive() and not writer.is_alive()
        assert result["put"] is True
        assert int(source.reduced(t, 1, z, THUMB_K)[0, 0, 0]) == value(t, 1, z)
    finally:
        release_read.set()
        reader.join(timeout=5)
        writer.join(timeout=5)
        source.close()


def test_undecodable_shared_chunk_is_quarantined_then_repaired(plate_nd2, monkeypatch):
    import zarr

    from nd2wsi.plate import THUMB_K, THUMBS_NAME, PlateSource, PlateStore, plate_container
    from nd2wsi.plate_integrity import digest_matches, zarr_v2_chunk_path

    first = PlateSource(plate_nd2)
    assert _wait_full(first), first.store.status()
    container = plate_container(plate_nd2)
    first.close()

    t, z = 1, 0
    payload = zarr_v2_chunk_path(
        container / THUMBS_NAME / "thumbs", (t, 0, z, 0, 0, 0)
    )
    assert payload.is_file()
    payload.write_bytes(b"not-a-valid-zarr-chunk")
    monkeypatch.setattr(PlateStore, "start", lambda self: None)

    repaired = PlateSource(plate_nd2)
    try:
        for p in range(P):
            frame = repaired.reduced(t, p, z, THUMB_K)
            assert int(frame[0, 0, 0]) == value(t, p, z)
            assert repaired.store.is_committed(t, p, z)
        status = repaired.store.status()
        assert status["integrityErrors"] == 1
        assert status["repairPending"] == 0
        assert status["done"] == T * P * Z
    finally:
        repaired.close()

    root = zarr.open_group(str(container / THUMBS_NAME), mode="r", zarr_format=2)
    retained = list(payload.parent.glob(f"{payload.name}.corrupt-*"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == b"not-a-valid-zarr-chunk"
    for p in range(P):
        stored = np.asarray(root["thumbs"][t, p, z])
        assert int(stored[0, 0, 0]) == value(t, p, z)
        assert digest_matches(stored, root["digest"][t, p, z])


def test_valid_all_zero_sparse_chunk_stays_committed_after_reopen(tmp_path, monkeypatch):
    from nd2wsi.plate import THUMB_K, THUMBS_NAME, PlateSource, PlateStore, plate_container
    from nd2wsi.plate_integrity import zarr_v2_chunk_path

    t, z = 0, 1
    path = tmp_path / "zero-block.nd2"
    _write_plate(path, value_at=lambda ti, p, zi: 0 if (ti, zi) == (t, z) else value(ti, p, zi))

    first = PlateSource(path)
    try:
        assert _wait_full(first), first.store.status()
        assert all(first.store.is_committed(t, p, z) for p in range(P))
    finally:
        first.close()

    payload = zarr_v2_chunk_path(
        plate_container(path) / THUMBS_NAME / "thumbs", (t, 0, z, 0, 0, 0)
    )
    # Zarr normally elides this all-zero payload. Removing it explicitly also
    # keeps the regression stable across supported Zarr patch versions.
    payload.unlink(missing_ok=True)
    assert not payload.exists()

    monkeypatch.setattr(PlateStore, "start", lambda self: None)

    def unexpected_source_read(self, seq):
        raise AssertionError("a digest-committed sparse zero frame should use the store")

    reopened = PlateSource(path)
    try:
        # PlateSource construction legitimately samples the ND2 to choose
        # display windows; only this cached thumbnail request must avoid it.
        monkeypatch.setattr(reopened, "_read_raw", unexpected_source_read)
        for p in range(P):
            frame = reopened.reduced(t, p, z, THUMB_K)
            assert not frame.any()
            assert reopened.store.is_committed(t, p, z)
        status = reopened.store.status()
        assert status["done"] == T * P * Z
        assert status["integrityErrors"] == 0
        assert status["repairPending"] == 0
        assert not payload.exists()
    finally:
        reopened.close()


def test_nonwriter_root_is_read_only_and_refreshes_writer_progress(plate_nd2, monkeypatch):
    from nd2wsi.plate import THUMB_K, PlateSource, PlateStore

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    writer = PlateSource(plate_nd2)
    reader = None
    try:
        assert writer.store.writable
        reader = PlateSource(plate_nd2)
        assert not reader.store.writable
        assert reader.store._root.store.read_only is True
        assert reader.store.count() == 0

        t, p, z = 1, 0, 1
        written = writer.reduced(t, p, z, THUMB_K)
        assert writer.store.is_committed(t, p, z)
        reader.store._refresh(force=True)
        assert reader.store.count() == 1
        assert reader.store.is_committed(t, p, z)

        def unexpected_source_read(seq):
            raise AssertionError("the refreshed reader should use the writer's commit")

        monkeypatch.setattr(reader, "_read_raw", unexpected_source_read)
        np.testing.assert_array_equal(reader.reduced(t, p, z, THUMB_K), written)

        other = np.full_like(written, 99)
        assert not reader.store.put(t, 1, z, other)
        assert writer.store.count() == 1
    finally:
        if reader is not None:
            reader.close()
        writer.close()


def test_nonwriter_revalidates_after_the_writer_repairs_a_bad_site(plate_nd2, monkeypatch):
    from nd2wsi.plate import THUMB_K, PlateSource, PlateStore

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    writer = PlateSource(plate_nd2)
    reader = None
    try:
        t, p, z = 0, 1, 0
        expected = writer.reduced(t, p, z, THUMB_K)
        reader = PlateSource(plate_nd2)
        reader.store._refresh(force=True)

        writer.store._thumbs[t, p, z] = np.full_like(expected, 77)
        assert reader.store.get(t, p, z) is None
        assert reader.store.status()["repairPending"] == 1

        assert writer.store.get(t, p, z) is None
        np.testing.assert_array_equal(writer.reduced(t, p, z, THUMB_K), expected)
        assert writer.store.is_committed(t, p, z)

        reader.store._refresh(force=True)
        np.testing.assert_array_equal(reader.store.get(t, p, z), expected)
        assert reader.store.status()["repairPending"] == 0
    finally:
        if reader is not None:
            reader.close()
        writer.close()


def test_each_open_has_a_distinct_ram_cache_owner(plate_nd2):
    from nd2wsi.plate import PlateSource

    first = PlateSource(plate_nd2, store=False)
    second = PlateSource(plate_nd2, store=False)
    try:
        assert first._owner[0] == second._owner[0] == str(plate_nd2.resolve())
        assert first._owner != second._owner
        assert first._open_generation != second._open_generation
    finally:
        second.close()
        first.close()


def test_old_inflight_read_cannot_fill_a_replacement_sources_cache(tmp_path, monkeypatch):
    from nd2wsi.plate import PlateSource

    path = tmp_path / "replace.nd2"
    replacement = tmp_path / "replacement.nd2"
    _write_plate(path)
    _write_plate(replacement, offset=7000)
    first = PlateSource(path, store=False)
    started = threading.Event()
    release = threading.Event()
    outcome = {}
    real_read = first._read_raw

    def gated_read(seq):
        frame = real_read(seq)
        started.set()
        assert release.wait(timeout=10)
        return frame

    monkeypatch.setattr(first, "_read_raw", gated_read)
    worker = threading.Thread(
        target=lambda: outcome.setdefault("frame", first.frame(1, 2, 1)), daemon=True
    )
    second = None
    try:
        worker.start()
        assert started.wait(timeout=10)
        replacement.replace(path)
        second = PlateSource(path, store=False)
        release.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert int(outcome["frame"][0, 0, 0]) == value(1, 2, 1)
        assert int(second.frame(1, 2, 1)[0, 0, 0]) == value(1, 2, 1) + 7000
        assert first._owner != second._owner
    finally:
        release.set()
        worker.join(timeout=10)
        if second is not None:
            second.close()
        first.close()


def test_close_drains_inflight_frame_before_the_final_cache_purge(plate_nd2, monkeypatch):
    from nd2wsi.plate import _FRAME_CACHE, PlateSource

    source = PlateSource(plate_nd2, store=False)
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    outcome = {}
    real_read = source._read_raw

    def gated_read(seq):
        frame = real_read(seq)
        started.set()
        assert release.wait(timeout=10)
        return frame

    monkeypatch.setattr(source, "_read_raw", gated_read)
    key = (source._owner, "frame", 1, 2, 1)
    reader = threading.Thread(
        target=lambda: outcome.setdefault("frame", source.frame(1, 2, 1)), daemon=True
    )
    closer = threading.Thread(target=lambda: (source.close(), closed.set()), daemon=True)
    try:
        reader.start()
        assert started.wait(timeout=10)
        closer.start()
        assert not closed.wait(timeout=0.1), "close did not wait for the active read"
        release.set()
        reader.join(timeout=10)
        closer.join(timeout=10)
        assert not reader.is_alive() and not closer.is_alive()
        assert int(outcome["frame"][0, 0, 0]) == value(1, 2, 1)
        assert _FRAME_CACHE.get(key) is None
    finally:
        release.set()
        reader.join(timeout=10)
        closer.join(timeout=10)
        source.close()


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


def test_legacy_plate_cache_rebuilds_from_source_before_gaining_integrity(
    plate_nd2, monkeypatch
):
    import zarr

    from nd2wsi.plate import (
        THUMB_K,
        THUMBS_NAME,
        PlateSource,
        PlateStore,
        plate_container,
    )
    from nd2wsi.plate_integrity import DIGEST_NAME

    first = PlateSource(plate_nd2)
    assert _wait_full(first), first.store.status()
    container = plate_container(plate_nd2)
    first.close()

    manifest_path = container / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("integrity", None)
    manifest_path.write_text(json.dumps(manifest))
    root = zarr.open_group(str(container / THUMBS_NAME), mode="r+", zarr_format=2)
    del root[DIGEST_NAME]
    wrong_t, wrong_p, wrong_z = 0, 1, 0
    root["thumbs"][wrong_t, wrong_p, wrong_z] = np.full(
        root["thumbs"].shape[3:], 77, dtype=np.uint16
    )

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    upgraded = PlateSource(plate_nd2)
    reader = PlateSource(plate_nd2)
    try:
        # A V1 done bit cannot prove that decodable bytes still match the ND2.
        # No legacy frame is served or blessed until source-backed reduction
        # writes a fresh digest for it.
        assert upgraded.store.status()["done"] == 0
        assert reader.store.status()["done"] == 0
        assert upgraded.store.get(wrong_t, wrong_p, wrong_z) is None
        first_rebuilt = upgraded.reduced(wrong_t, wrong_p, wrong_z, THUMB_K)
        reader.store._refresh(force=True)
        np.testing.assert_array_equal(
            reader.store.get(wrong_t, wrong_p, wrong_z), first_rebuilt
        )
        for t in range(T):
            for z in range(Z):
                for p in range(P):
                    rebuilt = upgraded.reduced(t, p, z, THUMB_K)
                    assert int(rebuilt[0, 0, 0]) == value(t, p, z)
        assert upgraded.store.status()["done"] == T * P * Z
        reader.store._refresh(force=True)
        assert reader.store.manifest["integrity"]["digest_name"] == DIGEST_NAME
        assert reader.store.status()["done"] == T * P * Z
    finally:
        reader.close()
        upgraded.close()

    manifest = json.loads(manifest_path.read_text())
    assert manifest["format"] == "nd2wsi-plate/1"
    assert manifest["integrity"]["digest_name"] == DIGEST_NAME
    root = zarr.open_group(str(container / THUMBS_NAME), mode="r", zarr_format=2)
    assert np.asarray(root[DIGEST_NAME][:], dtype=np.uint64).all()
    assert int(root["thumbs"][wrong_t, wrong_p, wrong_z, 0, 0, 0]) == value(
        wrong_t, wrong_p, wrong_z
    )


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


def test_store_in_the_old_layout_is_quarantined_before_rebuild(plate_nd2):
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
    annotation_name = "annotations_legacy.json"
    annotation_body = '{"items":[{"kind":"pin"}]}'
    (container / annotation_name).write_text(annotation_body)
    src = PlateSource(plate_nd2)
    try:
        assert tuple(src.store._thumbs.chunks) == (1, P, 1) + per_frame[3:]
        assert src.store.count() == 0
        assert _wait_full(src), src.store.status()
    finally:
        src.close()
    # Even an obsolete cache may contain user work left by an older build.
    # Rebuild into a fresh container and preserve the complete predecessor.
    quarantined = list(container.parent.glob(f"{container.name}.corrupt-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / annotation_name).read_text() == annotation_body
    assert container.exists()


def test_cached_histogram_rejects_same_path_source_replacement(plate_nd2):
    from nd2wsi.plate import PlateSource

    source = PlateSource(plate_nd2, store=False)
    replacement = plate_nd2.with_name("replacement.nd2")
    try:
        cached = source.histogram(1, 2, 1)
        assert source.histogram(1, 2, 1) is cached
        _write_plate(replacement, offset=7000)
        replacement.replace(plate_nd2)

        with pytest.raises(ValueError, match="changed while it was open"):
            source.histogram(1, 2, 1)
    finally:
        source.close()


def test_focus_map_rejects_same_path_source_replacement(plate_nd2):
    from nd2wsi.plate import PlateSource

    source = PlateSource(plate_nd2, store=False)
    replacement = plate_nd2.with_name("focus-replacement.nd2")
    try:
        source.focus_map()
        _write_plate(replacement, offset=8000)
        replacement.replace(plate_nd2)

        with pytest.raises(ValueError, match="changed while it was open"):
            source.focus_map()
    finally:
        source.close()


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


# ---- cache correctness and lifecycle regressions ---------------------------


def test_status_names_the_writer_and_storeless_fallback(plate_nd2, monkeypatch):
    from nd2wsi.plate import PlateSource, PlateStore

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    writer = PlateSource(plate_nd2)
    reader = PlateSource(plate_nd2)
    storeless = PlateSource(plate_nd2, store=False)
    try:
        assert writer.status()["writer"] is True
        assert reader.status()["writer"] is False
        assert writer.status()["format"] == "nd2wsi-plate/1"
        assert storeless.status() == {
            "done": 0,
            "total": T * P * Z,
            "perT": [0] * T,
            "path": None,
            "format": None,
            "building": False,
            "writer": False,
            "integrityErrors": 0,
            "repairPending": 0,
        }
    finally:
        storeless.close()
        reader.close()
        writer.close()


def test_status_uses_one_committed_snapshot(plate_nd2, monkeypatch):
    from nd2wsi.plate import PlateSource, PlateStore

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    source = PlateSource(plate_nd2)
    store = source.store
    real_mask = store._committed_mask
    calls = {"n": 0}

    def mutate_after_snapshot():
        snapshot = np.array(real_mask(), copy=True)
        calls["n"] += 1
        if calls["n"] == 1:
            store._digest_np[0, 0, 0] = 1
            store._done_np[0, 0, 0] = True
        return snapshot

    monkeypatch.setattr(store, "_committed_mask", mutate_after_snapshot)
    try:
        status = store.status()
        assert calls["n"] == 1
        assert status["done"] == sum(status["perT"])
    finally:
        source.close()


def test_integrity_done_without_digest_is_repaired_in_the_same_build(plate_nd2):
    import zarr

    from nd2wsi.plate import THUMBS_NAME, PlateSource, plate_container
    from nd2wsi.plate_integrity import DIGEST_NAME, UNCOMMITTED_DIGEST

    first = PlateSource(plate_nd2)
    assert _wait_full(first), first.store.status()
    container = plate_container(plate_nd2)
    first.close()

    t, p, z = 1, 2, 1
    root = zarr.open_group(str(container / THUMBS_NAME), mode="r+", zarr_format=2)
    assert root["done"][t, p, z] == 1
    root[DIGEST_NAME][t, p, z] = UNCOMMITTED_DIGEST

    repaired = PlateSource(plate_nd2)
    try:
        # The builder must regard done=1/digest=0 as missing.  If it only
        # discovers the bad commit during its final warm pass, it exits with
        # eleven effective commits and this wait never succeeds.
        assert _wait_full(repaired, timeout=5.0), repaired.store.status()
        assert repaired.store.is_committed(t, p, z)
        assert repaired.store._digest_np[t, p, z] != UNCOMMITTED_DIGEST
    finally:
        repaired.close()


def test_focus_map_masks_undone_and_reader_local_invalid_scores(tmp_path, monkeypatch):
    from nd2wsi.plate import THUMB_K, PlateSource, PlateStore

    path = tmp_path / "focus-mask.nd2"
    _write_focus_plate(path)
    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    writer = PlateSource(path)
    reader = None
    try:
        t, z = 0, 1
        writer.reduced(t, 1, z, THUMB_K)
        # Simulate a stale positive focus score whose payload is not committed.
        writer.store._focus_np[t, 0, z] = 123.0
        writer.store._focus_dirty += 1
        writer.store.flush_focus()

        reader = PlateSource(path)
        reader.store._invalid[t, 1, z] = True
        focus = reader.focus_map()
        assert focus["measured"] == 0
        assert focus["best"][t] == [reader.z_home] * P
    finally:
        if reader is not None:
            reader.close()
        writer.close()


def test_completed_focus_block_becomes_visible_to_a_second_viewer(
    tmp_path, monkeypatch
):
    from nd2wsi.plate import THUMB_K, PlateSource, PlateStore

    path = tmp_path / "focus-progress.nd2"
    _write_focus_plate(path)
    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    writer = PlateSource(path)
    reader = PlateSource(path)
    try:
        assert writer.status()["writer"] is True
        assert reader.status()["writer"] is False
        assert reader.focus_map()["measured"] == 0

        t, z = 0, 1
        for p in range(P):
            writer.reduced(t, p, z, THUMB_K)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            focus = reader.focus_map()
            if focus["measured"] == P:
                break
            time.sleep(0.05)
        assert focus["measured"] == P
        assert focus["best"][t] == [z] * P
    finally:
        reader.close()
        writer.close()


def test_start_failure_closes_the_opened_store_and_releases_writer(
    plate_nd2, monkeypatch
):
    from nd2wsi.plate import PlateSource, PlateStore

    real_close = PlateStore.close
    closed = []

    def fail_start(self):
        raise RuntimeError("synthetic start failure")

    def recording_close(self, *, release_writer=True, timeout=30.0):
        closed.append(self)
        return real_close(self, release_writer=release_writer, timeout=timeout)

    monkeypatch.setattr(PlateStore, "start", fail_start)
    monkeypatch.setattr(PlateStore, "close", recording_close)
    fallback = PlateSource(plate_nd2)
    successor = None
    try:
        assert fallback.store is None
        assert len(closed) == 1
        assert fallback.status()["writer"] is False

        monkeypatch.setattr(PlateStore, "start", lambda self: None)
        successor = PlateSource(plate_nd2)
        assert successor.store is not None
        assert successor.store.writable
    finally:
        if successor is not None:
            successor.close()
        fallback.close()


def test_concurrent_store_starts_create_only_one_worker(plate_nd2, monkeypatch):
    from nd2wsi.plate import PlateSource, PlateStore

    real_start = PlateStore.start
    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    source = PlateSource(plate_nd2)
    store = source.store
    entered = threading.Event()
    release = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def gated_build():
        with calls_lock:
            calls.append(threading.current_thread())
        entered.set()
        assert release.wait(timeout=10)

    monkeypatch.setattr(store, "_build", gated_build)
    monkeypatch.setattr(PlateStore, "start", real_start)
    barrier = threading.Barrier(9)
    callers = [
        threading.Thread(target=lambda: (barrier.wait(), store.start()), daemon=True)
        for _ in range(8)
    ]
    try:
        for caller in callers:
            caller.start()
        barrier.wait()
        for caller in callers:
            caller.join(timeout=5)
        assert all(not caller.is_alive() for caller in callers)
        assert entered.wait(timeout=5)
        assert len(calls) == 1
        assert store._thread is calls[0]
    finally:
        release.set()
        for caller in callers:
            caller.join(timeout=5)
        source.close()


def test_same_size_preserved_mtime_replacement_is_detected_while_opening(
    tmp_path, monkeypatch
):
    from nd2wsi import plate as plate_mod

    path = tmp_path / "opening-swap.nd2"
    replacement = tmp_path / "replacement.nd2"
    _write_plate(path)
    _write_plate(replacement, offset=7000)
    original = path.stat()
    replacement_stat = replacement.stat()
    assert replacement_stat.st_size == original.st_size
    os.utime(
        replacement,
        ns=(replacement.stat().st_atime_ns, original.st_mtime_ns),
    )
    assert replacement.stat().st_mtime_ns == original.st_mtime_ns

    real_nd2_file = nd2.ND2File

    def open_then_swap(open_path):
        handle = real_nd2_file(open_path)
        replacement.replace(path)
        return handle

    monkeypatch.setattr(nd2, "ND2File", open_then_swap)
    with pytest.raises(ValueError, match="changed while it was opening"):
        plate_mod.PlateSource(path, store=False)


def test_reader_detaches_when_cache_generation_at_its_path_changes(
    plate_nd2, monkeypatch
):
    from nd2wsi.plate import THUMB_K, PlateSource, PlateStore, plate_container

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    writer = PlateSource(plate_nd2)
    reader = PlateSource(plate_nd2)
    replacement = None
    try:
        old_generation = reader.store.manifest["generation"]
        container = plate_container(plate_nd2)
        aside = container.with_name(container.name + ".old-generation")
        writer.close()
        writer = None
        container.rename(aside)

        replacement = PlateSource(plate_nd2)
        assert replacement.store.manifest["generation"] != old_generation

        t, p, z = 1, 2, 1
        reader.store._refresh(force=True)
        assert reader.store.get(t, p, z) is None
        assert reader.store.count() == 0
        assert reader.store.writable is False
        frame = reader.reduced(t, p, z, THUMB_K)
        assert int(frame[0, 0, 0]) == value(t, p, z)
    finally:
        if replacement is not None:
            replacement.close()
        reader.close()
        if writer is not None:
            writer.close()


def test_failed_trash_drain_reopens_the_source_for_reads(plate_nd2):
    from nd2wsi.plate import PlateSource

    source = PlateSource(plate_nd2, store=False)
    entered = threading.Event()
    release = threading.Event()

    def hold_read():
        with source._life:
            entered.set()
            assert release.wait(timeout=10)

    active = threading.Thread(target=hold_read, daemon=True)
    active.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(RuntimeError, match="timed out waiting for plate reads"):
            source.close_for_trash(timeout=0.01)
        assert source._closed is False
        assert source._life.closed is False
        assert not source._f.closed
        release.set()
        active.join(timeout=5)
        assert int(source.frame(1, 2, 1)[0, 0, 0]) == value(1, 2, 1)
    finally:
        release.set()
        active.join(timeout=5)
        source.close()


def test_failed_trash_worker_stop_reopens_source_and_keeps_writer(plate_nd2, monkeypatch):
    from nd2wsi.plate import PlateSource, PlateStore

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    source = PlateSource(plate_nd2)
    store = source.store

    class StuckWorker:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            assert timeout == pytest.approx(0.01)

    store._thread = StuckWorker()
    try:
        with pytest.raises(RuntimeError, match="timed out stopping"):
            source.close_for_trash(timeout=0.01)
        assert source._closed is False
        assert source._life.closed is False
        assert store._stop is False
        assert store.writable
        assert int(source.frame(1, 2, 1)[0, 0, 0]) == value(1, 2, 1)
    finally:
        store._thread = None
        source.close()


@pytest.mark.parametrize("failure_site", ["nd2_close", "prefetch_join"])
def test_trash_close_returns_writer_after_cleanup_error(
    plate_nd2, monkeypatch, failure_site
):
    from nd2wsi.plate import PlateSource, PlateStore, PlateWriterLock, plate_container

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    source = PlateSource(plate_nd2)
    real_file = source._f

    class CloseThenError:
        def __getattr__(self, name):
            return getattr(real_file, name)

        def close(self):
            real_file.close()
            raise OSError("injected close report")

    if failure_site == "nd2_close":
        source._f = CloseThenError()
    else:
        class BrokenPrefetch:
            def is_alive(self):
                return True

            def join(self, timeout=None):
                raise RuntimeError("injected prefetch join failure")

        source._pf_thread = BrokenPrefetch()
    writer = None
    try:
        writer = source.close_for_trash(timeout=1.0)
        assert writer is not None and writer.acquired
        assert source._teardown_complete
        assert source._source_file.closed
        assert source._fd == -1

        contender = PlateWriterLock(plate_container(plate_nd2))
        with pytest.raises(TimeoutError):
            contender.acquire(timeout=0.05)
    finally:
        if writer is not None:
            writer.release()
        source.close()

    successor = PlateWriterLock(plate_container(plate_nd2))
    successor.acquire(timeout=0.1)
    successor.release()


def test_ordinary_close_timeout_stays_barred_and_finishes_after_drain(
    plate_nd2, monkeypatch
):
    from nd2wsi.plate import PlateSource, PlateStore

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    source = PlateSource(plate_nd2)
    entered = threading.Event()
    release = threading.Event()

    def hold_read():
        with source._life:
            entered.set()
            assert release.wait(timeout=10)

    active = threading.Thread(target=hold_read, daemon=True)
    active.start()
    successor = None
    try:
        assert entered.wait(timeout=5)
        assert source.store.writable
        source.close(timeout=0.01)
        retry = source._close_retry
        assert retry is not None and retry.is_alive()
        assert source._closed is True
        assert source._life.closed is True
        assert not source._f.closed
        with pytest.raises(ValueError, match="closed"):
            source.frame(0, 0, 0)

        release.set()
        active.join(timeout=5)
        retry.join(timeout=5)
        assert not retry.is_alive()
        assert source._teardown_complete
        assert source._f.closed
        assert source._fd == -1

        successor = PlateSource(plate_nd2)
        assert successor.store.writable
    finally:
        release.set()
        active.join(timeout=5)
        if successor is not None:
            successor.close()
        source.close()


def test_close_thread_start_failure_finishes_teardown_synchronously(
    plate_nd2, monkeypatch
):
    from nd2wsi.plate import PlateSource, PlateStore, PlateWriterLock, plate_container

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    source = PlateSource(plate_nd2)
    entered = threading.Event()
    release = threading.Event()

    def hold_read():
        with source._life:
            entered.set()
            assert release.wait(timeout=10)

    active = threading.Thread(target=hold_read, daemon=True)
    active.start()
    assert entered.wait(timeout=5)
    release_timer = threading.Timer(0.1, release.set)
    release_timer.start()
    real_start = threading.Thread.start

    def fail_close_retry(thread):
        if thread.name == "plate-close-retry":
            raise RuntimeError("injected close retry start failure")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_close_retry)
    try:
        source.close(timeout=0.01)
        active.join(timeout=5)
        release_timer.join(timeout=5)
        assert source._teardown_complete
        assert source._close_retry is None
        assert source._fd == -1

        successor = PlateWriterLock(plate_container(plate_nd2))
        successor.acquire(timeout=0.1)
        successor.release()
    finally:
        release.set()
        active.join(timeout=5)
        release_timer.join(timeout=5)
        source.close()


def test_close_thread_start_failure_after_worker_timeout_releases_writer(
    plate_nd2, monkeypatch
):
    from nd2wsi.plate import PlateSource, PlateStore, PlateWriterLock, plate_container

    monkeypatch.setattr(PlateStore, "start", lambda self: None)
    source = PlateSource(plate_nd2)

    class StopsOnlyForUnboundedJoin:
        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            if timeout is None:
                self.alive = False

    source.store._thread = StopsOnlyForUnboundedJoin()
    real_start = threading.Thread.start

    def fail_close_retry(thread):
        if thread.name == "plate-close-retry":
            raise RuntimeError("injected close retry start failure")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_close_retry)
    source.close(timeout=0.01)

    assert source._teardown_complete
    assert source._close_retry is None
    assert source._fd == -1
    successor = PlateWriterLock(plate_container(plate_nd2))
    successor.acquire(timeout=0.1)
    successor.release()
