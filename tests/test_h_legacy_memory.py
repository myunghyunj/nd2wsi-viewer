"""Admission includes overlapping active/aligned buffers and scoring work."""

import json
from pathlib import Path

import numpy as np
import pytest

from nd2wsi import h_model as hm


def plan(**overrides):
    values = dict(n_total=610, n_active=610, n_train=200, n_selection=205,
                  n_channels=4, input_itemsize=4, coarse_itemsize=4, n_targets=3)
    return hm._legacy_fit_workspace_plan(**{**values, **overrides})


def test_previous_admission_boundary_includes_aligned_cells():
    pixels, limit = 192 * 256, 1024**3
    old_estimate = (610 * (16 + 4 * 4) + 32 * (16 + (4 + 8) * 4)) * pixels
    # Reproduces the reviewer's concrete counterexample: the former estimate
    # passed although the three retained arrays alone exceed the entire cap.
    retained_arrays = (610 * (16 + 4 * 4) + 200 * 4 * 4) * pixels
    assert old_estimate == 1011 * 1024**2 <= limit
    assert retained_arrays == 1065 * 1024**2 > limit
    unrestricted = plan()
    assert unrestricted["retained_active_and_aligned_bytes"] == retained_arrays
    assert unrestricted["phase_estimates"]["aligned_sampling_bytes"] > retained_arrays
    assert unrestricted["phase_estimates"]["candidate_scoring_bytes"] > retained_arrays
    with pytest.raises(hm.Unsupported, match="fit_working_memory_limit"):
        plan(max_working_bytes=limit)


def test_chunk_reduction_only_admits_when_all_phases_fit():
    args = dict(n_total=300, n_active=300, n_train=100, n_selection=100)
    default = plan(**args)
    bounded = plan(**args, max_working_bytes=900 * 1024**2)
    assert default["chunk_cells"] == 32
    assert 1 <= bounded["chunk_cells"] < 32
    assert bounded["estimated_peak_bytes"] == max(bounded["phase_estimates"].values())
    assert bounded["estimated_peak_bytes"] <= bounded["limit_bytes"]
    # Sampling chunks cannot rescue a budget below candidate-building needs.
    with pytest.raises(hm.Unsupported, match="fit_working_memory_limit"):
        plan(**args, max_working_bytes=bounded["phase_estimates"]["candidate_build_bytes"] - 1)


def test_fit_rejects_boundary_before_normalization_or_high_resolution_sampling(monkeypatch, tmp_path):
    config = json.loads(Path(hm.__file__).with_name("h_config.json").read_text())
    meta = {"channel_mapping": {"FITC": 0, "CY3": 1, "DAPI": 2, "BF": 3}, "dtype": "uint16",
            "channel_acquisition_settings": [{"sensor_pixels_xy": {"cx": 512, "cy": 512}}] * 4}
    # Only the repeat counts are needed to exercise admission. Small coarse
    # stand-ins avoid allocating the >1 GiB high-resolution counterexample.
    records = [{"group": group, "row": i, "column": 0}
               for group, count in (("train", 200), ("heldout_columns", 205), ("heldout_rows", 205))
               for i in range(count)]
    monkeypatch.setattr(hm, "geometry", lambda *_: (np.diag([1 / 16, 1 / 16]), []))
    monkeypatch.setattr(hm, "phase_cells", lambda *_: (np.ones((610, 4, 1, 1)), records))
    monkeypatch.setattr(hm, "norm", lambda *_a, **_k: pytest.fail("budget rejection happened too late"))
    monkeypatch.setattr(hm, "_sample_and_norm_active_chunked", lambda *_a, **_k: pytest.fail("high-resolution sampling reached"))
    raw32 = np.full((4, 128, 128), 100, dtype=np.float32)
    with pytest.raises(hm.Unsupported, match="fit_working_memory_limit"):
        hm._fit_impl(raw32, raw32, meta, config, tmp_path / "rejected.npz", max_working_bytes=1024**3)
    assert not list(tmp_path.iterdir())
