"""Native TIFF export reads each channel of a tile once and writes the same bytes.

tifffile wants separate planes plane-major, so the exporter walks every tile once
per output channel. It used to read all C channels of the tile on each of those
passes and keep one; the expected bytes here come from that iterator, frozen
verbatim below, never from the code under test.
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import zarr

from nd2wsi import render
from nd2wsi.convert import _downsample_into, build_group_attrs
from nd2wsi.reader import ChannelInfo, PlaneSource, level_shapes

TILE = 64


# --- oracle: render._read_region and render._roi_tile_iter as of a8709b6 ---------

def _frozen_read_region(root, level_path, x, y, w, h):
    """(C, h, w) region from one pyramid level, clipped to bounds."""
    arr = root[level_path]
    _, H, W = arr.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((arr.shape[0], 0, 0), dtype=arr.dtype)
    return np.asarray(arr[:, y0:y1, x0:x1])


def _frozen_roi_tile_iter(root, level_path, x, y, w, h, channels, tile):
    for ci in channels:
        for ty0 in range(0, h, tile):
            th = min(tile, h - ty0)
            for tx0 in range(0, w, tile):
                tw = min(tile, w - tx0)
                block = _frozen_read_region(root, level_path, x + tx0, y + ty0, tw, th)[ci]
                if th < tile or tw < tile:
                    pad = np.zeros((tile, tile), dtype=block.dtype)
                    pad[:th, :tw] = block
                    block = pad
                yield block


# --- helpers ----------------------------------------------------------------------

class Recording:
    """A level proxy that logs every index the exporter asks it for."""

    def __init__(self, level):
        self.level = level
        self.shape = tuple(level.shape)
        self.dtype = level.dtype
        self.calls = []

    def __getitem__(self, index):
        self.calls.append(index)
        return self.level[index]


def export_bytes(path: Path, root, attrs, level, roi, channels) -> bytes:
    render.export_roi_tiff(root, attrs, str(path), level, *roi, channels)
    return path.read_bytes()


def oracle_bytes(monkeypatch, path: Path, root, attrs, level, roi, channels) -> bytes:
    with monkeypatch.context() as m:
        m.setattr(render, "_roi_tile_iter", _frozen_roi_tile_iter)
        return export_bytes(path, root, attrs, level, roi, channels)


def expected_reads(roi, channels, tile=TILE):
    """One scalar-channel read per tile per output plane, in tifffile's order."""
    x, y, w, h = roi
    return [
        (ci, slice(y + ty0, y + min(ty0 + tile, h)), slice(x + tx0, x + min(tx0 + tile, w)))
        for ci in channels
        for ty0 in range(0, h, tile)
        for tx0 in range(0, w, tile)
    ]


