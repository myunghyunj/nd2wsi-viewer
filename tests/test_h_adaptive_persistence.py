"""Schema and whole-domain safety, plus private three-channel cache round trips.

These synthetic fixtures test engineering behavior; they do not establish BF
correction quality or a real ND2 decoder's memory/time requirements.
"""

import copy
import io
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from test_h_persistence import _mean2, _write_group
from test_h_persistence import original as _legacy_original

from nd2wsi import h_persistence as hp
from nd2wsi.cache import read_manifest
from nd2wsi.convert import open_store
from nd2wsi.direct import open_nd2_backed
from nd2wsi.h_model import Model, Unsupported
from nd2wsi.storage.single_file import SQLiteStore

# Reuse the established private ND2/cache fixture with its original name.
original = _legacy_original


def _fields(*, adaptive=True):
    yy, xx = np.indices((8, 8))
    pattern = 0.15 * np.sin(xx * np.pi / 4) + 0.07 * np.cos(yy * np.pi / 4)
    fields = dict(
        basis=np.diag([0.6, 0.7]), oldbasis=np.diag([0.6, 0.7]),
        templates=np.stack([pattern, 0.7 * pattern, 0.5 * pattern]),
        oldtemplates=np.stack([pattern] * 3), delta=np.zeros((4, 4)),
        local=np.zeros(3, dtype=bool), old=np.zeros(3, dtype=bool),
        channel_indices=np.array([1, 2, 0]), enabled=np.ones(3, dtype=bool),
    )
    if adaptive:
        fields.update(
            model_schema=np.asarray(Model.ADAPTIVE_SCHEMA),
            amplitude_nodes=np.ones((3, 6, 6)),
            envelope_cell_origin=np.array([0, 0]),
        )
    return fields


def _payload(fields):
    stream = io.BytesIO()
    np.savez_compressed(stream, **fields)
    return stream.getvalue()


@pytest.fixture
def adaptive_model(tmp_path):
    path = tmp_path / "adaptive.npz"
    path.write_bytes(_payload(_fields()))
    model = Model(path)
    yield model
    model.data.close()


@pytest.mark.parametrize("change, message", [
    (lambda f: f.update(model_schema=np.asarray("unknown/1")), "schema"),
    (lambda f: f.update(model_schema=np.asarray(Model.LEGACY_SCHEMA)), "schema"),
    (lambda f: f.update(model_schema=np.array([Model.ADAPTIVE_SCHEMA])), "schema"),
    (lambda f: f.pop("amplitude_nodes"), "fields"),
    (lambda f: f.update(envelope_cell_origin=np.array([0, 0, 0])), "envelope geometry"),
    (lambda f: f.update(amplitude_nodes=np.ones((3, 1, 6))), "envelope geometry"),
    (lambda f: f["amplitude_nodes"].__setitem__((2, 5, 5), -0.01), "negative"),
    (lambda f: f["amplitude_nodes"].__setitem__((2, 5, 5), np.nan), "Nonfinite"),
    (lambda f: f.update(channel_indices=np.array([1., 2., 0.])), "channel mapping"),
    (lambda f: f.update(channel_indices=np.array([1, 1, 0])), "channel mapping"),
    (lambda f: f.update(enabled=np.array([True, True])), "channel mapping"),
    (lambda f: f["old"].__setitem__(0, True), "legacy template"),
])
def test_model_rejects_unknown_schema_and_malformed_fields(change, message):
    fields = _fields()
    change(fields)
    with pytest.raises(ValueError, match=message):
        Model(io.BytesIO(_payload(fields)))


@pytest.mark.parametrize("change", [
    lambda f: f["templates"].__setitem__((0, slice(None), slice(None)), 2.0),
    lambda f: f["templates"].__setitem__((0, slice(None), slice(None)), -0.8),
    # This node lies beyond the small native fixture's first tile. Loading must
    # still refuse it without relying on the image regions that happen to open.
    lambda f: f["amplitude_nodes"].__setitem__((0, 5, 5), 20.0),
])
def test_persisted_loader_rejects_invalid_gain_even_with_matching_hash(
        original, adaptive_model, change):
    prepared = hp.prepare_apply(original, adaptive_model, {}, threading.Event(), lambda _: None)
    try:
        fields = _fields()
        change(fields)
        payload = _payload(fields)
        manifest = copy.deepcopy(prepared.manifest)
        manifest["h_correction"]["model_sha256"] = hp._sha(payload)
        with SQLiteStore(prepared.staging) as store:
            store.write_bytes(hp.MODEL_KEY, payload)
        with pytest.raises(ValueError, match="gain bounds"):
            hp.load_model(prepared.staging, manifest)
    finally:
        prepared.abort()


