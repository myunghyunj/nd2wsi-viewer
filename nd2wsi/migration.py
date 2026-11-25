"""Lossless, bounded-memory import of managed directory caches into .nd2svs.

Import never deletes its input. Callers publish the verified file atomically;
removing an old cache is a separate, explicitly authorized operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .cache import (
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    OVERVIEW_FORMAT,
    SINGLE_FILE_FORMAT,
    STORE_NAME,
    fingerprints_match,
    names_match,
    quick_fingerprint,
)
from .platform_io import BINARY, filesystem_path
from .windows_fs import is_windows_name_surrogate_reparse

# Maximum size of any single cache entry during directory-to-single-file packing
# (256 MiB accommodates large manifests/chunks while refusing unbounded reads).
_MAX_PACK_ENTRY_BYTES = 256 * 1024 * 1024


def _check_real_dir(path: Path) -> None:
    try:
        st = os.lstat(filesystem_path(path))
    except OSError as exc:
        raise ValueError(f"not a real cache directory: {path}") from exc
    if stat.S_ISLNK(st.st_mode) or is_windows_name_surrogate_reparse(st) or not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"not a real cache directory: {path}")


def _read_regular_entry(path: Path) -> tuple[os.stat_result, bytes]:
    """Read a regular, non-symlink, non-reparse cache entry safely without TOCTOU."""
    native = filesystem_path(path)
    lstat = os.lstat(native)
    if stat.S_ISLNK(lstat.st_mode) or is_windows_name_surrogate_reparse(lstat) or not stat.S_ISREG(lstat.st_mode):
        raise ValueError(f"not a regular cache entry: {path}")
    flags = (
        os.O_RDONLY
        | BINARY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(native, flags)
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or is_windows_name_surrogate_reparse(st, fd=fd)
            or (
                st.st_ino != 0
                and lstat.st_ino != 0
                and (st.st_dev, st.st_ino) != (lstat.st_dev, lstat.st_ino)
            )
        ):
            raise ValueError(f"not a regular cache entry: {path}")
        if st.st_size > _MAX_PACK_ENTRY_BYTES:
            raise ValueError(f"cache entry exceeds size limit: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PACK_ENTRY_BYTES:
                raise ValueError(f"cache entry exceeds size limit: {path}")
            chunks.append(chunk)
        return st, b"".join(chunks)
    finally:
        os.close(fd)


def cache_entries(container: Path) -> list[tuple[str, Path]]:
    """Only cache-owned metadata and numeric Zarr v2 chunks are importable.

    Unknown files, symlinks and annotations make this operation fail closed.
    Finder AppleDouble metadata is ignored, not interpreted as image data.
    """
    _check_real_dir(container)
    walk_root = Path(filesystem_path(container))
    entries = []

    def _raise_walk_error(exc: OSError) -> None:
        raise exc

    for base, dirs, files in os.walk(walk_root, followlinks=False, onerror=_raise_walk_error):
        for name in dirs:
            subdir = Path(base) / name
            sub_st = os.lstat(filesystem_path(subdir))
            if stat.S_ISLNK(sub_st.st_mode) or is_windows_name_surrogate_reparse(sub_st):
                raise ValueError(f"symlink or junction in cache: {subdir}")
        for name in sorted(files):
            path = Path(base) / name
            file_st = os.lstat(filesystem_path(path))
            if stat.S_ISLNK(file_st.st_mode) or is_windows_name_surrogate_reparse(file_st) or not stat.S_ISREG(file_st.st_mode):
                raise ValueError(f"not a regular cache entry: {path}")
            if name.startswith("._") or name == ".DS_Store":
                continue
            key = path.relative_to(walk_root).as_posix()
            chunk_pat = (
                r"\d+(?:\.\d+)*(?:\.corrupt-[0-9a-fA-FT-]+)?"
                if key.startswith("thumbs.zarr/")
                else r"\d+(?:\.\d+)*"
            )
            owned = key == MANIFEST_NAME or (
                key.startswith((STORE_NAME + "/", "thumbs.zarr/"))
                and (name in (".zgroup", ".zattrs", ".zarray", ".zmetadata")
                     or re.fullmatch(chunk_pat, name))
            )
            if not owned:
                raise ValueError(f"unknown or user-owned file in cache; preserved: {path}")
            entries.append((key, path))
    return sorted(entries)


def pack_directory(
    container: str | Path,
    target: str | Path,
    *,
    source: str | Path | None = None,
    on_progress: Callable[[float], None] | None = None,
    storage_version: int = 2,
) -> dict[str, Any]:
    """Create and byte-verify a new file; never overwrite or remove anything.

    The sole transformed entry is manifest.json: format and relative source
    location describe the new container. All compressed chunks, array metadata,
    calibration, channels and selections remain byte-for-byte unchanged.
    """
    from . import __version__
    from .storage.single_file import CORRECTED_FORMAT_VERSION, FORMAT_VERSION, SQLiteStore

    if storage_version not in (FORMAT_VERSION, CORRECTED_FORMAT_VERSION):
        raise ValueError("unsupported single-file packing version")

    container, target = Path(container), Path(target)
    target_native = filesystem_path(target)
    if os.path.lexists(target_native):
        raise FileExistsError(target)
    entries = cache_entries(container)
    _, original_manifest = _read_regular_entry(container / MANIFEST_NAME)
    manifest = json.loads(original_manifest)
    if not isinstance(manifest, dict) or not manifest.get("complete"):
        raise ValueError(f"incomplete cache: {container}")
    old_format = manifest.get("format")
    if old_format not in (MANIFEST_FORMAT, OVERVIEW_FORMAT, "nd2wsi-plate/1"):
        raise ValueError(f"unknown cache format: {old_format}")
    source_info = manifest.get("source")
    if not isinstance(source_info, dict):
        raise ValueError("cache manifest lacks source identity")
    if (old_format == "nd2wsi-plate/1") != (manifest.get("kind") == "plate"):
        raise ValueError("cache format and kind disagree")
    if source is None:
        from .annotation_sidecars import _manifest_source_path

        resolved_source = _manifest_source_path(container, manifest)
        if resolved_source is None:
            raise ValueError(
                "cache manifest lacks a valid relative source path inside the slide folder"
            )
        source = resolved_source
    source = Path(source).resolve()
    if not names_match(source_info.get("name"), source.name):
        raise ValueError("cache source name does not match its relative path")
    source_before = quick_fingerprint(source)
    if not fingerprints_match(source_info, source_before):
        raise ValueError("cache source fingerprint does not match the source file")
    manifest["format"] = (
        "nd2wsi-plate/2" if manifest.get("kind") == "plate" else SINGLE_FILE_FORMAT
    )
    try:
        manifest["source"]["relative_path"] = Path(
            os.path.relpath(source, target.parent)
        ).as_posix()
    except ValueError:  # Windows: source and container on different mounts
        manifest["source"]["relative_path"] = ""
    manifest.setdefault("storage", {})["container"] = f"nd2svs-sqlite/{storage_version}"
    manifest["migration"] = {
        "from_format": old_format, "nd2wsi_version": __version__,
        "method": "compressed-entry-copy-sha256-verified",
    }
    # A failed import may leave a caller-owned staging file, but must never
    # advertise that unverified file as a complete, reusable cache.
    manifest["complete"] = False
    new_manifest = json.dumps(manifest, indent=1).encode("utf-8")
    expected: dict[str, tuple[int, str]] = {}
    original_stats = {}
    os.makedirs(filesystem_path(target.parent), exist_ok=True)
    store = SQLiteStore.create_exclusive(target, format_version=storage_version)
    try:
        # One transaction avoids a journal sync per tiny metadata/chunk entry.
        with store.batch():
            for index, (key, path) in enumerate(entries):
                st, original_payload = _read_regular_entry(path)
                if key == MANIFEST_NAME and original_payload != original_manifest:
                    raise ValueError(f"source cache manifest changed while packing: {path}")
                original_stats[key] = (
                    st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                    hashlib.sha256(original_payload).hexdigest(),
                )
                payload = new_manifest if key == MANIFEST_NAME else original_payload
                expected[key] = (len(payload), hashlib.sha256(payload).hexdigest())
                store.write_bytes(key, payload)
                if on_progress:
                    on_progress(0.5 * (index + 1) / max(1, len(entries)))
    finally:
        store.close()
    # Reopen from disk; comparing through the writing connection would not
    # establish that a movable, closed single file contains all its payloads.
    store = SQLiteStore(target, read_only=True)
    try:
        if set(store.list_keys()) != set(expected):
            raise ValueError("entry set mismatch after import")
        for index, (key, path) in enumerate(entries):
            payload = store.read_bytes(key)
            if (len(payload), hashlib.sha256(payload).hexdigest()) != expected[key]:
                raise ValueError(f"entry verification failed: {key}")
            st, current_payload = _read_regular_entry(path)
            if (
                st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                hashlib.sha256(current_payload).hexdigest(),
            ) != original_stats[key]:
                raise ValueError(f"source cache changed while packing: {path}")
            if on_progress:
                on_progress(0.5 + 0.5 * (index + 1) / max(1, len(entries)))
    finally:
        store.close()
    # A late addition must not disappear when an explicitly authorized caller
    # subsequently removes the old directory.
    if [(k, p) for k, p in cache_entries(container)] != entries:
        raise ValueError("source cache entry set changed while packing")
    if not fingerprints_match(
        source_before, quick_fingerprint(source), allow_mtime_skew=False
    ):
        raise ValueError("source file changed while packing its cache")
    manifest["complete"] = True
    final_manifest = json.dumps(manifest, indent=1).encode("utf-8")
    with SQLiteStore(target) as store:
        store.write_bytes(MANIFEST_NAME, final_manifest)
    with SQLiteStore(target, read_only=True) as store:
        if store.read_bytes(MANIFEST_NAME) != final_manifest:
            raise ValueError("final manifest verification failed")
    expected[MANIFEST_NAME] = (len(final_manifest), hashlib.sha256(final_manifest).hexdigest())
    # Windows FlushFileBuffers (used by fsync/_commit) needs a writable handle.
    # r+b does not truncate or rewrite the independently verified container.
    with open(target_native, "r+b") as handle:
        os.fsync(handle.fileno())
    return {
        "source_cache": str(container), "target": str(target),
        "source": str(source), "entries": len(expected),
        "payload_bytes": sum(size for size, _ in expected.values()),
        "file_bytes": os.stat(target_native).st_size,
        "verified": True, "manifest": manifest,
    }
