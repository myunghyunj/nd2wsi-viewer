"""Read chosen AstroMapper JPG/TXT pairs for the existing client-side importer.

astromap-import-v1.js owns inclusive pixel endpoints and exact source mapping.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024


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
    with path.open("rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Choose a regular TXT file")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("ROI TXT must be smaller than 16 MiB")
    try:
        text = raw.decode("utf-16" if raw.startswith((bytes([255, 254]), bytes([254, 255]))) else "utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("Save the ROI text as UTF-8 or UTF-16") from exc
    with Image.open(paired) as image:
        if image.format not in ("JPEG", "PNG"):
            raise ValueError("The coordinate reference must be a JPG or PNG")
        # Stored pixel axes match raster.py and the canonical JPEG header reader;
        # EXIF orientation and DPI must not rotate or calibrate these coordinates.
        size = {"width": image.width, "height": image.height}
    return {"ok": True, "name": path.name, "text": text, "coordinateSize": size,
            "reference": {"name": paired.name, **size}}
