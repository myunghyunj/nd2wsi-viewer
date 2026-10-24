"""BF extrapolation calibration excludes the predicted cell and admission strip."""

from copy import deepcopy

import numpy as np
import pytest

from nd2wsi import h_adaptive as adaptive
from nd2wsi.h_model import Unsupported


def cells(amplitude=1.0, count=4):
    return [
        {
            "row": 0, "column": 3 * i, "is_complete_97": True,
            "obs": {"BF": {
                "A": {"amplitude": amplitude, "pos_shape_r2": 0.95, "valid_fraction": 1.0},
                "B": {"amplitude": amplitude, "pos_shape_r2": 0.95, "valid_fraction": 1.0},
                # No amplitude can be read from the admission strip.
                "C": object(),
            }},
        } for i in range(count)
    ]


def test_bf_prediction_bound_holds_out_each_cell_and_never_reads_c(monkeypatch):
    observed = cells()
    omitted = []

    def predict(remaining, names, origin, extent, gammas, **kwargs):
        assert names == ["BF"]
        assert len(remaining) == len(observed) - 1
        omitted.append(next(i for i, cell in enumerate(observed)
                            if all(cell is not item for item in remaining)))
        return np.full((1, 3, 25), 2.0), {}

    monkeypatch.setattr(adaptive, "build_coupled_subcell_amplitude_nodes", predict)
    result = adaptive.calibrate_channel_prediction_bound(
        observed, (0, 0), (1, 12), np.ones(256), adaptive.DEFAULT_JUSTIFIED_CRITERIA,
    )
    assert omitted == [0, 1, 2, 3]
    assert result["scale"] == pytest.approx(0.5)
    assert result["assessed_cells"] == 4
    assert result["assessed_observations"] == 8
    assert result["admission_strip_C_used"] is False
    assert all(cell["obs"]["BF"]["A"]["amplitude"] == 1.0 for cell in observed)


def test_prediction_bound_cannot_increase_correction(monkeypatch):
    monkeypatch.setattr(adaptive, "build_coupled_subcell_amplitude_nodes",
                        lambda *a, **kw: (np.ones((1, 3, 25)), {}))
    result = adaptive.calibrate_channel_prediction_bound(
        cells(amplitude=2.0), (0, 0), (1, 12), np.ones(256), adaptive.DEFAULT_JUSTIFIED_CRITERIA,
    )
    assert result["scale"] == 1.0


def test_prediction_bound_requires_enough_independent_cells(monkeypatch):
    monkeypatch.setattr(adaptive, "build_coupled_subcell_amplitude_nodes",
                        lambda *a, **kw: (np.ones((1, 3, 25)), {}))
    with pytest.raises(Unsupported, match="insufficient_BF_predictive_calibration"):
        adaptive.calibrate_channel_prediction_bound(
            cells(count=3), (0, 0), (1, 12), np.ones(256), adaptive.DEFAULT_JUSTIFIED_CRITERIA,
        )


def test_prediction_bound_honors_configured_reliability_threshold(monkeypatch):
    monkeypatch.setattr(adaptive, "build_coupled_subcell_amplitude_nodes",
                        lambda *a, **kw: pytest.fail("unreliable cells must not be used"))
    criteria = deepcopy(adaptive.DEFAULT_JUSTIFIED_CRITERIA)
    criteria["reduced_resolution_gates"]["active_band_cell_definition"]["min_raw_block_c_shape_r2"] = 0.99
    with pytest.raises(Unsupported, match="insufficient_BF_predictive_calibration"):
        adaptive.calibrate_channel_prediction_bound(cells(), (0, 0), (1, 12), np.ones(256), criteria)


def test_prediction_bound_cancellation_precedes_refitting(monkeypatch):
    monkeypatch.setattr(adaptive, "build_coupled_subcell_amplitude_nodes",
                        lambda *a, **kw: pytest.fail("cancelled calibration must not refit"))

    def cancel():
        raise RuntimeError("cancel requested")

    with pytest.raises(RuntimeError, match="cancel requested"):
        adaptive.calibrate_channel_prediction_bound(
            cells(), (0, 0), (1, 12), np.ones(256), adaptive.DEFAULT_JUSTIFIED_CRITERIA, check=cancel,
        )


