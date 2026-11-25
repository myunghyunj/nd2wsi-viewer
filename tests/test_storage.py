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


def test_nested_store_metadata_and_direct_chunks_work_beyond_windows_max_path(tmp_path):
    """Zarr's UUID .partial files and chunk I/O must survive long cache paths."""
    import shutil

    from nd2wsi.convert import open_store
    from nd2wsi.platform_io import filesystem_path

    subtree = tmp_path / ("nested-cache-" + "a" * 90)
    path = subtree / ("selection-" + "b" * 90) / ("stage-" + "c" * 70) / "store.ome.zarr"
    assert len(str(path)) > 260
    storage = ZarrV2Storage()
    try:
        root = storage.create_group(path)
        root.attrs.update({"nd2wsi": {"source": "한글 원본.nd2"}, "multiscales": []})
        array = storage.create_array(root, "0", (1, 6, 7), np.dtype("uint16"), tile=4)
        chunks = storage.chunk_array(array)
        values = np.arange(16, dtype=np.uint16).reshape(1, 4, 4)
        assert chunks.write((0, 0, 0), values)
        assert np.array_equal(chunks.read((0, 0, 0)), values)

        # Callers retain the ordinary public Path; only disk access is prefixed.
        reopened, attrs = open_store(path)
        assert attrs["nd2wsi"]["source"] == "한글 원본.nd2"
        assert np.array_equal(np.asarray(reopened["0"][:, :4, :4]), values)
        assert not str(path).startswith("\\\\?\\")
    finally:
        # Cleanup also uses an extended I/O path on machines with MAX_PATH enabled.
        shutil.rmtree(filesystem_path(subtree), ignore_errors=False)


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


def _valid_zarray_bytes() -> bytes:
    import json

    return json.dumps(
        {
            "zarr_format": 2,
            "shape": [1, 8, 8],
            "chunks": [1, 4, 4],
            "dtype": "<u2",
            "fill_value": 0,
            "order": "C",
            "filters": None,
            "compressor": None,
        }
    ).encode("utf-8")


def _unsafe_zarray_bytes() -> bytes:
    import json

    return json.dumps(
        {
            "zarr_format": 2,
            "shape": [1, 8, 8],
            "chunks": [1, 4, 4],
            "dtype": "<u2",
            "fill_value": 0,
            "order": "C",
            "filters": None,
            "compressor": {"id": "pickle"},
        }
    ).encode("utf-8")


