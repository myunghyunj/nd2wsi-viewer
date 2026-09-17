"""Real rollback recovery, using only small synthetic databases and crashed children."""

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nd2wsi.cache import read_manifest
from nd2wsi.cache_removal import _validate_file_cache_for_trash
from nd2wsi.server import SlideRegistry
from nd2wsi.storage.single_file import SQLiteStore
from nd2wsi.window_sessions import create_window_session

MANIFEST = {"complete": True, "kind": "plate", "format": "nd2wsi-plate/2", "generation": "original"}
PAYLOAD = b"committed" * 2048


def _seed(path):
    with SQLiteStore(path) as store:
        with store.batch():
            store.write_bytes("manifest.json", json.dumps(MANIFEST).encode())
            for index in range(64):
                store.write_bytes(f"thumbs.zarr/thumbs/{index}.0.0.0.0.0", PAYLOAD)


def _crash_write(path):
    # A tiny page cache spills dirty pages and a durable journal to disk before
    # the process dies. An unspilled BEGIN/UPDATE is not a recovery regression.
    child = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA cache_size=4')
connection.execute('PRAGMA synchronous=FULL')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE entries SET value=? WHERE key=?', (b'{"complete":false}', 'manifest.json'))
for key, in connection.execute("SELECT key FROM entries WHERE key != 'manifest.json'").fetchall():
    connection.execute('UPDATE entries SET value=? WHERE key=?', (b'pending' * 4096, key))
