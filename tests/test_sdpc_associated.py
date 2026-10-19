"""Bounded SDPC still-image access using only mathematical JPEG/BMP pictures."""

import os
import struct
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event

import numpy as np
import pytest
from PIL import Image, ImageFile

from nd2wsi import _sdpc_smoke_fixture as fixture
from nd2wsi import sdpc


@pytest.fixture(scope="module")
def associated_bytes():
    return fixture.add_associated_images(fixture.load_sdpc())


@pytest.fixture
def associated_path(tmp_path, associated_bytes):
    path = tmp_path / "mathematical-associated.sdpc"
    path.write_bytes(associated_bytes)
    return path


def encoded_images(data):
    """Independent test extraction of just the generated, linked payloads."""
    header = sdpc._HEADER.unpack_from(data)
    offset = struct.unpack_from("<q", data, header[19] + 6)[0]
    records = []
    for index in range(header[4]):
        size = struct.unpack_from("<Q", data, offset + 34)[0]
        records.append((f"associated-{index + 1}", offset, offset + 123, size))
        offset = struct.unpack_from("<Q", data, offset + 43)[0]
    thumbnail = sdpc._LEVEL.unpack_from(data, offset)
    records.insert(0, ("thumbnail", offset, offset + sdpc._LEVEL.size, thumbnail[6]))
    return records


def test_inventory_is_neutral_and_has_no_patient_fields(associated_path, monkeypatch):
    monkeypatch.setattr(sdpc, "_require_av", lambda: pytest.fail("still images do not require HEVC"))
    assert sdpc.associated_image_names(associated_path) == [
        "thumbnail", "associated-1", "associated-2",
    ]
    metadata = sdpc.sdpc_info(associated_path)["associated_images"]
    assert metadata == [
        {"name": "thumbnail", "width": 48, "height": 32, "encoding": "BMP"},
        {"name": "associated-1", "width": 96, "height": 48, "encoding": "JPEG"},
        {"name": "associated-2", "width": 80, "height": 120, "encoding": "JPEG"},
    ]
    assert all(set(item) == {"name", "width", "height", "encoding"} for item in metadata)


def test_native_rgb_matches_independent_still_image_decode(associated_path, associated_bytes):
    for name, _, start, size in encoded_images(associated_bytes):
        with Image.open(BytesIO(associated_bytes[start:start + size])) as image:
            expected = np.array(image.convert("RGB"))
        actual = sdpc.associated_image_rgb(associated_path, name)
        assert actual.dtype == np.uint8 and actual.flags.c_contiguous
        np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("name", ["thumbnail", "associated-1", "associated-2"])
def test_preview_is_bounded_without_upscaling_or_metadata(associated_path, name):
    for max_side in (41, 512, 2048):
        preview = sdpc.associated_image_jpeg(associated_path, name, max_side=max_side)
        with Image.open(BytesIO(preview)) as image:
            assert image.format == "JPEG" and image.mode == "RGB"
            assert max(image.size) <= max_side
            assert "exif" not in image.info and "icc_profile" not in image.info
            if max_side == 2048:
                original = sdpc.associated_image_rgb(associated_path, name)
                assert image.size == (original.shape[1], original.shape[0])


@pytest.mark.parametrize("size", [0, -1, 2049, 100_000_000, 2.5, True, "512"])
def test_unbounded_preview_sizes_are_rejected_before_open(associated_path, monkeypatch, size):
    monkeypatch.setattr(sdpc, "_Slide", lambda *args: pytest.fail("must reject size before opening"))
    with pytest.raises(ValueError, match="preview size"):
        sdpc.associated_image_jpeg(associated_path, "associated-1", max_side=size)


@pytest.mark.parametrize("name", ["label", "macro", "associated-0", "associated-17", "../patient", ""])
def test_only_explicit_record_names_are_read(associated_path, monkeypatch, name):
    monkeypatch.setattr(sdpc, "_decode_associated", lambda *args: pytest.fail("unknown record decoded"))
    with pytest.raises(KeyError):
        sdpc.associated_image_rgb(associated_path, name)


@pytest.mark.parametrize("corruption", [
    "zero_size", "excessive_size", "bad_link", "zero_width", "excessive_side",
    "excessive_pixels", "too_many_records", "truncated_file",
])
def test_bad_record_bounds_fail_before_image_decode(associated_path, associated_bytes, monkeypatch,
                                                   corruption):
    data = bytearray(associated_bytes)
    _, record, _, _ = encoded_images(data)[1]
    if corruption == "zero_size":
        struct.pack_into("<Q", data, record + 34, 0)
    elif corruption == "excessive_size":
        struct.pack_into("<Q", data, record + 34, sdpc._MAX_ASSOCIATED_BYTES + 1)
    elif corruption == "bad_link":
        struct.pack_into("<Q", data, record + 43, record)
    elif corruption == "zero_width":
        struct.pack_into("<I", data, record + 10, 0)
    elif corruption == "excessive_side":
        struct.pack_into("<I", data, record + 10, sdpc._MAX_ASSOCIATED_SIDE + 1)
    elif corruption == "excessive_pixels":
        struct.pack_into("<2I", data, record + 10, 4096, 4096)
    elif corruption == "too_many_records":
        header = list(sdpc._HEADER.unpack_from(data))
        header[4] = 17
        sdpc._HEADER.pack_into(data, 0, *header)
    elif corruption == "truncated_file":
        del data[-1:]
    associated_path.write_bytes(data)
    monkeypatch.setattr(sdpc, "_decode_associated", lambda *args: pytest.fail("bad record decoded"))
    with pytest.raises(ValueError):
        sdpc.associated_image_names(associated_path)


