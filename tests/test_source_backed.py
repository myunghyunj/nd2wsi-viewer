"""Source-backed compact storage (0.9): the file itself plays level 0."""
import threading
import time

import numpy as np
import pytest
import zarr

from nd2wsi.convert import convert, ensure_cache, open_store
from nd2wsi.reader import PlaneSelection, is_source_backable, plane_view

limnd2 = pytest.importorskip("limnd2")
nd2 = pytest.importorskip("nd2")


@pytest.fixture()
def big_nd2(tmp_path):
    """A 1600 x 1400 2-channel uint16 slide with an all-zero corner tile."""
    rng = np.random.default_rng(7)
    img = rng.integers(1, 4000, size=(1400, 1600, 2), dtype=np.uint16)  # (Y, X, C)
    img[:512, :512] = 0  # exactly one level-0 chunk of nothing per channel
    path = tmp_path / "slide.nd2"
    attrs = limnd2.ImageAttributes.create(
        width=1600, height=1400, component_count=2, bits=16, sequence_count=1
    )
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = attrs
        f.setImage(0, img)
        mf = limnd2.MetadataFactory(
            objective_magnification=20.0, pixel_calibration=0.5
        )
        mf.addPlane(name="CY5", color="#FF0000")
        mf.addPlane(name="DAPI", color="#0000FF")
        f.pictureMetadata = mf.createMetadata()
    return path, np.moveaxis(img, -1, 0)  # (C, Y, X)


def test_plane_view_is_a_zero_copy_view(big_nd2):
    path, cyx = big_nd2
    with nd2.ND2File(str(path)) as f:
        view, rgb = plane_view(f, PlaneSelection())
        assert not rgb and view.shape == (2, 1400, 1600)
        assert not view.flags.owndata  # a view onto the map, not a copy
        assert np.array_equal(np.asarray(view[:, 700:710, 800:810]),
                              cyx[:, 700:710, 800:810])
        ok, why = is_source_backable(f, PlaneSelection())
        assert ok, why


def test_overview_store_lists_only_what_it_holds(big_nd2, tmp_path):
    path, cyx = big_nd2
    out = tmp_path / "ov.ome.zarr"
    convert(path, out, tile=512, overview=True, progress=False)
    assert not (out / "0").exists()
    _, attrs = open_store(out)
    meta = attrs["nd2wsi"]
    assert meta["kind"] == "overview"
    assert meta["overview_of"] == {"channels": 2, "height": 1400, "width": 1600}
    assert [lv["path"] for lv in meta["levels"]] == ["1", "2"]
    assert meta["levels"][0]["width"] == 800 and meta["levels"][0]["height"] == 700
    ds = attrs["multiscales"][0]["datasets"]
    assert [d["path"] for d in ds] == ["1", "2"]


def test_source_built_level1_matches_the_stored_pipeline(big_nd2, tmp_path):
    path, _ = big_nd2
    full = tmp_path / "full.ome.zarr"
    ov = tmp_path / "ov.ome.zarr"
    convert(path, full, tile=512, progress=False)
    convert(path, ov, tile=512, overview=True, progress=False)
    fr = zarr.open_group(str(full), mode="r")
    ovr = zarr.open_group(str(ov), mode="r")
    for k in ("1", "2"):  # level 2 downsamples across omitted zero chunks
        assert np.array_equal(np.asarray(fr[k][:]), np.asarray(ovr[k][:]))


def test_all_zero_chunks_are_omitted_and_read_back_as_zero(big_nd2, tmp_path):
    path, cyx = big_nd2
    out = tmp_path / "full.ome.zarr"
    convert(path, out, tile=512, progress=False)
    for ci in (0, 1):
        assert not (out / "0" / f"{ci}.0.0").exists()  # the empty corner
        assert (out / "0" / f"{ci}.1.1").exists()
    root = zarr.open_group(str(out), mode="r")
    assert np.array_equal(np.asarray(root["0"][:]), cyx)  # zero-filled reads
    lv1 = np.asarray(root["1"][:])
    assert lv1[:, :256, :256].max() == 0
    assert lv1[:, 256:, 256:].max() > 0


def test_ensure_cache_prefers_a_compact_overview(big_nd2):
    from nd2wsi.cache import read_manifest

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    m = read_manifest(store.parent)
    assert m and m["kind"] == "overview"
    assert m["image"]["shape_cyx"] == [2, 1400, 1600]
    assert not (store / "0").exists()
    assert ensure_cache(path, tile=512) == store  # fast path, no rebuild


def test_ensure_cache_full_still_available(big_nd2):
    from nd2wsi.cache import read_manifest

    path, _ = big_nd2
    store = ensure_cache(path, tile=512, kind="full")
    assert (store / "0").exists()
    assert read_manifest(store.parent)["kind"] == "full"


def test_registry_serves_the_source_as_level_zero(big_nd2):
    from nd2wsi.server import SlideRegistry

    path, cyx = big_nd2
    store = ensure_cache(path, tile=512)
    reg = SlideRegistry()
    sid = reg.add_store(store)
    st = reg.get(sid)
    meta = st.attrs["nd2wsi"]
    assert meta["kind"] == "source-backed"
    assert [lv["path"] for lv in meta["levels"]] == ["0", "1", "2"]
    assert meta["levels"][0]["width"] == 1600
    got = st.root["0"][:, 600:700, 900:1000]
    assert got.flags.owndata  # reads hand out copies, never the map itself
    assert np.array_equal(got, cyx[:, 600:700, 900:1000])
    assert np.asarray(st.root["1"][:, :10, :10]).shape == (2, 10, 10)
    reg.remove(sid)


