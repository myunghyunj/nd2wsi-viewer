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
import stat
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from zarr.abc.store import OffsetByteRequest, RangeByteRequest, Store, SuffixByteRequest
from zarr.core.buffer import Buffer, BufferPrototype, default_buffer_prototype

from ..platform_io import filesystem_path
from .zarr_v2 import ZarrV2RegionReader

APPLICATION_ID = 0x4E443253
FORMAT_VERSION = 2
# Corrected caches need a capability barrier even when opened directly. Raw
# caches keep version 2 so revocation restores the historical readable format.
CORRECTED_FORMAT_VERSION = 3
MAX_READABLE_VERSION = CORRECTED_FORMAT_VERSION
# Keep the search B-tree small: a WITHOUT ROWID table puts the large encoded
# chunk in its index payload, so even key comparisons can read overflow BLOBs.
# A rowid table's implicit UNIQUE key index contains only (key, rowid).
_SCHEMA = "CREATE TABLE entries (key TEXT PRIMARY KEY NOT NULL, value BLOB NOT NULL)"
_LEGACY_SCHEMA = "CREATE TABLE entries (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID"
_READABLE_VERSIONS = frozenset({1, FORMAT_VERSION, CORRECTED_FORMAT_VERSION})
_WRITABLE_VERSIONS = frozenset({FORMAT_VERSION, CORRECTED_FORMAT_VERSION})
_COMPANION_SUFFIXES = ("-journal", "-wal", "-shm")
_PAGE_KEYS = 256
_RECOVERY_TIMEOUT = 1.0


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
    writable containers accept formats 2 and 3 without upgrading in place.
    Creation defaults to format 2; corrected-cache writers opt into format 3.
    ``recover=True`` explicitly permits rollback recovery during a read-only
    open; ordinary inspection and Agent shared-cache reads must leave it false.
    """

    supports_writes = True
    supports_deletes = True
    supports_listing = True
    supports_partial_writes = False

    def __init__(
        self, path: str | os.PathLike, *, read_only: bool = False, prefix: str = "",
        recover: bool = False, _exclusive: bool = False, format_version: int = FORMAT_VERSION,
    ):
        super().__init__(read_only=read_only)
        self.path = self.root = Path(os.path.abspath(os.fspath(path)))
        self.prefix = _normalize_key(prefix, prefix=True).rstrip("/")
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self._in_batch = False
        self._exclusive = _exclusive
        self._recover = recover
        if format_version not in _WRITABLE_VERSIONS:
            raise ValueError("unsupported single-file creation version")
        self._format_version = format_version
        self._ensure_open_sync()

    @classmethod
    def create_exclusive(
        cls, path: str | os.PathLike, *, prefix: str = "", format_version: int = FORMAT_VERSION,
    ) -> SQLiteStore:
        """Atomically create a new container; never adopt an existing pathname."""
        return cls(path, prefix=prefix, _exclusive=True, format_version=format_version)

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
        if version not in _WRITABLE_VERSIONS and not read_only:
            raise ValueError("prototype single-file cache is read-only; rebuild it as format 2")
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name LIMIT 3"
        ).fetchall()
        expected = [("table", "entries", "entries")]
        if version in _WRITABLE_VERSIONS:
            expected.append(("index", "sqlite_autoindex_entries_1", "entries"))
        if [row[:3] for row in schema] != expected:
            raise ValueError("unsupported single-file cache schema")
        normalized = "".join((schema[0][3] or "").split()).lower()
        table_schema = _SCHEMA if version in _WRITABLE_VERSIONS else _LEGACY_SCHEMA
        if normalized != "".join(table_schema.split()).lower():
            raise ValueError("unsupported single-file cache schema")
        if version in _WRITABLE_VERSIONS and schema[1][3] is not None:
            raise ValueError("unsupported single-file cache schema")

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("PRAGMA temp_store=FILE")
        if hasattr(connection, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)

    def _recover_hot_journal(self) -> None:
        """Let SQLite roll back one interrupted owned write, never discard it.

        Called only after SQLITE_READONLY_ROLLBACK, not merely because a
        journal exists: active writers keep their journals and SQLite locks.
        The immutable inspection performs no recovery or entry reads; it checks
        the exact owned schema before granting SQLite temporary write access.
        Unknown/prototype files are preserved, and the caller retries read-only.
        """
        native = filesystem_path(self.path)
        before = os.stat(native, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("journal recovery requires a regular, singly linked cache file")
        if any(os.path.lexists(native + suffix) for suffix in ("-wal", "-shm")):
            raise ValueError("unsupported WAL companion; preserve and recover it")
        inspection = sqlite3.connect(
            _database_uri(self.path, "ro") + "&immutable=1", uri=True,
            timeout=_RECOVERY_TIMEOUT, isolation_level=None,
        )
        try:
            self._configure(inspection)
            self._validate(inspection, read_only=False)
        finally:
            inspection.close()
        current = os.stat(native, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("cache changed before journal recovery; reopen it")
        if not os.path.lexists(native + "-journal"):
            return  # Another reader has already recovered it; retry read-only.
        recovery = sqlite3.connect(
            _database_uri(self.path, "rw"), uri=True,
            timeout=_RECOVERY_TIMEOUT, isolation_level=None,
        )
        try:
            self._configure(recovery)
            # SQLite obtains its own recovery locks and performs rollback on
            # the first database read. No DML, migration, or journal deletion.
            self._validate(recovery, read_only=False)
        finally:
            recovery.close()

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
                    try:
                        if sys.platform == "darwin":
                            # macOS exFAT can change a new file's inode on its
                            # first flush. Stabilize it before SQLite records
                            # the identity, as newer SQLite's Unix VFS does.
                            # Only our exclusive new file may be initialized.
                            if os.write(fd, b"S") != 1:
                                raise OSError(errno.EIO, "could not initialize cache file", native)
                            os.fsync(fd)
                    finally:
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
                if version not in _WRITABLE_VERSIONS and not self.read_only:
                    raise ValueError("prototype single-file cache is read-only; rebuild it as format 2")
                # SQLite may create -shm when a stray -wal exists even if the
                # main header still says rollback mode. This format never uses
                # WAL; refuse those companions before any SQLite connection.
                # A normal active -journal remains supported for readers.
                if any(os.path.lexists(native + suffix) for suffix in ("-wal", "-shm")):
                    raise ValueError("unsupported WAL companion beside single-file cache; preserve and recover it")
            connection = self._open_validated(created=created)
            self._connection = connection
            self._is_open = True
            return connection

    def _open_validated(self, *, created: bool) -> sqlite3.Connection:
        for attempt in range(2):
            connection = sqlite3.connect(
                _database_uri(self.path, "ro" if self.read_only else "rw"),
                uri=True, timeout=30, isolation_level=None, check_same_thread=False,
            )
            try:
                self._configure(connection)
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
                        connection.execute(f"PRAGMA user_version={self._format_version}")
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    self._validate(connection, read_only=self.read_only)
            except sqlite3.Error as exc:
                connection.close()
                if (attempt == 0 and self.read_only and self._recover
                        and getattr(exc, "sqlite_errorcode", None)
                        == sqlite3.SQLITE_READONLY_ROLLBACK):
                    try:
                        self._recover_hot_journal()
                    except (OSError, ValueError, sqlite3.Error) as recovery_error:
                        raise ValueError(
                            f"cache needs rollback-journal recovery; cache and journal preserved: {self.path}"
                        ) from recovery_error
                    continue
                raise ValueError(f"invalid or unreadable single-file cache: {self.path}") from exc
            except BaseException:
                connection.close()
                raise
            return connection
        raise AssertionError("unreachable cache open retry")

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

    def _read_chunk(self, key: str) -> bytes | None:
        """One whole entry, or None where it was never written."""
        try:
            return self._read_bytes(key)
        except FileNotFoundError:
            return None

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


def read_entry(path: str | os.PathLike, key: str, *, recover: bool = False) -> bytes:
    try:
        with SQLiteStore(path, read_only=True, recover=recover) as store:
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
    return version if version > MAX_READABLE_VERSION else None


_CHUNK_POOL_LOCK = threading.Lock()
_CHUNK_POOL: tuple[int, ThreadPoolExecutor] | None = None


def _chunk_pool() -> ThreadPoolExecutor:
    """The process's chunk-decode workers, made again after a fork."""
    global _CHUNK_POOL
    with _CHUNK_POOL_LOCK:
        if _CHUNK_POOL is None or _CHUNK_POOL[0] != os.getpid():
            workers = min(8, os.cpu_count() or 1)
            _CHUNK_POOL = (os.getpid(), ThreadPoolExecutor(workers, thread_name_prefix="nd2wsi-chunk"))
        return _CHUNK_POOL[1]


