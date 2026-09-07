"""Validate native directory-record parsing on every build platform."""

import ctypes

import pytest

from nd2wsi.windows_fs import _DirectoryInfoHeader, _names_from_directory_buffer


def record(name, *, more=False):
    encoded = name.encode("utf-16-le", errors="surrogatepass")
    header = _DirectoryInfoHeader()
    header.name_length = len(encoded)
    size = ctypes.sizeof(header) + len(encoded)
    aligned = (size + 7) & ~7
    header.next_offset = aligned if more else 0
    return bytes(header) + encoded + bytes(aligned - size)


def test_directory_record_abi_and_unicode_names():
    assert ctypes.sizeof(_DirectoryInfoHeader) == 104
    assert _DirectoryInfoHeader.file_id.offset == 96
    data = record(".", more=True) + record("..", more=True) + record("한글 🧬.zarr", more=True) + record("0.0.0")
    assert list(_names_from_directory_buffer(data)) == ["한글 🧬.zarr", "0.0.0"]


@pytest.mark.parametrize("name", ["", "../outside", "a\\b", "C:drive", "bad\0name"])
def test_directory_records_reject_components_that_can_escape_parent(name):
    with pytest.raises(OSError):
        list(_names_from_directory_buffer(record(name)))


def test_directory_records_reject_truncation_and_overlapping_offsets():
    with pytest.raises(OSError):
        list(_names_from_directory_buffer(b"\0" * 90))
    header = _DirectoryInfoHeader()
    header.name_length = 20
    header.next_offset = 104
    with pytest.raises(OSError):
        list(_names_from_directory_buffer(bytes(header) + b"x" * 20))
