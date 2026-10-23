"""The archived x/h1 model stays readable and exact without any fitter present.

Every array here comes from tests/fixtures/bffree_legacy_x_h1 (see its
README); nothing in this file fits a model.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from nd2wsi import bffree_model as bm

FIXTURE = Path(__file__).parent / "fixtures" / "bffree_legacy_x_h1"
# Frozen query behind expected_gain_native.npy: original channels CY3, BF,
# DAPI, FITC over the first 2 native rows x 64 native columns.
QUERY_CHANNELS = (0, 1, 2, 3)
QUERY_SHAPE = (2, 64)
QUERY_ORIGIN = (0, 0)


@pytest.fixture(scope="module")
def sidecar():
    return json.loads((FIXTURE / "model.json").read_text(encoding="utf-8"))


@pytest.fixture
def frozen():
    model = bm.Model(FIXTURE / "model.npz")
    try:
        yield model
    finally:
        model.data.close()


def test_frozen_archive_opens_as_legacy_model(frozen, sidecar):
    assert isinstance(frozen, bm.LegacyModel)
    assert frozen.model_schema == bm.LEGACY_SCHEMA == sidecar["schema"]
    assert set(frozen.data.files) == bm.LEGACY_FIELD_NAMES
    selected = sidecar["selected"]
    assert frozen.indices == [row["channel_index"] for row in selected] == [0, 2, 3]
    assert frozen.enabled == [row["enabled"] for row in selected] == [True, False, True]
    assert selected[1]["channel"] == "DAPI" and selected[1]["reason"] == "weak_periodic_evidence"
    assert list(map(int, frozen.native_shape_yx)) == sidecar["native_shape_yx"]
    channels, _rows, columns = sidecar["input_shape_cyx"]
    assert frozen.gain_x.shape == (channels, columns)
    assert columns == (sidecar["native_shape_yx"][1] + bm.DOWNSAMPLE - 1) // bm.DOWNSAMPLE
    assert frozen.x_bin_centers.shape == (columns,)
    assert frozen.downsample == sidecar["config"]["downsample"] == bm.DOWNSAMPLE
    assert frozen.gain_bounds == [(row["gain_min"], row["gain_max"]) for row in selected]
    np.testing.assert_array_equal(frozen.gain_x[1], 1.0)


def test_frozen_input_matches_the_archive_geometry(sidecar):
    raw = np.load(FIXTURE / "input_raw32.npy")
    assert raw.dtype == np.float32 and list(raw.shape) == sidecar["input_shape_cyx"]
    assert sidecar["native_shape_yx"] == [n * bm.DOWNSAMPLE for n in raw.shape[-2:]]


def test_frozen_native_gain_is_reproduced_exactly(frozen):
    expected = np.load(FIXTURE / "expected_gain_native.npy")
    assert expected.shape == (len(QUERY_CHANNELS), *QUERY_SHAPE) and expected.dtype == np.float32
    actual = np.stack([frozen.gain(c, QUERY_SHAPE, QUERY_ORIGIN) for c in QUERY_CHANNELS])
    # gain() is np.interp on the stored bins plus a float32 cast: no reduction,
    # no BLAS, so the frozen field must come back bit for bit.
    np.testing.assert_array_equal(actual, expected)
    # BF (1) and the abstained DAPI (2) stay identity; fitted rows do not.
    np.testing.assert_array_equal(expected[1], 1.0)
    np.testing.assert_array_equal(expected[2], 1.0)
    assert not np.all(expected[0] == 1.0) and not np.all(expected[3] == 1.0)
    tile = (500 + np.arange(np.prod(QUERY_SHAPE), dtype=np.uint16)).reshape(QUERY_SHAPE)
    for c, field in zip(QUERY_CHANNELS, expected):
        np.testing.assert_array_equal(frozen.correct(c, tile, *QUERY_ORIGIN),
                                      tile.astype(np.float32) / field)