def _spread_decodes(tasks: list[Callable[[], None]]) -> None:
    """Run the first decode here and the others on the pool, then wait for all.

    Every submitted decode finishes before this returns or raises, so no
    worker writes into a result the caller has already dropped. During
    interpreter shutdown the pool refuses work and the rest runs here.
    """
    pool = _chunk_pool()
    futures = []
    for task in tasks[1:]:
        try:
            futures.append(pool.submit(task))
        except RuntimeError:
            break
    error: BaseException | None = None
    try:
        for task in (tasks[0], *tasks[1 + len(futures):]):
            task()
    finally:
        for future in futures:
            try:
                future.result()
            except BaseException as exc:  # re-raised once all have finished
                error = error or exc
    if error is not None:
        raise error


_UNREAD = object()


class _DirectReadArray:
    """A read-only Zarr array whose tile-sized reads skip Zarr's dispatch.

    ``ZarrV2RegionReader`` serves the selections it can prove equal to
    Zarr's, straight from this container's encoded chunks; every other
    selection and every other attribute goes to the Zarr array. Reads use
    the store's own connection and lock, so closing the store ends both.
    """

    def __init__(self, array: Any):
        self._array = array
        self._reader: Any = _UNREAD

    def __getattr__(self, name: str) -> Any:
        return getattr(self._array, name)

    def __repr__(self) -> str:
        return repr(self._array)

    def __array__(self, *args: Any, **kwargs: Any) -> Any:
        return self._array.__array__(*args, **kwargs)

    def __setitem__(self, selection: Any, value: Any) -> None:
        self._array[selection] = value

    def __getitem__(self, selection: Any) -> Any:
        reader = self._reader
        if reader is _UNREAD:
            reader = self._reader = self._make_reader()
        if reader is not None:
            result = reader.read(selection)
            if result is not None:
                return result
        return self._array[selection]

    def _make_reader(self) -> ZarrV2RegionReader | None:
        array = self._array
        store = array.store
        if not isinstance(store, SQLiteStore):
            return None
        reader = ZarrV2RegionReader.open(array.path, store._read_chunk, _spread_decodes)
        return reader if reader is not None and reader.shape == tuple(array.shape) else None


