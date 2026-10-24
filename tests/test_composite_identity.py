"""render.composite must stay bit-identical to the pre-LUT implementation.

The CPU compositor is the reference the Metal viewport shader is validated
against, so any output change is a regression. ``_reference_composite`` is a
verbatim copy of ``nd2wsi.render.composite`` at v2.5.3 (a8709b6). Every test
compares the current function with it on the same inputs; expected images are
never computed with the new code.
"""

from __future__ import annotations

import threading
import tracemalloc
import warnings
from fractions import Fraction

import numpy as np
import pytest

from nd2wsi import render


def _reference_composite(
    region: np.ndarray,
    channels: list[int],
    windows: list[tuple[float, float]],
    colors: list[tuple[int, int, int]],
    rgb: bool,
    gammas: list[float] | None = None,
) -> np.ndarray:
    """(C, h, w) raw values -> (h, w, 3) uint8 display image.

    ``gammas`` follows the NIS convention: gamma > 1 brightens midtones
    (``v ** (1/gamma)`` after windowing).
    """
    c, h, w = region.shape
    if h == 0 or w == 0:
        return np.zeros((h, w, 3), dtype=np.uint8)
    gammas = gammas or [1.0] * len(windows)
    floating = np.issubdtype(region.dtype, np.floating)
    # Preserve both sub-micro windows and small float64 differences at large
    # offsets. Integer sources keep the existing float32 compositing path.
    working_dtype = np.float64 if floating else np.float32

    if rgb:
        # RGB components keep their physical R/G/B slots, not fluorescence
        # pseudocolours. Each component has its own display window and gamma;
        # hiding one leaves the other two unchanged. Defaults remain identity
        # for an 8-bit colour acquisition.
        out = np.zeros((h, w, 3), dtype=np.uint8)
        for ci in channels:
            lo, hi = windows[ci]
            span = (hi - lo if hi > lo else 1.0) if floating else max(hi - lo, 1e-6)
            v = (region[ci].astype(working_dtype) - lo) / span
            np.clip(v, 0, 1, out=v)
            if gammas[ci] != 1.0:
                v **= 1.0 / gammas[ci]
            out[..., ci] = (v * 255).astype(np.uint8)
        return out

    out = np.zeros((h, w, 3), dtype=np.float32)
    for ci in channels:
        lo, hi = windows[ci]
        span = (hi - lo if hi > lo else 1.0) if floating else max(hi - lo, 1e-6)
        v = (region[ci].astype(working_dtype) - lo) / span
        np.clip(v, 0, 1, out=v)
        if gammas[ci] != 1.0:
            v **= 1.0 / gammas[ci]
        col = np.asarray(colors[ci], dtype=np.float32) / 255.0
        out += v[..., None] * col
    return (np.clip(out, 0, 1) * 255).astype(np.uint8)


def _quiet(fn, *args):
    # NaN/inf/overflow inputs make both implementations warn identically.
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        return fn(*args)


def assert_same(region, channels, windows, colors, rgb, gammas=None):
    expected = _quiet(_reference_composite, region, channels, windows, colors, rgb, gammas)
    got = _quiet(render.composite, region, channels, windows, colors, rgb, gammas)
    assert got.dtype == expected.dtype == np.uint8
    assert got.shape == expected.shape
    np.testing.assert_array_equal(got, expected)
    return got


GAMMAS = [0.1, 0.25, 0.5, 1.0, 1.6, 2.2, 4.0, 10.0]
PRIMARIES = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255)]
NO_ZEROS = [(255, 255, 255), (255, 200, 100), (90, 255, 30), (128, 64, 255)]
ODD_COLOURS = [(0, 0, 0), (17, 200, 3), (255, 128, 0), (1, 1, 1)]
ALL_CHANNELS = [0, 1, 2, 3]


@pytest.fixture(autouse=True)
def _fresh_lut_cache():
    render._clear_gamma_lut_cache()
    yield
    render._clear_gamma_lut_cache()


def _all_uint16_codes() -> np.ndarray:
    codes = np.arange(65536, dtype=np.uint16)
    rng = np.random.default_rng(16)
    planes = [codes, codes[::-1], rng.permutation(codes), np.roll(codes, 12345)]
    return np.stack([p.reshape(256, 256) for p in planes])


