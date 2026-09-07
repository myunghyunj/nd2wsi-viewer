"""Exercise the real Windows handle APIs on Windows CI, including ARM."""
import os
import subprocess

import pytest

from nd2wsi.server import SlideRegistry, ViewerState

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows kernel APIs")


def test_windows_cache_delete_rescues_annotations_and_preserves_source(tmp_path):
    source = tmp_path / "원본 slide.nd2"
    source.write_bytes(b"source must never change")
    cache = tmp_path / "pyramid.ome.zarr"
    (cache / "0").mkdir(parents=True)
    (cache / "0" / "chunk").write_bytes(b"pixels")
    annotations = b'{"items": [{"text": "keep me"}]}'
    (cache / "annotations_legacy.json").write_bytes(annotations)
    registry = SlideRegistry()
    registry.slides["one"] = ViewerState({"0": object()}, {}, trash_path=cache)
    assert registry.trash_cache("one") >= len(b"pixels")
    assert not cache.exists()
    assert source.read_bytes() == b"source must never change"
    assert (tmp_path / "nd2wsi" / "annotations" / "annotations_legacy.json").read_bytes() == annotations


def test_windows_cache_delete_never_follows_junction(tmp_path):
    cache = tmp_path / "pyramid.ome.zarr"
    cache.mkdir()
    outside = tmp_path / "unrelated"
    outside.mkdir()
    valuable = outside / "keep.txt"
    valuable.write_text("user work", encoding="utf-8")
    junction = cache / "external"
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True, capture_output=True,
    )
    registry = SlideRegistry()
    registry.slides["one"] = ViewerState({"0": object()}, {}, trash_path=cache)
    registry.trash_cache("one")
    assert not cache.exists()
    assert valuable.read_text(encoding="utf-8") == "user work"


def test_windows_guard_prevents_directory_substitution(tmp_path):
    from nd2wsi.windows_fs import open_guard

    directory = tmp_path / "pinned"
    directory.mkdir()
    fd = open_guard(directory)
    try:
        assert os.fstat(fd).st_ino == directory.stat().st_ino
        with pytest.raises(OSError):
            directory.rename(tmp_path / "moved")
        assert directory.is_dir()
    finally:
        os.close(fd)
    directory.rename(tmp_path / "moved")


def test_windows_cache_delete_reads_every_directory_batch(tmp_path):
    from nd2wsi.windows_fs import delete_verified_tree

    cache = tmp_path / "many-chunks.ome.zarr"
    cache.mkdir()
    for index in range(2000):
        (cache / f"chunk-{index:04d}").write_bytes(b"tile")
    seen = []
    freed = delete_verified_tree(cache, cache.stat(), seen.append)
    assert freed == 2000 * len(b"tile")
    assert not cache.exists()
    assert len(seen) > 10 and seen == sorted(seen)
    assert seen[-1] == 1.0


def _set_junction(path, target, access):
    """Change an existing empty directory in place, without renaming it."""
    import ctypes
    import struct
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    control = kernel.DeviceIoControl
    control.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    control.restype = wintypes.BOOL
    handle = create(str(path), access, 7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        substitute = ("\\??\\" + str(target)).encode("utf-16-le")
        display = str(target).encode("utf-16-le")
        names = substitute + b"\0\0" + display + b"\0\0"
        data = struct.pack(
            "<IHHHHHH", 0xA0000003, 8 + len(names), 0,
            0, len(substitute), len(substitute) + 2, len(display),
        ) + names
        buffer = ctypes.create_string_buffer(data)
        returned = wintypes.DWORD()
        if not control(handle, 0x900A4, buffer, len(data), None, 0,
                       ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


@pytest.mark.parametrize("nested", [False, True])
def test_windows_delete_cannot_follow_a_junction_created_on_an_open_directory(
    tmp_path, monkeypatch, nested,
):
    from nd2wsi import windows_fs

    outside = tmp_path / "unrelated-work"
    outside.mkdir()
    valuable = outside / "keep.txt"
    valuable.write_bytes(b"must remain intact")
    # Discover the access the filesystem requires without a guard present.
    # Windows permits some reparse changes with WRITE_ATTRIBUTES alone, which
    # is exempt from CreateFile sharing restrictions.
    probe = tmp_path / "junction-capability"
    probe.mkdir()
    access = 0x100  # FILE_WRITE_ATTRIBUTES
    try:
        _set_junction(probe, outside, access)
    except OSError as exc:
        if exc.winerror != 5:
            raise
        access = 0x40000000  # GENERIC_WRITE on filesystems requiring data access
        _set_junction(probe, outside, access)
    assert (probe / valuable.name).read_bytes() == b"must remain intact"
    probe.rmdir()  # remove this junction itself

    cache = tmp_path / "cache.ome.zarr"
    cache.mkdir()
    target = cache / "empty-level" if nested else cache
    target.mkdir(exist_ok=True)
    captured = target.stat()
    expected_root = cache.stat()
    enumerate_names = windows_fs._directory_names
    attempted = False

    def change_before_enumeration(fd):
        nonlocal attempted
        opened = os.fstat(fd)
        if not attempted and (opened.st_dev, opened.st_ino) == (captured.st_dev, captured.st_ino):
            attempted = True
            try:
                _set_junction(target, outside, access)
            except OSError as exc:
                if exc.winerror not in (5, 32):
                    raise
        return enumerate_names(fd)

    monkeypatch.setattr(windows_fs, "_directory_names", change_before_enumeration)
    try:
        windows_fs.delete_verified_tree(cache, expected_root)
    except OSError:
        pass  # refusing the changed directory is also safe
    assert attempted
    assert valuable.read_bytes() == b"must remain intact"
    assert outside.is_dir()
