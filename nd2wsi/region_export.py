"""Transport-independent ROI export planning and temporary-file ownership.

The server retains the slide's lifecycle lease for planning, writing, and
streaming. This service owns ROI bounds, format policy, progress, and cleanup;
it has no socket, HTTP handler, registry, or global slide-state dependency.
"""
from __future__ import annotations

import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from . import render
from .direct import _parse_cyx_index

JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class ExportRequestError(ValueError):
    """An expected export rejection that the HTTP adapter reports as 400."""


class _OriginalReducedLevel:
    """Recreate one RAW pyramid level without reading corrected cache pixels.

    Match convert's box-mean-floor-v1: round each 2x reduction to the original
    dtype, drop odd edges, and repeat axes already collapsed to one pixel.
    Recursive reads stay bounded even when exporting a deeply reduced level.
    """

    def __init__(self, previous):
        self.previous = previous
        c, h, w = previous.shape
        self.shape = (c, max(1, h // 2), max(1, w // 2))
        self.dtype = np.dtype(previous.dtype)

    def __getitem__(self, index):
        cs, y0, y1, x0, x1 = _parse_cyx_index(index, self.shape)
        selected = np.arange(self.shape[0])[cs]
        scalar = np.ndim(selected) == 0
        selected = np.atleast_1d(selected)
        out = np.empty((len(selected), y1 - y0, x1 - x0), dtype=self.dtype)
        _, ph, pw = self.previous.shape
        integer = np.issubdtype(self.dtype, np.integer)
        for slot, ci in enumerate(selected):
            for y in range(y0, y1, 128):
                ey = min(y + 128, y1)
                for x in range(x0, x1, 128):
                    ex = min(x + 128, x1)
                    block = np.asarray(self.previous[int(ci), y * 2:min(ey * 2, ph),
                                                     x * 2:min(ex * 2, pw)])
                    if ph == 1:
                        block = np.repeat(block, 2, axis=0)
                    if pw == 1:
                        block = np.repeat(block, 2, axis=1)
                    mean = block.reshape(ey - y, 2, ex - x, 2).mean(
                        axis=(1, 3), dtype=np.float32)
                    values = np.rint(mean) if integer else mean
                    out[slot, y - y0:ey - y0, x - x0:ex - x0] = values.astype(self.dtype)
        return out[0] if scalar else out


def _original_export_root(root, entry: dict, dtype: str):
    native = getattr(root, "raw_level0", None)
    if native is None:
        raise ExportRequestError(
            "Original ND2 pixels are unavailable; reopen the original ND2 to export native ND2/TIFF values"
        )
    level = int(entry["path"])
    _, h, w = native.shape
    if (level < 0 or entry.get("downsample") != 2**level
            or (entry["height"], entry["width"]) != (max(1, h // 2**level), max(1, w // 2**level))
            or np.dtype(native.dtype) != np.dtype(dtype)):
        raise ExportRequestError("The original ND2 geometry does not match this export level; reopen the slide")
    levels = {"0": native}
    for k in range(1, level + 1):
        levels[str(k)] = _OriginalReducedLevel(levels[str(k - 1)])
    return levels


@dataclass(frozen=True)
class RenderedExport:
    body: bytes
    content_type: str
    filename: str


@dataclass(frozen=True)
class FileExport:
    path: Path
    content_type: str
    filename: str

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    def copy_to(self, output: BinaryIO) -> None:
        """Stream with bounded memory while the owner keeps this file alive."""
        with self.path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)


@dataclass(frozen=True)
class RegionExport:
    root: Any
    attrs: dict[str, Any]
    level: int
    x: int
    y: int
    w: int
    h: int
    channels: list[int]
    fmt: str
    window: str | None
    scale_bar: bool
    filename: str
    job: str | None
    max_render_mpx: float

    @property
    def streams_file(self) -> bool:
        return self.fmt in ("nd2", "tif", "tiff")

    def rendered(self) -> RenderedExport:
        if self.fmt not in ("png", "jpg", "jpeg", "svg"):
            raise ExportRequestError(f"unknown format {self.fmt}")
        if self.w * self.h / 1e6 > self.max_render_mpx:
            raise ExportRequestError(
                f"rendered export capped at {self.max_render_mpx:.0f} MPx; "
                f"requested {self.w * self.h / 1e6:.0f} MPx -- use format=tiff "
                "(streams any size) or a higher level"
            )
        try:
            body = render.export_roi_rendered(
                self.root, self.attrs, self.level, self.x, self.y, self.w, self.h,
                self.channels, self.fmt, self.window, scale_bar=self.scale_bar,
            )
        except ValueError as exc:
            raise ExportRequestError(str(exc)) from exc
        ext = "jpg" if self.fmt == "jpeg" else self.fmt
        content_type = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}[ext]
        suffix = "_scalebar" if self.scale_bar else ""
        return RenderedExport(body, content_type, f"{self.filename}{suffix}.{ext}")

    @contextmanager
    def file(self, update_job: Callable[..., None]) -> Iterator[FileExport]:
        """Own a raw export until its consumer finishes or disconnects.

        The named file is closed before a writer opens it (required on Windows),
        and unlinked after both successful transmission and every failure path.
        Progress reaches ``done`` only after the caller leaves the context.
        """
        if not self.streams_file:
            raise ExportRequestError(f"unknown format {self.fmt}")
        is_nd2 = self.fmt == "nd2"
        ext = ".nd2" if is_nd2 else ".tif"
        content_type = "application/octet-stream" if is_nd2 else "image/tiff"
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        update_job(self.job, state="writing", pct=0)

        def on_progress(frac: float) -> None:
            update_job(self.job, state="writing", pct=int(min(1.0, frac) * 100))

        try:
            tmp.close()
            args = (self.root, self.attrs, tmp.name, self.level, self.x, self.y,
                    self.w, self.h, self.channels)
            if is_nd2:
                from .export_nd2 import export_roi_nd2

                try:
                    export_roi_nd2(*args, on_progress=on_progress)
                except (RuntimeError, ValueError) as exc:
                    # These writer rejections were always client errors. Other
                    # failures retain their exception type for the HTTP adapter.
                    raise ExportRequestError(str(exc)) from exc
            else:
                render.export_roi_tiff(*args, on_progress=on_progress)
            update_job(self.job, state="streaming", pct=100)
            yield FileExport(Path(tmp.name), content_type, f"{self.filename}{ext}")
            update_job(self.job, state="done", pct=100)
        except ExportRequestError as exc:
            update_job(self.job, state="error", error=str(exc))
            raise
        except BrokenPipeError:
            update_job(self.job, state="error", error="client disconnected")
            raise
        except Exception as exc:
            update_job(self.job, state="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def prepare_export(
    state: Any,
    query: dict,
    *,
    resolve_frame: Callable[..., tuple[int, int, int] | None],
    filename_prefix: str = "",
) -> RegionExport:
    """Resolve the requested region without owning or changing slide state.

    The shared frame resolver is supplied by the adapter so tile, histogram,
    pixel, and export requests continue to use exactly the same site policy.
    """
    def qi(name: str, default: int | None = None) -> int:
        value = query.get(name)
        if not value:
            if default is None:
                raise ValueError(f"missing parameter {name}")
            return default
        return int(float(value[0]))

    meta = state.attrs["nd2wsi"]
    level = qi("level", 0)
    try:
        entry = render.level_entry(meta["levels"], level)
    except KeyError:
        raise ExportRequestError(f"level {level} out of range") from None
    lw, lh = entry["width"], entry["height"]
    try:
        x, y, w, h = qi("x"), qi("y"), qi("w"), qi("h")
    except ValueError as exc:
        raise ExportRequestError(str(exc)) from exc
    x, y = max(0, min(x, lw - 1)), max(0, min(y, lh - 1))
    w, h = max(1, min(w, lw - x)), max(1, min(h, lh - y))
    fmt = (query.get("format") or ["nd2"])[0].lower()
    scale_bar = fmt == "svg" or (query.get("scalebar") or ["0"])[0] == "1"
    if scale_bar and fmt not in ("svg", "jpg", "jpeg"):
        raise ExportRequestError("Scale bar export supports SVG and JPEG")
    channels = render.parse_channels(
        (query.get("c") or [None])[0], len(state.attrs["omero"]["channels"])
    )
    window = (query.get("win") or [None])[0]
    job = (query.get("job") or [None])[0]
    if job and not JOB_RE.match(job):
        job = None
    stem = Path(meta["source"]).stem
    try:
        frame = resolve_frame(state, query, require_p=True)
    except ValueError as exc:
        raise ExportRequestError(str(exc)) from exc
    root = state.root
    if frame is not None:
        root = state.plate.root_for(*frame)
        stem += "_t{}_p{}_z{}".format(*frame)
    applied = frame is None and (state.manifest.get("h_correction") or {}).get("applied") is True
    if applied:
        if fmt in ("nd2", "tif", "tiff"):
            root = _original_export_root(root, entry, meta["dtype"])
        elif fmt in ("png", "jpg", "jpeg", "svg"):
            stem += "_shading-corrected"
    filename = f"{filename_prefix}{stem}_L{level}_x{x}_y{y}_{w}x{h}"
    return RegionExport(
        root, state.attrs, level, x, y, w, h, channels, fmt, window, scale_bar,
        filename, job, state.max_render_mpx,
    )
