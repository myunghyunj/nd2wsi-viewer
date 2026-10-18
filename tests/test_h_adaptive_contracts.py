"""Adaptive fit budget, cancellation and explicit admission configuration."""

import json
import weakref
from pathlib import Path

import numpy as np
import pytest

from nd2wsi import h_adaptive as adaptive
from nd2wsi import h_model as base


@pytest.fixture
def config():
    return json.loads(Path(adaptive.__file__).with_name("h_config.json").read_text())


@pytest.fixture
def inputs():
    raw32 = np.full((4, 96, 96), 100, dtype=np.float32)
    raw8 = np.full((4, 384, 384), 100, dtype=np.float32)
    meta = {
        "channel_mapping": {"BF": 0, "FITC": 1, "CY3": 2, "DAPI": 3},
        "channel_acquisition_settings": [{"sensor_pixels_xy": {"cx": 320, "cy": 320}}] * 4,
        "dtype": "uint16",
    }
    return raw8, raw32, meta


def test_default_json_is_the_previously_evaluated_strict_contract(config):
    criteria = adaptive.validated_acceptance_criteria(config["h_adaptive"])
    assert criteria == adaptive.DEFAULT_JUSTIFIED_CRITERIA
    gates = criteria["reduced_resolution_gates"]
    assert gates["min_p10_block_c_residual_amplitude_all_heldout"] == -0.10
    assert gates["min_worst_cell_block_c_residual_amplitude_all_heldout"] == -0.15
    criteria["reduced_resolution_gates"]["min_active_cells_per_heldout_group"] = 30
    assert config["h_adaptive"]["acceptance_criteria"] == adaptive.DEFAULT_JUSTIFIED_CRITERIA


@pytest.mark.parametrize("mutation", [
    lambda c: c.pop("acceptance_criteria"),
    lambda c: c["acceptance_criteria"].update(unrecognized_gate=1),
    lambda c: c["acceptance_criteria"]["reduced_resolution_gates"].update(min_active_cells_per_heldout_group=True),
    lambda c: c["acceptance_criteria"]["reduced_resolution_gates"].update(min_active_cells_per_heldout_group=2.5),
    lambda c: c["acceptance_criteria"]["reduced_resolution_gates"].update(min_p10_block_c_residual_amplitude_all_heldout=float("nan")),
    lambda c: c["acceptance_criteria"]["gain_and_engineering_invariants"].update(maximum_valid_gain=3.0),
])
def test_invalid_admission_configuration_is_not_silently_ignored(config, mutation):
    mutation(config["h_adaptive"])
    with pytest.raises(ValueError, match="[Aa]daptive"):
        adaptive.validated_acceptance_criteria(config["h_adaptive"])


def test_budget_rejected_before_geometry_or_output(inputs, config, tmp_path, monkeypatch):
    raw8, raw32, meta = inputs
    monkeypatch.setattr(base, "geometry", lambda *_: pytest.fail("allocation reached geometry"))
    output = tmp_path / "must-not-exist" / "fit.npz"
    for fitter in (base.fit, adaptive.fit_adaptive_from_config):
        with pytest.raises(base.Unsupported, match="fit_working_memory_limit"):
            fitter(raw8, raw32, meta, config, output, max_working_bytes=1)
    assert not output.parent.exists()


def test_adaptive_lattice_budget_rejected_before_sampling(inputs, config, tmp_path, monkeypatch):
    raw8, raw32, meta = inputs
    basis = np.diag([0.10, 0.10])
    monkeypatch.setattr(base, "geometry", lambda *_: (basis, []))
    monkeypatch.setattr(adaptive, "supported_x_geometry", lambda *_: (basis[1], {}))
    monkeypatch.setattr(adaptive, "enumerate_all_lattice_cells", lambda **_: pytest.fail("cell allocation reached"))
    with pytest.raises(base.Unsupported, match="fit_working_memory_limit"):
        adaptive.fit_adaptive_from_config(raw8, raw32, meta, config, tmp_path / "fit.npz",
                                         max_working_bytes=40 * 1024**2)
    assert not list(tmp_path.iterdir())


def test_fallback_passes_budget_and_releases_original_frame(inputs, config, tmp_path, monkeypatch):
    refs = []
    raw8, raw32, meta = inputs

    def first_attempt(*args, **kwargs):
        scratch = np.empty((32, 32))
        refs.append(weakref.ref(scratch))
        raise base.Unsupported("insufficient_repeat_cells_heldout_rows")

    def check():
        pass

    def progress(_):
        pass

    def fallback(*args, **kwargs):
        assert refs[0]() is None
        assert kwargs["max_working_bytes"] == 123456789
        assert kwargs["check"] is check and kwargs["progress"] is progress
        assert kwargs["fallback_reason"] == "insufficient_repeat_cells_heldout_rows"
        return "model", {"status": "supported"}

    monkeypatch.setattr(base, "_fit_impl", first_attempt)
    monkeypatch.setattr(adaptive, "fit_adaptive_from_config", fallback)
    assert base.fit(raw8, raw32, meta, config, tmp_path / "fit.npz", check=check,
                    progress=progress, max_working_bytes=123456789)[0] == "model"