def _all_uint8_codes() -> np.ndarray:
    codes = np.arange(256, dtype=np.uint8)
    rng = np.random.default_rng(8)
    planes = [codes, codes[::-1], rng.permutation(codes), np.roll(codes, 77)]
    return np.stack([p.reshape(16, 16) for p in planes])


UINT16_WINDOWS = [
    [(1667.17578125, 4480.0), (7488.70749999999, 11812.41499999998),
     (548.66568359375, 4905.80499999999), (300.0, 9000.0)],
    [(0.0, 65535.0)] * 4,
    [(1000.0, 1000.0)] * 4,
    [(4000.0, 1000.0)] * 4,
    [(-500.5, 70000.25), (0.25, 0.75), (65535.0, 1e9), (-1e30, 1e30)],
    [(100, 200), (0, 65535), (-3, 3), (12345, 12345)],
]


@pytest.mark.parametrize("gamma", GAMMAS + ["mixed"])
@pytest.mark.parametrize("windows", UINT16_WINDOWS, ids=range(len(UINT16_WINDOWS)))
def test_every_uint16_code_matches_reference(gamma, windows):
    region = _all_uint16_codes()
    gammas = [1.0, 1.6, 0.25, 10.0] if gamma == "mixed" else [gamma] * 4
    for colours in (PRIMARIES, NO_ZEROS, ODD_COLOURS):
        assert_same(region, ALL_CHANNELS, windows, colours, False, gammas)
    assert_same(region[:3], [0, 1, 2], windows, PRIMARIES, True, gammas)


@pytest.mark.parametrize("gamma", GAMMAS + ["mixed"])
def test_every_uint8_code_matches_reference(gamma):
    region = _all_uint8_codes()
    gammas = [2.2, 1.0, 0.5, 4.0] if gamma == "mixed" else [gamma] * 4
    for windows in ([(0.0, 255.0)] * 4, [(10.5, 200.25), (50, 60), (-20.0, 300.0),
                                          (128.0, 128.0)], [(200.0, 100.0)] * 4):
        for colours in (PRIMARIES, NO_ZEROS, ODD_COLOURS):
            assert_same(region, ALL_CHANNELS, windows, colours, False, gammas)
        assert_same(region[:3], [0, 1, 2], windows, PRIMARIES, True, gammas)


def _region(dtype, shape, seed=0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    dtype = np.dtype(dtype)
    if dtype.kind == "f":
        values = rng.normal(2000.0, 1500.0, shape) * rng.choice([1e-3, 1.0, 40.0], shape)
        return values.astype(dtype)
    info = np.iinfo(dtype)
    return rng.integers(info.min, int(info.max) + 1, shape, dtype=dtype)


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.uint32, np.int16, np.float32,
                                   np.float64])
def test_dtypes_match_reference(dtype):
    region = _region(dtype, (4, 150, 131), seed=3)
    lo = float(np.percentile(region, 2)) if np.dtype(dtype).kind == "f" else 0.0
    hi = float(np.percentile(region, 98)) if np.dtype(dtype).kind == "f" else 3000.0
    for gammas in ([1.0] * 4, [1.6] * 4, [0.5, 1.0, 2.2, 10.0]):
        for colours in (PRIMARIES, NO_ZEROS, ODD_COLOURS):
            assert_same(region, ALL_CHANNELS, [(lo, hi)] * 4, colours, False, gammas)
        assert_same(region[:3], [0, 1, 2], [(lo, hi)] * 3, PRIMARIES, True, gammas[:3])


WINDOW_EDGES = [
    (500.0, 500.0),          # lo == hi
    (3000.0, 100.0),         # lo > hi
    (-250.0, 900.0),         # negative lo
    (0.0, 1e6),              # hi beyond every integer dtype's useful range
    (0.5, 65535.5),          # fractional bounds
    (-1e30, 1e30),           # huge span
    (-3.5e38, 3.5e38),       # float32 overflow in the working math
    (1.5e-7, 2.5e-7),        # sub-micro window
    (float("nan"), 100.0),
    (0.0, float("inf")),
    (float("-inf"), 100.0),
    (float("-inf"), float("inf")),
    (10**39, 10**39 + 5),     # Python ints beyond float32
]


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.uint32, np.int16, np.float32,
                                   np.float64])
