"""Persistent H replacement uses only small private, synthetic ND2/cache fixtures."""

import copy
import json
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import h_persistence as hp
from nd2wsi.cache import CacheLock, quick_fingerprint, read_manifest, write_manifest
from nd2wsi.convert import open_store
from nd2wsi.direct import _Lifecycle, _PersistedHLevel0, open_nd2_backed
from nd2wsi.h_model import Model
from nd2wsi.migration import pack_directory
from nd2wsi.storage import DEFAULT_STORAGE
from nd2wsi.storage.single_file import SQLiteStore, read_entry


def _write_group(path, values, attrs, tile=16):
    group = DEFAULT_STORAGE.create_group(path)
    for key, value in values.items():
        arr = DEFAULT_STORAGE.create_array(group, key, value.shape, value.dtype, tile)
        arr[:] = value
    group.attrs.update(attrs)


@pytest.fixture
def original(tmp_path):
    rng = np.random.default_rng(43)
    native = rng.integers(0, 8000, (4, 67, 73), dtype=np.uint16)
    return _original_state(tmp_path, native)


def _original_state(tmp_path, native, tile=16):
    source = tmp_path / "original.nd2"
    source.write_bytes(b"private synthetic source that must remain byte-identical")
    channels, height, width = native.shape
    values = {"0": native}
    levels = [{"path": "0", "height": height, "width": width, "downsample": 1}]
    for k in range(1, 4):
        prior = values[str(k - 1)]
        h, w = prior.shape[1] // 2, prior.shape[2] // 2
        values[str(k)] = np.rint(prior[:, :h * 2, :w * 2].reshape(channels, h, 2, w, 2).mean(axis=(2, 4))).astype(np.uint16)
        levels.append({"path": str(k), "height": h, "width": w, "downsample": 2**k})
    attrs = {
        "multiscales": [{"datasets": [{"path": lv["path"], "coordinateTransformations": [{"type": "scale", "scale": [1, lv["downsample"], lv["downsample"]]}]} for lv in levels]}],
        "omero": {"channels": [{"label": name, "window": {"start": 0, "end": 8191}} for name in ("BF", "FITC", "CY3", "DAPI")]},
        "nd2wsi": {"levels": levels, "rgb": False, "kind": "full", "tile": tile, "dtype": "uint16", "selection": {"t": 0, "p": 0, "z": 0}},
    }
    directory = tmp_path / "initial.nd2wsi-cache"
    _write_group(directory / "store.ome.zarr", values, attrs, tile)
    write_manifest(directory, source, quick_fingerprint(source), {"t": 0, "p": 0, "z": "mid"}, 0, tile, native.shape, "uint16")
    final = tmp_path / "original.nd2svs"
    pack_directory(directory, final, source=source)
    return SimpleNamespace(root=values, attrs=attrs, manifest=read_manifest(final), source_path=source,
                           container_path=final, store_path=final, busy=_Lifecycle(), plate=None)


@pytest.fixture
def model(tmp_path):
    path = tmp_path / "fitted-model.npz"
    yy, xx = np.indices((8, 8))
    pattern = (0.15 * np.sin(xx * np.pi / 4) + 0.07 * np.cos(yy * np.pi / 4))
    np.savez_compressed(path, basis=np.diag([0.6, 0.7]), oldbasis=np.diag([0.6, 0.7]),
                        templates=np.stack([pattern, pattern]), oldtemplates=np.stack([pattern, pattern]),
                        delta=np.zeros((4, 4)), local=np.array([False, False]), old=np.array([False, False]),
                        channel_indices=np.array([1, 2]), enabled=np.array([True, False]))
    result = Model(path)
    yield result
    result.data.close()


def _prepare(original, model, event=None, progress=lambda _: None):
    return hp.prepare_apply(original, model, {"selected": [{"channel": "FITC", "outcome": "fitted"}]},
                            event or threading.Event(), progress)


def _mean2(a):
    h, w = a.shape[0] // 2, a.shape[1] // 2
    return a[:h * 2, :w * 2].reshape(h, 2, w, 2).mean(axis=(1, 3), dtype=np.float64).astype(np.float32)


