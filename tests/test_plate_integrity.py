from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import zarr

from nd2wsi.plate_integrity import (
    DIGEST_ALGORITHM,
    UNCOMMITTED_DIGEST,
    digest_matches,
    ensure_digest_array,
    frame_digest,
    quarantine_chunk,
    zarr_v2_chunk_path,
)


def test_valid_zero_frame_has_a_nonzero_commit_digest():
    frame = np.zeros((1, 8, 12), dtype=np.uint16)

    committed = frame_digest(frame)

    assert DIGEST_ALGORITHM == "blake2b-64-v1"
    assert committed != UNCOMMITTED_DIGEST
    assert frame_digest(frame) == committed
    assert digest_matches(frame, committed)
    assert not digest_matches(frame, UNCOMMITTED_DIGEST)


def test_frame_digest_includes_dtype_and_shape():
    raw = np.arange(24, dtype=np.uint8)
    same_bytes_other_shape = raw.reshape(2, 12)
    same_bytes_other_dtype = raw.view(np.uint16)

    assert frame_digest(raw) != frame_digest(same_bytes_other_shape)
    assert frame_digest(raw) != frame_digest(same_bytes_other_dtype)


def test_ensure_digest_array_creates_the_tpz_uint64_commit_map(tmp_path):
    root = zarr.open_group(str(tmp_path / "store.zarr"), mode="w", zarr_format=2)

    digest = ensure_digest_array(root, (2, 3, 4))

    assert digest.shape == (2, 3, 4)
    assert digest.chunks == (1, 3, 4)
    assert np.dtype(digest.dtype) == np.dtype(np.uint64)
    np.testing.assert_array_equal(digest[:], np.zeros((2, 3, 4), dtype=np.uint64))
    assert ensure_digest_array(root, (2, 3, 4)).path == "digest"


@pytest.mark.parametrize(
    ("shape", "dtype", "message"),
    [
        ((1, 2, 4), np.uint64, "shape"),
        ((1, 2, 3), np.uint32, "dtype"),
    ],
)
def test_ensure_digest_array_rejects_an_incompatible_existing_array(
    tmp_path, shape, dtype, message
):
    root = zarr.open_group(str(tmp_path / "store.zarr"), mode="w", zarr_format=2)
    root.create_array("digest", shape=shape, chunks=shape, dtype=dtype)

    with pytest.raises(ValueError, match=message):
        ensure_digest_array(root, (1, 2, 3))


@pytest.mark.parametrize(
    ("separator", "expected"),
    [
        (".", Path("thumbs") / "2.0.4.0.0.0"),
        ("/", Path("thumbs") / "2" / "0" / "4" / "0" / "0" / "0"),
    ],
)
def test_zarr_v2_chunk_path_honours_the_dimension_separator(tmp_path, separator, expected):
    array_dir = tmp_path / "thumbs"
    array_dir.mkdir()
    (array_dir / ".zarray").write_text(
        json.dumps({"zarr_format": 2, "dimension_separator": separator}),
        encoding="utf-8",
    )

    path = zarr_v2_chunk_path(array_dir, (2, 0, 4, 0, 0, 0))

    assert path == tmp_path / expected


def test_quarantine_chunk_preserves_the_original_bytes(tmp_path):
    payload = tmp_path / "0.0.0"
    payload.write_bytes(b"broken-payload")

    retained = quarantine_chunk(payload)

    assert retained is not None
    assert not payload.exists()
    assert retained.parent == payload.parent
    assert retained.name.startswith("0.0.0.corrupt-")
    assert retained.read_bytes() == b"broken-payload"
