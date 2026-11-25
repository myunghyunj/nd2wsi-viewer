"""Regression tests for Windows 10 compatibility fixes (2.6.7).

Covers:
1. Name-surrogate vs non-surrogate Windows reparse point classification
   (OneDrive Cloud Files, NTFS deduplication, junctions, symlinks, fail-closed,
   and GetFileInformationByHandleEx(FileAttributeTagInfo) fd queries).
2. All hardened filesystem modules accepting OneDrive placeholders while still
   rejecting name-surrogate links.
3. AnnotationWorkspace Windows PermissionError atomic-replace retry.
4. pywebview 6.2.1 win32 storage_path alongside private_mode=True.
5. packaging/launch.py empty/unset LOCALAPPDATA fallback.
6. packaging/windows-installer.nsi Win10 hardening (CheckPathChain OS/volume
   ancestor scoping, Defender MoveFileExW/DeleteFileW retry helpers, and
   APP_REG_KEY uninstall ownership check).
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import re
import runpy
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import (
    annotation_workspace,
    app,
    cache,
    cache_removal,
    cli,
    lut_settings,
    migration,
    session_lock,
    windows_fs,
)
from nd2wsi.window_sessions import create_window_session

ROOT = Path(__file__).resolve().parents[1]

TAG_ONEDRIVE_CLOUD = 0x9000001A
TAG_NTFS_DEDUP = 0x80000013
TAG_JUNCTION = 0xA0000003
TAG_SYMLINK = 0xA000000C
ATTR_REPARSE = 0x400


class _StatProxy:
    """Wrap a real os.stat_result with synthetic Windows reparse attributes."""

    def __init__(self, real: os.stat_result, *, attrs: int, tag: int = 0):
        self._real = real
        self.st_file_attributes = attrs
        self.st_reparse_tag = tag

    def __getattr__(self, name: str):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# 1. is_windows_name_surrogate_reparse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attrs,tag,expected",
    [
        (0, 0, False),
        (0x10, 0, False),
        (ATTR_REPARSE, TAG_ONEDRIVE_CLOUD, False),
        (ATTR_REPARSE, 0x9000101A, False),  # Cloud partial placeholder
        (ATTR_REPARSE, TAG_NTFS_DEDUP, False),
        (ATTR_REPARSE, TAG_JUNCTION, True),
        (ATTR_REPARSE, TAG_SYMLINK, True),
        (ATTR_REPARSE, 0, True),  # missing/zero tag fails closed
    ],
)
def test_is_windows_name_surrogate_reparse_tags(attrs: int, tag: int, expected: bool) -> None:
    st = SimpleNamespace(st_file_attributes=attrs, st_reparse_tag=tag)
    assert windows_fs.is_windows_name_surrogate_reparse(st) is expected


def test_is_windows_name_surrogate_reparse_none_and_missing_attr() -> None:
    assert windows_fs.is_windows_name_surrogate_reparse(None) is False
    # Attribute bit set with no st_reparse_tag attribute at all -> fail closed.
    assert windows_fs.is_windows_name_surrogate_reparse(SimpleNamespace(st_file_attributes=ATTR_REPARSE)) is True
    assert windows_fs.is_windows_name_surrogate_reparse(SimpleNamespace()) is False


@pytest.mark.parametrize(
    "query_ok,q_attrs,q_tag,expected",
    [
        (True, ATTR_REPARSE, TAG_ONEDRIVE_CLOUD, False),
        (True, ATTR_REPARSE, TAG_NTFS_DEDUP, False),
        (True, ATTR_REPARSE, TAG_JUNCTION, True),
        (True, ATTR_REPARSE, TAG_SYMLINK, True),
        (True, 0, 0, False),  # reparse bit no longer set on handle
        (True, ATTR_REPARSE, 0, True),  # zero tag fails closed
        (False, ATTR_REPARSE, TAG_ONEDRIVE_CLOUD, True),  # Win32 query failed -> fail closed
    ],
)
def test_is_windows_name_surrogate_reparse_fd_query(
    monkeypatch: pytest.MonkeyPatch,
    query_ok: bool,
    q_attrs: int,
    q_tag: int,
    expected: bool,
) -> None:
    seen: list[tuple[int, int, int]] = []

    class _Query:
        def __call__(self, handle, info_class, output, size):
            seen.append((handle, info_class, size))
            output._obj.FileAttributes = q_attrs
            output._obj.ReparseTag = q_tag
            return query_ok

    monkeypatch.setattr(windows_fs.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd + 1000))
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **kw: SimpleNamespace(GetFileInformationByHandleEx=_Query()),
        raising=False,
    )

    fstat_result = SimpleNamespace(st_file_attributes=ATTR_REPARSE, st_reparse_tag=0)
    assert windows_fs.is_windows_name_surrogate_reparse(fstat_result, fd=7) is expected
    assert seen == [(1007, windows_fs._FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.sizeof(windows_fs._FileAttributeTagInfo))]


def test_is_windows_name_surrogate_reparse_fd_oserror_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_fs.sys, "platform", "win32")
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(get_osfhandle=Mock(side_effect=OSError("bad fd"))),
    )
    fstat_result = SimpleNamespace(st_file_attributes=ATTR_REPARSE, st_reparse_tag=0)
    assert windows_fs.is_windows_name_surrogate_reparse(fstat_result, fd=9) is True


# ---------------------------------------------------------------------------
# 2. All 8 updated Python modules accept OneDrive placeholders & reject links
# ---------------------------------------------------------------------------


def test_session_lock_accepts_onedrive_and_rejects_surrogate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "slide.lock"
    lock_path.write_text("{}", encoding="utf-8")
    real_lstat = os.lstat
    real_fstat = os.fstat
    real_stat = os.stat
    state = {"tag": TAG_ONEDRIVE_CLOUD}

    def fake_lstat(p, *a, **kw):
        res = real_lstat(p, *a, **kw)
        if Path(p) == lock_path:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=state["tag"])
        return res

    def fake_stat(p, *a, **kw):
        res = real_stat(p, *a, **kw)
        if isinstance(p, (str, os.PathLike)) and Path(p) == lock_path:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=state["tag"])
        return res

    def fake_fstat(fd):
        res = real_fstat(fd)
        return _StatProxy(res, attrs=ATTR_REPARSE, tag=0)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(windows_fs, "_query_fd_reparse_info", lambda _fd: (ATTR_REPARSE, state["tag"]))

    lock = session_lock.SessionFileLock(lock_path)
    lock.acquire(timeout=1.0)
    try:
        assert lock.acquired
    finally:
        lock.release()

    state["tag"] = TAG_JUNCTION
    with pytest.raises(OSError, match="reparse"):
        session_lock.SessionFileLock(lock_path).acquire(timeout=0.1)


def test_annotation_workspace_accepts_onedrive_and_rejects_surrogate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = tmp_path / "annotations_slide.json"
    sidecar.write_bytes(b'{"items": [{"id": "ok"}]}')
    real_lstat = os.lstat
    real_fstat = os.fstat
    state = {"lstat_tag": TAG_ONEDRIVE_CLOUD, "fstat_tag": TAG_ONEDRIVE_CLOUD}

    def fake_lstat(p, *a, **kw):
        res = real_lstat(p, *a, **kw)
        if Path(p) == sidecar:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=state["lstat_tag"])
        return res

    def fake_fstat(fd):
        return _StatProxy(real_fstat(fd), attrs=ATTR_REPARSE, tag=0)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(windows_fs, "_query_fd_reparse_info", lambda _fd: (ATTR_REPARSE, state["fstat_tag"]))

    assert annotation_workspace._read(sidecar) == b'{"items": [{"id": "ok"}]}'

    # Surrogate detected at fstat time (TOCTOU swap)
    state["fstat_tag"] = TAG_SYMLINK
    with pytest.raises(OSError, match="regular file"):
        annotation_workspace._read(sidecar)

    # Surrogate detected at lstat time
    state["lstat_tag"] = TAG_SYMLINK
    with pytest.raises(OSError, match="symlinked"):
        annotation_workspace._read(sidecar)


def test_cache_lock_accepts_onedrive_and_rejects_surrogate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "slide.nd2svs"
    lock = cache.CacheLock(target)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    # Write a stale released lockfile so CacheLock takes the FileExistsError -> lstat/fstat path.
    lock.path.write_text(json.dumps({"pid": 999999, "released": True, "time": 0}), encoding="utf-8")

    real_lstat = os.lstat
    real_fstat = os.fstat
    state = {"tag": TAG_ONEDRIVE_CLOUD}

    def fake_lstat(p, *a, **kw):
        res = real_lstat(p, *a, **kw)
        if Path(p) == lock.path:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=state["tag"])
        return res

    def fake_fstat(fd):
        res = real_fstat(fd)
        if res.st_ino == real_lstat(lock.path).st_ino:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=0)
        return res

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(windows_fs, "_query_fd_reparse_info", lambda _fd: (ATTR_REPARSE, state["tag"]))

    lock.acquire(timeout=1.0, poll=0.01)
    try:
        assert lock._fd is not None
    finally:
        lock.release()

    # Now simulate symlink reparse point on the lockfile: must refuse to reclaim.
    state["tag"] = TAG_SYMLINK
    lock2 = cache.CacheLock(target)
    with pytest.raises(TimeoutError):
        lock2.acquire(timeout=0.04, poll=0.01)


def test_migration_accepts_onedrive_and_rejects_surrogate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = tmp_path / "legacy.nd2wsi-cache"
    (container / "store.ome.zarr" / "0").mkdir(parents=True)
    manifest = container / "manifest.json"
    manifest.write_bytes(b'{"complete": true}')
    chunk = container / "store.ome.zarr" / "0" / "0.0.0"
    chunk.write_bytes(b"chunk-data")

    real_lstat = os.lstat
    real_fstat = os.fstat
    state = {"dir_tag": TAG_ONEDRIVE_CLOUD, "file_tag": TAG_ONEDRIVE_CLOUD}

    def fake_lstat(p, *a, **kw):
        res = real_lstat(p, *a, **kw)
        path = Path(p)
        if path == container or container in path.parents:
            tag = state["dir_tag"] if stat.S_ISDIR(res.st_mode) else state["file_tag"]
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=tag)
        return res

    def fake_fstat(fd):
        return _StatProxy(real_fstat(fd), attrs=ATTR_REPARSE, tag=0)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(windows_fs, "_query_fd_reparse_info", lambda _fd: (ATTR_REPARSE, state["file_tag"]))

    entries = migration.cache_entries(container)
    assert [k for k, _ in entries] == ["manifest.json", "store.ome.zarr/0/0.0.0"]
    _, raw = migration._read_regular_entry(manifest)
    assert raw == b'{"complete": true}'

    # Junction directory is rejected.
    state["dir_tag"] = TAG_JUNCTION
    with pytest.raises(ValueError, match="not a real cache directory"):
        migration.cache_entries(container)

    # Symlink file is rejected.
    state["dir_tag"] = TAG_ONEDRIVE_CLOUD
    state["file_tag"] = TAG_SYMLINK
    with pytest.raises(ValueError, match="not a regular cache entry"):
        migration.cache_entries(container)


@pytest.mark.parametrize("fake_nt", [False, True])
def test_cache_removal_accepts_onedrive_and_rejects_surrogate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_nt: bool
) -> None:
    folder = tmp_path / "doomed.nd2wsi-cache"
    folder.mkdir()
    ann = folder / "annotations_slide.json"
    ann.write_bytes(b'{"items": [{"id": "keep"}]}')
    home = tmp_path / "rescued"

    real_path_stat = Path.stat
    real_fstat = os.fstat
    real_scandir = os.scandir
    state = {"dir_tag": TAG_ONEDRIVE_CLOUD, "file_tag": TAG_ONEDRIVE_CLOUD}

    def fake_path_stat(self, *a, **kw):
        res = real_path_stat(self, *a, **kw)
        if self == folder:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=state["dir_tag"])
        if self == ann:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=state["file_tag"])
        return res

    def fake_fstat(fd):
        return _StatProxy(real_fstat(fd), attrs=ATTR_REPARSE, tag=0)

    class _FakeDirEntry:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def stat(self, *, follow_symlinks=True):
            res = self._entry.stat(follow_symlinks=follow_symlinks)
            tag = state["dir_tag"] if stat.S_ISDIR(res.st_mode) else state["file_tag"]
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=tag)

    monkeypatch.setattr(Path, "stat", fake_path_stat)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(windows_fs, "_query_fd_reparse_info", lambda _fd: (ATTR_REPARSE, state["file_tag"]))
    if fake_nt:
        class _FakeOs:
            name = "nt"

            def scandir(self, p):
                return [_FakeDirEntry(e) for e in real_scandir(p)]

            def __getattr__(self, item: str):
                return getattr(os, item)

        monkeypatch.setattr(cache_removal, "os", _FakeOs())

    saved = cache_removal.rescue_annotations(folder, home)
    assert len(saved) == 1
    assert saved[0].read_bytes() == b'{"items": [{"id": "keep"}]}'

    # Junction folder rejected.
    state["dir_tag"] = TAG_JUNCTION
    with pytest.raises(ValueError, match="not a real directory"):
        cache_removal.rescue_annotations(folder, home)

    # Symlinked annotation rejected.
    state["dir_tag"] = TAG_ONEDRIVE_CLOUD
    state["file_tag"] = TAG_SYMLINK
    with pytest.raises(ValueError, match="refusing to read"):
        cache_removal.rescue_annotations(folder, tmp_path / "rescued2")


def test_lut_settings_stat_signature_accepts_onedrive_and_rejects_surrogate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "slide.nd2"
    source.write_bytes(b"pixel-bytes-for-lut-test")
    captured = source.stat()
    real_fstat = os.fstat
    real_close = os.close
    guards: set[int] = set()
    state = {"tag": TAG_ONEDRIVE_CLOUD}

    def open_guard(path):
        fd = os.open(path, os.O_RDONLY)
        guards.add(fd)
        return fd

    def fake_fstat(fd):
        res = real_fstat(fd)
        if fd in guards:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=0)
        return res

    def fake_close(fd):
        guards.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(lut_settings.sys, "platform", "win32")
    monkeypatch.setattr(windows_fs, "file_change_time", lambda _p, _s: None)
    monkeypatch.setattr(windows_fs, "open_guard", open_guard)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(os, "close", fake_close)
    monkeypatch.setattr(windows_fs, "_query_fd_reparse_info", lambda _fd: (ATTR_REPARSE, state["tag"]))

    sig = lut_settings._stat_signature(source)
    assert "sampled-content" in sig
    assert sig[2] == captured.st_size
    assert not guards

    state["tag"] = TAG_SYMLINK
    with pytest.raises(OSError, match="source identity changed"):
        lut_settings._stat_signature(source)
    assert not guards


def test_cli_tidy_corrupt_caches_accepts_onedrive_and_rejects_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    caches = tmp_path / "nd2wsi" / "caches"
    caches.mkdir(parents=True)
    corpse = caches / "slide.nd2wsi-cache.corrupt-20261121T000000"
    corpse.mkdir()

    real_path_stat = Path.stat
    state = {"tag": TAG_ONEDRIVE_CLOUD}

    def fake_path_stat(self, *a, **kw):
        res = real_path_stat(self, *a, **kw)
        if self == corpse:
            return _StatProxy(res, attrs=ATTR_REPARSE, tag=state["tag"])
        return res

    monkeypatch.setattr(Path, "stat", fake_path_stat)

    rc = cli.main(["tidy", str(tmp_path), "--remove-corrupt-caches", "--dry-run"])
    assert rc == 0
    assert f"would remove {corpse.name}" in capsys.readouterr().out

    state["tag"] = TAG_JUNCTION
    rc = cli.main(["tidy", str(tmp_path), "--remove-corrupt-caches", "--dry-run"])
    assert rc == 0
    assert f"kept {corpse.name}" in capsys.readouterr().out


def test_windows_fs_file_change_time_accepts_onedrive_and_rejects_junction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = SimpleNamespace(
        st_dev=1, st_ino=2, st_size=10, st_mtime_ns=100, st_ctime_ns=200,
        st_birthtime_ns=200, st_mode=stat.S_IFREG | 0o600,
        st_file_attributes=ATTR_REPARSE, st_reparse_tag=TAG_ONEDRIVE_CLOUD,
    )
    opened = SimpleNamespace(
        st_dev=1, st_ino=2, st_size=10, st_mtime_ns=100, st_ctime_ns=200,
        st_birthtime_ns=200, st_mode=stat.S_IFREG | 0o600,
        st_file_attributes=ATTR_REPARSE, st_reparse_tag=0,
    )
    state = {"tag": TAG_ONEDRIVE_CLOUD, "stamp": 133000000000000099}

    class _Query:
        def __call__(self, handle, info_class, output, size):
            if info_class == windows_fs._FILE_ATTRIBUTE_TAG_INFO_CLASS:
                output._obj.FileAttributes = ATTR_REPARSE
                output._obj.ReparseTag = state["tag"]
                return True
            output._obj.ChangeTime = state["stamp"]
            output._obj.FileAttributes = ATTR_REPARSE
            return True

    class _Volume:
        def __call__(self, handle, label, label_size, serial, component, flags, output, size):
            output.value = "NTFS"
            return True

    closed: list[int] = []
    monkeypatch.setattr(windows_fs.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **kw: SimpleNamespace(
            GetFileInformationByHandleEx=_Query(), GetVolumeInformationByHandleW=_Volume()
        ),
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd + 50))
    monkeypatch.setattr(windows_fs, "open_guard", lambda _p: 11)
    monkeypatch.setattr(os, "fstat", lambda _fd: opened)
    monkeypatch.setattr(os, "close", closed.append)

    assert windows_fs.file_change_time(Path("cloud.nd2"), expected) == state["stamp"]
    assert closed == [11]

    state["tag"] = TAG_NTFS_DEDUP
    assert windows_fs.file_change_time(Path("dedup.nd2"), expected) == state["stamp"]

    state["tag"] = TAG_JUNCTION
    with pytest.raises(OSError, match="identity changed"):
        windows_fs.file_change_time(Path("link.nd2"), expected)


def test_windows_fs_delete_verified_tree_handles_onedrive_and_junction_fds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_st = SimpleNamespace(
        st_dev=1, st_ino=10, st_size=0, st_mode=stat.S_IFDIR | 0o700,
        st_file_attributes=ATTR_REPARSE, st_reparse_tag=0,
    )
    file_st = SimpleNamespace(
        st_dev=1, st_ino=11, st_size=42, st_mode=stat.S_IFREG | 0o600,
        st_file_attributes=ATTR_REPARSE, st_reparse_tag=0,
    )
    junction_st = SimpleNamespace(
        st_dev=1, st_ino=12, st_size=0, st_mode=stat.S_IFDIR | 0o700,
        st_file_attributes=ATTR_REPARSE, st_reparse_tag=0,
    )
    fd_stats = {100: root_st, 101: file_st, 102: junction_st}
    fd_tags = {
        100: (ATTR_REPARSE, TAG_ONEDRIVE_CLOUD),
        101: (ATTR_REPARSE, TAG_ONEDRIVE_CLOUD),
        102: (ATTR_REPARSE, TAG_JUNCTION),
    }
    enumerated: list[int] = []
    deleted: list[int] = []

    def fake_dir_names(fd: int):
        enumerated.append(fd)
        if fd == 100:
            return ["chunk.bin", "link_dir"]
        pytest.fail(f"must not enumerate junction fd={fd}")

    def fake_open_child(parent_fd: int, name: str, *, delete: bool) -> int:
        assert parent_fd == 100
        return 101 if name == "chunk.bin" else 102

    monkeypatch.setattr(windows_fs, "open_guard", lambda _p, delete=False: 100)
    monkeypatch.setattr(os, "fstat", lambda fd: fd_stats[fd])
    monkeypatch.setattr(os, "close", lambda _fd: None)
    monkeypatch.setattr(windows_fs, "_query_fd_reparse_info", lambda fd: fd_tags[fd])
    monkeypatch.setattr(windows_fs, "_directory_names", fake_dir_names)
    monkeypatch.setattr(windows_fs, "_open_child", fake_open_child)
    monkeypatch.setattr(windows_fs, "_delete_handle", deleted.append)

    freed = windows_fs.delete_verified_tree(Path("cache"), root_st)
    assert freed == 42
    assert enumerated == [100]
    assert deleted == [101, 102, 100]

    # Junction root itself is rejected before enumeration.
    fd_tags[100] = (ATTR_REPARSE, TAG_JUNCTION)
    enumerated.clear()
    with pytest.raises(OSError, match="not a real directory"):
        windows_fs.delete_verified_tree(Path("cache"), root_st)
    assert enumerated == []


# ---------------------------------------------------------------------------
# 3. AnnotationWorkspace Windows PermissionError atomic-replace retry
# ---------------------------------------------------------------------------


def test_annotation_workspace_atomic_write_retries_permission_error_on_win32(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "annotations_slide.json"
    target.write_bytes(b'{"items": []}')
    real_replace = os.replace
    attempts = {"count": 0}
    sleeps: list[float] = []

    def flaky_replace(src, dst):
        attempts["count"] += 1
        if attempts["count"] <= 3:
            raise PermissionError(13, "Access is denied (Defender scan)")
        return real_replace(src, dst)

    monkeypatch.setattr(annotation_workspace.sys, "platform", "win32")
    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    annotation_workspace._atomic_write(target, b'{"items": [{"id": "saved"}]}')
    assert target.read_bytes() == b'{"items": [{"id": "saved"}]}'
    assert attempts["count"] == 4
    assert sleeps == [annotation_workspace._WINDOWS_REPLACE_DELAY_S] * 3
    assert list(tmp_path.glob("*.tmp")) == []


def test_annotation_workspace_atomic_write_exhausts_retries_and_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "annotations_slide.json"
    target.write_bytes(b'{"items": ["original"]}')
    attempts = {"count": 0}
    sleeps: list[float] = []

    def always_denied(_src, _dst):
        attempts["count"] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(annotation_workspace.sys, "platform", "win32")
    monkeypatch.setattr(os, "replace", always_denied)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    with pytest.raises(PermissionError):
        annotation_workspace._atomic_write(target, b'{"items": ["new"]}')
    assert attempts["count"] == annotation_workspace._WINDOWS_REPLACE_ATTEMPTS
    assert len(sleeps) == annotation_workspace._WINDOWS_REPLACE_ATTEMPTS - 1
    assert target.read_bytes() == b'{"items": ["original"]}'
    assert list(tmp_path.glob("*.tmp")) == []

    # Non-Windows does not sleep/retry on PermissionError.
    attempts["count"] = 0
    sleeps.clear()
    monkeypatch.setattr(annotation_workspace.sys, "platform", "darwin")
    with pytest.raises(PermissionError):
        annotation_workspace._atomic_write(target, b'{"items": ["new"]}')
    assert attempts["count"] == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# 4. app.py win32 storage_path alongside private_mode=True
# ---------------------------------------------------------------------------


def test_app_win32_passes_session_storage_path_alongside_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path))
    api = SimpleNamespace(_startup_error=None, _stop_server_for_update=Mock())
    create = Mock(return_value=(api, object()))
    monkeypatch.setattr(app, "create_app_window", create)
    webview = SimpleNamespace(start=Mock())
    monkeypatch.setitem(sys.modules, "webview", webview)

    assert app.main([]) == 0
    session = create.call_args.kwargs["window_session"]
    expected_udf = session.root / "webview2-udf"
    assert expected_udf.is_dir()
    webview.start.assert_called_once_with(
        gui="edgechromium",
        private_mode=True,
        storage_path=str(expected_udf),
    )


def test_app_win32_guards_lightweight_window_session_without_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    lightweight = SimpleNamespace(role="user", id="mock-id", mark_closed=Mock())
    monkeypatch.setattr("nd2wsi.window_sessions.create_window_session", lambda _role: lightweight)
    api = SimpleNamespace(_startup_error=None, _stop_server_for_update=Mock())
    monkeypatch.setattr(app, "create_app_window", Mock(return_value=(api, object())))
    webview = SimpleNamespace(start=Mock())
    monkeypatch.setitem(sys.modules, "webview", webview)

    assert app.main([]) == 0
    webview.start.assert_called_once_with(gui="edgechromium", private_mode=True)
    lightweight.mark_closed.assert_called_once()


def test_app_darwin_does_not_pass_storage_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path))
    api = SimpleNamespace(_startup_error=None, _stop_server_for_update=Mock())
    monkeypatch.setattr(app, "create_app_window", Mock(return_value=(api, object())))
    webview = SimpleNamespace(start=Mock())
    monkeypatch.setitem(sys.modules, "webview", webview)

    assert app.main([]) == 0
    webview.start.assert_called_once_with(private_mode=True)


# ---------------------------------------------------------------------------
# 5. packaging/launch.py empty/unset LOCALAPPDATA fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("localappdata", ["", None])
def test_launch_py_empty_or_unset_localappdata_uses_home_appdata_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, localappdata: str | None
) -> None:
    launcher = ROOT / "packaging" / "launch.py"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd_before = tmp_path / "cwd"
    cwd_before.mkdir()
    monkeypatch.chdir(cwd_before)

    raw = io.BytesIO()
    inherited = io.TextIOWrapper(raw, encoding="utf-8")
    stream = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys, "platform", "win32")
            patch.setattr(sys, "argv", ["nd2wsi-viewer.exe"])
            patch.setattr(sys, "stdout", inherited)
            patch.setattr(sys, "stderr", inherited)
            patch.setattr(Path, "home", classmethod(lambda cls: fake_home))
            patch.delenv("ND2WSI_LOG_FILE", raising=False)
            if localappdata is None:
                patch.delenv("LOCALAPPDATA", raising=False)
            else:
                patch.setenv("LOCALAPPDATA", localappdata)
            runpy.run_path(str(launcher), run_name="__launch_localappdata_test__")
            stream = sys.stdout
            log_path = Path(stream.name)
        expected_dir = fake_home / "AppData" / "Local" / "nd2wsi-viewer" / "logs"
        assert log_path.parent == expected_dir
        assert log_path.is_file()
        assert not (cwd_before / "nd2wsi-viewer").exists()
    finally:
        if stream is not None:
            stream.close()
        inherited.close()


# ---------------------------------------------------------------------------
# 6. packaging/windows-installer.nsi Win10 hardening contracts
# ---------------------------------------------------------------------------


def test_nsis_app_reg_key_uninstall_ownership_guard() -> None:
    nsi = (ROOT / "packaging" / "windows-installer.nsi").read_text(encoding="utf-8")
    fn_start = nsi.index("Function un.RemoveOwnedAppRegKey")
    fn_end = nsi.index("FunctionEnd", fn_start)
    fn_body = nsi[fn_start:fn_end]

    assert 'ReadRegStr $R0 HKCU "${APP_REG_KEY}\\shell\\open\\command" ""' in fn_body
    assert '"$INSTDIR\\${APP_EXE}" "%1"' in fn_body
    assert "Call un.NormalizePath" in fn_body
    assert '${GetParent} "$R1" $R2' in fn_body
    assert "$R2 == $INSTDIR" in fn_body
    assert 'DeleteRegKey HKCU "${APP_REG_KEY}"' in fn_body

    remove_start = nsi.index("Function un.RemoveFileAssociations")
    remove_end = nsi.index("FunctionEnd", remove_start)
    remove_body = nsi[remove_start:remove_end]
    assert "Call un.RemoveOwnedAppRegKey" in remove_body
    assert 'DeleteRegKey HKCU "${APP_REG_KEY}"' not in remove_body


def test_nsis_defender_retry_helpers_wrap_staging_and_removal_ops() -> None:
    nsi = (ROOT / "packaging" / "windows-installer.nsi").read_text(encoding="utf-8")

    for fn_name, win32_api in (
        ("Function ${PREFIX}MoveFileExWithRetry", "kernel32::MoveFileExW"),
        ("Function ${PREFIX}DeleteFileWithRetry", "kernel32::DeleteFileW"),
    ):
        start = nsi.index(fn_name)
        end = nsi.index("FunctionEnd", start)
        body = nsi[start:end]
        assert win32_api in body
        assert "!= 5" in body and "!= 32" in body
        assert "> 10" in body
        assert "Sleep 150" in body

    # Every raw MoveFileExW / DeleteFileW call lives inside its retry helper.
    assert len(re.findall(r"kernel32::MoveFileExW", nsi)) == 1
    assert len(re.findall(r"kernel32::DeleteFileW", nsi)) == 1

    # Critical staging & deletion functions route through the retry helpers.
    for caller, helper in (
        ("Function ${PREFIX}WriteOwnershipMarker", "Call ${PREFIX}MoveFileExWithRetry"),
        ("Function ${PREFIX}WriteOwnershipMarker", "Call ${PREFIX}DeleteFileWithRetry"),
        ("Function WriteSafeUninstaller", "Call MoveFileExWithRetry"),
        ("Function WriteSafeUninstaller", "Call DeleteFileWithRetry"),
        ("Function ${PREFIX}DeleteOwnedFile", "Call ${PREFIX}DeleteFileWithRetry"),
    ):
        start = nsi.index(caller)
        end = nsi.index("FunctionEnd", start)
        assert helper in nsi[start:end], f"{caller} missing {helper}"


def test_nsis_check_path_chain_scopes_ancestors_and_preserves_leaf_guards() -> None:
    nsi = (ROOT / "packaging" / "windows-installer.nsi").read_text(encoding="utf-8")

    os_fn_start = nsi.index("Function ${PREFIX}IsOsOwnedAncestor")
    os_fn_end = nsi.index("FunctionEnd", os_fn_start)
    os_fn = nsi[os_fn_start:os_fn_end]
    for root_var in ("$LOCALAPPDATA", "$APPDATA", "$PROFILE", "$SMPROGRAMS", "$DESKTOP"):
        assert root_var in os_fn
    assert '${GetParent} "$PROFILE"' in os_fn

    chain_start = nsi.index("Function ${PREFIX}CheckPathChain")
    chain_end = nsi.index("FunctionEnd", chain_start)
    chain = nsi[chain_start:chain_end]

    # Volume mount points are detected via GetVolumePathNameW and treated as roots
    # only for ancestors ($R4 != 0), never skipping the leaf ($R4 == 0).
    assert "kernel32::GetVolumePathNameW" in chain
    assert "StrCpy $R4 0" in chain
    assert "${If} $R4 != 0" in chain
    assert "Call ${PREFIX}IsOsOwnedAncestor" in chain
    assert "${AndIf} $R3 == 5" in chain
    assert "Call ${PREFIX}IsNameSurrogateReparse" in chain

    # Structural balance across the entire NSIS script.
    assert len(re.findall(r"(?m)^\s*Function\s+", nsi)) == len(re.findall(r"(?m)^\s*FunctionEnd\s*$", nsi))
    assert len(re.findall(r"(?m)^\s*Section\s+", nsi)) == len(re.findall(r"(?m)^\s*SectionEnd\s*$", nsi))
    assert len(re.findall(r"(?m)^\s*!macro\s+", nsi)) == len(re.findall(r"(?m)^\s*!macroend\s*$", nsi))
    assert len(re.findall(r"\$\{(?:If|IfNot)\}", nsi)) == len(re.findall(r"\$\{EndIf\}", nsi))
    assert len(re.findall(r"\$\{(?:Do|DoWhile)\}", nsi)) == len(re.findall(r"\$\{Loop\}", nsi))


def test_annotation_workspace_end_to_end_write_with_win32_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = create_window_session("user", tmp_path / "sessions")
    workspace = annotation_workspace.AnnotationWorkspace(session)
    sidecar = tmp_path / "annotations_sample.json"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        if Path(dst) == sidecar and calls["n"] < 2:
            calls["n"] += 1
            raise PermissionError(13, "transient Defender lock")
        return real_replace(src, dst)

    monkeypatch.setattr(annotation_workspace.sys, "platform", "win32")
    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    doc, rev = workspace.read(sidecar)
    assert doc == {} and rev == "missing"
    new_rev = workspace.write(sidecar, {"items": [{"id": "roi-1"}]}, rev)
    assert new_rev.startswith("sha256:")
    assert calls["n"] == 2
    saved_doc, saved_rev = workspace.read(sidecar)
    assert saved_doc == {"items": [{"id": "roi-1"}]}
    assert saved_rev == new_rev
