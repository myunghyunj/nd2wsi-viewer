"""Interactive multi-plane Z/T/C stack loading, frame caching, prefetching, and container scene sources."""

from __future__ import annotations

import math
import os
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from . import plate as _plate_mod
from . import render
from .direct_common import (
    LeaseClosed,
    _Lifecycle,
    _parse_cyx_index,
    _Root,
    _TileCache,
    box_mean,
)
from .ometiff_levels import _MAX_STACK_FRAME_BYTES, _compute_default_windows
from .ometiff_metadata import _apply_col_override
from .platform_io import BINARY
from .reader import ChannelInfo, PlaneSource, level_shapes

STACK_MIN_DIM = 8
STACK_MAX_DIM = 8192
STACK_MAX_FRAME_PX = 40_000_000

PLATE_K = (2, 4, 8, 16)
THUMB_K = 8
HISTOGRAM_LRU = 64
PREFETCH_PENDING_MAX = 64
Z_PREFETCH_PENDING_MAX = 2

# Share the process-wide full-frame budget with PlateSource so opening OME-TIFF
# and ND2/CZI/LIF Z-stacks concurrently never compounds two separate frame caches.
_STACK_FRAME_CACHE = _plate_mod._FRAME_CACHE
_STACK_REDUCED_CACHE = _TileCache(64 * 1024 * 1024)


def _sharpness_score(a: np.ndarray) -> float:
    x = np.asarray(a, dtype=np.float32)
    if x.ndim == 3:
        x = x.mean(axis=0)
    if x.ndim != 2 or x.shape[0] < 2 or x.shape[1] < 2:
        return 0.0
    gy = np.diff(x, axis=0)
    gx = np.diff(x, axis=1)
    energy = float(np.mean(gy * gy)) + float(np.mean(gx * gx))
    level = float(x.mean())
    if not math.isfinite(energy) or not math.isfinite(level):
        return 0.0
    return energy / (level * level + 1e-6)


class _StackArrayLevel:
    def __init__(self, array: np.ndarray):
        self._a = array
        self.shape = tuple(array.shape)
        self.dtype = array.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        return self._a[cs, y0:y1, x0:x1].copy()


