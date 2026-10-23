"""Hand-written golden values for the ``box-mean-floor-v1`` 2x reduction.

The contract (``nd2wsi.cache.ALGORITHM``): a 2x2 box mean accumulated in
float32, rounded with ``np.rint`` (ties-to-even) for an integer output and
never rounded for a floating output, an odd trailing row or column dropped,
and an axis already collapsed to one pixel repeated by the CALLER before it
reduces.

Three implementations share that contract and are pinned here against the
same literal tables: ``nd2wsi.metal.reduce2x_or_cpu`` (plus the Metal kernel
itself when it is enabled and available), the converter's
``_downsample_into`` level writer, and
``nd2wsi.region_export._OriginalReducedLevel``. The last two now route every
block through ``reduce2x_or_cpu``, whose CPU half is ``reduce2x_cpu``, so
the tables also pin that single kernel directly. Every expected value below
was worked out by hand in the comments next to it; none is computed with
NumPy or with the code under test. That keeps this module an oracle that is
independent of the implementations it checks, which neither
``tests/test_metal_pyramid.py`` (``_reference`` is the production formula)
nor the ``persisted_correction`` fixture (expected levels come from the
converter) can be.

Two reducers deliberately keep their own arithmetic. They are neither
covered here nor routed through the shared helper: ``nd2wsi.plate`` reduces
by a k-fold mean where k may differ from 2, and
``nd2wsi.h_persistence._reduce2`` builds the corrected pyramid with float64
accumulation, float32 output and no rounding. The Metal dispatch order is
also unchanged: the converter still asks the GPU kernel first and only then
falls back to the exact CPU mean.
"""
from __future__ import annotations

import numpy as np
import pytest

from nd2wsi import metal
from nd2wsi.region_export import _OriginalReducedLevel

# ---------------------------------------------------------------------------
# uint16, 8x8 -> 4x4. Each 2x2 block is written as [[a, b], [c, d]] with a, b
# on the even input row and c, d on the odd row below it. mean = (a+b+c+d)/4.
#
# block row 0 (input rows 0-1):
#   B00 [[0,0],[0,0]]          sum 0      mean 0        -> 0      (zeros)
#   B01 [[0,0],[1,1]]          sum 2      mean 0.5      -> 0      (tie, even 0)
#   B02 [[1,1],[2,2]]          sum 6      mean 1.5      -> 2      (tie, even 2)
#   B03 [[2,2],[3,3]]          sum 10     mean 2.5      -> 2      (tie, even 2)
# block row 1 (input rows 2-3):
#   B10 [[3,3],[4,4]]          sum 14     mean 3.5      -> 4      (tie, even 4)
#   B11 [[1,0],[0,0]]          sum 1      mean 0.25     -> 0      (.25 rounds down)
#   B12 [[1,1],[1,0]]          sum 3      mean 0.75     -> 1      (.75 rounds up)
#   B13 [[65535]*2]*2          sum 262140 mean 65535    -> 65535  (max; a uint16
#        accumulator would wrap 262140 to 65532 and yield 16383)
# block row 2 (input rows 4-5):
#   B20 [[65535,65535],[0,0]]  sum 131070 mean 32767.5  -> 32768  (tie, even 32768)
#   B21 [[65535,0],[0,0]]      sum 65535  mean 16383.75 -> 16384  (.75 rounds up)
#   B22 [[65535,65535],[65535,0]] sum 196605 mean 49151.25 -> 49151 (.25 rounds down)
#   B23 [[65534,65534],[65535,65535]] sum 262138 mean 65534.5 -> 65534 (tie, even)
# block row 3 (input rows 6-7):
#   B30 [[100,200],[300,400]]  sum 1000   mean 250      -> 250    (exact, mixed)
#   B31 [[7,9],[11,13]]        sum 40     mean 10       -> 10     (exact, mixed)
#   B32 [[4,4],[5,5]]          sum 18     mean 4.5      -> 4      (tie, even 4)
#   B33 [[5,5],[6,6]]          sum 22     mean 5.5      -> 6      (tie, even 6)
# ---------------------------------------------------------------------------
INPUT_U16 = np.array(
    [
        [0, 0, 0, 0, 1, 1, 2, 2],  # B00 B01 B02 B03 (top rows)
        [0, 0, 1, 1, 2, 2, 3, 3],  # B00 B01 B02 B03 (bottom rows)
        [3, 3, 1, 0, 1, 1, 65535, 65535],  # B10 B11 B12 B13
        [4, 4, 0, 0, 1, 0, 65535, 65535],  # B10 B11 B12 B13
        [65535, 65535, 65535, 0, 65535, 65535, 65534, 65534],  # B20 B21 B22 B23
        [0, 0, 0, 0, 65535, 0, 65535, 65535],  # B20 B21 B22 B23
        [100, 200, 7, 9, 4, 4, 5, 5],  # B30 B31 B32 B33
        [300, 400, 11, 13, 5, 5, 6, 6],  # B30 B31 B32 B33
    ],
    dtype=np.uint16,
)
GOLDEN_U16 = np.array(
    [
        [0, 0, 2, 2],
        [4, 0, 1, 65535],
        [32768, 16384, 49151, 65534],
        [250, 10, 4, 6],
    ],
    dtype=np.uint16,
)