def test_chunked_profile_reduction_preserves_full_stack_arithmetic():
    rng = np.random.default_rng(92)
    cells = [{"values_fit": rng.uniform(1, 8000, (3, 192, 256)).astype(np.float32),
              "valid_mask": rng.random((192, 256)) > 0.03} for _ in range(5)]
    stacked = np.stack([np.where(c["valid_mask"][None], c["values_fit"], np.nan) for c in cells])
    expected = np.nanmedian(base.norm(stacked, strict=False), axis=-2)
    np.testing.assert_array_equal(adaptive._normalized_cell_profiles(cells), expected)


class Cancelled(RuntimeError):
    pass


def cancel():
    raise Cancelled()


@pytest.mark.parametrize("operation", [
    lambda: adaptive.compute_subcell_observations([{}], [], {}, [], {}, check=cancel),
    lambda: adaptive.compute_train_channel_coupling([{"group": "train"}], ["FITC"], check=cancel),
    lambda: adaptive._build_conservative_subcell_field([{}], (0, 0), (1, 1), None, check=cancel),
    lambda: adaptive.build_m5_adaptive_hybrid_nodes([], ["FITC"], (0, 0), (1, 1), {}, check=cancel),
])
def test_long_cell_and_node_loops_honor_cancellation(operation):
    with pytest.raises(Cancelled):
        operation()


def test_explicit_configured_gate_changes_admission(config, monkeypatch):
    criteria = config["h_adaptive"]["acceptance_criteria"]
    cells = []
    for group in ("heldout_columns", "heldout_rows", "heldout_both"):
        for row in range(2):
            cells.append({"is_complete_97": True, "whole_coverage": 1, "group": group,
                          "row": row, "column": 1, "coords_raw8": np.zeros((2, 192, 256)),
                          "values_fit": np.ones((1, 192, 256)), "bg_eval_mask": {"FITC": np.ones((40, 256), bool)},
                          "obs": {"FITC": {"C": {"amplitude": 1., "pos_shape_r2": 1., "profile_energy": 1.}}}})
    monkeypatch.setattr(adaptive, "_gain_at_coords", lambda *args: np.ones((40, 256)))
    monkeypatch.setattr(adaptive, "robust_strip_profile", lambda *args, **kwargs: (np.zeros(256), 1))
    monkeypatch.setattr(adaptive, "project_amplitude", lambda *args: {"amplitude": .1, "profile_energy": .01})
    def evaluate(c):
        return adaptive.evaluate_candidate_on_block_c(None, cells, ["FITC"], {"FITC": 0}, [0], {"FITC": np.ones(256)}, c)
    assert evaluate(criteria)["all_channels_admitted"]
    criteria["reduced_resolution_gates"]["min_active_cells_per_heldout_group"] = 3
    result = evaluate(criteria)
    assert not result["all_channels_admitted"]
    assert all("insufficient_active_cells(2)" in reason for reason in result["channels"]["FITC"]["admission_failures"])
    with pytest.raises(Cancelled):
        adaptive.evaluate_candidate_on_block_c(None, cells, ["FITC"], {"FITC": 0}, [0], {"FITC": np.ones(256)}, criteria, check=cancel)


def test_fitted_record_preserves_the_exact_applied_contract(inputs, config, tmp_path, monkeypatch):
    """Use known sampled periodic cells; fitting/admission/save/load remain real."""
    raw8, raw32, meta = inputs
    basis = np.diag([.1, .1])
    signal = 100 * (1 + .1 * np.sin(2 * np.pi * np.arange(256) / 256))
    values = np.broadcast_to(signal, (3, 192, 256)).astype(np.float32).copy()
    cells = []
    for row, group in enumerate(("train", "heldout_columns", "heldout_rows", "heldout_both")):
        for column in range(4):
            coordinates = base.coords_for(basis, [{"row": row, "column": column}], factor=8)[0].astype(np.float32)
            valid = np.ones((192, 256), bool)
            cells.append({"row": row, "column": column, "group": group, "is_complete_97": True,
                          "whole_coverage": 1., "coords_raw8": coordinates, "values_fit": values.copy(),
                          "valid_mask": valid, "valid_est_mask": valid})
    monkeypatch.setattr(base, "geometry", lambda *_: (basis, []))
    monkeypatch.setattr(adaptive, "supported_x_geometry", lambda *_: (basis[1], {}))
    monkeypatch.setattr(adaptive, "enumerate_all_lattice_cells", lambda **_: (cells, (0, 0), (4, 4)))
    configured = config["h_adaptive"]["acceptance_criteria"]
    configured["reduced_resolution_gates"]["min_active_cells_per_heldout_group"] = 3
    output = tmp_path / "fit.npz"
    model, record = adaptive.fit_adaptive_from_config(raw8, raw32, meta, config, output, max_working_bytes=1024**3)
    try:
        assert all(model.enabled)
        assert record["acceptance_criteria"] == configured
        assert json.loads(output.with_suffix(".json").read_text())["acceptance_criteria"] == configured
        assert record["admission_evaluation"]["all_channels_admitted"]
        memory = record["working_memory"]
        assert memory["estimated_peak_bytes"] <= memory["limit_bytes"]
        assert memory["lattice_cell_upper_bound"] >= len(cells)
        assert memory["normalization_chunk_cells"] == 1
    finally:
        model.data.close()
