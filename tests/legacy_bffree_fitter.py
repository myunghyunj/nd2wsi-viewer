"""Retired independent x/h1 fitter, kept for the compatibility tests only.

New fits go through nd2wsi.bffree_model.fit (consensus). This module still
writes `BF_free_periodic_x1/1` archives exactly as the shipped reader
(nd2wsi.bffree_model.LegacyModel) expects them, so tests can exercise the
reader against freshly fitted models; tests/fixtures/bffree_legacy_x_h1 holds
a frozen archive produced by this function before it left the package.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import bffree_periodic_legacy as periodic
import numpy as np

from nd2wsi.bffree_model import (
    DOWNSAMPLE,
    LEGACY_SCHEMA,
    LegacyModel,
    _input_contract,
    _workspace_plan,
    bin_centers,
)


def fit_legacy(raw32, metadata, output, *, check=lambda: None, progress=lambda _fraction: None):
    """Fit only target rows sorted by original index; abstentions stay identity.

    The caller supplies true 32x32 means (partial edge bins use their actual
    sample count), never a resized overview or any BF-derived support. Channel
    names/indices and native shape establish application coordinates only.
    """
    check()
    native, pairs = _input_contract(raw32, metadata)
    memory = _workspace_plan(raw32.shape)
    output = Path(output)
    if output.suffix.lower() != ".npz":
        raise ValueError("BF-free model output must have an .npz suffix")
    gains = np.ones((len(pairs), raw32.shape[-1]), dtype=np.float64)
    enabled, selected = [], []
    progress(0.0)
    for row, (index, name) in enumerate(pairs):
        check()
        try:
            gain, evidence = periodic.fit_channel(raw32[row], check=check)
            gains[row] = gain
            applied, reason = True, None
        except periodic.Abstain as exc:
            applied, reason = False, exc.code
            evidence = {"axis": "x", "status": "abstained", "reason": reason}
        enabled.append(applied)
        selected.append({"channel": name, "channel_index": index, "input_row": row,
                         "model": "periodic_independent_x_h1" if applied else None,
                         "outcome": "fitted" if applied else "unchanged", "reason": reason,
                         "enabled": applied, "axes": [evidence],
                         "gain_min": float(gains[row].min()), "gain_max": float(gains[row].max())})
        progress(.95 * (row + 1) / len(pairs))
    config = {"family": "periodic_independent", "candidate_id": "periodic_independent_x_h1",
              "parameters": periodic.frozen_parameters(), "downsample": DOWNSAMPLE,
              "preparation": "actual mean of each 32x32 bin, including partial final bins",
              "interpolation": "linear in native x on actual mean-bin centres; nearest endpoints"}
    result = {"schema": LEGACY_SCHEMA, "experimental": True, "production_eligible": False,
              "uses_bf": False, "BF_modified": False,
              "DAPI_modified": any(row["channel"] == "DAPI" and row["enabled"] for row in selected),
              "temporal_normalization": False, "config": config, "selected": selected,
              "target_channels": [name for _, name in pairs],
              "supported_channel_axis_count": sum(enabled), "working_memory": memory,
              "native_shape_yx": list(native), "input_shape_cyx": list(raw32.shape),
              "abstention_policy": "independent target channels; no supported channel produces a valid disabled identity model",
              "limitation": "Within-acquisition periodic consistency cannot distinguish illumination from repeating specimen structures; no scientific admission."}
    payload = json.dumps(result, indent=2, allow_nan=False) + "\n"
    check()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bffree-model-", dir=output.parent) as folder:
        staged = Path(folder) / "model.npz"
        np.savez_compressed(staged, model_schema=np.asarray(LEGACY_SCHEMA),
                            channel_indices=np.asarray([index for index, _ in pairs], dtype=np.int64),
                            enabled=np.asarray(enabled, dtype=bool), gain_x=gains,
                            x_bin_centers=bin_centers(native[1]),
                            native_shape_yx=np.asarray(native, dtype=np.int64),
                            downsample=np.asarray(DOWNSAMPLE, dtype=np.int64))
        saved = LegacyModel(staged)
        saved.data.close()
        sidecar = staged.with_suffix(".json")
        sidecar.write_text(payload, encoding="utf-8")
        check()
        os.replace(staged, output)
        os.replace(sidecar, output.with_suffix(".json"))
    progress(1.0)
    return LegacyModel(output), result
