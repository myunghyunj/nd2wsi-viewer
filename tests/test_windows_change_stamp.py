"""Read-only Windows change-stamp ABI and identity checks on every host."""

import ctypes
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nd2wsi import windows_fs


@pytest.fixture
def native(monkeypatch):
    value = SimpleNamespace(
        st_dev=7, st_ino=13, st_size=24, st_mtime_ns=123, st_ctime_ns=456,
        st_mode=stat.S_IFREG | 0o600, st_file_attributes=0,
    )
    state = SimpleNamespace(
        expected=value, opened=value, after=value, stamp=133000000000000001,
        attributes=0, ok=True, opened_calls=[], closed=[], queries=[], fstats=0,
        filesystem="NTFS", volume_ok=True, volume_queries=[],
    )

    class Query:
        def __call__(self, handle, information_class, output, size):
            state.queries.append((handle, information_class, size))
            output._obj.ChangeTime = state.stamp
            output._obj.FileAttributes = state.attributes
            return state.ok

    query = Query()

    class Volume:
        def __call__(self, handle, label, label_size, serial, component, flags, output, size):
            assert label is serial is component is flags is None and label_size == 0
            state.volume_queries.append((handle, size))
            output.value = state.filesystem
            return state.volume_ok

    volume = Volume()

    def dll(name, **kwargs):
        assert name == "kernel32" and kwargs == {"use_last_error": True}
        return SimpleNamespace(GetFileInformationByHandleEx=query, GetVolumeInformationByHandleW=volume)

    def open_guard(path, **kwargs):
        state.opened_calls.append((path, kwargs))
        return 41

    def fstat(fd):
        assert fd == 41
        state.fstats += 1
        return state.opened if state.fstats == 1 else state.after

    monkeypatch.setattr(ctypes, "WinDLL", dll, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "query failed"), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd + 100))
    monkeypatch.setattr(windows_fs, "open_guard", open_guard)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "close", state.closed.append)
    return state


@pytest.mark.parametrize("filesystem", ["NTFS", "ReFS"])
def test_basic_info_layout_and_query_preserve_exact_change_time(native, filesystem):
    native.filesystem = filesystem
    assert ctypes.sizeof(windows_fs._FileBasicInfo) == 40
    assert windows_fs._FileBasicInfo.ChangeTime.offset == 24
    assert windows_fs._FileBasicInfo.FileAttributes.offset == 32
    path = Path("source.nd2")
    assert windows_fs.file_change_time(path, native.expected) == native.stamp
    assert native.opened_calls == [(path, {})]  # no delete access or rename sharing
    assert native.queries == [(141, 0, 40)]  # FileBasicInfo, not FileStandardInfo
    assert native.volume_queries == [(141, 32)]  # only the already-guarded file's filesystem
    assert native.closed == [41] and native.fstats == 2


@pytest.mark.parametrize("field,value", [
    ("st_dev", 8), ("st_ino", 14), ("st_size", 25),
    ("st_mtime_ns", 124), ("st_ctime_ns", 457),
    ("st_mode", stat.S_IFDIR | 0o700), ("st_file_attributes", 0x400),
])
def test_changed_or_reparse_file_is_rejected_before_query(native, field, value):
    native.opened = SimpleNamespace(**{**vars(native.expected), field: value})
    with pytest.raises(OSError, match="identity changed"):
        windows_fs.file_change_time(Path("source.nd2"), native.expected)
    assert native.queries == [] and native.closed == [41]


def test_file_changed_during_query_is_rejected_and_handle_closed(native):
    native.after = SimpleNamespace(**{**vars(native.expected), "st_mtime_ns": 124})
    with pytest.raises(OSError, match="while reading"):
        windows_fs.file_change_time(Path("source.nd2"), native.expected)
    assert native.queries == [(141, 0, 40)] and native.closed == [41]


@pytest.mark.parametrize("stamp,attributes", [(0, 0), (-1, 0), (123, 0x400)])
def test_unusable_stamp_or_new_reparse_point_fails_closed(native, stamp, attributes):
    native.stamp, native.attributes = stamp, attributes
    with pytest.raises(OSError, match="unavailable"):
        windows_fs.file_change_time(Path("source.nd2"), native.expected)
    assert native.closed == [41]


def test_unsupported_basic_info_query_propagates_failure_and_closes_handle(native):
    native.ok = False
    with pytest.raises(OSError, match="query failed"):
        windows_fs.file_change_time(Path("source.nd2"), native.expected)
    assert native.closed == [41]


@pytest.mark.parametrize("filesystem", ["FAT32", "exFAT", "unknown", ""])
def test_nonzero_alias_does_not_authorize_unsupported_filesystem(native, filesystem):
    native.filesystem = filesystem
    assert native.stamp > 0  # presence alone must not be treated as support
    assert windows_fs.file_change_time(Path("source.nd2"), native.expected) is None
    assert native.queries == [] and native.closed == [41]


def test_unavailable_filesystem_query_requires_content_fallback(native):
    native.volume_ok = False
    assert windows_fs.file_change_time(Path("source.nd2"), native.expected) is None
    assert native.queries == [] and native.closed == [41]
