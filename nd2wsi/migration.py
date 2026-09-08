"""Lossless, bounded-memory import of managed directory caches into .nd2svs.

Import never deletes its input. Callers publish the verified file atomically;
removing an old cache is a separate, explicitly authorized operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
    quick_fingerprint,
    source_base,
)


def cache_entries(container: Path) -> list[tuple[str, Path]]:
    """Only cache-owned metadata and numeric Zarr v2 chunks are importable.

    Unknown files, symlinks and annotations make this operation fail closed.
    Finder AppleDouble metadata is ignored, not interpreted as image data.
    """
    if container.is_symlink() or not container.is_dir():
        raise ValueError(f"not a real cache directory: {container}")
    entries = []
    for base, dirs, files in os.walk(container, followlinks=False):
        for name in dirs:
            if (Path(base) / name).is_symlink():
                raise ValueError(f"symlink in cache: {Path(base) / name}")
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"not a regular cache entry: {path}")
            if name.startswith("._") or name == ".DS_Store":
                continue
            key = path.relative_to(container).as_posix()
            owned = key == MANIFEST_NAME or (
                key.startswith((STORE_NAME + "/", "thumbs.zarr/"))
                and (name in (".zgroup", ".zattrs", ".zarray", ".zmetadata")
                     or re.fullmatch(r"\d+(?:\.\d+)*", name))
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
) -> dict[str, Any]:
    """Create and byte-verify a new file; never overwrite or remove anything.

    The sole transformed entry is manifest.json: format and relative source
    location describe the new container. All compressed chunks, array metadata,
    calibration, channels and selections remain byte-for-byte unchanged.
    """
    from . import __version__
    from .storage.single_file import FORMAT_VERSION, SQLiteStore

    container, target = Path(container), Path(target)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    entries = cache_entries(container)
    original_manifest = (container / MANIFEST_NAME).read_bytes()
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
        rel = manifest.get("source", {}).get("relative_path")
        if not isinstance(rel, str) or not rel:
            raise ValueError("cache manifest lacks a relative source path")
        source = (source_base(container) / rel).resolve()
    source = Path(source).resolve()
    if source_info.get("name") != source.name:
        raise ValueError("cache source name does not match its relative path")
    source_before = quick_fingerprint(source)
    if not fingerprints_match(source_info, source_before):
        raise ValueError("cache source fingerprint does not match the source file")
    manifest["format"] = (
        "nd2wsi-plate/2" if manifest.get("kind") == "plate" else SINGLE_FILE_FORMAT
    )
    manifest["source"]["relative_path"] = Path(
        os.path.relpath(source, target.parent)
    ).as_posix()
    manifest.setdefault("storage", {})["container"] = f"nd2svs-sqlite/{FORMAT_VERSION}"
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
    target.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore.create_exclusive(target)
    try:
        # One transaction avoids a journal sync per tiny metadata/chunk entry.
        with store.batch():
            for index, (key, path) in enumerate(entries):
                st = path.stat()
                original_payload = path.read_bytes()
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
            st = path.stat()
            if (
                st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
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
    if not fingerprints_match(source_before, quick_fingerprint(source)):
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
    with target.open("r+b") as handle:
        os.fsync(handle.fileno())
    return {
        "source_cache": str(container), "target": str(target),
        "source": str(source), "entries": len(expected),
        "payload_bytes": sum(size for size, _ in expected.values()),
        "file_bytes": target.stat().st_size,
        "verified": True, "manifest": manifest,
    }