def test_apply_replaces_existing_path_and_reopens_native_corrected_pixels(original, model, monkeypatch):
    old_bytes = original.container_path.read_bytes()
    source_bytes = original.source_path.read_bytes()
    prepared = _prepare(original, model)
    assert original.container_path.read_bytes() == old_bytes
    prepared.publish()
    prepared.close()
    manifest = read_manifest(original.container_path)
    assert manifest["h_correction"]["applied"] is True
    assert manifest["format"] == "nd2wsi-cache/5"
    assert manifest["storage"]["container"] == "nd2svs-sqlite/3"
    assert manifest["generation"] != original.manifest["generation"]
    with original.container_path.open("rb") as stream:
        header = stream.read(100)
    assert int.from_bytes(header[60:64], "big") == 3  # old readers reject before accessing pixels
    stored, attrs = open_store(original.container_path)
    try:
        assert attrs["nd2wsi"]["dtype"] == "uint16"
        assert attrs["nd2wsi"]["display_dtype"] == "float32"
        expected = model.correct(1, original.root["0"][1], 0, 0)
        for key in ("1", "2", "3"):
            expected = _mean2(expected)
            assert stored[key].dtype == np.dtype("float32")
            np.testing.assert_array_equal(stored[key][1], expected)
            for ci in (0, 2, 3):
                np.testing.assert_array_equal(stored[key][ci], original.root[key][ci])
    finally:
        stored.close()
    reader = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("nd2.ND2File", lambda _: reader)
    monkeypatch.setattr("nd2wsi.reader.is_source_backable", lambda *_: (True, ""))
    monkeypatch.setattr("nd2wsi.reader.plane_view", lambda *_: (original.root["0"], None))
    reopened, _ = open_nd2_backed(original.source_path, original.container_path)
    try:
        np.testing.assert_array_equal(reopened.raw_level0[:, 3:17, 7:29], original.root["0"][:, 3:17, 7:29])
        np.testing.assert_array_equal(reopened["0"][1, 3:17, 7:29], model.correct(1, original.root["0"][1, 3:17, 7:29], 3, 7))
        np.testing.assert_array_equal(reopened["0"][3], original.root["0"][3].astype(np.float32))
    finally:
        reopened.close(delay=0)
    assert original.source_path.read_bytes() == source_bytes


def test_direct_native_wrapper_keeps_channel_and_window_coordinates(original, model):
    wrapped = _PersistedHLevel0(original.root["0"], model, _Lifecycle())
    expected = original.root["0"][:, 7:19, 9:31].astype(np.float32)
    expected[1] = model.correct(1, expected[1], 7, 9)
    np.testing.assert_array_equal(wrapped[:, 7:19, 9:31], expected)
    assert wrapped[1, :0, :0].shape == (0, 0)


def test_cancel_during_apply_keeps_original_bytes_and_releases_lock(original, model):
    before = original.container_path.read_bytes()
    event = threading.Event()
    with pytest.raises(hp.PreparationCancelled):
        _prepare(original, model, event, lambda _: event.set())
    assert original.container_path.read_bytes() == before
    assert not list(original.container_path.parent.glob("*.building-h-*"))
    with CacheLock(original.container_path):
        pass


def test_failed_atomic_publish_and_source_change_preserve_old_cache(original, model, monkeypatch):
    before = original.container_path.read_bytes()
    prepared = _prepare(original, model)

    def fail_replace(*_):
        raise PermissionError("another viewer holds the file")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="another viewer"):
        prepared.publish()
    assert original.container_path.read_bytes() == before
    prepared.abort()
    assert not prepared.staging.exists()


def test_publish_refuses_source_changed_since_preparation(original, model):
    before = original.container_path.read_bytes()
    prepared = _prepare(original, model)
    original.source_path.write_bytes(b"source deliberately changed in a private test")
    with pytest.raises(ValueError, match="original ND2 changed"):
        prepared.publish()
    prepared.close()
    assert original.container_path.read_bytes() == before


def test_another_writer_and_unknown_user_entry_are_preserved(original, model):
    before = original.container_path.read_bytes()
    with CacheLock(original.container_path):
        with pytest.raises(TimeoutError):
            _prepare(original, model)
    assert original.container_path.read_bytes() == before
    with SQLiteStore(original.container_path) as store:
        store.write_bytes("annotations/user.json", b'{"manual":true}')
    before = original.container_path.read_bytes()
    with pytest.raises(ValueError, match="user-owned entry"):
        _prepare(original, model)
    assert original.container_path.read_bytes() == before


