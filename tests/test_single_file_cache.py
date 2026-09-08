"""Single-file cache publication and cleanup use synthetic temporary data only."""

import os
import sqlite3
from pathlib import Path

import pytest

from nd2wsi.cache import (
    CacheFromNewerApp,
    CacheLock,
    cache_container,
    commit_container,
    is_file_container,
    newer_cache_format,
    quarantine,
    sweep_stale_builds,
)
from nd2wsi.convert import ensure_cache
from nd2wsi.storage.single_file import FORMAT_VERSION, SQLiteStore, read_entry


def _write_cache(path, value=b"retained bytes"):
    with SQLiteStore(path) as store:
        store.write_bytes("marker", value)


def _snapshot(folder):
    return {
        path.relative_to(folder).as_posix(): (
            ("symlink", os.readlink(path)) if path.is_symlink() else ("file", path.read_bytes())
        )
        for path in folder.rglob("*") if path.is_symlink() or path.is_file()
    }


def _make_companion(path, suffix, symlink):
    companion = path.with_name(path.name + suffix)
    if symlink:
        try:
            companion.symlink_to(path.parent / "missing-companion-target")
        except OSError:
            pytest.skip("symlink creation is unavailable")
    else:
        companion.write_bytes(b"preserve interrupted transaction")
    return companion


def test_quarantine_reports_windows_busy_cache_without_removing_it(tmp_path, monkeypatch):
    cache = tmp_path / 'busy.nd2svs'
    _write_cache(cache)
    before = cache.read_bytes()

    def busy(*args):
        error = PermissionError('simulated sharing violation')
        error.winerror = 32
        raise error

    monkeypatch.setattr(Path, 'rename', busy)
    with pytest.raises(PermissionError, match='close all windows'):
        quarantine(cache)
    assert cache.read_bytes() == before
    assert not list(tmp_path.glob('*.corrupt-*'))


def test_ensure_cache_refuses_future_storage_header_without_opening_sqlite_or_changing_files(tmp_path, monkeypatch):
    source = tmp_path / "source.nd2"
    source.write_bytes(b"synthetic source must not be parsed or modified")
    container = cache_container(source)
    _write_cache(container)
    with sqlite3.connect(container) as connection:
        connection.execute(f"PRAGMA user_version={FORMAT_VERSION + 1}")
    before = _snapshot(tmp_path)

    def must_not_connect(*args, **kwargs):
        raise AssertionError("future format must be rejected from the header alone")

    monkeypatch.setattr(sqlite3, "connect", must_not_connect)
    with pytest.raises(CacheFromNewerApp, match="newer nd2wsi-viewer"):
        ensure_cache(source)
    assert _snapshot(tmp_path) == before
    assert not list(container.parent.glob("*.corrupt-*"))
    assert not list(container.parent.glob("*.building-*"))
    assert not list(container.parent.glob("*.lock*"))