def test_persisted_loader_rejects_unknown_schema_even_with_matching_hash(original, adaptive_model):
    prepared = hp.prepare_apply(original, adaptive_model, {}, threading.Event(), lambda _: None)
    try:
        fields = _fields()
        fields["model_schema"] = np.asarray("unreviewed_future_model/2")
        payload = _payload(fields)
        manifest = copy.deepcopy(prepared.manifest)
        manifest["h_correction"]["model_sha256"] = hp._sha(payload)
        with SQLiteStore(prepared.staging) as store:
            store.write_bytes(hp.MODEL_KEY, payload)
        with pytest.raises(ValueError, match="schema"):
            hp.load_model(prepared.staging, manifest)
    finally:
        prepared.abort()


def test_adaptive_boundaries_and_legacy_gain_contract_remain_distinct():
    fields = _fields()
    fields["templates"][0] = -0.75
    fields["templates"][1] = 0.75
    model = Model(io.BytesIO(_payload(fields)))
    try:
        assert model.gain_bounds[:2] == [(0.25, 1.0), (1.0, 1.75)]
        assert model.gain(1, (12, 20), (32, 32)).min() == 0.25
        assert model.gain(2, (12, 20), (32, 32)).max() == 1.75
        np.testing.assert_array_equal(model.gain(1, (12, 20), (-1000, -1000)), 1.0)
        # Runtime protection also covers accidental in-process changes.
        model.templates[0] = 2.0
        with pytest.raises(Unsupported, match="invalid_fitted_gain"):
            model.gain(1, (12, 20), (32, 32))
    finally:
        model.data.close()
    fields = _fields(adaptive=False)
    fields["templates"][0] = 2.0
    legacy = Model(io.BytesIO(_payload(fields)))
    try:
        assert legacy.model_schema == Model.LEGACY_SCHEMA
        np.testing.assert_array_equal(legacy.gain(1, (12, 20), (32, 32)), 3.0)
    finally:
        legacy.data.close()


def test_three_channels_apply_reopen_revoke_preserves_raw_and_dapi(
        original, adaptive_model, monkeypatch):
    source_bytes = original.source_path.read_bytes()
    old_cache = original.container_path.read_bytes()
    prepared = hp.prepare_apply(original, adaptive_model, {}, threading.Event(), lambda _: None)
    assert original.container_path.read_bytes() == old_cache
    prepared.publish()
    prepared.close()
    manifest = read_manifest(original.container_path)
    assert manifest["h_correction"]["channel_indices"] == [1, 2, 0]
    stored, _ = open_store(original.container_path)
    try:
        for ci in (1, 2, 0):
            expected = adaptive_model.correct(ci, original.root["0"][ci], 0, 0)
            for key in ("1", "2", "3"):
                expected = _mean2(expected)
                np.testing.assert_array_equal(stored[key][ci], expected)
        for key in ("1", "2", "3"):
            np.testing.assert_array_equal(stored[key][3], original.root[key][3])
    finally:
        stored.close()
    reader = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("nd2.ND2File", lambda _: reader)
    monkeypatch.setattr("nd2wsi.reader.is_source_backable", lambda *_: (True, ""))
    monkeypatch.setattr("nd2wsi.reader.plane_view", lambda *_: (original.root["0"], None))
    reopened, _ = open_nd2_backed(original.source_path, original.container_path)
    try:
        np.testing.assert_array_equal(reopened.raw_level0[:], original.root["0"])
        for ci in (1, 2, 0):
            expected = adaptive_model.correct(ci, original.root["0"][ci, 3:17, 7:29], 3, 7)
            np.testing.assert_array_equal(reopened["0"][ci, 3:17, 7:29], expected)
        np.testing.assert_array_equal(reopened["0"][3], original.root["0"][3])
    finally:
        reopened.close(delay=0)
    original.manifest = manifest

    def raw_converter(source, target, **kwargs):
        assert source == original.source_path
        _write_group(target, original.root, copy.deepcopy(original.attrs))
        kwargs["on_progress"](1.0)

    monkeypatch.setattr("nd2wsi.convert.convert", raw_converter)
    prepared = hp.prepare_revoke(original, threading.Event(), lambda _: None)
    prepared.publish()
    prepared.close()
    assert "h_correction" not in read_manifest(original.container_path)
    restored, _ = open_store(original.container_path)
    try:
        for key, raw in original.root.items():
            np.testing.assert_array_equal(restored[key][:], raw)
    finally:
        restored.close()
    assert original.source_path.read_bytes() == source_bytes