@pytest.mark.parametrize("window", WINDOW_EDGES, ids=[repr(w) for w in WINDOW_EDGES])
def test_window_edge_cases_match_reference(dtype, window):
    region = _region(dtype, (4, 130, 127), seed=5)
    windows = [window, (0.0, 1000.0), window, (100.0, 50.0)]
    for gammas in ([1.0] * 4, [1.6] * 4, [0.25, 1.0, 4.0, 1.0]):
        for colours in (PRIMARIES, NO_ZEROS, ODD_COLOURS):
            assert_same(region, ALL_CHANNELS, windows, colours, False, gammas)
        assert_same(region[:3], [0, 1, 2], windows[:3], PRIMARIES, True, gammas[:3])


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32])
def test_degenerate_gammas_match_reference(dtype):
    # Not reachable through parse_windows (it clamps to [0.1, 10]), but a
    # negative or NaN gamma turns zeros into inf/NaN, which must still reach
    # every colour component the reference added them to.
    region = _region(dtype, (4, 130, 127), seed=6)
    region[:, :4, :4] = 0
    windows = [(0.0, 1000.0), (200.0, 3000.0), (5.0, 6.0), (0.0, 1.0)]
    for gamma in (-1.0, -0.5, float("nan"), float("inf"), 1e-300, 7, -2):
        gammas = [gamma, 1.0, gamma, 2.2]
        for colours in (PRIMARIES, NO_ZEROS, ODD_COLOURS):
            assert_same(region, ALL_CHANNELS, windows, colours, False, gammas)


COLOUR_SETS = [
    PRIMARIES,
    NO_ZEROS,
    ODD_COLOURS,
    [(0, 0, 0)] * 4,
    [(255, 255, 255)] * 4,
    [(255, 0, 0), (0, 0, 0), (0, 0, 255), (0, 255, 0)],
    [(300, -20, 0), (0.5, 0, 254.75), (1e-30, 0, -0.0), (65535, 1, 0)],
    [128, (200,), (0,), 64],  # scalar and length-1 colours broadcast to grey
    [(10, 20, 30), 0, (0, 0, 0), (40,)],
]


@pytest.mark.parametrize("colours", COLOUR_SETS, ids=range(len(COLOUR_SETS)))
@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32, np.float64])
def test_colours_match_reference(colours, dtype):
    region = _region(dtype, (4, 130, 128), seed=7)
    windows = [(0.0, 2500.0), (100.0, 900.0), (-5.0, 60.0), (1000.0, 4000.0)]
    for gammas in ([1.0] * 4, [1.6] * 4, [1.0, 2.2, 1.0, 0.5]):
        assert_same(region, ALL_CHANNELS, windows, colours, False, gammas)
        assert_same(region, [3, 1], windows, colours, False, gammas)


CHANNEL_LISTS = [[], [0, 1, 2, 3], [3, 2, 1, 0], [2], [1, 3], [0, 0], [2, 1, 2, 1, 2], [-1, 0]]


@pytest.mark.parametrize("channels", CHANNEL_LISTS, ids=[str(c) for c in CHANNEL_LISTS])
@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32])
def test_channel_lists_match_reference(channels, dtype):
    region = _region(dtype, (4, 140, 133), seed=9)
    windows = [(0.0, 2500.0), (100.0, 900.0), (20.0, 60.0), (1000.0, 4000.0)]
    for gammas in ([1.0] * 4, [1.6] * 4, [0.5, 1.0, 2.2, 1.6]):
        for colours in (PRIMARIES, NO_ZEROS, ODD_COLOURS):
            assert_same(region, channels, windows, colours, False, gammas)
    rgb_channels = [c for c in channels if c in (0, 1, 2)]
    assert_same(region[:3], rgb_channels, windows[:3], PRIMARIES, True, [1.6, 1.0, 0.5])


def test_empty_channels_render_black():
    region = _region(np.uint16, (4, 9, 11))
    got = assert_same(region, [], [(0.0, 1.0)] * 4, PRIMARIES, False, None)
    assert got.shape == (9, 11, 3) and not got.any()


