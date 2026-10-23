"""JPEG/PNG images as a read-only RGB pyramid, in stored pixel axes.

Astromapper coordinates describe the decoded pixel grid, so EXIF display
rotation and DPI do not change the image dimensions or invent calibration.
Only the base image is retained; lower levels are bounded window resamples.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from .direct_common import _Lifecycle, _parse_cyx_index, _Root
from .reader import ChannelInfo, PlaneSource, level_shapes

RASTER_SUFFIXES = {".jpg", ".jpeg", ".png"}
MAX_PIXELS = 64_000_000  # includes an 8000 x 8000 Astromap image
MAX_JPEG_PIXELS = 512_000_000  # full-resolution stitched microscopy JPEGs


def is_raster(path: str | Path) -> bool:
    return Path(path).suffix.lower() in RASTER_SUFFIXES


class _RasterLevel:
    def __init__(self, image, shape, downsample, life, lock):
        self._image, self._life, self._lock = image, life, lock
        self.shape = (3, *shape)
        self.dtype = np.dtype("uint8")
        self._downsample = downsample

    def __getitem__(self, index: Any) -> np.ndarray:
        from PIL import Image

        channels, y0, y1, x0, x1 = _parse_cyx_index(index, self.shape)
        with self._life, self._lock:
            if x0 == x1 or y0 == y1:
                return np.empty((3, y1 - y0, x1 - x0), self.dtype)[channels]
            d = self._downsample
            # A narrow axis can already have collapsed to one pixel.
            dx, dy = min(d, self._image.width), min(d, self._image.height)
            box = (x0 * dx, y0 * dy, x1 * dx, y1 * dy)
            if d == 1:
                window = self._image.crop(box)
            else:
                window = self._image.resize((x1 - x0, y1 - y0), Image.Resampling.BOX, box=box)
            try:
                return np.ascontiguousarray(np.moveaxis(np.asarray(window), -1, 0)[channels])
            finally:
                window.close()


def open_direct(path: str | Path) -> tuple[Any, dict[str, Any]]:
    """Open a bounded raster once, with no pixel cache or source writes."""
    from PIL import Image, JpegImagePlugin

    from .convert import available_memory_bytes, build_group_attrs
    from .render import _available_memory

    path = Path(path)
    with path.open("rb") as probe:
        is_jpeg = probe.read(3) == b"\xff\xd8\xff"
    # Use Pillow's ordinary JPEG header handler directly, then apply our own
    # pixel and memory limits before decoding. Image.open's generic bomb limit
    # rejects legitimate large microscopy JPEGs; never change that global limit.
    image = JpegImagePlugin.jpeg_factory(path) if is_jpeg else Image.open(path)
    try:
        if image.format not in {"JPEG", "PNG"}:
            raise ValueError("Raster input must be a JPEG or PNG file")
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("Animated PNG is not supported; open a single frame")
        width, height = image.size
        max_pixels = MAX_JPEG_PIXELS if image.format == "JPEG" else MAX_PIXELS
        if width * height > max_pixels:
            raise ValueError(f"{image.format} input exceeds {max_pixels / 1_000_000:g} megapixels")
        # Budget the source decode, RGB/RGBA conversion, and alpha mask before
        # asking Pillow's decoder to allocate pixels (RGB uses four bytes/px).
        # Windows/Linux report current available physical memory. On macOS,
        # keep the established conservative quarter-of-physical-memory budget.
        available = available_memory_bytes()
        budget = (available if available is not None else _available_memory()) // 2
        if width * height * 16 > budget:
            raise ValueError("Not enough memory to open this image at full resolution; close other slides and retry")
        orientation = image.getexif().get(274, 1)
        notes = ["Stored pixel orientation; physical calibration is unavailable"]
        if orientation != 1:
            notes.append(f"EXIF orientation {orientation} is ignored to preserve TXT pixel coordinates")
        image.load()
        if image.mode != "RGB":
            original = image
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image if image.mode == "RGBA" else image.convert("RGBA")
                try:
                    image = Image.new("RGB", rgba.size, (0, 0, 0))
                    alpha = rgba.getchannel("A")
                    try:
                        image.paste(rgba, mask=alpha)
                    finally:
                        alpha.close()
                finally:
                    if rgba is not original:
                        rgba.close()
                notes.append("Transparency is composited on black")
            else:
                image = image.convert("RGB")
            original.close()
    except BaseException:
        image.close()
        raise

    try:
        shapes = level_shapes(height, width, 512)
        source = PlaneSource(
            data=None, dtype=np.dtype("uint8"), shape=(3, height, width), rgb=True,
            channels=[ChannelInfo("Red", (255, 0, 0)), ChannelInfo("Green", (0, 255, 0)),
                      ChannelInfo("Blue", (0, 0, 255))],
            pixel_size_um=None, source_name=str(path), notes=notes,
        )
        windows = [{"start": 0.0, "end": 255.0, "min": 0.0, "max": 255.0}] * 3
        attrs = build_group_attrs(source, shapes, 512, windows)
        attrs["nd2wsi"].update(direct=True, raster=True)
        life, lock = _Lifecycle(), threading.Lock()
        levels = {str(k): _RasterLevel(image, shape, 2 ** k, life, lock)
                  for k, shape in enumerate(shapes)}

        def close():
            if life.close():
                image.close()

        return _Root(levels, close), attrs
    except BaseException:
        image.close()
        raise