# ---------------------------------------------------------------------------
# uint8, 8x8 -> 4x4. Block rows 0, 1 (except B13) and 3 repeat the small
# values above; the large blocks use the uint8 maximum instead.
#   B13 [[255]*2]*2            sum 1020  mean 255     -> 255  (max; a uint8
#        accumulator would wrap 1020 to 252 and yield 63)
#   B20 [[255,255],[0,0]]      sum 510   mean 127.5   -> 128  (tie, even 128)
#   B21 [[255,0],[0,0]]        sum 255   mean 63.75   -> 64   (.75 rounds up)
#   B22 [[255,255],[255,0]]    sum 765   mean 191.25  -> 191  (.25 rounds down)
#   B23 [[254,254],[255,255]]  sum 1018  mean 254.5   -> 254  (tie, even 254)
#   B30 [[10,20],[30,40]]      sum 100   mean 25      -> 25   (exact, mixed)
# ---------------------------------------------------------------------------
INPUT_U8 = np.array(
    [
        [0, 0, 0, 0, 1, 1, 2, 2],
        [0, 0, 1, 1, 2, 2, 3, 3],
        [3, 3, 1, 0, 1, 1, 255, 255],
        [4, 4, 0, 0, 1, 0, 255, 255],
        [255, 255, 255, 0, 255, 255, 254, 254],
        [0, 0, 0, 0, 255, 0, 255, 255],
        [10, 20, 7, 9, 4, 4, 5, 5],
        [30, 40, 11, 13, 5, 5, 6, 6],
    ],
    dtype=np.uint8,
)
GOLDEN_U8 = np.array(
    [
        [0, 0, 2, 2],
        [4, 0, 1, 255],
        [128, 64, 191, 254],
        [25, 10, 4, 6],
    ],
    dtype=np.uint8,
)

# ---------------------------------------------------------------------------
# float32, 4x4 -> 2x2. Nothing may be rounded: every mean keeps its fraction.
#   B00 [[0.5,0.5],[0.5,0.5]]          sum 2.0    mean 0.5     (rint would give 0)
#   B01 [[1.0,2.0],[2.0,1.0]]          sum 6.0    mean 1.5     (rint would give 2)
#   B10 [[1000.25]*2]*2                sum 4001.0 mean 1000.25 (rint would give 1000)
#   B11 [[3.0,3.0],[3.0,0.0]]          sum 9.0    mean 2.25    (rint would give 2)
# All inputs, sums and quotients are exactly representable in float32.
# ---------------------------------------------------------------------------
INPUT_F32 = np.array(
    [
        [0.5, 0.5, 1.0, 2.0],
        [0.5, 0.5, 2.0, 1.0],
        [1000.25, 1000.25, 3.0, 3.0],
        [1000.25, 1000.25, 3.0, 0.0],
    ],
    dtype=np.float32,
)
GOLDEN_F32 = np.array([[0.5, 1.5], [1000.25, 2.25]], dtype=np.float32)

