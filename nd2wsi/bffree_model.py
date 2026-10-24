"""Consensus fitting with read compatibility for the prior x/h1 model.

Only FITC/CY3/DAPI mean-bin arrays enter estimation; DAPI and BF application
remain identity. The legacy x/h1 fitter lives in tests/legacy_bffree_fitter.py;
only its reader (LegacyModel) ships.
"""
from __future__ import annotations

import json
import operator
import os
import tempfile
import zipfile
from pathlib import Path

import numpy as np

from . import bffree_consensus as consensus

LEGACY_SCHEMA = "BF_free_periodic_x1/1"
LEGACY_FIELD_NAMES = frozenset(("model_schema", "channel_indices", "enabled", "gain_x",
                         "x_bin_centers", "native_shape_yx", "downsample"))
DOWNSAMPLE = 32
MAX_WORKING_BYTES = 1024**3
MAX_MODEL_BYTES = 32 * 1024**2
_TARGETS = frozenset(("FITC", "CY3", "DAPI"))
_METADATA_FIELDS = frozenset(("channel_mapping", "channel_names", "sizes"))


class Unsupported(RuntimeError):
    """The input cannot be fitted within the fixed preview resource contract."""


def _integer(value, label):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} must be an integer")
    try:
        return operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{label} must be an integer") from exc


def _pair(value, label):
    if not isinstance(value, (tuple, list, np.ndarray)) or len(value) != 2:
        raise ValueError(f"{label} must contain two integers")
    return tuple(_integer(item, label) for item in value)


def bin_centers(length):
    """Centres of actual 32-pixel means, including a short final bin."""
    length = _integer(length, "Native length")
    if length < 1:
        raise ValueError("Native length must be positive")
    starts = np.arange(0, length, DOWNSAMPLE, dtype=np.int64)
    return starts + (np.minimum(DOWNSAMPLE, length - starts) - 1) / 2.0


def _archive_budget(path):
    position = path.tell() if hasattr(path, "read") else None
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (set(names) != {name + ".npy" for name in LEGACY_FIELD_NAMES}
                    or len(names) != len(LEGACY_FIELD_NAMES)
                    or sum(entry.file_size for entry in entries) > MAX_MODEL_BYTES):
                raise ValueError("Unsupported or oversized BF-free model archive")
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid BF-free model archive") from exc
    finally:
        if position is not None:
            path.seek(position)


