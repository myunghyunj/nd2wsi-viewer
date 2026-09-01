"""Fast local Zarr v2 storage.

Only this module knows about ``.zarray``, v2 chunk keys, physical chunk files,
and codec metadata. Pyramid generation uses logical chunk indices. A later v3
backend may use shards without changing the converter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


class ZarrV2ChunkArray:
    """Direct chunk access for one local, C-order Zarr v2 array."""

    def __init__(self, array: Any):
        import numcodecs

        self.shape = tuple(int(value) for value in array.shape)
        self.dtype = np.dtype(array.dtype)
        self._path = Path(array.store.root) / array.path
        metadata = json.loads((self._path / ".zarray").read_text())
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

        return zarr.open_group(str(path), mode="w", zarr_format=2)

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