def test_missing_source_degrades_to_the_stored_overview(big_nd2):
    from nd2wsi import render
    from nd2wsi.server import SlideRegistry

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    hidden = path.with_name("elsewhere.nd2")
    path.rename(hidden)
    try:
        reg = SlideRegistry()
        st = reg.get(reg.add_store(store))
        meta = st.attrs["nd2wsi"]
        assert meta["kind"] == "overview-degraded"
        assert [lv["downsample"] for lv in meta["levels"]] == [1, 2]
        # a pixel of the new base level is twice the size, and the scalebar
        # and every measurement must say so
        assert meta["pixel_size_um"] == [1.0, 1.0]
        assert st.generation.endswith("-degraded")
        assert st.annotations_path is None  # level-0 work must not be halved
        assert any("missing" in n for n in meta["notes"])
        # levels are addressed by their stored path, not list position:
        # the base level here is "1" and there is no "0" to serve
        assert render.render_tile(st.root, st.attrs, 1, 0, 0, [0, 1])
        with pytest.raises(KeyError):
            render.render_tile(st.root, st.attrs, 0, 0, 0, [0, 1])
    finally:
        hidden.rename(path)


def test_source_return_upgrades_the_degraded_slide(big_nd2):
    from nd2wsi.server import SlideRegistry

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    hidden = path.with_name("elsewhere.nd2")
    path.rename(hidden)
    reg = SlideRegistry()
    sid = reg.add_store(store)
    assert reg.get(sid).attrs["nd2wsi"]["kind"] == "overview-degraded"
    hidden.rename(path)
    # the same sid re-registers against the restored source
    assert reg.add_store(store) == sid
    st = reg.get(sid)
    assert st.attrs["nd2wsi"]["kind"] == "source-backed"
    assert not st.generation.endswith("-degraded")
    reg.remove(sid)


def test_rebuild_evicts_the_registered_state(big_nd2):
    import os

    from nd2wsi.server import SlideRegistry

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    reg = SlideRegistry()
    sid = reg.add_store(store)
    gen0 = reg.get(sid).generation
    # the slide's mtime changes, so the cache rebuilds with a new generation
    os.utime(path, ns=(0, 0))
    assert ensure_cache(path, tile=512) == store
    assert reg.add_store(store) == sid
    st = reg.get(sid)
    assert st.generation != gen0  # the stale root was evicted, not reused
    assert st.attrs["nd2wsi"]["kind"] == "source-backed"
    reg.remove(sid)


def test_level0_reads_hand_out_real_copies(big_nd2):
    from nd2wsi.server import SlideRegistry

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    reg = SlideRegistry()
    sid = reg.add_store(store)
    st = reg.get(sid)
    # a full-width slice of a C-contiguous single plane is itself
    # contiguous, where ascontiguousarray would hand back the mapped view
    got = st.root["0"][0, 100:110, 0:1600]
    assert got.flags.owndata and got.base is None
    reg.remove(sid)


def test_source_changed_while_open_raises_instead_of_serving_garbage(big_nd2):
    import os

    from nd2wsi.server import SlideRegistry

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    reg = SlideRegistry()
    sid = reg.add_store(store)
    st = reg.get(sid)
    assert st.root["0"][:, :4, :4].shape == (2, 4, 4)
    os.utime(path)  # the file changed underneath the open map
    with pytest.raises(ValueError, match="changed"):
        st.root["0"][:, :4, :4]
    reg.remove(sid)


def test_overview_manifest_uses_the_new_format(big_nd2):
    from nd2wsi.cache import OVERVIEW_FORMAT, read_manifest

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    m = read_manifest(store.parent)
    # a pre-0.9 reader must reject this container instead of serving the
    # overview as a full store at half resolution
    assert m["format"] == OVERVIEW_FORMAT
    full = ensure_cache(path, tile=512, kind="full")
    assert read_manifest(full.parent)["format"] == "nd2wsi-cache/2"


def test_trash_refuses_while_an_export_runs(big_nd2):
    from nd2wsi.server import SlideRegistry

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    reg = SlideRegistry()
    sid = reg.add_store(store)
    st = reg.get(sid)
    with st.busy:  # an export is in flight
        with pytest.raises(ValueError, match="still running"):
            reg.trash_cache(sid)
    assert reg.trash_cache(sid) > 0  # drained: deletion proceeds
    assert path.exists()


def test_trash_removes_the_container_and_spares_the_source(big_nd2):
    from nd2wsi.server import SlideRegistry

    path, _ = big_nd2
    store = ensure_cache(path, tile=512)
    reg = SlideRegistry()
    sid = reg.add_store(store)
    freed = reg.trash_cache(sid)
    assert freed > 0
    assert not store.parent.exists()
    assert path.exists()


def test_lifecycle_waits_for_readers_then_bars_the_door():
    from nd2wsi.direct import _Lifecycle

    life = _Lifecycle()
    inside = threading.Event()
    release = threading.Event()

    def reader():
        with life:
            inside.set()
            release.wait(5)

    t = threading.Thread(target=reader)
    t.start()
    assert inside.wait(5)
    closed = []
    closer = threading.Thread(target=lambda: closed.append(life.close(timeout=5)))
    closer.start()
    time.sleep(0.1)
    assert not closed  # close is waiting on the active reader
    with pytest.raises(ValueError):
        life.__enter__()  # new reads are already barred
    release.set()
    closer.join(5)
    assert closed == [True]
    t.join(5)
