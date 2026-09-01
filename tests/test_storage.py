"""Storage boundaries and the current Zarr v2 implementation."""

from pathlib import Path

import numpy as np

from nd2wsi.cache import read_manifest, write_manifest
from nd2wsi.storage import ZarrV2Storage


def test_converter_does_not_know_the_zarr_v2_file_layout():
    source = Path("nd2wsi/convert.py").read_text()
    for private_detail in (
        ".zarray",
        "dimension_separator",
        ".read_bytes()",
        ".write_bytes(",
        "codec.decode",
    ):
        assert private_detail not in source


def test_zarr_v2_backend_handles_fill_chunks_and_edge_padding(tmp_path):
    storage = ZarrV2Storage()
    root = storage.create_group(tmp_path / "store.ome.zarr")
    array = storage.create_array(root, "0", (1, 6, 7), np.dtype("uint16"), tile=4)
    chunks = storage.chunk_array(array)

    zeros = np.zeros((1, 4, 4), np.uint16)
    assert chunks.chunk_shape == zeros.shape
    assert np.array_equal(chunks.read((0, 0, 0)), zeros)

    edge = np.arange(6, dtype=np.uint16).reshape(1, 2, 3)
    assert chunks.write((0, 1, 1), edge)
    got = chunks.read((0, 1, 1))
    assert np.array_equal(got[:, :2, :3], edge)
    assert not got[:, 2:, :].any()
    assert not got[:, :, 3:].any()

    assert not chunks.write((0, 1, 1), np.zeros((1, 2, 3), np.uint16))
    assert not chunks.read((0, 1, 1)).any()


def test_manifest_records_storage_and_metadata_versions(tmp_path):
    slide = tmp_path / "slide.nd2"
    slide.write_bytes(b"nd2")
    container = tmp_path / "slide.nd2wsi-cache"
    container.mkdir()
    storage = ZarrV2Storage().descriptor(ngff_version="0.4")

    write_manifest(
        container,
        slide,
        {"name": slide.name, "size": 3, "mtime_ns": 1, "quick_sha256": "x"},
        {"t": 0, "p": 0, "z": "mid"},
        0,
        512,
        (1, 16, 16),
        "uint16",
        storage=storage,
    )

    manifest = read_manifest(container)
    assert manifest is not None
    assert manifest["storage"] == {
        "format": "zarr",
        "zarr_version": 2,
        "ngff_version": "0.4",
        "backend": "zarr-v2-direct",
    }


def test_zarr_v2_policy_owns_tile_and_allocation_model():
    storage = ZarrV2Storage()
    assert storage.recommended_tile(allocation_block=4096) == 512
    assert storage.recommended_tile(allocation_block=1 << 20) == 1024

    layout = storage.estimate_layout(
        shapes=[(1024, 1024), (512, 512)],
        channels=2,
        itemsize=2,
        tile=512,
        compression_ratio=0.5,
        allocation_block=4096,
        zero_fraction=0.25,
    )
    assert layout["raw_bytes"] == 5 * 1024 * 1024
    assert layout["data_bytes"] == layout["raw_bytes"] // 2
    assert layout["files"] == 7  # ten logical chunks, then 25% omitted and floored
    assert layout["disk_bytes"] == 7 * 262144 + 4 * 4096
    assert layout["disk_bytes"] < layout["data_bytes"]  # exact-zero chunks cost no file