@pytest.mark.parametrize("shape", [(1, 1), (7, 13), (13, 7), (0, 5), (5, 0), (0, 0), (1, 300)])
@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32])
def test_shapes_match_reference(shape, dtype):
    region = _region(dtype, (4, *shape), seed=11)
    windows = [(0.0, 3000.0), (5.0, 200.0), (-10.0, 40.0), (1.0, 1.0)]
    for gammas in ([1.0] * 4, [1.6] * 4, [0.5, 1.0, 10.0, 1.6]):
        assert_same(region, ALL_CHANNELS, windows, PRIMARIES, False, gammas)
        assert_same(region[:3], [0, 1, 2], windows[:3], PRIMARIES, True, gammas[:3])


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.int16, np.float32])
def test_non_contiguous_regions_match_reference(dtype):
    base = _region(dtype, (5, 300, 420), seed=13)
    views = [
        base[:4, ::2, 1::3],
        base[1:, ::-1, :],
        base[::-1][:4, 7:200, 3:150],
        np.asfortranarray(base[:4, :140, :150]),
        base[:4].transpose(0, 2, 1),
        base[:4, :150, :150].astype(np.dtype(dtype).newbyteorder()),
    ]
    windows = [(0.0, 3000.0), (5.0, 200.0), (-10.0, 40.0), (100.0, 9000.0)]
    for view in views:
        for gammas in ([1.0] * 4, [1.6] * 4, [0.5, 1.0, 10.0, 1.6]):
            assert_same(view, ALL_CHANNELS, windows, PRIMARIES, False, gammas)
            assert_same(view, [2, 0, 2], windows, NO_ZEROS, False, gammas)
            assert_same(view[:3], [0, 1, 2], windows[:3], PRIMARIES, True, gammas[:3])


SCALAR_TYPES = [np.float64, np.float32, np.float16, int, float, np.int64, np.uint16]


@pytest.mark.parametrize("window_type", SCALAR_TYPES, ids=lambda t: t.__name__)
@pytest.mark.parametrize("gamma_type", SCALAR_TYPES, ids=lambda t: t.__name__)
@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32])
def test_scalar_types_match_reference(window_type, gamma_type, dtype):
    # NumPy scalars are not "weak" under NEP 50: np.float64 windows move the
    # integer path to float64 math, which a float32 table would not reproduce.
    region = _region(dtype, (4, 130, 127), seed=17)
    with np.errstate(over="ignore"):  # np.float16(65535) is inf: a valid edge case
        windows = [(window_type(100), window_type(2900)), (window_type(7), window_type(250)),
                   (window_type(0), window_type(65535)), (window_type(40), window_type(40))]
    gammas = [gamma_type(2), gamma_type(1), gamma_type(4), gamma_type(1)]
    assert_same(region, ALL_CHANNELS, windows, PRIMARIES, False, gammas)
    assert_same(region, ALL_CHANNELS, windows, NO_ZEROS, False, gammas)
    assert_same(region[:3], [0, 1, 2], windows[:3], PRIMARIES, True, gammas[:3])
    fractional = [(window_type(100.7), window_type(2900.3))] * 4
    for g in (1.6, 2.2, 0.25):
        gammas = [gamma_type(g)] * 4
        if gamma_type in (int, np.int64, np.uint16):
            continue
        assert_same(region, ALL_CHANNELS, fractional, PRIMARIES, False, gammas)
        assert_same(region, ALL_CHANNELS, fractional, ODD_COLOURS, False, gammas)


def test_numpy_scalar_windows_on_full_uint16_range_match_reference():
    region = _all_uint16_codes()
    for scalar in (np.float64, np.float32):
        windows = [(scalar(1667.17578125), scalar(4480.0)),
                   (scalar(7488.70749999999), scalar(11812.41499999998)),
                   (scalar(548.66568359375), scalar(4905.80499999999)), (300.0, 9000.0)]
        for g in GAMMAS:
            for gammas in ([g] * 4, [scalar(g)] * 4):
                assert_same(region, ALL_CHANNELS, windows, PRIMARIES, False, gammas)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_non_finite_float_pixels_match_reference(dtype):
    region = _region(dtype, (4, 130, 131), seed=19)
    rng = np.random.default_rng(23)
    specials = np.array([np.nan, np.inf, -np.inf], dtype=dtype)
    mask = rng.random(region.shape) < 0.05
    region[mask] = rng.choice(specials, int(mask.sum()))
    only_nan, only_pinf, only_ninf = region.copy(), region.copy(), region.copy()
    only_nan[1, 10:20, 10:20] = np.nan
    only_pinf[:, :30, :30] = np.inf
    only_ninf[2] = -np.inf
    clean = _region(dtype, (4, 130, 131), seed=19)
    single_nan = clean.copy()
    single_nan[3, 64, 64] = np.nan
    windows = [(0.0, 2500.0), (100.0, 900.0), (-5.0, 60.0), (1000.0, 1000.0)]
    for case in (region, only_nan, only_pinf, only_ninf, single_nan, clean):
        for gammas in ([1.0] * 4, [1.6] * 4, [0.5, 1.0, 2.2, 1.6]):
            for colours in (PRIMARIES, NO_ZEROS, ODD_COLOURS, [(0, 0, 0)] * 4):
                assert_same(case, ALL_CHANNELS, windows, colours, False, gammas)
                assert_same(case, [1, 1, 3], windows, colours, False, gammas)
            assert_same(case[:3], [0, 1, 2], windows[:3], PRIMARIES, True, gammas[:3])


