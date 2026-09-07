"""Small OS-specific primitives used by the cache and slide readers.

On Windows positional I/O uses an independent binary file descriptor and a
short seek/read (or seek/write) critical section. Callers must not share its
file position with a buffered reader or a duplicated descriptor. Decoding is
outside this section, and POSIX keeps its native concurrent pread/pwrite.
"""

from __future__ import annotations

import os
import threading
from functools import lru_cache

# A fixed number of locks avoids retaining a lock for every recycled fd.
_POSITION_LOCKS = tuple(threading.RLock() for _ in range(64))
BINARY = getattr(os, "O_BINARY", 0)


def filesystem_path(path: str | os.PathLike, *, platform: str | None = None) -> str:
    """An OS path for internal I/O; public paths and cache identities stay plain.

    Zarr appends a UUID and ``.partial`` while committing metadata. That can
    exceed MAX_PATH even when the cache root itself fits. Extended absolute
    paths let Windows perform these writes without an administrator setting.
    """
    value = os.fspath(path)
    if (platform or os.name) != "nt":
        return value
    import ntpath

    # Normalize separators and dot segments *before* adding the prefix;
    # extended Windows paths deliberately bypass that Win32 normalization.
    absolute = ntpath.abspath(value)
    if absolute.startswith(("\\\\?\\", "\\\\.\\")):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def read_at(fd: int, size: int, offset: int) -> bytes:
    """Read at an absolute offset without changing the descriptor's position."""
    native = getattr(os, "pread", None)
    if native is not None:
        return native(fd, size, offset)
    if size < 0 or offset < 0:
        raise ValueError("size and offset must be nonnegative")
    with _POSITION_LOCKS[fd % len(_POSITION_LOCKS)]:
        position = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            return os.read(fd, size)
        finally:
            os.lseek(fd, position, os.SEEK_SET)


def write_at(fd: int, data: bytes | memoryview, offset: int) -> int:
    """Write at an absolute offset without changing the descriptor's position."""
    native = getattr(os, "pwrite", None)
    if native is not None:
        return native(fd, data, offset)
    if offset < 0:
        raise ValueError("offset must be nonnegative")
    with _POSITION_LOCKS[fd % len(_POSITION_LOCKS)]:
        position = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            return os.write(fd, data)
        finally:
            os.lseek(fd, position, os.SEEK_SET)


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel.SetFileTime.restype = wintypes.BOOL
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumePathNameW.restype = wintypes.BOOL
    kernel.GetDiskFreeSpaceW.argtypes = [wintypes.LPCWSTR] + [ctypes.POINTER(wintypes.DWORD)] * 4
    kernel.GetDiskFreeSpaceW.restype = wintypes.BOOL
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    return kernel


def windows_allocation_block_bytes(path: str | os.PathLike) -> int:
    """Return the volume's cluster size, including mounted drives and shares."""
    import ctypes
    from ctypes import wintypes

    kernel = _windows_api()
    root = ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(os.path.abspath(path), root, len(root)):
        raise ctypes.WinError(ctypes.get_last_error())
    sectors, sector_bytes, free, total = (wintypes.DWORD() for _ in range(4))
    if not kernel.GetDiskFreeSpaceW(
        root.value,
        ctypes.byref(sectors),
        ctypes.byref(sector_bytes),
        ctypes.byref(free),
        ctypes.byref(total),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    block = int(sectors.value) * int(sector_bytes.value)
    if block < 512:
        raise OSError("Windows returned an invalid allocation block size")
    return block


def windows_available_memory_bytes() -> int:
    """Available physical memory, including reclaimable Windows standby pages."""
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32),
            ("memory_load", ctypes.c_uint32),
            ("total_phys", ctypes.c_uint64),
            ("avail_phys", ctypes.c_uint64),
            ("total_pagefile", ctypes.c_uint64),
            ("avail_pagefile", ctypes.c_uint64),
            ("total_virtual", ctypes.c_uint64),
            ("avail_virtual", ctypes.c_uint64),
            ("avail_extended_virtual", ctypes.c_uint64),
        ]

    status = MemoryStatusEx()
    status.length = ctypes.sizeof(status)
    if not _windows_api().GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(status.avail_phys)


def process_is_alive(pid: int) -> bool:
    """Conservatively check liveness without sending a signal on Windows.

    ``os.kill(pid, 0)`` is a POSIX probe but terminates a Windows process.
    A zero-time wait on a SYNCHRONIZE-only handle has no effect on the target.
    Access failures remain live so a cache writer is never stolen on doubt.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OverflowError):
            return False
        except PermissionError:
            return True
        return True

    import ctypes

    if pid > 0xFFFFFFFF:
        return False
    kernel = _windows_api()
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        # ERROR_INVALID_PARAMETER means the PID does not identify a process.
        return ctypes.get_last_error() != 87
    try:
        # WAIT_OBJECT_0 is only returned after the process has exited.
        return kernel.WaitForSingleObject(handle, 0) != 0
    finally:
        kernel.CloseHandle(handle)


def set_fd_times(fd: int, atime: float, mtime: float) -> None:
    """Update the opened inode's times, never following its pathname."""
    if os.name != "nt":
        os.utime(fd, (atime, mtime))
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    def filetime(timestamp: float):
        ticks = int((timestamp + 11644473600) * 10_000_000)
        return wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)

    access, modified = filetime(atime), filetime(mtime)
    if not _windows_api().SetFileTime(
        msvcrt.get_osfhandle(fd), None, ctypes.byref(access), ctypes.byref(modified)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