# 9x9: the uint16 table with a ninth row and column of 65535 appended. The
# floor contract drops them, so the result is the 4x4 table above, not a 5x5
# and not a table whose last row/column absorbed any 65535.
INPUT_U16_9X9 = np.full((9, 9), 65535, dtype=np.uint16)
INPUT_U16_9X9[:8, :8] = INPUT_U16

# A level already collapsed to one row, (1, 4) -> (1, 2). The caller repeats
# the single row before reducing, so each block is [[a,b],[a,b]]:
#   [[1,2],[1,2]] sum 6  mean 1.5 -> 2 (tie, even 2)
#   [[3,4],[3,4]] sum 14 mean 3.5 -> 4 (tie, even 4)
INPUT_ONE_ROW = np.array([[1, 2, 3, 4]], dtype=np.uint16)
GOLDEN_ONE_ROW = np.array([[2, 4]], dtype=np.uint16)

# Two 2x reductions are not one 4x reduction. For this 4x4 block
#   [[0,0,0,0], [1,1,1,1], [0,0,1,1], [1,1,2,2]]
# the first 2x pass gives [[0,0],[1,1]]->0.5->0, [[0,0],[1,1]]->0,
# [[0,0],[1,1]]->0 and [[1,1],[2,2]]->1.5->2, i.e. [[0,0],[0,2]]; the second
# pass gives (0+0+0+2)/4 = 0.5 -> 0. One 4x4 mean is 12/16 = 0.75 -> 1.
INPUT_TWICE = np.array(
    [[0, 0, 0, 0], [1, 1, 1, 1], [0, 0, 1, 1], [1, 1, 2, 2]], dtype=np.uint16
)
GOLDEN_TWICE_2X = np.array([[0]], dtype=np.uint16)
GOLDEN_ONCE_4X = 1

CASES_2D = {
    "uint16": (INPUT_U16, GOLDEN_U16),
    "uint8": (INPUT_U8, GOLDEN_U8),
    "float32": (INPUT_F32, GOLDEN_F32),
    "uint16-9x9": (INPUT_U16_9X9, GOLDEN_U16),
}
INTEGER_CASES = ["uint16", "uint8", "uint16-9x9"]


def _assert_golden(actual, golden):
    actual = np.asarray(actual)
    assert actual.dtype == golden.dtype
    assert actual.shape == golden.shape
    np.testing.assert_array_equal(actual, golden)


# ---------------------------------------------------------------------------
# (a) nd2wsi.metal.reduce2x_or_cpu (and the CPU kernel it shares with the
# converter and the export path), plus the Metal kernel when it is on.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reducer", [metal.reduce2x_or_cpu, metal.reduce2x_cpu])
@pytest.mark.parametrize("case", list(CASES_2D))
def test_reduce2x_or_cpu_matches_golden_on_cyx(reducer, case):
    source, golden = CASES_2D[case]
    before = source.copy()
    _assert_golden(reducer(source[np.newaxis]), golden[np.newaxis])
    np.testing.assert_array_equal(source, before)


@pytest.mark.parametrize("reducer", [metal.reduce2x_or_cpu, metal.reduce2x_cpu])
@pytest.mark.parametrize("case", list(CASES_2D))
def test_reduce2x_or_cpu_matches_golden_on_yx(reducer, case):
    source, golden = CASES_2D[case]
    _assert_golden(reducer(source), golden)
    _assert_golden(reducer(source, source.dtype), golden)