def test_default_and_empty_gammas_match_reference():
    region = _region(np.uint16, (4, 130, 127), seed=29)
    windows = [(0.0, 3000.0), (5.0, 200.0), (-10.0, 40.0), (100.0, 9000.0)]
    for gammas in (None, []):
        assert_same(region, ALL_CHANNELS, windows, PRIMARIES, False, gammas)
        assert_same(region[:3], [0, 1, 2], windows[:3], PRIMARIES, True, gammas)


def test_unusual_colour_shapes_and_working_dtypes_match_reference():
    # Colours that broadcast against (h, w, 3) other than as an RGB triple, and
    # windows whose arithmetic leaves float32/float64, take the dense path.
    region = _region(np.uint16, (3, 40, 50), seed=59)
    rng = np.random.default_rng(61)
    per_column = rng.integers(0, 256, (50, 3))
    per_pixel = rng.integers(0, 256, (40, 50, 3))
    windows = [(0.0, 3000.0), (5.0, 900.0), (100.0, 60000.0)]
    for colours in ([per_column, (255, 0, 0), (0, 0, 255)], [(0, 255, 0), per_pixel, 7],
                    [(0, 0, 255), (255, 0, 0), per_column]):
        for gammas in ([1.0] * 3, [1.6, 2.2, 1.0]):
            assert_same(region, [0, 1, 2], windows, colours, False, gammas)
    longdouble = [(np.longdouble(10.5), np.longdouble(2000.0))] * 3
    assert_same(region, [0, 1, 2], longdouble, PRIMARIES, False, [1.6, 1.0, 2.2])
    for fn in (_reference_composite, render.composite):
        with pytest.raises(TypeError):
            fn(region, [1, 0], [(Fraction(1, 3), 2000)] * 3, PRIMARIES, False, [1.0] * 3)


def test_errors_match_reference():
    region = _region(np.uint16, (4, 130, 127), seed=31)
    windows = [(10**400, 10**400 + 1)] * 4
    for fn in (_reference_composite, render.composite):
        with pytest.raises(OverflowError):
            fn(region, [0], windows, PRIMARIES, False, [1.6] * 4)
        with pytest.raises(OverflowError):
            fn(region, [0], windows, PRIMARIES, False, [1.0] * 4)
        with pytest.raises(IndexError):
            fn(region, [4], [(0.0, 1.0)] * 5, PRIMARIES * 2, False, [1.6] * 5)
        with pytest.raises(ValueError):
            fn(region, [0], [(0.0, 1.0)] * 4, [(1, 2)] * 4, False, [1.6] * 4)


class _TaggedArray(np.ndarray):
    """A trivial ndarray subclass: ufuncs keep the subclass, values unchanged."""


def _masked(region, seed):
    rng = np.random.default_rng(seed)
    return np.ma.masked_array(region, mask=rng.random(region.shape) < 0.3)


