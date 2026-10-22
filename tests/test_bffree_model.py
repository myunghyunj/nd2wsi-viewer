"""Frozen-method equivalence and native preview boundaries, using synthetic data."""
from __future__ import annotations

import io
import warnings
import zipfile

import numpy as np
import pytest

from nd2wsi import bffree_model as bm
from nd2wsi import bffree_periodic as bp

# Frozen from the independent x/h1 research implementation, SHA256
# d75563963b39b1231c7ac0aeb42c3c740cea5e428cc4f327f8e53a04240b20e6.
# Each row exactly represents its saved 512-bin gain as
# 1 + offset + cosine*cos(2*pi*f*x) + sine*sin(2*pi*f*x).
_FROZEN_WAVES = (
    (.03891048726609349, .0003030845778671632, .07768220984549232, -.015749377155793966),
    (.032051247534033454, .0008083949541351767, .106159021437156, -.08942651811955718),
    (.03424659652905738, .0019221204716048286, .05410271995397904, -.10628994402501533),
)
_FROZEN_AXES = (
    {"frequency": .03891048726609349, "period_px": 25.700012265623766,
     "peak_ratio": 150332637.34229267,
     "split_frequencies": [.03891012988476654, .03891019018707058],
     "split_peak_ratios": [10266807.541995455, 5246176.5422261115],
     "applied_frequency": .03891048726609349,
     "split_wave_correlation": .9999999995473715, "split_amplitude_ratio": 1.000000077679493},
    {"frequency": .032051247534033454, "period_px": 31.20003360050666,
     "peak_ratio": 13859637.126360139,
     "split_frequencies": [.03205088431672654, .03205108689099812],
     "split_peak_ratios": [641166.430963229, 598165.9229363692],
     "applied_frequency": .032051247534033454,
     "split_wave_correlation": .9999999984424565, "split_amplitude_ratio": .9999997225813099},
    {"frequency": .03424659652905738, "period_px": 29.19998193547569,
     "peak_ratio": 9313707.282717708,
     "split_frequencies": [.034246474279979885, .034246924200890275],
     "split_peak_ratios": [400211.3351878009, 185207.179781061],
     "applied_frequency": .03424659652905738,
     "split_wave_correlation": .9999999994160631, "split_amplitude_ratio": .9999998403882437},
)
_FROZEN_PARAMETERS = {
    "axes": ["x"], "period_min_px": 12.0, "period_max_px": 128.0, "min_repeats": 6.0,
    "harmonics": 1, "peak_ratio": 8.0, "min_modulation": .005, "frequency_tolerance": .10,
    "split_correlation": .65, "gain_min": .5, "gain_max": 2.0, "min_valid_fraction": .70,
    "min_images": 5, "smooth_sigma_px": 12.0, "multiimage_split_rmse": .15,
}


def _frozen_gains():
    x = np.arange(512, dtype=float)
    return np.array([1 + offset + cosine*np.cos(2*np.pi*f*x) + sine*np.sin(2*np.pi*f*x)
                     for f, offset, cosine, sine in _FROZEN_WAVES])


def _native_line_reference(gain, native_width, yxhw):
    """Independent segment-weight interpolation with actual short-bin centres."""
    _, x, height, width = yxhw
    starts = np.arange(0, native_width, 32)
    centres = (starts + np.minimum(starts + 32, native_width) - 1) / 2
    coordinates = np.arange(x, x + width)
    right = np.clip(np.searchsorted(centres, coordinates, side="right"), 1, len(centres) - 1)
    left = right - 1
    weight = np.clip((coordinates - centres[left]) / (centres[right] - centres[left]), 0, 1)
    line = (1 - weight)*gain[left] + weight*gain[right]
    return np.broadcast_to(line.astype(np.float32), (height, width))


def metadata(raw, *, native=None):
    return {"channel_mapping": {"FITC": 3, "DAPI": 2, "CY3": 0},
            "channel_names": ["CY3", "BF", "DAPI", "FITC"],
            "sizes": dict(zip(("Y", "X"), native or tuple(n * 32 for n in raw.shape[-2:])))}


@pytest.fixture
def target_data():
    y, x = np.indices((48, 512), dtype=np.float64)
    raw = np.stack([1000 * (1 + .02 * y / 48 + amplitude * np.cos(2 * np.pi * x / period + phase))
                    for amplitude, period, phase in ((.08, 25.7, .2), (.14, 31.2, .7), (.12, 29.2, 1.1))])
    return raw.astype(np.float32)


def one_metadata(raw, *, native=None):
    return {"channel_mapping": {"FITC": 0}, "channel_names": ["FITC"],
            "sizes": dict(zip(("Y", "X"), native or tuple(n * 32 for n in raw.shape[-2:])))}


