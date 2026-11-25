"""Fast local Zarr v2 storage.

Only this module knows about ``.zarray``, v2 chunk keys, physical chunk files,
and codec metadata. Pyramid generation uses logical chunk indices. A later v3
backend may use shards without changing the converter.
"""

from __future__ import annotations

import bz2
import json
import lzma
import math
import os
import stat
import zlib
from collections import deque
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from ..platform_io import BINARY, filesystem_path
from ..windows_fs import is_windows_name_surrogate_reparse

_ALLOWED_COMPRESSOR_IDS = frozenset({"blosc", "zstd", "zlib", "gzip", "lz4", "bz2", "lzma"})
_ALLOWED_FILTER_IDS = frozenset(
    {"delta", "fixedscaleoffset", "shuffle", "bitround", "packbits", "astype", "quantize"}
)
# Upper bound on a single decoded chunk (64 MiB). Normal pyramid tiles are
# 512x512..1024x1024 uint16 (~0.5..2 MiB); bounding rejects crafted zip/chunk bombs.
MAX_CHUNK_BYTES = 64 * 1024 * 1024
MAX_ZARR_WALK_DEPTH = 16
MAX_ZARR_WALK_DIRS = 4096
MAX_ZARRAY_BYTES = 1_048_576


class UnsafeZarrArrayError(ValueError):
    """Raised when Zarr array metadata violates codec, filter, or chunk-size safety limits."""


def _safe_compressor(config: Any) -> tuple[Any, str | None]:
    if config is None:
        return None, None
    if not isinstance(config, dict):
        raise UnsafeZarrArrayError("compressor configuration must be a JSON object")
    codec_id = config.get("id")
    if not isinstance(codec_id, str) or codec_id not in _ALLOWED_COMPRESSOR_IDS:
        raise UnsafeZarrArrayError(f"unsupported or unsafe Zarr compressor {codec_id!r}")
    import numcodecs

    return numcodecs.get_codec(config), codec_id


def _validate_filters(filters: Any) -> None:
    if filters is None or filters == []:
        return
    if not isinstance(filters, list):
        raise UnsafeZarrArrayError("Zarr filters configuration must be a JSON list")
    for item in filters:
        if not isinstance(item, dict):
            raise UnsafeZarrArrayError("Zarr filter entry must be a JSON object")
        fid = item.get("id")
        if not isinstance(fid, str) or fid not in _ALLOWED_FILTER_IDS:
            raise UnsafeZarrArrayError(f"unsupported or unsafe Zarr filter {fid!r}")


def _checked_chunk_bytes(chunk_shape: tuple[int, ...], dtype: np.dtype) -> int:
    if not chunk_shape or min(chunk_shape) < 1:
        raise UnsafeZarrArrayError(f"invalid chunk shape {chunk_shape!r}")
    total = int(math.prod(chunk_shape)) * int(dtype.itemsize)
    if total <= 0 or total > MAX_CHUNK_BYTES:
        raise UnsafeZarrArrayError(
            f"chunk size {total} bytes exceeds safety limit ({MAX_CHUNK_BYTES} bytes)"
        )
    return total


def validate_zarr_v2_metadata(metadata: Any) -> int:
    """Validate Zarr v2 ``.zarray`` safety invariants (chunk cap, codec & filter allowlists)."""
    if not isinstance(metadata, dict):
        raise UnsafeZarrArrayError("Zarr array metadata must be a JSON object")
    if not isinstance(metadata.get("dtype"), str):
        raise UnsafeZarrArrayError("Zarr array metadata requires a string dtype")
    try:
        dtype = np.dtype(metadata["dtype"])
    except (TypeError, ValueError) as exc:
        raise UnsafeZarrArrayError(f"invalid Zarr dtype: {metadata.get('dtype')!r}") from exc
    if dtype.kind not in "biuf":
        raise UnsafeZarrArrayError(f"unsupported Zarr dtype kind {dtype.kind!r}")
    raw_chunks = metadata.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise UnsafeZarrArrayError("Zarr array metadata requires non-empty chunks")
    try:
        chunk_shape = tuple(int(v) for v in raw_chunks)
    except (TypeError, ValueError) as exc:
        raise UnsafeZarrArrayError("Zarr chunks must be integers") from exc
    chunk_bytes = _checked_chunk_bytes(chunk_shape, dtype)
    _validate_filters(metadata.get("filters"))
    _safe_compressor(metadata.get("compressor"))
    return chunk_bytes


