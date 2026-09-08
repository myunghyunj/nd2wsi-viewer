"""A bounded-memory SQLite container for unchanged, encoded Zarr v2 objects.

The database is a key/value store, not a second array encoding. A container can
hold ``manifest.json``, ``store.ome.zarr/`` and ``thumbs.zarr/`` together. Nothing
is extracted to the filesystem. Each write is durable and atomic; migration
can group synchronous writes with ``with store.batch():``.
"""

from __future__ import annotations

import asyncio
import errno
import ntpath
import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from zarr.abc.store import OffsetByteRequest, RangeByteRequest, Store, SuffixByteRequest
from zarr.core.buffer import Buffer, BufferPrototype, default_buffer_prototype

from ..platform_io import filesystem_path

APPLICATION_ID = 0x4E443253
FORMAT_VERSION = 2
# Keep the search B-tree small: a WITHOUT ROWID table puts the large encoded
# chunk in its index payload, so even key comparisons can read overflow BLOBs.
# A rowid table's implicit UNIQUE key index contains only (key, rowid).
_SCHEMA = "CREATE TABLE entries (key TEXT PRIMARY KEY NOT NULL, value BLOB NOT NULL)"
_LEGACY_SCHEMA = "CREATE TABLE entries (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID"
_READABLE_VERSIONS = frozenset({1, FORMAT_VERSION})
_COMPANION_SUFFIXES = ("-journal", "-wal", "-shm")
_PAGE_KEYS = 256


def _normalize_key(key: str, *, prefix: bool = False) -> str:
    if not isinstance(key, str):
        raise TypeError("store keys must be strings")
    try:
        key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("store keys must be valid Unicode") from exc
    if "\x00" in key or "\\" in key or key.startswith("/") or ntpath.splitdrive(key)[0]:
        raise ValueError(f"invalid store key: {key!r}")
    value = key.rstrip("/") if prefix else key
    if not value:
        if prefix and not key:
            return ""
        raise ValueError("store keys must not be empty")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"invalid store key: {key!r}")
    return value + ("/" if prefix and key.endswith("/") else "")


def _database_uri(path: Path, mode: str) -> str:
    # Encoding native backslashes avoids treating a Windows extended/UNC path
    # as a URI authority. SQLite decodes the path before handing it to its VFS.
    # Literal ?, # and % in a source name can never become URI parameters.
    return f"file:{quote(filesystem_path(path), safe='/:')}?mode={mode}"


def _prefix_predicate(prefix: str) -> tuple[str, tuple[str, ...]]:
    if not prefix:
        return "1", ()
    for index in range(len(prefix) - 1, -1, -1):
        if ord(prefix[index]) < 0x10FFFF:
            next_codepoint = ord(prefix[index]) + 1
            if next_codepoint == 0xD800:
                next_codepoint = 0xE000  # Skip the non-UTF-8 surrogate range.
            upper = prefix[:index] + chr(next_codepoint)
            return "key >= ? AND key < ?", (prefix, upper)
    return "key >= ?", (prefix,)


