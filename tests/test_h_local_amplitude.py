"""Local evidence restores correction without extending trust across missing data."""
import copy
import io

import numpy as np
import pytest

from nd2wsi import h_adaptive as adaptive
from nd2wsi import h_model


@pytest.fixture
def sampled():
    x = (np.arange(256) + .5) / 256
    template = -.12 * np.cos(2 * np.pi * x)
    cells = []
    for row in range(3):
        for column in range(5):
            amplitude = .3 if column == 4 else 1.
            values = np.broadcast_to(1000 * (1 + amplitude * template), (1, 192, 256)).copy()
            stats = {"amplitude": amplitude, "pos_shape_r2": 1., "valid_fraction": 1.}
            cells.append({"row": row, "column": column, "values_fit": values,
                          "valid_est_mask": np.ones((192, 256), bool),
                          "obs": {"BF": {"A": dict(stats), "B": dict(stats)}}})
    return cells, template


def build(cells, template, **kwargs):
    return adaptive.build_local_trough_bound_nodes(
        cells, ["BF"], (0, 0), (3, 5), {"BF": 1.}, {"BF": {"scale": .5}},
        templates={"BF": template}, mapping={"BF": 0}, fit_indices=[0], **kwargs,
    )


def test_pure_template_retains_its_measured_amplitude(sampled):
    _, template = sampled
    assert adaptive.local_trough_amplitude_cap(.8 * template, template) == pytest.approx(.8, abs=1e-9)


def test_shape_mismatch_reduces_the_local_bound(sampled):
    _, template = sampled
    x = (np.arange(256) + .5) / 256
    mismatched = .8 * template + .025 * np.sin(4 * np.pi * x)
    assert adaptive.project_amplitude(mismatched, template)["amplitude"] == pytest.approx(.8)
    assert 0 < adaptive.local_trough_amplitude_cap(mismatched, template) < .8


def test_cap_search_never_uses_singular_or_out_of_range_gains():
    x = (np.arange(256) + .5) / 256
    template = -.8 * np.cos(2 * np.pi * x)
    with np.errstate(divide="raise", invalid="raise"):
        cap = adaptive.local_trough_amplitude_cap(2 * template, template)
    gains = 1 + cap * template
    assert gains.min() >= .25 and gains.max() <= 1.75


def test_strong_supported_regions_restore_strength_and_local_weak_region_is_bounded(sampled):
    cells, template = sampled
    coupled, _ = adaptive.build_coupled_subcell_amplitude_nodes(cells, ["BF"], (0, 0), (3, 5), {"BF": 1.})
    nodes, record = build(cells, template, coupled_nodes=coupled)
    assert nodes[0, 3, 1] > 1.7 * (.5 * coupled[0, 3, 1])
    assert nodes[0, 3, 9] <= .3
    assert record["channels"]["BF"]["cap_observations"] > 0
    assert record["channels"]["BF"]["restored_above_fallback_nodes"] > 0


def test_finite_C_signal_and_C_observations_are_never_used(sampled):
    cells, template = sampled

    class NoC(dict):
        def __getitem__(self, key):
            if key == "C":
                pytest.fail("C observation used for local estimation")
            return super().__getitem__(key)

    for cell in cells:
        cell["obs"]["BF"] = NoC(cell["obs"]["BF"])
    baseline, _ = build(cells, template)
    altered = copy.deepcopy(cells)
    for cell in altered:
        cell["values_fit"][:, 136:176] = np.arange(40 * 256).reshape(1, 40, 256) + 1e6
    actual, _ = build(altered, template)
    np.testing.assert_array_equal(actual, baseline)


def test_missing_sample_forces_every_influencing_node_to_fallback(sampled):
    cells, template = sampled
    cell = next(c for c in cells if (c["row"], c["column"]) == (1, 2))
    cell["values_fit"][0, 80, 100] = np.nan
    support = adaptive.finite_node_support(cells, (0, 0), (3, 5))
    np.testing.assert_array_equal(support[2:4, 4:6], np.zeros((2, 2)))
    coupled, _ = adaptive.build_coupled_subcell_amplitude_nodes(cells, ["BF"], (0, 0), (3, 5), {"BF": 1.})
    nodes, _ = build(cells, template, coupled_nodes=coupled)
    np.testing.assert_array_equal(nodes[0, 2:4, 4:6], .5 * coupled[0, 2:4, 4:6])
    assert support[3, 1] == 1


def test_rejected_channel_calibration_does_not_abort_other_channels(sampled):
    cells, template = sampled
    for cell in cells:
        cell["values_fit"] = np.repeat(cell["values_fit"], 2, axis=0)
        cell["obs"]["FITC"] = copy.deepcopy(cell["obs"]["BF"])
    nodes, record = adaptive.build_local_trough_bound_nodes(
        cells, ["BF", "FITC"], (0, 0), (3, 5), {"BF": 1., "FITC": 1.},
        {"BF": {"scale": .5}, "FITC": {"status": "rejected"}},
        templates={"BF": template, "FITC": template}, mapping={"BF": 0, "FITC": 1}, fit_indices=[0, 1],
    )
    assert np.any(nodes[0] > 0) and np.all(nodes[1] == 0)
    assert record["channels"]["BF"]["status"] == "fitted"
    assert record["channels"]["FITC"]["status"] == "unchanged_no_admitted_prediction_calibration"


