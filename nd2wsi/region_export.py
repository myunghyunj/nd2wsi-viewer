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

from . import render

JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class ExportRequestError(ValueError):
    """An expected export rejection that the HTTP adapter reports as 400."""


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
    filename = f"{filename_prefix}{stem}_L{level}_x{x}_y{y}_{w}x{h}"
    return RegionExport(
        root, state.attrs, level, x, y, w, h, channels, fmt, window, scale_bar,
        filename, job, state.max_render_mpx,
    )