def fields():
    native = np.array([1051, 16377], dtype=np.int64)
    centres = bm.bin_centers(int(native[1]))
    return {"model_schema": np.asarray(bm.LEGACY_SCHEMA), "channel_indices": np.array([3]),
            "enabled": np.array([True]), "gain_x": (1 + .1 * np.cos(np.arange(len(centres)) / 7))[None],
            "x_bin_centers": centres, "native_shape_yx": native, "downsample": np.asarray(32)}


def test_equivalent_to_frozen_benchmark_and_no_input_mutation(target_data, tmp_path):
    before = target_data.copy()
    reference = _frozen_gains()
    progress = []
    model, result = bm.fit_legacy(target_data, metadata(target_data), tmp_path / "model.npz", progress=progress.append)
    try:
        assert model.model_schema == bm.LEGACY_SCHEMA
        assert set(model.data.files) == bm.LEGACY_FIELD_NAMES
        assert model.indices == [0, 2, 3] and model.enabled == [True, True, True]
        np.testing.assert_allclose(model.gain_x, reference, rtol=1e-8, atol=1e-10)
        np.testing.assert_array_equal(target_data, before)
        assert result["config"]["parameters"] == _FROZEN_PARAMETERS
        assert result["uses_bf"] is False and result["BF_modified"] is False
        assert result["production_eligible"] is False
        assert result["working_memory"]["estimated_peak_bytes"] <= 1024**3
        assert progress[0] == 0 and progress[-1] == 1 and progress == sorted(progress)
        for row, ref in zip(result["selected"], _FROZEN_AXES):
            assert row["outcome"] == "fitted"
            assert row["axes"][0]["axis"] == "x" and row["axes"][0]["status"] == "fitted"
            for key, value in ref.items():
                assert row["axes"][0][key] == pytest.approx(value, rel=1e-7, abs=1e-10)
    finally:
        model.data.close()


def test_independent_abstention_does_not_disable_other_target_or_touch_bf(target_data, tmp_path):
    target_data[1] = 0
    target_data[2] = 1000
    reference = _frozen_gains()[0]
    model, result = bm.fit_legacy(target_data, metadata(target_data), tmp_path / "model.npz")
    try:
        assert model.enabled == [True, False, False]
        np.testing.assert_allclose(model.gain_x[0], reference, rtol=1e-8, atol=1e-10)
        assert result["selected"][1]["reason"] == "insufficient_positive_target_support"
        assert result["selected"][2]["reason"] == "weak_periodic_evidence"
        tile = np.arange(63, dtype=np.uint16).reshape(7, 9)
        for index in (1, 2, 3, 99):  # BF, two abstentions, and an absent channel.
            np.testing.assert_array_equal(model.correct(index, tile, 100, 200), tile.astype(np.float32))
    finally:
        model.data.close()


def test_all_abstained_is_valid_disabled_model(tmp_path):
    raw = np.full((1, 16, 384), 1000, dtype=np.float32)
    model, result = bm.fit_legacy(raw, one_metadata(raw), tmp_path / "identity.npz")
    try:
        assert model.enabled == [False]
        assert result["supported_channel_axis_count"] == 0
        assert result["selected"][0]["outcome"] == "unchanged"
        np.testing.assert_array_equal(model.gain_x, np.ones((1, 384)))
        np.testing.assert_array_equal(model.gain(0, (3, 7), (0, 0)), np.ones((3, 7)))
        model.validate()
    finally:
        model.data.close()


@pytest.mark.parametrize("change", ["bf_mapping", "bf_mask", "camera_geometry", "extra_row", "wrong_name", "duplicate_index", "native_mismatch"])
def test_non_target_or_inconsistent_inputs_rejected_before_fit(target_data, tmp_path, monkeypatch, change):
    meta = metadata(target_data)
    raw = target_data
    if change == "bf_mapping":
        meta["channel_mapping"] = {"BF": 1, "DAPI": 2, "FITC": 3}
    elif change in ("bf_mask", "camera_geometry"):
        meta[change] = np.ones((3, 3))
    elif change == "extra_row":
        raw = np.concatenate((raw, raw[:1]))
    elif change == "wrong_name":
        meta["channel_names"][3] = "BF"
    elif change == "duplicate_index":
        meta["channel_mapping"]["FITC"] = 0
    else:
        meta["sizes"]["X"] += 32
    monkeypatch.setattr(bp, "fit_channel", lambda *_args, **_kwargs: pytest.fail("Invalid input reached fitting"))
    with pytest.raises(ValueError):
        bm.fit_legacy(raw, meta, tmp_path / "bad.npz")
    assert not list(tmp_path.iterdir())


