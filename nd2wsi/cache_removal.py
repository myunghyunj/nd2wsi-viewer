"""Filesystem safeguards used after a registry authorizes cache removal.

This module owns identity-checked deletion and durable annotation rescue, not
slide registration or HTTP routing. Its callers retain the writer/build locks
through rescue and rename, and transfer descriptor ownership to deletion.
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any


def _validate_file_cache_for_trash(path: Path, manifest: dict[str, Any]) -> None:
    """Fail closed on embedded user work, unfamiliar formats, or active journals."""
    from .cache import MANIFEST_NAME, SINGLE_FILE_FORMAT, STORE_NAME
    from .storage.single_file import SQLiteStore

    for suffix in ("-journal", "-wal", "-shm"):
        companion = path.with_name(path.name + suffix)
        if companion.exists() or companion.is_symlink():
            raise ValueError("cache has a SQLite journal; close its writer before deleting")
    with SQLiteStore(path, read_only=True) as store:
        current = json.loads(store.read_bytes(MANIFEST_NAME))
        if not isinstance(current, dict) or not current.get("complete"):
            raise ValueError("refusing to delete an incomplete cache")
        kind = current.get("kind")
        expected_format = "nd2wsi-plate/2" if kind == "plate" else SINGLE_FILE_FORMAT
        if kind not in ("full", "overview", "plate") or current.get("format") != expected_format:
            raise ValueError("refusing to delete an unfamiliar cache format")
        if not manifest.get("generation") or current.get("generation") != manifest.get("generation"):
            raise ValueError("cache generation changed; reopen before deleting")
        prefix = "thumbs.zarr" if kind == "plate" else STORE_NAME
        arrays = {"thumbs", "done", "digest", "focus"} if kind == "plate" else None
        for key in store.list_keys():
            if key == MANIFEST_NAME:
                continue
            parts = key.split("/")
            owned = len(parts) == 2 and parts[0] == prefix and parts[1] in (
                ".zattrs", ".zgroup", ".zmetadata",
            )
            if len(parts) == 3 and parts[0] == prefix:
                array = parts[1] in arrays if arrays is not None else parts[1].isdigit()
                owned = array and (
                    parts[2] in (".zarray", ".zattrs")
                    or re.fullmatch(r"\d+(?:\.\d+)*", parts[2]) is not None
                )
            if not owned:
                raise ValueError(f"unknown or user-owned embedded entry; cache preserved: {key}")


def _delete_verified_cache_file(path: Path, expected: os.stat_result, root_fd: int,
                                on_progress=None) -> int:
    """Unlink only the renamed, still-guarded regular file; never follow links."""
    try:
        current = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or not _same_file_identity(current, expected)
                or not _same_file_identity(os.fstat(root_fd), current)):
            raise OSError(f"cache changed before deletion; retained safely at {path}")
        size = current.st_size
        if os.name == "nt":
            from .windows_fs import _delete_handle, open_guard

            fd = open_guard(path, delete=True)
            try:
                if not _same_file_identity(os.fstat(fd), expected):
                    raise OSError(f"cache changed before deletion: {path}")
                _delete_handle(fd)
            finally:
                os.close(fd)
        else:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            parent_fd = os.open(path.parent, flags)
            try:
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or not _same_file_identity(current, expected):
                    raise OSError(f"cache changed before deletion: {path}")
                os.unlink(path.name, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        if on_progress:
            on_progress(1.0)
        return size
    finally:
        os.close(root_fd)


def rescue_annotations(folder: str | Path, home: str | Path) -> list[Path]:
    """Copy annotation sidecars out of ``folder`` before it is deleted.

    Annotations belong beside the slide, but a store built by an older
    version may hold them, and they are work rather than cache. Returns the
    new safe copies. Any copy failure is fatal to cache deletion: the caller
    must never destroy the only copy of user work.
    """
    folder, home = Path(folder), Path(home)
    try:
        folder_stat = folder.stat(follow_symlinks=False)
    except OSError as exc:
        raise OSError(f"could not inspect annotation source: {folder}") from exc
    if not stat.S_ISDIR(folder_stat.st_mode):
        raise ValueError(f"annotation source is not a real directory: {folder}")
    home.mkdir(parents=True, exist_ok=True)
    folder_resolved = folder.resolve()
    home_resolved = home.resolve()
    if home_resolved == folder_resolved or folder_resolved in home_resolved.parents:
        raise ValueError("annotation rescue destination is inside the doomed cache")
    saved = []
    for path in folder.rglob("annotations_*.json"):
        try:
            source = path.read_bytes()
        except OSError as exc:
            raise OSError(f"could not read annotation before cache deletion: {path}") from exc
        target = home / path.name
        while True:
            fd = None
            owned_target = False
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                owned_target = True
            except FileExistsError:
                try:
                    target_stat = target.lstat()
                    # Never count a symlink as a safe rescue. It may merely
                    # point back into the cache that is about to disappear.
                    target_resolved = target.resolve(strict=True)
                    outside_doomed = not (
                        target_resolved == folder_resolved
                        or folder_resolved in target_resolved.parents
                    )
                    if (
                        stat.S_ISREG(target_stat.st_mode)
                        and outside_doomed
                        and target.read_bytes() == source
                    ):
                        break  # an identical safe copy already exists
                except OSError:
                    pass
                # A same-named but distinct annotation is still user work.
                # UUID allocation plus O_EXCL makes concurrent rescues safe.
                target = home / (
                    f"{path.stem}.rescued-{time.strftime('%Y%m%dT%H%M%S')}-"
                    f"{uuid.uuid4().hex[:8]}{path.suffix}"
                )
                continue
            try:
                with os.fdopen(fd, "wb") as out:
                    fd = None  # the file object owns it now
                    out.write(source)
                    out.flush()
                    os.fsync(out.fileno())
                if target.read_bytes() != source:
                    raise OSError(f"annotation verification failed: {target}")
                # File fsync does not necessarily persist its new directory
                # entry. Flush the destination directory when the filesystem
                # supports it before the embedded original can be deleted.
                dir_fd = None
                try:
                    # CRT directory fsync is unavailable on Windows. The file
                    # itself was flushed with FlushFileBuffers via os.fsync.
                    if os.name != "nt":
                        dir_fd = os.open(
                            home,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                        )
                        os.fsync(dir_fd)
                except OSError as exc:
                    if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
                        raise
                finally:
                    if dir_fd is not None:
                        os.close(dir_fd)
            except BaseException:
                if fd is not None:
                    os.close(fd)
                if owned_target:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            saved.append(target)
            break
    return saved


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_child_directory(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> int:
    """Open exactly one already-inspected child without following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not _same_file_identity(opened, expected)
            or not _same_file_identity(opened, current)
        ):
            raise OSError(errno.EPERM, "cache directory changed during deletion", name)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _tree_totals_fd(directory_fd: int) -> tuple[int, int]:
    files = 0
    logical_bytes = 0
    for name in os.listdir(directory_fd):
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(entry.st_mode):
            child_fd = _open_child_directory(directory_fd, name, entry)
            try:
                child_files, child_bytes = _tree_totals_fd(child_fd)
            finally:
                os.close(child_fd)
            files += child_files
            logical_bytes += child_bytes
        else:
            files += 1
            logical_bytes += int(entry.st_size)
    return files, logical_bytes