@pytest.mark.parametrize("reducer", [metal.reduce2x_or_cpu, metal.reduce2x_cpu])
def test_output_dtype_decides_whether_the_mean_is_rounded(reducer):
    # The same uint16 table reduced into float32 keeps every fraction: the
    # ties and quarters of block rows 0-1 are 0.5, 1.5, 2.5, 3.5, 0.25, 0.75.
    unrounded = np.array(
        [[0.0, 0.5, 1.5, 2.5], [3.5, 0.25, 0.75, 65535.0]], dtype=np.float32
    )
    _assert_golden(reducer(INPUT_U16, np.float32)[:2], unrounded)
    # And the float32 table reduced into an integer dtype is rounded
    # ties-to-even: 0.5 -> 0, 1.5 -> 2, 1000.25 -> 1000, 2.25 -> 2.
    _assert_golden(reducer(INPUT_F32, np.uint16),
                   np.array([[0, 2], [1000, 2]], dtype=np.uint16))
    _assert_golden(reducer(INPUT_F32[np.newaxis], np.dtype("uint16")),
                   np.array([[[0, 2], [1000, 2]]], dtype=np.uint16))


def test_reduce2x_or_cpu_keeps_a_gpu_row_for_integer_output_only(monkeypatch):
    """GPU first for integer output; a floating output never sees the kernel."""
    calls = []

    def fake_gpu(block):
        calls.append(block.shape)
        return GOLDEN_U16.copy()

    monkeypatch.setattr(metal, "reduce2x", fake_gpu)
    _assert_golden(metal.reduce2x_or_cpu(INPUT_U16), GOLDEN_U16)
    assert calls == [(8, 8)]
    result = metal.reduce2x_or_cpu(INPUT_U16, np.float32)
    assert calls == [(8, 8)]
    assert result.dtype == np.float32
    assert result[0, 1] == 0.5


@pytest.mark.parametrize("case", INTEGER_CASES)
def test_metal_kernel_matches_golden_when_enabled(case):
    if not metal.enabled():
        pytest.skip("ND2WSI_GPU_PYRAMID=1 on Apple silicon is required for the Metal kernel")
    source, golden = CASES_2D[case]
    for block, expected in ((source, golden), (source[np.newaxis], golden[np.newaxis])):
        result = metal.reduce2x(block)
        if result is None:
            # Enabled but no usable library/device: the fallback is legitimate
            # only when the module itself reports the GPU as unavailable, and
            # a run without the kernel must not read as a kernel check.
            assert not metal.available()
            pytest.skip("Metal kernel unavailable (no built library or GPU); CPU paths covered elsewhere")
        _assert_golden(result, expected)


def test_reduce2x_or_cpu_applies_each_2x_pass_separately():
    first = metal.reduce2x_or_cpu(INPUT_TWICE[np.newaxis])
    _assert_golden(first, np.array([[[0, 0], [0, 2]]], dtype=np.uint16))
    second = metal.reduce2x_or_cpu(first)
    _assert_golden(second, GOLDEN_TWICE_2X[np.newaxis])
    assert int(second[0, 0, 0]) != GOLDEN_ONCE_4X
    # The literal 1 above is the oracle; numpy only cross-checks the comment.
    assert np.rint(INPUT_TWICE.mean(dtype=np.float32)) == GOLDEN_ONCE_4X


