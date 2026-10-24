"""One-off generator for the frozen legacy BF-free x/h1 fixture in this folder.

Tests never run this file: the committed .npz/.npy/.json files are the frozen
contract that the shipped reader (nd2wsi.bffree_model.LegacyModel) must keep
honouring. Re-running it would silently redefine that contract. See README.md.

    PYTHONPATH=. python tests/fixtures/bffree_legacy_x_h1/generate.py [OUT_DIR]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tests/ helpers

from legacy_bffree_fitter import fit_legacy  # noqa: E402
from test_bffree_model import metadata, synthetic_target_data  # noqa: E402

SEED = 20261024
ROWS = 16  # first 16 of the generator's 48 mean-bin rows keeps the fixture small
NOISE_SIGMA = 1.0  # counts, on ~1000-count means
# Native gain query frozen in expected_gain_native.npy: original channels
# CY3, BF, DAPI, FITC, the first 2 rows x 64 columns of the native frame.
QUERY_CHANNELS = (0, 1, 2, 3)
QUERY_SHAPE = (2, 64)
QUERY_ORIGIN = (0, 0)


def fixture_input(seed=SEED):
    raw = synthetic_target_data()[:, :ROWS, :].copy()
    # Row 1 is DAPI (original index 2): a flat frame carries no periodic
    # evidence, so the fitter abstains and the row stays identity.
    raw[1] = 1000.0
    rng = np.random.default_rng(seed)
    raw += rng.normal(0.0, NOISE_SIGMA, raw.shape).astype(np.float32)
    return raw


def main(argv):
    out = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    raw = fixture_input()
    model, result = fit_legacy(raw, metadata(raw), out / "model.npz")
    try:
        gains = np.stack([model.gain(c, QUERY_SHAPE, QUERY_ORIGIN) for c in QUERY_CHANNELS])
    finally:
        model.data.close()
    np.save(out / "input_raw32.npy", raw)
    np.save(out / "expected_gain_native.npy", np.ascontiguousarray(gains))
    print("enabled:", [row["enabled"] for row in result["selected"]],
          "reasons:", [row["reason"] for row in result["selected"]])
    print("gain_x shape:", result["input_shape_cyx"], "expected:", gains.shape, gains.dtype)


if __name__ == "__main__":
    main(sys.argv)