os._exit(23)
""", str(path)], check=False, capture_output=True, timeout=20)
    assert child.returncode == 23, child.stderr.decode()
    journal = Path(str(path) + "-journal")
    assert journal.stat().st_size > 512
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    return journal


@pytest.fixture
def interrupted_cache(tmp_path):
    path = tmp_path / "interrupted.nd2svs"
    _seed(path)
    journal = _crash_write(path)
    return path, journal


def test_opt_in_recovers_committed_contents_and_keeps_reader_read_only(interrupted_cache):
    path, journal = interrupted_cache
    before = (path.read_bytes(), journal.read_bytes())
    assert read_manifest(path) is None
    assert (path.read_bytes(), journal.read_bytes()) == before
    with pytest.raises(ValueError, match="journal"):
        _validate_file_cache_for_trash(path, MANIFEST)
    with SQLiteStore(path, read_only=True, recover=True) as reader:
        assert reader.read_only
        assert json.loads(reader.read_bytes("manifest.json")) == MANIFEST
        for index in range(64):
            assert reader.read_bytes(f"thumbs.zarr/thumbs/{index}.0.0.0.0.0") == PAYLOAD
        with pytest.raises(ValueError, match="read-only"):
            reader.write_bytes("unrequested-write", b"no")
    assert not journal.exists()
    assert read_manifest(path) == MANIFEST
    _validate_file_cache_for_trash(path, MANIFEST)
    assert not list(path.parent.glob("*.corrupt-*"))


def test_healthy_and_live_journal_readers_never_open_writable(tmp_path, monkeypatch):
    path = tmp_path / "live.nd2svs"
    with SQLiteStore(path) as writer:
        writer.write_bytes("value", b"committed")
        real_connect = sqlite3.connect
        opens = []

        def record(database, *args, **kwargs):
            opens.append(str(database))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", record)
        with SQLiteStore(path, read_only=True, recover=True) as reader:
            assert reader.read_bytes("value") == b"committed"
        with writer.batch():
            writer.write_bytes("value", b"pending")
            journal = Path(str(path) + "-journal")
            before = journal.read_bytes()
            with SQLiteStore(path, read_only=True, recover=True) as reader:
                assert reader.read_bytes("value") == b"committed"
            assert journal.read_bytes() == before
        assert opens and all(uri.endswith("?mode=ro") for uri in opens)
        assert writer.read_bytes("value") == b"pending"


@pytest.mark.parametrize("kind", ["unknown-schema", "unknown-id", "future", "prototype"])
def test_unrecognized_or_prototype_hot_database_is_preserved(tmp_path, monkeypatch, kind):
    path = tmp_path / "unsupported.nd2svs"
    _seed(path)
    with sqlite3.connect(path) as connection:
        if kind == "unknown-schema":
            connection.execute("CREATE TABLE private_work (value BLOB)")
        elif kind == "unknown-id":
            connection.execute("PRAGMA application_id=123")
        elif kind == "future":
            connection.execute("PRAGMA user_version=999")
        else:
            connection.execute("ALTER TABLE entries RENAME TO original")
            connection.execute("CREATE TABLE entries (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID")
            connection.execute("INSERT INTO entries SELECT * FROM original")
            connection.execute("DROP TABLE original")
            connection.execute("PRAGMA user_version=1")
    journal = _crash_write(path)
    before = path.read_bytes(), journal.read_bytes()
    real_connect = sqlite3.connect
    opens = []

    def record(database, *args, **kwargs):
        opens.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", record)
    with pytest.raises(ValueError):
        SQLiteStore(path, read_only=True, recover=True)
    assert not any("mode=rw" in uri for uri in opens)
    assert (path.read_bytes(), journal.read_bytes()) == before


def test_refused_recovery_preserves_database_and_journal(interrupted_cache, monkeypatch):
    path, journal = interrupted_cache
    before = path.read_bytes(), journal.read_bytes()
    real_connect = sqlite3.connect
    recovery_timeouts = []

    def read_only_volume(database, *args, **kwargs):
        if "mode=rw" in str(database):
            recovery_timeouts.append(kwargs["timeout"])
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", read_only_volume)
    with pytest.raises(ValueError, match="cache and journal preserved"):
        SQLiteStore(path, read_only=True, recover=True)
    assert recovery_timeouts == [1.0]
    assert (path.read_bytes(), journal.read_bytes()) == before


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="requires POSIX permission enforcement as a non-root user")
def test_read_only_filesystem_permissions_preserve_hot_journal(interrupted_cache):
    path, journal = interrupted_cache
    before = path.read_bytes(), journal.read_bytes()
    path.chmod(0o400)
    journal.chmod(0o400)
    path.parent.chmod(0o500)
    try:
        with pytest.raises(ValueError, match="cache and journal preserved"):
            SQLiteStore(path, read_only=True, recover=True)
        assert (path.read_bytes(), journal.read_bytes()) == before
    finally:
        path.parent.chmod(0o700)
        path.chmod(0o600)
        journal.chmod(0o600)


def test_concurrent_recovery_readers_agree_on_committed_data(interrupted_cache):
    path, journal = interrupted_cache

    def read(_):
        with SQLiteStore(path, read_only=True, recover=True) as reader:
            return json.loads(reader.read_bytes("manifest.json")), reader.read_bytes("thumbs.zarr/thumbs/0.0.0.0.0.0")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(read, range(8)))
    assert results == [(MANIFEST, PAYLOAD)] * 8
    assert not journal.exists()


def test_agent_cache_open_does_not_recover_shared_hot_journal(interrupted_cache, tmp_path):
    path, journal = interrupted_cache
    before = path.read_bytes(), journal.read_bytes()
    session = create_window_session("agent", tmp_path / "sessions")
    registry = SlideRegistry(window_session=session)
    try:
        with pytest.raises(ValueError, match="no complete manifest"):
            registry.open_path(path)
        assert not registry.slides
        assert (path.read_bytes(), journal.read_bytes()) == before
    finally:
        registry.close_all(immediate=True)


def test_user_explicit_cache_open_recovers_before_manifest_dispatch(interrupted_cache, monkeypatch):
    path, journal = interrupted_cache
    registry = SlideRegistry()
    received = []
    monkeypatch.setattr(registry, "_add_plate_cache", lambda p, m: received.append((p, m)) or "plate")
    assert registry.open_path(path) == "plate"
    assert received == [(path, MANIFEST)]
    assert not journal.exists()
