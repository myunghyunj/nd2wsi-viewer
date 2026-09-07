"""Windows handle-based cache deletion without traversing reparse points.

Directory enumeration and child opens are relative to existing handles, so
even an in-place junction change cannot redirect traversal. Final removal
also acts on the opened handle, never on a replaceable pathname.
"""

from __future__ import annotations

import ctypes
import os
import stat
from functools import lru_cache
from pathlib import Path

from .platform_io import filesystem_path


def open_guard(path: Path, *, allow_rename: bool = False, delete: bool = False) -> int:
    """Open the entry itself and return an owned CRT descriptor."""
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    # Attribute-only handles are exempt from Windows sharing checks. Request
    # READ_DATA/LIST_DIRECTORY too so a guard really prevents substitution.
    access = 0x81 | (0x10000 if delete else 0)  # READ_DATA, READ_ATTRIBUTES, DELETE
    share = 0x1 | 0x2 | (0x4 if allow_rename else 0)
    handle = create(filesystem_path(path), access, share, None, 3, 0x02200000, None)
    # BACKUP_SEMANTICS allows directories; OPEN_REPARSE_POINT avoids following links.
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise


def _is_reparse(entry: os.stat_result) -> bool:
    return bool(getattr(entry, "st_file_attributes", 0) & 0x400)


def allocated_bytes(path: Path) -> int:
    """Ask the filesystem for allocation, including sparse/compressed files."""
    import msvcrt
    from ctypes import wintypes

    class StandardInfo(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_int64), ("EndOfFile", ctypes.c_int64),
            ("NumberOfLinks", wintypes.DWORD), ("DeletePending", ctypes.c_ubyte),
            ("Directory", ctypes.c_ubyte),
        ]

    query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    query.restype = wintypes.BOOL
    fd = open_guard(path, allow_rename=True)
    try:
        info = StandardInfo()
        if not query(msvcrt.get_osfhandle(fd), 1, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.AllocationSize)
    finally:
        os.close(fd)


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


