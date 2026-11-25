"""Read chosen AstroMapper JPG/PNG and TXT pairs for the client-side importer.

astromap-import-v1.js owns inclusive pixel endpoints and exact source mapping.
"""
from __future__ import annotations

import os
import stat
import struct
from pathlib import Path

from .platform_io import BINARY, filesystem_path

MAX_BYTES = 16 * 1024 * 1024
# Header-only inspection never decodes pixels; allow full-slide reference images
# up to 2 billion pixels (e.g. ~45k x 45k) while refusing absurd headers.
_MAX_REFERENCE_HEADER_PIXELS = 2_000_000_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = frozenset((0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF))
_MAX_JPEG_SEGMENTS = 4096
_MAX_JPEG_PADDING = 4096
ROI_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def roi_pair_paths(paths, *, require_pair: bool = True) -> list[Path]:
    """Resolve one explicitly selected pair; never guess among siblings."""
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 2:
        raise ValueError("Choose one matching JPG/PNG and TXT pair.")
    resolved = [Path(path) for path in paths]
    allowed = (".txt", *ROI_IMAGE_SUFFIXES)
    if any(path.suffix.casefold() not in allowed for path in resolved):
        raise ValueError("Choose a coordinate-reference JPG/PNG and AstroMapper TXT.")
    if len(resolved) == 1:
        chosen = resolved[0]
        suffixes = ROI_IMAGE_SUFFIXES if chosen.suffix.casefold() == ".txt" else (".txt",)
        companions: list[Path] = []
        with os.scandir(filesystem_path(chosen.parent)) as entries:
            for entry in entries:
                candidate = chosen.parent / entry.name
                if (
                    candidate.stem.casefold() == chosen.stem.casefold()
                    and candidate.suffix.casefold() in suffixes
                    and Path(filesystem_path(candidate)).is_file()
                ):
                    companions.append(candidate)
        if len(companions) == 1:
            resolved.append(companions[0])
        elif require_pair:
            message = (
                "More than one matching reference file was found. Drop the intended image and TXT together."
                if companions
                else "The matching JPG/PNG or TXT is missing. Drop both files together."
            )
            raise ValueError(message)
        else:
            return resolved
    text = [path for path in resolved if path.suffix.casefold() == ".txt"]
    images = [path for path in resolved if path.suffix.casefold() in ROI_IMAGE_SUFFIXES]
    if len(text) != 1 or len(images) != 1:
        raise ValueError("Choose one JPG/PNG and one TXT file.")
    if text[0].stem.casefold() != images[0].stem.casefold():
        raise ValueError("The reference image and TXT must have the same filename before the extension.")
    return [text[0], images[0]]