class LegacyModel:
    """Native-pixel model API compatible with the existing H model consumer."""

    SCHEMA = LEGACY_SCHEMA
    FIELD_NAMES = LEGACY_FIELD_NAMES

    def __init__(self, path):
        self.path = None if hasattr(path, "read") else Path(path)
        _archive_budget(path)
        self.data = np.load(path, allow_pickle=False)
        try:
            if set(self.data.files) != LEGACY_FIELD_NAMES or len(self.data.files) != len(LEGACY_FIELD_NAMES):
                raise ValueError("Unsupported BF-free correction model fields")
            schema = self.data["model_schema"]
            if schema.shape != () or schema.dtype.kind != "U" or schema.item() != LEGACY_SCHEMA:
                raise ValueError("Unsupported BF-free correction model schema")
            self.model_schema = schema.item()
            indices, enabled = self.data["channel_indices"], self.data["enabled"]
            if (indices.ndim != 1 or indices.dtype.kind not in "iu"
                    or not 1 <= len(indices) <= 3 or np.any(indices < 0)
                    or len(np.unique(indices)) != len(indices)
                    or enabled.shape != indices.shape or enabled.dtype.kind != "b"):
                raise ValueError("Invalid BF-free correction channel mapping")
            self.indices = list(map(int, indices))
            self.enabled = list(map(bool, enabled))
            self.gain_x = self.data["gain_x"]
            self.x_bin_centers = self.data["x_bin_centers"]
            self.native_shape_yx = self.data["native_shape_yx"]
            factor = self.data["downsample"]
            if factor.shape != () or factor.dtype.kind not in "iu" or factor.item() != DOWNSAMPLE:
                raise ValueError("Invalid BF-free mean-bin factor")
            self.downsample = int(factor.item())
            self.validate()
        except BaseException:
            self.data.close()
            raise

    def validate(self):
        if (len(self.indices) != len(self.enabled) or not 1 <= len(self.indices) <= 3
                or len(set(self.indices)) != len(self.indices)
                or any(_integer(c, "Channel index") < 0 for c in self.indices)
                or any(not isinstance(value, (bool, np.bool_)) for value in self.enabled)):
            raise ValueError("Invalid BF-free correction channel mapping")
        native = self.native_shape_yx
        if native.shape != (2,) or native.dtype.kind not in "iu" or np.any(native <= 0):
            raise ValueError("Invalid BF-free native geometry")
        count = (int(native[1]) + DOWNSAMPLE - 1) // DOWNSAMPLE
        if (self.gain_x.shape != (len(self.indices), count)
                or self.x_bin_centers.shape != (count,)):
            raise ValueError("Invalid BF-free mean-bin geometry")
        for array in (self.gain_x, self.x_bin_centers):
            if array.dtype.kind not in "iuf" or not np.isfinite(array).all():
                raise ValueError("Nonfinite or non-real BF-free model")
        if not np.array_equal(self.x_bin_centers, bin_centers(int(native[1]))):
            raise ValueError("BF-free centres do not match actual native mean bins")
        if np.any(self.gain_x < .5) or np.any(self.gain_x > 2.0):
            raise ValueError("Invalid BF-free gain bounds")
        self.gain_bounds = [(float(np.min(row)), float(np.max(row))) if enabled else (1.0, 1.0)
                            for row, enabled in zip(self.gain_x, self.enabled)]

    def gain(self, c, shape, origin):
        c = _integer(c, "Channel index")
        h, w = _pair(shape, "Gain shape")
        y, x = _pair(origin, "Gain origin")
        native_h, native_w = map(int, self.native_shape_yx)
        if c < 0 or h < 0 or w < 0 or not (0 <= y <= y + h <= native_h and 0 <= x <= x + w <= native_w):
            raise ValueError("Gain region lies outside the native frame")
        if c not in self.indices or not self.enabled[self.indices.index(c)]:
            line = np.ones(w, dtype=np.float32)
        else:
            # Piecewise linear native-domain interpolation is equivalent to
            # benchmark metrics.native_gain on the y-invariant stored field.
            line = np.interp(x + np.arange(w, dtype=np.float64), self.x_bin_centers,
                             self.gain_x[self.indices.index(c)]).astype(np.float32)
        # A read-only broadcast avoids allocating repeated rows of this x-only
        # field. Correction allocates its ordinary output tile, as H does.
        return np.broadcast_to(line, (h, w))

    def correct(self, c, raw, y, x, support=None):
        raw = np.asarray(raw)
        if raw.ndim != 2 or raw.dtype.kind not in "iuf":
            raise ValueError("Correction expects a real two-dimensional native tile")
        out = np.asarray(raw, np.float32) / self.gain(c, raw.shape, (y, x))
        if support is not None:
            support = np.asarray(support)
            if support.shape != raw.shape or support.dtype.kind != "b":
                raise ValueError("Correction support must be a same-shape boolean mask")
            out = np.where(support, out, 0).astype(np.float32)
        return out


