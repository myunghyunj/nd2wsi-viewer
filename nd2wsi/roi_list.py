"""Read chosen AstroMapper JPG/TXT pairs for the existing client-side importer.

astromap-import-v1.js owns inclusive pixel endpoints and exact source mapping.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .platform_io import BINARY

MAX_BYTES = 16 * 1024 * 1024
# Header-only inspection never decodes pixels; allow full-slide reference images
# up to 2 billion pixels (e.g. ~45k x 45k) while refusing absurd headers.
_MAX_REFERENCE_HEADER_PIXELS = 2_000_000_000


def load_roi_file(path, *, width: int, height: int, image_path=None) -> dict:
    """Read headers/text only; never decode pixels or write user annotations."""
    from PIL import Image

    path = Path(path)
    if path.suffix.lower() != ".txt":
        raise ValueError("Choose an AstroMapper .txt file")
    paired = Path(image_path) if image_path is not None else next(
        (path.with_suffix(suffix) for suffix in (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG")
         if path.with_suffix(suffix).is_file()), None)
    if paired is None:
        raise ValueError("Load the matching full-image JPG and AstroMapper TXT together")
    if (paired.suffix.lower() not in (".jpg", ".jpeg", ".png") or
            paired.stem.casefold() != path.stem.casefold()):
        raise ValueError("The reference image and TXT must have the same filename stem")
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise ValueError("Invalid image dimensions")
    flags = os.O_RDONLY | BINARY | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags)
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
    prev_limit = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = _MAX_REFERENCE_HEADER_PIXELS
        with Image.open(paired) as image:
            if image.format not in ("JPEG", "PNG"):
                raise ValueError("The coordinate reference must be a JPG or PNG")
            if image.width <= 0 or image.height <= 0:
                raise ValueError("The coordinate reference has invalid dimensions")
            # Stored pixel axes match raster.py and the canonical JPEG header reader;
            # EXIF orientation and DPI must not rotate or calibrate these coordinates.
            size = {"width": image.width, "height": image.height}
    except Image.DecompressionBombError as exc:
        raise ValueError("The coordinate reference image dimensions exceed safety limits") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit
    return {"ok": True, "name": path.name, "text": text, "coordinateSize": size,
            "reference": {"name": paired.name, **size}}
