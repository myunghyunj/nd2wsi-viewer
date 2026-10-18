"""Legacy H_auto fits BF with the original evidence and candidate rules."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from nd2wsi import h_model as hm


@pytest.fixture
def periodic_input(monkeypatch):
    config = json.loads(Path(hm.__file__).with_name("h_config.json").read_text())
    config["h_auto"]["split_fractions_y0y1x0x1"] = {
        "train": [0, .5, 0, .5], "heldout_columns": [0, .5, .5, 1],
        "heldout_rows": [.5, 1, 0, .5], "heldout_both": [.5, 1, .5, 1],
    }
    basis = np.diag([1 / 16, 1 / 16])
    # Nonstandard channel order exercises metadata-driven selection and keeps
    # the DAPI index between fitted channels.
    names, amplitudes = ["BF", "DAPI", "FITC", "CY3"], [.18, .04, .12, .08]

    def raw(side, factor):
        y, x = np.indices((side, side))
        phase = np.cos(2 * np.pi * (x * factor + (factor - 1) / 2 - 15.5) / 512)
        phase += .3 * np.cos(2 * np.pi * (y * factor + (factor - 1) / 2 - 15.5) / 512)
        return np.stack([1000 * (1 + amplitude * phase) for amplitude in amplitudes]).astype(np.float32)

    meta = {"channel_mapping": {name: i for i, name in enumerate(names)}, "dtype": "uint16",
            "channel_acquisition_settings": [{"sensor_pixels_xy": {"cx": 512, "cy": 512}}] * 4,
            "sizes": {"Y": 4096, "X": 4096}}
    # Known synthetic geometry; all sampling, refinement, channel selection,
    # candidate scoring, gain validation and persistence run normally.
    monkeypatch.setattr(hm, "geometry", lambda *_: (basis, []))
    return raw(512, 8), raw(128, 32), meta, config


def test_bf_is_fitted_without_changing_fluorescence_or_selecting_dapi(periodic_input, tmp_path):
    raw8, raw32, meta, config = periodic_input
    previous_config = copy.deepcopy(config)
    previous_config["h_auto"]["target_channels"] = ["FITC", "CY3"]
    previous, _ = hm._fit_impl(raw8, raw32, meta, previous_config, tmp_path / "two.npz")
    current, record = hm._fit_impl(raw8, raw32, meta, config, tmp_path / "three.npz")
    try:
        assert current.indices == [2, 3, 0]
        assert all(current.enabled)
        assert meta["channel_mapping"]["DAPI"] not in current.indices
        assert record["BF_modified"] is True and record["DAPI_modified"] is False
        bf = next(row for row in record["selected"] if row["channel"] == "BF")
        assert bf["outcome"] == "fitted" and min(bf["scores"].values()) > 0
        assert bf["gain_min"] >= .25
        for field in ("basis", "oldbasis", "delta"):
            np.testing.assert_array_equal(getattr(previous, field), getattr(current, field))
        for ci in previous.indices:
            np.testing.assert_array_equal(previous.gain(ci, (129, 181), (307, 983)),
                                          current.gain(ci, (129, 181), (307, 983)))
    finally:
        previous.data.close()
        current.data.close()


def test_bf_failed_channel_evidence_keeps_bf_unchanged(periodic_input, tmp_path, monkeypatch):
    raw8, raw32, meta, config = periodic_input
    sample = hm._sample_and_norm_active_chunked

    def incompatible_bf_heldout_samples(raw, basis, records, active_mask, **kwargs):
        coordinates, observations, finite = sample(raw, basis, records, active_mask, **kwargs)
        for i, row in enumerate(r for r, active in zip(records, active_mask) if active):
            if row["group"] in ("heldout_columns", "heldout_rows"):
                observations[i, meta["channel_mapping"]["BF"]] *= -1
        return coordinates, observations, finite

    monkeypatch.setattr(hm, "_sample_and_norm_active_chunked", incompatible_bf_heldout_samples)
    model, record = hm._fit_impl(raw8, raw32, meta, config, tmp_path / "refused.npz")
    try:
        bf = next(row for row in record["selected"] if row["channel"] == "BF")
        assert bf["outcome"] == "unchanged_no_reproducible_or_valid_channel_template"
        assert max(bf["scores"].values()) < 0
        assert record["BF_modified"] is False
        assert model.enabled == [True, True, False]
        np.testing.assert_array_equal(model.gain(0, (19, 29), (123, 456)), np.ones((19, 29)))
    finally:
        model.data.close()


@pytest.mark.parametrize("targets", [["DAPI"], ["BF", "BF"], [], "BF", ["FITC", None]])
def test_invalid_targets_are_rejected_before_fit(periodic_input, tmp_path, monkeypatch, targets):
    raw8, raw32, meta, config = periodic_input
    config["h_auto"]["target_channels"] = targets
    monkeypatch.setattr(hm, "geometry", lambda *_: pytest.fail("invalid target reached fitting"))
    with pytest.raises(ValueError, match="target_channels"):
        hm._fit_impl(raw8, raw32, meta, config, tmp_path / "invalid.npz")
    assert not list(tmp_path.iterdir())
