"""Plate mode. A time series of camera fields, served straight from the ND2.

A multipoint time lapse with a z stack holds thousands of small frames,
one camera field each, and no stitching. Building a pyramid cache of one
plane would be useless for such a file, so this module keeps the ND2 open
and reads frames straight from its memory map. Reduced frames feed the
site grid, and one site at a time can be viewed at full resolution
through the same tile pipeline the stitched slides use.

Nothing is written to disk. The reduced frames live in a process wide
cache bounded by bytes, and a single background thread warms the frames
the viewer is most likely to ask for next.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
import warnings
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

import numpy as np

from . import render
from .cache import (
    CACHE_SUFFIX,
    CACHES_DIR,
    MANIFEST_NAME,
    CacheLock,
    fingerprints_match,
    managed_dir,
    quarantine,
    quick_fingerprint,
    source_tag,
)
from .direct import _Lifecycle, _parse_cyx_index, _Root, _TileCache
from .reader import (
    FRAME_AXES,
    PlaneSource,
    _channel_infos,
    _frame_to_cyx,
    _nd2_pixel_size,
    _seq_index,
    level_shapes,
    objective_magnification,
)

PLATE_MAX_FRAME_PX = 4_500_000  # a camera field, not a stitched scan
PLATE_CACHE_BYTES = 768 * 1024 * 1024  # reduced frames kept in memory
FRAME_CACHE_BYTES = 320 * 1024 * 1024  # whole frames kept for the focused site
PLATE_K = (2, 4, 8, 16)  # box mean factors the grid may ask for
PREFETCH_PENDING_MAX = 256  # frames queued for warming, oldest dropped first
HISTOGRAM_LRU = 64
THUMB_K = 8  # the reduction the store keeps, the one the site grid shows
PLATE_FORMAT = "nd2wsi-plate/1"
PLATE_TAG = "plate"
THUMBS_NAME = "thumbs.zarr"

# one reduction budget for the whole process, not one per open file
_REDUCED_CACHE = _TileCache(PLATE_CACHE_BYTES)
# whole frames read for the focused site or for a reduction, a few at a time
_FRAME_CACHE = _TileCache(FRAME_CACHE_BYTES)


def is_plate_file(path: str | Path) -> bool:
    """True for an uncompressed ND2 of camera fields with a T, P or Z loop."""
    try:
        path = Path(path)
        if path.suffix.lower() != ".nd2" or not path.is_file():
            return False
        import nd2

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with nd2.ND2File(str(path)) as f:
                if getattr(f, "is_legacy", False):
                    return False
                if (getattr(f.attributes, "compressionType", None) or "none") != "none":
                    return False
                sizes = dict(f.sizes)
                area = int(sizes.get("Y", 0)) * int(sizes.get("X", 0))
                if area <= 0 or area > PLATE_MAX_FRAME_PX:
                    return False
                loops = sizes.get("T", 1) * sizes.get("P", 1) * sizes.get("Z", 1)
                return loops > 1
    except Exception:
        return False


def _cluster_1d(values: list[float]) -> list[int]:
    """Cluster index per value, ascending, split at gaps wider than
    ``max(2000 um, 0.3 * span)``."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    span = values[order[-1]] - values[order[0]]
    gap = max(2000.0, 0.3 * span)
    labels = [0] * n
    c = 0
    for prev, cur in zip(order, order[1:]):
        if values[cur] - values[prev] > gap:
            c += 1
        labels[cur] = c
    return labels


def site_layout(points: list[tuple[float, float] | None]) -> list[tuple[int, int]]:
    """(row, col) per stage point. Rows follow ascending Y, columns ascending X.

    Without usable coordinates every site lands in row 0 at its own index.
    """
    n = len(points)
    if n == 0:
        return []
    xs: list[float] = []
    ys: list[float] = []
    for pt in points:
        try:
            x, y = float(pt[0]), float(pt[1])  # type: ignore[index]
        except (TypeError, ValueError, IndexError):
            return [(0, i) for i in range(n)]
        if not (math.isfinite(x) and math.isfinite(y)):
            return [(0, i) for i in range(n)]
        xs.append(x)
        ys.append(y)
    rows = _cluster_1d(ys)
    cols = _cluster_1d(xs)
    return list(zip(rows, cols))


