"""AstroMapper-compatible JPG/TSV exports with bounded source reads.

Rectangles stay in the viewer's displayed-base coordinates. Only the final
small RGB image is assembled; there is no full-size or 20,000-pixel canvas.
The browser receives a short-lived download capability, never a local path.
"""
from __future__ import annotations

import math
import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import render

MAX_EDGE = 8000
MAX_ROIS = 2000
READ_EDGE = 512
# Plate ND2 exposes a zero-copy frame view, but some compressed readers may
# decode it eagerly. Refuse larger frames before opening that view.
MAX_PLATE_FRAME_BYTES = 64 * 1024 * 1024
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
HEADER = "INDEX\tX1\tY1\tX2\tY2\tNOTE\n"


class AstromapError(ValueError):
    """A request the caller can correct without restarting the viewer."""


class AstromapConflict(AstromapError):
    """A stale generation or a concurrent export, reported as HTTP 409."""


def output_size(width: int, height: int) -> tuple[int, int]:
    major = max(width, height)
    if major <= MAX_EDGE:
        return width, height
    return max(1, width * MAX_EDGE // major), max(1, height * MAX_EDGE // major)


def _integer(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AstromapError(f"{name} must be a finite pixel coordinate")
    return int(value)


@dataclass(frozen=True)
class ExportBox:
    x1: int
    y1: int
    x2: int
    y2: int
    note: str


def convert_boxes(rois: list, source_size: tuple[int, int], size: tuple[int, int]) -> tuple[ExportBox, ...]:
    """Match Qt QRect inclusive endpoints, with correct target-image clipping."""
    width, height = source_size
    ow, oh = size
    if not isinstance(rois, list) or not 1 <= len(rois) <= MAX_ROIS:
        raise AstromapError(f"Select between 1 and {MAX_ROIS} ROIs")
    result = []
    for index, roi in enumerate(rois, 1):
        if not isinstance(roi, dict):
            raise AstromapError("Each ROI must be an object")
        if "enabled" in roi and type(roi["enabled"]) is not bool:
            raise AstromapError("ROI enabled must be a boolean")
        if roi.get("enabled", True) is False:
            continue
        try:
            x, y, w, h = (_integer(roi[key], key) for key in ("x", "y", "w", "h"))
        except KeyError as exc:
            raise AstromapError(f"ROI is missing {exc.args[0]}") from None
        if w < 1 or h < 1:
            raise AstromapError("ROI width and height must be at least one pixel")
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width - 1, x + w - 1), min(height - 1, y + h - 1)
        if x0 > x1 or y0 > y1:
            raise AstromapError("ROI is outside the image")
        note = roi.get("note", "")
        if not isinstance(note, str) or len(note) > 4096:
            raise AstromapError("ROI note must be text of at most 4096 characters")
        note = note.replace("\t", " ").replace("\r", " ").replace("\n", " ") or str(index)
        # Integer arithmetic is deterministic even for very large source images.
        result.append(ExportBox(
            min(ow - 1, x0 * ow // width), min(oh - 1, y0 * oh // height),
            min(ow - 1, x1 * ow // width), min(oh - 1, y1 * oh // height), note,
        ))
    if not result:
        raise AstromapError("Select at least one enabled ROI")
    return tuple(result)


def coordinates_text(boxes: tuple[ExportBox, ...]) -> str:
    return HEADER + "".join(
        f"{index}\t{box.x1}\t{box.y1}\t{box.x2}\t{box.y2}\t{box.note}\n"
        for index, box in enumerate(boxes, 1)
    )


def _filename(source: str) -> str:
    # Windows names and pasted paths must never become paths inside the archive.
    stem = Path(source.replace("\\", "/")).stem
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", stem).strip(". ")[:120]
    return stem or "slide"


@dataclass(frozen=True)
class AstromapPlan:
    root: Any
    attrs: dict
    level_path: str
    size: tuple[int, int]
    boxes: tuple[ExportBox, ...]
    channels: tuple[int, ...]
    window: str | None
    stem: str
    include_pdf: bool = False
    source_boxes: tuple[ExportBox, ...] = ()
    levels: tuple[dict, ...] = ()

    def render_image(self):
        """One final RGB canvas plus bounded channel/tile intermediates."""
        from PIL import Image, ImageDraw

        ow, oh = self.size
        # Pillow stores RGB images in four-byte internal pixels. Leave ample
        # room for encoding and the viewer's already-live data/cache.
        if ow * oh * 4 > render._available_memory() // 2:
            raise AstromapError("Not enough free memory for the small JPG; close other slides and retry")
        image = Image.new("RGB", self.size)
        try:
            if self.channels:
                arr = self.root[self.level_path]
                _, lh, lw = arr.shape
                sx, sy = lw / ow, lh / oh
                # Real viewer pyramids put the selected level within 2x of the
                # output. Adaptive tiles also bound reads for sparse pyramids.
                edge = min(READ_EDGE, max(16, int(math.sqrt(2_000_000 / len(self.channels)))))
                halo = max(2, math.ceil(max(sx, sy)))
                tile = max(1, min(512, int((edge - 2 * halo - 4) / max(sx, sy))))
                windows, colors = render.display_params(self.attrs)
                windows, gammas = render.parse_windows(self.window, windows)
                rgb = bool(self.attrs["nd2wsi"].get("rgb"))
                for oy in range(0, oh, tile):
                    ey = min(oh, oy + tile)
                    for ox in range(0, ow, tile):
                        ex = min(ow, ox + tile)
                        # Bilinear sampling uses identical global pixel centers
                        # for every output tile. The halo prevents tile seams.
                        x0 = max(0, math.floor(ox * sx) - halo)
                        y0 = max(0, math.floor(oy * sy) - halo)
                        x1 = min(lw, math.ceil(ex * sx) + halo)
                        y1 = min(lh, math.ceil(ey * sy) + halo)
                        if x1 - x0 > edge * 2 or y1 - y0 > edge * 2:
                            raise AstromapError("The image needs a reduced pyramid before AstroMap export")
                        if rgb:
                            block = np.zeros((3, y1 - y0, x1 - x0), dtype=arr.dtype)
                            for channel in self.channels:
                                block[channel] = np.asarray(arr[channel, y0:y1, x0:x1])
                            pixels = render.composite(block, list(self.channels), windows, colors, True, gammas)
                        else:
                            block = np.stack([np.asarray(arr[channel, y0:y1, x0:x1])
                                              for channel in self.channels])
                            pixels = render.composite(
                                block, list(range(len(self.channels))),
                                [windows[c] for c in self.channels], [colors[c] for c in self.channels],
                                False, [gammas[c] for c in self.channels],
                            )
                        source_tile = Image.fromarray(pixels)
                        resized = source_tile.resize(
                            (ex - ox, ey - oy), Image.Resampling.BILINEAR,
                            box=(ox * sx - x0, oy * sy - y0, ex * sx - x0, ey * sy - y0),
                        )
                        image.paste(resized, (ox, oy))
                        resized.close()
                        source_tile.close()
                del pixels, block
            draw = ImageDraw.Draw(image, "RGBA")
            for box in self.boxes:
                draw.rectangle((box.x1, box.y1, box.x2, box.y2),
                               fill=(255, 217, 102, 100), outline=(255, 255, 255, 255), width=1)
            return image
        except BaseException:
            image.close()
            raise

    def write(self, directory: Path) -> Path:
        """Disk-backed JPEG and ZIP; no large image-encoded bytes in memory."""
        jpg = directory / f"{self.stem}.jpg"
        txt = directory / f"{self.stem}.txt"
        pdf = directory / f"{self.stem}_rois.pdf"
        output = directory / f"{self.stem}_astromap.zip"
        with self.render_image() as image:
            image.save(jpg, "JPEG", quality=92)
        txt.write_text(coordinates_text(self.boxes), encoding="utf-8", newline="")
        if self.include_pdf:
            from .astromap_pdf import write_pdf

            write_pdf(self, jpg, pdf)
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.write(jpg, jpg.name, compress_type=zipfile.ZIP_STORED)
            bundle.write(txt, txt.name, compress_type=zipfile.ZIP_DEFLATED)
            if self.include_pdf:
                bundle.write(pdf, pdf.name, compress_type=zipfile.ZIP_STORED)
        jpg.unlink()
        txt.unlink()
        if self.include_pdf:
            pdf.unlink()
        return output


def prepare_astromap(state, payload: dict, *, resolve_frame, filename_prefix="") -> AstromapPlan:
    if not isinstance(payload, dict):
        raise AstromapError("Expected an AstroMap export object")
    include_pdf = payload.get("include_pdf", False)
    if type(include_pdf) is not bool:
        raise AstromapError("include_pdf must be a boolean")
    if not isinstance(payload.get("generation"), str):
        raise AstromapError("The slide generation is required")
    if payload["generation"] != state.generation:
        raise AstromapConflict("The pyramid changed; refresh the slide before exporting")
    meta = state.attrs["nd2wsi"]
    levels = meta["levels"]
    width, height = (int(levels[0][key]) for key in ("width", "height"))
    size = output_size(width, height)
    boxes = convert_boxes(payload.get("rois"), (width, height), size)
    source_boxes = convert_boxes(payload.get("rois"), (width, height), (width, height)) if include_pdf else ()
    channels = payload.get("channels")
    count = len(state.attrs["omero"]["channels"])
    if (not isinstance(channels, list) or len(channels) > count
            or any(type(c) is not int or not 0 <= c < count for c in channels)
            or len(set(channels)) != len(channels)):
        raise AstromapError("Channels must be a list of unique valid channel indices")
    if meta.get("rgb") and any(c > 2 for c in channels):
        raise AstromapError("RGB images support only the R, G, and B components")
    window = payload.get("window")
    if window is not None and (not isinstance(window, str) or len(window) > 32768):
        raise AstromapError("Invalid display window")
    frame = payload.get("frame")
    if state.plate is None:
        if frame is not None:
            raise AstromapError("This slide does not have plate frames")
        root = state.root
        suffix = ""
    else:
        if (not isinstance(frame, dict) or set(frame) != {"t", "p", "z"}
                or any(type(v) is not int for v in frame.values())):
            raise AstromapError("Choose a plate site and frame before exporting")
        try:
            resolved = resolve_frame(state, {k: [str(v)] for k, v in frame.items()}, require_p=True)
        except ValueError as exc:
            raise AstromapError(str(exc)) from exc
        # root_for eagerly caches the entire all-channel native frame, even if
        # only one reduced level is needed. Keep a zero-copy frame view under
        # the route's lifecycle lease, then read selected channels in tiles.
        # The preflight bound also covers readers that must decode a full frame.
        frame_bytes = math.prod(state.plate.frame_shape) * np.dtype(state.plate.dtype).itemsize
        if channels and frame_bytes > MAX_PLATE_FRAME_BYTES:
            raise AstromapError(
                "This plate frame exceeds the 64 MiB Astromap frame limit; "
                "large plate-frame export is not supported yet"
            )
        levels = [levels[0]]
        root = {str(levels[0]["path"]): state.plate.frame_view(*resolved)} if channels else {}
        suffix = "_t{}_p{}_z{}".format(*resolved)
    # The coordinate origin stays levels[0], even in a degraded overview whose
    # first stored path is 1. Use actual dimensions rather than 2**level.
    candidates = [lv for lv in levels if lv["width"] >= size[0] and lv["height"] >= size[1]]
    level = min(candidates or [levels[0]], key=lambda lv: lv["width"] * lv["height"])
    stem = filename_prefix + _filename(str(meta.get("source", "slide"))) + suffix
    return AstromapPlan(root, state.attrs, str(level["path"]), size, boxes, tuple(channels), window, stem,
                       include_pdf, source_boxes, tuple(levels))


@dataclass
class _Download:
    directory: Path
    path: Path
    filename: str
    owner: Any
    expires: float
    readers: int = 0


class AstromapDownloads:
    """Per-window, bounded download capabilities; failed/cancelled builds clean up."""

    def __init__(self, directory: Path | None = None, *, ttl=600.0, limit=4):
        self.directory = directory
        self.ttl, self.limit = ttl, limit
        self._items: OrderedDict[str, _Download] = OrderedDict()
        self._lock = threading.RLock()
        self._build = threading.Lock()
        self._closed = False
        self._timer = None

    def _prune(self):
        now = time.monotonic()
        for key, item in list(self._items.items()):
            if item.readers == 0 and (self._closed or item.expires <= now):
                shutil.rmtree(item.directory, ignore_errors=True)
                del self._items[key]

    def _schedule(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._items and not self._closed:
            self._timer = threading.Timer(max(0.1, self.ttl), self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _expire(self):
        with self._lock:
            self._timer = None
            self._prune()
            self._schedule()

    def create(self, plan: AstromapPlan, owner) -> dict:
        if not self._build.acquire(blocking=False):
            raise AstromapConflict("An AstroMap export is already running in this window")
        folder = None
        try:
            with self._lock:
                self._prune()
                if self._closed:
                    raise AstromapConflict("This window is closing")
                while len(self._items) >= self.limit:
                    oldest = next((key for key, item in self._items.items() if not item.readers), None)
                    if oldest is None:
                        raise AstromapConflict("Wait for the current AstroMap downloads to finish")
                    item = self._items.pop(oldest)
                    shutil.rmtree(item.directory, ignore_errors=True)
            if self.directory is not None:
                self.directory.mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix="astromap-", dir=self.directory))
            path = plan.write(folder)
            key = secrets.token_urlsafe(24)
            with self._lock:
                if self._closed:
                    raise AstromapConflict("This window closed before export finished")
                self._items[key] = _Download(folder, path, path.name, owner, time.monotonic() + self.ttl)
                self._schedule()
                folder = None
            return {"download_url": f"api/astromap/download?token={key}", "filename": path.name,
                    "width": plan.size[0], "height": plan.size[1], "count": len(plan.boxes)}
        finally:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            self._build.release()

    @contextmanager
    def download(self, token: str, owner):
        with self._lock:
            self._prune()
            item = self._items.get(token) if TOKEN_RE.fullmatch(token) else None
            if item is None or item.owner is not owner or self._closed:
                raise AstromapError("AstroMap download expired; export again")
            item.readers += 1
        try:
            # Open before returning; the lease prevents expiry/eviction until the
            # response finishes. Retain a failed download for a normal retry.
            with item.path.open("rb") as handle:
                yield item, handle
        finally:
            with self._lock:
                item.readers -= 1
                self._prune()

    def close(self):
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._prune()
