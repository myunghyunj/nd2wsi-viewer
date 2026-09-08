"""Single-file viewer provenance, lifetime, and preservation boundaries."""
import json
import os

import numpy as np
import pytest

from nd2wsi.cache import quick_fingerprint, read_manifest
from nd2wsi.server import SlideRegistry
from nd2wsi.storage.single_file import SQLiteStore, open_zarr_group


@pytest.fixture()
def cache_file(tmp_path):
    source = tmp_path / "sample.nd2"
    source.write_bytes(b"original source, not a cache")
    cache = tmp_path / "nd2wsi" / "caches" / "sample.nd2--t0-p0-z0.nd2svs"
    group = open_zarr_group(cache, prefix="store.ome.zarr", mode="a")
    try:
        group.create_array("0", data=np.arange(64, dtype=np.uint16).reshape(1, 8, 8), chunks=(1, 4, 4))
        group.attrs.update({
            "multiscales": [{"datasets": [{"path": "0"}]}],
            "nd2wsi": {"source": source.name, "selection": {"t": 0, "p": 0, "z": 0},
                       "levels": [{"path": "0", "width": 8, "height": 8, "downsample": 1}]},
        })
    finally:
        group.store.close()
    manifest = {
        "format": "nd2wsi-cache/4", "kind": "full", "complete": True,
        "generation": "original-generation",
        "source": {**quick_fingerprint(source), "relative_path": os.path.relpath(source, cache.parent)},
        "selection": {"t": 0, "p": 0, "z": 0, "z_resolved": 0},
    }
    with SQLiteStore(cache) as store:
        store.write_bytes("manifest.json", json.dumps(manifest).encode())
    return cache, source


def test_file_opens_with_source_provenance_and_external_annotations(cache_file):
    cache, source = cache_file
    registry = SlideRegistry()
    state = registry.get(registry.open_path(cache))
    try:
        assert state.store_path == state.container_path == state.trash_path == cache
        assert state.source_path == source
        assert state.generation == "original-generation"
        assert state.annotations_path == source.parent / "nd2wsi" / "annotations" / "annotations_sample.nd2--t0-p0-z0.json"
        assert np.asarray(state.root["0"][:]).sum() == 2016
    finally:
        registry.close_all(immediate=True)
    assert state.root.store._closed


def test_file_trash_preserves_source_and_annotation(cache_file):
    cache, source = cache_file
    registry = SlideRegistry()
    sid = registry.open_path(cache)
    state = registry.get(sid)
    state.annotations_path.write_text('{"roi": "manual work"}')
    before = source.read_bytes()
    assert registry.trash_cache(sid) > 0
    assert not cache.exists()
    assert source.read_bytes() == before
    assert state.annotations_path.read_text() == '{"roi": "manual work"}'
    assert state.root.store._closed


@pytest.mark.parametrize("key", ["annotations_manual.json", "store.ome.zarr/annotations_manual.json", "store.ome.zarr/manual/0"])
def test_file_trash_refuses_unknown_embedded_user_work(cache_file, key):
    cache, _ = cache_file
    registry = SlideRegistry()
    sid = registry.open_path(cache)
    with SQLiteStore(cache) as store:
        store.write_bytes(key, b"irreplaceable work")
    try:
        with pytest.raises(ValueError, match="user-owned"):
            registry.trash_cache(sid)
        assert cache.exists()
        assert not registry.get(sid).root.store._closed
        with SQLiteStore(cache, read_only=True) as store:
            assert store.read_bytes(key) == b"irreplaceable work"
    finally:
        registry.close_all(immediate=True)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_file_trash_refuses_journal_companions(cache_file, suffix):
    cache, _ = cache_file
    registry = SlideRegistry()
    sid = registry.open_path(cache)
    companion = cache.with_name(cache.name + suffix)
    companion.write_bytes(b"possibly active")
    try:
        with pytest.raises(ValueError, match="journal"):
            registry.trash_cache(sid)
        assert cache.exists() and companion.read_bytes() == b"possibly active"
    finally:
        companion.unlink()
        registry.close_all(immediate=True)


