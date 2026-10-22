"""Packaged rank-1 port equivalence and native/persistence boundaries.

The reference was produced once by the pinned original research implementations,
not by this package. Tests never need an external drive or a research slide.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from nd2wsi import bffree_consensus as bc
from nd2wsi import bffree_consensus_amplitude as ba
from nd2wsi import bffree_consensus_template as bt
from nd2wsi import bffree_model as bm


def synthetic_raw():
    h, w = 256, 384
    y, x = np.indices((h, w), dtype=float)
    return np.stack([
        1200 * (1 + .08*y/h) * (1 + (.075 + .04*y/h)*np.cos(2*np.pi*x/27.3 + .4)),
        900 * (1 + .12*x/w) * (1 + (.11 + .025*x/w)*np.cos(2*np.pi*y/25.7 + .9)),
        800 * (1 + .02*y/h) * (1 + .09*np.cos(2*np.pi*x/29.2 + 1.1)),
    ]).astype(np.float32)


def metadata(raw):
    return {"channel_mapping": {"DAPI": 3, "CY3": 2, "FITC": 0},
            "channel_names": ["FITC", "BF", "CY3", "DAPI"],
            "sizes": {"Y": raw.shape[1]*32-3, "X": raw.shape[2]*32-7}}


@pytest.fixture(scope="module")
def fitted(tmp_path_factory):
    raw = synthetic_raw()
    before = raw.copy()
    folder = tmp_path_factory.mktemp("consensus-rank1")
    progress = []
    model, result = bm.fit(raw, metadata(raw), folder / "model.npz", progress=progress.append)
    np.testing.assert_array_equal(raw, before)
    assert progress[0] == 0 and progress[-1] == 1 and progress == sorted(progress)
    yield model, result, folder
    model.data.close()


def test_port_matches_pinned_original_template_amplitude_native_gain_and_decisions(fitted):
    model, result, _ = fitted
    reference = json.loads((Path(__file__).parent / "fixtures/consensus_rank1_port_reference.json").read_text())
    assert hashlib.sha256(synthetic_raw().tobytes()).hexdigest() == reference["raw_sha256"]
    assert result["config"]["parameters"] == reference["parameters"]
    for name, digest in reference["source_sha256"].items():
        assert result["config"]["research_source_sha256"][name] == digest
    assert model.model_schema == bm.SCHEMA == "BF_free_consensus_rank1/1"
    assert set(model.data.files) == bm.FIELD_NAMES
    assert model.indices == [0, 2, 3] and model.enabled == [True, True, False]
    assert result["config"]["candidate_id"] == "consensus_rank1"
    assert result["uses_bf"] is False and result["BF_modified"] is False and result["DAPI_modified"] is False
    assert result["working_memory"]["estimated_peak_bytes"] <= 1024**3
    for row, (actual, expected) in enumerate(zip(model.prediction["channel_models"], reference["channel_models"])):
        for key in ("status", "applied", "reason", "channel_name", "channel_index"):
            assert actual[key] == expected[key]
        a, e = actual["model"], expected["model"]
        assert a["axis"] == e["axis"]
        assert a["supported_harmonics"] == e["supported_harmonics"]
        for key in ("frequency_cycles_per_native_pixel", "period_native_pixels", "phase_grid", "template_values"):
            np.testing.assert_allclose(a[key], e[key], rtol=1e-9, atol=1e-10)
        for key in ("joint_amplitude_nodes", "joint_phase_nodes", "joint_orthogonal_native_nodes", "amplitude_cap"):
            np.testing.assert_allclose(a[key], e[key], rtol=2e-6, atol=2e-8)
        for roi, expected_gain in zip(reference["rois_yxhw"], reference["native_gains"][row]):
            actual_gain = model.gain(model.indices[row], roi[2:], roi[:2])
            assert actual_gain.dtype == np.float64
            np.testing.assert_allclose(actual_gain, expected_gain, rtol=1e-7, atol=1e-8)
        ys, xs = [bc.evidence.centers(int(n), 32) for n in model.native_shape_yx]
        gain = bc.evidence.components_at(model._models[row], ys, xs)["G"] if model.enabled[row] else np.ones((len(ys), len(xs)))
        assert np.median(gain) == pytest.approx(reference["median_overview_gain"][row], rel=1e-7, abs=1e-8)


def test_native_float64_division_identity_and_subregion_coordinates(fitted):
    model, _, _ = fitted
    tile = np.arange(13*37, dtype=np.uint32).reshape(13, 37) + 2**24 + 1
    whole = model.gain(0, tile.shape, (1031, 713))
    np.testing.assert_array_equal(whole[:, 9:23], model.gain(0, (13, 14), (1031, 722)))
    expected = np.asarray(tile / whole, dtype=np.float32)
    np.testing.assert_array_equal(model.correct(0, tile, 1031, 713), expected)
    # This fixture detects a premature float32 conversion of gain or source.
    assert np.any(expected != tile.astype(np.float32) / whole.astype(np.float32))
    for c in (1, 3, 99):  # BF, estimated DAPI and absent channel.
        np.testing.assert_array_equal(model.correct(c, tile, 1031, 713), tile.astype(np.float32))
    with pytest.raises(ValueError, match="outside"):
        model.gain(0, (2, 2), (int(model.native_shape_yx[0])-1, 0))


def test_roundtrip_from_read_only_bytes_preserves_exact_native_gain(fitted):
    model, result, folder = fitted
    reopened = bm.Model(io.BytesIO((folder / "model.npz").read_bytes()))
    try:
        assert reopened.path is None
        assert json.loads((folder / "model.json").read_text())["config"] == result["config"]
        for channel in model.indices:
            np.testing.assert_array_equal(reopened.gain(channel, (19, 23), (4101, 5133)),
                                          model.gain(channel, (19, 23), (4101, 5133)))
    finally:
        reopened.data.close()


def test_all_unsupported_is_valid_disabled_identity(tmp_path):
    raw = np.full((1, 16, 384), 1000, dtype=np.float32)
    meta = {"channel_mapping": {"FITC": 0}, "channel_names": ["FITC"], "sizes": {"Y": 512, "X": 12288}}
    model, result = bm.fit(raw, meta, tmp_path / "identity.npz")
    try:
        assert model.enabled == [False] and result["supported_channel_axis_count"] == 0
        np.testing.assert_array_equal(model.gain(0, (2, 7), (0, 0)), np.ones((2, 7)))
    finally:
        model.data.close()


@pytest.mark.parametrize("change", ["BF", "bf_mask", "camera_geometry", "native_shape"])
def test_forbidden_metadata_or_mapping_cannot_enter_estimation(tmp_path, monkeypatch, change):
    raw = synthetic_raw()
    meta = metadata(raw)
    if change == "BF":
        meta["channel_mapping"] = {"BF": 1, "CY3": 2, "DAPI": 3}
    elif change == "native_shape":
        meta["sizes"]["X"] += 32
    else:
        meta[change] = np.ones((4, 4))
    monkeypatch.setattr(bc, "fit_prediction", lambda *_a, **_k: pytest.fail("Invalid input reached fitting"))
    with pytest.raises(ValueError):
        bm.fit(raw, meta, tmp_path / "bad.npz")
    assert not list(tmp_path.iterdir())


def test_memory_admission_precedes_pixel_estimation(tmp_path, monkeypatch):
    raw = np.broadcast_to(np.float32(1000), (1, 4096, 4096))
    meta = {"channel_mapping": {"FITC": 0}, "channel_names": ["FITC"], "sizes": {"Y": 131072, "X": 131072}}
    monkeypatch.setattr(bc, "fit_prediction", lambda *_a, **_k: pytest.fail("Oversized fit entered estimation"))
    with pytest.raises(bm.Unsupported, match="working_memory_budget_exceeded"):
        bm.fit(raw, meta, tmp_path / "too-large.npz")


@pytest.mark.parametrize("stage", ["template", "amplitude"])
def test_cancellation_reaches_optimizer_and_preserves_prior_files(tmp_path, monkeypatch, stage):
    class Cancelled(Exception):
        pass
    module, name = (bt, "minimize_scalar") if stage == "template" else (ba, "minimize")
    original = getattr(module, name)
    optimizing = False
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
    monkeypatch.setattr(module, name, optimizer)
    path = tmp_path / "model.npz"
    path.write_bytes(b"preserved model")
    path.with_suffix(".json").write_text("preserved provenance")
    raw = synthetic_raw()
    with pytest.raises(Cancelled):
        bm.fit(raw, metadata(raw), path, check=check)
    assert path.read_bytes() == b"preserved model"
    assert path.with_suffix(".json").read_text() == "preserved provenance"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["model.json", "model.npz"]


@pytest.mark.parametrize("change", ["DAPI", "BF", "nan", "bounds", "template", "phase", "spacing", "method", "schema"])
def test_malformed_rank1_archive_rejected(fitted, tmp_path, change):
    model, _, folder = fitted
    with np.load(folder / "model.npz", allow_pickle=False) as data:
        fields = {key: data[key].copy() for key in data.files}
    prediction = json.loads(fields["prediction_json"].item())
    if change == "DAPI":
        fields["enabled"][2] = True
        prediction["channel_models"][2]["applied"] = True
    elif change == "BF":
        fields["channel_names"][0] = "BF"
    elif change == "schema":
        fields["model_schema"] = np.asarray("BF_free_consensus_rank2/1")
    elif change == "method":
        prediction["diagnostics"]["config"]["parameters"]["amplitude"]["rank"] = 2
    else:
        rec = prediction["channel_models"][0]["model"]
        if change in ("nan", "bounds"):
            rec["joint_amplitude_nodes"][0][0] = float("nan") if change == "nan" else 4
        elif change == "template":
            rec["template_values"][0] += .1
        elif change == "phase":
            rec["phase_grid"][1] += .01
        else:
            rec["joint_phase_nodes"][1] += .1
    fields["prediction_json"] = np.asarray(json.dumps(prediction))
    path = tmp_path / "bad.npz"
    np.savez_compressed(path, **fields)
    with pytest.raises(ValueError):
        bm.Model(path)
