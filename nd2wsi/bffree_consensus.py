"""Fluorescence-only consensus model and bounded native application.

Research equations are packaged in the adjacent consensus modules. Only
overview pixels enter fitting; no pretrained slide, BF image or camera prior
is read. Native evaluation retains the sampled template and P-spaced lattice.
"""
from __future__ import annotations

import copy
import json
import operator
import zipfile
from pathlib import Path

import numpy as np

from . import bffree_consensus_amplitude as amplitude
from . import bffree_consensus_evidence as evidence
from . import bffree_consensus_template as template
from .bffree_consensus_math import cancellation, checkpoint
from .bffree_serialization import SCHEMA, SUPPORTED_SCHEMAS, contract_for_schema

FIELD_NAMES = frozenset(("model_schema", "channel_indices", "channel_names", "enabled",
                         "native_shape_yx", "downsample", "prediction_json"))
MAX_MODEL_BYTES = 32 * 1024**2
DOWNSAMPLE = 32
_TEMPLATE_PARAMETERS = copy.deepcopy(template.DEFAULT_CONFIG)
_EVIDENCE_PARAMETERS = copy.deepcopy(evidence.EVIDENCE_CONFIG)
_AMPLITUDE_PARAMETERS = copy.deepcopy(amplitude.DEFAULT_CONFIG)
_SOURCES = {
    "joint_periodic.py": "a8f02710340a95cd52e32d076ccb1fcdbc738a9b33a1eed0bd6b86722bf53733",
    "channel_periodic.py": "0ad64238acc5fec41452ac23862fad3fc1bff9142fd6897ba0450279535ea1f5",
    "common.py": "9e82182ef02b69cb119aae48e75744f35e695f2a2500964f9211bddf5445c676",
    "model_consensus.py": "56e02b6f1c04b6f3ac61d32ec972ce14d1a1e303fb4b1c5520360ec719d11c91",
}


def frozen_config(schema=SCHEMA):
    contract = contract_for_schema(schema)
    return {"family": contract["method"], "candidate_id": contract["method"],
            "display_name": contract["display_name"], "port_version": 1,
            "parameters": {"template": copy.deepcopy(_TEMPLATE_PARAMETERS),
                           "evidence": copy.deepcopy(_EVIDENCE_PARAMETERS),
                           "amplitude": copy.deepcopy(_AMPLITUDE_PARAMETERS)},
            "research_source_sha256": dict(_SOURCES), "downsample": DOWNSAMPLE,
            "preparation": "actual mean of each 32x32 bin; short final bins excluded from fitting",
            "interpolation": "native sampled T with circular linear interpolation; P-spaced A with C1 convex interpolation and zero edge taper",
            "formula": "G = 1 + A(x,y) * fixed_T(phi); corrected = float32(raw / float64(G))",
            "baseline_policy": "intensity baselines are fitting nuisances only; no brightness normalization"}


