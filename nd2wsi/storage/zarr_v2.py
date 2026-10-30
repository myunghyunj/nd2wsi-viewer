"""Fast local Zarr v2 storage.

Only this module knows about ``.zarray``, v2 chunk keys, physical chunk files,
and codec metadata. Pyramid generation uses logical chunk indices. A later v3
backend may use shards without changing the converter.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from ..platform_io import filesystem_path


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
        import numcodecs

        if metadata.get("zarr_format") != 2 or metadata.get("order", "C") != "C":
            raise ValueError("direct reads need a C-order Zarr v2 array")
        if metadata.get("filters"):
            raise ValueError("direct reads do not apply filters")
        fill = metadata.get("fill_value")
        if not isinstance(fill, (int, float)) or fill != 0 or math.copysign(1, fill) < 0:
            raise ValueError("direct reads need a zero fill value")  # -0.0 is not zeros
        if not isinstance(metadata.get("dtype"), str):
            raise ValueError("direct reads need a plain dtype")
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
        compressor = metadata.get("compressor")
        self._codec = None if compressor is None else numcodecs.get_codec(compressor)
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
        decoded = payload if self._codec is None else self._codec.decode(payload)
        chunk = np.frombuffer(decoded, self.dtype).reshape(self.chunk_shape)
        for slot, offset in rows:
            out[slot] = chunk[(offset, *inner)]


class ZarrV2ChunkArray:
    """Direct chunk access for one local, C-order Zarr v2 array."""

    def __init__(self, array: Any):
        import numcodecs

        self.shape = tuple(int(value) for value in array.shape)
        self.dtype = np.dtype(array.dtype)
        self._path = Path(filesystem_path(array.store.root)) / array.path
        metadata = json.loads((self._path / ".zarray").read_text(encoding="utf-8"))
        if metadata.get("order", "C") != "C":
            raise ValueError("expected C-order chunks")
        if metadata.get("fill_value", 0) not in (0, 0.0, None):
            raise ValueError("direct storage requires a zero fill value")
        self._codec = numcodecs.get_codec(metadata["compressor"])
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
        decoded = self._codec.decode(payload)
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
        path.write_bytes(self._codec.encode(np.ascontiguousarray(block)))
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

        return zarr.open_group(filesystem_path(path), mode="w", zarr_format=2)

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
