"""Display-window tests that need no slide file."""

from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi.convert import _background_mode_start, _percentile_windows
from nd2wsi.render import compute_histograms


def test_background_mode_raises_fluorescence_black_point_above_tail():
    rng = np.random.default_rng(41)
    background = rng.normal(1200, 120, 90_000)
    signal = rng.uniform(3_000, 30_000, 10_000)
    channel = np.clip(np.concatenate([background, signal]), 0, None)
    fallback = float(np.percentile(channel, 1.0))
    end = float(np.percentile(channel, 99.8))

    start = _background_mode_start(channel, end, fallback)

    assert start > fallback + 100
    assert start == pytest.approx(1200, abs=150)


def test_background_mode_keeps_percentile_for_bright_background():
    rng = np.random.default_rng(42)
    tissue = rng.uniform(2_000, 48_000, 10_000)
    bright_background = rng.normal(62_000, 350, 90_000)
    channel = np.clip(np.concatenate([tissue, bright_background]), 0, 65_535)
    fallback = float(np.percentile(channel, 1.0))
    end = float(np.percentile(channel, 99.8))

    assert _background_mode_start(channel, end, fallback) == fallback


def test_background_mode_respects_nonzero_histogram_origin():
    channel = np.concatenate(
        [np.full(8_000, 10_000, dtype=np.uint16), np.arange(12_000, 20_000, dtype=np.uint16)]
    )
    fallback = float(np.percentile(channel, 1.0))
    end = float(np.percentile(channel, 99.8))

    start = _background_mode_start(channel, end, fallback)

    assert 9_900 <= start <= 10_100


class _Array:
    def __init__(self, data):
        self.data = data

    def __getitem__(self, key):
        return self.data[key]


def test_uint16_histogram_covers_full_dtype_despite_narrow_signal():
    data = np.arange(10_000, 20_000, dtype=np.uint16).reshape(1, 100, 100)
    root = {"0": _Array(data)}
    attrs = {
        "nd2wsi": {"levels": [{"path": "0", "width": 100, "height": 100}]},
        "omero": {"channels": [{"window": {"start": 10_200, "end": 19_000}}]},
    }

    histogram = compute_histograms(root, attrs, min_pixels=1)[0]

    assert histogram["vmin"] == 0
    assert histogram["vmax"] == 65_535
    assert sum(histogram["bins"]) == data.size


def test_float_windows_ignore_nan_and_infinity():
    data = np.array(
        [[[np.nan, -np.inf, 10.0, 10.0], [11.0, 15.0, 20.0, np.inf]]],
        dtype=np.float32,
    )
    root = {"0": _Array(data)}
    source = SimpleNamespace(dtype=data.dtype, rgb=False)

    window = _percentile_windows(root, ["0"], source)[0]

    assert all(np.isfinite(value) for value in window.values())
    assert window["min"] == 10.0
    assert window["max"] == 20.0
    assert window["end"] > window["start"]


@pytest.mark.parametrize("dtype, expected", [
    (np.uint8, (0, 255)), (np.uint16, (0, 65535)),
    (np.int16, (-32768, 32767)), (np.bool_, (0, 1)),
])
def test_histogram_axis_is_independent_of_signal_and_display_window(dtype, expected):
    data = np.full((1, 10, 10), 1, dtype=dtype)
    attrs = {"nd2wsi": {"levels": [{"path": "0", "width": 10, "height": 10}]},
             "omero": {"channels": [{"window": {"start": 0, "end": 2}}]}}
    histogram = compute_histograms({"0": _Array(data)}, attrs, min_pixels=1)[0]
    assert (histogram["vmin"], histogram["vmax"]) == expected
    assert sum(histogram["bins"]) == 100


def test_full_histogram_keeps_sparse_bright_values_in_their_actual_bins():
    data = np.full((1, 100, 100), 100, dtype=np.uint16)
    data[0, 0, :3] = [10000, 50000, 65535]
    attrs = {"nd2wsi": {"levels": [{"path": "0", "width": 100, "height": 100}]},
             "omero": {"channels": [{"window": {"start": 100, "end": 200}}]}}
    histogram = compute_histograms({"0": _Array(data)}, attrs, min_pixels=1)[0]
    assert sum(histogram["bins"]) == data.size
    assert histogram["detail"] == {"values": [100, 10000, 50000, 65535],
                                   "counts": [9997, 1, 1, 1]}
    assert histogram["bins"][39] == 1
    assert histogram["bins"][195] == 1
    assert histogram["bins"][-1] == 1
    assert histogram["autoHistogram"]["vmax"] < 10000
    assert sum(histogram["autoHistogram"]["bins"]) == data.size


def test_float_histogram_includes_extremes_and_stored_range_without_percentile_crop():
    data = np.array([[[np.nan, -np.inf, 5, 10, 11, 99999, np.inf]]], dtype=np.float32)
    attrs = {"nd2wsi": {"levels": [{"path": "0", "width": 7, "height": 1}]},
             "omero": {"channels": [{"window": {"min": -20, "max": 100000,
                                                   "start": 5, "end": 11}}]}}
    histogram = compute_histograms({"0": _Array(data)}, attrs, min_pixels=1)[0]
    assert (histogram["vmin"], histogram["vmax"]) == (-20, 100000)
    assert sum(histogram["bins"]) == 4
    assert histogram["bins"][-1] == 1