@pytest.mark.parametrize("corruption", ["invalid_jpeg", "truncated_jpeg", "dimensions", "invalid_bmp"])
def test_malformed_payloads_fail_without_fabricated_images(associated_path, associated_bytes, corruption):
    data = bytearray(associated_bytes)
    _, record, start, size = encoded_images(data)[1]
    name = "associated-1"
    if corruption == "invalid_jpeg":
        data[start:start + size] = b"\x00" * size
    elif corruption == "truncated_jpeg":
        # Keep the linked record's byte count intact but remove its JPEG ending.
        data[start + size - 24:start + size] = b"\x00" * 24
    elif corruption == "dimensions":
        struct.pack_into("<I", data, record + 10, 95)
    elif corruption == "invalid_bmp":
        name, _, start, size = encoded_images(data)[0]
        data[start + 2:start + size] = b"\x00" * (size - 2)
    associated_path.write_bytes(data)
    with pytest.raises(ValueError, match="cannot decode SDPC associated image"):
        sdpc.associated_image_rgb(associated_path, name)


def test_jpeg_claimed_dimensions_are_checked_before_allocation(associated_path, associated_bytes, monkeypatch):
    data = bytearray(associated_bytes)
    _, _, start, size = encoded_images(data)[1]
    frame_header = data.find(b"\xff\xc0", start, start + size)
    assert frame_header >= 0
    struct.pack_into(">H", data, frame_header + 7, sdpc._MAX_ASSOCIATED_SIDE + 1)
    associated_path.write_bytes(data)
    monkeypatch.setattr(ImageFile.ImageFile, "load", lambda *args: pytest.fail("oversized JPEG decoded"))
    with pytest.raises(ValueError, match="declared dimensions"):
        sdpc.associated_image_rgb(associated_path, "associated-1")


def test_source_changes_and_closed_handles_are_rejected(associated_path):
    source = sdpc._Slide(associated_path)
    try:
        original = source.read_associated_image("associated-1")
        assert original.shape == (48, 96, 3)
        before = associated_path.stat()
        os.utime(associated_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        with pytest.raises(ValueError, match="source changed"):
            source.read_associated_image("associated-1")
        with pytest.raises(ValueError, match="source changed"):
            source.associated_image_metadata()
    finally:
        source.close()
    source.close()
    with pytest.raises(ValueError, match="closed"):
        source.read_associated_image("associated-1")
    with pytest.raises(ValueError, match="closed"):
        source.associated_image_metadata()
    with pytest.raises(OSError):
        os.fstat(source.fd)


def test_close_waits_for_associated_read(associated_path, monkeypatch):
    source = sdpc._Slide(associated_path)
    started, release, closing_started = Event(), Event(), Event()
    decode = sdpc._decode_associated

    def blocked_decode(*args):
        started.set()
        assert release.wait(5), "test did not release the still-image decoder"
        return decode(*args)

    def close():
        closing_started.set()
        source.close()

    monkeypatch.setattr(sdpc, "_decode_associated", blocked_decode)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading = pool.submit(source.read_associated_image, "associated-1")
            try:
                assert started.wait(5)
                closing = pool.submit(close)
                assert closing_started.wait(5)
                assert not closing.done()
                os.fstat(source.fd)
            finally:
                release.set()
            assert reading.result(timeout=5).shape == (48, 96, 3)
            closing.result(timeout=5)
        with pytest.raises(ValueError, match="closed"):
            source.read_associated_image("associated-1")
        with pytest.raises(OSError):
            os.fstat(source.fd)
    finally:
        release.set()
        source.close()


def test_synthetic_attachment_keeps_original_hevc_payloads(associated_bytes):
    original = fixture.load_sdpc()
    old_header = sdpc._HEADER.unpack_from(original)
    new_header = sdpc._HEADER.unpack_from(associated_bytes)
    old_offset, new_offset = old_header[20], new_header[20]
    for _ in range(old_header[6]):
        old_level = sdpc._LEVEL.unpack_from(original, old_offset)
        new_level = sdpc._LEVEL.unpack_from(associated_bytes, new_offset)
        assert old_level[:7] == new_level[:7] and old_level[8:] == new_level[8:]
        assert original[old_offset + sdpc._LEVEL.size:old_level[7]] == (
            associated_bytes[new_offset + sdpc._LEVEL.size:new_level[7]])
        old_offset, new_offset = old_level[7], new_level[7]


def test_absent_associated_chain_has_empty_metadata(tmp_path):
    path = tmp_path / "no-associated.sdpc"
    path.write_bytes(fixture.load_sdpc())
    assert sdpc.associated_image_names(path) == []
    assert sdpc.sdpc_info(path)["associated_images"] == []
    with pytest.raises(KeyError):
        sdpc.associated_image_rgb(path, "thumbnail")
