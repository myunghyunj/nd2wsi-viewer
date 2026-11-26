"""Internal direct-to-SQLite Zarr v2 staging writer for single-file caches.

Writes Zarr v2 metadata and Blosc-compressed chunks straight into one writable
staging ``SQLiteStore`` without loose-file directory staging or repacking.
This module is internal to ``nd2wsi`` and is intentionally not exported from
:mod:`nd2wsi.storage`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from ..cache import MANIFEST_NAME, fingerprints_match, quick_fingerprint
from ..platform_io import filesystem_path, retry_windows_staging_op
from .single_file import FORMAT_VERSION, SQLiteStore
from .zarr_v2 import ZarrV2Storage, _decode_chunk_bounded, validate_zarr_v2_metadata

DEFAULT_MAX_INFLIGHT_BYTES = 32 * 1024 * 1024
DEFAULT_FLUSH_THRESHOLD_BYTES = 16 * 1024 * 1024
STAGING_CACHE_SIZE_KIB = -8192

_COORD_UNSEEN = 0
_COORD_WRITTEN = 1
_COORD_OMITTED = 2


class _SingleFileAttrsProxy:
    """Minimal ``root.attrs`` proxy writing ``store.ome.zarr/.zattrs``."""

    def __init__(self, group: SingleFileWriteGroup) -> None:
        self._group = group
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}

    def update(self, mapping: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(mapping)
            payload = json.dumps(self._data, indent=1).encode("utf-8")
        self._group.write_meta("store.ome.zarr/.zattrs", payload)

    def asdict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


class SingleFileArrayHandle:
    """Zarr-like level handle returned by :meth:`SingleFileWriteGroup.register_level`."""

    def __init__(
        self,
        group: SingleFileWriteGroup,
        path: str,
        shape: tuple[int, int, int],
        dtype: np.dtype,
        tile: int,
    ) -> None:
        self.group = group
        self.path = path
        self.shape = tuple(int(v) for v in shape)
        self.dtype = np.dtype(dtype)
        self.chunk_shape = (1, int(tile), int(tile))

    def __getitem__(self, key: Any) -> np.ndarray:
        if key != slice(None):
            raise NotImplementedError(
                f"SingleFileArrayHandle only supports full slice [:], got {key!r}"
            )
        self.group.flush()
        c, h, w = self.shape
        _, tile_y, tile_x = self.chunk_shape
        out = np.zeros(self.shape, dtype=self.dtype)
        chunked = SingleFileChunkArray(self)
        ny = math.ceil(h / tile_y)
        nx = math.ceil(w / tile_x)
        for ci in range(c):
            for ty in range(ny):
                y0 = ty * tile_y
                y1 = min(y0 + tile_y, h)
                for tx in range(nx):
                    x0 = tx * tile_x
                    x1 = min(x0 + tile_x, w)
                    block = chunked.read_or_none((ci, ty, tx))
                    if block is not None:
                        out[ci, y0:y1, x0:x1] = block[0, : y1 - y0, : x1 - x0]
        return out


class SingleFileWriteGroup:
    """Single-connection SQLite staging group with bounded ready-to-commit batching.

    Concurrency invariant: ``_buf_cv`` is NEVER held while calling into
    ``SQLiteStore`` (``store.batch()``, ``store.write_bytes()``, or
    ``store._read_chunk()``). ``store.batch()`` is opened only on already-ready
    in-memory bytes and never waits on producer threads.
    """

    def __init__(
        self,
        staging_path: str | Path,
        *,
        max_inflight_bytes: int = DEFAULT_MAX_INFLIGHT_BYTES,
        flush_threshold_bytes: int = DEFAULT_FLUSH_THRESHOLD_BYTES,
        format_version: int = FORMAT_VERSION,
        cache_size: int = STAGING_CACHE_SIZE_KIB,
    ) -> None:
        if max_inflight_bytes < 1:
            raise ValueError("max_inflight_bytes must be >= 1")
        if flush_threshold_bytes < 1:
            raise ValueError("flush_threshold_bytes must be >= 1")
        self.staging_path = Path(staging_path)
        self._max_inflight_bytes = int(max_inflight_bytes)
        self._flush_threshold_bytes = min(
            int(flush_threshold_bytes), max(1, self._max_inflight_bytes // 2)
        )
        import numcodecs.blosc as _blosc

        _blosc.use_threads = False
        self.codec = ZarrV2Storage().compressor()
        self.store = SQLiteStore.create_exclusive(
            self.staging_path,
            format_version=format_version,
            cache_size=cache_size,
        )
        self._buf_cv = threading.Condition(threading.Lock())
        self._pending: list[tuple[str, bytes]] = []
        self._queued_bytes = 0
        self._flushing = False
        self._flushing_bytes = 0
        self._oversized_waiters = 0
        self._closed = False
        self._store_closed = False
        self._error: BaseException | None = None
        self._zgroup_queued = False
        self._arrays: dict[str, SingleFileArrayHandle] = {}
        self._coord_grids: dict[str, np.ndarray] = {}
        self._expected_entries: dict[str, tuple[int, bytes]] = {}
        self.attrs = _SingleFileAttrsProxy(self)

    def __getitem__(self, level_path: str) -> SingleFileArrayHandle:
        return self._arrays[str(level_path)]

    def _claim_flush_locked(self) -> list[tuple[str, bytes]]:
        batch = self._pending
        self._pending = []
        self._flushing = True
        self._flushing_bytes = self._queued_bytes
        self._queued_bytes = 0
        self._buf_cv.notify_all()
        return batch

    def _commit_batch(self, batch: list[tuple[str, bytes]]) -> None:
        try:
            with self.store.batch():
                for key, payload in batch:
                    self.store.write_bytes(key, payload)
        except BaseException as exc:
            with self._buf_cv:
                if self._error is None:
                    self._error = exc
                self._pending.clear()
                self._queued_bytes = 0
                self._flushing_bytes = 0
                self._flushing = False
                self._buf_cv.notify_all()
            raise
        with self._buf_cv:
            self._flushing_bytes = 0
            self._flushing = False
            self._buf_cv.notify_all()

    def write_meta(self, key: str, payload: bytes) -> None:
        digest = hashlib.sha256(payload).digest()
        with self._buf_cv:
            if self._error is not None:
                raise self._error
            if self._closed:
                raise RuntimeError("single-file staging writer is closed")
            self._expected_entries[key] = (len(payload), digest)
            self._pending.append((key, payload))
            self._queued_bytes += len(payload)

    def register_level(
        self,
        level_path: str,
        shape: tuple[int, ...],
        dtype: np.dtype,
        tile: int,
    ) -> SingleFileArrayHandle:
        if len(shape) != 3:
            raise ValueError(f"expected 3D (C, Y, X) level shape, got {shape!r}")
        c, h, w = (int(v) for v in shape)
        tile_int = int(tile)
        if c < 1 or h < 1 or w < 1 or tile_int < 1:
            raise ValueError(f"invalid level shape {shape!r} or tile {tile!r}")
        dt = np.dtype(dtype)
        fill_value: float | int = 0.0 if np.issubdtype(dt, np.floating) else 0
        zarray_meta: dict[str, Any] = {
            "chunks": [1, tile_int, tile_int],
            "compressor": self.codec.get_config(),
            "dimension_separator": ".",
            "dtype": dt.str,
            "fill_value": fill_value,
            "filters": None,
            "order": "C",
            "shape": [c, h, w],
            "zarr_format": 2,
        }
        validate_zarr_v2_metadata(zarray_meta)
        zgroup_payload: bytes | None = None
        with self._buf_cv:
            if not self._zgroup_queued:
                self._zgroup_queued = True
                zgroup_payload = json.dumps({"zarr_format": 2}, indent=1).encode("utf-8")
        if zgroup_payload is not None:
            self.write_meta("store.ome.zarr/.zgroup", zgroup_payload)
        zarray_payload = json.dumps(zarray_meta, indent=1).encode("utf-8")
        self.write_meta(f"store.ome.zarr/{level_path}/.zarray", zarray_payload)
        self.write_meta(f"store.ome.zarr/{level_path}/.zattrs", b"{}")
        ny = math.ceil(h / tile_int)
        nx = math.ceil(w / tile_int)
        handle = SingleFileArrayHandle(self, str(level_path), (c, h, w), dt, tile_int)
        with self._buf_cv:
            self._arrays[str(level_path)] = handle
            self._coord_grids[str(level_path)] = np.zeros((c, ny, nx), dtype=np.uint8)
        return handle

    def submit_chunk(
        self,
        level_path: str,
        coord: tuple[int, int, int],
        encoded: bytes | None,
        digest: bytes | None,
    ) -> None:
        key = f"store.ome.zarr/{level_path}/{coord[0]}.{coord[1]}.{coord[2]}"
        if encoded is None:
            with self._buf_cv:
                if self._error is not None:
                    raise self._error
                if self._closed:
                    raise RuntimeError("single-file staging writer is closed")
                grid = self._coord_grids[level_path]
                if grid[coord] != _COORD_UNSEEN:
                    raise RuntimeError(
                        f"duplicate chunk coordinate {coord} in level {level_path}"
                    )
                grid[coord] = _COORD_OMITTED
            return

        if digest is None:
            raise ValueError("digest is required for non-None encoded chunk")
        payload_len = len(encoded)
        if payload_len > self._max_inflight_bytes:
            with self._buf_cv:
                self._oversized_waiters += 1
            commit_single: list[tuple[str, bytes]] | None = None
            try:
                while True:
                    batch_to_flush: list[tuple[str, bytes]] | None = None
                    with self._buf_cv:
                        if self._error is not None:
                            raise self._error
                        if self._closed:
                            raise RuntimeError("single-file staging writer is closed")
                        grid = self._coord_grids[level_path]
                        if grid[coord] != _COORD_UNSEEN:
                            raise RuntimeError(
                                f"duplicate chunk coordinate {coord} in level {level_path}"
                            )
                        if self._flushing:
                            self._buf_cv.wait()
                            continue
                        if self._pending:
                            batch_to_flush = self._claim_flush_locked()
                        else:
                            grid[coord] = _COORD_WRITTEN
                            self._expected_entries[key] = (payload_len, digest)
                            self._flushing = True
                            self._flushing_bytes = payload_len
                            commit_single = [(key, encoded)]
                            break
                    if batch_to_flush is not None:
                        self._commit_batch(batch_to_flush)
            finally:
                with self._buf_cv:
                    self._oversized_waiters -= 1
                    self._buf_cv.notify_all()
            if commit_single is not None:
                self._commit_batch(commit_single)
            return

        while True:
            batch_to_flush = None
            admitted = False
            with self._buf_cv:
                if self._error is not None:
                    raise self._error
                if self._closed:
                    raise RuntimeError("single-file staging writer is closed")
                grid = self._coord_grids[level_path]
                if grid[coord] != _COORD_UNSEEN:
                    raise RuntimeError(
                        f"duplicate chunk coordinate {coord} in level {level_path}"
                    )
                if (
                    self._oversized_waiters > 0
                    or self._queued_bytes + self._flushing_bytes + payload_len
                    > self._max_inflight_bytes
                ):
                    if not self._flushing and self._pending:
                        batch_to_flush = self._claim_flush_locked()
                    else:
                        self._buf_cv.wait()
                        continue
                else:
                    grid[coord] = _COORD_WRITTEN
                    self._expected_entries[key] = (payload_len, digest)
                    self._pending.append((key, encoded))
                    self._queued_bytes += payload_len
                    admitted = True
                    if not self._flushing and self._queued_bytes >= self._flush_threshold_bytes:
                        batch_to_flush = self._claim_flush_locked()
            if batch_to_flush is not None:
                self._commit_batch(batch_to_flush)
            if admitted:
                return

    def flush(self) -> None:
        while True:
            batch_to_flush: list[tuple[str, bytes]] | None = None
            with self._buf_cv:
                while self._flushing:
                    self._buf_cv.wait()
                if self._error is not None:
                    raise self._error
                if not self._pending:
                    return
                batch_to_flush = self._claim_flush_locked()
            self._commit_batch(batch_to_flush)

    def assert_all_coords_accounted(self) -> None:
        with self._buf_cv:
            for level_path, grid in self._coord_grids.items():
                unseen = int((grid == _COORD_UNSEEN).sum())
                if unseen != 0:
                    raise RuntimeError(
                        f"incomplete pyramid level {level_path}: "
                        f"{unseen} planned chunk coordinate(s) unaccounted for"
                    )

    def snapshot_expected_entries(self) -> dict[str, tuple[int, bytes]]:
        with self._buf_cv:
            return dict(self._expected_entries)

    def close_handles(self, *, suppress_errors: bool = False) -> None:
        with self._buf_cv:
            self._closed = True
            self._buf_cv.notify_all()
            while self._flushing:
                self._buf_cv.wait()
            if self._store_closed:
                return
            self._store_closed = True
        try:
            self.store.close()
        except sqlite3.Error:
            if not suppress_errors:
                raise


class SingleFileChunkArray:
    """Direct-to-SQLite ``ChunkArray`` implementation with zero-omission accounting."""

    def __init__(self, array: SingleFileArrayHandle) -> None:
        self._array = array
        self.shape = array.shape
        self.dtype = array.dtype
        self.chunk_shape = array.chunk_shape
        self._expected_bytes = int(math.prod(self.chunk_shape)) * int(self.dtype.itemsize)
        self._grid_shape = tuple(
            math.ceil(dim / step) for dim, step in zip(self.shape, self.chunk_shape)
        )

    def _validate_index(self, index: tuple[int, ...]) -> tuple[int, int, int]:
        if len(index) != 3:
            raise ValueError(f"chunk index rank {len(index)} does not match 3")
        ci, ty, tx = (int(v) for v in index)
        gc, gy, gx = self._grid_shape
        if not (0 <= ci < gc and 0 <= ty < gy and 0 <= tx < gx):
            raise IndexError(
                f"chunk index {(ci, ty, tx)} out of range for grid {self._grid_shape}"
            )
        return ci, ty, tx

    def read_or_none(self, index: tuple[int, ...]) -> np.ndarray | None:
        ci, ty, tx = self._validate_index(index)
        key = f"store.ome.zarr/{self._array.path}/{ci}.{ty}.{tx}"
        payload = self._array.group.store._read_chunk(key)
        if payload is None:
            return None
        decoded = _decode_chunk_bounded(
            self._array.group.codec, "blosc", payload, self._expected_bytes
        )
        return np.frombuffer(decoded, dtype=self.dtype).reshape(self.chunk_shape)

    def read(self, index: tuple[int, ...]) -> np.ndarray:
        block = self.read_or_none(index)
        return np.zeros(self.chunk_shape, dtype=self.dtype) if block is None else block

    def record_zero(self, index: tuple[int, ...]) -> None:
        coord = self._validate_index(index)
        self._array.group.submit_chunk(self._array.path, coord, None, None)

    def write(self, index: tuple[int, ...], data: np.ndarray) -> bool:
        coord = self._validate_index(index)
        block = np.asarray(data, dtype=self.dtype)
        if block.ndim != len(self.chunk_shape):
            raise ValueError(
                f"chunk rank {block.ndim} does not match {len(self.chunk_shape)}"
            )
        if any(size > limit for size, limit in zip(block.shape, self.chunk_shape)):
            raise ValueError(
                f"chunk shape {block.shape} exceeds physical shape {self.chunk_shape}"
            )
        if not block.any():
            self._array.group.submit_chunk(self._array.path, coord, None, None)
            return False
        if block.shape != self.chunk_shape:
            full = np.zeros(self.chunk_shape, dtype=self.dtype)
            full[tuple(slice(0, size) for size in block.shape)] = block
            block = full
        contig = np.ascontiguousarray(block)
        encoded = bytes(self._array.group.codec.encode(contig))
        digest = hashlib.sha256(encoded).digest()
        self._array.group.submit_chunk(self._array.path, coord, encoded, digest)
        return True


class _SingleFileStagingBackend(ZarrV2Storage):
    """Internal ``ZarrV2Storage`` subclass targeting one staging ``SQLiteStore``."""

    def __init__(
        self,
        *,
        max_inflight_bytes: int = DEFAULT_MAX_INFLIGHT_BYTES,
        flush_threshold_bytes: int = DEFAULT_FLUSH_THRESHOLD_BYTES,
        format_version: int = FORMAT_VERSION,
        cache_size: int = STAGING_CACHE_SIZE_KIB,
    ) -> None:
        self._max_inflight_bytes = max_inflight_bytes
        self._flush_threshold_bytes = flush_threshold_bytes
        self._format_version = format_version
        self._cache_size = cache_size

    def create_group(self, path: str | Path) -> SingleFileWriteGroup:
        return SingleFileWriteGroup(
            Path(path),
            max_inflight_bytes=self._max_inflight_bytes,
            flush_threshold_bytes=self._flush_threshold_bytes,
            format_version=self._format_version,
            cache_size=self._cache_size,
        )

    def create_array(
        self,
        root: Any,
        path: str,
        shape: tuple[int, ...],
        dtype: np.dtype,
        tile: int,
    ) -> SingleFileArrayHandle:
        return root.register_level(path, shape, dtype, tile)

    def chunk_array(self, array: Any) -> SingleFileChunkArray:
        return SingleFileChunkArray(array)


def verify_and_finalize_single_file(
    group: SingleFileWriteGroup,
    *,
    staging_path: Path,
    source: Path,
    before_fp: dict[str, Any],
    manifest: dict[str, Any],
    validate_fn: Callable[[Path], None],
    on_verify_progress: Callable[[float], None] | None = None,
) -> None:
    """Verify every chunk coordinate and entry SHA-256 on disk, then mark complete."""
    staging_path = Path(staging_path)
    try:
        group.assert_all_coords_accounted()
        manifest["complete"] = False
        group.write_meta(MANIFEST_NAME, json.dumps(manifest, indent=1).encode("utf-8"))
        group.flush()
        expected = group.snapshot_expected_entries()
    except BaseException:
        group.close_handles(suppress_errors=True)
        raise
    group.close_handles(suppress_errors=False)

    total = max(1, len(expected))
    verified = 0
    with retry_windows_staging_op(lambda: SQLiteStore(staging_path, read_only=True)) as ro:
        if set(ro.list_keys()) != set(expected):
            raise RuntimeError("single-file cache entry set mismatch after build")
        for key, payload in ro.iter_entries_by_rowid():
            exp = expected.get(key)
            if exp is None or (len(payload), hashlib.sha256(payload).digest()) != exp:
                raise RuntimeError(f"single-file cache verification failed: {key}")
            verified += 1
            if on_verify_progress is not None and (verified % 64 == 0 or verified == total):
                on_verify_progress(min(1.0, verified / total))
        if verified != len(expected):
            raise RuntimeError("single-file cache rowid scan count mismatch")

    after_fp = quick_fingerprint(source)
    if not fingerprints_match(before_fp, after_fp, allow_mtime_skew=False):
        raise RuntimeError(
            f"{source.name} changed while building its cache "
            "(still copying, or being written by the scanner?) — "
            "try again once the file is settled"
        )

    manifest["complete"] = True
    final_manifest = json.dumps(manifest, indent=1).encode("utf-8")

    def _write_complete_manifest() -> None:
        with SQLiteStore(staging_path) as st:
            st.write_bytes(MANIFEST_NAME, final_manifest)

    retry_windows_staging_op(_write_complete_manifest)

    staging_native = filesystem_path(staging_path)

    def _fsync_staging() -> None:
        with open(staging_native, "r+b") as handle:
            os.fsync(handle.fileno())

    retry_windows_staging_op(_fsync_staging)

    def _confirm_manifest() -> None:
        with SQLiteStore(staging_path, read_only=True) as ro:
            if ro.read_bytes(MANIFEST_NAME) != final_manifest:
                raise RuntimeError("final manifest verification failed")

    retry_windows_staging_op(_confirm_manifest)
    retry_windows_staging_op(lambda: validate_fn(staging_path))