@pytest.mark.parametrize('failure', ['bf_calibration', 'cy3_gain'])
def test_one_channel_candidate_rejection_keeps_other_channels_eligible(failure, tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from nd2wsi import h_model as base

    config = json.loads(Path(adaptive.__file__).with_name('h_config.json').read_text())
    mapping = {'BF': 0, 'FITC': 1, 'CY3': 2, 'DAPI': 3}
    raw32 = np.full((4, 96, 96), 100, dtype=np.float32)
    raw8 = np.full((4, 384, 384), 100, dtype=np.float32)
    meta = {'channel_mapping': mapping, 'dtype': 'uint16',
            'channel_acquisition_settings': [{'sensor_pixels_xy': {'cx': 320, 'cy': 320}}] * 4}
    basis = np.diag([.1, .1])
    signal = 100 * (1 + .1 * np.sin(2 * np.pi * np.arange(256) / 256))
    values = np.broadcast_to(signal, (3, 192, 256)).astype(np.float32).copy()
    sampled = []
    for row, group in enumerate(('train', 'heldout_columns', 'heldout_rows', 'heldout_both')):
        for column in range(4):
            coordinates = base.coords_for(basis, [{'row': row, 'column': column}], factor=8)[0].astype(np.float32)
            valid = np.ones((192, 256), bool)
            sampled.append({'row': row, 'column': column, 'group': group, 'is_complete_97': True,
                            'whole_coverage': 1., 'coords_raw8': coordinates, 'values_fit': values.copy(),
                            'valid_mask': valid, 'valid_est_mask': valid})
    monkeypatch.setattr(base, 'geometry', lambda *_: (basis, []))
    monkeypatch.setattr(adaptive, 'supported_x_geometry', lambda *_: (basis[1], {}))
    monkeypatch.setattr(adaptive, 'enumerate_all_lattice_cells', lambda **_: (sampled, (0, 0), (4, 4)))
    rejected = 'BF' if failure == 'bf_calibration' else 'CY3'
    reason = 'insufficient_BF_predictive_calibration' if failure == 'bf_calibration' else 'invalid_candidate_gain_bounds'
    if failure == 'bf_calibration':
        original_calibration = adaptive.calibrate_channel_prediction_bound

        def unavailable(*args, **kwargs):
            if kwargs["channel"] == "BF":
                raise Unsupported(reason)
            return original_calibration(*args, **kwargs)
        monkeypatch.setattr(adaptive, 'calibrate_channel_prediction_bound', unavailable)
    else:
        # Inject after local bounds: an excessive preliminary coupled-node estimate is
        # now legitimately constrained before final candidate preflight.
        original = adaptive.build_local_trough_bound_nodes
        monkeypatch.setattr(adaptive, 'calibrate_channel_prediction_bound',
                            lambda *a, **kw: {'scale': 1.0})

        def excessive(*args, **kwargs):
            nodes, record = original(*args, **kwargs)
            if 'CY3' in args[1]:
                nodes[args[1].index('CY3')] *= 100
            return nodes, record
        monkeypatch.setattr(adaptive, 'build_local_trough_bound_nodes', excessive)
    output = tmp_path / 'fit.npz'
    model, record = adaptive.fit_adaptive_from_config(raw8, raw32, meta, config, output)
    try:
        assert dict(zip(model.indices, model.enabled)) == {mapping[n]: n != rejected for n in ('FITC', 'CY3', 'BF')}
        selected = {r['channel']: r for r in record['selected']}
        assert selected[rejected]['failures'] == [reason]
        np.testing.assert_array_equal(model.gain(mapping[rejected], (10, 10), (0, 0)), 1.)
        assert all(selected[n]['outcome'] == 'fitted' for n in selected if n != rejected)
        # The persisted final model must preserve exactly the same decisions.
        reopened = base.Model(output)
        try:
            assert reopened.enabled == model.enabled
        finally:
            reopened.data.close()
    finally:
        model.data.close()


@pytest.mark.parametrize('channel', ['FITC', 'CY3'])
def test_channel_bound_uses_own_observations_and_coupling(channel, monkeypatch):
    observed = cells(amplitude=9.0)
    for cell in observed:
        cell['obs'][channel] = {
            'A': {'amplitude': 1., 'pos_shape_r2': .95, 'valid_fraction': 1.},
            'B': {'amplitude': 1., 'pos_shape_r2': .95, 'valid_fraction': 1.},
            'C': object(),
        }

    def predict(remaining, names, origin, extent, gammas, **kwargs):
        assert names == [channel] and gammas == {channel: 1.25}
        return np.full((1, 3, 25), 2.), {}

    monkeypatch.setattr(adaptive, 'build_coupled_subcell_amplitude_nodes', predict)
    record = adaptive.calibrate_channel_prediction_bound(
        observed, (0, 0), (1, 12), np.ones(256), adaptive.DEFAULT_JUSTIFIED_CRITERIA,
        channel=channel, gamma=1.25,
    )
    assert record['channel'] == channel
    assert record['scale'] == pytest.approx(.5)
    assert record['admission_strip_C_used'] is False


def test_prediction_bound_withholds_immediate_spatial_neighbors(monkeypatch):
    observed = cells(count=16)
    for index, cell in enumerate(observed):
        cell['row'], cell['column'] = divmod(index, 4)
    calls = []

    def predict(remaining, names, origin, extent, gammas, **kwargs):
        centre = observed[len(calls)]
        expected = [c for c in observed if abs(c['row'] - centre['row']) > 1
                    or abs(c['column'] - centre['column']) > 1]
        assert [id(c) for c in remaining] == [id(c) for c in expected]
        calls.append(centre)
        return np.full((1, 9, 9), 2.), {}

    monkeypatch.setattr(adaptive, 'build_coupled_subcell_amplitude_nodes', predict)
    result = adaptive.calibrate_channel_prediction_bound(
        observed, (0, 0), (4, 4), np.ones(256), adaptive.DEFAULT_JUSTIFIED_CRITERIA,
    )
    assert len(calls) == 16
    assert result['scale'] == pytest.approx(.5)
    assert result['excluded_neighborhood_radius_cells'] == 1
