"""Outlier-resistant display estimates preserve pixels and full-range inspection."""
import numpy as np
import pytest

from nd2wsi.contrast import auto_window
from nd2wsi.render import compute_histograms


def hist(data, **meta):
    return compute_histograms({"0": data}, {
        "nd2wsi": {"levels": [{"path": "0", "width": data.shape[2], "height": data.shape[1]}], **meta},
        "omero": {"channels": [{"window": {"start": 0, "end": 65535}}] * len(data)},
    }, min_pixels=1)


def test_rare_hot_tail_does_not_stretch_auto_or_follow_stale_cached_window():
    data = np.tile(np.arange(100, 201, dtype=np.uint16), 1000)
    data[:300] = 65535  # 0.3% bright outliers, enough to defeat the old 99.9% limit
    data = data.reshape(1, 100, 1010)
    before = data.copy()
    result = hist(data)[0]
    assert 99 <= result["autoWindow"]["lo"] <= 103
    assert 199 <= result["autoWindow"]["hi"] <= 201
    assert result["autoHistogram"]["vmax"] < 300
    assert result["vmax"] == 65535
    assert result["detail"]["counts"][-1] == 300
    assert sum(result["bins"]) == data.size
    assert sum(result["autoHistogram"]["bins"]) == data.size - 300
    np.testing.assert_array_equal(data, before)


def test_zero_padding_does_not_hide_sparse_varying_signal():
    signal = np.arange(100, 200, dtype=np.uint16)
    data = np.concatenate([np.zeros(100_000, dtype=np.uint16), signal])
    low, high = auto_window(data)
    assert 99 <= low <= 102 and 198 <= high <= 200


@pytest.mark.parametrize("value", [1, 500, 65535])
def test_sparse_constant_signal_remains_visible(value):
    data = np.zeros(100_000, dtype=np.uint16)
    data[0] = value
    assert auto_window(data) == (0, value)


def test_saturated_bright_background_is_not_discarded_as_an_artifact():
    data = np.concatenate([np.arange(4000, 6500, dtype=np.uint16),
                           np.full(7500, 65535, dtype=np.uint16)])
    low, high = auto_window(data)
    assert 4000 <= low < 4100 and high == 65535


@pytest.mark.parametrize("value", [0, 100, 65535])
def test_constant_uint16_estimate_stays_inside_sensor_range(value):
    low, high = auto_window(np.full(100, value, dtype=np.uint16))
    assert 0 <= low < high <= 65535


def test_float_nan_and_subunit_windows_are_finite_and_not_widened_to_one():
    signal = np.linspace(-2e-9, -1e-9, 1000)
    low, high = auto_window(np.concatenate([signal, [np.nan, np.inf, -np.inf]]))
    assert -2e-9 <= low < high <= -1e-9
    assert high - low > 0.9e-9
    assert auto_window(np.array([np.nan, np.inf])) == (0, 1)


def test_rgb_automatic_contrast_uses_one_gain_for_all_components():
    data = np.stack([np.full((100, 100), value, np.uint8) for value in (20, 40, 60)])
    rows = hist(data, rgb=True)
    assert [row["autoWindow"] for row in rows] == [{"lo": 0, "hi": 60}] * 3
    assert all(row["vmax"] == 255 for row in rows)


def test_plate_percentile_black_point_preserves_phase_contrast_distribution():
    data = np.concatenate([np.arange(100, 200), np.full(10000, 160)]).astype(np.uint16)
    low, high = auto_window(data, background_mode=False)
    assert 100 <= low < 115 and high >= 160