def test_masked_array_minimal_case_matches_reference():
    # numpy.ma restores the operand's data under the mask inside every ufunc,
    # so the reference writes the masked pixel's windowed value (1.0 here) into
    # all three components, including the zero ones.
    region = np.ma.masked_array(np.array([[[10000, 50000]]], dtype=np.uint16),
                                mask=[[[False, True]]])
    for gamma in (1.0, 2.2):
        got = assert_same(region, [0], [(0.0, 65535.0)], [(0, 255, 0)], False, [gamma])
        assert got[0, 1].tolist() == [255, 255, 255]


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32])
def test_ndarray_subclass_regions_match_reference(dtype, tmp_path):
    # 256 x 256 is large enough to build a gamma table for uint8 and uint16.
    base = _region(dtype, (4, 256, 256), seed=67)
    mm = np.memmap(tmp_path / "region.bin", dtype=base.dtype, mode="w+", shape=base.shape)
    mm[:] = base
    regions = [_masked(base, 71), _masked(base[:, :64, :64], 73), mm, base.view(_TaggedArray)]
    windows = [(0.0, 3000.0), (5.0, 200.0), (-10.0, 40.0), (100.0, 60000.0)]
    for region in regions:
        for gammas in ([1.0] * 4, [2.2] * 4, [0.5, 1.0, 10.0, 1.6]):
            for colours in (PRIMARIES, NO_ZEROS, ODD_COLOURS):
                got = assert_same(region, ALL_CHANNELS, windows, colours, False, gammas)
                assert type(got) is type(_quiet(_reference_composite, region, ALL_CHANNELS,
                                                windows, colours, False, gammas))
            assert_same(region, [2, 0, 2], windows, NO_ZEROS, False, gammas)
            assert_same(region[:3], [0, 1, 2], windows[:3], PRIMARIES, True, gammas[:3])
    del mm


def test_masked_window_bounds_match_reference():
    # A subclass can also arrive through a window bound; single-element bounds
    # are the only ones the span arithmetic accepts.
    region = _region(np.uint16, (2, 64, 64), seed=79)
    for mask in (True, False):
        lo = np.ma.masked_array([[100.0]], mask=[[mask]])
        for gammas in ([1.0, 1.0], [2.2, 1.6]):
            assert_same(region, [0, 1], [(lo, 3000.0), (0.0, 65535.0)],
                        [(0, 255, 0), (255, 0, 0)], False, gammas)


def test_extra_unit_dimensions_raise_like_reference():
    # The dense in-place += refuses to broadcast a windowed plane with extra
    # leading unit dimensions; the planar path must not silently drop them.
    small = np.arange(12, dtype=np.uint16).reshape(1, 3, 4)
    big = _region(np.uint16, (1, 128, 128), seed=83)  # builds a gamma table
    cases = [
        (small, [True], [(0.0, 11.0)] * 2, [(255, 0, 0)] * 2, [1.0, 1.0]),
        (small.astype(np.float32), [True], [(0.0, 11.0)] * 2, [(255, 0, 0)] * 2, [1.0, 1.0]),
        (small, [0], [(np.array([[[0.0]]]), 11.0)], [(255, 0, 0)], [1.0]),
        (small.astype(np.float32), [0], [(np.array([[[0.0]]]), 11.0)], [(255, 0, 0)], [1.6]),
        (big, [True], [(0.0, 9000.0)] * 2, [(255, 0, 0)] * 2, [2.2, 2.2]),
        (big, [0, True], [(0.0, 9000.0)] * 2, [(255, 0, 0)] * 2, [2.2, 2.2]),
    ]
    for region, channels, windows, colours, gammas in cases:
        for fn in (_reference_composite, render.composite):
            with pytest.raises(ValueError):
                _quiet(fn, region, channels, windows, colours, False, gammas)
    # A bound that broadcasts to exactly (h, w) is still accepted by both.
    assert_same(big, [0], [(np.array([[100.0]]), 9000.0)], PRIMARIES, False, [2.2])
    assert_same(big, [0], [(np.array([[100.0]]), 9000.0)], PRIMARIES, False, [1.0])


def _peak_bytes(fn, *args) -> int:
    _quiet(fn, *args)  # warm: gamma tables are built once, not per call
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        _quiet(fn, *args)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("dtype", [np.uint16, np.float32])
@pytest.mark.parametrize("gamma", [1.0, 2.2])
def test_peak_memory_does_not_exceed_reference(dtype, gamma):
    # Export strips span the full ROI width, so transient memory per call
    # matters; the planar path must not hold more than the dense one did.
    region = _region(dtype, (4, 256, 512), seed=89)
    windows = [(0.0, 3000.0), (5.0, 900.0), (-10.0, 400.0), (100.0, 9000.0)]
    for colours in (PRIMARIES, NO_ZEROS):
        args = (region, ALL_CHANNELS, windows, colours, False, [gamma] * 4)
        assert _peak_bytes(render.composite, *args) <= _peak_bytes(_reference_composite, *args)