class _DirectoryInfoHeader(ctypes.Structure):
    """Fixed header of FILE_ID_BOTH_DIR_INFO; names follow as UTF-16 bytes."""

    _fields_ = [
        ("next_offset", ctypes.c_uint32),
        ("file_index", ctypes.c_uint32),
        ("creation_time", ctypes.c_int64),
        ("access_time", ctypes.c_int64),
        ("write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("end_of_file", ctypes.c_int64),
        ("allocation_size", ctypes.c_int64),
        ("attributes", ctypes.c_uint32),
        ("name_length", ctypes.c_uint32),
        ("ea_size", ctypes.c_uint32),
        ("short_name_length", ctypes.c_int8),
        ("short_name", ctypes.c_uint16 * 12),
        ("file_id", ctypes.c_int64),
    ]


def _names_from_directory_buffer(data: bytes):
    """Parse a bounded FILE_ID_BOTH_DIR_INFO batch without trusting offsets."""
    offset = 0
    header_size = ctypes.sizeof(_DirectoryInfoHeader)
    while True:
        if offset + header_size > len(data):
            raise OSError("truncated Windows directory information")
        entry = _DirectoryInfoHeader.from_buffer_copy(data, offset)
        start = offset + header_size
        end = start + entry.name_length
        following = offset + entry.next_offset if entry.next_offset else len(data)
        if (
            entry.name_length % 2
            or end > len(data)
            or end > following
            or following > len(data)
            or (entry.next_offset and entry.next_offset < header_size)
        ):
            raise OSError("invalid Windows directory information offsets")
        name = data[start:end].decode("utf-16-le", errors="surrogatepass")
        if name not in (".", ".."):
            if not name or any(char in name for char in ("\\", "/", ":", "\0")):
                raise OSError("invalid Windows directory entry name")
            yield name
        if not entry.next_offset:
            return
        offset = following


def _directory_names(fd: int):
    """Enumerate the opened directory itself, never its possibly changed path."""
    import msvcrt
    from ctypes import wintypes

    query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    query.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(64 * 1024)
    restart = True
    while True:
        # FileIdBothDirectoryRestartInfo=11; subsequent pages use Info=10.
        if not query(msvcrt.get_osfhandle(fd), 11 if restart else 10, buffer, len(buffer)):
            error = ctypes.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES
                return
            raise ctypes.WinError(error)
        restart = False
        yield from _names_from_directory_buffer(buffer.raw)


@lru_cache(maxsize=1)
def _native_open_api():
    """Cache the native ABI types and functions instead of rebuilding per chunk."""
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint16),
            ("maximum_length", ctypes.c_uint16),
            ("buffer", ctypes.c_void_p),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(UnicodeString)),
            ("attributes", ctypes.c_uint32),
            ("security_descriptor", ctypes.c_void_p),
            ("security_quality_of_service", ctypes.c_void_p),
        ]

    class IoStatusBlock(ctypes.Structure):
        # The first field is a union of NTSTATUS and PVOID, so pointer-sized.
        _fields_ = [("status_or_pointer", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    native = ctypes.WinDLL("ntdll")
    create = native.NtCreateFile
    create.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    create.restype = ctypes.c_int32
    to_winerror = native.RtlNtStatusToDosError
    to_winerror.argtypes = [ctypes.c_int32]
    to_winerror.restype = wintypes.DWORD
    return create, to_winerror, UnicodeString, ObjectAttributes, IoStatusBlock


def _open_child(parent_fd: int, name: str, *, delete: bool) -> int:
    """Open one component against the held parent, bypassing reparse points."""
    import msvcrt
    from ctypes import wintypes

    if name in ("", ".", "..") or any(char in name for char in ("\\", "/", ":", "\0")):
        raise OSError("child must be one ordinary Windows filename")
    create, to_winerror, UnicodeString, ObjectAttributes, IoStatusBlock = _native_open_api()
    encoded = name.encode("utf-16-le", errors="surrogatepass")
    if len(encoded) > 65532:
        raise OSError("Windows filename exceeds UNICODE_STRING capacity")
    text = ctypes.create_string_buffer(encoded + b"\0\0")
    unicode_name = UnicodeString(len(encoded), len(encoded) + 2, ctypes.addressof(text))
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes), msvcrt.get_osfhandle(parent_fd),
        ctypes.pointer(unicode_name), 0, None, None,
    )
    status_block = IoStatusBlock()
    handle = wintypes.HANDLE()
    status = create(
        ctypes.byref(handle),
        0x100081 | (0x10000 if delete else 0),  # SYNCHRONIZE, LIST/READ, ATTR, DELETE
        ctypes.byref(attributes), ctypes.byref(status_block),
        None, 0, 0x3, 1,  # share read/write; FILE_OPEN (never create or replace)
        0x00204020,  # OPEN_REPARSE_POINT, BACKUP_INTENT, SYNCHRONOUS_IO_NONALERT
        None, 0,
    )
    if status < 0:
        raise ctypes.WinError(to_winerror(status))
    try:
        return msvcrt.open_osfhandle(handle.value, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        close(handle)
        raise


def _delete_handle(fd: int) -> None:
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    dispose = kernel.SetFileInformationByHandle
    dispose.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    dispose.restype = wintypes.BOOL
    delete = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN
    if not dispose(msvcrt.get_osfhandle(fd), 4, ctypes.byref(delete), ctypes.sizeof(delete)):
        raise ctypes.WinError(ctypes.get_last_error())


def delete_verified_tree(path: Path, expected: os.stat_result, on_progress=None) -> int:
    """Walk and remove only descendants reached through the captured root handle."""
    total = 1
    completed = 0

    def walk(fd: int, *, deleting: bool) -> tuple[int, int]:
        nonlocal completed
        opened = os.fstat(fd)
        freed = 0
        files = 0
        directory = stat.S_ISDIR(opened.st_mode) and not _is_reparse(opened)
        if directory:
            # Finish enumeration before deleting entries so the filesystem's
            # continuation cursor cannot skip names as its index changes.
            for name in list(_directory_names(fd)):
                try:
                    child_fd = _open_child(fd, name, delete=deleting)
                except FileNotFoundError:
                    continue
                try:
                    child_bytes, child_files = walk(child_fd, deleting=deleting)
                    freed += child_bytes
                    files += child_files
                finally:
                    os.close(child_fd)
        else:
            freed = int(opened.st_size)
            files = 1
        if deleting:
            _delete_handle(fd)
            if not directory:
                completed += 1
                if on_progress:
                    on_progress(min(completed / max(1, total), 0.99))
        return freed, files

    fd = None
    try:
        # Retain this one root handle through both passes. Count callbacks and
        # in-place reparse changes cannot redirect subsequent opens elsewhere.
        fd = open_guard(path, delete=True)
        opened = os.fstat(fd)
        if not _same(opened, expected):
            raise OSError(f"cache root changed during deletion: {path}")
        if _is_reparse(opened) or not stat.S_ISDIR(opened.st_mode):
            raise OSError(f"cache root is not a real directory: {path}")
        if on_progress:
            _, total = walk(fd, deleting=False)
        freed, _ = walk(fd, deleting=True)
    except Exception as exc:
        raise OSError(
            f"cache deletion incomplete; remaining data was kept at {path}: {exc}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    if on_progress:
        on_progress(1.0)
    return freed
