"""Hand-written golden values for every k x k box mean, k = 2, 4, 8 and 16.

The contract every reducer shares (``box-mean-floor-v1`` at k=2): each box is
averaged in float32, an integer output is rounded with ``np.rint``
(ties-to-even), a floating output is never rounded, and rows or columns that
do not fill a whole box are dropped. ``tests/test_box_mean_golden.py`` pins
the 2x reducers with literal 8x8 tables; this module pins the k-fold users:

* ``nd2wsi.plate.PlateSource.reduced`` (k = 2, 4, 8, 16), read back from a
  real ND2 whose sites carry the tables below;
* ``nd2wsi.direct_common._SvsLevel`` gap levels (e = 2, 4, 8) of SVS and
  SDPC, whose reads arrive channels-last with an alpha channel;
* ``nd2wsi.convert._write_level1_from_source``, the CPU level 1 of a cache
  built straight from the source.

Every expected value is worked out by hand in the comment above ``_cases``
(sum, mean, rounding); none is computed with the code under test. Each box is
described by the values it holds, filled in raster order (``_fill``), so a
kernel that groups the wrong rows or columns also lands on the wrong answer:
every designed box sits between background boxes of a different value.
"""
from __future__ import annotations

import numpy as np
import pytest

from nd2wsi import direct_common

BACKGROUND_U16 = 1000
BACKGROUND_U8 = 100
CROP_U16 = 65535  # rows/columns past the last whole box: must never count
CROP_U8 = 255

# One row per designed box: (name, runs as fractions of n = k*k, expected).
# A run is (value, count) with count written as a function of n so one table
# serves every k. ``half`` = n/2, ``quarter`` = n/4. The expected value is the
# rounded mean, worked out below for every k because it does not depend on k
# except in the two "one pixel" rows, which list it per k.
#
#   zeros          n x 0                      sum 0           mean 0        -> 0
#   tie-to-0       half x 0, half x 1         sum half        mean 0.5      -> 0 (tie, even)
#   tie-to-2       half x 1, half x 2         sum 3*half      mean 1.5      -> 2 (tie, even)
#   tie-stays-2    half x 2, half x 3         sum 5*half      mean 2.5      -> 2 (tie, even)
#   quarter        3*quarter x 0, quarter x 1 sum quarter     mean 0.25     -> 0
#   three-quarter  quarter x 0, 3*quarter x 1 sum 3*quarter   mean 0.75     -> 1
#   under-tie      (half+1) x 0, (half-1) x 1 sum half-1      mean 0.5-1/n  -> 0
#   over-tie       (half-1) x 0, (half+1) x 1 sum half+1      mean 0.5+1/n  -> 1
#   mixed          quarter each of 100/200/300/400            mean 250      -> 250 (exact)
#   max            n x MAX                                    mean MAX      -> MAX
#   tie-at-max     half x (MAX-1), half x MAX                 mean MAX-0.5  -> MAX-1 (MAX-1 even)
#   half-max       half x MAX, half x 0                       mean MAX/2    -> see table
#   over-tie-max   (half-1) x (MAX-1), (half+1) x MAX         mean MAX-0.5+1/n -> MAX
#   one-short      (n-1) x MAX, 1 x (MAX-1)                   mean MAX-1/n  -> MAX
#   hot-pixel      1 x MAX, (n-1) x 0                         mean MAX/n    -> see table
#
# uint16 (MAX 65535):  half-max 32767.5 -> 32768 (tie, even 32768)
#   hot-pixel k=2: 65535/4   = 16383.75     -> 16384
#             k=4: 65535/16  = 4095.9375    -> 4096
#             k=8: 65535/64  = 1023.984375  -> 1024
#            k=16: 65535/256 = 255.99609375 -> 256
#   A k=16 box of 65535 sums to 16,776,960: under 2**24, so the float32
#   mean is exact, and far over 2**16, so a uint16 accumulator would wrap.
# uint8 (MAX 255): "mixed" uses 10/20/30/40 (mean 25) instead.
#   half-max 127.5 -> 128 (tie, even 128)
#   hot-pixel k=2: 255/4   = 63.75      -> 64
#             k=4: 255/16  = 15.9375    -> 16
#             k=8: 255/64  = 3.984375   -> 4
#            k=16: 255/256 = 0.99609375 -> 1
#   A k=16 box of 255 sums to 65,280, and rounding adds at most 128 more:
#   65,408 still fits the uint16 accumulator the exact route uses.
KS = (2, 4, 8, 16)