def test_file_trash_refuses_changed_generation(cache_file):
    cache, _ = cache_file
    registry = SlideRegistry()
    sid = registry.open_path(cache)
    manifest = read_manifest(cache)
    manifest["generation"] = "replacement-generation"
    with SQLiteStore(cache) as store:
        store.write_bytes("manifest.json", json.dumps(manifest).encode())
    try:
        with pytest.raises(ValueError, match="generation changed"):
            registry.trash_cache(sid)
        assert cache.exists()
    finally:
        registry.close_all(immediate=True)


def test_file_trash_refuses_hard_link(cache_file):
    cache, _ = cache_file
    registry = SlideRegistry()
    sid = registry.open_path(cache)
    alias = cache.with_name("another.nd2svs")
    try:
        os.link(cache, alias)
    except OSError:
        registry.close_all(immediate=True)
        pytest.skip("filesystem does not support hard links")
    try:
        with pytest.raises(ValueError, match="not a managed cache"):
            registry.trash_cache(sid)
        assert cache.exists() and alias.exists()
    finally:
        registry.close_all(immediate=True)


def test_single_file_open_refuses_symlink(cache_file, symlink_support):
    cache, _ = cache_file
    alias = cache.with_name("alias.nd2svs")
    alias.symlink_to(cache)
    with pytest.raises(ValueError, match="symbolic link"):
        SlideRegistry().open_path(alias)
    assert cache.exists()


def test_failed_registration_closes_single_file(cache_file, monkeypatch):
    import nd2wsi.convert as convert_module
    import nd2wsi.server as server

    cache, _ = cache_file
    opened = []
    original = convert_module.open_store
    def tracked(path):
        result = original(path)
        opened.append(result[0])
        return result
    def fail(*args):
        raise OSError("sidecar unavailable")
    monkeypatch.setattr(convert_module, "open_store", tracked)
    monkeypatch.setattr(server, "annotations_sidecar", fail)
    with pytest.raises(OSError, match="sidecar unavailable"):
        SlideRegistry().open_path(cache)
    assert len(opened) == 1 and opened[0].store._closed


def test_app_accepts_single_file_without_conversion(cache_file, monkeypatch):
    import nd2wsi.convert as convert_module
    from nd2wsi.app import open_or_convert

    cache, _ = cache_file
    def fail(*args, **kwargs):
        raise AssertionError("a cache must not be converted as a source")
    monkeypatch.setattr(convert_module, "ensure_cache", fail)
    assert open_or_convert(cache) == cache


def test_plate_cache_missing_source_is_not_opened_as_a_pyramid(cache_file):
    cache, source = cache_file
    manifest = read_manifest(cache)
    manifest.update(kind="plate", format="nd2wsi-plate/2")
    with SQLiteStore(cache) as store:
        store.write_bytes("manifest.json", json.dumps(manifest).encode())
    source.unlink()
    with pytest.raises(ValueError, match="original ND2"):
        SlideRegistry().open_path(cache)
    assert cache.exists()


def test_cli_views_cache_without_rebuilding(cache_file, monkeypatch):
    import nd2wsi.convert as convert_module
    import nd2wsi.server as server
    from nd2wsi.cli import main

    cache, _ = cache_file
    served = []
    monkeypatch.setattr(server, 'serve', lambda paths, **kwargs: served.extend(paths))
    def fail(*args, **kwargs):
        raise AssertionError('must not rebuild cache')
    monkeypatch.setattr(convert_module, 'ensure_cache', fail)
    assert main(['view', str(cache)]) == 0
    assert served == [cache]
    with pytest.raises(SystemExit) as exc:
        main(['view', str(cache), '--z', '0'])
    assert exc.value.code == 2


def test_uppercase_extension_keeps_manifest_and_provenance(cache_file):
    cache, source = cache_file
    upper = cache.with_suffix('.ND2SVS')
    cache.rename(upper)
    registry = SlideRegistry()
    try:
        state = registry.get(registry.open_path(upper))
        assert state.container_path == upper and state.source_path == source
    finally:
        registry.close_all(immediate=True)