class _StackReducedLevel:
    def __init__(self, source: Any, t: int, p: int, z: int, k: int):
        self._source = source
        self._tpz = (t, p, z)
        self._k = int(k)
        c, h, w = source.frame_shape
        self.shape = (c, max(1, h // self._k), max(1, w // self._k))
        self.dtype = source.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        t, p, z = self._tpz
        return self._source.reduced(t, p, z, self._k)[cs, y0:y1, x0:x1].copy()


class _BaseInteractiveStackSource:
    """Shared interactive multi-plane Z/T stack source (PlateSource-compatible)."""

    def _init_common(self, path: str | Path, series: int, tile: int) -> None:
        import uuid

        self.path = Path(path).resolve()
        self.series_index = int(series)
        self.tile = int(tile)
        self.store = None
        self._open_generation = uuid.uuid4().hex
        self._owner = (str(self.path), self.series_index, self._open_generation)
        self._life = _Lifecycle()
        self._io_lock = threading.Lock()
        self._closed = False
        self._close_event = threading.Event()
        self._fg_lock = threading.Lock()
        self._fg = 0
        self._bg_tls = threading.local()
        self._inflight_lock = threading.Lock()
        self._inflight_frames: dict[tuple[int, int, int], threading.Event] = {}
        self._hist_lock = threading.Lock()
        self._hist: OrderedDict[tuple[int, int, int], list[dict[str, Any]]] = OrderedDict()
        self._focus_lock = threading.Lock()
        self._focus_cache: dict[int, list[float]] = {}
        self._pf_lock = threading.Lock()
        self._pf_cv = threading.Condition(self._pf_lock)
        self._pf_queue: deque[tuple[int, int, int, int]] = deque()
        self._pf_set: set[tuple[int, int, int, int]] = set()
        self._pf_thread: threading.Thread | None = None
        self._prefetch_epoch = 0
        self._zpf_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._zpf_executor: ThreadPoolExecutor | None = None
        self._zpf_futures: list[Future[None]] = []
        self._zpf_pending_targets: list[tuple[int, int, int]] = []
        self._zpf_thread: threading.Thread | None = None
        self._last_prefetch_tpz: tuple[int, int, int] | None = None
        self._zpf_dir = 1

        st = os.stat(self.path)
        self._ident = (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))

    def _finalize_stack_attrs(
        self,
        *,
        source_label: str,
        include_selection_p: bool,
        channel_dtypes: list[str] | None = None,
    ) -> None:
        from .convert import build_group_attrs

        _, H, W = self.frame_shape
        shapes = level_shapes(H, W, self.tile)
        self.levels = [
            {"path": str(k), "width": w, "height": h, "downsample": 2**k}
            for k, (h, w) in enumerate(shapes)
        ]

        home_small = self.reduced(0, 0, self.z_home, 4)
        windows = _compute_default_windows(home_small, self.dtype, self.rgb)

        notes: list[str] = []
        if self.T > 1:
            notes.append(f"time series: {self.T} frames")
        if self.z_step_um:
            notes.append(f"{self.Z} z planes, step {self.z_step_um:g} µm")
        elif self.Z > 1:
            notes.append(f"{self.Z} z planes")
        if self.pixel_size_um is None:
            notes.append(f"no physical pixel calibration in {source_label} metadata; measurements are in pixels")
        notes.append(f"interactive stack served directly from {source_label}")

        selection_meta: dict[str, Any] = {"p": self.series_index} if include_selection_p else {}
        src_info = PlaneSource(
            data=None,
            dtype=self.dtype,
            shape=self.frame_shape,
            rgb=self.rgb,
            channels=self.channels,
            pixel_size_um=self.pixel_size_um,
            source_name=str(self.path),
            selection=selection_meta,
            notes=notes,
            magnification=self.magnification,
            calibration_source=self.calibration_source if self.pixel_size_um else "unknown",
            channel_dtypes=channel_dtypes,
        )
        attrs = build_group_attrs(src_info, shapes, self.tile, windows)
        meta = attrs["nd2wsi"]
        meta["direct"] = True
        meta["kind"] = "plate"
        meta["plate"] = {
            "annotationScope": "plane",
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
            "frameW": W,
            "frameH": H,
        }
        self.attrs = attrs

    def _read_plane_cyx(self, t: int, p: int, z: int) -> np.ndarray:
        raise NotImplementedError

    def _close_backend_handles(self) -> None:
        pass

    def _check_source(self) -> None:
        if self._closed:
            raise ValueError("slide is closed")
        try:
            st = os.stat(self.path)
        except OSError as exc:
            raise ValueError(
                f"the source file is unreachable ({exc.strerror}); close and reopen the slide"
            ) from exc
        if (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns)) != self._ident:
            raise ValueError("the source file changed while it was open; close and reopen the slide")

    def frame(self, t: int, p: int, z: int) -> np.ndarray:
        if self._closed:
            raise ValueError("slide is closed")
        t, p, z = int(t), int(p), int(z)
        if not (0 <= t < self.T and 0 <= p < self.P and 0 <= z < self.Z):
            raise ValueError(f"frame ({t}, {p}, {z}) out of range")
        tpz = (t, p, z)
        key = (self._owner, "frame", t, p, z)
        with self._life:
            self._check_source()
            hit = _STACK_FRAME_CACHE.get(key)
            if hit is not None:
                return hit
            with self._inflight_lock:
                hit = _STACK_FRAME_CACHE.get(key)
                if hit is not None:
                    return hit
                waiter = self._inflight_frames.get(tpz)
                if waiter is None:
                    waiter = threading.Event()
                    self._inflight_frames[tpz] = waiter
                    leader = True
                else:
                    leader = False
            if not leader:
                waiter.wait()
                if self._closed:
                    raise ValueError("slide is closed")
                hit = _STACK_FRAME_CACHE.get(key)
                if hit is not None:
                    return hit
            fg_delta = 0 if getattr(self._bg_tls, "active", False) else 1
            if fg_delta:
                with self._fg_lock:
                    self._fg += fg_delta
            try:
                cyx = self._read_plane_cyx(t, p, z)
                cyx = np.ascontiguousarray(cyx)
                if not self._closed:
                    _STACK_FRAME_CACHE.put(key, cyx)
                return cyx
            finally:
                if fg_delta:
                    with self._fg_lock:
                        self._fg -= fg_delta
                if leader:
                    with self._inflight_lock:
                        self._inflight_frames.pop(tpz, None)
                    waiter.set()

    def frame_view(self, t: int, p: int, z: int) -> np.ndarray:
        return self.frame(t, p, z)

    def reduced(self, t: int, p: int, z: int, k: int, *, persist: bool = True) -> np.ndarray:
        if self._closed:
            raise ValueError("slide is closed")
        k = int(k)
        if k not in PLATE_K:
            raise ValueError(f"k must be one of {PLATE_K}")
        t, p, z = int(t), int(p), int(z)
        with self._life:
            self._check_source()
            key = (self._owner, t, p, z, k)
            hit = _STACK_REDUCED_CACHE.get(key)
            if hit is not None:
                return hit
            full = self.frame(t, p, z)
            _, h, w = full.shape
            if h >= k and w >= k:
                out = np.ascontiguousarray(box_mean(full, k, self.dtype))
            else:
                out = np.ascontiguousarray(full[:, :: min(k, h), :: min(k, w)])
            if not self._closed:
                _STACK_REDUCED_CACHE.put(key, out)
            return out

    def root_for(self, t: int, p: int, z: int) -> _Root:
        levels: dict[str, Any] = {"0": _StackArrayLevel(self.frame(t, p, z))}
        for lv in self.levels[1:]:
            levels[lv["path"]] = _StackReducedLevel(self, t, p, z, int(lv["downsample"]))
        return _Root(levels, closer=None)

    def status(self) -> dict[str, Any]:
        total = self.T * self.P * self.Z
        return {
            "done": total,
            "total": total,
            "perT": [self.P * self.Z] * self.T,
            "path": None,
            "format": None,
            "building": False,
            "writer": False,
            "integrityErrors": 0,
            "repairPending": 0,
        }

    def focus_map(self) -> dict[str, Any]:
        with self._life:
            self._check_source()
            best: list[list[int]] = []
            measured_z: list[list[int]] = []
            complete: list[list[bool]] = []
            for t in range(self.T):
                with self._focus_lock:
                    scores = self._focus_cache.get(t)
                if scores is None:
                    scores = [
                        _sharpness_score(self.reduced(t, 0, z, THUMB_K))
                        for z in range(self.Z)
                    ]
                    with self._focus_lock:
                        self._focus_cache[t] = scores
                best_z = int(np.argmax(scores)) if scores else int(self.z_home)
                best.append([best_z])
                measured_z.append([self.Z])
                complete.append([True])
            return {
                "best": best,
                "measured": self.T * self.P,
                "total": self.T * self.P,
                "measuredZ": measured_z,
                "totalZ": self.Z,
                "complete": complete,
                "completeCount": self.T * self.P,
                "zHome": int(self.z_home),
                "scoring": {
                    "channelMode": "all-channels-mean-image",
                    "metric": "mean-squared-gradient-normalized-by-mean",
                },
            }

    def histogram(self, t: int, p: int, z: int) -> list[dict[str, Any]]:
        key = (int(t), int(p), int(z))
        with self._life:
            self._check_source()
            with self._hist_lock:
                hit = self._hist.get(key)
                if hit is not None:
                    self._hist.move_to_end(key)
                    return hit
        small = self.reduced(t, p, z, THUMB_K)
        attrs = dict(self.attrs)
        attrs["nd2wsi"] = {
            **self.attrs["nd2wsi"],
            "levels": [
                {
                    "path": "0",
                    "width": small.shape[2],
                    "height": small.shape[1],
                    "downsample": THUMB_K,
                }
            ],
        }
        hist = render.compute_histograms({"0": _StackArrayLevel(small)}, attrs, min_pixels=1)
        for h_item in hist:
            h_item["level"] = str(THUMB_K)
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
        col: str | None = None,
    ) -> bytes:
        with self._fg_lock:
            self._fg += 1
        try:
            region = self.reduced(t, p, z, k)
        finally:
            with self._fg_lock:
                self._fg -= 1
        windows, colors = render.display_params(self.attrs)
        if col and not self.rgb:
            colors = _apply_col_override(colors, col)
        windows, gammas = render.parse_windows(win, windows)
        img = render.composite(region, channels, windows, colors, self.rgb, gammas)
        return render.encode_image(img, fmt)

    def prefetch(self, t: int, z: int, k: int) -> None:
        if k not in PLATE_K or self._closed:
            return
        wanted: list[tuple[int, int, int, int]] = []
        if t + 1 < self.T:
            wanted.append((t + 1, 0, z, int(k)))
        for z2 in (z + 1, z - 1):
            if 0 <= z2 < self.Z:
                wanted.append((t, 0, z2, int(k)))
        try:
            with self._pf_cv:
                if self._closed:
                    return
                for item in wanted:
                    if item in self._pf_set:
                        continue
                    while len(self._pf_queue) >= PREFETCH_PENDING_MAX:
                        old = self._pf_queue.popleft()
                        self._pf_set.discard(old)
                    self._pf_queue.append(item)
                    self._pf_set.add(item)
                if self._pf_thread is None or not self._pf_thread.is_alive():
                    self._pf_thread = threading.Thread(
                        target=self._prefetch_worker, name="stack-prefetch", daemon=True
                    )
                    self._pf_thread.start()
                self._pf_cv.notify()
        except Exception:
            pass

    def prefetch_neighbors(self, t: int, p: int, z: int) -> None:
        try:
            if self.Z <= 1 or self._closed:
                return
            t_i, p_i, z_i = int(t), int(p), int(z)
            if not (0 <= t_i < self.T and 0 <= p_i < self.P and 0 <= z_i < self.Z):
                return
            estimated_frame_bytes = int(math.prod(self.frame_shape)) * int(self.dtype.itemsize)
            cache_budget = _plate_mod._effective_frame_cache_budget()
            if cache_budget <= 0 or estimated_frame_bytes * 3 > cache_budget * 2:
                max_neighbors = 0
            elif estimated_frame_bytes * 2 > cache_budget or estimated_frame_bytes * 3 > cache_budget:
                max_neighbors = 1
            else:
                max_neighbors = 2

            with self._zpf_lock:
                if self._closed:
                    return
                self._prefetch_epoch += 1
                epoch = self._prefetch_epoch
                if self._last_prefetch_tpz is not None:
                    prev_t, prev_p, prev_z = self._last_prefetch_tpz
                    if (prev_t, prev_p) == (t_i, p_i) and z_i != prev_z:
                        self._zpf_dir = 1 if z_i > prev_z else -1
                    elif (prev_t, prev_p) != (t_i, p_i):
                        self._zpf_dir = 1
                self._last_prefetch_tpz = (t_i, p_i, z_i)

                for fut in self._zpf_futures:
                    fut.cancel()
                self._zpf_futures.clear()
                self._zpf_pending_targets.clear()

                if max_neighbors <= 0:
                    return
                step = self._zpf_dir
                neighbor_zs = [
                    z2 for z2 in (z_i + step, z_i - step) if 0 <= z2 < self.Z
                ][:max_neighbors]
                targets = [
                    (t_i, p_i, z2)
                    for z2 in neighbor_zs
                    if _STACK_FRAME_CACHE.get((self._owner, "frame", t_i, p_i, z2)) is None
                ]
                if not targets:
                    return
                if self._zpf_executor is None:
                    self._zpf_executor = ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="stack-z-prefetch",
                    )
                self._zpf_pending_targets = list(targets)
                for target in targets[:Z_PREFETCH_PENDING_MAX]:
                    self._zpf_futures.append(
                        self._zpf_executor.submit(self._run_z_prefetch, epoch, target)
                    )
                if self._zpf_thread is None:
                    self._zpf_thread = next(
                        iter(getattr(self._zpf_executor, "_threads", ())), None
                    )
        except Exception:
            pass

    def _run_z_prefetch(self, epoch: int, target: tuple[int, int, int]) -> None:
        self._zpf_thread = threading.current_thread()
        if epoch != self._prefetch_epoch or self._closed:
            return
        if self._close_event.wait(0.015):
            return
        if epoch != self._prefetch_epoch or self._closed:
            return
        while self._fg > 0:
            if epoch != self._prefetch_epoch or self._closed or self._close_event.wait(0.005):
                return
        if epoch != self._prefetch_epoch or self._closed:
            return
        t_i, p_i, z_i = target
        if _STACK_FRAME_CACHE.get((self._owner, "frame", t_i, p_i, z_i)) is not None:
            return
        if epoch != self._prefetch_epoch or self._closed:
            return
        self._bg_tls.active = True
        try:
            self.frame(t_i, p_i, z_i)
        except (LeaseClosed, ValueError, OSError):
            pass
        finally:
            self._bg_tls.active = False

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
            while self._fg > 0 and not self._closed:
                time.sleep(0.015)
            if self._closed:
                return
            try:
                if _STACK_REDUCED_CACHE.get((self._owner, t, p, z, k)) is None:
                    self.reduced(t, p, z, k)
            except (LeaseClosed, ValueError, OSError):
                if self._closed:
                    return

    def close(self, timeout: float | None = 300.0, **_kw: Any) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._close_event.set()
        with self._pf_cv:
            self._pf_queue.clear()
            self._pf_set.clear()
            self._pf_cv.notify_all()
        with self._zpf_lock:
            self._prefetch_epoch += 1
            for fut in self._zpf_futures:
                fut.cancel()
            self._zpf_futures.clear()
            self._zpf_pending_targets.clear()
        drained = self._life.close(timeout=timeout)
        with self._zpf_lock:
            zpf_exec, self._zpf_executor = self._zpf_executor, None
        pf_thread = self._pf_thread
        zpf_thread = self._zpf_thread
        if zpf_exec is not None:
            zpf_exec.shutdown(
                wait=(threading.current_thread() is not zpf_thread),
                cancel_futures=True,
            )
        if zpf_thread is not None and zpf_thread.is_alive() and zpf_thread is not threading.current_thread():
            zpf_thread.join(timeout=5.0)
        if pf_thread is not None and pf_thread.is_alive() and pf_thread is not threading.current_thread():
            pf_thread.join(timeout=5.0)
        if drained:
            self._close_backend_handles()
            _STACK_FRAME_CACHE.purge(self._owner)
            _STACK_REDUCED_CACHE.purge(self._owner)