def _read_zarray_bounded(zarray_path: Path) -> Any:
    native = filesystem_path(zarray_path)
    try:
        st = os.lstat(native)
    except OSError as exc:
        raise UnsafeZarrArrayError(f"cannot inspect Zarr metadata at {zarray_path}") from exc
    if (
        stat.S_ISLNK(st.st_mode)
        or is_windows_name_surrogate_reparse(st)
        or not stat.S_ISREG(st.st_mode)
    ):
        raise UnsafeZarrArrayError(f"refusing unsafe Zarr metadata: {zarray_path}")
    if st.st_size > MAX_ZARRAY_BYTES:
        raise UnsafeZarrArrayError(
            f"Zarr metadata at {zarray_path} exceeds size limit ({MAX_ZARRAY_BYTES} bytes)"
        )
    flags = (
        os.O_RDONLY
        | BINARY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(native, flags)
    except OSError as exc:
        raise UnsafeZarrArrayError(f"cannot open Zarr metadata at {zarray_path}") from exc
    try:
        fst = os.fstat(fd)
        if (
            not stat.S_ISREG(fst.st_mode)
            or is_windows_name_surrogate_reparse(fst, fd=fd)
            or (
                fst.st_ino != 0
                and st.st_ino != 0
                and (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino)
            )
        ):
            raise UnsafeZarrArrayError(f"refusing unsafe Zarr metadata: {zarray_path}")
        if fst.st_size > MAX_ZARRAY_BYTES:
            raise UnsafeZarrArrayError(
                f"Zarr metadata at {zarray_path} exceeds size limit ({MAX_ZARRAY_BYTES} bytes)"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ZARRAY_BYTES:
                raise UnsafeZarrArrayError(
                    f"Zarr metadata at {zarray_path} exceeds size limit ({MAX_ZARRAY_BYTES} bytes)"
                )
            chunks.append(chunk)
    except UnsafeZarrArrayError:
        raise
    except OSError as exc:
        raise UnsafeZarrArrayError(f"cannot read Zarr metadata at {zarray_path}") from exc
    finally:
        os.close(fd)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise UnsafeZarrArrayError(f"invalid Zarr metadata at {zarray_path}") from exc


def validate_zarr_v2_directory(store_dir: Path) -> None:
    """Validate every Zarr v2 array metadata file inside a directory store."""
    store_path = Path(store_dir)
    root_native = filesystem_path(store_path)
    try:
        root_st = os.lstat(root_native)
    except OSError as exc:
        raise UnsafeZarrArrayError(f"cannot inspect Zarr store directory {store_path}") from exc
    if (
        stat.S_ISLNK(root_st.st_mode)
        or is_windows_name_surrogate_reparse(root_st)
        or not stat.S_ISDIR(root_st.st_mode)
    ):
        raise UnsafeZarrArrayError(f"refusing unsafe or non-directory Zarr store: {store_path}")

    queue: deque[tuple[Path, int]] = deque([(store_path, 0)])
    discovered_dirs = 1
    visited_dirs = 0
    while queue:
        dirpath, depth = queue.popleft()
        visited_dirs += 1
        if visited_dirs > MAX_ZARR_WALK_DIRS:
            raise UnsafeZarrArrayError(
                f"Zarr store exceeds directory walk limit ({MAX_ZARR_WALK_DIRS}): {store_path}"
            )
        try:
            with os.scandir(filesystem_path(dirpath)) as it:
                entries = sorted(it, key=lambda item: item.name)
        except OSError as exc:
            raise UnsafeZarrArrayError(f"cannot scan Zarr directory {dirpath}") from exc
        for entry in entries:
            name = entry.name
            entry_path = dirpath / name
            if name == ".zarray":
                meta = _read_zarray_bounded(entry_path)
                validate_zarr_v2_metadata(meta)
                continue
            try:
                is_link = entry.is_symlink()
                is_junc = entry.is_junction() if hasattr(entry, "is_junction") else False
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise UnsafeZarrArrayError(f"cannot inspect Zarr entry {entry_path}") from exc
            if is_link or is_junc:
                raise UnsafeZarrArrayError(f"refusing symlinked or junctioned Zarr entry: {entry_path}")
            if not is_dir:
                continue
            try:
                sub_st = os.lstat(filesystem_path(entry_path))
            except OSError as exc:
                raise UnsafeZarrArrayError(f"cannot inspect Zarr subdirectory {entry_path}") from exc
            if (
                stat.S_ISLNK(sub_st.st_mode)
                or is_windows_name_surrogate_reparse(sub_st)
                or not stat.S_ISDIR(sub_st.st_mode)
            ):
                raise UnsafeZarrArrayError(f"refusing unsafe Zarr subdirectory: {entry_path}")
            next_depth = depth + 1
            if next_depth > MAX_ZARR_WALK_DEPTH:
                raise UnsafeZarrArrayError(
                    f"Zarr store exceeds directory depth limit ({MAX_ZARR_WALK_DEPTH}): {entry_path}"
                )
            discovered_dirs += 1
            if discovered_dirs > MAX_ZARR_WALK_DIRS:
                raise UnsafeZarrArrayError(
                    f"Zarr store exceeds directory walk limit ({MAX_ZARR_WALK_DIRS}): {store_path}"
                )
            queue.append((entry_path, next_depth))


def _decode_chunk_bounded(
    codec: Any, codec_id: str | None, payload: bytes, expected_bytes: int
) -> bytes | bytearray:
    """Decompress one chunk without allocating unbounded output on crafted payloads."""
    if codec is None:
        if len(payload) != expected_bytes:
            raise ValueError(
                f"decoded chunk size {len(payload)} does not match expected {expected_bytes}"
            )
        return payload
    if codec_id == "zlib":
        decomp = zlib.decompressobj()
        decoded = decomp.decompress(payload, expected_bytes + 1)
        if len(decoded) != expected_bytes or not decomp.eof or decomp.unused_data:
            raise ValueError(
                f"decoded chunk size {len(decoded)} does not match expected {expected_bytes}"
            )
        return decoded
    if codec_id == "gzip":
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decomp.decompress(payload, expected_bytes + 1)
        if len(decoded) != expected_bytes or not decomp.eof or decomp.unused_data:
            raise ValueError(
                f"decoded chunk size {len(decoded)} does not match expected {expected_bytes}"
            )
        return decoded
    if codec_id == "bz2":
        decomp = bz2.BZ2Decompressor()
        decoded = decomp.decompress(payload, max_length=expected_bytes + 1)
        if len(decoded) != expected_bytes or not decomp.eof or decomp.unused_data:
            raise ValueError(
                f"decoded chunk size {len(decoded)} does not match expected {expected_bytes}"
            )
        return decoded
    if codec_id == "lzma":
        decomp = lzma.LZMADecompressor()
        decoded = decomp.decompress(payload, max_length=expected_bytes + 1)
        if len(decoded) != expected_bytes or not decomp.eof or decomp.unused_data:
            raise ValueError(
                f"decoded chunk size {len(decoded)} does not match expected {expected_bytes}"
            )
        return decoded
    if codec_id == "blosc":
        out = bytearray(expected_bytes)
        codec.decode(payload, out=out)
        if len(payload) < 16 or int.from_bytes(payload[4:8], "little") != expected_bytes:
            raise ValueError(
                f"blosc chunk header size does not match expected {expected_bytes}"
            )
        return out
    if codec_id == "lz4":
        out = bytearray(expected_bytes)
        codec.decode(payload, out=out)
        if len(payload) < 4 or int.from_bytes(payload[:4], "little") != expected_bytes:
            raise ValueError(
                f"lz4 chunk header size does not match expected {expected_bytes}"
            )
        return out
    if codec_id == "zstd":
        out = bytearray(expected_bytes)
        decoded = codec.decode(payload, out=out)
        if len(decoded) != expected_bytes:
            raise ValueError(
                f"decoded chunk size {len(decoded)} does not match expected {expected_bytes}"
            )
        return out
    decoded = codec.decode(payload)
    if len(decoded) != expected_bytes:
        raise ValueError(
            f"decoded chunk size {len(decoded)} does not match expected {expected_bytes}"
        )
    return decoded


def _index(value: Any, length: int, *, wrap: bool) -> int | None:
    """``value`` as an in-range axis position, or None when it is not one."""
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        return None
    index = int(value)
    if wrap and index < 0:
        index += length
    return index if 0 <= index < length else None


def _window(value: Any, length: int) -> tuple[int, int, bool] | None:
    """(start, stop, keeps_axis) for an integer or a non-empty unit-step slice."""
    if isinstance(value, slice):
        if value.step is not None and value.step != 1:
            return None
        try:
            start, stop, _ = value.indices(length)
        except (TypeError, ValueError):
            return None
        return (start, stop, True) if start < stop else None
    index = _index(value, length, wrap=True)
    return None if index is None else (index, index + 1, False)


class ZarrV2RegionReader:
    """Read small selections of one Zarr v2 array straight from its chunks.

    Zarr routes every read through its event loop and worker threads, which
    costs more than the chunk decode itself for a cached tile and serializes
    concurrent readers. This reader serves only selections whose result is
    certain to equal Zarr's in values, shape and dtype: a full-rank tuple of
    integers and unit-step slices, or a list of in-range integers on the first
    axis, whose window lies inside one chunk along every other axis. That is
    a cached tile, one channel of it, or a few of its channels. ``read``
    returns None for anything else, and the caller asks Zarr instead.

    ``fetch(key)`` returns a chunk's encoded bytes, or None where the chunk
    was never written (Zarr fills it with the zero fill value). ``spread``
    runs several chunk decodes, possibly concurrently, and returns when all
    of them are done, raising the first error. Without it they run in turn.
    """

    def __init__(
        self,
        metadata: dict[str, Any],
        path: str,
        fetch: Callable[[str], bytes | None],
        spread: Callable[[list[Callable[[], None]]], None] | None = None,
    ):
        if not isinstance(metadata, dict):
            raise ValueError("direct reads need a JSON object for Zarr metadata")
        if metadata.get("zarr_format") != 2:
            # Still reject unsafe codec/filter declarations even if zarr_format != 2.
            _validate_filters(metadata.get("filters"))
            _safe_compressor(metadata.get("compressor"))
            raise ValueError("direct reads need a C-order Zarr v2 array")
        # Validate security/safety invariants BEFORE fast-path eligibility checks
        # so unsafe codecs/filters or oversized chunks can never silently fall
        # back to raw zarr.Array reads.
        self._chunk_bytes = validate_zarr_v2_metadata(metadata)
        self._codec, self._codec_id = _safe_compressor(metadata.get("compressor"))
        if metadata.get("order", "C") != "C":
            raise ValueError("direct reads need a C-order Zarr v2 array")
        if metadata.get("filters"):
            raise ValueError("direct reads do not apply filters")
        fill = metadata.get("fill_value")
        if not isinstance(fill, (int, float)) or fill != 0 or math.copysign(1, fill) < 0:
            raise ValueError("direct reads need a zero fill value")  # -0.0 is not zeros
        self.dtype = np.dtype(metadata["dtype"])
        if self.dtype.kind not in "biuf" or not self.dtype.isnative:
            raise ValueError("direct reads need a native numeric dtype")
        self.shape = tuple(int(value) for value in metadata["shape"])
        self.chunk_shape = tuple(int(value) for value in metadata["chunks"])
        if not self.shape or len(self.chunk_shape) != len(self.shape) or min(self.chunk_shape) < 1:
            raise ValueError("direct reads need matching shape and chunks")
        separator = metadata.get("dimension_separator", ".")
        if separator not in (".", "/"):
            raise ValueError(f"unknown dimension separator {separator!r}")
        self._separator = separator
        self._prefix = f"{path}/" if path else ""
        self._fetch = fetch
        self._spread = spread

    @classmethod
    def open(
        cls,
        path: str,
        fetch: Callable[[str], bytes | None],
        spread: Callable[[list[Callable[[], None]]], None] | None = None,
    ) -> ZarrV2RegionReader | None:
        """The reader for the array at ``path``, or None when only Zarr can read it."""
        raw = fetch(f"{path}/.zarray" if path else ".zarray")
        if raw is None:
            return None
        try:
            return cls(json.loads(raw), path, fetch, spread)
        except UnsafeZarrArrayError:
            raise
        except (ValueError, TypeError, KeyError):
            return None

    def read(self, selection: Any) -> np.ndarray | None:
        """The selected values, or None when Zarr should serve this selection."""
        if not isinstance(selection, tuple) or len(selection) != len(self.shape):
            return None
        first, *rest = selection
        length, step = self.shape[0], self.chunk_shape[0]
        if isinstance(first, list):
            # Zarr reads this orthogonally only beside plain ints and slices,
            # and with no slice at all it is a coordinate selection instead.
            if not any(isinstance(value, slice) for value in rest) or not all(
                isinstance(value, slice) or type(value) is int for value in rest
            ):
                return None
            rows = [_index(value, length, wrap=False) for value in first]
            if not rows or None in rows:
                return None
            keep_first = True
        else:
            span = _window(first, length)
            if span is None:
                return None
            rows, keep_first = range(span[0], span[1]), span[2]
        windows = []
        for value, size, edge in zip(rest, self.shape[1:], self.chunk_shape[1:]):
            span = _window(value, size)
            if span is None or span[0] // edge != (span[1] - 1) // edge:
                return None  # a wider window: Zarr fetches its chunks concurrently
            windows.append(span)
        if not keep_first and not any(keep for _, _, keep in windows):
            return None  # a single element is a 0-d array in Zarr
        grid = [start // edge for (start, _, _), edge in zip(windows, self.chunk_shape[1:])]
        inner = tuple(
            slice(start - cell * edge, stop - cell * edge)
            for (start, stop, _), cell, edge in zip(windows, grid, self.chunk_shape[1:])
        )
        out = np.empty((len(rows), *(stop - start for start, stop, _ in windows)), self.dtype)
        by_chunk: dict[int, list[tuple[int, int]]] = {}
        for slot, row in enumerate(rows):
            by_chunk.setdefault(row // step, []).append((slot, row % step))
        tasks = [
            partial(self._fill, out, self._key((cell, *grid)), rows_in_chunk, inner)
            for cell, rows_in_chunk in by_chunk.items()
        ]
        if len(tasks) > 1 and self._spread is not None:
            self._spread(tasks)
        else:
            for task in tasks:
                task()
        keep = (slice(None) if keep_first else 0,) + tuple(
            slice(None) if keep else 0 for _, _, keep in windows
        )
        return out[keep]

    def _key(self, index: tuple[int, ...]) -> str:
        return self._prefix + self._separator.join(str(value) for value in index)

    def _fill(
        self, out: np.ndarray, key: str, rows: list[tuple[int, int]], inner: tuple[slice, ...]
    ) -> None:
        payload = self._fetch(key)
        if payload is None:
            for slot, _ in rows:
                out[slot] = 0
            return
        decoded = _decode_chunk_bounded(
            self._codec, self._codec_id, payload, self._chunk_bytes
        )
        chunk = np.frombuffer(decoded, self.dtype).reshape(self.chunk_shape)
        for slot, offset in rows:
            out[slot] = chunk[(offset, *inner)]


class ZarrV2ChunkArray:
    """Direct chunk access for one local, C-order Zarr v2 array."""

    def __init__(self, array: Any):
        self.shape = tuple(int(value) for value in array.shape)
        self.dtype = np.dtype(array.dtype)
        self._path = Path(filesystem_path(array.store.root)) / array.path
        metadata = json.loads((self._path / ".zarray").read_text(encoding="utf-8"))
        self._chunk_bytes = validate_zarr_v2_metadata(metadata)
        self._codec, self._codec_id = _safe_compressor(metadata.get("compressor"))
        if metadata.get("order", "C") != "C":
            raise ValueError("expected C-order chunks")
        if metadata.get("filters"):
            raise ValueError("direct storage does not apply filters")
        if metadata.get("fill_value", 0) not in (0, 0.0, None):
            raise ValueError("direct storage requires a zero fill value")
        self._separator = metadata.get("dimension_separator", ".")
        self.chunk_shape = tuple(int(value) for value in metadata["chunks"])

    def _file(self, index: tuple[int, ...]) -> Path:
        if len(index) != len(self.chunk_shape):
            raise ValueError(
                f"chunk index {index} does not match rank {len(self.chunk_shape)}"
            )
        return self._path / self._separator.join(str(int(value)) for value in index)

    def read_or_none(self, index: tuple[int, ...]) -> np.ndarray | None:
        """The decoded chunk, or None where an all-zero chunk was omitted.

        Callers whose destination already holds zeros skip the None case
        outright instead of paying for a full zero chunk per absence.
        """
        path = self._file(index)
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return None
        decoded = _decode_chunk_bounded(
            self._codec, self._codec_id, payload, self._chunk_bytes
        )
        return np.frombuffer(decoded, self.dtype).reshape(self.chunk_shape)

    def read(self, index: tuple[int, ...]) -> np.ndarray:
        chunk = self.read_or_none(index)
        if chunk is None:
            return np.zeros(self.chunk_shape, dtype=self.dtype)
        return chunk

    def write(self, index: tuple[int, ...], data: np.ndarray) -> bool:
        block = np.asarray(data, dtype=self.dtype)
        if block.ndim != len(self.chunk_shape):
            raise ValueError(
                f"chunk rank {block.ndim} does not match {len(self.chunk_shape)}"
            )
        if any(size > limit for size, limit in zip(block.shape, self.chunk_shape)):
            raise ValueError(
                f"chunk shape {block.shape} exceeds physical shape {self.chunk_shape}"
            )
        if block.shape != self.chunk_shape:
            full = np.zeros(self.chunk_shape, dtype=self.dtype)
            full[tuple(slice(0, size) for size in block.shape)] = block
            block = full
        path = self._file(index)
        if not block.any():
            path.unlink(missing_ok=True)
            return False
        contig = np.ascontiguousarray(block)
        encoded = contig.tobytes() if self._codec is None else self._codec.encode(contig)
        path.write_bytes(encoded)
        return True


class ZarrV2Storage:
    """Current storage backend: OME-NGFF metadata on local Zarr v2."""

    name = "zarr-v2-direct"
    zarr_version = 2

    def compressor(self) -> Any:
        from numcodecs import Blosc

        return Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE)

    def create_group(self, path: str | Path) -> Any:
        import zarr

        # A str is parsed as a URL by zarr 3 ('#'/'?' truncate it); a Path is
        # taken literally. "w-" never clears a pre-existing directory.
        return zarr.open_group(Path(filesystem_path(path)), mode="w-", zarr_format=2)

    def create_array(
        self,
        root: Any,
        path: str,
        shape: tuple[int, ...],
        dtype: np.dtype,
        tile: int,
    ) -> Any:
        return root.create_array(
            name=path,
            shape=shape,
            chunks=(1, tile, tile),
            dtype=dtype,
            compressors=self.compressor(),
            dimension_names=None,
            overwrite=True,
            fill_value=0,
        )

    def chunk_array(self, array: Any) -> ZarrV2ChunkArray:
        return ZarrV2ChunkArray(array)

    @staticmethod
    def _is_aligned(
        chunks: tuple[tuple[int, ...], ...], chunk_shape: tuple[int, ...]
    ) -> bool:
        if len(chunks) != len(chunk_shape):
            return False
        for dimension, step in zip(chunks, chunk_shape):
            edge = 0
            for size in dimension[:-1]:
                edge += size
                if edge % step:
                    return False
        return True

    def store_dask(
        self,
        data: Any,
        array: Any,
        *,
        allow_rechunk: bool = True,
    ) -> None:
        """Store Dask blocks as independently encoded v2 chunk files.

        The compact level-1 writer passes ``allow_rechunk=False``. A future
        change that reintroduces a hidden shuffle then fails instead of making
        the first open unexpectedly slow.
        """

        import dask.array as da

        chunked = self.chunk_array(array)
        chunk_shape = chunked.chunk_shape
        if not self._is_aligned(data.chunks, chunk_shape):
            if not allow_rechunk:
                raise ValueError(
                    f"Dask blocks {data.chunks} do not align with storage chunks "
                    f"{chunk_shape}"
                )
            data = data.rechunk(chunk_shape)

        def put(block: np.ndarray, block_info: Any = None) -> np.ndarray:
            # Dask may call once without location metadata while inferring
            # output metadata. That call must not touch the store.
            if not block_info:
                return np.zeros((1,) * block.ndim, np.uint8)
            starts = [start for start, _ in block_info[0]["array-location"]]
            ranges = [
                range(0, size, step)
                for size, step in zip(block.shape, chunk_shape)
            ]
            for c0 in ranges[0]:
                for y0 in ranges[1]:
                    for x0 in ranges[2]:
                        offsets = (c0, y0, x0)
                        sub = block[
                            c0 : c0 + chunk_shape[0],
                            y0 : y0 + chunk_shape[1],
                            x0 : x0 + chunk_shape[2],
                        ]
                        index = tuple(
                            (start + offset) // step
                            for start, offset, step in zip(
                                starts, offsets, chunk_shape
                            )
                        )
                        chunked.write(index, sub)
            return np.zeros((1,) * block.ndim, np.uint8)

        da.map_blocks(
            put,
            data,
            chunks=tuple((1,) * len(dimension) for dimension in data.chunks),
            dtype=np.uint8,
        ).compute()

    def recommended_tile(self, *, allocation_block: int) -> int:
        """Choose a logical chunk edge for a one-file-per-chunk v2 store."""
        return 1024 if allocation_block >= 65536 else 512

    def estimate_layout(
        self,
        *,
        shapes: list[tuple[int, int]],
        channels: int,
        itemsize: int,
        tile: int,
        compression_ratio: float,
        allocation_block: int,
        zero_fraction: float = 0.0,
    ) -> dict[str, int]:
        """Estimate logical and allocated bytes for local Zarr v2 chunks.

        This calculation belongs here because it assumes one physical file per
        logical chunk. A sharded v3 backend will use a different model.
        """
        raw_bytes = sum(channels * height * width for height, width in shapes) * itemsize
        data_bytes = int(raw_bytes * compression_ratio)
        chunk_bytes = tile * tile * itemsize * compression_ratio
        blocks = max(1, -(-int(chunk_bytes) // allocation_block))
        bytes_per_file = blocks * allocation_block
        chunk_files = sum(
            channels * -(-height // tile) * -(-width // tile)
            for height, width in shapes
        )
        zero_fraction = min(1.0, max(0.0, zero_fraction))
        chunk_files = int(chunk_files * (1.0 - zero_fraction))
        allocated_bytes = (
            chunk_files * bytes_per_file
            + allocation_block * (len(shapes) + 2)
        )
        return {
            "raw_bytes": int(raw_bytes),
            "data_bytes": data_bytes,
            "disk_bytes": int(allocated_bytes),
            "files": chunk_files,
        }

    def descriptor(self, *, ngff_version: str) -> dict[str, Any]:
        return {
            "format": "zarr",
            "zarr_version": self.zarr_version,
            "ngff_version": ngff_version,
            "backend": self.name,
        }


DEFAULT_STORAGE = ZarrV2Storage()