def fit_prediction(raw32, names, native, *, check=lambda: None, progress=lambda _fraction: None):
    """Retain the original numerical order without unused full overview gains."""
    with cancellation(check):
        prior = template.fit_templates(raw32[None], list(names), native_shape_yx=native,
                                       downsample=DOWNSAMPLE,
                                       config=copy.deepcopy(_TEMPLATE_PARAMETERS))
        progress(.35)
        records = []
        for row, (name, source) in enumerate(zip(names, prior["channel_models"])):
            checkpoint()
            rec = dict(channel_index=row, channel_name=name, status="abstained", applied=False,
                       reason="no_frozen_supported_template", model=None,
                       template_status=source["status"], template_reason=source["reason"])
            records.append(rec)
            if source.get("model") is not None and source["status"] == "accepted":
                obs = evidence.prepare_channel(raw32[row], source["model"], native, DOWNSAMPLE)
                nodes, diagnostics = amplitude.fit_amplitude(obs, copy.deepcopy(_AMPLITUDE_PARAMETERS))
                nodes = np.asarray(nodes, float)
                if nodes.shape != obs["amplitude"].shape or not np.isfinite(nodes).all():
                    raise ValueError("Invalid joint amplitude field")
                if nodes.min() < -1e-10 or nodes.max() > obs["amplitude_cap"] + 1e-10:
                    raise ValueError("Joint amplitude violates generic physical gain bounds")
                if np.any(abs(nodes[~obs["valid"]]) > 1e-12):
                    raise ValueError("Joint amplitude fills invalid source support")
                nodes = np.clip(nodes, 0, obs["amplitude_cap"])
                frozen = source["model"]
                model = {key: copy.deepcopy(frozen[key]) for key in evidence.FROZEN_KEYS if key in frozen}
                model.update(joint_amplitude_nodes=nodes.tolist(), joint_phase_nodes=obs["phase_nodes"].tolist(),
                             joint_orthogonal_native_nodes=obs["orthogonal_native_nodes"].tolist(),
                             amplitude_cap=obs["amplitude_cap"], frozen_template_sha256=evidence.json_hash(
                                 {key: frozen[key] for key in evidence.FROZEN_KEYS if key in frozen}))
                rec.update(status="accepted", applied=name != "DAPI" and bool(np.any(nodes > 0)),
                           reason=None, model=model, method_diagnostics=diagnostics,
                           evidence_summary=dict(valid_fraction=float(obs["valid"].mean()),
                               mean_confidence=float(obs["weight"].mean()),
                               positive_observation_fraction=float((obs["amplitude"] > 0).mean()),
                               nonzero_node_fraction=float((nodes > 0).mean())))
                del obs, nodes
            progress(.35 + .6 * (row + 1) / len(names))
        checkpoint()
    return {"channel_models": records,
            "diagnostics": {"schema": SCHEMA, "uses_bf": False, "channel_names": list(names),
                "native_shape_yx": list(native), "downsample": DOWNSAMPLE,
                "input_shape_ncyx": [1, *raw32.shape], "method": "consensus",
                "config": frozen_config(), "DAPI_application": "exact_identity",
                "status": "accepted" if any(rec["applied"] for rec in records) else "abstained"}}


def _integer(value, label):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} must be an integer")
    try:
        return operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{label} must be an integer") from exc


def _finite(value):
    array = np.asarray(value)
    if array.dtype.kind not in "iuf" or not np.isfinite(array).all():
        raise ValueError("Nonfinite or non-real consensus model")
    return array


def _archive_budget(path, limit):
    position = path.tell() if hasattr(path, "read") else None
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if (len(entries) != len(FIELD_NAMES)
                    or {entry.filename for entry in entries} != {name + ".npy" for name in FIELD_NAMES}
                    or sum(entry.file_size for entry in entries) > limit):
                raise ValueError("Unsupported or oversized consensus model archive")
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid consensus model archive") from exc
    finally:
        if position is not None:
            path.seek(position)