def _input_contract(raw32, metadata):
    # Admission precedes casts, reductions or reading any array element.
    if not isinstance(raw32, np.ndarray) or raw32.ndim != 3 or raw32.dtype.kind not in "iuf":
        raise ValueError("raw32 must be a real target-only C,Y,X NumPy array")
    if not 1 <= raw32.shape[0] <= 3 or min(raw32.shape[-2:]) < 8:
        raise ValueError("raw32 requires 1-3 target channels and Y,X >= 8")
    if not isinstance(metadata, dict) or set(metadata) != _METADATA_FIELDS:
        raise ValueError("BF-free metadata accepts only channel_mapping, channel_names and sizes")
    mapping, names, sizes = (metadata[key] for key in ("channel_mapping", "channel_names", "sizes"))
    if (not isinstance(mapping, dict) or not mapping or set(mapping) - _TARGETS
            or len(mapping) != raw32.shape[0]):
        raise ValueError("BF-free channel_mapping accepts only the supplied FITC/CY3/DAPI targets")
    if not isinstance(names, (list, tuple)) or not names or not all(isinstance(name, str) for name in names):
        raise ValueError("Original channel_names are required")
    if not isinstance(sizes, dict) or not {"Y", "X"} <= set(sizes):
        raise ValueError("Native Y/X sizes are required")
    # Other source dimensions are not model inputs; reject geometry/priors
    # nested under sizes rather than accidentally accepting a second contract.
    if set(sizes) - {"Y", "X"}:
        raise ValueError("BF-free sizes accepts native Y/X only")
    native = tuple(_integer(sizes[axis], "Native size") for axis in ("Y", "X"))
    if min(native) < 1 or tuple((n + DOWNSAMPLE - 1) // DOWNSAMPLE for n in native) != raw32.shape[-2:]:
        raise ValueError("raw32 shape differs from actual 32-pixel native mean bins")
    pairs = []
    for name, index in mapping.items():
        index = _integer(index, "Original channel index")
        if not 0 <= index < len(names) or names[index].strip().upper() != name:
            raise ValueError("Target channel mapping disagrees with original channel_names")
        pairs.append((index, name))
    if len({index for index, _ in pairs}) != len(pairs):
        raise ValueError("Target channel indices must be unique")
    pairs.sort()
    return native, pairs


def _workspace_plan(shape):
    channels, h, w = map(int, shape)
    # Conservative live-buffer allowance: per-channel float64 conversion,
    # validity/normalization/nanmedian copies and scratch, then profile FFT and
    # robust least-squares buffers. Compact gains retained across channels and
    # model serialization/validation are included. Caller-owned input maps,
    # decoder memory, interpreter and allocator overhead are excluded.
    reserve = 32 * 1024**2
    estimate = reserve + h * w * 128 + w * (1024 + 16 * channels)
    if estimate > MAX_WORKING_BYTES:
        raise Unsupported("working_memory_budget_exceeded: BF-free fitting workspace exceeds 1 GiB")
    return {"scope": "additional_fitting_workspace_excluding_inputs_decoder_and_process_overhead",
            "limit_bytes": MAX_WORKING_BYTES, "estimated_peak_bytes": estimate,
            "channel_order": "one target channel at a time", "input_maps_excluded": True}


# Public route: new fits use consensus; archived x/h1 gains remain readable.
SCHEMA = consensus.SCHEMA
SUPPORTED_SCHEMAS = consensus.SUPPORTED_SCHEMAS
FIELD_NAMES = consensus.FIELD_NAMES


class Model:
    SCHEMA = SCHEMA
    FIELD_NAMES = FIELD_NAMES

    def __new__(cls, path):
        position = path.tell() if hasattr(path, "read") else None
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                names = {entry.filename for entry in entries}
                if (len(entries) != len(names)
                        or sum(entry.file_size for entry in entries) > MAX_MODEL_BYTES):
                    raise ValueError("Unsupported or oversized BF-free model archive")
        except zipfile.BadZipFile as exc:
            raise ValueError("Invalid BF-free model archive") from exc
        finally:
            if position is not None:
                path.seek(position)
        if names == {name + ".npy" for name in LEGACY_FIELD_NAMES}:
            return LegacyModel(path)
        if names == {name + ".npy" for name in FIELD_NAMES}:
            return consensus.Model(path, max_model_bytes=MAX_MODEL_BYTES)
        raise ValueError("Unsupported BF-free model archive")


def fit(raw32, metadata, output, *, check=lambda: None, progress=lambda _fraction: None):
    """Fit the frozen fluorescence-only consensus procedure.

    `raw32` contains actual 32x32 means sorted by original channel index.
    Source/selection fingerprints remain the caller's transaction boundary;
    metadata cannot carry BF-derived support, geometry or acquisition priors.
    """
    check()
    native, pairs = _input_contract(raw32, metadata)
    memory = _workspace_plan(raw32.shape)
    # Original template admission evaluates a bounded 1-D native axis. The
    # additional reserve also covers compact JSON/lattice serialization.
    memory["estimated_peak_bytes"] += max(native) * 64 + raw32.shape[-2] * 1024
    memory["channel_order"] = "all fluorescence profiles, then one target amplitude lattice at a time"
    if memory["estimated_peak_bytes"] > MAX_WORKING_BYTES:
        raise Unsupported("working_memory_budget_exceeded: consensus fitting workspace exceeds 1 GiB")
    output = Path(output)
    if output.suffix.lower() != ".npz":
        raise ValueError("BF-free model output must have an .npz suffix")
    progress(0.)
    names = [name for _, name in pairs]
    prediction = consensus.fit_prediction(raw32, names, native, check=check, progress=progress)
    selected, enabled = [], []
    for row, ((index, name), rec) in enumerate(zip(pairs, prediction["channel_models"])):
        applied = bool(rec["applied"])
        enabled.append(applied)
        field = rec["model"]
        if applied:
            amplitude_max = float(np.max(field["joint_amplitude_nodes"]))
            bounds = [1 + amplitude_max * float(min(field["template_values"])),
                      1 + amplitude_max * float(max(field["template_values"]))]
        else:
            bounds = [1., 1.]
        selected.append({"channel": name, "channel_index": index, "input_row": row,
            "model": "consensus" if applied else None,
            "outcome": "fitted" if applied else "unchanged", "enabled": applied,
            "reason": "DAPI_identity_policy" if name == "DAPI" else (rec["template_reason"] or rec["reason"]),
            "axes": [] if field is None else [{"axis": field["axis"], "status": rec["status"],
                "period_native_pixels": field["period_native_pixels"],
                "frequency_cycles_per_native_pixel": field["frequency_cycles_per_native_pixel"],
                "supported_harmonics": field["supported_harmonics"]}],
            "gain_min": bounds[0], "gain_max": bounds[1]})
    result = {"schema": SCHEMA, "experimental": False, "production_eligible": True,
        "uses_bf": False, "BF_modified": False, "DAPI_modified": False,
        "temporal_normalization": False, "config": consensus.frozen_config(), "selected": selected,
        "target_channels": names, "supported_channel_axis_count": sum(enabled),
        "working_memory": memory, "native_shape_yx": list(native), "input_shape_cyx": list(raw32.shape),
        "abstention_policy": "Own-channel template acceptance; unsupported channels and DAPI stay identity",
        "limitation": "Within-acquisition periodic consistency cannot distinguish illumination from repeating specimen structures; biological preservation is not established by this fit."}
    prediction_text = json.dumps(prediction, separators=(",", ":"), allow_nan=False)
    payload = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if len(prediction_text) * 4 + 4096 > MAX_MODEL_BYTES:
        raise Unsupported("model_storage_budget_exceeded: consensus model exceeds 32 MiB")
    check()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bffree-consensus-model-", dir=output.parent) as folder:
        staged = Path(folder) / "model.npz"
        np.savez_compressed(staged, model_schema=np.asarray(SCHEMA),
            channel_indices=np.asarray([index for index, _ in pairs], dtype=np.int64),
            channel_names=np.asarray(names), enabled=np.asarray(enabled, dtype=bool),
            native_shape_yx=np.asarray(native, dtype=np.int64),
            downsample=np.asarray(DOWNSAMPLE, dtype=np.int64), prediction_json=np.asarray(prediction_text))
        saved = Model(staged)
        saved.data.close()
        sidecar = staged.with_suffix(".json")
        sidecar.write_text(payload, encoding="utf-8")
        check()
        os.replace(staged, output)
        os.replace(sidecar, output.with_suffix(".json"))
    progress(1.)
    return Model(output), result
