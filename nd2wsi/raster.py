"""JPEG/PNG images as a read-only RGB pyramid, in stored pixel axes.

Astromapper coordinates describe the decoded pixel grid, so EXIF display
rotation and DPI do not change the image dimensions or invent calibration.
The base image is retained; finer levels are bounded window resamples, and
overview levels are resampled whole on first read and kept until close.
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
# Pillow crops and resamples without the GIL and only reads the shared base
# image, so reads may overlap; the bound caps how many resample
# intermediates (one tile window or one memo band each) exist at once.
MAX_CONCURRENT_READS = 4
# Levels this coarse are kept whole after their first read. All of them
# together hold about 1/64 of the base image's memory (Pillow RGB is 4 B/px).
MEMO_MIN_DOWNSAMPLE = 8
_MEMO_BAND_ROWS = 64  # output rows per resample while building one memo level


def is_raster(path: str | Path) -> bool:
    return Path(path).suffix.lower() in RASTER_SUFFIXES


class _RasterLevel:
    def __init__(self, image, shape, downsample, life, slots):
        self._image, self._life, self._slots = image, life, slots
        self.shape = (3, *shape)
        self.dtype = np.dtype("uint8")
        self._downsample = downsample
        self._memo: np.ndarray | None = None
        self._memo_lock = threading.Lock()

    def _resample(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        """One window of this level as (h, w, 3), straight from the base image."""
        from PIL import Image

        d = self._downsample
        # A narrow axis can already have collapsed to one pixel.
        dx, dy = min(d, self._image.width), min(d, self._image.height)
        box = (x0 * dx, y0 * dy, x1 * dx, y1 * dy)
        with self._slots:
            if d == 1:
                window = self._image.crop(box)
            else:
                window = self._image.resize((x1 - x0, y1 - y0), Image.Resampling.BOX, box=box)
            try:
                return np.asarray(window)
            finally:
                window.close()

    def _whole(self) -> np.ndarray:
        """The whole level as (3, H, W), resampled once; callers hold the lifecycle.

        BOX windows on this level's grid do not depend on their neighbours,
        so building it in bands gives exactly the pixels a per-tile read
        would, while bounding the resampler's intermediate to one band.
        """
        with self._memo_lock:
            if self._memo is None:
                _, h, w = self.shape
                memo = np.empty(self.shape, self.dtype)
                for r0 in range(0, h, _MEMO_BAND_ROWS):
                    r1 = min(h, r0 + _MEMO_BAND_ROWS)
                    memo[:, r0:r1] = np.moveaxis(self._resample(r0, r1, 0, w), -1, 0)
                self._memo = memo
            return self._memo

    def release(self) -> None:
        """Drop the memoized level; only after the lifecycle has drained."""
        self._memo = None

    def __getitem__(self, index: Any) -> np.ndarray:
        channels, y0, y1, x0, x1 = _parse_cyx_index(index, self.shape)
        with self._life:
            if x0 == x1 or y0 == y1:
                return np.empty((3, y1 - y0, x1 - x0), self.dtype)[channels]
            if self._downsample >= MEMO_MIN_DOWNSAMPLE:
                # A copy, so no caller or tile cache pins or edits the memo.
                return self._whole()[:, y0:y1, x0:x1][channels].copy()
            window = self._resample(y0, y1, x0, x1)
            return np.ascontiguousarray(np.moveaxis(window, -1, 0)[channels])


def open_direct(path: str | Path) -> tuple[Any, dict[str, Any]]:
    """Open a bounded raster once, with no source writes.

    Reads at downsample >= ``MEMO_MIN_DOWNSAMPLE`` come from a level memoized
    on first use; close frees it, and the base image, once reads drain.
    """
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
        life, slots = _Lifecycle(), threading.BoundedSemaphore(MAX_CONCURRENT_READS)
        levels = {str(k): _RasterLevel(image, shape, 2 ** k, life, slots)
                  for k, shape in enumerate(shapes)}

        def close():
            if life.close():
                image.close()
                for level in levels.values():
                    level.release()

        return _Root(levels, close), attrs
    except BaseException:
        image.close()
        raise
