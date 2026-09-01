"""Boundary between pyramid math and persistent storage.

The converter works with logical arrays and chunk indices. A backend owns the
on-disk format: metadata files, keys, codecs, fill chunks, and sharding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np


class ChunkArray(Protocol):
    """Chunk-level access used by the bounded downsampler."""

    shape: tuple[int, ...]
    dtype: np.dtype
    chunk_shape: tuple[int, ...]

    def read(self, index: tuple[int, ...]) -> np.ndarray: ...

    def write(self, index: tuple[int, ...], data: np.ndarray) -> bool: ...


class StorageBackend(Protocol):
    """The deliberately small contract used by pyramid generation."""

    name: str
    zarr_version: int

    def compressor(self) -> Any: ...

    def create_group(self, path: str | Path) -> Any: ...

    def create_array(
        self,
        root: Any,
        path: str,
        shape: tuple[int, ...],
        dtype: np.dtype,
        tile: int,
    ) -> Any: ...

    def chunk_array(self, array: Any) -> ChunkArray: ...

    def store_dask(
        self,
        data: Any,
        array: Any,
        *,
        allow_rechunk: bool = True,
    ) -> None: ...

    def recommended_tile(self, *, allocation_block: int) -> int: ...

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
    ) -> dict[str, int]: ...

    def descriptor(self, *, ngff_version: str) -> dict[str, Any]: ...