@pytest.mark.parametrize("suffix", [".nd2svs", ".ND2SVS"])
def test_future_single_file_suffix_recognition_is_case_insensitive(tmp_path, suffix):
    path = tmp_path / ("source" + suffix)
    _write_cache(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={FORMAT_VERSION + 1}")
    before = path.read_bytes()
    assert is_file_container(path)
    assert newer_cache_format(path) == f"nd2svs-sqlite/{FORMAT_VERSION + 1}"
    assert path.read_bytes() == before


@pytest.mark.parametrize("family", [
    "slide.nd2--t0-p0-zmid.nd2svs",
    "slide.building-round2.nd2--t0-p0-zmid.nd2svs",
    "slide.replaced-earlier.nd2--t0-p0-zmid.nd2svs",
    ".hidden.nd2--t0-p0-zmid.nd2svs",
    "UPPER.ND2--t0-p0-zmid.ND2SVS",
])
@pytest.mark.parametrize("suffix", ["building", "replaced"])
def test_live_uuid_artifact_uses_the_exact_family_even_with_separator_text(tmp_path, family, suffix):
    container = tmp_path / family
    artifact = tmp_path / f"{family}.{suffix}-0123456789abcdef0123456789abcdef"
    _write_cache(artifact)
    before = artifact.read_bytes()
    with CacheLock(container):
        assert sweep_stale_builds(tmp_path) == 0
        assert artifact.read_bytes() == before


def test_dead_recognized_uuid_file_is_swept_but_unknown_artifacts_are_preserved(tmp_path):
    dead = tmp_path / ("dead.nd2--t0-p0-zmid.nd2svs.building-" + "a" * 32)
    _write_cache(dead)
    unknown_file = tmp_path / ("notes.nd2svs.building-" + "b" * 32)
    unknown_file.write_bytes(b"user notes, not a database")
    unknown_dir = tmp_path / "research-notes.building-current"
    unknown_dir.mkdir()
    (unknown_dir / "annotations.json").write_bytes(b"important annotations")
    unrelated_database = tmp_path / "research.sqlite.building-current"
    _write_cache(unrelated_database)
    future = tmp_path / ("future.nd2svs.building-" + "c" * 32)
    _write_cache(future)
    with sqlite3.connect(future) as connection:
        connection.execute(f"PRAGMA user_version={FORMAT_VERSION + 1}")
    expected = _snapshot(tmp_path)
    expected.pop(dead.name)
    assert sweep_stale_builds(tmp_path) == 1
    assert not dead.exists()
    assert _snapshot(tmp_path) == expected


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("symlink", [False, True])
def test_quarantine_refuses_companions_and_preserves_the_complete_family(tmp_path, suffix, symlink):
    cache = tmp_path / "original.nd2svs"
    _write_cache(cache)
    _make_companion(cache, suffix, symlink)
    before = _snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="journal|companion"):
        quarantine(cache)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("location", ["staging", "existing-final", "missing-final"])
def test_publication_refuses_companions_on_either_name_before_any_rename(tmp_path, suffix, symlink, location):
    staging = tmp_path / ("source.nd2svs.building-" + "d" * 32)
    final = tmp_path / "source.nd2svs"
    _write_cache(staging, b"new cache")
    if location != "missing-final":
        _write_cache(final, b"old cache")
    _make_companion(staging if location == "staging" else final, suffix, symlink)
    before = _snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="journal|companion"):
        commit_container(staging, final)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_stale_sweep_does_not_detach_a_cache_from_its_companion(tmp_path, suffix):
    staging = tmp_path / ("source.nd2svs.building-" + "e" * 32)
    _write_cache(staging)
    _make_companion(staging, suffix, False)
    before = _snapshot(tmp_path)
    assert sweep_stale_builds(tmp_path) == 0
    assert _snapshot(tmp_path) == before


def test_publication_restores_previous_file_if_final_rename_fails(tmp_path, monkeypatch):
    staging = tmp_path / ("source.nd2svs.building-" + "f" * 32)
    final = tmp_path / "source.nd2svs"
    _write_cache(staging, b"new cache")
    _write_cache(final, b"old cache")
    before = _snapshot(tmp_path)
    real_rename = Path.rename

    def fail_publish(self, destination):
        if self == staging:
            raise OSError("injected publication failure")
        return real_rename(self, destination)

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(OSError, match="injected publication"):
        commit_container(staging, final)
    assert _snapshot(tmp_path) == before
    assert read_entry(final, "marker") == b"old cache"


def test_closed_single_file_publication_keeps_only_the_new_final_file(tmp_path):
    staging = tmp_path / ("source.nd2svs.building-" + "f" * 32)
    final = tmp_path / "source.nd2svs"
    _write_cache(staging, b"new cache")
    _write_cache(final, b"old cache")
    commit_container(staging, final)
    assert list(tmp_path.iterdir()) == [final]
    assert read_entry(final, "marker") == b"new cache"