def test_actual_partial_bin_means_and_native_interpolation(tmp_path):
    height, width = 32 * 9 - 3, 32 * 512 - 7
    native_x = 1000 * (1 + .12 * np.cos(2 * np.pi * np.arange(width) / (32 * 29.2) + .3))
    native_y = 1 + .02 * np.arange(height) / height
    native = native_y[:, None] * native_x[None, :]
    ys, xs = np.arange(0, height, 32), np.arange(0, width, 32)
    sums = np.add.reduceat(np.add.reduceat(native, ys, axis=0), xs, axis=1)
    counts = np.minimum(32, height - ys)[:, None] * np.minimum(32, width - xs)[None, :]
    raw = (sums / counts)[None]
    assert raw[0, -1, -1] == pytest.approx(native[-29:, -25:].mean())
    model, _ = bm.fit_legacy(raw, one_metadata(raw, native=(height, width)), tmp_path / "partial.npz")
    try:
        assert model.x_bin_centers[-1] == width - 13
        assert model.x_bin_centers[-1] != 15.5 + 32 * (raw.shape[-1] - 1)
        roi = (height - 11, width - 103, 11, 103)
        expected = _native_line_reference(model.gain_x[0], width, roi)
        np.testing.assert_allclose(model.gain(0, roi[2:], roi[:2]), expected, rtol=1e-7, atol=1e-7)
        first = _native_line_reference(model.gain_x[0], width, (0, 0, 3, 35))
        np.testing.assert_allclose(model.gain(0, (3, 35), (0, 0)), first, rtol=1e-7, atol=1e-7)
        with pytest.raises(ValueError, match="outside"):
            model.gain(0, (4, 4), (height - 3, width - 3))
    finally:
        model.data.close()


@pytest.mark.parametrize("change", ["extra", "missing", "schema", "object", "nan", "low", "high",
                                    "indices", "enabled", "centres", "native", "factor"])
def test_malformed_models_are_rejected(tmp_path, change):
    values = fields()
    if change == "extra":
        values["basis"] = np.eye(2)
    elif change == "missing":
        del values["enabled"]
    elif change == "schema":
        values["model_schema"] = np.asarray("BF_free_periodic_x2/1")
    elif change == "object":
        values["gain_x"] = values["gain_x"].astype(object)
    elif change in ("nan", "low", "high"):
        values["gain_x"][0, 0] = {"nan": np.nan, "low": .499, "high": 2.001}[change]
    elif change == "indices":
        values["channel_indices"] = np.array([-1])
    elif change == "enabled":
        values["enabled"] = np.array([1])
    elif change == "centres":
        values["x_bin_centers"][-1] += .5
    elif change == "native":
        values["native_shape_yx"][1] += 32
    else:
        values["downsample"] = np.asarray(16)
    path = tmp_path / "bad.npz"
    np.savez_compressed(path, **values)
    with pytest.raises(ValueError):
        bm.Model(path)


def test_duplicate_archive_entries_rejected(tmp_path):
    path = tmp_path / "duplicate.npz"
    np.savez_compressed(path, **fields())
    with zipfile.ZipFile(path) as archive:
        duplicate = archive.read("enabled.npy")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("enabled.npy", duplicate)
    with pytest.raises(ValueError, match="archive"):
        bm.Model(path)


def test_bounded_archive_and_read_only_stream(tmp_path, monkeypatch):
    stream = io.BytesIO()
    np.savez_compressed(stream, **fields())
    stream.seek(0)
    model = bm.Model(stream)
    try:
        assert model.path is None
        model.enabled[0] = False
        model.validate()
        np.testing.assert_array_equal(model.gain(3, (4, 7), (2, 5)), np.ones((4, 7)))
        assert model.gain_bounds == [(1.0, 1.0)]
    finally:
        model.data.close()
    path = tmp_path / "bounded.npz"
    np.savez_compressed(path, **fields())
    monkeypatch.setattr(bm, "MAX_MODEL_BYTES", 100)
    monkeypatch.setattr(bm.np, "load", lambda *_args, **_kwargs: pytest.fail("Oversized archive was decompressed"))
    with pytest.raises(ValueError, match="oversized"):
        bm.Model(path)


def test_memory_guard_precedes_pixel_work(tmp_path, monkeypatch):
    raw = np.broadcast_to(np.float32(1000), (1, 4096, 4096))
    monkeypatch.setattr(bp, "fit_channel", lambda *_args, **_kwargs: pytest.fail("Budget refusal reached fitting"))
    with pytest.raises(bm.Unsupported, match="working_memory_budget_exceeded"):
        bm.fit_legacy(raw, one_metadata(raw), tmp_path / "oversized.npz")
    assert not list(tmp_path.iterdir())


def test_cancellation_reaches_optimizer_without_output(target_data, tmp_path, monkeypatch):
    class Cancelled(Exception):
        pass
    optimizing = False
    original = bp.minimize_scalar

    def optimizer(*args, **kwargs):
        nonlocal optimizing
        optimizing = True
        try:
            return original(*args, **kwargs)
        finally:
            optimizing = False

    def check():
        if optimizing:
            raise Cancelled

    monkeypatch.setattr(bp, "minimize_scalar", optimizer)
    with pytest.raises(Cancelled):
        bm.fit_legacy(target_data, metadata(target_data), tmp_path / "cancelled.npz", check=check)
    assert not list(tmp_path.iterdir())