# ---------------------------------------------------------------------------
# (b) the real converter: a tiny level 0 in a temporary Zarr v2 store, built
# exactly the way tests/test_region_export_service.py::persisted_correction
# drives nd2wsi.convert._downsample_into and build_group_attrs.
# ---------------------------------------------------------------------------
def _convert_levels(tmp_path, raw, *, chunk, levels=1):
    import zarr

    from nd2wsi.convert import _downsample_into, build_group_attrs
    from nd2wsi.reader import ChannelInfo, PlaneSource

    store = zarr.open_group(str(tmp_path / "original.zarr"), mode="w", zarr_format=2)
    a = store.create_array("0", shape=raw.shape, chunks=(1, chunk, chunk), dtype=raw.dtype)
    a[:] = raw
    written = {}
    shapes = [raw.shape[1:]]
    try:
        for level in range(1, levels + 1):
            ph, pw = shapes[-1]
            shape = (max(1, ph // 2), max(1, pw // 2))
            target = store.create_array(str(level), shape=(raw.shape[0], *shape),
                                        chunks=(1, chunk, chunk), dtype=raw.dtype)
            _downsample_into(store[str(level - 1)], target, chunk, raw.dtype)
            written[str(level)] = np.asarray(target[:])
            shapes.append(shape)
        src = PlaneSource(data=None, dtype=raw.dtype, shape=raw.shape, rgb=False,
                          channels=[ChannelInfo("FITC", (255, 255, 255))] * raw.shape[0],
                          pixel_size_um=(0.5, 0.5), source_name="example.nd2",
                          selection={"t": 0, "p": 0, "z": 0})
        attrs = build_group_attrs(src, shapes, chunk,
                                  [dict(start=0, end=255, min=0, max=255)] * raw.shape[0])
    finally:
        store.store.close()
    declared = [(entry["height"], entry["width"]) for entry in attrs["nd2wsi"]["levels"]]
    assert declared == shapes
    assert attrs["multiscales"][0]["type"] == "2x2 local mean"
    return written


@pytest.fixture(params=["gpu-disabled", "gpu-default"])
def gpu_mode(request, monkeypatch):
    if request.param == "gpu-disabled":
        monkeypatch.setattr(metal, "reduce2x", lambda block: None)
    return request.param


@pytest.mark.parametrize("chunk", [64, 4])
@pytest.mark.parametrize("case", list(CASES_2D))
def test_converter_level_matches_golden(tmp_path, gpu_mode, case, chunk):
    source, golden = CASES_2D[case]
    levels = _convert_levels(tmp_path, source[np.newaxis], chunk=chunk)
    _assert_golden(levels["1"], golden[np.newaxis])


def test_converter_repeats_a_collapsed_row(tmp_path, gpu_mode):
    levels = _convert_levels(tmp_path, INPUT_ONE_ROW[np.newaxis], chunk=64)
    _assert_golden(levels["1"], GOLDEN_ONE_ROW[np.newaxis])


def test_converter_applies_each_2x_pass_separately(tmp_path, gpu_mode):
    levels = _convert_levels(tmp_path, INPUT_TWICE[np.newaxis], chunk=64, levels=2)
    _assert_golden(levels["1"], np.array([[[0, 0], [0, 2]]], dtype=np.uint16))
    _assert_golden(levels["2"], GOLDEN_TWICE_2X[np.newaxis])
    assert int(levels["2"][0, 0, 0]) != GOLDEN_ONCE_4X


# ---------------------------------------------------------------------------
# (c) nd2wsi.region_export._OriginalReducedLevel over a bare level-0 object.
# ---------------------------------------------------------------------------
class _Level0:
    """Only what _OriginalReducedLevel reads: shape, dtype and CYX getitem."""

    def __init__(self, values):
        self._values = values
        self.shape = values.shape
        self.dtype = values.dtype

    def __getitem__(self, index):
        return self._values[index]


@pytest.mark.parametrize("case", list(CASES_2D))
def test_original_reduced_level_matches_golden(case):
    source, golden = CASES_2D[case]
    reduced = _OriginalReducedLevel(_Level0(source[np.newaxis]))
    assert reduced.shape == (1, *golden.shape)
    assert reduced.dtype == golden.dtype
    _assert_golden(reduced[:, :, :], golden[np.newaxis])
    _assert_golden(reduced[0], golden)
    # A partial window must land on the same block grid, not re-origin it.
    _assert_golden(reduced[0, 1:, 2:], golden[1:, 2:])


def test_original_reduced_level_repeats_a_collapsed_row():
    reduced = _OriginalReducedLevel(_Level0(INPUT_ONE_ROW[np.newaxis]))
    assert reduced.shape == (1, 1, 2)
    _assert_golden(reduced[0], GOLDEN_ONE_ROW)


def test_original_reduced_level_applies_each_2x_pass_separately():
    first = _OriginalReducedLevel(_Level0(INPUT_TWICE[np.newaxis]))
    second = _OriginalReducedLevel(first)
    _assert_golden(first[0], np.array([[0, 0], [0, 2]], dtype=np.uint16))
    _assert_golden(second[0], GOLDEN_TWICE_2X)
    assert int(second[0][0, 0]) != GOLDEN_ONCE_4X