def _delete_tree_contents_fd(
    directory_fd: int,
    *,
    total: int,
    progress: list[int],
    on_progress: Any,
) -> int:
    """Delete one opened tree without ever following a pathname symlink."""
    freed = 0
    for name in os.listdir(directory_fd):
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(entry.st_mode):
            child_fd = _open_child_directory(directory_fd, name, entry)
            try:
                freed += _delete_tree_contents_fd(
                    child_fd,
                    total=total,
                    progress=progress,
                    on_progress=on_progress,
                )
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _same_file_identity(current, entry):
                raise OSError(
                    errno.EPERM,
                    "cache directory changed during deletion",
                    name,
                )
            os.rmdir(name, dir_fd=directory_fd)
            continue

        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        freed += int(entry.st_size)
        progress[0] += 1
        if on_progress:
            on_progress(min(progress[0] / max(1, total), 0.99))
    return freed


def _delete_verified_cache_tree(
    path: Path,
    expected: os.stat_result,
    on_progress: Any = None,
    root_fd: int | None = None,
) -> int:
    """Remove the captured cache inode with fd-relative, no-follow traversal."""
    if os.name == "nt":
        from .windows_fs import delete_verified_tree

        if root_fd is not None:
            os.close(root_fd)
        return delete_verified_tree(path, expected, on_progress)
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_fd = None
    try:
        # The caller may transfer an already-open cache root. Opening the
        # parent can itself fail, so enter the cleanup scope before that first
        # operation or the transferred descriptor would leak.
        parent_fd = os.open(path.parent, parent_flags)
        if root_fd is None:
            root_fd = _open_child_directory(parent_fd, path.name, expected)
        else:
            opened = os.fstat(root_fd)
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not _same_file_identity(opened, expected)
                or not _same_file_identity(opened, current)
            ):
                raise OSError(
                    errno.EPERM,
                    "cache root changed during deletion",
                    str(path),
                )
        try:
            total, _ = _tree_totals_fd(root_fd)
            freed = _delete_tree_contents_fd(
                root_fd,
                total=total,
                progress=[0],
                on_progress=on_progress,
            )
        finally:
            os.close(root_fd)
            root_fd = None
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_file_identity(current, expected):
            raise OSError(
                errno.EPERM,
                "cache root changed during deletion",
                str(path),
            )
        os.rmdir(path.name, dir_fd=parent_fd)
    except Exception as exc:
        raise OSError(
            f"cache deletion incomplete; remaining data was kept at {path}: {exc}"
        ) from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    if on_progress:
        on_progress(1.0)
    return freed