def assert_one_scalar_read_per_tile(rec: Recording, roi, channels, tile=TILE):
    x, y, w, h = roi
    tiles = -(-w // tile) * -(-h // tile)
    assert len(rec.calls) == len(channels) * tiles
    for index in rec.calls:
        assert isinstance(index, tuple) and len(index) == 3
        assert isinstance(index[0], (int, np.integer)) and np.ndim(index[0]) == 0
    assert rec.calls == expected_reads(roi, channels, tile)


def decoded(path: Path):
    import tifffile

    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        layout = (len(tf.pages), page.shape, page.tile, page.planarconfig, page.dtype)
        return tf.asarray(), layout


def plane_source(dtype, channels: int, rgb: bool = False) -> PlaneSource:
    return PlaneSource(
        data=None, dtype=np.dtype(dtype), shape=(channels, 0, 0), rgb=rgb,
        channels=[ChannelInfo(f"C{i}", (255, 255, 255)) for i in range(channels)],
        pixel_size_um=(0.5, 0.5), source_name="example.nd2",
        selection={"t": 0, "p": 0, "z": 0},
    )


# --- a zarr pyramid: level 0 and reduced levels -------------------------------------

@pytest.fixture(params=["uint16", "uint8"])
def zarr_slide(request, tmp_path):
    dtype = np.dtype(request.param)
    hi = np.iinfo(dtype).max
    raw = np.random.default_rng(11).integers(0, hi, (3, 301, 389), dtype=dtype)
    raw[:, -1, :] = hi  # odd edges carry a value padding would never invent
    raw[:, :, -1] = hi
    store = zarr.open_group(str(tmp_path / "slide.zarr"), mode="w", zarr_format=2)
    shapes = level_shapes(*raw.shape[1:], TILE)
    store.create_array("0", shape=raw.shape, chunks=(1, TILE, TILE), dtype=dtype)[:] = raw
    for level, shape in enumerate(shapes[1:], start=1):
        target = store.create_array(str(level), shape=(3, *shape),
                                    chunks=(1, TILE, TILE), dtype=dtype)
        _downsample_into(store[str(level - 1)], target, TILE, dtype)
    attrs = build_group_attrs(plane_source(dtype, 3), shapes, TILE,
                              [dict(start=0, end=int(hi), min=0, max=int(hi))] * 3)
    try:
        yield store, attrs
    finally:
        store.store.close()


@pytest.mark.parametrize("channels", [[0, 1, 2], [0, 2], [2, 0], [1]])
@pytest.mark.parametrize("level,roi", [
    (0, (5, 3, 200, 150)),   # 4 x 3 tiles, right and bottom tiles padded
    (1, (7, 9, 131, 97)),    # a reduced zarr level, 3 x 2 tiles
    (2, (0, 0, 97, 75)),     # the whole level, both axes ending mid-tile
    (0, (388, 300, 1, 1)),   # one pixel in the last row and column
])
def test_zarr_levels_read_each_channel_once_and_write_identical_tiffs(
        zarr_slide, tmp_path, monkeypatch, level, roi, channels):
    store, attrs = zarr_slide
    rec = Recording(store[str(level)])
    actual = export_bytes(tmp_path / "new.tif", {str(level): rec}, attrs, level, roi, channels)
    assert_one_scalar_read_per_tile(rec, roi, channels)

    expected = oracle_bytes(monkeypatch, tmp_path / "old.tif", store, attrs, level, roi, channels)
    assert actual == expected

    x, y, w, h = roi
    truth = np.asarray(store[str(level)][:])[channels, y:y + h, x:x + w]
    if len(channels) == 1:
        truth = truth[0]
    pixels, (pages, shape, tile, planar, dtype) = decoded(tmp_path / "new.tif")
    np.testing.assert_array_equal(pixels, truth)
    assert (pages, shape, tile, dtype) == (1, truth.shape, (TILE, TILE), store["0"].dtype)
    assert (planar == 2) == (len(channels) > 1)  # separate planes for several channels


def test_rgb_store_channel_subset_still_writes_separate_planes(tmp_path, monkeypatch):
    rgb = np.random.default_rng(5).integers(0, 256, (3, 150, 140), dtype=np.uint8)
    attrs = build_group_attrs(plane_source(np.uint8, 3, rgb=True), [rgb.shape[1:]], TILE,
                              [dict(start=0, end=255, min=0, max=255)] * 3)
    roi, channels = (3, 4, 130, 129), [2, 0]
    rec = Recording(rgb)
    actual = export_bytes(tmp_path / "new.tif", {"0": rec}, attrs, 0, roi, channels)
    assert_one_scalar_read_per_tile(rec, roi, channels)
    assert actual == oracle_bytes(monkeypatch, tmp_path / "old.tif", {"0": rgb}, attrs, 0, roi,
                                  channels)


# --- a source-backed ND2: level 0 from the memory map, reduced levels stored ---------

@pytest.fixture(params=[16, 8])
def source_backed(request, tmp_path):
    limnd2 = pytest.importorskip("limnd2")
    pytest.importorskip("nd2")
    from nd2wsi.convert import ensure_cache
    from nd2wsi.server import SlideRegistry

    bits = request.param
    dtype = np.uint16 if bits == 16 else np.uint8
    img = np.random.default_rng(23).integers(1, 4000 if bits == 16 else 255,
                                             (390, 520, 3), dtype=dtype)  # (Y, X, C)
    path = tmp_path / "slide.nd2"
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = limnd2.ImageAttributes.create(
            width=520, height=390, component_count=3, bits=bits, sequence_count=1)
        f.setImage(0, img)
        mf = limnd2.MetadataFactory(objective_magnification=20.0, pixel_calibration=0.5)
        for name, color in (("CY5", "#FF0000"), ("FITC", "#00FF00"), ("DAPI", "#0000FF")):
            mf.addPlane(name=name, color=color)
        f.pictureMetadata = mf.createMetadata()
    reg = SlideRegistry()
    sid = reg.add_store(ensure_cache(path, tile=128))
    st = reg.get(sid)
    try:
        yield st, np.moveaxis(img, -1, 0)
    finally:
        reg.remove(sid)


@pytest.mark.parametrize("channels", [[0, 1, 2], [0, 2], [1]])
@pytest.mark.parametrize("level,roi", [
    (0, (37, 11, 301, 263)),  # the ND2 memory map itself, 3 x 3 tiles of 128
    (1, (5, 6, 250, 189)),    # a stored reduced level, 2 x 2 tiles
])
def test_source_backed_nd2_reads_each_channel_once_and_writes_identical_tiffs(
        source_backed, tmp_path, monkeypatch, level, roi, channels):
    from nd2wsi.direct import _Nd2Level0

    st, cyx = source_backed
    meta = st.attrs["nd2wsi"]
    assert meta["kind"] == "source-backed" and meta["tile"] == 128
    assert isinstance(st.root["0"], _Nd2Level0)
    rec = Recording(st.root[str(level)])
    actual = export_bytes(tmp_path / "new.tif", {str(level): rec}, st.attrs, level, roi, channels)
    assert_one_scalar_read_per_tile(rec, roi, channels, tile=128)
    assert actual == oracle_bytes(monkeypatch, tmp_path / "old.tif", st.root, st.attrs, level,
                                  roi, channels)
    if level == 0:
        x, y, w, h = roi
        pixels, _ = decoded(tmp_path / "new.tif")
        truth = cyx[channels, y:y + h, x:x + w]
        np.testing.assert_array_equal(pixels, truth[0] if len(channels) == 1 else truth)


# --- a shading-corrected slide: raw reduced levels recomputed from the original -----

def planes_read(calls, channels=3):
    """Channel planes fetched by a list of (C, Y, X) indices."""
    return sum(np.arange(channels)[index[0]].size for index in calls)


@pytest.fixture
def corrected_slide():
    from nd2wsi.server import ViewerState

    raw = np.random.default_rng(29).integers(0, 48, (3, 300, 410), dtype=np.uint16)
    raw[:, -1, :] = 65535
    raw[:, :, -1] = 65535

    class Native:
        shape, dtype = raw.shape, raw.dtype

        def __init__(self):
            self.calls = []

        def __getitem__(self, index):
            self.calls.append(index)
            return raw[index].copy()

    class Root(dict):
        pass

    shapes = level_shapes(*raw.shape[1:], TILE)
    # corrected display levels the native export must never read
    root = Root({str(k): np.full((3, *s), 7.5, np.float32) for k, s in enumerate(shapes)})
    root.raw_level0 = Native()
    attrs = build_group_attrs(plane_source(raw.dtype, 3), shapes, TILE,
                              [dict(start=0, end=255, min=0, max=255)] * 3)
    return ViewerState(root, attrs, manifest={"h_correction": {"applied": True}})


@pytest.mark.parametrize("channels", [[0, 1, 2], [2, 0]])
@pytest.mark.parametrize("level,roi", [
    (0, (3, 5, 133, 70)),
    (1, (9, 4, 141, 146)),
    (2, (3, 5, 97, 69)),
])
def test_corrected_original_levels_reduce_each_channel_once_and_write_identical_tiffs(
        corrected_slide, tmp_path, monkeypatch, level, roi, channels):
    from nd2wsi.region_export import _OriginalReducedLevel, prepare_export
    from nd2wsi.server import _frame_args

    st = corrected_slide
    native = st.root.raw_level0
    x, y, w, h = roi
    q = {"format": ["tiff"], "level": [str(level)], "x": [str(x)], "y": [str(y)],
         "w": [str(w)], "h": [str(h)], "c": [",".join(map(str, channels))]}
    plan = prepare_export(st, q, resolve_frame=_frame_args)
    assert (plan.x, plan.y, plan.w, plan.h, plan.channels) == (x, y, w, h, channels)
    assert level == 0 or isinstance(plan.root[str(level)], _OriginalReducedLevel)

    rec = Recording(plan.root[str(level)])
    export_bytes(tmp_path / "counted.tif", {str(level): rec}, st.attrs, level, roi, channels)
    assert_one_scalar_read_per_tile(rec, roi, channels)

    native.calls.clear()
    with plan.file(lambda *args, **kwargs: None) as result:
        actual = result.path.read_bytes()
    new_planes = planes_read(native.calls)
    assert all(np.ndim(index[0]) == 0 for index in native.calls)

    native.calls.clear()
    expected = oracle_bytes(monkeypatch, tmp_path / "old.tif", plan.root, st.attrs, level, roi,
                            channels)
    assert actual == expected
    # every per-plane pass used to read, and below level 0 box-reduce, all three channels
    assert planes_read(native.calls) == 3 * new_planes


# --- the channel read equals the old full read's slice on every level adapter -------

def _adapters(tmp_path):
    from PIL import Image

    from nd2wsi.direct import _Nd2Level0, _PersistedHLevel0
    from nd2wsi.direct_common import _Lifecycle, _SvsLevel
    from nd2wsi.plate import _ArrayLevel, _ReducedLevel
    from nd2wsi.raster import _RasterLevel
    from nd2wsi.region_export import _OriginalReducedLevel

    rng = np.random.default_rng(3)
    raw = rng.integers(0, 60000, (3, 71, 93), dtype=np.uint16)
    life = _Lifecycle()
    src = tmp_path / "fake.nd2"
    src.write_bytes(b"x")
    mapped = np.moveaxis(np.ascontiguousarray(np.moveaxis(raw, 0, -1)), -1, 0)  # strided, like a map

    class Model:
        indices, enabled = [0, 1, 2], [True, False, True]

        def correct(self, channel, values, y0, x0):
            yy, xx = np.mgrid[0:values.shape[0], 0:values.shape[1]]
            return values * (1.5 + channel) + (yy + y0) * 0.25 - (xx + x0) * 0.125

    group = zarr.open_group(str(tmp_path / "levels.zarr"), mode="w", zarr_format=2)
    for chunks in ((1, 32, 32), (3, 32, 32)):
        group.create_array(f"c{chunks[0]}", shape=raw.shape, chunks=chunks, dtype=raw.dtype)[:] = raw
    rgba = rng.integers(0, 256, (200, 300, 4), dtype=np.uint8)
    reduced = SimpleNamespace(frame_shape=raw.shape, dtype=raw.dtype,
                              reduced=lambda t, p, z, k: raw[:, ::k, ::k].copy())
    image = Image.fromarray(rgba[..., :3])
    return {
        "zarr planar chunks": group["c1"],
        "zarr interleaved chunks": group["c3"],
        "nd2 level 0": _Nd2Level0(mapped, life, src),
        "corrected level 0": _PersistedHLevel0(_Nd2Level0(mapped, life, src), Model(), life),
        "original reduced level": _OriginalReducedLevel(_OriginalReducedLevel(raw)),
        "original collapsed axis": _OriginalReducedLevel(raw[:, :1, :]),
        "svs level": _SvsLevel(lambda y0, y1, x0, x1: rgba[y0:y1, x0:x1], 100, 150, 2, life),
        "plate frame": _ArrayLevel(raw),
        "plate reduced": _ReducedLevel(reduced, 0, 0, 0, 2),
        "raster level": _RasterLevel(image, (100, 150), 2, life, threading.Lock()),
    }


def test_channel_region_equals_the_full_region_slice_on_every_level_adapter(tmp_path):
    for name, level in _adapters(tmp_path).items():
        root = {"L": level}
        c, height, width = level.shape
        windows = [(0, 0, width, height), (3, 5, 17, 11), (width - 7, height - 5, 20, 20),
                   (-4, -3, 9, 8), (width + 2, 0, 5, 5)]
        for ci in range(c):
            for window in windows:
                old = _frozen_read_region(root, "L", *window)[ci]
                new = render._read_channel_region(root, "L", ci, *window)
                assert new.ndim == 2, name
                assert (new.shape, new.dtype) == (old.shape, old.dtype), name
                np.testing.assert_array_equal(new, old, err_msg=name)