def _read_exact(handle, offset: int, length: int, file_size: int) -> bytes:
    if offset < 0 or length < 0 or offset + length > file_size:
        raise ValueError("The coordinate reference must be a JPG or PNG")
    if length == 0:
        return b""
    chunks = bytearray()
    while len(chunks) < length:
        chunk = handle.read(length - len(chunks))
        if not chunk:
            raise ValueError("The coordinate reference must be a JPG or PNG")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_image_size_stream(handle, file_size: int) -> dict[str, int]:
    """Read stored PNG/JPEG dimensions from headers only via bounded seeks."""
    if not isinstance(file_size, int) or isinstance(file_size, bool) or file_size < 4:
        raise ValueError("The coordinate reference must be a JPG or PNG")
    handle.seek(0)
    prefix = _read_exact(handle, 0, min(file_size, 24), file_size)
    if prefix.startswith(_PNG_SIGNATURE):
        if file_size < 33 or len(prefix) < 24 or prefix[8:16] != b"\x00\x00\x00\x0dIHDR":
            raise ValueError("The coordinate reference must be a JPG or PNG")
        width, height = struct.unpack(">II", prefix[16:24])
        if width <= 0 or height <= 0 or width > 2_147_483_647 or height > 2_147_483_647:
            raise ValueError("The coordinate reference has invalid dimensions")
        if width * height > _MAX_REFERENCE_HEADER_PIXELS:
            raise ValueError("The coordinate reference image dimensions exceed safety limits")
        return {"width": width, "height": height}
    if len(prefix) < 2 or prefix[0] != 0xFF or prefix[1] != 0xD8:
        raise ValueError("The coordinate reference must be a JPG or PNG")
    offset = 2
    padding = 0
    handle.seek(offset)
    for _ in range(_MAX_JPEG_SEGMENTS):
        marker = _read_exact(handle, offset, 2, file_size)
        if marker[0] != 0xFF:
            raise ValueError("The coordinate reference must be a JPG or PNG")
        offset += 2
        kind = marker[1]
        while kind == 0xFF:
            padding += 1
            if padding > _MAX_JPEG_PADDING:
                raise ValueError("The coordinate reference must be a JPG or PNG")
            kind = _read_exact(handle, offset, 1, file_size)[0]
            offset += 1
        if kind in (0xDA, 0xD9, 0x00, 0xD8):
            raise ValueError("The coordinate reference must be a JPG or PNG")
        if kind == 0x01 or 0xD0 <= kind <= 0xD7:
            continue
        length_bytes = _read_exact(handle, offset, 2, file_size)
        length = (length_bytes[0] << 8) | length_bytes[1]
        if length < 2 or offset + length > file_size:
            raise ValueError("The coordinate reference must be a JPG or PNG")
        if kind in _JPEG_SOF_MARKERS:
            if length < 8:
                raise ValueError("The coordinate reference must be a JPG or PNG")
            frame = _read_exact(handle, offset + 2, 6, file_size)
            if length < 8 + 3 * frame[5]:
                raise ValueError("The coordinate reference must be a JPG or PNG")
            height = (frame[1] << 8) | frame[2]
            width = (frame[3] << 8) | frame[4]
            if width <= 0 or height <= 0:
                raise ValueError("The coordinate reference has invalid dimensions")
            if width * height > _MAX_REFERENCE_HEADER_PIXELS:
                raise ValueError("The coordinate reference image dimensions exceed safety limits")
            return {"width": width, "height": height}
        offset += length
        handle.seek(offset)
    raise ValueError("The coordinate reference must be a JPG or PNG")


def load_roi_file(path, *, width: int, height: int, image_path=None) -> dict:
    """Read headers/text only; never decode pixels or write user annotations."""
    path = Path(path)
    if path.suffix.lower() != ".txt":
        raise ValueError("Choose an AstroMapper .txt file")
    candidate_suffixes = (*ROI_IMAGE_SUFFIXES, *(suffix.upper() for suffix in ROI_IMAGE_SUFFIXES))
    paired = Path(image_path) if image_path is not None else next(
        (candidate for suffix in candidate_suffixes
         if Path(filesystem_path(candidate := path.with_suffix(suffix))).is_file()), None)
    if paired is None:
        raise ValueError("Load the matching full-image JPG and AstroMapper TXT together")
    if (paired.suffix.lower() not in ROI_IMAGE_SUFFIXES or
            paired.stem.casefold() != path.stem.casefold()):
        raise ValueError("The reference image and TXT must have the same filename stem")
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise ValueError("Invalid image dimensions")
    flags = os.O_RDONLY | BINARY | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(filesystem_path(path), flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Choose a regular TXT file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > MAX_BYTES:
        raise ValueError("ROI TXT must be smaller than 16 MiB")
    try:
        text = raw.decode("utf-16" if raw.startswith((bytes([255, 254]), bytes([254, 255]))) else "utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("Save the ROI text as UTF-8 or UTF-16") from exc
    img_fd = os.open(filesystem_path(paired), flags)
    try:
        img_stat = os.fstat(img_fd)
        if not stat.S_ISREG(img_stat.st_mode):
            raise ValueError("Choose a regular reference image")
        with os.fdopen(img_fd, "rb") as handle:
            img_fd = -1
            # Stored pixel axes match raster.py and the canonical JPEG header reader;
            # EXIF orientation and DPI must not rotate or calibrate these coordinates.
            size = _read_image_size_stream(handle, img_stat.st_size)
    finally:
        if img_fd >= 0:
            os.close(img_fd)
    return {"ok": True, "name": path.name, "text": text, "coordinateSize": size,
            "reference": {"name": paired.name, **size}}