def test_revoke_rebuilds_from_original_nd2_then_returns_raw_v2(original, model, monkeypatch):
    prepared = _prepare(original, model)
    prepared.publish()
    prepared.close()
    corrected_bytes = original.container_path.read_bytes()
    original.manifest = read_manifest(original.container_path)
    called = []

    def raw_converter(source, target, **kwargs):
        called.append(Path(source))
        assert source == original.source_path
        assert original.container_path.read_bytes() == corrected_bytes
        _write_group(target, original.root, copy.deepcopy(original.attrs))
        kwargs["on_progress"](1.0)

    monkeypatch.setattr("nd2wsi.convert.convert", raw_converter)
    replacement = hp.prepare_revoke(original, threading.Event(), lambda _: None)
    assert called == [original.source_path]
    assert original.container_path.read_bytes() == corrected_bytes
    replacement.publish()
    replacement.close()
    manifest = read_manifest(original.container_path)
    assert "h_correction" not in manifest
    assert manifest["format"] == "nd2wsi-cache/4"
    assert manifest["storage"]["container"] == "nd2svs-sqlite/2"
    with SQLiteStore(original.container_path, read_only=True) as store:
        assert not any(key.startswith("h-correction/") for key in store.list_keys())
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 2
    restored, _ = open_store(original.container_path)
    try:
        for key, raw in original.root.items():
            np.testing.assert_array_equal(restored[key][:], raw)
    finally:
        restored.close()


def test_persisted_model_hash_mismatch_is_rejected(original, model):
    replacement = _prepare(original, model)
    with SQLiteStore(replacement.staging) as store:
        store.write_bytes(hp.MODEL_KEY, b"tampered")
    with pytest.raises(ValueError, match="model/provenance changed"):
        hp.load_model(replacement.staging, replacement.manifest)
    replacement.abort()


def test_bffree_persistence_reopens_exact_native_gains_and_preserves_bf(original, tmp_path):
    from nd2wsi.bffree_model import LEGACY_SCHEMA, bin_centers
    from nd2wsi.bffree_model import Model as BFFreeModel

    path = tmp_path / "bffree.npz"
    np.savez_compressed(path, model_schema=np.asarray(LEGACY_SCHEMA),
        channel_indices=np.array([1, 2, 3]), enabled=np.array([True, False, True]),
        gain_x=np.array([[.9, 1., 1.1], [1., 1., 1.], [1.2, .8, 1.]]),
        x_bin_centers=bin_centers(73), native_shape_yx=np.array([67, 73]), downsample=np.array(32))
    model = BFFreeModel(path)
    replacement = hp.prepare_apply(original, model, {"config": {"test": "known bin gains"}, "selected": []},
                                   threading.Event(), lambda _: None)
    try:
        correction = replacement.manifest["h_correction"]
        assert correction["method"] == "bffree" and correction["experimental"] is True
        assert correction["model_schema"] == LEGACY_SCHEMA
        assert correction["channel_indices"] == [1, 3]
        loaded = hp.load_model(replacement.staging, replacement.manifest)
        try:
            wrapped = _PersistedHLevel0(original.root["0"], loaded, _Lifecycle())
            for ci in range(4):
                expected = model.correct(ci, original.root["0"][ci, 3:27, 9:72], 3, 9)
                np.testing.assert_array_equal(wrapped[ci, 3:27, 9:72], expected)
            stored, _ = open_store(replacement.staging)
            try:
                expected = model.correct(3, original.root["0"][3], 0, 0)
                for key in ("1", "2", "3"):
                    expected = _mean2(expected)
                    np.testing.assert_array_equal(stored[key][3], expected)
                    np.testing.assert_array_equal(stored[key][0], original.root[key][0])
            finally:
                stored.close()
        finally:
            loaded.data.close()
        # Metadata cannot quietly reinterpret the archive as the established method.
        incompatible = copy.deepcopy(replacement.manifest)
        incompatible["h_correction"]["method"] = "h"
        with pytest.raises(ValueError, match="source/selection|method"):
            hp.load_model(replacement.staging, incompatible)
        # Caches written before rank-1 adoption have no explicit model_schema
        # in their manifest/provenance. Their archived schema remains authoritative.
        old_manifest = copy.deepcopy(replacement.manifest)
        old_manifest["h_correction"].pop("model_schema")
        old_provenance = json.loads(read_entry(replacement.staging, hp.PROVENANCE_KEY))
        for key in ("model_schema", "experimental", "source_shape_cyx", "source_channel_mapping", "native_coordinate_contract"):
            old_provenance.pop(key, None)
        payload = hp._json_bytes(old_provenance)
        old_manifest["h_correction"]["provenance_sha256"] = hp._sha(payload)
        attrs = json.loads(read_entry(replacement.staging, "store.ome.zarr/.zattrs"))
        attrs["nd2wsi"]["h_correction"] = old_manifest["h_correction"]
        with SQLiteStore(replacement.staging) as store:
            store.write_bytes(hp.PROVENANCE_KEY, payload)
            store.write_bytes("manifest.json", hp._json_bytes(old_manifest))
            store.write_bytes("store.ome.zarr/.zattrs", hp._json_bytes(attrs))
        loaded = hp.load_model(replacement.staging, old_manifest)
        try:
            assert loaded.model_schema == LEGACY_SCHEMA
            assert loaded.enabled == [True, False, True]  # retain historical DAPI behavior
        finally:
            loaded.data.close()
    finally:
        replacement.abort()
        model.data.close()


