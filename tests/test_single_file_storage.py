"""Single-file persistence checks use tiny synthetic data, never real caches."""

import asyncio
import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

import numpy as np
import pytest
import zarr
from numcodecs import Blosc
from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
from zarr.core.buffer import default_buffer_prototype

from nd2wsi.platform_io import filesystem_path
from nd2wsi.storage.single_file import (
    APPLICATION_ID,
    FORMAT_VERSION,
    SQLiteStore,
    _database_uri,
    is_single_file,
    newer_file_format,
    open_zarr_group,
    read_entry,
    write_entry,
)


def run(coroutine):
    return asyncio.run(coroutine)


async def collect(iterator):
    return [value async for value in iterator]


def test_container_header_schema_and_bounded_connection_settings(tmp_path):
    path = tmp_path / "cache.nd2svs"
    with SQLiteStore(path) as store:
        store.write_bytes("manifest.json", b'{"complete":true}')
        connection = store._connection
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("PRAGMA user_version").fetchone()[0] == FORMAT_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA cache_size").fetchone()[0] == -8192
        assert connection.execute("PRAGMA mmap_size").fetchone()[0] == 0
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert "WITHOUT ROWID" not in connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='entries'"
        ).fetchone()[0]
        assert connection.execute("PRAGMA index_list(entries)").fetchall() == [
            (0, "sqlite_autoindex_entries_1", 1, "pk", 0),
        ]
        assert connection.execute("PRAGMA index_info(sqlite_autoindex_entries_1)").fetchall() == [
            (0, 0, "key"),
        ]
    assert path.read_bytes().startswith(b"SQLite format 3\0")
    assert list(tmp_path.iterdir()) == [path]
    assert is_single_file(path)


def test_large_blob_key_lookups_and_listing_use_compact_covering_index(tmp_path):
    """Guard the index structure, not a machine-dependent timing threshold."""
    path = tmp_path / "large-chunks.nd2svs"
    payloads = {f"store.ome.zarr/0/{index:03d}": bytes([index]) * (130_000 + index * 300)
                for index in range(32)}
    with SQLiteStore(path) as store:
        with store.batch():
            for key, payload in payloads.items():
                store.write_bytes(key, payload)
        connection = store._connection
        for query, parameters in [
            ("SELECT 1 FROM entries WHERE key=?", (next(iter(payloads)),)),
            ("SELECT key FROM entries WHERE key>=? AND key<? ORDER BY key LIMIT ?",
             ("store.ome.zarr/", "store.ome.zarr0", 256)),
        ]:
            plan = connection.execute("EXPLAIN QUERY PLAN " + query, parameters).fetchall()
            assert any("COVERING INDEX sqlite_autoindex_entries_1" in row[3] for row in plan)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT value FROM entries WHERE key=?", (next(iter(payloads)),),
        ).fetchall()
        assert any("USING INDEX sqlite_autoindex_entries_1" in row[3] for row in plan)
        # The table really has rowids; the payload is absent from its key index.
        assert connection.execute("SELECT count(rowid) FROM entries").fetchone()[0] == len(payloads)
    with SQLiteStore(path, read_only=True) as store:
        assert list(store.list_keys()) == sorted(payloads)
        for key, payload in reversed(list(payloads.items())):
            assert store.read_bytes(key) == payload
            assert store.get_sync(key, byte_range=RangeByteRequest(100_000, 100_032)).to_bytes() == (
                payload[100_000:100_032]
            )


