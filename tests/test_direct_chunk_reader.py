"""Direct chunk reads from single-file caches equal Zarr's reads exactly."""
import itertools
import json
import threading
import time

import numpy as np
import pytest
import zarr
from numcodecs import Blosc, Delta, Zstd

from nd2wsi import render
from nd2wsi.convert import open_store
from nd2wsi.storage import single_file
from nd2wsi.storage.single_file import (
    _DirectReadArray,
    _DirectReadGroup,
    _spread_decodes,
    open_zarr_group,
)
from nd2wsi.storage.zarr_v2 import ZarrV2RegionReader

CACHE_CODEC = Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE)


def _write(path, data, chunks, *, compressors=CACHE_CODEC, separator=".", name="0", drop=(),
           **options):
    group = open_zarr_group(path, mode="w")
    try:
        array = group.create_array(
            name, shape=data.shape, chunks=chunks, dtype=data.dtype, compressors=compressors,
            chunk_key_encoding={"name": "v2", "separator": separator}, **options,
        )
        array[:] = data
        for key in drop:
            group.store.delete_sync(key)
    finally:
        group.store.close()


class _Spy:
    """Stands in for the Zarr array after the reader exists; counts fallbacks."""

    def __init__(self, array):
        self.array = array
        self.reads = []

    def __getattr__(self, name):
        return getattr(self.array, name)

    def __getitem__(self, selection):
        self.reads.append(selection)
        return self.array[selection]


def _spied(array):
    assert isinstance(array, _DirectReadArray)
    array._reader = array._make_reader()
    assert array._reader is not None
    spy = _Spy(array._array)
    array._array = spy
    return spy


def _same(want, got):
    want, got = np.asarray(want), np.asarray(got)
    assert (want.dtype, want.shape) == (got.dtype, got.shape)
    assert want.tobytes() == got.tobytes()