# ---- randomized sweep -------------------------------------------------------

SWEEP_DTYPES = [np.uint8, np.uint16, np.uint32, np.int16, np.float32, np.float64]
SWEEP_SHAPES = [(1, 1), (3, 5), (17, 9), (64, 64), (128, 128), (129, 131), (160, 103)]
SWEEP_GAMMAS = GAMMAS + [2, 1, np.float64(1.6), np.float32(2.2), np.float64(1.0), 3.0]
SWEEP_COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255), (0, 255, 255),
                 (255, 255, 255), (0, 0, 0), (17, 200, 3), (255, 128, 0), (1, 1, 1),
                 (90, 0, 30), (0, 64, 0)]


def _random_window(rng, region):
    kind = rng.integers(0, 10)
    finite = region[np.isfinite(region)] if region.dtype.kind == "f" else region
    if finite.size:
        a, b = (float(x) for x in np.percentile(finite, sorted(rng.uniform(0, 100, 2))))
    else:
        a, b = 0.0, 1.0
    if kind == 0:
        return (a, a)
    if kind == 1:
        return (b, a - 1.0)
    if kind == 2:
        return (-abs(a) - 100.5, b)
    if kind == 3:
        return (a, 1e7)
    if kind == 4:
        return (int(a), int(b) + 1)
    if kind == 5:
        return (np.float64(a), np.float64(b))
    if kind == 6:
        return (np.float32(a), np.float32(b))
    if kind == 7:
        return (-1e30, 1e30)
    return (a + 0.25, b + 0.75)


def _random_region(rng, dtype, c, shape):
    dtype = np.dtype(dtype)
    h, w = shape
    if rng.random() < 0.3:
        # A non-contiguous view of a larger buffer.
        big = _region(dtype, (c + 1, 2 * h + 1, 3 * w + 2), seed=int(rng.integers(1 << 30)))
        region = big[1:, 1::2, 2::3][:, :h, :w]
    else:
        region = _region(dtype, (c, h, w), seed=int(rng.integers(1 << 30)))
    if dtype.kind == "f" and rng.random() < 0.3:
        region = region.copy()
        mask = rng.random(region.shape) < 0.02
        region[mask] = rng.choice(np.array([np.nan, np.inf, -np.inf], dtype=dtype), int(mask.sum()))
    return region


def test_randomized_sweep_matches_reference():
    rng = np.random.default_rng(20261024)
    for _ in range(300):
        dtype = SWEEP_DTYPES[rng.integers(len(SWEEP_DTYPES))]
        shape = SWEEP_SHAPES[rng.integers(len(SWEEP_SHAPES))]
        rgb = bool(rng.random() < 0.2)
        c = 3 if rgb else int(rng.integers(1, 6))
        region = _random_region(rng, dtype, c, shape)
        windows = [_random_window(rng, region) for _ in range(c)]
        gammas = [SWEEP_GAMMAS[i] for i in rng.integers(len(SWEEP_GAMMAS), size=c)]
        colours = [SWEEP_COLOURS[i] for i in rng.integers(len(SWEEP_COLOURS), size=c)]
        n = int(rng.integers(0, c + 3))
        channels = [int(i) for i in rng.integers(0, c, size=n)]
        assert_same(region, channels, windows, colours, rgb, gammas)


# ---- gamma LUT cache ----------------------------------------------------------


def _cached_tables() -> list[np.ndarray]:
    with render._gamma_lut_lock:
        return [table for table, _ in render._gamma_luts.values()]


def test_lut_cache_stays_bounded():
    region = _region(np.uint16, (2, 128, 128), seed=37)
    for i in range(3 * render._GAMMA_LUT_CAPACITY):
        windows = [(float(i), 3000.0 + i), (0.0, 65535.0)]
        assert_same(region, [0, 1], windows, PRIMARIES, False, [1.6, 2.2])
        assert len(render._gamma_luts) <= render._GAMMA_LUT_CAPACITY
    assert len(render._gamma_luts) == render._GAMMA_LUT_CAPACITY