def _cases(dtype):
    big = 65535 if dtype == np.uint16 else 255
    mixed = (100, 200, 300, 400) if dtype == np.uint16 else (10, 20, 30, 40)
    mixed_mean = 250 if dtype == np.uint16 else 25
    half_max = 32768 if dtype == np.uint16 else 128
    hot = {2: 16384, 4: 4096, 8: 1024, 16: 256} if dtype == np.uint16 else {2: 64, 4: 16, 8: 4, 16: 1}
    return [
        ("zeros", lambda n: [(0, n)], 0),
        ("tie-to-0", lambda n: [(0, n // 2), (1, n // 2)], 0),
        ("tie-to-2", lambda n: [(1, n // 2), (2, n // 2)], 2),
        ("tie-stays-2", lambda n: [(2, n // 2), (3, n // 2)], 2),
        ("quarter", lambda n: [(0, 3 * n // 4), (1, n // 4)], 0),
        ("three-quarter", lambda n: [(0, n // 4), (1, 3 * n // 4)], 1),
        ("under-tie", lambda n: [(0, n // 2 + 1), (1, n // 2 - 1)], 0),
        ("over-tie", lambda n: [(0, n // 2 - 1), (1, n // 2 + 1)], 1),
        ("mixed", lambda n: [(v, n // 4) for v in mixed], mixed_mean),
        ("max", lambda n: [(big, n)], big),
        ("tie-at-max", lambda n: [(big - 1, n // 2), (big, n // 2)], big - 1),
        ("half-max", lambda n: [(big, n // 2), (0, n // 2)], half_max),
        ("over-tie-max", lambda n: [(big - 1, n // 2 - 1), (big, n // 2 + 1)], big),
        ("one-short", lambda n: [(big, n - 1), (big - 1, 1)], big),
        ("hot-pixel", lambda n: [(big, 1), (0, n - 1)], hot),
    ]


# 71 x 73 frames: whole boxes cover 70x72 (k=2), 68x72 (k=4), 64x72 (k=8)
# and 64x64 (k=16); every k leaves a margin that must be dropped.
FRAME_H, FRAME_W = 71, 73


def _fill(frame, by, bx, k, runs):
    """Write one k x k box from (value, count) runs, raster order."""
    values = [v for v, count in runs for _ in range(count)]
    assert len(values) == k * k
    frame[by * k:(by + 1) * k, bx * k:(bx + 1) * k] = np.array(values).reshape(k, k)


def _table(k, dtype):
    """(frame, golden) for one k: designed boxes in raster order, then background."""
    background = BACKGROUND_U16 if dtype == np.uint16 else BACKGROUND_U8
    crop = CROP_U16 if dtype == np.uint16 else CROP_U8
    gh, gw = FRAME_H // k, FRAME_W // k
    frame = np.full((FRAME_H, FRAME_W), crop, dtype=dtype)
    frame[: gh * k, : gw * k] = background
    golden = np.full((gh, gw), background, dtype=dtype)
    for i, (_name, runs, expected) in enumerate(_cases(dtype)):
        by, bx = divmod(i, gw)
        _fill(frame, by, bx, k, runs(k * k))
        golden[by, bx] = expected[k] if isinstance(expected, dict) else expected
    return frame, golden


def test_tables_place_every_case_and_drop_a_margin():
    for k in KS:
        frame, golden = _table(k, np.uint16)
        assert golden.shape == (FRAME_H // k, FRAME_W // k)
        assert golden.size > len(_cases(np.uint16))  # background boxes remain
        assert FRAME_H % k and FRAME_W % k  # a margin to drop at every k
        assert frame[-1, -1] == CROP_U16


# ---------------------------------------------------------------------------
# (b) the plate reductions, read from a real ND2 whose sites hold the tables
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def golden_plate(tmp_path_factory):
    limnd2 = pytest.importorskip("limnd2")
    pytest.importorskip("nd2")
    path = tmp_path_factory.mktemp("box-mean") / "golden.nd2"
    frames = [_table(k, np.uint16)[0] for k in KS]  # site p holds the table for KS[p]
    attrs = limnd2.ImageAttributes.create(
        width=FRAME_W, height=FRAME_H, component_count=1, bits=16,
        sequence_count=len(frames),
    )
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = attrs
        f.experiment = limnd2.ExperimentFactory(
            m={"count": len(frames), "xcoords": [100.0 * i for i in range(len(frames))],
               "ycoords": [0.0] * len(frames)},
        ).createExperiment()
        for seq, frame in enumerate(frames):
            f.setImage(seq, frame[..., np.newaxis])
        mf = limnd2.MetadataFactory(objective_magnification=20.0, pixel_calibration=0.5)
        mf.addPlane(name="br", color="#FFFFFF")
        f.pictureMetadata = mf.createMetadata()
    return path


@pytest.mark.parametrize("k", KS)
def test_plate_reduction_matches_golden(golden_plate, k):
    from nd2wsi.plate import PlateSource

    source = PlateSource(golden_plate, store=False)
    try:
        assert (source.T, source.P, source.Z) == (1, len(KS), 1)
        p = KS.index(k)
        frame, golden = _table(k, np.uint16)
        np.testing.assert_array_equal(source.frame(0, p, 0)[0], frame)
        out = source.reduced(0, p, 0, k)
        assert out.dtype == np.uint16 and out.flags.c_contiguous
        np.testing.assert_array_equal(out, golden[np.newaxis])
    finally:
        source.close()


# ---------------------------------------------------------------------------
# (c) SVS/SDPC gap levels: channels-last reads with an alpha channel
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("e", (2, 4, 8))
def test_svs_gap_level_matches_golden(e):
    frame, golden = _table(e, np.uint8)
    gh, gw = golden.shape
    # R holds the table, G is black, B is white, alpha is junk that must go.
    rgba = np.stack(
        [frame, np.zeros_like(frame), np.full_like(frame, 255), np.full_like(frame, 17)],
        axis=-1,
    )
    before = rgba.copy()
    reads = []

    def read(y0, y1, x0, x1):
        reads.append((y0, y1, x0, x1))
        return rgba[y0:y1, x0:x1]

    level = direct_common._SvsLevel(read, gh, gw, e)
    out = level[:, :, :]
    assert reads == [(0, gh * e, 0, gw * e)]
    assert out.shape == (3, gh, gw) and out.dtype == np.uint8 and out.flags.c_contiguous
    np.testing.assert_array_equal(out[0], golden)
    assert not out[1].any() and (out[2] == 255).all()
    # a window lands on the same box grid instead of re-originating it
    np.testing.assert_array_equal(level[0, 1:, 2:], golden[1:, 2:])
    np.testing.assert_array_equal(rgba, before)  # a cached native tile stays intact


# ---------------------------------------------------------------------------
# (d) the converter's level 1 straight from the source (GPU off)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dtype", [np.uint16, np.uint8])
@pytest.mark.parametrize("tile", [4, 64])
def test_level1_from_source_matches_golden(tmp_path, monkeypatch, dtype, tile):
    from types import SimpleNamespace

    import dask.array as da

    from nd2wsi.convert import _write_level1_from_source
    from nd2wsi.storage import ZarrV2Storage

    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "0")
    frame, golden = _table(2, dtype)
    # Channel 1 is flat 3 inside the boxes and junk in the dropped margin.
    flat = np.full_like(frame, CROP_U16 if dtype == np.uint16 else CROP_U8)
    flat[: golden.shape[0] * 2, : golden.shape[1] * 2] = 3
    values = np.stack([frame, flat])
    storage = ZarrV2Storage()
    root = storage.create_group(tmp_path / "pyramid")
    # Source chunks that straddle the box grid, as an ND2 memory map may.
    source = SimpleNamespace(data=da.from_array(values, chunks=(1, 7, 11)),
                             shape=values.shape, dtype=values.dtype)
    level = storage.create_array(root, "1", (2, *golden.shape), values.dtype, tile=tile)
    _write_level1_from_source(source, level, storage)
    out = np.asarray(level[:])
    assert out.dtype == golden.dtype
    np.testing.assert_array_equal(out[0], golden)
    assert (out[1] == 3).all()