def _write_prototype_container(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE entries (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID"
        )
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=1")
        connection.executemany("INSERT INTO entries VALUES (?, ?)", [
            ("manifest.json", b'{"complete":true}'),
            ("store.ome.zarr/.zgroup", b'{"zarr_format":2}'),
            ("store.ome.zarr/.zattrs", b'{"prototype":true}'),
            ("store.ome.zarr/chunk", b"x" * 150_000),
        ])


def test_prototype_format_one_remains_read_only_without_in_place_upgrade(tmp_path):
    path = tmp_path / "prototype.nd2svs"
    _write_prototype_container(path)
    before = path.read_bytes()
    with SQLiteStore(path, read_only=True) as store:
        assert store.read_bytes("manifest.json") == b'{"complete":true}'
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 1
        with store.child("store.ome.zarr") as child:
            assert child.read_bytes("chunk") == b"x" * 150_000
            assert child.get_sync("chunk", byte_range=SuffixByteRequest(32)).to_bytes() == b"x" * 32
            assert list(child.list_keys()) == [".zattrs", ".zgroup", "chunk"]
        with pytest.raises(ValueError, match="read-only"):
            store.write_bytes("no", b"mutation")
        with pytest.raises(ValueError, match="rebuild"):
            store.with_read_only(False)
    assert read_entry(path, "manifest.json") == b'{"complete":true}'
    assert is_single_file(path)
    group = open_zarr_group(path)
    try:
        assert group.attrs["prototype"] is True
    finally:
        group.store.close()
    for open_writable in [
        lambda: SQLiteStore(path),
        lambda: write_entry(path, "no", b"mutation"),
        lambda: open_zarr_group(path, mode="r+"),
        lambda: open_zarr_group(path, mode="w"),
    ]:
        with pytest.raises(ValueError, match="rebuild"):
            open_writable()
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_prototype_write_is_refused_before_sqlite_opens_the_file(tmp_path, monkeypatch):
    path = tmp_path / "prototype.nd2svs"
    _write_prototype_container(path)
    before = path.read_bytes()

    def must_not_connect(*args, **kwargs):
        raise AssertionError("prototype write must not open SQLite or recover a journal")

    monkeypatch.setattr(sqlite3, "connect", must_not_connect)
    with pytest.raises(ValueError, match="rebuild"):
        SQLiteStore(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("prototype", [False, True])
def test_schema_version_and_actual_table_structure_must_match(tmp_path, prototype):
    path = tmp_path / "mismatched.nd2svs"
    if prototype:
        _write_prototype_container(path)
    else:
        write_entry(path, "manifest.json", b"{}")
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={FORMAT_VERSION if prototype else 1}")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        SQLiteStore(path, read_only=True)
    assert not is_single_file(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(("version", "application", "expected"), [
    (1, APPLICATION_ID, None),
    (FORMAT_VERSION, APPLICATION_ID, None),
    (FORMAT_VERSION + 1, APPLICATION_ID, FORMAT_VERSION + 1),
    (999, APPLICATION_ID, 999),
    (999, 123, None),
])
def test_future_format_header_guard_never_opens_sqlite(tmp_path, monkeypatch, version, application, expected):
    path = tmp_path / "future.nd2svs"
    write_entry(path, "manifest.json", b"{}")
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={version}")
        connection.execute(f"PRAGMA application_id={application}")
    before = path.read_bytes()

    def must_not_connect(*args, **kwargs):
        raise AssertionError("header preservation guard must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", must_not_connect)
    assert newer_file_format(path) == expected
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    assert newer_file_format(tmp_path / "missing.nd2svs") is None
    assert newer_file_format(tmp_path) is None


@pytest.mark.parametrize("contents", [b"", b"not SQLite" * 20, b"SQLite format 3\0short"])
def test_future_format_guard_ignores_unrecognized_headers(tmp_path, contents):
    path = tmp_path / "unrelated.nd2svs"
    path.write_bytes(contents)
    assert newer_file_format(path) is None
    assert path.read_bytes() == contents


def test_corrupt_zarr_metadata_normalizes_error_and_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.nd2svs"
    with SQLiteStore(path) as store:
        store.write_bytes("store.ome.zarr/.zgroup", b'{"zarr_format":2}')
        store.write_bytes("store.ome.zarr/.zattrs", json.dumps({"padding": "x" * 150_000}).encode())
        page_size = store._connection.execute("PRAGMA page_size").fetchone()[0]
        first_overflow = store._connection.execute("SELECT max(rootpage)+1 FROM sqlite_schema").fetchone()[0]
    damaged = bytearray(path.read_bytes())
    offset = (first_overflow - 1) * page_size
    # In this fresh two-entry fixture the first non-root page is the attrs
    # overflow chain. Break its next-page pointer, preserving header/index.
    assert 0 < int.from_bytes(damaged[offset:offset + 4], "big") < len(damaged) // page_size
    damaged[offset:offset + 4] = (len(damaged) // page_size + 999).to_bytes(4, "big")
    path.write_bytes(damaged)
    before = path.read_bytes()
    closed_connections = []
    real_close = SQLiteStore.close

    def record_close(self):
        if self._connection is not None:
            closed_connections.append(self._connection)
        real_close(self)

    monkeypatch.setattr(SQLiteStore, "close", record_close)
    with pytest.raises(ValueError, match="invalid or unreadable"):
        open_zarr_group(path)
    assert closed_connections
    for connection in closed_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert path.read_bytes() == before
    path.rename(tmp_path / "preserved-corrupt.nd2svs")


def test_exclusive_creation_never_adopts_an_existing_file(tmp_path):
    path = tmp_path / "cache.nd2svs"
    with SQLiteStore.create_exclusive(path) as store:
        store.write_bytes("winner", b"original")
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        SQLiteStore.create_exclusive(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("exclusive", [False, True])
def test_missing_database_creation_refuses_orphan_companions(tmp_path, suffix, symlink, exclusive):
    path = tmp_path / "new.nd2svs"
    companion = path.with_name(path.name + suffix)
    if symlink:
        # A dangling link must count as an existing companion too.
        try:
            companion.symlink_to(tmp_path / "missing-journal-target")
        except OSError:
            pytest.skip("symlink creation is unavailable")
    else:
        companion.write_bytes(b"preserve interrupted transaction")
    create = SQLiteStore.create_exclusive if exclusive else SQLiteStore
    with pytest.raises(FileExistsError, match="companion already exists"):
        create(path)
    assert not path.exists()
    if symlink:
        assert companion.is_symlink()
    else:
        assert companion.read_bytes() == b"preserve interrupted transaction"
    assert list(tmp_path.iterdir()) == [companion]


def test_existing_reader_can_observe_committed_data_during_active_rollback_journal(tmp_path):
    path = tmp_path / "live.nd2svs"
    with SQLiteStore(path) as writer:
        writer.write_bytes("value", b"committed")
        with writer.batch():
            writer.write_bytes("value", b"pending")
            assert path.with_name(path.name + "-journal").is_file()
            with SQLiteStore(path, read_only=True) as reader:
                assert reader.read_bytes("value") == b"committed"
        assert writer.read_bytes("value") == b"pending"


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("symlink", [False, True])
def test_rollback_header_with_wal_companion_is_refused_before_sqlite(tmp_path, monkeypatch, suffix, symlink):
    path = tmp_path / "rollback.nd2svs"
    write_entry(path, "manifest.json", b"{}")
    assert path.read_bytes()[18:20] == b"\x01\x01"
    companion = path.with_name(path.name + suffix)
    if symlink:
        try:
            companion.symlink_to(tmp_path / "missing-wal-target")
        except OSError:
            pytest.skip("symlink creation is unavailable")
    else:
        companion.write_bytes(b"preserve stray WAL state")
    before = path.read_bytes()
    before_names = sorted(file.name for file in tmp_path.iterdir())

    def must_not_connect(*args, **kwargs):
        raise AssertionError("a WAL companion must be rejected before SQLite can create -shm")

    monkeypatch.setattr(sqlite3, "connect", must_not_connect)
    for read_only in (True, False):
        with pytest.raises(ValueError, match="WAL companion"):
            SQLiteStore(path, read_only=read_only)
    assert path.read_bytes() == before
    assert sorted(file.name for file in tmp_path.iterdir()) == before_names
    if symlink:
        assert companion.is_symlink()
    else:
        assert companion.read_bytes() == b"preserve stray WAL state"


def test_synthetic_zarr_v2_create_partial_read_and_reopen(tmp_path):
    path = tmp_path / "sample.nd2svs"
    expected = np.arange(2 * 13 * 15, dtype=np.uint16).reshape(2, 13, 15)
    group = open_zarr_group(path, mode="w")
    try:
        group.attrs.update({"multiscales": [], "nd2wsi": {"source": "원본.nd2"}})
        array = group.create_array(
            "0", shape=expected.shape, chunks=(1, 4, 4), dtype=expected.dtype,
            compressors=Blosc(cname="zstd", clevel=3),
        )
        array[:] = expected
        assert np.array_equal(array[:, 3:11, 5:13], expected[:, 3:11, 5:13])
        assert json.loads(group.store.read_bytes("0/.zarray"))["zarr_format"] == 2
    finally:
        group.store.close()
    reopened = open_zarr_group(path)
    try:
        assert reopened.attrs["nd2wsi"]["source"] == "원본.nd2"
        assert np.array_equal(reopened["0"][:], expected)
    finally:
        reopened.store.close()
    assert list(tmp_path.iterdir()) == [path]


def test_encoded_legacy_chunks_are_copied_without_reencoding(tmp_path):
    source = tmp_path / "legacy.ome.zarr"
    expected = np.arange(2 * 8 * 9, dtype=np.uint16).reshape(2, 8, 9)
    legacy = zarr.open_group(source, mode="w", zarr_format=2)
    array = legacy.create_array("0", shape=expected.shape, chunks=(1, 4, 4), dtype="u2")
    array[:] = expected
    legacy.store.close()
    path = tmp_path / "migrated.nd2svs"
    with SQLiteStore(path) as store:
        with store.batch():
            store.write_bytes("manifest.json", b"{}")
            for file in source.rglob("*"):
                if file.is_file():
                    store.write_bytes("store.ome.zarr/" + file.relative_to(source).as_posix(),
                                      file.read_bytes())
        for file in source.rglob("*"):
            if file.is_file():
                assert store.read_bytes("store.ome.zarr/" + file.relative_to(source).as_posix()) == (
                    file.read_bytes()
                )
    group = open_zarr_group(path)
    try:
        assert np.array_equal(group["0"][:], expected)
    finally:
        group.store.close()


@pytest.mark.parametrize(("byte_request", "expected"), [
    (None, b"0123456789"),
    (RangeByteRequest(2, 5), b"234"),
    (RangeByteRequest(7, 99), b"789"),
    (RangeByteRequest(10, 12), b""),
    (RangeByteRequest(3, 3), b""),
    (OffsetByteRequest(4), b"456789"),
    (OffsetByteRequest(11), b""),
    (SuffixByteRequest(3), b"789"),
    (SuffixByteRequest(99), b"0123456789"),
    (SuffixByteRequest(0), b""),
])
def test_partial_byte_requests(tmp_path, byte_request, expected):
    with SQLiteStore(tmp_path / "cache.nd2svs") as store:
        store.write_bytes("chunk", b"0123456789")
        result = run(store.get("chunk", byte_range=byte_request))
        assert result.to_bytes() == expected
        assert store.get_sync("chunk", byte_range=byte_request).to_bytes() == expected
        assert run(store.get("missing", byte_range=byte_request)) is None


def test_partial_reads_issue_substr_not_full_value_queries(tmp_path):
    with SQLiteStore(tmp_path / "cache.nd2svs") as store:
        store.write_bytes("chunk", b"x" * (4 * 1024 * 1024))
        statements = []
        store._connection.set_trace_callback(statements.append)
        assert store.get_sync("chunk", byte_range=RangeByteRequest(100, 132)).to_bytes() == b"x" * 32
        assert statements == ["SELECT substr(value,101,32) FROM entries WHERE key='chunk'"]


def test_partial_values_order_missing_and_size(tmp_path):
    prototype = default_buffer_prototype()
    with SQLiteStore(tmp_path / "cache.nd2svs") as store:
        run(store.set("x", prototype.buffer.from_bytes(b"abcdef")))
        result = run(store.get_partial_values(prototype, [
            ("x", RangeByteRequest(1, 3)), ("missing", None), ("x", SuffixByteRequest(1)),
        ]))
        assert [value.to_bytes() if value is not None else None for value in result] == [
            b"bc", None, b"f",
        ]
        assert run(store.getsize("x")) == 6
        assert run(store.getsize_prefix("")) == 6
        with pytest.raises(FileNotFoundError):
            run(store.getsize("missing"))
        with pytest.raises(ValueError):
            run(store.get("x", byte_range=RangeByteRequest(-1, 2)))
        with pytest.raises(TypeError):
            run(store.get("x", byte_range=(1, 2)))


def test_overwrite_delete_empty_values_and_atomic_set_if_absent(tmp_path):
    prototype = default_buffer_prototype()
    with SQLiteStore(tmp_path / "cache.nd2svs") as store:
        store.write_bytes("key", b"first")
        store.write_bytes("key", bytearray(b"second"))
        assert store.read_bytes("key") == b"second"
        run(store.set_if_not_exists("key", prototype.buffer.from_bytes(b"ignored")))
        assert store.read_bytes("key") == b"second"
        run(store.set_if_not_exists("new", prototype.buffer.from_bytes(b"inserted")))
        assert store.read_bytes("new") == b"inserted"
        store.write_bytes("empty", memoryview(b""))
        assert store.read_bytes("empty") == b""
        assert run(store.exists("empty"))
        run(store.delete("key"))
        store.delete_sync("key")
        assert not run(store.exists("key"))
        with pytest.raises(FileNotFoundError):
            store.read_bytes("key")


def test_prefix_children_listing_and_clear_do_not_escape_scope(tmp_path):
    path = tmp_path / "cache.nd2svs"
    with SQLiteStore(path) as root, root.child("store.ome.zarr/") as child:
        root.write_bytes("manifest.json", b"{}")
        root.write_bytes("store.ome.zarr-other/keep", b"other")
        child.write_bytes(".zgroup", b"{}")
        child.write_bytes("0/.zarray", b"{}")
        child.write_bytes("0/0.0.0", b"chunk")
        child.write_bytes("01/.zarray", b"{}")
        child.write_bytes("wild%_/keep", b"wild")
        assert list(child.list_keys("0/")) == ["0/.zarray", "0/0.0.0"]
        assert list(child.list_keys("wild%_")) == ["wild%_/keep"]
        assert run(collect(child.list_dir(""))) == [".zgroup", "0", "01", "wild%_"]
        assert run(collect(child.list_dir("0"))) == [".zarray", "0.0.0"]
        assert run(collect(child.list())) == list(child.list_keys())
        assert run(child.getsize_prefix("0")) == 7
        run(child.delete_dir("0"))
        assert not list(child.list_keys("0/"))
        assert child.read_bytes("01/.zarray") == b"{}"
        run(child.clear())
        assert list(child.list_keys()) == []
        assert root.read_bytes("manifest.json") == b"{}"
        assert root.read_bytes("store.ome.zarr-other/keep") == b"other"


def test_two_zarr_namespaces_and_overwrite_preserve_manifest_and_sibling(tmp_path):
    path = tmp_path / "cache.nd2svs"
    write_entry(path, "manifest.json", b"metadata")
    for prefix in ("store.ome.zarr", "thumbs.zarr"):
        group = open_zarr_group(path, prefix, "w")
        group.create_array("0", shape=(2,), dtype="u1")[:] = [2, 4]
        group.store.close()
    group = open_zarr_group(path, "thumbs.zarr", "w")
    try:
        assert list(group.array_keys()) == []
    finally:
        group.store.close()
    group = open_zarr_group(path)
    try:
        assert list(group["0"][:]) == [2, 4]
    finally:
        group.store.close()
    assert read_entry(path, "manifest.json") == b"metadata"


def test_listing_is_paged_and_keeps_sorted_relative_keys(tmp_path):
    with SQLiteStore(tmp_path / "cache.nd2svs", prefix="data") as store:
        with store.batch():
            for index in range(700):
                store.write_bytes(f"key-{index:04d}", b"v")
        assert list(store.list_keys()) == [f"key-{index:04d}" for index in range(700)]
        assert run(collect(store.list_prefix("key-06"))) == [
            f"key-{index:04d}" for index in range(600, 700)
        ]


def test_batch_commit_and_rollback_including_overwrite_and_delete(tmp_path):
    path = tmp_path / "cache.nd2svs"
    with SQLiteStore(path) as store:
        store.write_bytes("old", b"original")
        with pytest.raises(RuntimeError, match="abort"):
            with store.batch():
                store.write_bytes("old", b"changed")
                store.write_bytes("new", b"temporary")
                store.delete_sync("old")
                raise RuntimeError("abort")
        assert store.read_bytes("old") == b"original"
        assert not run(store.exists("new"))
        with store.batch():
            store.write_bytes("old", b"committed")
            store.write_bytes("new", b"committed too")
        with SQLiteStore(path, read_only=True) as reader:
            assert reader.read_bytes("old") == b"committed"
            assert reader.read_bytes("new") == b"committed too"
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_reads_and_writes_share_no_cursor_or_transaction_state(tmp_path):
    path = tmp_path / "cache.nd2svs"
    with SQLiteStore(path) as store:
        with store.batch():
            for index in range(50):
                store.write_bytes(f"source/{index}", bytes([index]) * 64)

        def read_and_write(worker):
            for index in range(50):
                assert store.read_bytes(f"source/{index}") == bytes([index]) * 64
                store.write_bytes(f"worker/{worker}/{index}", bytes([worker, index]))

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(read_and_write, range(6)))
        assert len(list(store.list_keys("worker/"))) == 300
    with SQLiteStore(path, read_only=True) as reader:
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(lambda index: reader.read_bytes(f"source/{index % 50}"), range(200)))
        assert result == [bytes([index % 50]) * 64 for index in range(200)]


def test_read_only_missing_closed_and_independent_child_lifetime(tmp_path):
    path = tmp_path / "cache.nd2svs"
    with pytest.raises(FileNotFoundError):
        SQLiteStore(path, read_only=True)
    assert not path.exists()
    write_entry(path, "a/value", b"value")
    before = hashlib.sha256(path.read_bytes()).digest()
    with SQLiteStore(path, read_only=True) as root:
        child = root.child("a")
        assert child.read_bytes("value") == b"value"
        with pytest.raises(ValueError, match="read-only"):
            root.write_bytes("new", b"no")
        with pytest.raises(ValueError, match="read-only"):
            root.delete_sync("a/value")
        with pytest.raises(ValueError, match="read-only"):
            with root.batch():
                pass
    assert child.read_bytes("value") == b"value"
    child.close()
    child.close()
    with pytest.raises(ValueError, match="closed"):
        child.read_bytes("value")
    with pytest.raises(ValueError, match="closed"):
        run(child.get("value"))
    assert hashlib.sha256(path.read_bytes()).digest() == before


@pytest.mark.parametrize("content", [b"", b"not a SQLite database", b"SQLite format 3\0broken"])
def test_unknown_existing_files_are_never_initialized_or_modified(tmp_path, content):
    path = tmp_path / "unknown.nd2svs"
    path.write_bytes(content)
    assert not is_single_file(path)
    for read_only in (False, True):
        with pytest.raises(ValueError):
            SQLiteStore(path, read_only=read_only)
        assert path.read_bytes() == content
    with pytest.raises(ValueError):
        read_entry(path, "manifest.json")
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("change", [
    "PRAGMA application_id=123",
    "PRAGMA user_version=999",
    "CREATE TABLE extra(value TEXT)",
    "CREATE VIEW impostor AS SELECT * FROM entries",
    "CREATE TRIGGER injected AFTER INSERT ON entries BEGIN DELETE FROM entries; END",
    "CREATE INDEX extra_index ON entries(value)",
])
def test_unsupported_header_or_extra_schema_is_refused_without_mutation(tmp_path, change):
    path = tmp_path / "untrusted.nd2svs"
    write_entry(path, "manifest.json", b"{}")
    with sqlite3.connect(path) as connection:
        connection.execute(change)
    before = path.read_bytes()
    assert not is_single_file(path)
    with pytest.raises(ValueError):
        SQLiteStore(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("key", ["", "/absolute", "../escape", "a/../b", "a/./b",
                                      "a//b", "a/", "a\x00b", "C:/absolute", "a\\b"])
def test_invalid_keys_are_rejected_for_every_byte_operation(tmp_path, key):
    with SQLiteStore(tmp_path / "cache.nd2svs") as store:
        for operation in (store.read_bytes, store.delete_sync, lambda k: store.write_bytes(k, b"x")):
            with pytest.raises(ValueError):
                operation(key)
        assert list(store.list_keys()) == []


def test_unicode_prefix_boundaries_and_invalid_surrogates(tmp_path):
    with SQLiteStore(tmp_path / "cache.nd2svs") as store:
        store.write_bytes("\ud7ff/key", b"before-surrogates")
        store.write_bytes("\ue000/key", b"after-surrogates")
        store.write_bytes("\U0010ffff/key", b"last-codepoint")
        assert list(store.list_keys("\ud7ff")) == ["\ud7ff/key"]
        assert list(store.list_keys("\U0010ffff")) == ["\U0010ffff/key"]
        with pytest.raises(ValueError, match="Unicode"):
            store.write_bytes("\ud800", b"invalid")


def test_wal_database_is_refused_before_sqlite_can_create_sidecars(tmp_path, monkeypatch):
    path = tmp_path / "wal.nd2svs"
    write_entry(path, "manifest.json", b"{}")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.close()
    before = {file.name: file.read_bytes() for file in tmp_path.iterdir()}

    def must_not_connect(*args, **kwargs):
        raise AssertionError("unrecognized headers must be refused before SQLite opens them")

    monkeypatch.setattr(sqlite3, "connect", must_not_connect)
    assert not is_single_file(path)
    with pytest.raises(ValueError):
        SQLiteStore(path)
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == before


@pytest.mark.parametrize("prefix", ["/", "../escape", "C:/root", "a\\b", "a/../b"])
def test_invalid_prefix_is_rejected_before_file_creation(tmp_path, prefix):
    path = tmp_path / "absent.nd2svs"
    with pytest.raises(ValueError):
        SQLiteStore(path, prefix=prefix)
    assert not path.exists()


def test_unicode_uri_characters_and_long_portable_path(tmp_path):
    subtree = tmp_path / ("nested-" + "a" * 90)
    # '?' is a valid POSIX filename character but is rejected by Windows
    # before SQLite opens it. Keep URI escaping coverage legal on each OS.
    filename = "원본 # % &.nd2svs" if os.name == "nt" else "원본 # ? % &.nd2svs"
    path = subtree / ("selection-" + "b" * 90) / ("stage-" + "c" * 80) / filename
    assert len(str(path)) > 260
    try:
        write_entry(path, "한글/μ/.zattrs", "원본 이름".encode())
        assert read_entry(path, "한글/μ/.zattrs") == "원본 이름".encode()
        assert is_single_file(path)
        assert not str(path).startswith("\\\\?\\")
        uri = _database_uri(path, "ro")
        assert urlsplit(uri).query == "mode=ro"
        assert unquote(uri[5:].rsplit("?", 1)[0]) == filesystem_path(path)
    finally:
        import shutil

        shutil.rmtree(filesystem_path(subtree))


def test_helpers_open_modes_read_only_cloning_and_buffer_type_validation(tmp_path):
    path = tmp_path / "cache.nd2svs"
    with pytest.raises(FileNotFoundError):
        open_zarr_group(path, mode="r+")
    with pytest.raises(ValueError):
        open_zarr_group(path, mode="invalid")
    assert not path.exists()
    with SQLiteStore(path) as store:
        store.write_bytes("key", b"value")
        with store.with_read_only(True) as readonly:
            assert readonly == store
            assert readonly.read_bytes("key") == b"value"
            with pytest.raises(ValueError):
                run(readonly.clear())
        with pytest.raises(TypeError):
            store.set_sync("bad", b"not a Buffer")
        with pytest.raises(TypeError):
            store.write_bytes("bad", "not bytes")
        with store.batch():
            with pytest.raises(RuntimeError, match="nested"):
                with store.batch():
                    pass
            with pytest.raises(RuntimeError, match="inside"):
                store.close()