class SQLiteStore(Store):
    """Closeable Zarr store backed by one validated SQLite database.

    Child stores have independent connections and lifetimes. Each connection
    serializes access with a lock, uses at most an 8 MiB SQLite page-cache target,
    and disables memory mapping. ``batch`` is for synchronous byte writes only;
    do not call Zarr's asynchronous/synchronous array API inside its context.
    Prototype format 1 is accepted read-only for verification/recovery; new and
    writable containers require format 2 and never upgrade a file in place.
    """

    supports_writes = True
    supports_deletes = True
    supports_listing = True
    supports_partial_writes = False

    def __init__(
        self, path: str | os.PathLike, *, read_only: bool = False, prefix: str = "",
        _exclusive: bool = False,
    ):
        super().__init__(read_only=read_only)
        self.path = self.root = Path(os.path.abspath(os.fspath(path)))
        self.prefix = _normalize_key(prefix, prefix=True).rstrip("/")
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self._in_batch = False
        self._exclusive = _exclusive
        self._ensure_open_sync()

    @classmethod
    def create_exclusive(cls, path: str | os.PathLike, *, prefix: str = "") -> SQLiteStore:
        """Atomically create a new container; never adopt an existing pathname."""
        return cls(path, prefix=prefix, _exclusive=True)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SQLiteStore) and (self.path, self.prefix) == (
            other.path, other.prefix,
        )

    def __repr__(self) -> str:
        return f"SQLiteStore({str(self.path)!r}, read_only={self.read_only}, prefix={self.prefix!r})"

    def __str__(self) -> str:
        return f"sqlite:{self.path}#{self.prefix}"

    def with_read_only(self, read_only: bool = False) -> SQLiteStore:
        return type(self)(self.path, read_only=read_only, prefix=self.prefix)

    def child(self, prefix: str) -> SQLiteStore:
        relative = _normalize_key(prefix, prefix=True).rstrip("/")
        combined = "/".join(part for part in (self.prefix, relative) if part)
        return type(self)(self.path, read_only=self.read_only, prefix=combined)

    @staticmethod
    def _validate(connection: sqlite3.Connection, *, read_only: bool) -> None:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID or version not in _READABLE_VERSIONS:
            raise ValueError("not a supported nd2wsi single-file cache (application ID/version)")
        if version != FORMAT_VERSION and not read_only:
            raise ValueError("prototype single-file cache is read-only; rebuild it as format 2")
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name LIMIT 3"
        ).fetchall()
        expected = [("table", "entries", "entries")]
        if version == FORMAT_VERSION:
            expected.append(("index", "sqlite_autoindex_entries_1", "entries"))
        if [row[:3] for row in schema] != expected:
            raise ValueError("unsupported single-file cache schema")
        normalized = "".join((schema[0][3] or "").split()).lower()
        table_schema = _SCHEMA if version == FORMAT_VERSION else _LEGACY_SCHEMA
        if normalized != "".join(table_schema.split()).lower():
            raise ValueError("unsupported single-file cache schema")
        if version == FORMAT_VERSION and schema[1][3] is not None:
            raise ValueError("unsupported single-file cache schema")

    def _ensure_open_sync(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise ValueError("SQLiteStore is closed")
            if self._connection is not None:
                return self._connection
            native = filesystem_path(self.path)
            created = False
            if self._exclusive or not os.path.exists(native):
                if self.read_only:
                    raise FileNotFoundError(self.path)
                # Never let a new database inherit a previous database's hot
                # journal/WAL. Existing database opens still use SQLite's own
                # recovery/concurrency protocol, including active journals.
                for suffix in _COMPANION_SUFFIXES:
                    companion = native + suffix
                    if os.path.lexists(companion):
                        raise FileExistsError(
                            errno.EEXIST,
                            "SQLite companion already exists; preserve/recover it before creating a cache",
                            companion,
                        )
                os.makedirs(filesystem_path(self.path.parent), exist_ok=True)
                try:
                    fd = os.open(native, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                except FileExistsError:
                    if self._exclusive:
                        raise
                    pass  # Validate the winner; never adopt an unknown existing file.
                else:
                    os.close(fd)
                    created = True
            if not created:
                # Check identity before asking SQLite to open untrusted data.
                # In particular, opening a WAL database can create a -shm file
                # even in mode=ro; this format only accepts rollback journals.
                with open(native, "rb") as source:
                    header = source.read(100)
                version = int.from_bytes(header[60:64], "big")
                if (
                    len(header) != 100 or header[:16] != b"SQLite format 3\0"
                    or header[18:20] != b"\x01\x01"
                    or version not in _READABLE_VERSIONS
                    or int.from_bytes(header[68:72], "big") != APPLICATION_ID
                ):
                    raise ValueError("not a supported nd2wsi single-file cache (header)")
                if version != FORMAT_VERSION and not self.read_only:
                    raise ValueError("prototype single-file cache is read-only; rebuild it as format 2")
                # SQLite may create -shm when a stray -wal exists even if the
                # main header still says rollback mode. This format never uses
                # WAL; refuse those companions before any SQLite connection.
                # A normal active -journal remains supported for readers.
                if any(os.path.lexists(native + suffix) for suffix in ("-wal", "-shm")):
                    raise ValueError("unsupported WAL companion beside single-file cache; preserve and recover it")
            connection = sqlite3.connect(
                _database_uri(self.path, "ro" if self.read_only else "rw"),
                uri=True, timeout=30, isolation_level=None, check_same_thread=False,
            )
            try:
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA cache_size=-8192")
                connection.execute("PRAGMA mmap_size=0")
                connection.execute("PRAGMA temp_store=FILE")
                if hasattr(connection, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
                    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
                if not created:
                    self._validate(connection, read_only=self.read_only)
                if not self.read_only:
                    mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                    if mode.lower() != "delete":
                        raise ValueError("single-file caches require DELETE journal mode")
                    connection.execute("PRAGMA synchronous=FULL")
                if created:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(_SCHEMA)
                        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version={FORMAT_VERSION}")
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    self._validate(connection, read_only=self.read_only)
            except sqlite3.Error as exc:
                connection.close()
                raise ValueError(f"invalid or unreadable single-file cache: {self.path}") from exc
            except BaseException:
                connection.close()
                raise
            self._connection = connection
            self._is_open = True
            return connection

    async def _open(self) -> None:
        await asyncio.to_thread(self._ensure_open_sync)

    def close(self) -> None:
        with self._lock:
            if self._in_batch:
                raise RuntimeError("cannot close a SQLiteStore inside its batch")
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._closed = True
            self._is_open = False

    def __del__(self):
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()

    def _key(self, key: str) -> str:
        key = _normalize_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def _listing_prefix(self, prefix: str) -> str:
        prefix = _normalize_key(prefix, prefix=True)
        return f"{self.prefix}/{prefix}" if self.prefix else prefix

    @contextmanager
    def batch(self) -> Iterator[SQLiteStore]:
        """Commit all synchronous writes together, or roll all of them back."""
        with self._lock:
            connection = self._ensure_open_sync()
            self._check_writable()
            if self._in_batch:
                raise RuntimeError("nested SQLiteStore batches are not supported")
            connection.execute("BEGIN IMMEDIATE")
            self._in_batch = True
            try:
                yield self
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                self._in_batch = False

    def _write(self, sql: str, parameters: tuple) -> None:
        with self._lock:
            connection = self._ensure_open_sync()
            self._check_writable()
            if self._in_batch:
                connection.execute(sql, parameters)
            else:
                with self.batch():
                    connection.execute(sql, parameters)

    def write_bytes(self, key: str, payload: bytes | bytearray | memoryview) -> None:
        key = self._key(key)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("store payloads must be bytes-like")
        self._write(
            "INSERT INTO entries(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, payload),
        )

    def _read_bytes(self, key: str, byte_range=None) -> bytes:
        full_key = self._key(key)
        if byte_range is None:
            sql, parameters = "SELECT value FROM entries WHERE key=?", (full_key,)
        elif isinstance(byte_range, RangeByteRequest):
            if byte_range.start < 0 or byte_range.end < byte_range.start:
                raise ValueError("invalid byte range")
            sql = "SELECT substr(value,?,?) FROM entries WHERE key=?"
            parameters = (byte_range.start + 1, byte_range.end - byte_range.start, full_key)
        elif isinstance(byte_range, OffsetByteRequest):
            if byte_range.offset < 0:
                raise ValueError("byte offset must be nonnegative")
            sql, parameters = "SELECT substr(value,?) FROM entries WHERE key=?", (
                byte_range.offset + 1, full_key,
            )
        elif isinstance(byte_range, SuffixByteRequest):
            if byte_range.suffix < 0:
                raise ValueError("suffix size must be nonnegative")
            if byte_range.suffix == 0:
                sql, parameters = "SELECT substr(value,1,0) FROM entries WHERE key=?", (full_key,)
            else:
                sql, parameters = "SELECT substr(value,?) FROM entries WHERE key=?", (
                    -byte_range.suffix, full_key,
                )
        else:
            raise TypeError(f"unsupported byte range: {byte_range!r}")
        with self._lock:
            row = self._ensure_open_sync().execute(sql, parameters).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        if not isinstance(row[0], bytes):
            raise ValueError("single-file cache entry is not a BLOB")
        return row[0]

    def read_bytes(self, key: str) -> bytes:
        return self._read_bytes(key)

    def get_sync(self, key: str, *, prototype=None, byte_range=None) -> Buffer | None:
        prototype = prototype or default_buffer_prototype()
        try:
            value = self._read_bytes(key, byte_range)
        except FileNotFoundError:
            return None
        return prototype.buffer.from_bytes(value)

    async def get(self, key: str, prototype=None, byte_range=None) -> Buffer | None:
        return await asyncio.to_thread(self.get_sync, key, prototype=prototype, byte_range=byte_range)

    async def get_partial_values(
        self, prototype: BufferPrototype, key_ranges: Iterable[tuple[str, object]],
    ) -> list[Buffer | None]:
        # Sequential requests bound in-flight memory even for a large iterator.
        return [await self.get(key, prototype, request) for key, request in key_ranges]

    def set_sync(self, key: str, value: Buffer) -> None:
        if not isinstance(value, Buffer):
            raise TypeError("Zarr values must be Buffer instances")
        self.write_bytes(key, value.to_bytes())

    async def set(self, key: str, value: Buffer) -> None:
        await asyncio.to_thread(self.set_sync, key, value)

    async def set_if_not_exists(self, key: str, value: Buffer) -> None:
        full_key = self._key(key)
        if not isinstance(value, Buffer):
            raise TypeError("Zarr values must be Buffer instances")
        await asyncio.to_thread(
            self._write, "INSERT OR IGNORE INTO entries(key,value) VALUES(?,?)",
            (full_key, value.to_bytes()),
        )

    def delete_sync(self, key: str) -> None:
        self._write("DELETE FROM entries WHERE key=?", (self._key(key),))

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.delete_sync, key)

    def _exists(self, key: str) -> bool:
        full_key = self._key(key)
        with self._lock:
            return self._ensure_open_sync().execute(
                "SELECT 1 FROM entries WHERE key=?", (full_key,),
            ).fetchone() is not None

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._exists, key)

    def _list_page(self, prefix: str, after: str) -> list[str]:
        predicate, parameters = _prefix_predicate(prefix)
        with self._lock:
            rows = self._ensure_open_sync().execute(
                f"SELECT key FROM entries WHERE ({predicate}) AND key>? ORDER BY key LIMIT ?",
                (*parameters, after, _PAGE_KEYS),
            ).fetchall()
        return [row[0] for row in rows]

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Yield relative keys in sorted, bounded pages (not a full-store list)."""
        full_prefix = self._listing_prefix(prefix)
        strip = len(self.prefix) + 1 if self.prefix else 0
        after = ""
        while page := self._list_page(full_prefix, after):
            for key in page:
                yield key[strip:]
            after = page[-1]

    async def list(self) -> AsyncIterator[str]:
        async for key in self.list_prefix(""):
            yield key

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        full_prefix = self._listing_prefix(prefix)
        strip = len(self.prefix) + 1 if self.prefix else 0
        after = ""
        while page := await asyncio.to_thread(self._list_page, full_prefix, after):
            for key in page:
                yield key[strip:]
            after = page[-1]

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        relative = _normalize_key(prefix, prefix=True).rstrip("/")
        search = relative + "/" if relative else ""
        previous = None
        async for key in self.list_prefix(search):
            name = key[len(search):].split("/", 1)[0]
            if name != previous:
                yield name
                previous = name

    async def delete_dir(self, prefix: str) -> None:
        relative = _normalize_key(prefix, prefix=True).rstrip("/")
        full_prefix = self._listing_prefix(relative + "/" if relative else "")
        predicate, parameters = _prefix_predicate(full_prefix)
        await asyncio.to_thread(self._write, f"DELETE FROM entries WHERE {predicate}", parameters)

    def _getsize(self, key: str) -> int:
        full_key = self._key(key)
        with self._lock:
            row = self._ensure_open_sync().execute(
                "SELECT length(value) FROM entries WHERE key=?", (full_key,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        return row[0]

    async def getsize(self, key: str) -> int:
        return await asyncio.to_thread(self._getsize, key)

    def _getsize_prefix(self, prefix: str) -> int:
        relative = _normalize_key(prefix, prefix=True).rstrip("/")
        full_prefix = self._listing_prefix(relative + "/" if relative else "")
        predicate, parameters = _prefix_predicate(full_prefix)
        with self._lock:
            row = self._ensure_open_sync().execute(
                f"SELECT coalesce(sum(length(value)),0) FROM entries WHERE {predicate}", parameters,
            ).fetchone()
        return row[0]

    async def getsize_prefix(self, prefix: str) -> int:
        return await asyncio.to_thread(self._getsize_prefix, prefix)


def read_entry(path: str | os.PathLike, key: str) -> bytes:
    try:
        with SQLiteStore(path, read_only=True) as store:
            return store.read_bytes(key)
    except sqlite3.Error as exc:
        raise ValueError(f"invalid or unreadable single-file cache: {path}") from exc


def write_entry(path: str | os.PathLike, key: str, payload: bytes | bytearray | memoryview) -> None:
    with SQLiteStore(path) as store:
        store.write_bytes(key, payload)


def is_single_file(path: str | os.PathLike) -> bool:
    try:
        with SQLiteStore(path, read_only=True):
            return True
    except (OSError, ValueError, sqlite3.Error):
        return False


def newer_file_format(path: str | os.PathLike) -> int | None:
    """Recognize a future owned format from its header without opening SQLite.

    This is a preservation guard, not schema validation. It also works when
    the future schema or journal mode cannot be interpreted by this build.
    """
    try:
        with open(filesystem_path(path), "rb") as source:
            header = source.read(100)
    except OSError:
        return None
    if (
        len(header) != 100 or header[:16] != b"SQLite format 3\0"
        or int.from_bytes(header[68:72], "big") != APPLICATION_ID
    ):
        return None
    version = int.from_bytes(header[60:64], "big")
    return version if version > FORMAT_VERSION else None


def open_zarr_group(path: str | os.PathLike, prefix: str = "store.ome.zarr", mode: str = "r"):
    """Open an embedded v2 group; the caller owns ``group.store.close()``."""
    import zarr

    if mode not in {"r", "r+", "a", "w", "w-", "x"}:
        raise ValueError(f"unsupported Zarr open mode: {mode!r}")
    if mode in {"r", "r+"} and not os.path.exists(filesystem_path(path)):
        raise FileNotFoundError(path)
    store = SQLiteStore(path, read_only=mode == "r", prefix=prefix)
    try:
        return zarr.open_group(store=store, mode=mode, zarr_format=2)
    except sqlite3.Error as exc:
        store.close()
        raise ValueError(f"invalid or unreadable single-file cache: {path}") from exc
    except BaseException:
        store.close()
        raise