def _choices(axis, size):
    values = [0, size - 1, -1, -size, size, -size - 1, np.int64(size // 2), True,
              slice(None), slice(1, None), slice(None, -1), slice(2, 3), slice(-3, None),
              slice(0, size, 2), slice(3, 1), slice(-100, 100), slice(None, None, -1)]
    if axis == 0:
        values += [[0], [size - 1, 0], [0, 0], [], [-1], [size], [True], np.array([0])]
    return values


def _selections(shape, limit=1500):
    """Every combination for small ranks, a fixed sample of them otherwise."""
    axes = [_choices(axis, size) for axis, size in enumerate(shape)]
    combinations = list(itertools.product(*(range(len(values)) for values in axes)))
    if len(combinations) > limit:
        picks = np.random.default_rng(11).choice(len(combinations), limit, replace=False)
        combinations = [combinations[i] for i in sorted(picks)]
    for combination in combinations:
        yield tuple(values[i] for values, i in zip(axes, combination))


LAYOUTS = [
    pytest.param("<u2", (3, 37, 45), (1, 8, 16), CACHE_CODEC, ".", id="cache-u2"),
    pytest.param("|u1", (3, 20, 21), (2, 7, 5), None, "/", id="raw-u1-slash"),
    pytest.param("<f4", (2, 30, 30), (1, 30, 30), Zstd(level=1), ".", id="f4-one-chunk"),
    pytest.param("<i2", (4, 9, 11), (3, 4, 4), Blosc(), ".", id="i2-grouped-rows"),
    pytest.param("<u2", (17,), (5,), Blosc(), ".", id="1d"),
    pytest.param("<u2", (2, 3, 8, 9), (1, 2, 4, 4), Blosc(), ".", id="4d"),
]


@pytest.mark.parametrize("dtype, shape, chunks, codec, separator", LAYOUTS)
def test_every_selection_matches_zarr_values_shape_dtype_and_errors(
    tmp_path, dtype, shape, chunks, codec, separator,
):
    rng = np.random.default_rng(3)
    data = (rng.random(shape) * 250 - (60 if dtype[1] in "if" else 0)).astype(dtype)
    if data.dtype.kind == "f":
        data.flat[:3] = [np.nan, -0.0, np.inf]  # bit-exact copies, not value-equal ones
    path = tmp_path / "cache.nd2svs"
    grid = [-(-s // c) for s, c in zip(shape, chunks)]
    missing = "0/" + separator.join(str(g // 2) for g in grid)
    _write(path, data, chunks, compressors=codec, separator=separator, drop=[missing])
    group = open_zarr_group(path)
    try:
        array = group["0"]
        zarr_array = array._array
        served = 0
        for selection in _selections(shape):
            try:
                want, want_error = zarr_array[selection], None
            except Exception as exc:
                want, want_error = None, type(exc)
            try:
                got, got_error = array[selection], None
            except Exception as exc:
                got, got_error = None, type(exc)
            assert want_error is got_error, selection
            if want_error is None:
                _same(want, got)
                served += array._reader.read(selection) is not None
        assert served >= 5  # the comparison really exercised the direct path
    finally:
        group.store.close()


def test_tile_reads_skip_zarr_and_wide_windows_still_use_it(tmp_path):
    data = np.arange(3 * 40 * 50, dtype=np.uint16).reshape(3, 40, 50)
    path = tmp_path / "cache.nd2svs"
    _write(path, data, (1, 16, 16))
    group = open_zarr_group(path)
    try:
        array = group["0"]
        spy = _spied(array)
        served = [
            (slice(None), slice(16, 32), slice(16, 32)),  # an aligned tile, every channel
            (2, slice(32, 40), slice(48, 50)),  # an edge tile of one channel
            ([2, 0], slice(0, 16), slice(32, 48)),  # chosen channels, in their order
            (slice(1, 3), slice(18, 30), slice(1, 15)),  # a window inside one chunk
            (1, 5, slice(0, 16)),
        ]
        for selection in served:
            _same(data[selection] if not isinstance(selection[0], list)
                  else data[selection[0]][:, selection[1], selection[2]], array[selection])
        assert spy.reads == []
        wide = [
            (slice(None), slice(8, 24), slice(0, 16)),  # crosses a chunk row
            (0, slice(0, 16), slice(10, 20)),  # crosses a chunk column
            (slice(None),),  # not full rank
            slice(None),
            (0, 0, 0),  # Zarr returns a 0-d array
            ([0, 1], 0, 0),  # a coordinate selection in Zarr
        ]
        for selection in wide:
            _same(zarr.Array.__getitem__(spy.array, selection), array[selection])
        assert spy.reads == wide
        assert np.asarray(array).tobytes() == data.tobytes()  # __array__ still works
        assert (array.shape, array.dtype, array.chunks) == (data.shape, data.dtype, (1, 16, 16))
    finally:
        group.store.close()


def test_missing_chunks_read_as_the_zero_fill_value(tmp_path):
    data = np.full((2, 16, 16), 7, dtype=np.uint16)
    data[1, :8, :8] = 0  # an all-zero chunk Zarr never writes
    path = tmp_path / "cache.nd2svs"
    _write(path, data, (1, 8, 8), drop=["0/0.1.1"])
    group = open_zarr_group(path)
    try:
        array = group["0"]
        assert "0/1.0.0" not in list(group.store.list_keys("0/"))
        expected = data.copy()
        expected[0, 8:, 8:] = 0
        spy = _spied(array)
        _same(expected[:, 8:16, 8:16], array[:, 8:16, 8:16])
        _same(expected[:, 0:8, 0:8], array[:, 0:8, 0:8])
        assert spy.reads == []
    finally:
        group.store.close()


@pytest.mark.parametrize("options", [
    {"filters": [Delta(dtype="<u2")]},
    {"fill_value": 5},
    {"order": "F"},
], ids=["filters", "fill-value", "fortran-order"])
def test_layouts_only_zarr_can_read_stay_with_zarr(tmp_path, options):
    data = np.arange(2 * 16 * 16, dtype=np.uint16).reshape(2, 16, 16)
    path = tmp_path / "cache.nd2svs"
    _write(path, data, (1, 8, 8), drop=["0/1.1.1"], **options)
    group = open_zarr_group(path)
    try:
        array = group["0"]
        assert array._make_reader() is None
        expected = np.asarray(array._array[:, 8:16, 8:16])
        _same(expected, array[:, 8:16, 8:16])
    finally:
        group.store.close()


def test_reader_refuses_metadata_it_cannot_reproduce():
    base = {"zarr_format": 2, "shape": [2, 8, 8], "chunks": [1, 4, 4], "dtype": "<u2",
            "fill_value": 0, "order": "C", "filters": None, "compressor": None}

    def fetch(_key):
        raise AssertionError("not read")

    assert ZarrV2RegionReader(base, "0", fetch).shape == (2, 8, 8)
    assert ZarrV2RegionReader.open("0", {"0/.zarray": json.dumps(base).encode()}.get).shape == (2, 8, 8)
    assert ZarrV2RegionReader.open("0", {}.get) is None  # no metadata: Zarr decides
    assert ZarrV2RegionReader.open("0", {"0/.zarray": b'{"zarr_format": 3}'}.get) is None
    assert ZarrV2RegionReader.open("0", {"0/.zarray": b"not json"}.get) is None
    for change in [{"zarr_format": 3}, {"order": "F"}, {"filters": [{"id": "delta", "dtype": "<u2"}]},
                   {"fill_value": None}, {"fill_value": "NaN"}, {"fill_value": 1},
                   {"fill_value": -0.0, "dtype": "<f4"}, {"fill_value": True},
                   {"dtype": ">u2"}, {"dtype": [["a", "<u2"]]}, {"dtype": "|O"},
                   {"chunks": [1, 4]}, {"chunks": [0, 4, 4]}, {"dimension_separator": "_"},
                   {"compressor": {"id": "no-such-codec"}}]:
        with pytest.raises((ValueError, TypeError)):
            ZarrV2RegionReader({**base, **change}, "0", fetch)


def test_corrupt_chunk_and_closed_store_fail_like_zarr(tmp_path):
    data = np.arange(2 * 16 * 16, dtype=np.uint16).reshape(2, 16, 16)
    path = tmp_path / "cache.nd2svs"
    _write(path, data, (1, 8, 8))
    with single_file.SQLiteStore(path, prefix="store.ome.zarr") as store:
        store.write_bytes("0/1.0.0", b"not a blosc frame")
    group = open_zarr_group(path)
    array, unread = group["0"], group["0"]
    spy = _spied(array)
    for selection in [(1, slice(0, 8), slice(0, 8)), (slice(None), slice(0, 8), slice(0, 8))]:
        with pytest.raises(RuntimeError) as zarr_error:
            zarr.Array.__getitem__(spy.array, selection)
        with pytest.raises(RuntimeError) as direct_error:
            array[selection]
        assert str(direct_error.value) == str(zarr_error.value)
    assert spy.reads == []
    group.store.close()
    for selection in [(0, slice(0, 8), slice(0, 8)), (0, slice(0, 16), slice(0, 16))]:
        for closed in (array, unread):  # with and without a reader built before the close
            with pytest.raises(ValueError, match="closed"):
                closed[selection]  # served and fallback reads alike
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cache.nd2svs"]


def test_read_only_groups_hand_out_direct_arrays_and_writable_groups_do_not(tmp_path):
    path = tmp_path / "cache.nd2svs"
    group = open_zarr_group(path, mode="w")
    try:
        sub = group.create_group("level")
        sub.create_array("0", shape=(1, 4, 4), chunks=(1, 4, 4), dtype="u2")[:] = 3
        group.attrs["name"] = "kept"
    finally:
        group.store.close()
    for mode in ("r+", "a"):
        writable = open_zarr_group(path, mode=mode)
        try:
            assert isinstance(writable, zarr.Group)
            assert isinstance(writable["level"]["0"], zarr.Array)
        finally:
            writable.store.close()
    group = open_zarr_group(path)
    try:
        assert isinstance(group, _DirectReadGroup)
        assert "level" in group and list(group) == ["level"] and len(group) == 1
        assert group.attrs["name"] == "kept"
        level = group["level"]
        assert isinstance(level, _DirectReadGroup)
        array = level["0"]
        assert isinstance(array, _DirectReadArray)
        _same(np.full((4, 4), 3, np.uint16), array[0, :, :])
        assert array._reader is not None
        with pytest.raises(Exception) as zarr_error:
            array._array[0, 0, 0] = 1
        with pytest.raises(type(zarr_error.value)):
            array[0, 0, 0] = 1  # still read-only, through Zarr
        assert "SQLiteStore" in repr(group.store)
    finally:
        group.store.close()


def test_open_store_tiles_read_directly_and_match_zarr(tmp_path):
    rng = np.random.default_rng(5)
    data = rng.integers(0, 65535, (2, 40, 36), dtype=np.uint16)
    path = tmp_path / "pyramid.nd2svs"
    group = open_zarr_group(path, mode="w")
    try:
        group.create_array("0", data=data, chunks=(1, 16, 16), compressors=CACHE_CODEC)
        group.attrs.update({
            "multiscales": [{"datasets": [{"path": "0"}]}],
            "nd2wsi": {"tile": 16, "rgb": False, "dtype": "uint16",
                       "levels": [{"path": "0", "width": 36, "height": 40, "downsample": 1}]},
            "omero": {"channels": [{"color": "FF0000", "window": {"start": 0, "end": 65535}},
                                   {"color": "00FF00", "window": {"start": 0, "end": 65535}}]},
        })
    finally:
        group.store.close()
    root, attrs = open_store(path)
    try:
        level = root["0"]
        assert root["0"] is level and isinstance(level, _DirectReadArray)
        spy = _spied(level)
        for tx, ty in itertools.product(range(3), range(3)):
            x, y = tx * 16, ty * 16
            _same(data[:, y:y + 16, x:x + 16], render._read_region(root, "0", x, y, 16, 16))
            _same(data[1, y:y + 16, x:x + 16],
                  render._read_channel_region(root, "0", 1, x, y, 16, 16))
            tile = render.render_tile(root, attrs, 0, tx, ty, [0, 1], "png")
            level._array = spy.array
            try:
                assert render.render_tile(root, attrs, 0, tx, ty, [0, 1], "png") == tile
            finally:
                level._array = spy
        assert spy.reads == []
    finally:
        root.close()


def test_concurrent_tile_reads_agree_with_zarr(tmp_path):
    rng = np.random.default_rng(9)
    data = rng.integers(0, 4096, (4, 64, 64), dtype=np.uint16)
    path = tmp_path / "cache.nd2svs"
    _write(path, data, (1, 16, 16), drop=["0/2.1.1"])
    expected = data.copy()
    expected[2, 16:32, 16:32] = 0
    group = open_zarr_group(path)
    try:
        array = group["0"]
        tiles = [(y, x) for y in range(0, 64, 16) for x in range(0, 64, 16)] * 8
        errors = []

        def read(tile):
            y, x = tile
            try:
                _same(expected[:, y:y + 16, x:x + 16], array[:, y:y + 16, x:x + 16])
                _same(expected[[3, 1], y:y + 16, x:x + 16], array[[3, 1], y:y + 16, x:x + 16])
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=lambda part=tiles[i::6]: [read(t) for t in part])
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not errors and not any(thread.is_alive() for thread in threads)
    finally:
        group.store.close()


def test_spread_waits_for_every_decode_before_raising():
    finished = []

    def slow(index):
        time.sleep(0.05)
        finished.append(index)

    def failing():
        raise OSError("first decode failed")

    with pytest.raises(OSError, match="first decode"):
        _spread_decodes([failing, *(lambda i=i: slow(i) for i in range(3))])
    assert sorted(finished) == [0, 1, 2]

    finished.clear()

    def late_failure():
        time.sleep(0.02)
        raise KeyError("pool decode failed")

    with pytest.raises(KeyError, match="pool decode"):
        _spread_decodes([lambda: slow("here"), late_failure, lambda: slow("pool")])
    assert sorted(finished) == ["here", "pool"]


def test_spread_runs_everything_here_when_the_pool_refuses_work(monkeypatch):
    class Refusing:
        def submit(self, _task):
            raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    monkeypatch.setattr(single_file, "_chunk_pool", lambda: Refusing())
    ran = []
    _spread_decodes([lambda i=i: ran.append((i, threading.get_ident())) for i in range(4)])
    assert [i for i, _ in ran] == [0, 1, 2, 3]
    assert {ident for _, ident in ran} == {threading.get_ident()}


def test_a_forked_child_gets_its_own_decode_pool(monkeypatch):
    current = single_file._chunk_pool()
    assert single_file._chunk_pool() is current
    stale = object()
    monkeypatch.setattr(single_file, "_CHUNK_POOL", (-1, stale))
    fresh = single_file._chunk_pool()
    assert fresh is not stale and fresh is not current
    assert fresh.submit(lambda: 42).result(5) == 42
    fresh.shutdown(wait=True)