class ContainerSceneStackSource(_BaseInteractiveStackSource):
    """Targeted single-series interactive Z/T stack source for multi-series LIF and CZI."""

    def __init__(self, path: str | Path, *, series: int, tile: int = 512):
        self._init_common(path, series, tile)
        suffix = self.path.suffix.lower()
        if suffix not in (".lif", ".czi"):
            raise ValueError(f"Unsupported container stack format {suffix}")
        self._backend = suffix[1:]
        self._fd = os.open(str(self.path), os.O_RDONLY | BINARY)
        try:
            file_size = os.fstat(self._fd).st_size
            if self._backend == "lif":
                ch_dtypes = self._init_lif_series(file_size)
                label = "LIF"
            else:
                ch_dtypes = self._init_czi_scene(file_size)
                label = "CZI"
            self._finalize_stack_attrs(
                source_label=label,
                include_selection_p=True,
                channel_dtypes=ch_dtypes,
            )
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            raise

    def _init_lif_series(self, file_size: int) -> list[str]:
        from .lif import _list_lif_series, _read_lif_header_and_blocks

        root, blocks = _read_lif_header_and_blocks(self._fd, file_size)
        all_series = _list_lif_series(root, blocks)
        if not (0 <= self.series_index < len(all_series)):
            raise ValueError(f"series {self.series_index} out of range (0..{len(all_series) - 1})")
        ser = all_series[self.series_index]
        self._lif_spec = ser
        if ser.tile_positions_px and ser.n_m > 1:
            width = max(tx + ser.width for tx, _ty in ser.tile_positions_px)
            height = max(ty + ser.height for _tx, ty in ser.tile_positions_px)
        else:
            width, height = ser.width, ser.height
        n_c = len(ser.channels)
        if width <= 0 or height <= 0 or width * height * max(1, n_c) * np.dtype(ser.dtype).itemsize > _MAX_STACK_FRAME_BYTES:
            raise ValueError(f"LIF interactive stack frame ({n_c}x{height}x{width}) exceeds memory budget")
        self.T = max(1, int(ser.n_t))
        self.P = 1
        self.Z = max(1, int(ser.n_z))
        self.frame_shape = (n_c, height, width)
        self.dtype = np.dtype(ser.dtype)
        self.rgb = bool(ser.rgb)
        self.channels = [ChannelInfo(ch.name, ch.color) for ch in ser.channels]
        self.pixel_size_um = ser.pixel_size_um
        self.calibration_source = ser.calibration_source
        self.magnification = ser.magnification
        self.sites = [
            {"i": 0, "name": ser.name or f"Series {self.series_index + 1}", "row": 0, "col": 0, "stageUm": None}
        ]
        self.rows = 1
        self.cols = 1
        self.z_home = self.Z // 2
        self.z_step_um = ser.z_step_um
        self.bottom_to_top = True if self.z_step_um is not None else None
        self.period_ms = None
        self.times_ms = []
        self.exposure_ms = None
        return [str(ch.dtype) for ch in ser.channels]

    def _init_czi_scene(self, file_size: int) -> list[str]:
        from .czi import (
            _extract_czi_metadata,
            _extract_czi_z_step_um,
            _level0_entries,
            _read_czi_header_and_index,
            _resolve_czi_entries_spec,
        )

        entries, xml_root = _read_czi_header_and_index(self._fd, file_size)
        lvl0 = _level0_entries(entries)
        s_values = sorted({e.s for e in lvl0})
        if not (0 <= self.series_index < len(s_values)):
            raise ValueError(f"series {self.series_index} out of range (0..{len(s_values) - 1})")
        s_target = s_values[self.series_index]
        s_entries = [e for e in lvl0 if e.s == s_target]

        x0 = min(e.x_start for e in s_entries)
        y0 = min(e.y_start for e in s_entries)
        width = max(e.x_start + e.x_size for e in s_entries) - x0
        height = max(e.y_start + e.y_size for e in s_entries) - y0

        dtype, rgb, channel_dtypes = _resolve_czi_entries_spec(s_entries)
        c_values = sorted({e.c for e in s_entries})
        z_values = sorted({e.z for e in s_entries})
        t_values = sorted({e.t for e in s_entries})
        n_c = 3 if rgb else len(c_values)
        if width <= 0 or height <= 0 or width * height * max(1, n_c) * np.dtype(dtype).itemsize > _MAX_STACK_FRAME_BYTES:
            raise ValueError(f"CZI interactive stack frame ({n_c}x{height}x{width}) exceeds memory budget")

        self.T = max(1, len(t_values))
        self.P = 1
        self.Z = max(1, len(z_values))
        self.frame_shape = (n_c, height, width)
        self.dtype = np.dtype(dtype)
        self.rgb = bool(rgb)

        self._czi_origin = (x0, y0)
        self._czi_samples = 3 if rgb else 1
        self._czi_c_values = c_values
        self._czi_z_values = z_values
        self._czi_t_values = t_values
        by_tz: dict[tuple[int, int], list[Any]] = {}
        by_tzc: dict[tuple[int, int, int], list[Any]] = {}
        for sb in s_entries:
            by_tz.setdefault((sb.t, sb.z), []).append(sb)
            by_tzc.setdefault((sb.t, sb.z, sb.c), []).append(sb)
        self._czi_by_tz = by_tz
        self._czi_by_tzc = by_tzc

        pixel_size_um, cal_source, magnification, channels = _extract_czi_metadata(
            xml_root, n_c, self.rgb, c_values=c_values
        )
        self.channels = channels
        self.pixel_size_um = pixel_size_um
        self.calibration_source = cal_source
        self.magnification = magnification
        self.sites = [{"i": 0, "name": f"Scene {self.series_index + 1}", "row": 0, "col": 0, "stageUm": None}]
        self.rows = 1
        self.cols = 1
        self.z_home = self.Z // 2
        self.z_step_um = _extract_czi_z_step_um(xml_root)
        self.bottom_to_top = True if self.z_step_um is not None else None
        self.period_ms = None
        self.times_ms = []
        self.exposure_ms = None
        return channel_dtypes

    def _read_plane_cyx(self, t: int, _p: int, z: int) -> np.ndarray:
        if self._backend == "lif":
            from .lif import _read_lif_channel_window

            ser = self._lif_spec
            tile_coords = (
                ser.tile_positions_px if (ser.tile_positions_px and ser.n_m > 1) else [(0, 0)]
            )
            out = np.zeros(self.frame_shape, dtype=self.dtype)
            for ci, ch_spec in enumerate(ser.channels):
                for m_idx, (tx0, ty0) in enumerate(tile_coords):
                    patch = _read_lif_channel_window(
                        self._fd, ser, ch_spec, z, t, m_idx, 0, ser.height, 0, ser.width
                    )
                    out[ci, ty0 : ty0 + ser.height, tx0 : tx0 + ser.width] = patch
            return out

        from .czi import _read_subblock_window

        t_val = self._czi_t_values[t]
        z_val = self._czi_z_values[z]
        x0, y0 = self._czi_origin
        n_c, height, width = self.frame_shape
        if self.rgb:
            z_sbs = self._czi_by_tz.get((t_val, z_val), ())
            bg_fill = (
                (65535 if self.dtype == np.uint16 else (255 if self.dtype == np.uint8 else 0))
                if len(z_sbs) > 1
                else 0
            )
            out = np.full((3, height, width), bg_fill, dtype=self.dtype)
            for sb in z_sbs:
                sx0, sy0 = sb.x_start - x0, sb.y_start - y0
                patch = _read_subblock_window(
                    self._fd, sb, self.dtype, self._czi_samples, 0, sb.y_size, 0, sb.x_size
                )
                out[:, sy0 : sy0 + sb.y_size, sx0 : sx0 + sb.x_size] = np.moveaxis(patch, -1, 0)
            return out
        out = np.zeros((n_c, height, width), dtype=self.dtype)
        for ci, c_val in enumerate(self._czi_c_values):
            for sb in self._czi_by_tzc.get((t_val, z_val, c_val), ()):
                sx0, sy0 = sb.x_start - x0, sb.y_start - y0
                patch = _read_subblock_window(
                    self._fd, sb, self.dtype, 1, 0, sb.y_size, 0, sb.x_size
                )
                out[ci, sy0 : sy0 + sb.y_size, sx0 : sx0 + sb.x_size] = patch
        return out

    def _close_backend_handles(self) -> None:
        with self._close_lock:
            fd, self._fd = self._fd, -1
        if fd >= 0:
            os.close(fd)