def _exposure_ms(text_info: dict) -> float | None:
    for key in ("capturing", "description"):
        text = text_info.get(key)
        if not isinstance(text, str):
            continue
        m = re.search(r"Exposure:\s*([0-9]+(?:\.[0-9]+)?)\s*ms", text)
        if m:
            return float(m.group(1))
    return None


def _finite_positive(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _loop(f: Any, kind: str) -> Any:
    try:
        for loop in f.experiment or []:
            if getattr(loop, "type", None) == kind:
                return loop
    except Exception:
        return None
    return None


def plate_container(slide: str | Path) -> Path:
    """Where the thumbnail store of a plate file lives, beside the file."""
    slide = Path(slide)
    return managed_dir(slide) / CACHES_DIR / f"{source_tag(slide)}--{PLATE_TAG}{CACHE_SUFFIX}"


class PlateStore:
    """The 8x reductions of every frame, kept on disk beside the file.

    One zarr array holds the reductions, chunked one frame at a time, and a
    small ``done`` array records which frames are in. Frames arrive as the
    viewer asks for them and, in the background, as a builder walks every
    frame that is still missing, starting near where the viewer is looking.
    The container is keyed by the source fingerprint like every other
    cache, so a drive that moves to another Mac brings the store along.
    """

    def __init__(self, source: PlateSource, container: Path, root: Any, manifest: dict):
        self._source = source
        self.container = container
        self._root = root
        self._thumbs = root["thumbs"]
        self._done = root["done"]
        self.manifest = manifest
        self.total = int(source.T * source.P * source.Z)
        self._lock = threading.Lock()
        self._done_np = np.asarray(self._done[:]).astype(bool)
        self._cursor = (0, source.z_home)
        self._stop = False
        self._thread: threading.Thread | None = None

    # ---- open or create -------------------------------------------------
    @classmethod
    def open_or_create(cls, source: PlateSource) -> PlateStore:
        import zarr

        container = plate_container(source.path)
        fingerprint = quick_fingerprint(source.path)
        c, h, w = source.frame_shape
        shape = (source.T, source.P, source.Z, c, h // THUMB_K, w // THUMB_K)
        container.parent.mkdir(parents=True, exist_ok=True)
        with CacheLock(container):
            manifest = cls._read(container)
            if manifest is not None and not cls._matches(manifest, fingerprint, shape, source):
                quarantine(container)
                manifest = None
            if manifest is None:
                manifest = cls._create(source, container, fingerprint, shape)
        root = zarr.open_group(str(container / THUMBS_NAME), mode="r+", zarr_format=2)
        store = cls(source, container, root, manifest)
        store.sweep()
        return store

    def sweep(self) -> None:
        """Drop the AppleDouble twins macOS leaves beside every file on
        exFAT and NTFS drives; each one costs a whole allocation block."""
        try:
            from .convert import sweep_appledouble

            sweep_appledouble(self.container)
        except Exception:
            pass

    @staticmethod
    def _read(container: Path) -> dict | None:
        try:
            m = json.loads((container / MANIFEST_NAME).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return m if isinstance(m, dict) and m.get("format") == PLATE_FORMAT else None

    @staticmethod
    def _matches(manifest: dict, fingerprint: dict, shape: tuple, source: PlateSource) -> bool:
        thumbs = manifest.get("thumbs") or {}
        return (
            fingerprints_match(manifest.get("source") or {}, fingerprint)
            and list(thumbs.get("shape") or []) == list(shape)
            and thumbs.get("dtype") == str(source.dtype)
            and thumbs.get("k") == THUMB_K
            and (Path(source.path).parent / (container_name := manifest.get("thumbs_name", THUMBS_NAME)))
            is not None
            and container_name == THUMBS_NAME
        )

    @staticmethod
    def _create(source: PlateSource, container: Path, fingerprint: dict, shape: tuple) -> dict:
        import zarr
        from numcodecs import Blosc

        from . import __version__

        if container.exists():
            quarantine(container)
        container.mkdir(parents=True, exist_ok=True)
        root = zarr.open_group(str(container / THUMBS_NAME), mode="w", zarr_format=2)
        c, h8, w8 = shape[3], shape[4], shape[5]
        # one chunk per time point and z plane holding every site: that is
        # what the grid reads in one go, and it keeps the file count low on
        # exFAT drives whose allocation blocks are a megabyte each
        root.create_array(
            name="thumbs",
            shape=shape,
            chunks=(1, shape[1], 1, c, h8, w8),
            dtype=source.dtype,
            compressors=Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE),
            dimension_names=None,
            overwrite=True,
            fill_value=0,
        )
        root.create_array(
            name="done",
            shape=shape[:3],
            chunks=(1, shape[1], shape[2]),
            dtype=np.uint8,
            overwrite=True,
            fill_value=0,
        )
        manifest = {
            "format": PLATE_FORMAT,
            "kind": "plate",
            "complete": True,
            "generation": uuid.uuid4().hex,
            "source": {**fingerprint, "relative_path": os.path.relpath(source.path, container)},
            "thumbs": {"k": THUMB_K, "shape": list(shape), "dtype": str(source.dtype)},
            "thumbs_name": THUMBS_NAME,
            "created_by": {"nd2wsi_version": __version__},
        }
        tmp = container / f".{MANIFEST_NAME}.tmp"
        tmp.write_text(json.dumps(manifest, indent=1))
        tmp.replace(container / MANIFEST_NAME)
        return manifest

    # ---- frames -----------------------------------------------------------
    def get(self, t: int, p: int, z: int) -> np.ndarray | None:
        if not self._done_np[t, p, z]:
            return None
        try:
            return np.ascontiguousarray(self._thumbs[t, p, z])
        except Exception:
            return None

    def put(self, t: int, p: int, z: int, frame: np.ndarray) -> None:
        if self._done_np[t, p, z] or self._stop:
            return
        # sites share a chunk, so writes are serialized: two writers
        # rewriting the same chunk would drop each other's site
        with self._lock:
            if self._done_np[t, p, z]:
                return
            try:
                self._thumbs[t, p, z] = frame
                self._done[t, p, z] = 1
                self._done_np[t, p, z] = True
            except Exception:
                pass  # a store that cannot be written is only a slower one

    def count(self) -> int:
        return int(self._done_np.sum())

    def per_t(self) -> list[int]:
        return [int(v) for v in self._done_np.reshape(self._done_np.shape[0], -1).sum(axis=1)]

    def status(self) -> dict[str, Any]:
        return {
            "done": self.count(),
            "total": self.total,
            "perT": self.per_t(),
            "path": str(self.container),
            "building": bool(self._thread and self._thread.is_alive()),
        }

    # ---- builder ----------------------------------------------------------
    def steer(self, t: int, z: int) -> None:
        self._cursor = (int(t), int(z))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self.count() >= self.total:
            return
        self._thread = threading.Thread(target=self._build, name="plate-store", daemon=True)
        self._thread.start()

    def _next_missing(self) -> tuple[int, int, int] | None:
        """The first frame not yet stored, walking from the viewer's
        position: this z at this t, then later times, then other planes."""
        T, P, Z = self._done_np.shape
        t0, z0 = self._cursor
        order_z = [z0] + [z for d in range(1, Z) for z in (z0 + d, z0 - d) if 0 <= z < Z]
        for dt in range(T):
            t = (t0 + dt) % T
            for z in order_z:
                for p in range(P):
                    if not self._done_np[t, p, z]:
                        return (t, p, z)
        return None

    def _build(self) -> None:
        src = self._source
        while not self._stop:
            item = self._next_missing()
            if item is None:
                return
            while src._fg > 0 and not self._stop:
                time.sleep(0.02)
            if self._stop:
                return
            try:
                src.reduced(*item, THUMB_K)
            except Exception:
                if self._stop or src._closed:
                    return
                time.sleep(0.5)
            self._since_sweep = getattr(self, "_since_sweep", 0) + 1
            if self._since_sweep >= 64:
                self._since_sweep = 0
                self.sweep()
            time.sleep(0.005)
        self.sweep()

    def close(self) -> None:
        self._stop = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)


class _ArrayLevel:
    """A whole frame held in memory, sliced like a (C, H, W) array."""

    def __init__(self, array: np.ndarray):
        self._a = array
        self.shape = tuple(array.shape)
        self.dtype = array.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        return self._a[cs, y0:y1, x0:x1].copy()


class _ReducedLevel:
    """One pyramid level of a site, sliced like a (C, H//k, W//k) array."""

    def __init__(self, source: PlateSource, t: int, p: int, z: int, k: int):
        self._source = source
        self._tpz = (t, p, z)
        self._k = k
        c, h, w = source.frame_shape
        self.shape = (c, h // k, w // k)
        self.dtype = source.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        t, p, z = self._tpz
        return self._source.reduced(t, p, z, self._k)[cs, y0:y1, x0:x1].copy()


class PlateSource:
    """An open ND2 of camera fields, addressed by (t, p, z).

    A cold frame is read with one sequential read of its bytes, never by
    faulting the memory map page by page, which on an external drive costs
    seconds per frame. The frames of the focused site and the reduced
    frames of the grid live in two process wide caches bounded by bytes.
    The memory map is only touched by ``frame_view`` for callers that ask
    for a zero copy view, inside the lifecycle guard.
    """

    def __init__(self, path: str | Path, tile: int = 512, store: bool = True):
        import nd2

        self.path = Path(path)
        self.tile = int(tile)
        self.store: PlateStore | None = None
        self._owner = str(self.path)
        self._life = _Lifecycle()
        self._closed = False
        self._hist_lock = threading.Lock()
        self._hist: OrderedDict[tuple[int, int, int], list[dict]] = OrderedDict()
        self._pf_lock = threading.Lock()
        self._pf_cv = threading.Condition(self._pf_lock)
        self._pf_queue: deque[tuple[int, int, int, int]] = deque()
        self._pf_set: set[tuple[int, int, int, int]] = set()
        self._pf_thread: threading.Thread | None = None
        # a replaced file must never alias frames a previous open reduced
        _REDUCED_CACHE.purge(self._owner)

        self._fd = -1
        self._fg = 0  # foreground reads in flight; the prefetch worker yields to them
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._f = nd2.ND2File(str(self.path))
        try:
            self._fd = os.open(str(self.path), os.O_RDONLY)
            self._init_from_file(self._f)
            if store:
                try:
                    self.store = PlateStore.open_or_create(self)
                    self.store.start()
                except Exception as e:  # a store that will not open is only a slower view
                    self.store = None
                    self.attrs["nd2wsi"].setdefault("notes", []).append(
                        f"thumbnail store unavailable ({type(e).__name__}); frames are read each time"
                    )
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            self._f.close()
            raise

    # ---- metadata --------------------------------------------------------
    def _init_from_file(self, f: Any) -> None:
        self.sizes = dict(f.sizes)
        self._coord_axes = [a for a in self.sizes if a not in FRAME_AXES]
        self.T = int(self.sizes.get("T", 1))
        self.P = int(self.sizes.get("P", 1))
        self.Z = int(self.sizes.get("Z", 1))
        st = os.stat(self.path)
        self._ident = (st.st_size, st.st_mtime_ns)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw0 = f.read_frame(0)
        self._raw_shape = tuple(int(v) for v in raw0.shape)
        cyx, rgb = _frame_to_cyx(raw0, self.sizes)
        self.frame_shape = tuple(int(v) for v in cyx.shape)
        self.dtype = np.dtype(cyx.dtype)
        self.rgb = bool(rgb)
        self.channels = _channel_infos(f, self.frame_shape[0], self.rgb)
        self.pixel_size_um, self.calibration_source = _nd2_pixel_size(f)
        self.magnification = objective_magnification(f)

        # sites from the XYPosLoop
        self.sites: list[dict[str, Any]] = []
        points: list[tuple[float, float] | None] = []
        xy = _loop(f, "XYPosLoop")
        raw_points = []
        try:
            raw_points = list(xy.parameters.points) if xy is not None else []
        except Exception:
            raw_points = []
        for p in range(self.P):
            name = f"Site {p + 1}"
            stage = None
            if p < len(raw_points):
                pt = raw_points[p]
                pname = getattr(pt, "name", None)
                if pname:
                    name = str(pname)
                pos = getattr(pt, "stagePositionUm", None)
                try:
                    stage = [float(pos.x), float(pos.y), float(pos.z)]
                    if not all(math.isfinite(v) for v in stage):
                        stage = None
                except (AttributeError, TypeError, ValueError):
                    stage = None
            self.sites.append({"i": p, "name": name, "row": 0, "col": p, "stageUm": stage})
            points.append((stage[0], stage[1]) if stage else None)
        for site, (row, col) in zip(self.sites, site_layout(points)):
            site["row"], site["col"] = int(row), int(col)
        self.rows = 1 + max((s["row"] for s in self.sites), default=0)
        self.cols = 1 + max((s["col"] for s in self.sites), default=0)

        # z from the ZStackLoop
        zl = _loop(f, "ZStackLoop")
        self.z_home = self.Z // 2
        self.z_step_um: float | None = None
        self.bottom_to_top = True
        if zl is not None:
            params = getattr(zl, "parameters", None)
            home = getattr(params, "homeIndex", None)
            if isinstance(home, int) and 0 <= home < self.Z:
                self.z_home = home
            self.z_step_um = _finite_positive(getattr(params, "stepUm", None))
            self.bottom_to_top = bool(getattr(params, "bottomToTop", True))
        if self.z_step_um is None:
            try:
                self.z_step_um = _finite_positive(f.voxel_size().z)
            except Exception:
                self.z_step_um = None

        # time from the TimeLoop and the per frame metadata
        tl = _loop(f, "TimeLoop")
        self.period_ms: float | None = None
        if tl is not None:
            self.period_ms = _finite_positive(
                getattr(getattr(tl, "parameters", None), "periodMs", None)
            )
        self.times_ms: list[float] = []
        for t in range(self.T):
            value = None
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    fm = f.frame_metadata(self.seq(t, 0, 0))
                value = float(fm.channels[0].time.relativeTimeMs)
                if not math.isfinite(value):
                    value = None
            except Exception:
                value = None
            if value is None:
                value = t * self.period_ms if self.period_ms else float(t)
            self.times_ms.append(value)

        try:
            self.exposure_ms = _exposure_ms(dict(f.text_info or {}))
        except Exception:
            self.exposure_ms = None

        _, H, W = self.frame_shape
        shapes = level_shapes(H, W, self.tile)
        self.levels = [
            {"path": str(k), "width": w, "height": h, "downsample": 2**k}
            for k, (h, w) in enumerate(shapes)
        ]
        self.attrs = self._build_attrs(shapes)

    def _build_attrs(self, shapes: list[tuple[int, int]]) -> dict[str, Any]:
        from .convert import _background_mode_start, build_group_attrs

        notes = []
        if self.T > 1 and self.period_ms:
            notes.append(
                f"time series: {self.T} frames every {self.period_ms / 60000:g} min"
            )
        elif self.T > 1:
            notes.append(f"time series: {self.T} frames")
        notes.append(f"{self.P} sites")
        if self.z_step_um:
            notes.append(f"{self.Z} z planes, step {self.z_step_um:g} µm")
        else:
            notes.append(f"{self.Z} z planes")
        if self.pixel_size_um is None:
            notes.append("no pixel calibration in the file; measurements are in pixels")
        notes.append("served straight from the ND2; nothing is cached on disk")

        src = PlaneSource(
            data=None,
            dtype=self.dtype,
            shape=self.frame_shape,
            rgb=self.rgb,
            channels=self.channels,
            pixel_size_um=self.pixel_size_um,
            source_name=str(self.path),
            selection={},
            notes=notes,
            magnification=self.magnification,
            calibration_source=self.calibration_source,
        )
        windows = self._windows(_background_mode_start)
        attrs = build_group_attrs(src, shapes, self.tile, windows)
        meta = attrs["nd2wsi"]
        meta["direct"] = True
        meta["kind"] = "plate"
        meta["plate"] = {
            "T": self.T,
            "P": self.P,
            "Z": self.Z,
            "sites": [dict(s) for s in self.sites],
            "rows": self.rows,
            "cols": self.cols,
            "zHome": self.z_home,
            "zStepUm": self.z_step_um,
            "bottomToTop": self.bottom_to_top,
            "timesMs": list(self.times_ms),
            "periodMs": self.period_ms,
            "exposureMs": self.exposure_ms,
            "frameW": self.frame_shape[2],
            "frameH": self.frame_shape[1],
        }
        return attrs

    def _windows(self, background_mode_start: Any) -> list[dict[str, float]]:
        """Display windows from the 4x reductions of every site at t 0, home z.

        A camera field of a plate is phase contrast or bright field as
        often as fluorescence, and its histogram peak is the sample, not a
        black background, so the black point is a low percentile rather
        than the background mode the stitched scans use.
        """
        samples = [self.reduced(0, p, self.z_home, 4) for p in range(self.P)]
        small = np.concatenate([s.reshape(s.shape[0], -1) for s in samples], axis=1)
        if np.issubdtype(self.dtype, np.integer):
            info = np.iinfo(self.dtype)
            lo_all, hi_all = float(info.min), float(info.max)
        else:
            finite = small[np.isfinite(small)]
            lo_all, hi_all = (
                (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
            )
        windows = []
        for ci in range(small.shape[0]):
            if self.rgb:
                start, end = 0.0, 255.0
            else:
                ch = small[ci]
                values = ch[np.isfinite(ch)] if np.issubdtype(ch.dtype, np.floating) else ch
                if values.size:
                    start = float(np.percentile(values, 0.5))
                    end = float(np.percentile(values, 99.8))
                    if end <= start:
                        end = start + 1.0
                else:
                    start, end = 0.0, 1.0
            windows.append({"start": start, "end": end, "min": lo_all, "max": hi_all})
        return windows

    # ---- frames ----------------------------------------------------------
    def seq(self, t: int, p: int, z: int) -> int:
        return _seq_index(self.sizes, self._coord_axes, {"T": t, "P": p, "Z": z})

    def _check_source(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError as e:
            raise ValueError(
                f"the source file is unreachable ({e.strerror}); close and reopen the slide"
            ) from e
        if (st.st_size, st.st_mtime_ns) != self._ident:
            raise ValueError(
                "the source file changed while it was open; close and reopen the slide"
            )

    def frame_view(self, t: int, p: int, z: int) -> np.ndarray:
        """(C, H, W) zero copy view onto the memory map. Valid until close."""
        seq = self.seq(t, p, z)
        with self._life:
            view, _ = _frame_to_cyx(self._f.read_frame(seq), self.sizes)
        return view

    def _read_raw(self, seq: int) -> np.ndarray:
        """The frame's pixels as an array that owns its memory.

        One ``pread`` of the frame's bytes: a whole frame arrives in one
        sequential read instead of thousands of page faults. Falls back to
        a copy out of the memory map when the file pads its rows or the
        reader gives no offset.
        """
        rdr = getattr(self._f, "_rdr", None)
        offset = None
        strides = None
        try:
            offset = rdr._frame_offsets.get(seq) if rdr is not None else None
            strides = rdr._strides if rdr is not None else None
        except Exception:
            offset = None
        nbytes = int(np.prod(self._raw_shape)) * int(self.dtype.itemsize)
        if offset is not None and strides is None and self._fd >= 0:
            buf = os.pread(self._fd, nbytes, int(offset))
            if len(buf) == nbytes:
                return np.frombuffer(buf, dtype=self.dtype).reshape(self._raw_shape)
        with self._life:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return np.array(self._f.read_frame(seq), copy=True)

    def frame(self, t: int, p: int, z: int) -> np.ndarray:
        """(C, H, W) frame that owns its memory, cached a few at a time."""
        key = (self._owner, "frame", int(t), int(p), int(z))
        hit = _FRAME_CACHE.get(key)
        if hit is not None:
            return hit
        seq = self.seq(t, p, z)
        self._fg += 1
        try:
            self._check_source()
            raw = self._read_raw(seq)
        finally:
            self._fg -= 1
        cyx, _ = _frame_to_cyx(raw, self.sizes)
        cyx = np.ascontiguousarray(cyx)
        _FRAME_CACHE.put(key, cyx)
        return cyx

    def reduced(self, t: int, p: int, z: int, k: int) -> np.ndarray:
        """(C, H//k, W//k) box mean of one frame, rounded to the source dtype."""
        k = int(k)
        if k not in PLATE_K:
            raise ValueError(f"k must be one of {PLATE_K}")
        key = (self._owner, int(t), int(p), int(z), k)
        hit = _REDUCED_CACHE.get(key)
        if hit is not None:
            return hit
        if k == THUMB_K and self.store is not None:
            stored = self.store.get(int(t), int(p), int(z))
            if stored is not None:
                _REDUCED_CACHE.put(key, stored)
                return stored
        frame = self.frame(t, p, z)
        c, h, w = frame.shape
        h2, w2 = h // k, w // k
        block = frame[:, : h2 * k, : w2 * k].reshape(c, h2, k, w2, k)
        out = block.mean(axis=(2, 4), dtype=np.float32)
        if np.issubdtype(self.dtype, np.integer):
            out = np.rint(out).astype(self.dtype)
        else:
            out = out.astype(self.dtype)
        out = np.ascontiguousarray(out)
        _REDUCED_CACHE.put(key, out)
        if k == THUMB_K and self.store is not None:
            self.store.put(int(t), int(p), int(z), out)
        return out

    def status(self) -> dict[str, Any]:
        """How much of the thumbnail store is filled, for the time line."""
        if self.store is None:
            return {"done": 0, "total": self.T * self.P * self.Z, "perT": [0] * self.T, "path": None, "building": False}
        return self.store.status()

    def root_for(self, t: int, p: int, z: int) -> _Root:
        """A virtual pyramid root for one frame, level 0 from the frame cache."""
        levels: dict[str, Any] = {"0": _ArrayLevel(self.frame(t, p, z))}
        for lv in self.levels[1:]:
            levels[lv["path"]] = _ReducedLevel(self, t, p, z, int(lv["downsample"]))
        return _Root(levels, closer=None)

    def histogram(self, t: int, p: int, z: int) -> list[dict]:
        key = (int(t), int(p), int(z))
        with self._hist_lock:
            hit = self._hist.get(key)
            if hit is not None:
                self._hist.move_to_end(key)
                return hit
        hist = render.compute_histograms(self.root_for(t, p, z), self.attrs)
        with self._hist_lock:
            self._hist[key] = hist
            while len(self._hist) > HISTOGRAM_LRU:
                self._hist.popitem(last=False)
        return hist

    def render_frame(
        self,
        t: int,
        p: int,
        z: int,
        k: int,
        channels: list[int],
        fmt: str = "jpg",
        win: str | None = None,
    ) -> bytes:
        self._fg += 1
        try:
            region = self.reduced(t, p, z, k)
        finally:
            self._fg -= 1
        windows, colors = render.display_params(self.attrs)
        windows, gammas = render.parse_windows(win, windows)
        img = render.composite(region, channels, windows, colors, self.rgb, gammas)
        return render.encode_image(img, fmt)

    # ---- prefetch --------------------------------------------------------
    def prefetch(self, t: int, z: int, k: int) -> None:
        """Queue the neighbouring frames for warming. Never blocks, never raises."""
        try:
            k = int(k)
            if k not in PLATE_K or self._closed:
                return
            if self.store is not None:
                self.store.steer(t, z)
                self.store.start()
            wanted: list[tuple[int, int, int, int]] = []
            for p in range(self.P):
                if t + 1 < self.T:
                    wanted.append((t + 1, p, z, k))
                for z2 in (z + 1, z - 1):
                    if 0 <= z2 < self.Z:
                        wanted.append((t, p, z2, k))
            with self._pf_cv:
                for key in wanted:
                    if key in self._pf_set:
                        continue
                    while len(self._pf_queue) >= PREFETCH_PENDING_MAX:
                        old = self._pf_queue.popleft()
                        self._pf_set.discard(old)
                    self._pf_queue.append(key)
                    self._pf_set.add(key)
                if self._pf_thread is None or not self._pf_thread.is_alive():
                    self._pf_thread = threading.Thread(
                        target=self._prefetch_worker, name="plate-prefetch", daemon=True
                    )
                    self._pf_thread.start()
                self._pf_cv.notify()
        except Exception:
            pass

    def _prefetch_worker(self) -> None:
        while True:
            with self._pf_cv:
                while not self._pf_queue and not self._closed:
                    self._pf_cv.wait()
                if self._closed:
                    self._pf_queue.clear()
                    self._pf_set.clear()
                    return
                t, p, z, k = self._pf_queue.popleft()
                self._pf_set.discard((t, p, z, k))
            # a request in flight owns the disk; warming waits its turn
            while self._fg > 0 and not self._closed:
                time.sleep(0.02)
            try:
                if _REDUCED_CACHE.get((self._owner, t, p, z, k)) is None:
                    self.reduced(t, p, z, k)
            except Exception:
                if self._closed:
                    return

    # ---- teardown --------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.store is not None:
            self.store.close()
        with self._pf_cv:
            self._pf_cv.notify_all()
        if not self._life.close():
            # a reader still holds the map after the drain timeout. nd2's
            # frames are views onto the mmap and raise no BufferError on
            # close, so unmapping now would crash that reader. The handle
            # leaks until process exit instead.
            return
        try:
            self._f.close()
        except BufferError:  # pragma: no cover - defensive
            pass
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        _REDUCED_CACHE.purge(self._owner)
        _FRAME_CACHE.purge(self._owner)