@pytest.fixture(scope="module")
def rank1_persisted(tmp_path_factory):
    """Fit once on actual 32x32 means, then reuse a private prepared cache."""
    from nd2wsi.bffree_model import fit

    folder = tmp_path_factory.mktemp("rank1-persistence")
    x = np.arange(12288, dtype=float)
    native = np.stack([np.broadcast_to(np.rint(line).astype(np.uint16), (256, len(x))) for line in (
        np.full(x.shape, 500),
        1200 * (1 + .15*np.cos(2*np.pi*x/512 + .2)),
        900 * (1 + .20*np.cos(2*np.pi*x/640 + .4)),
        800 * (1 + .25*np.cos(2*np.pi*x/768 + .7)),
    )])
    state = _original_state(folder, native, tile=256)
    raw32 = native[1:].reshape(3, 8, 32, 384, 32).mean(axis=(2, 4)).astype(np.float32)
    metadata = {"channel_mapping": {"FITC": 1, "CY3": 2, "DAPI": 3},
                "channel_names": ["BF", "FITC", "CY3", "DAPI"],
                "sizes": {"Y": 256, "X": 12288}}
    model, result = fit(raw32, metadata, folder / "rank1.npz")
    assert model.enabled == [True, True, False]
    replacement = hp.prepare_apply(state, model, result, threading.Event(), lambda _: None)
    try:
        yield state, model, result, replacement
    finally:
        replacement.abort()
        model.data.close()


def test_rank1_persistence_reopens_exact_pixels_and_keeps_dapi_bf_identity(rank1_persisted, monkeypatch):
    from nd2wsi.bffree_model import SCHEMA as RANK1_SCHEMA

    state, model, result, replacement = rank1_persisted
    source_before = state.source_path.read_bytes()
    correction = replacement.manifest["h_correction"]
    assert correction["model_schema"] == RANK1_SCHEMA and correction["experimental"] is False
    assert correction["method"] == "bffree" and correction["channel_indices"] == [1, 2]
    assert correction["native_coordinate_contract"] == hp.RANK1_NATIVE_CONTRACT
    provenance = json.loads(read_entry(replacement.staging, hp.PROVENANCE_KEY))
    assert provenance["model_schema"] == RANK1_SCHEMA and provenance["experimental"] is False
    assert provenance["source_shape_cyx"] == [4, 256, 12288]
    assert provenance["source_channel_mapping"] == {"FITC": 1, "CY3": 2, "DAPI": 3}
    assert provenance["config"] == result["config"]
    reader = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("nd2.ND2File", lambda _: reader)
    monkeypatch.setattr("nd2wsi.reader.is_source_backable", lambda *_: (True, ""))
    monkeypatch.setattr("nd2wsi.reader.plane_view", lambda *_: (state.root["0"], None))
    reopened, _ = open_nd2_backed(state.source_path, replacement.staging)
    try:
        for ci in range(4):
            expected = model.correct(ci, state.root["0"][ci, 63:93, 201:317], 63, 201)
            np.testing.assert_array_equal(reopened["0"][ci, 63:93, 201:317], expected)
        np.testing.assert_array_equal(reopened.raw_level0[:, 63:93, 201:317], state.root["0"][:, 63:93, 201:317])
        for ci in (1, 2):
            expected = model.correct(ci, state.root["0"][ci], 0, 0)
            for key in ("1", "2", "3"):
                expected = _mean2(expected)
                np.testing.assert_array_equal(reopened[key][ci], expected)
        for ci in (0, 3):
            for key in ("1", "2", "3"):
                np.testing.assert_array_equal(reopened[key][ci], state.root[key][ci])
    finally:
        reopened.close(delay=0)
    assert state.source_path.read_bytes() == source_before