class _DirectReadGroup:
    """A read-only Zarr group whose arrays read tiles directly (see above)."""

    def __init__(self, group: Any):
        self._group = group

    def __getattr__(self, name: str) -> Any:
        return getattr(self._group, name)

    def __repr__(self) -> str:
        return repr(self._group)

    def __contains__(self, key: Any) -> bool:
        return key in self._group

    def __iter__(self) -> Iterator[str]:
        return iter(self._group)

    def __len__(self) -> int:
        return len(self._group)

    def __getitem__(self, key: str) -> Any:
        import zarr

        member = self._group[key]
        if isinstance(member, zarr.Array):
            return _DirectReadArray(member)
        if isinstance(member, zarr.Group):
            return _DirectReadGroup(member)
        return member


def open_zarr_group(path: str | os.PathLike, prefix: str = "store.ome.zarr", mode: str = "r"):
    """Open an embedded v2 group; the caller owns ``group.store.close()``.

    A read-only group (``mode="r"``) hands out arrays whose tile-sized reads
    go straight to the container's chunks; values, shapes and dtypes stay
    exactly Zarr's, and everything else is the Zarr group and array.
    """
    import zarr

    if mode not in {"r", "r+", "a", "w", "w-", "x"}:
        raise ValueError(f"unsupported Zarr open mode: {mode!r}")
    if mode in {"r", "r+"} and not os.path.exists(filesystem_path(path)):
        raise FileNotFoundError(path)
    store = SQLiteStore(path, read_only=mode == "r", prefix=prefix)
    try:
        group = zarr.open_group(store=store, mode=mode, zarr_format=2)
        return _DirectReadGroup(group) if mode == "r" else group
    except sqlite3.Error as exc:
        store.close()
        raise ValueError(f"invalid or unreadable single-file cache: {path}") from exc
    except BaseException:
        store.close()
        raise