class Model:
    SCHEMA = SCHEMA
    FIELD_NAMES = FIELD_NAMES

    def __init__(self, path, *, max_model_bytes=MAX_MODEL_BYTES):
        self.path = None if hasattr(path, "read") else Path(path)
        _archive_budget(path, max_model_bytes)
        self.data = np.load(path, allow_pickle=False)
        try:
            schema = self.data["model_schema"]
            if schema.shape != () or schema.dtype.kind != "U" or schema.item() not in SUPPORTED_SCHEMAS:
                raise ValueError("Unsupported consensus model schema")
            self.model_schema = schema.item()
            indices, enabled = self.data["channel_indices"], self.data["enabled"]
            names = self.data["channel_names"]
            if (indices.ndim != 1 or indices.dtype.kind not in "iu" or not 1 <= len(indices) <= 3
                    or np.any(indices < 0) or len(np.unique(indices)) != len(indices)
                    or enabled.shape != indices.shape or enabled.dtype.kind != "b"
                    or names.shape != indices.shape or names.dtype.kind != "U"):
                raise ValueError("Invalid consensus channel mapping")
            self.indices, self.enabled, self.channel_names = list(map(int, indices)), list(map(bool, enabled)), names.tolist()
            self.native_shape_yx = self.data["native_shape_yx"]
            factor = self.data["downsample"]
            if factor.shape != () or factor.dtype.kind not in "iu" or factor.item() != DOWNSAMPLE:
                raise ValueError("Invalid consensus mean-bin factor")
            self.downsample = int(factor.item())
            payload = self.data["prediction_json"]
            if payload.shape != () or payload.dtype.kind != "U":
                raise ValueError("Invalid consensus prediction record")
            self.prediction = json.loads(payload.item(), parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("Nonfinite consensus JSON value")))
            self.validate()
        except BaseException:
            self.data.close()
            raise

    def validate(self):
        native = self.native_shape_yx
        if native.shape != (2,) or native.dtype.kind not in "iu" or np.any(native < 1):
            raise ValueError("Invalid consensus native geometry")
        if (len(self.indices) != len(self.enabled) or len(self.indices) != len(self.channel_names)
                or not 1 <= len(self.indices) <= 3 or len(set(self.indices)) != len(self.indices)
                or any(_integer(index, "Channel index") < 0 for index in self.indices)
                or len(set(self.channel_names)) != len(self.channel_names)
                or set(self.channel_names) - {"FITC", "CY3", "DAPI"}
                or any(not isinstance(item, (bool, np.bool_)) for item in self.enabled)):
            raise ValueError("Invalid consensus channel mapping")
        prediction = self.prediction
        if not isinstance(prediction, dict) or set(prediction) != {"channel_models", "diagnostics"}:
            raise ValueError("Invalid consensus prediction")
        diag = prediction["diagnostics"]
        if (not isinstance(diag, dict) or diag.get("schema") != self.model_schema or diag.get("uses_bf") is not False
                or diag.get("method") != contract_for_schema(self.model_schema)["method"]
                or diag.get("config") != frozen_config(self.model_schema)
                or diag.get("DAPI_application") != "exact_identity"
                or diag.get("channel_names") != self.channel_names
                or diag.get("native_shape_yx") != native.tolist() or diag.get("downsample") != DOWNSAMPLE
                or diag.get("input_shape_ncyx") != [1, len(self.indices), *((int(n) + 31) // 32 for n in native)]):
            raise ValueError("Consensus geometry, method or provenance differs")
        records = prediction["channel_models"]
        if not isinstance(records, list) or len(records) != len(self.indices):
            raise ValueError("Invalid consensus channel records")
        bounds = []
        for row, (name, enabled, rec) in enumerate(zip(self.channel_names, self.enabled, records)):
            if (not isinstance(rec, dict) or rec.get("channel_index") != row or rec.get("channel_name") != name
                    or rec.get("status") not in ("accepted", "abstained") or type(rec.get("applied")) is not bool
                    or (enabled and not rec["applied"]) or (name == "DAPI" and (enabled or rec["applied"]))):
                raise ValueError("Invalid consensus application policy")
            model = rec.get("model")
            if model is None:
                if rec["applied"] or enabled or rec["status"] != "abstained":
                    raise ValueError("Consensus enabled channel lacks a model")
                bounds.append((1.0, 1.0))
                continue
            if rec["status"] != "accepted" or not isinstance(model, dict):
                raise ValueError("Invalid consensus accepted model")
            required = set(evidence.FROZEN_KEYS) | {"joint_amplitude_nodes", "joint_phase_nodes",
                "joint_orthogonal_native_nodes", "amplitude_cap", "frozen_template_sha256"}
            if set(model) != required or model["axis"] not in ("x", "y"):
                raise ValueError("Invalid consensus model fields or axis")
            frequency = _finite(model["frequency_cycles_per_native_pixel"])
            period = _finite(model["period_native_pixels"])
            if (frequency.shape != () or period.shape != () or not 12 * 32 <= period <= 128 * 32
                    or not np.isclose(frequency * period, 1., rtol=1e-12, atol=0)):
                raise ValueError("Invalid consensus period")
            phase = _finite(model["phase_grid"])
            wave = _finite(model["template_values"])
            if (phase.shape != (64,) or not np.array_equal(phase, np.arange(64) / 64)
                    or wave.shape != phase.shape or abs(float(wave.mean())) > 1e-10
                    or not np.isclose(np.mean(wave**2), 1, rtol=1e-10, atol=1e-10)):
                raise ValueError("Invalid consensus normalized sampled template")
            harmonics = model["supported_harmonics"]
            if (not isinstance(harmonics, list) or not harmonics or len(set(harmonics)) != len(harmonics)
                    or any(type(k) is not int or not 1 <= k <= 8 for k in harmonics)):
                raise ValueError("Invalid consensus harmonic support")
            frozen = {key: model[key] for key in evidence.FROZEN_KEYS}
            if evidence.json_hash(frozen) != model["frozen_template_sha256"]:
                raise ValueError("Consensus frozen template digest differs")
            axis = 1 if model["axis"] == "x" else 0
            coords = evidence.centers(int(native[axis]), 32)[:int(native[axis]) // 32]
            orth = evidence.centers(int(native[1-axis]), 32)[:int(native[1-axis]) // 32]
            if len(coords) < 8 or len(orth) < 8:
                raise ValueError("Invalid consensus complete-bin geometry")
            first = int(np.ceil(coords[0] * frequency))
            last = int(np.floor(coords[-1] * frequency - phase[-1]))
            pn = _finite(model["joint_phase_nodes"])
            on = _finite(model["joint_orthogonal_native_nodes"])
            nodes = _finite(model["joint_amplitude_nodes"])
            cap = _finite(model["amplitude_cap"])
            if (pn.ndim != 1 or len(pn) < 2 or not np.array_equal(pn, np.arange(first, last + 1) + .5)
                    or not np.array_equal(on, orth) or nodes.shape != (len(orth), len(pn))
                    or cap.shape != () or not np.isclose(cap, evidence._amplitude_limits(wave)[1], rtol=1e-12, atol=0)
                    or np.any(nodes < 0) or np.any(nodes > cap)):
                raise ValueError("Invalid consensus amplitude lattice or cap")
            maximum = float(nodes.max())
            lower, upper = 1 + maximum * float(wave.min()), 1 + maximum * float(wave.max())
            if lower < .5 - 1e-12 or upper > 2.0 + 1e-12:
                raise ValueError("Consensus gain lies outside frozen bounds")
            bounds.append((lower, upper) if enabled else (1.0, 1.0))
        self.gain_bounds = bounds
        # Preserve numeric views once; native tiles do not repeatedly parse lists.
        self._models = [None if rec.get("model") is None else {
            key: np.asarray(value, dtype=float) if key in ("phase_grid", "template_values",
                "joint_amplitude_nodes", "joint_phase_nodes", "joint_orthogonal_native_nodes") else value
            for key, value in rec["model"].items()} for rec in records]

    def gain(self, c, shape, origin):
        c = _integer(c, "Channel index")
        if len(shape) != 2 or len(origin) != 2:
            raise ValueError("Gain shape and origin require two integers")
        h, w = (_integer(value, "Gain shape") for value in shape)
        y, x = (_integer(value, "Gain origin") for value in origin)
        ny, nx = map(int, self.native_shape_yx)
        if c < 0 or min(h, w, y, x) < 0 or y + h > ny or x + w > nx:
            raise ValueError("Gain region lies outside the native frame")
        if c not in self.indices or not self.enabled[self.indices.index(c)]:
            return np.broadcast_to(np.ones((), dtype=np.float64), (h, w))
        model = self._models[self.indices.index(c)]
        return evidence.components_at(model, np.arange(y, y+h), np.arange(x, x+w))["G"]

    def correct(self, c, raw, y, x, support=None):
        raw = np.asarray(raw)
        if raw.ndim != 2 or raw.dtype.kind not in "iuf":
            raise ValueError("Correction expects a real two-dimensional native tile")
        out = np.asarray(raw / self.gain(c, raw.shape, (y, x)), dtype=np.float32)
        if support is not None:
            support = np.asarray(support)
            if support.shape != raw.shape or support.dtype.kind != "b":
                raise ValueError("Correction support must be a same-shape boolean mask")
            out = np.where(support, out, 0).astype(np.float32)
        return out
