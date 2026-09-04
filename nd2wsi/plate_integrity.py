"""Integrity primitives for the plate thumbnail cache.

Integrity-enabled stores use a per-frame digest alongside their ``done``
commit marker. A digest is therefore meaningful even when Zarr legitimately
omits an all-zero payload chunk. Physical chunk paths are used only to retain
an undecodable payload before source-backed repair; presence is never treated
as proof of a valid frame.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

DIGEST_NAME = "digest"
DIGEST_ALGORITHM = "blake2b-64-v1"
UNCOMMITTED_DIGEST = np.uint64(0)


def frame_digest(frame: np.ndarray) -> np.uint64:
    """Return a stable, nonzero digest for one cached frame.

    Shape and dtype are part of the digest domain so equal byte strings with
    different array interpretations do not compare as the same frame.  Zero is
    reserved for the uncommitted state, including when ``frame`` itself is all
    zero and its sparse Zarr payload may not exist on disk.
    """
    array = np.ascontiguousarray(frame)
    digest = hashlib.blake2b(digest_size=8, person=b"nd2wsi")
    digest.update(array.dtype.str.encode("ascii"))
    digest.update(np.asarray(array.shape, dtype="<u8").tobytes())
    digest.update(array.tobytes())
    value = int.from_bytes(digest.digest(), byteorder="little")
    return np.uint64(value or 1)


def digest_matches(frame: np.ndarray, expected: int | np.integer[Any]) -> bool:
    """Whether ``frame`` matches a committed digest value."""
    try:
        expected_value = int(expected)
    except (TypeError, ValueError, OverflowError):
        return False
    return expected_value != int(UNCOMMITTED_DIGEST) and int(
        frame_digest(frame)
    ) == expected_value


def ensure_digest_array(root: Any, tpz_shape: Sequence[int]) -> Any:
    """Return the uint64 ``(T, P, Z)`` commit map, creating it if absent."""
    shape = tuple(int(value) for value in tpz_shape)
    if len(shape) != 3 or any(value <= 0 for value in shape):
        raise ValueError(f"digest shape must be three positive dimensions, got {shape!r}")

    try:
        digest = root[DIGEST_NAME]
    except KeyError:
        return root.create_array(
            name=DIGEST_NAME,
            shape=shape,
            chunks=(1, shape[1], shape[2]),
            dtype=np.uint64,
            overwrite=False,
            fill_value=UNCOMMITTED_DIGEST,
        )

    if tuple(digest.shape) != shape:
        raise ValueError(
            f"existing digest shape {tuple(digest.shape)!r} does not match {shape!r}"
        )
    if np.dtype(digest.dtype) != np.dtype(np.uint64):
        raise ValueError(f"existing digest dtype {digest.dtype!r} is not uint64")
    return digest


def zarr_v2_chunk_path(array_dir: Path, coords: Sequence[int]) -> Path:
    """Return a Zarr-v2 array's physical chunk path.

    Runtime validity uses digests, because a valid all-zero chunk may be absent
    when ``write_empty_chunks`` is disabled. This path is only for retaining an
    undecodable physical payload before repair.
    """
    metadata = json.loads((array_dir / ".zarray").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("zarr_format") != 2:
        raise ValueError(f"{array_dir} is not a Zarr-v2 array")

    chunk_coords = tuple(int(value) for value in coords)
    if not chunk_coords or any(value < 0 for value in chunk_coords):
        raise ValueError(f"invalid chunk coordinates: {chunk_coords!r}")

    separator = metadata.get("dimension_separator", ".")
    if separator == ".":
        return array_dir / ".".join(str(value) for value in chunk_coords)
    if separator == "/":
        return array_dir.joinpath(*(str(value) for value in chunk_coords))
    raise ValueError(f"unsupported Zarr-v2 dimension separator: {separator!r}")


def quarantine_chunk(path: Path) -> Path | None:
    """Atomically retain one corrupt payload beside the array before repair.

    Callers serialize the failed read and this rename with the writer's local
    store lock. The one process-wide writer lease excludes other current
    writers, so the pathname cannot become a newly repaired chunk in between.
    """
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = path.with_name(
        f"{path.name}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    try:
        path.rename(target)
    except FileNotFoundError:
        return None
    return target
