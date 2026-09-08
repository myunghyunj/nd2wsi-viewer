"""Byte-preserving cache migration, publication, and failure boundaries."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest
import zarr

from nd2wsi import convert as convert_mod
from nd2wsi.cache import (
    MANIFEST_NAME,
    SINGLE_FILE_FORMAT,
    cache_container,
    cache_matches,
    directory_cache_container,
    legacy_cache_container,
    quick_fingerprint,
    read_manifest,
    write_manifest,
)
from nd2wsi.convert import ensure_cache, existing_cache_store, open_store
from nd2wsi.migration import cache_entries, pack_directory
from nd2wsi.reader import PlaneSelection
from nd2wsi.storage.single_file import FORMAT_VERSION, SQLiteStore, read_entry


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(path):
    return {file.relative_to(path).as_posix(): file_hash(file)
            for file in path.rglob("*") if file.is_file()}


def make_store(path, source, selection=None, overview=False):
    selection = selection or PlaneSelection()
    expected = np.arange(2 * 9 * 11, dtype=np.uint16).reshape(2, 9, 11)
    level = "1" if overview else "0"
    group = zarr.open_group(path, mode="w", zarr_format=2)
    try:
        array = group.create_array(level, shape=expected.shape, chunks=(1, 4, 4), dtype="u2")
        array[:] = expected
        nd2wsi = {
            "source": source.name, "dtype": "uint16", "tile": 4,
            "pixel_size_um": [0.325, 0.41],
            "calibration": {"status": "calibrated", "source": "axesCalibrated"},
            "selection": {**selection.describe(), "z": 3},
            "levels": [{"path": level, "width": 11, "height": 9,
                        "downsample": 2 if overview else 1}],
        }
        if overview:
            nd2wsi["overview_of"] = {"channels": 2, "height": 18, "width": 22}
        group.attrs.update({
            "multiscales": [{
                "version": "0.4", "axes": [
                    {"name": "c", "type": "channel"},
                    {"name": "y", "type": "space", "unit": "micrometer"},
                    {"name": "x", "type": "space", "unit": "micrometer"},
                ],
                "datasets": [{"path": level, "coordinateTransformations": [
                    {"type": "scale", "scale": [1.0, 0.65 if overview else 0.325,
                                                   0.82 if overview else 0.41]},
                ]}],
            }],
            "nd2wsi": nd2wsi,
            "omero": {"channels": [{"label": "MPO", "color": "00FF00"},
                                   {"label": "DAPI", "color": "0000FF"}]},
        })
    finally:
        group.store.close()
    return expected


def make_legacy(tmp_path, *, overview=False, stem_legacy=False, selection=None):
    selection = selection or PlaneSelection()
    source = tmp_path / "한글 원본.nd2"
    source.write_bytes(b"immutable source data\0" * 100)
    factory = legacy_cache_container if stem_legacy else directory_cache_container
    container = factory(source, selection)
    expected = make_store(container / "store.ome.zarr", source, selection, overview)
    write_manifest(
        container, source, quick_fingerprint(source), selection.describe(), 3, 4,
        (2, 18, 22) if overview else expected.shape, "uint16",
        kind="overview" if overview else "full",
    )
    return source, container, expected


@pytest.mark.parametrize("overview", [False, True])
def test_pack_preserves_source_every_chunk_calibration_channels_and_selection(tmp_path, overview):
    selection = PlaneSelection(t=1, p=2, z="max")
    source, legacy, expected = make_legacy(tmp_path, overview=overview, selection=selection)
    source_before, tree_before = file_hash(source), tree_hashes(legacy)
    manifest_before = read_manifest(legacy)
    target = tmp_path / "moved" / "cache.nd2svs"
    progress = []
    result = pack_directory(legacy, target, on_progress=progress.append)
    assert result["verified"] is True
    assert progress == sorted(progress) and progress[-1] == 1.0
    assert file_hash(source) == source_before
    assert tree_hashes(legacy) == tree_before
    manifest_after = read_manifest(target)
    assert manifest_after["format"] == SINGLE_FILE_FORMAT
    assert manifest_after["storage"]["container"] == f"nd2svs-sqlite/{FORMAT_VERSION}"
    assert manifest_after["generation"] == manifest_before["generation"]
    assert manifest_after["selection"] == manifest_before["selection"]
    assert manifest_after["kind"] == manifest_before["kind"]
    assert (target.parent / manifest_after["source"]["relative_path"]).resolve() == source
    assert cache_matches(target, source, selection)
    with SQLiteStore(target, read_only=True) as store:
        keys = list(store.list_keys())
        assert set(keys) == set(tree_before)
        for key in keys:
            if key != MANIFEST_NAME:
                assert hashlib.sha256(store.read_bytes(key)).hexdigest() == tree_before[key]
        assert result["payload_bytes"] == sum(len(store.read_bytes(key)) for key in keys)
    group, attrs = open_store(target)
    try:
        level = "1" if overview else "0"
        assert np.array_equal(group[level][:], expected)
        assert attrs["nd2wsi"]["pixel_size_um"] == [0.325, 0.41]
        assert attrs["nd2wsi"]["calibration"]["source"] == "axesCalibrated"
        assert [channel["label"] for channel in attrs["omero"]["channels"]] == ["MPO", "DAPI"]
        assert attrs["nd2wsi"]["selection"] == {"t": 1, "p": 2, "z": 3}
    finally:
        group.close()
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("interrupt", [False, True])
def test_migration_releases_all_connections_before_return_or_failure(tmp_path, monkeypatch, interrupt):
    _, legacy, _ = make_legacy(tmp_path)
    target = tmp_path / "closed-handles.nd2svs"
    connections = {}
    real_open = SQLiteStore._ensure_open_sync

    def record_connection(self):
        connection = real_open(self)
        connections[id(connection)] = connection  # Keep alive; do not rely on GC.
        return connection

    def on_progress(progress):
        if interrupt and progress > 0.6:
            raise RuntimeError("injected verification interruption")

    monkeypatch.setattr(SQLiteStore, "_ensure_open_sync", record_connection)
    if interrupt:
        with pytest.raises(RuntimeError, match="injected verification"):
            pack_directory(legacy, target, on_progress=on_progress)
    else:
        pack_directory(legacy, target, on_progress=on_progress)
    assert connections
    for connection in connections.values():
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    moved = target.with_name("movable-after-close.nd2svs")
    target.rename(moved)
    assert moved.is_file()
    assert legacy.is_dir()


def test_final_durability_flush_uses_nontruncating_writable_handle(tmp_path, monkeypatch):
    _, legacy, _ = make_legacy(tmp_path)
    target = tmp_path / "durable.nd2svs"
    target_handles = []
    real_open = Path.open
    real_fsync = os.fsync
    flushed = []

    def record_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if self == target:
            target_handles.append((mode, handle))
        return handle

    def checked_fsync(fd):
        handles = [(mode, handle) for mode, handle in target_handles if not handle.closed]
        assert len(handles) == 1
        mode, handle = handles[0]
        assert fd == handle.fileno()
        assert mode == "r+b" and handle.writable() and handle.readable()
        flushed.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(Path, "open", record_open)
    monkeypatch.setattr(os, "fsync", checked_fsync)
    result = pack_directory(legacy, target)
    assert result["verified"]
    assert len(flushed) == 1
    assert all(handle.closed for _, handle in target_handles)


@pytest.mark.parametrize("key", ["annotations.json", "notes.txt", "store.ome.zarr/annotations.json",
                                  "profile/analysis.json"])
def test_user_owned_entries_refuse_migration_without_changing_anything(tmp_path, key):
    source, legacy, _ = make_legacy(tmp_path)
    extra = legacy / key
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"important user work")
    before = tree_hashes(legacy)
    target = tmp_path / "refused.nd2svs"
    with pytest.raises(ValueError, match="unknown or user-owned"):
        pack_directory(legacy, target, source=source)
    assert not target.exists()
    assert tree_hashes(legacy) == before


def test_symlinked_entries_and_directories_are_refused(tmp_path):
    source, legacy, _ = make_legacy(tmp_path)
    target = tmp_path / "refused.nd2svs"
    linked = legacy / "store.ome.zarr" / "0" / "9.9.9"
    try:
        linked.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="not a regular"):
        pack_directory(legacy, target)
    linked.unlink()
    linked.symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        pack_directory(legacy, target)
    assert not target.exists()


def test_appledouble_metadata_is_ignored_but_originals_remain(tmp_path):
    _, legacy, _ = make_legacy(tmp_path)
    (legacy / "._manifest.json").write_bytes(b"Finder metadata")
    (legacy / ".DS_Store").write_bytes(b"Finder view")
    before = tree_hashes(legacy)
    target = tmp_path / "cache.nd2svs"
    pack_directory(legacy, target)
    with SQLiteStore(target, read_only=True) as store:
        assert "._manifest.json" not in set(store.list_keys())
        assert ".DS_Store" not in set(store.list_keys())
    assert tree_hashes(legacy) == before


@pytest.mark.parametrize("format_name", ["nd2wsi-cache/999", "nd2wsi-cache/4", "unknown/1"])
def test_unknown_or_file_only_manifest_formats_are_refused(tmp_path, format_name):
    _, legacy, _ = make_legacy(tmp_path)
    manifest = read_manifest(legacy)
    manifest["format"] = format_name
    (legacy / MANIFEST_NAME).write_text(json.dumps(manifest))
    before = tree_hashes(legacy)
    with pytest.raises(ValueError, match="unknown cache format"):
        pack_directory(legacy, tmp_path / "refused.nd2svs")
    assert tree_hashes(legacy) == before


def test_incomplete_cache_and_wrong_same_name_source_are_refused(tmp_path):
    source, legacy, _ = make_legacy(tmp_path)
    wrong = tmp_path / "different" / source.name
    wrong.parent.mkdir()
    wrong.write_bytes(b"different acquisition")
    with pytest.raises(ValueError, match="fingerprint"):
        pack_directory(legacy, tmp_path / "refused.nd2svs", source=wrong)
    manifest = read_manifest(legacy)
    manifest["complete"] = False
    (legacy / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="incomplete"):
        pack_directory(legacy, tmp_path / "refused.nd2svs")


def test_existing_target_is_never_overwritten_even_when_created_during_open(tmp_path, monkeypatch):
    _, legacy, _ = make_legacy(tmp_path)
    target = tmp_path / "contended.nd2svs"
    real_create = SQLiteStore.create_exclusive

    def competing_create(path, **kwargs):
        with real_create(path) as competitor:
            competitor.write_bytes("winner", b"do not change")
        return real_create(path, **kwargs)

    monkeypatch.setattr(SQLiteStore, "create_exclusive", competing_create)
    with pytest.raises(FileExistsError):
        pack_directory(legacy, target)
    assert read_entry(target, "winner") == b"do not change"
    with SQLiteStore(target, read_only=True) as store:
        assert list(store.list_keys()) == ["winner"]


@pytest.mark.parametrize("fail_at", [0.1, 0.6])
def test_failures_preserve_input_and_never_advertise_unverified_completion(tmp_path, fail_at):
    source, legacy, _ = make_legacy(tmp_path)
    source_before, tree_before = file_hash(source), tree_hashes(legacy)
    target = tmp_path / "interrupted.nd2svs"

    def fail(progress):
        if progress >= fail_at:
            raise RuntimeError("injected interruption")

    with pytest.raises(RuntimeError, match="injected"):
        pack_directory(legacy, target, on_progress=fail)
    assert tree_hashes(legacy) == tree_before
    assert file_hash(source) == source_before
    assert read_manifest(target) is None
    assert not Path(str(target) + "-journal").exists()


def test_cache_mutation_with_unchanged_size_and_mtime_is_detected(tmp_path):
    _, legacy, _ = make_legacy(tmp_path)
    chunk = next(path for key, path in cache_entries(legacy) if key.endswith("0/0.0.0"))
    before = chunk.stat()
    mutated = False

    def change_after_copy(progress):
        nonlocal mutated
        if progress == 0.5 and not mutated:
            payload = chunk.read_bytes()
            chunk.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
            os.utime(chunk, ns=(before.st_atime_ns, before.st_mtime_ns))
            mutated = True

    target = tmp_path / "interrupted.nd2svs"
    with pytest.raises(ValueError, match="source cache changed"):
        pack_directory(legacy, target, on_progress=change_after_copy)
    assert mutated
    assert read_manifest(target) is None


def test_late_annotation_addition_prevents_success(tmp_path):
    _, legacy, _ = make_legacy(tmp_path)
    target = tmp_path / "interrupted.nd2svs"

    def add_user_work(progress):
        if progress == 1:
            (legacy / "annotations.json").write_bytes(b"new annotation")

    with pytest.raises(ValueError, match="unknown or user-owned"):
        pack_directory(legacy, target, on_progress=add_user_work)
    assert (legacy / "annotations.json").read_bytes() == b"new annotation"
    assert read_manifest(target) is None


def test_source_change_during_pack_prevents_success(tmp_path):
    source, legacy, _ = make_legacy(tmp_path)
    target = tmp_path / "interrupted.nd2svs"

    def change_source(progress):
        if progress == 1:
            source.write_bytes(b"source replaced by scanner")

    with pytest.raises(ValueError, match="source file changed"):
        pack_directory(legacy, target, on_progress=change_source)
    assert read_manifest(target) is None


@pytest.mark.parametrize("stem_legacy", [False, True])
def test_ensure_imports_legacy_cache_once_and_preserves_legacy_tree(tmp_path, monkeypatch, stem_legacy):
    source, legacy, expected = make_legacy(tmp_path, stem_legacy=stem_legacy)
    before, source_before = tree_hashes(legacy), file_hash(source)

    def must_not_convert(*args, **kwargs):
        raise AssertionError("legacy cache should be copied, not decoded from the source")

    monkeypatch.setattr(convert_mod, "convert", must_not_convert)
    target = ensure_cache(source)
    assert target == cache_container(source) and target.suffix == ".nd2svs"
    assert target.is_file() and legacy.is_dir()
    assert tree_hashes(legacy) == before
    assert file_hash(source) == source_before
    assert existing_cache_store(source) == target
    target_before = file_hash(target)
    assert ensure_cache(source) == target
    assert file_hash(target) == target_before
    group, _ = open_store(target)
    try:
        assert np.array_equal(group["0"][:], expected)
    finally:
        group.close()


def test_ensure_new_cache_returns_one_file_and_reuses_it(tmp_path, monkeypatch):
    source = tmp_path / "new.svs"
    source.write_bytes(b"read-only synthetic source")
    source_before = file_hash(source)
    builds = []

    def synthetic_convert(source_path, out_path, **kwargs):
        builds.append(Path(out_path))
        make_store(out_path, Path(source_path), kwargs.get("selection"))

    monkeypatch.setattr(convert_mod, "convert", synthetic_convert)
    target = ensure_cache(source, tile=4)
    assert target == cache_container(source)
    assert target.is_file() and read_manifest(target)["complete"]
    assert len(builds) == 1
    assert ensure_cache(source, tile=4) == target
    assert len(builds) == 1
    assert not list(target.parent.glob("*.building-*"))
    assert file_hash(source) == source_before


def test_invalid_single_file_is_not_a_reusable_manifest(tmp_path):
    path = tmp_path / "not-a-cache.nd2svs"
    path.write_bytes(b"unrecognized bytes")
    before = path.read_bytes()
    assert read_manifest(path) is None
    with pytest.raises(ValueError):
        open_store(path)
    assert path.read_bytes() == before
