"""The Windows I/O contract, including fallbacks exercised on every OS."""

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from nd2wsi.platform_io import BINARY, process_is_alive, read_at, set_fd_times, write_at


@pytest.mark.parametrize(("source", "expected"), [
    ("C:/한글/cache/../store", "\\\\?\\C:\\한글\\store"),
    ("\\\\server\\share\\cache", "\\\\?\\UNC\\server\\share\\cache"),
    ("\\\\?\\C:\\store", "\\\\?\\C:\\store"),
    ("\\\\?\\UNC\\server\\share\\store", "\\\\?\\UNC\\server\\share\\store"),
])
def test_windows_io_paths_normalize_before_extended_prefix(source, expected):
    from nd2wsi.platform_io import filesystem_path

    assert filesystem_path(source, platform="nt") == expected


def test_posix_io_paths_keep_literal_backslashes_and_relative_identity():
    from nd2wsi.platform_io import filesystem_path

    source = "../data/a\\b.nd2"
    assert filesystem_path(source, platform="posix") == source


@pytest.mark.parametrize("fallback", [False, True])
def test_parallel_positional_reads_preserve_bytes_and_position(tmp_path, monkeypatch, fallback):
    if fallback:
        monkeypatch.delattr(os, "pread", raising=False)
    data = bytes(range(256)) * 128
    path = tmp_path / "한글 slide.bin"
    path.write_bytes(data)
    fd = os.open(path, os.O_RDONLY | BINARY)
    try:
        os.lseek(fd, 17, os.SEEK_SET)
        barrier = threading.Barrier(8)

        def reader(worker):
            barrier.wait(timeout=10)
            for i in range(80):
                start = (worker * 701 + i * 157) % len(data)
                assert read_at(fd, 803, start) == data[start : start + 803]

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(reader, range(8)))
        assert os.lseek(fd, 0, os.SEEK_CUR) == 17
        assert read_at(fd, 20, len(data) + 10) == b""
    finally:
        os.close(fd)


def test_parallel_fallback_writes_do_not_mix_offsets(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "pwrite", raising=False)
    fd = os.open(tmp_path / "written.bin", os.O_CREAT | os.O_RDWR | BINARY, 0o600)
    try:
        os.lseek(fd, 7, os.SEEK_SET)

        def writer(worker):
            payload = bytes([worker, 13, 10, 26]) * 512
            for _ in range(12):
                assert write_at(fd, payload, worker * len(payload)) == len(payload)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(writer, range(8)))
        assert os.lseek(fd, 0, os.SEEK_CUR) == 7
        expected = b"".join(bytes([i, 13, 10, 26]) * 512 for i in range(8))
        assert read_at(fd, len(expected), 0) == expected
    finally:
        os.close(fd)


def test_fallback_restores_position_when_read_fails(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "pread", raising=False)
    path = tmp_path / "data.bin"
    path.write_bytes(b"0123456789")
    fd = os.open(path, os.O_RDONLY | BINARY)
    try:
        os.lseek(fd, 3, os.SEEK_SET)

        def fail_read(*_):
            raise OSError("injected read error")

        monkeypatch.setattr(os, "read", fail_read)
        with pytest.raises(OSError, match="injected read error"):
            read_at(fd, 2, 7)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 3
    finally:
        os.close(fd)


def test_process_probe_never_terminates_a_live_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        for _ in range(4):
            assert process_is_alive(child.pid)
        time.sleep(0.05)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert not process_is_alive(child.pid)
    assert not process_is_alive(0)
    assert not process_is_alive(-1)
    assert not process_is_alive(2**80)


def test_descriptor_times_can_invalidate_a_failed_cache_claim(tmp_path):
    path = tmp_path / "cache.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | BINARY, 0o600)
    try:
        os.write(fd, b"invalid")
        set_fd_times(fd, 0, 0)
        assert os.fstat(fd).st_mtime == pytest.approx(0, abs=2)
    finally:
        os.close(fd)


def test_windows_allocation_probe_uses_the_mounted_volume_geometry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nd2wsi import platform_io

    def get_volume(path, root, length):
        assert path == str(tmp_path.resolve())
        assert length >= 260
        root.value = "X:\\mounted\\"
        return True

    def disk_free(root, sectors, sector_bytes, free, total):
        assert root == "X:\\mounted\\"
        sectors._obj.value = 2048
        sector_bytes._obj.value = 512
        return True

    monkeypatch.setattr(platform_io, "_windows_api", lambda: SimpleNamespace(
        GetVolumePathNameW=get_volume, GetDiskFreeSpaceW=disk_free,
    ))
    assert platform_io.windows_allocation_block_bytes(tmp_path) == 1024 * 1024


def test_windows_memory_probe_uses_available_physical_bytes(monkeypatch):
    from types import SimpleNamespace

    from nd2wsi import platform_io

    def memory_status(pointer):
        status = pointer._obj
        assert status.length == 64  # MEMORYSTATUSEX ABI on both ARM64 and x64
        status.total_phys = 32 * 1024**3
        status.avail_phys = 3 * 1024**3
        status.avail_pagefile = 12 * 1024**3
        return True

    monkeypatch.setattr(platform_io, "_windows_api", lambda: SimpleNamespace(
        GlobalMemoryStatusEx=memory_status,
    ))
    assert platform_io.windows_available_memory_bytes() == 3 * 1024**3


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel APIs")
def test_windows_memory_and_allocation_probes_work_on_this_machine(tmp_path):
    from nd2wsi.platform_io import (
        windows_allocation_block_bytes,
        windows_available_memory_bytes,
    )

    assert windows_available_memory_bytes() > 0
    block = windows_allocation_block_bytes(tmp_path)
    assert block >= 512 and block & (block - 1) == 0