def test_local_helpers_honor_cancellation(sampled):
    cells, template = sampled

    def cancel():
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        adaptive.local_trough_amplitude_cap(template, template, check=cancel)
    with pytest.raises(InterruptedError):
        adaptive.finite_node_support(cells, (0, 0), (3, 5), check=cancel)
    calls = 0

    def cancel_during_loop():
        nonlocal calls
        calls += 1
        if calls == 25:
            cancel()

    with pytest.raises(InterruptedError):
        build(cells, template, coupled_nodes=np.ones((1, 7, 11)), check=cancel_during_loop)
    assert calls == 25


def _reference_field_at(t, basis, delta, ys, xs, local=True):
    # Literal 2.5.4 h_model.field_at, kept to pin Model.gain bit for bit.
    yy = (np.asarray(ys) - 15.5) / 32
    xx = (np.asarray(xs) - 15.5) / 32
    phase = np.array([basis[0, 0] * yy + basis[0, 1] * xx, basis[1, 0] * yy + basis[1, 1] * xx])
    if local:
        phase[1] += h_model.phase_delta(phase, delta)
    return h_model.sample_template(t, phase)


def _reference_gain(model, c, shape, origin):
    # Literal 2.5.4 Model.gain: the envelope recomputed the phase itself.
    ci = model.indices.index(int(c))
    h, w = shape
    y, x = origin
    if not model.enabled[ci]:
        return np.ones(shape, np.float32)
    xx = x + np.arange(w)[None, :]
    out = np.empty(shape, np.float32)
    has_envelope = model.amplitude_nodes is not None and model.envelope_cell_origin is not None
    for row in range(0, h, 128):
        yy = y + np.arange(row, min(h, row + 128))[:, None]
        if model.old[ci]:
            f = _reference_field_at(model.oldtemplates[ci], model.oldbasis, model.delta, yy, xx, False)
        else:
            f = _reference_field_at(
                model.templates[ci], model.basis, model.delta, yy, xx, bool(model.local[ci])
            )
        if has_envelope:
            yy32 = (np.asarray(yy, dtype=np.float64) - 15.5) / 32.0
            xx32 = (np.asarray(xx, dtype=np.float64) - 15.5) / 32.0
            phase = np.array([
                model.basis[0, 0] * yy32 + model.basis[0, 1] * xx32,
                model.basis[1, 0] * yy32 + model.basis[1, 1] * xx32,
            ])
            if bool(model.local[ci]):
                phase[1] = phase[1] + h_model.phase_delta(phase, model.delta)
            amp = h_model.smooth_node_envelope_at(
                model.amplitude_nodes[ci], model.envelope_cell_origin, phase
            )
            f = amp * f
        out[row : row + len(yy)] = 1 + f
    return out


def _gain_model(kind):
    rng = np.random.default_rng(27)
    fields = dict(
        basis=np.array([[0.9, 0.1], [-0.05, 1.1]]),
        oldbasis=np.array([[1.02, -0.03], [0.04, 0.97]]),
        templates=rng.normal(0, 0.05, (3, 16, 24)),
        oldtemplates=rng.normal(0, 0.05, (3, 20, 20)),
        delta=rng.normal(0, 0.05, (12, 12)),
        local=np.array([True, False, True]),
        old=np.zeros(3, dtype=bool),
        channel_indices=np.array([2, 0, 1]),
        enabled=np.ones(3, dtype=bool),
    )
    if kind == "legacy":
        fields["old"][2] = True
    else:
        fields.update(
            model_schema=np.asarray(h_model.Model.ADAPTIVE_SCHEMA),
            amplitude_nodes=rng.uniform(0.5, 1.0, (3, 14, 14)),
            envelope_cell_origin=np.array([0.5, 1.0]),
        )
    stream = io.BytesIO()
    np.savez(stream, **fields)
    stream.seek(0)
    model = h_model.Model(stream)
    if kind == "adaptive_old":
        # Loading refuses this pairing; an in-process change still keeps the
        # envelope on the current basis, exactly as before.
        model.old[2] = True
    return model


@pytest.mark.parametrize("kind", ["legacy", "adaptive", "adaptive_old"])
def test_gain_reuses_phase_bit_identically(kind):
    model = _gain_model(kind)
    shape = (300, 257)  # two full 128-row bands and a partial one
    origins = [(0, 0), (130, -40), (-77, 913.5), (np.int64(1536), np.int32(2048))]
    raw = np.random.default_rng(5).integers(0, 4000, shape).astype(np.uint16)
    try:
        for c in model.indices:
            for origin in origins:
                expected = _reference_gain(model, c, shape, origin)
                actual = model.gain(c, shape, origin)
                assert actual.dtype == expected.dtype == np.float32
                np.testing.assert_array_equal(actual.view(np.uint32), expected.view(np.uint32))
                corrected = model.correct(c, raw, *origin)
                np.testing.assert_array_equal(
                    corrected.view(np.uint32),
                    (np.asarray(raw, np.float32) / expected).view(np.uint32),
                )
        if kind != "legacy":
            # The envelope lattice covers part of this tile: its interior,
            # taper and zero region (gain exactly 1) are all exercised.
            near = _reference_gain(model, model.indices[0], shape, (0, 0))
            assert np.any(near == 1) and np.any(np.abs(near - 1) > 1e-3)
    finally:
        model.data.close()