def test_lut_cache_reuses_read_only_tables():
    region = _region(np.uint16, (4, 256, 256), seed=41)
    windows = [(100.0, 3000.0), (0.0, 65535.0), (100.0, 3000.0), (5.0, 900.0)]
    gammas = [1.6, 2.2, 1.6, 1.0]
    first = assert_same(region, ALL_CHANNELS, windows, PRIMARIES, False, gammas)
    tables = _cached_tables()
    # Two distinct (window, gamma) pairs with gamma != 1; channel 0 and 2 share one.
    assert len(tables) == 2
    for table in tables:
        assert table.flags.writeable is False
        assert table.dtype == np.float32 and table.shape == (65536,)
        with pytest.raises(ValueError):
            table[0] = 1.0
    second = assert_same(region, ALL_CHANNELS, windows, PRIMARIES, False, gammas)
    np.testing.assert_array_equal(first, second)
    again = _cached_tables()
    assert len(again) == 2 and all(a is b for a, b in zip(tables, again, strict=True))
    # AstroMap re-indexes windows; the key is the window, not the channel slot.
    reindexed = [windows[3], windows[2], windows[1], windows[0]]
    assert_same(region[::-1], ALL_CHANNELS, reindexed, PRIMARIES, False, gammas[::-1])
    assert len(_cached_tables()) == 2


def test_lut_cache_keys_on_exact_scalar_values_and_types():
    region = _region(np.uint16, (1, 128, 128), seed=43)
    for window, gamma in (((100, 3000), 2), ((100.0, 3000.0), 2.0), ((100.0, 3000.0), 2),
                          ((-0.0, 3000.0), 2.0), ((0.0, 3000.0), 2.0)):
        assert_same(region, [0], [window], PRIMARIES, False, [gamma])
    assert len(render._gamma_luts) == 5
    for scalar_window in ((np.float64(100.0), 3000.0), (100.0, np.float32(3000.0))):
        assert_same(region, [0], [scalar_window], PRIMARIES, False, [2.0])
    assert_same(region, [0], [(100.0, 3000.0)], PRIMARIES, False, [np.float64(2.0)])
    assert_same(region.astype(np.uint32), [0], [(100.0, 3000.0)], PRIMARIES, False, [2.0])
    assert len(render._gamma_luts) == 5


def test_small_regions_reuse_but_do_not_build_tables():
    big = _region(np.uint16, (1, 128, 128), seed=47)
    small = big[:, :9, :7]
    windows = [(100.0, 3000.0)]
    assert_same(small, [0], windows, PRIMARIES, False, [1.6])
    assert len(render._gamma_luts) == 0
    assert_same(big, [0], windows, PRIMARIES, False, [1.6])
    assert len(render._gamma_luts) == 1
    assert_same(small, [0], windows, PRIMARIES, False, [1.6])
    assert len(render._gamma_luts) == 1


def test_concurrent_composites_match_serial_reference(monkeypatch):
    # A small capacity forces evictions and duplicate builds while threads race.
    monkeypatch.setattr(render, "_GAMMA_LUT_CAPACITY", 3)
    rng = np.random.default_rng(53)
    jobs = []
    for i in range(48):
        dtype = (np.uint16, np.uint8, np.float32)[i % 3]
        region = _region(dtype, (3, 128, 128), seed=1000 + i)
        windows = [(float(i % 5) * 100.0, 2000.0 + (i % 7) * 10.0), (0.0, 255.0),
                   (50.0, 60000.0)]
        gammas = [(1.6, 2.2, 0.5, 1.0)[i % 4], (4.0, 1.6)[i % 2], 1.0]
        colours = [SWEEP_COLOURS[j] for j in rng.integers(len(SWEEP_COLOURS), size=3)]
        jobs.append((region, [0, 1, 2, 0], windows, colours, False, gammas))
    expected = [_quiet(_reference_composite, *job) for job in jobs]
    results: list[list] = [[] for _ in range(8)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(slot: int) -> None:
        try:
            barrier.wait()
            for rep in range(3):
                for index in range(slot + rep, len(jobs) + slot + rep):
                    job = jobs[index % len(jobs)]
                    results[slot].append((index % len(jobs), _quiet(render.composite, *job)))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(slot,)) for slot in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sum(len(r) for r in results) == 8 * 3 * len(jobs)
    for slot_results in results:
        for index, image in slot_results:
            np.testing.assert_array_equal(image, expected[index])
    assert len(render._gamma_luts) <= 3