def test_validate_zarr_v2_directory_walks_nested_subdirs_and_skips_chunk_lstat(
    tmp_path, monkeypatch
):
    import os

    import pytest

    from nd2wsi.storage import zarr_v2

    store = tmp_path / "nested.ome.zarr"
    nested = store / "group_a" / "group_b" / "0"
    nested.mkdir(parents=True)
    (nested / ".zarray").write_bytes(_valid_zarray_bytes())
    for i in range(64):
        (nested / f"0.0.{i}").write_bytes(b"chunk-data")

    lstat_targets: list[str] = []
    real_lstat = os.lstat

    def spy_lstat(path, *args, **kwargs):
        lstat_targets.append(os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", spy_lstat)
    zarr_v2.validate_zarr_v2_directory(store)
    # Chunk files must never trigger os.lstat calls during directory validation.
    assert not any("0.0." in target for target in lstat_targets)

    # An unsafe .zarray nested deeper than depth 1 must be caught.
    (nested / ".zarray").write_bytes(_unsafe_zarray_bytes())
    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        zarr_v2.validate_zarr_v2_directory(store)


def test_validate_zarr_v2_directory_rejects_symlinks_junctions_and_non_dirs(
    tmp_path, monkeypatch, symlink_support
):
    import os

    import pytest

    from nd2wsi.storage import zarr_v2

    real_store = tmp_path / "real.ome.zarr"
    (real_store / "0").mkdir(parents=True)
    (real_store / "0" / ".zarray").write_bytes(_valid_zarray_bytes())

    # Root symlink, non-directory file, and missing path are refused.
    root_link = tmp_path / "root_link.ome.zarr"
    root_link.symlink_to(real_store, target_is_directory=True)
    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        zarr_v2.validate_zarr_v2_directory(root_link)

    plain_file = tmp_path / "not-a-dir.ome.zarr"
    plain_file.write_bytes(b"file")
    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        zarr_v2.validate_zarr_v2_directory(plain_file)

    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        zarr_v2.validate_zarr_v2_directory(tmp_path / "missing.ome.zarr")

    # Symlinked subdirectory inside store is refused.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".zarray").write_bytes(_valid_zarray_bytes())
    (real_store / "linked_sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        zarr_v2.validate_zarr_v2_directory(real_store)
    (real_store / "linked_sub").unlink()

    # Symlinked .zarray inside store is refused.
    (real_store / "0" / ".zarray").unlink()
    (real_store / "0" / ".zarray").symlink_to(outside / ".zarray")
    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        zarr_v2.validate_zarr_v2_directory(real_store)
    (real_store / "0" / ".zarray").unlink()
    (real_store / "0" / ".zarray").write_bytes(_valid_zarray_bytes())

    # Simulated Windows junction on root or subdirectory is refused.
    target_ino = os.lstat(real_store / "0").st_ino
    monkeypatch.setattr(
        zarr_v2,
        "is_windows_name_surrogate_reparse",
        lambda st, fd=None: getattr(st, "st_ino", None) == target_ino,
    )
    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        zarr_v2.validate_zarr_v2_directory(real_store)

    root_ino = os.lstat(real_store).st_ino
    monkeypatch.setattr(
        zarr_v2,
        "is_windows_name_surrogate_reparse",
        lambda st, fd=None: getattr(st, "st_ino", None) == root_ino,
    )
    with pytest.raises(zarr_v2.UnsafeZarrArrayError):
        zarr_v2.validate_zarr_v2_directory(real_store)


def test_validate_zarr_v2_directory_enforces_depth_dir_count_size_and_fail_closed(
    tmp_path, monkeypatch
):
    import os

    import pytest

    from nd2wsi.storage import zarr_v2

    # Depth bound: MAX_ZARR_WALK_DEPTH allowed, +1 rejected.
    deep_store = tmp_path / "deep.ome.zarr"
    cur = deep_store
    for i in range(zarr_v2.MAX_ZARR_WALK_DEPTH):
        cur = cur / f"d{i}"
    cur.mkdir(parents=True)
    (cur / ".zarray").write_bytes(_valid_zarray_bytes())
    zarr_v2.validate_zarr_v2_directory(deep_store)

    too_deep = cur / "overflow"
    too_deep.mkdir()
    with pytest.raises(zarr_v2.UnsafeZarrArrayError, match="depth limit"):
        zarr_v2.validate_zarr_v2_directory(deep_store)

    # Directory count bound (using small cap via monkeypatch).
    wide_store = tmp_path / "wide.ome.zarr"
    for i in range(3):
        (wide_store / f"s{i}").mkdir(parents=True)
    monkeypatch.setattr(zarr_v2, "MAX_ZARR_WALK_DIRS", 4)
    zarr_v2.validate_zarr_v2_directory(wide_store)
    (wide_store / "s3").mkdir()
    with pytest.raises(zarr_v2.UnsafeZarrArrayError, match="directory walk limit"):
        zarr_v2.validate_zarr_v2_directory(wide_store)

    # Oversized .zarray bound.
    big_store = tmp_path / "big.ome.zarr"
    (big_store / "0").mkdir(parents=True)
    (big_store / "0" / ".zarray").write_bytes(b" " * (zarr_v2.MAX_ZARRAY_BYTES + 1))
    with pytest.raises(zarr_v2.UnsafeZarrArrayError, match="size limit"):
        zarr_v2.validate_zarr_v2_directory(big_store)

    # Fail closed on os.scandir OSError.
    ok_store = tmp_path / "err.ome.zarr"
    ok_store.mkdir()

    def boom(_p):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "scandir", boom)
    with pytest.raises(zarr_v2.UnsafeZarrArrayError, match="cannot scan"):
        zarr_v2.validate_zarr_v2_directory(ok_store)