@pytest.mark.parametrize("change", ["schema", "missing_schema", "experimental", "native_contract", "shape",
                                    "channel_labels", "channel_mapping", "stored_labels", "fit_config", "fit_channels"])
def test_rank1_persisted_provenance_mismatch_is_rejected(rank1_persisted, tmp_path, change):
    _, _, _, replacement = rank1_persisted
    target = tmp_path / "changed.nd2svs"
    shutil.copyfile(replacement.staging, target)
    manifest = copy.deepcopy(replacement.manifest)
    correction = manifest["h_correction"]
    provenance = json.loads(read_entry(target, hp.PROVENANCE_KEY))
    attrs = json.loads(read_entry(target, "store.ome.zarr/.zattrs"))
    if change == "schema":
        correction["model_schema"] = provenance["model_schema"] = "BF_free_periodic_x1/1"
    elif change == "missing_schema":
        correction.pop("model_schema")
        provenance.pop("model_schema")
    elif change == "experimental":
        correction["experimental"] = provenance["experimental"] = True
    elif change == "native_contract":
        correction["native_coordinate_contract"] = provenance["native_coordinate_contract"] = hp.LEGACY_BFFREE_NATIVE_CONTRACT
    elif change == "shape":
        provenance["source_shape_cyx"][1] += 1
    elif change == "channel_labels":
        provenance["source_channel_names"][1:3] = ["CY3", "FITC"]
        attrs["omero"]["channels"][1]["label"] = "CY3"
        attrs["omero"]["channels"][2]["label"] = "FITC"
    elif change == "channel_mapping":
        provenance["source_channel_mapping"]["FITC"] = 2
    elif change == "stored_labels":
        attrs["omero"]["channels"][1]["label"] = "DAPI"
    elif change == "fit_config":
        provenance["fit"]["config"]["candidate_id"] = "periodic_independent_x_h1"
    else:
        provenance["fit"]["selected"][0]["channel_index"] = 0
    payload = hp._json_bytes(provenance)
    correction["provenance_sha256"] = hp._sha(payload)
    attrs["nd2wsi"]["h_correction"] = correction
    with SQLiteStore(target) as store:
        store.write_bytes(hp.PROVENANCE_KEY, payload)
        store.write_bytes("manifest.json", hp._json_bytes(manifest))
        store.write_bytes("store.ome.zarr/.zattrs", hp._json_bytes(attrs))
    with pytest.raises(ValueError, match="Rank1|BF-free"):
        hp.load_model(target, manifest)


def test_rank1_wrong_source_mapping_refused_before_pyramid_work(rank1_persisted, tmp_path, monkeypatch):
    state, model, result, _ = rank1_persisted
    # A separate cache/source avoids touching the shared read-only staged fixture.
    local = copy.copy(state)
    local.source_path = tmp_path / "original.nd2"
    local.source_path.write_bytes(state.source_path.read_bytes())
    shutil.copystat(state.source_path, local.source_path)
    local.container_path = local.store_path = tmp_path / "original.nd2svs"
    shutil.copyfile(state.container_path, local.container_path)
    local.attrs = copy.deepcopy(state.attrs)
    local.attrs["omero"]["channels"][1]["label"] = "CY3"
    local.attrs["omero"]["channels"][2]["label"] = "FITC"
    before = local.container_path.read_bytes()
    monkeypatch.setattr(hp, "_read_native", lambda *_a: pytest.fail("Mismatched model reached pixel work"))
    with pytest.raises(ValueError, match="Rank1 model channels"):
        hp.prepare_apply(local, model, result, threading.Event(), lambda _: None)
    assert local.container_path.read_bytes() == before
