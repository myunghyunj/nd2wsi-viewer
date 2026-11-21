"""Plate mode. A time series of camera fields, served from one open ND2.

A multipoint time lapse with a z stack holds thousands of small frames,
one camera field each, and no stitching. Building a pyramid cache of one
plane would be useless for such a file, so this module keeps the ND2 open
and reads frames straight from its memory map. Reduced frames feed the
site grid, and one site at a time can be viewed at full resolution
through the same tile pipeline the stitched slides use.

Reduced grid frames are persisted in a managed thumbnail cache beside the
source and also live in a process-wide, bytes-bounded memory cache.  The
source ND2 remains authoritative: an incomplete or unverifiable thumbnail
falls back to the corresponding source frame and is repaired by the one
viewer that owns the cache writer lease.
"""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
import warnings
from collections import OrderedDict, deque
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from . import render
from .cache import quarantine as quarantine
from .cache import quick_fingerprint as quick_fingerprint
from .contrast import auto_window
from .direct_common import _Lifecycle, _parse_cyx_index, _Root, box_mean
from .direct_common import _TileCache as _TileCache
from .plate_layout import CZI_LIF_PLATE_MAX_DIM as CZI_LIF_PLATE_MAX_DIM
from .plate_layout import CZI_LIF_PLATE_MAX_FRAME_PX as CZI_LIF_PLATE_MAX_FRAME_PX
from .plate_layout import PLATE_MAX_FRAME_PX as PLATE_MAX_FRAME_PX
from .plate_layout import THUMB_K as THUMB_K
from .plate_layout import (
    _build_plate_block,
    _build_plate_notes,
    _build_pyramid_levels,
    _czi_plate_geometry,
    _index_czi_subblocks,
    _lif_plate_sites,
    _lif_series_bounds,
    _nd2_site_layout,
    _nd2_time_metadata,
    _nd2_z_metadata,
)
from .plate_layout import _cluster_1d as _cluster_1d
from .plate_layout import _exposure_ms as _exposure_ms
from .plate_layout import _finite_positive as _finite_positive
from .plate_layout import _lif_plate_series as _lif_plate_series
from .plate_layout import _loop as _loop
from .plate_layout import _named_site_layout as _named_site_layout
from .plate_layout import is_plate_file as is_plate_file
from .plate_layout import site_layout as site_layout
from .plate_store import _FINGERPRINT_KEYS as _FINGERPRINT_KEYS
from .plate_store import _FRAME_CACHE as _FRAME_CACHE
from .plate_store import _INIT_FRAME_CACHE_BYTES as _INIT_FRAME_CACHE_BYTES
from .plate_store import _REDUCED_CACHE as _REDUCED_CACHE
from .plate_store import _WINDOW_FIELDS as _WINDOW_FIELDS
from .plate_store import FRAME_CACHE_BYTES as FRAME_CACHE_BYTES
from .plate_store import FRAME_CACHE_MAX_BYTES as FRAME_CACHE_MAX_BYTES
from .plate_store import FRAME_CACHE_MIN_BYTES as FRAME_CACHE_MIN_BYTES
from .plate_store import LEGACY_PLATE_FORMAT as LEGACY_PLATE_FORMAT
from .plate_store import PLATE_CACHE_BYTES as PLATE_CACHE_BYTES
from .plate_store import PLATE_FORMAT as PLATE_FORMAT
from .plate_store import PLATE_TAG as PLATE_TAG
from .plate_store import READER_REFRESH_S as READER_REFRESH_S
from .plate_store import THUMBS_NAME as THUMBS_NAME
from .plate_store import UNSCORED as UNSCORED
from .plate_store import WINDOWS_K as WINDOWS_K
from .plate_store import WINDOWS_KEY as WINDOWS_KEY
from .plate_store import WINDOWS_METHOD as WINDOWS_METHOD
from .plate_store import WINDOWS_T as WINDOWS_T
from .plate_store import PlateStore as PlateStore
from .plate_store import PlateWriterLock as PlateWriterLock
from .plate_store import _close_plate_group as _close_plate_group
from .plate_store import _default_frame_cache_bytes as _default_frame_cache_bytes
from .plate_store import _effective_frame_cache_budget as _effective_frame_cache_budget
from .plate_store import _empty_focus_map, _empty_plate_status
from .plate_store import _file_identity as _file_identity
from .plate_store import _finite_number as _finite_number
from .plate_store import _has_integrity as _has_integrity
from .plate_store import _plate_group as _plate_group
from .plate_store import _sharpness as _sharpness
from .plate_store import _stored_windows as _stored_windows
from .plate_store import _windows_record as _windows_record
from .plate_store import plate_container as plate_container
from .plate_store import plate_session_lock_path as plate_session_lock_path
from .platform_io import BINARY, read_at
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

PLATE_K = (2, 4, 8, 16)  # box mean factors the grid may ask for
PREFETCH_PENDING_MAX = 256  # frames queued for warming, oldest dropped first
Z_PREFETCH_PENDING_MAX = 2  # at most (z - 1, z + 1) pending for the latest epoch only
HISTOGRAM_LRU = 64


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

    def __init__(self, path: str | Path, tile: int = 512, store: bool = True, *, cache_path: str | Path | None = None):
        import nd2

        self.path = Path(path)
        self.tile = int(tile)
        self.store: PlateStore | None = None
        # Default display windows: an open that uses the store first looks
        # for those its cache records, else measures them. Once the store
        # opens, its writer records them unless its manifest already does.
        self._windows_lookup = (bool(store), cache_path)
        self._display_windows: list[dict[str, float]] | None = None
        # Process-wide RAM caches must never alias two opens of the same path.
        # This is deliberately not the server-visible source generation.
        self._open_generation = uuid.uuid4().hex
        self._owner = (str(self.path.resolve()), self._open_generation)
        self._life = _Lifecycle()
        self._closed = False
        self._close_event = threading.Event()
        self._teardown_complete = False
        self._close_lock = threading.Lock()
        self._close_retry_lock = threading.Lock()
        self._close_retry: threading.Thread | None = None
        self._hist_lock = threading.Lock()
        self._hist: OrderedDict[tuple[int, int, int], list[dict]] = OrderedDict()
        self._pf_lock = threading.Lock()
        self._pf_cv = threading.Condition(self._pf_lock)
        self._pf_queue: deque[tuple[int, int, int, int]] = deque()
        self._pf_set: set[tuple[int, int, int, int]] = set()
        self._pf_thread: threading.Thread | None = None
        self._inflight_lock = threading.Lock()
        self._inflight_frames: dict[tuple[int, int, int], threading.Event] = {}
        self._bg_tls = threading.local()
        self._prefetch_epoch = 0
        self._zpf_lock = threading.Lock()
        self._zpf_executor: ThreadPoolExecutor | None = None
        self._zpf_futures: list[Future[None]] = []
        self._zpf_pending_targets: list[tuple[int, int, int]] = []
        self._zpf_thread: threading.Thread | None = None
        self._last_prefetch_tpz: tuple[int, int, int] | None = None
        self._zpf_dir = 1
        self._fd = -1
        self._fg_lock = threading.Lock()
        self._fg = 0  # foreground reads in flight; the prefetch worker yields to them
        self._f: Any = None
        suffix = self.path.suffix.lower()
        self._backend = "czi" if suffix == ".czi" else "lif" if suffix == ".lif" else "nd2"
        self._source_file = open(self.path, "rb")
        try:
            if self._backend == "nd2":
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self._f = nd2.ND2File(self._source_file)
            # The Windows positional-read fallback owns its file position;
            # dup would share that position with nd2's buffered source reader.
            self._fd = (
                os.open(self.path, os.O_RDONLY | BINARY)
                if os.name == "nt"
                else os.dup(self._source_file.fileno())
            )
            opened = os.fstat(self._fd)
            after = os.stat(self.path)
            if (
                _file_identity(opened) != _file_identity(after)
                or _file_identity(opened) != _file_identity(os.fstat(self._source_file.fileno()))
            ):
                raise ValueError(
                    "the source file changed while it was opening; retry the open"
                )
            if self._backend == "czi":
                self._init_from_czi()
            elif self._backend == "lif":
                self._init_from_lif()
            else:
                self._init_from_file(self._f)
            self._check_source()
            if store:
                opened_store = None
                try:
                    opened_store = (PlateStore.open_or_create(self) if cache_path is None
                                    else PlateStore.open_or_create(self, cache_path=cache_path))
                    self.store = opened_store
                    # A cache rebuilt by open_or_create has lost the record
                    # the windows may have been read from; write it again.
                    if (
                        self._display_windows is not None
                        and _stored_windows(opened_store.manifest, self) is None
                    ):
                        opened_store.keep_display_windows(self._display_windows)
                    opened_store.start()
                except Exception as e:  # a store that will not open is only a slower view
                    if opened_store is not None:
                        opened_store.close()
                    self.store = None
                    if cache_path is not None:
                        raise
                    self.attrs["nd2wsi"].setdefault("notes", []).append(
                        f"thumbnail store unavailable ({type(e).__name__}); frames are read each time"
                    )
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            if self._f is not None:
                self._f.close()
            self._source_file.close()
            raise

    # ---- metadata --------------------------------------------------------
    def _init_from_file(self, f: Any) -> None:
        self.sizes = dict(f.sizes)
        self._coord_axes = [a for a in self.sizes if a not in FRAME_AXES]
        self.T = int(self.sizes.get("T", 1))
        self.P = int(self.sizes.get("P", 1))
        self.Z = int(self.sizes.get("Z", 1))
        st = os.fstat(self._fd)
        self._ident = _file_identity(st)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw0 = f.read_frame(0)
        self._raw_shape = tuple(int(v) for v in raw0.shape)
        # the bytes on disk run (H, W, C, components); read_frame reshapes
        # to that and then transposes, so the fast read below must do the
        # same or a multi-channel frame comes out interleaved
        try:
            self._disk_shape: tuple[int, ...] | None = tuple(
                int(v) for v in f._raw_frame_shape
            )
        except Exception:
            self._disk_shape = None
        cyx, rgb = _frame_to_cyx(raw0, self.sizes)
        self.frame_shape = tuple(int(v) for v in cyx.shape)
        self.dtype = np.dtype(cyx.dtype)
        self.rgb = bool(rgb)
        self.channels = _channel_infos(f, self.frame_shape[0], self.rgb)
        self.pixel_size_um, self.calibration_source = _nd2_pixel_size(f)
        self.magnification = objective_magnification(f)

        # sites from the XYPosLoop
        self.sites, self.rows, self.cols = _nd2_site_layout(
            f, self.P, layout_fn=site_layout
        )

        # z from the ZStackLoop
        self.z_home, self.z_step_um, self.bottom_to_top = _nd2_z_metadata(f, self.Z)

        # time from the TimeLoop and the per frame metadata
        self.period_ms, self.times_ms, self.exposure_ms = _nd2_time_metadata(
            f, self.T, self.seq
        )

        _, H, W = self.frame_shape
        shapes = level_shapes(H, W, self.tile)
        self.levels = _build_pyramid_levels(shapes)
        self.attrs = self._build_attrs(shapes)

    def _init_from_czi(self) -> None:
        from .czi import (
            _extract_czi_metadata,
            _extract_czi_z_step_um,
            _level0_entries,
            _read_czi_header_and_index,
            _resolve_czi_entries_spec,
        )

        st = os.fstat(self._fd)
        self._ident = _file_identity(st)
        entries, xml_root = _read_czi_header_and_index(self._fd, st.st_size)
        lvl0 = _level0_entries(entries)
        s_values = sorted({e.s for e in lvl0})
        s0_entries = [e for e in lvl0 if e.s == s_values[0]] if s_values else lvl0

        x0, y0, width, height = _czi_plate_geometry(s0_entries)

        dtype, rgb, channel_dtypes = _resolve_czi_entries_spec(s0_entries)
        samples = 3 if rgb else 1
        c_values = sorted({e.c for e in s0_entries})
        z_values = sorted({e.z for e in s0_entries})
        t_values = sorted({e.t for e in s0_entries})
        n_c = 3 if rgb else len(c_values)

        self.T = max(1, len(t_values))
        self.P = 1
        self.Z = max(1, len(z_values))
        self.sizes = {"T": self.T, "P": self.P, "Z": self.Z, "C": n_c, "Y": height, "X": width}
        self._coord_axes = ["T", "P", "Z"]
        self.frame_shape = (n_c, height, width)
        self.dtype = np.dtype(dtype)
        self.rgb = bool(rgb)
        self._channel_dtypes = channel_dtypes

        self._czi_origin = (x0, y0)
        self._czi_samples = samples
        self._czi_c_values = c_values
        self._czi_z_values = z_values
        self._czi_t_values = t_values
        self._czi_by_tz, self._czi_by_tzc, self._requires_manifest_channel_dtypes = (
            _index_czi_subblocks(
                s0_entries, x0, y0, width, height, t_values, z_values, rgb=self.rgb
            )
        )

        pixel_size_um, cal_source, magnification, channels = _extract_czi_metadata(
            xml_root, n_c, self.rgb, c_values=c_values
        )
        self.channels = channels
        self.pixel_size_um = pixel_size_um
        self.calibration_source = cal_source
        self.magnification = magnification
        self.sites = [{"i": 0, "name": "Site 1", "row": 0, "col": 0, "stageUm": None}]
        self.rows = 1
        self.cols = 1
        self.z_home = self.Z // 2
        self.z_step_um = _extract_czi_z_step_um(xml_root)
        self.bottom_to_top = True if self.z_step_um is not None else None
        self.period_ms = None
        # Frame indices are not acquisition times. Until timing metadata is
        # decoded, leave it explicitly unknown rather than inventing 1 ms.
        self.times_ms = []
        self.exposure_ms = None

        _, H, W = self.frame_shape
        shapes = level_shapes(H, W, self.tile)
        self.levels = _build_pyramid_levels(shapes)
        self.attrs = self._build_attrs(shapes)

    def _init_from_lif(self) -> None:
        from .lif import _list_lif_series, _read_lif_header_and_blocks
        from .reader import ChannelInfo

        st = os.fstat(self._fd)
        self._ident = _file_identity(st)
        root, blocks = _read_lif_header_and_blocks(self._fd, st.st_size)
        all_series = _list_lif_series(root, blocks)
        s0 = all_series[0]
        selected = _lif_plate_series(all_series)
        if not selected:
            raise ValueError(
                "LIF series have different geometry, calibration or channels; "
                "open the original LIF or an individually converted series instead"
            )
        self._lif_series = selected

        width, height = _lif_series_bounds(s0)
        n_c = len(s0.channels)

        self.T = max(1, int(s0.n_t))
        self.P = len(selected)
        self.Z = max(1, int(s0.n_z))
        self.sizes = {"T": self.T, "P": self.P, "Z": self.Z, "C": n_c, "Y": height, "X": width}
        self._coord_axes = ["T", "P", "Z"]
        self.frame_shape = (n_c, height, width)
        self.dtype = np.dtype(s0.dtype)
        self.rgb = bool(s0.rgb)
        self._channel_dtypes = [str(ch.dtype) for ch in s0.channels]
        self.channels = [ChannelInfo(ch.name, ch.color) for ch in s0.channels]
        self.pixel_size_um = s0.pixel_size_um
        self.calibration_source = s0.calibration_source
        self.magnification = s0.magnification
        self.sites, self.rows, self.cols = _lif_plate_sites(selected)
        self.z_home = self.Z // 2
        self.z_step_um = s0.z_step_um
        self.bottom_to_top = True if self.z_step_um is not None else None
        self.period_ms = None
        self.times_ms = []
        self.exposure_ms = None

        _, H, W = self.frame_shape
        shapes = level_shapes(H, W, self.tile)
        self.levels = _build_pyramid_levels(shapes)
        self.attrs = self._build_attrs(shapes)

    def _build_attrs(self, shapes: list[tuple[int, int]]) -> dict[str, Any]:
        from .convert import _background_mode_start, build_group_attrs

        notes = _build_plate_notes(
            n_t=self.T,
            n_p=self.P,
            n_z=self.Z,
            period_ms=self.period_ms,
            z_step_um=self.z_step_um,
            pixel_size_um=self.pixel_size_um,
        )
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
        meta["plate"] = _build_plate_block(
            backend=self._backend,
            n_t=self.T,
            n_p=self.P,
            n_z=self.Z,
            sites=self.sites,
            rows=self.rows,
            cols=self.cols,
            z_home=self.z_home,
            z_step_um=self.z_step_um,
            bottom_to_top=self.bottom_to_top,
            times_ms=self.times_ms,
            period_ms=self.period_ms,
            exposure_ms=self.exposure_ms,
            frame_shape=self.frame_shape,
        )
        return attrs

    def _windows(self, background_mode_start: Any) -> list[dict[str, float]]:
        """Default display windows: those a matching cache already records
        for this source, else measured now. Either is kept for the store's
        writer."""
        use_store, cache_path = self._windows_lookup
        if use_store:
            recorded = PlateStore.recorded_windows(self, cache_path=cache_path)
            if recorded is not None:
                self._display_windows = recorded
                return recorded
        windows = self._measure_windows()
        self._display_windows = windows
        return windows

    def _measure_windows(self) -> list[dict[str, float]]:
        """Display windows from the 4x reductions of every site at t 0, home z.

        A camera field of a plate is phase contrast or bright field as
        often as fluorescence, and its histogram peak is the sample, not a
        black background, so the black point is a low percentile rather
        than the background mode the stitched scans use.
        """
        samples = [self.reduced(WINDOWS_T, p, self.z_home, WINDOWS_K) for p in range(self.P)]
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
                if np.issubdtype(self.dtype, np.integer):
                    start, end = (
                        (0.0, hi_all) if self.dtype != np.dtype(np.uint8) else (0.0, 255.0)
                    )
                else:
                    start, end = (lo_all, hi_all if hi_all > lo_all else lo_all + 1.0)
            else:
                start, end = auto_window(small[ci], dtype=self.dtype, background_mode=False)
            windows.append({"start": start, "end": end, "min": lo_all, "max": hi_all})
        return windows

    # ---- frames ----------------------------------------------------------
    def seq(self, t: int, p: int, z: int) -> int:
        return _seq_index(self.sizes, self._coord_axes, {"T": t, "P": p, "Z": z})

    def _check_source(self) -> None:
        if self._closed:
            raise ValueError("slide is closed")
        try:
            st = os.stat(self.path)
        except OSError as e:
            raise ValueError(
                f"the source file is unreachable ({e.strerror}); close and reopen the slide"
            ) from e
        if _file_identity(st) != self._ident:
            raise ValueError(
                "the source file changed while it was open; close and reopen the slide"
            )

    class _LeasedFrame:
        """Slice-safe view onto one ND2 plate frame guarded by ``PlateSource._life``.

        Unlike returning a bare ``ndarray(buffer=mmap)`` after releasing ``_life``,
        every slice read acquires ``_life`` and copies only the requested region,
        preventing use-after-unmap ``SIGSEGV`` if the plate closes concurrently.
        """

        def __init__(self, source: PlateSource, seq: int):
            self._source = source
            self._seq = seq
            self.shape = source.frame_shape
            self.dtype = np.dtype(source.dtype)
            self.ndim = len(self.shape)

        def __getitem__(self, index):
            with self._source._life:
                self._source._check_source()
                view, _ = _frame_to_cyx(self._source._f.read_frame(self._seq), self._source.sizes)
                return np.array(view[index], copy=True)

        def __array__(self, dtype=None, copy=None):
            arr = self[slice(None)]
            return np.asarray(arr, dtype=dtype) if dtype is not None else arr

    def frame_view(self, t: int, p: int, z: int):
        """(C, H, W) lease-guarded view onto one plate frame. Valid until close."""
        if self._backend in ("czi", "lif"):
            return self.frame(t, p, z)
        with self._life:
            self._check_source()
            seq = self.seq(t, p, z)
        return self._LeasedFrame(self, seq)

    def _read_czi_frame(self, t: int, p: int, z: int) -> np.ndarray:
        from .czi import _read_subblock_window

        if not (0 <= t < self.T and 0 <= p < self.P and 0 <= z < self.Z):
            raise ValueError(f"frame ({t}, {p}, {z}) out of range")
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
                sx0 = sb.x_start - x0
                sy0 = sb.y_start - y0
                patch = _read_subblock_window(
                    self._fd, sb, self.dtype, self._czi_samples, 0, sb.y_size, 0, sb.x_size
                )
                out[:, sy0 : sy0 + sb.y_size, sx0 : sx0 + sb.x_size] = np.moveaxis(patch, -1, 0)
            return out
        out = np.zeros((n_c, height, width), dtype=self.dtype)
        for ci, c_val in enumerate(self._czi_c_values):
            for sb in self._czi_by_tzc.get((t_val, z_val, c_val), ()):
                sx0 = sb.x_start - x0
                sy0 = sb.y_start - y0
                patch = _read_subblock_window(
                    self._fd, sb, self.dtype, 1, 0, sb.y_size, 0, sb.x_size
                )
                out[ci, sy0 : sy0 + sb.y_size, sx0 : sx0 + sb.x_size] = patch
        return out

    def _read_lif_frame(self, t: int, p: int, z: int) -> np.ndarray:
        from .lif import _read_lif_channel_window

        if not (0 <= t < self.T and 0 <= p < self.P and 0 <= z < self.Z):
            raise ValueError(f"frame ({t}, {p}, {z}) out of range")
        series = self._lif_series[p]
        tile_coords = (
            series.tile_positions_px
            if (series.tile_positions_px and series.n_m > 1)
            else [(0, 0)]
        )
        out = np.zeros(self.frame_shape, dtype=self.dtype)
        for ci, ch_spec in enumerate(series.channels):
            for m_idx, (tx0, ty0) in enumerate(tile_coords):
                patch = _read_lif_channel_window(
                    self._fd, series, ch_spec, z, t, m_idx, 0, series.height, 0, series.width
                )
                out[ci, ty0 : ty0 + series.height, tx0 : tx0 + series.width] = patch
        return out

    def _read_raw(self, seq: int) -> np.ndarray:
        """The frame's pixels as an array that owns its memory.

        One positional read of the frame's bytes: a whole frame arrives in one
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
        if offset is not None and strides is None and self._fd >= 0 and self._disk_shape:
            buf = read_at(self._fd, nbytes, int(offset))
            if len(buf) == nbytes:
                flat = np.frombuffer(buf, dtype=self.dtype)
                arr = flat.reshape(self._disk_shape).transpose((2, 0, 1, 3)).squeeze()
                if arr.shape == self._raw_shape:
                    # already contiguous with one channel, copied only for
                    # the interleaved case, which is the one that reorders
                    return np.ascontiguousarray(arr)
        with self._life:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return np.array(self._f.read_frame(seq), copy=True)

    def frame(self, t: int, p: int, z: int) -> np.ndarray:
        """(C, H, W) frame that owns its memory, cached a few at a time."""
        if self._closed:
            raise ValueError("slide is closed")
        t_i, p_i, z_i = int(t), int(p), int(z)
        tpz = (t_i, p_i, z_i)
        key = (self._owner, "frame", t_i, p_i, z_i)
        with self._life:
            self._check_source()
            hit = _FRAME_CACHE.get(key)
            if hit is not None:
                return hit
            with self._inflight_lock:
                hit = _FRAME_CACHE.get(key)
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
                hit = _FRAME_CACHE.get(key)
                if hit is not None:
                    return hit
            fg_delta = 0 if getattr(self._bg_tls, "active", False) else 1
            try:
                if fg_delta:
                    with self._fg_lock:
                        self._fg += fg_delta
                try:
                    if self._backend == "czi":
                        cyx = self._read_czi_frame(t_i, p_i, z_i)
                    elif self._backend == "lif":
                        cyx = self._read_lif_frame(t_i, p_i, z_i)
                    else:
                        seq = self.seq(t_i, p_i, z_i)
                        raw = self._read_raw(seq)
                        cyx, _ = _frame_to_cyx(raw, self.sizes)
                finally:
                    if fg_delta:
                        with self._fg_lock:
                            self._fg -= fg_delta
                cyx = np.ascontiguousarray(cyx)
                if not self._closed:
                    _FRAME_CACHE.put(key, cyx)
                return cyx
            finally:
                if leader:
                    with self._inflight_lock:
                        self._inflight_frames.pop(tpz, None)
                    waiter.set()

    def reduced(self, t: int, p: int, z: int, k: int, *, persist: bool = True) -> np.ndarray:
        """(C, H//k, W//k) box mean of one frame, rounded to the source dtype.

        An 8x reduction is also stored through :meth:`PlateStore.offer`:
        on its own, or with the chunk the builder is staging. The builder
        itself passes ``persist=False`` and commits a whole chunk of sites
        at once with :meth:`PlateStore.put_block`.
        """
        if self._closed:
            raise ValueError("slide is closed")
        with self._life:
            self._check_source()
            k = int(k)
            if k not in PLATE_K:
                raise ValueError(f"k must be one of {PLATE_K}")
            t, p, z = int(t), int(p), int(z)
            key = (self._owner, t, p, z, k)
            hit = _REDUCED_CACHE.get(key)
            if hit is not None:
                if (
                    persist
                    and k == THUMB_K
                    and self.store is not None
                    and not self.store.is_committed(t, p, z)
                    and not self._closed
                ):
                    self.store.offer(t, p, z, hit)
                return hit
            if k == THUMB_K and self.store is not None:
                stored = self.store.get(t, p, z)
                if stored is not None:
                    if not self._closed:
                        _REDUCED_CACHE.put(key, stored)
                    return stored
            out = np.ascontiguousarray(box_mean(self.frame(t, p, z), k, self.dtype))
            if not self._closed:
                _REDUCED_CACHE.put(key, out)
                if persist and k == THUMB_K and self.store is not None:
                    self.store.offer(t, p, z, out)
            return out

    def status(self) -> dict[str, Any]:
        """How much of the thumbnail store is filled, for the time line."""
        if self.store is None:
            return _empty_plate_status(self.T, self.P, self.Z)
        return self.store.status()

    def focus_map(self) -> dict[str, Any]:
        """Focus progress and provisional best plane per time point and site."""
        # Autofocus scores are derived from source pixels. A same-path source
        # replacement invalidates them just as it invalidates RAM histograms.
        with self._life:
            self._check_source()
            if self.store is None:
                return _empty_focus_map(self.T, self.P, self.Z, self.z_home)
            return self.store.focus_map()

    def root_for(self, t: int, p: int, z: int) -> _Root:
        """A virtual pyramid root for one frame, level 0 from the frame cache."""
        levels: dict[str, Any] = {"0": _ArrayLevel(self.frame(t, p, z))}
        for lv in self.levels[1:]:
            levels[lv["path"]] = _ReducedLevel(self, t, p, z, int(lv["downsample"]))
        return _Root(levels, closer=None)

    def histogram(self, t: int, p: int, z: int) -> list[dict]:
        key = (int(t), int(p), int(z))
        # A histogram is derived pixel data just like a frame. Validate the
        # still-open source before serving even a RAM hit so a same-path file
        # replacement cannot leak a result from the previous generation.
        with self._life:
            self._check_source()
            with self._hist_lock:
                hit = self._hist.get(key)
                if hit is not None:
                    self._hist.move_to_end(key)
                    return hit
        # the stored 8x reduction has 65 thousand samples per site, plenty
        # for a 256 bin display histogram, and it never touches the ND2
        # once the store holds it
        small = self.reduced(t, p, z, THUMB_K)
        attrs = dict(self.attrs)
        attrs["nd2wsi"] = {
            **self.attrs["nd2wsi"],
            "levels": [{"path": "0", "width": small.shape[2], "height": small.shape[1], "downsample": THUMB_K}],
        }
        hist = render.compute_histograms({"0": _ArrayLevel(small)}, attrs, min_pixels=1)
        for h in hist:
            h["level"] = str(THUMB_K)
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
        col: str | Sequence[str | None] | None = None,
    ) -> bytes:
        with self._fg_lock:
            self._fg += 1
        try:
            region = self.reduced(t, p, z, k)
        finally:
            with self._fg_lock:
                self._fg -= 1
        windows, colors = render.display_params(self.attrs, col=col)
        windows, gammas = render.parse_windows(win, windows)
        img = render.composite(region, channels, windows, colors, self.rgb, gammas)
        return render.encode_image(img, fmt)

    # ---- prefetch --------------------------------------------------------
    def prefetch_neighbors(self, t: int, p: int, z: int) -> None:
        """Asynchronously warm neighboring full Z-planes ``(z - 1, z + 1)`` into
        ``_FRAME_CACHE`` for the latest epoch only, cancelling stale intermediate
        scrub epochs before they touch disk/decoders."""
        try:
            if self._closed or self.Z <= 1:
                return
            t_i, p_i, z_i = int(t), int(p), int(z)
            if not (0 <= t_i < self.T and 0 <= p_i < self.P and 0 <= z_i < self.Z):
                return
            estimated_frame_bytes = int(math.prod(self.frame_shape)) * int(self.dtype.itemsize)
            cache_budget = _effective_frame_cache_budget()
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
                    if _FRAME_CACHE.get((self._owner, "frame", t_i, p_i, z2)) is None
                ]
                if not targets:
                    return
                if self._zpf_executor is None:
                    self._zpf_executor = ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="plate-z-prefetch",
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
        # Short 15 ms coalescing settle window (woken at once on close) so rapid
        # Z-slider scrubbing cancels intermediate epochs with 0 wasted disk reads.
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
        if _FRAME_CACHE.get((self._owner, "frame", t_i, p_i, z_i)) is not None:
            return
        if epoch != self._prefetch_epoch or self._closed:
            return
        self._bg_tls.active = True
        try:
            self.frame(t_i, p_i, z_i)
        except Exception:
            pass
        finally:
            self._bg_tls.active = False

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
    def _close_source(
        self,
        *,
        release_writer: bool,
        timeout: float | None = 300.0,
        reopen_on_timeout: bool = False,
    ) -> tuple[bool, PlateWriterLock | None]:
        with self._close_lock:
            return self._close_source_locked(
                release_writer=release_writer,
                timeout=timeout,
                reopen_on_timeout=reopen_on_timeout,
            )

    def _close_source_locked(
        self,
        *,
        release_writer: bool,
        timeout: float | None,
        reopen_on_timeout: bool,
    ) -> tuple[bool, PlateWriterLock | None]:
        if self._teardown_complete:
            return True, None
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
        if not self._life.close(timeout=timeout):
            # a reader still holds the map after the drain timeout. nd2's
            # frames are views onto the mmap and raise no BufferError on
            # close, so unmapping or releasing the writer now could corrupt
            # data. A failed trash operation keeps the registered source and
            # therefore restores admission. Ordinary close stays barred and
            # lets a daemon finish teardown as soon as the readers drain.
            if reopen_on_timeout:
                self._closed = False
                self._close_event.clear()
                self._life.reopen()
            else:
                if self._schedule_close_retry():
                    return False, None
                # If the fallback thread itself cannot start, synchronously
                # wait for existing readers. This can delay close, but it
                # guarantees native handles and the writer lease are not lost.
                self._life.close(timeout=None)
            if reopen_on_timeout:
                return False, None

        writer = None
        if self.store is not None:
            try:
                writer = self.store.close(
                    release_writer=release_writer,
                    timeout=timeout,
                )
            except RuntimeError:
                if reopen_on_timeout:
                    self._closed = False
                    self._close_event.clear()
                    self._life.reopen()
                    with self._pf_cv:
                        self._pf_cv.notify_all()
                    raise
                if self._schedule_close_retry():
                    return False, None
                # As above, thread creation failure turns the bounded close
                # into a synchronous safe close instead of leaking the writer.
                writer = self.store.close(
                    release_writer=release_writer,
                    timeout=None,
                )
        prefetch = self._pf_thread
        with self._zpf_lock:
            zpf_exec, self._zpf_executor = self._zpf_executor, None
            for fut in self._zpf_futures:
                fut.cancel()
            self._zpf_futures.clear()
            self._zpf_pending_targets.clear()
        z_prefetch = self._zpf_thread
        try:
            # Once store.close() transfers the writer lease, no ordinary
            # handle-cleanup exception may hide that lease from the caller.
            # Drain every resource best-effort and return the escrowed writer so
            # trash_cache retains exclusion through its rename.
            try:
                if prefetch is not None and prefetch.is_alive() and prefetch is not threading.current_thread():
                    prefetch.join(timeout=5.0)
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                if zpf_exec is not None:
                    zpf_exec.shutdown(
                        wait=(threading.current_thread() is not z_prefetch),
                        cancel_futures=True,
                    )
                if z_prefetch is not None and z_prefetch.is_alive() and z_prefetch is not threading.current_thread():
                    z_prefetch.join(timeout=5.0)
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                if self._f is not None:
                    self._f.close()
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                self._source_file.close()
            except Exception:  # pragma: no cover - defensive
                pass
            if self._fd >= 0:
                raw_fd, self._fd = self._fd, -1
                try:
                    os.close(raw_fd)
                except OSError:  # pragma: no cover - defensive
                    pass
            try:
                _REDUCED_CACHE.purge(self._owner)
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                _FRAME_CACHE.purge(self._owner)
            except Exception:  # pragma: no cover - defensive
                pass
            self._teardown_complete = True
            return True, writer
        except BaseException:
            # KeyboardInterrupt/SystemExit must propagate, but never with an
            # unreturnable writer lease still held.
            if writer is not None:
                try:
                    writer.release()
                except Exception:
                    pass
            raise

    def _schedule_close_retry(self) -> bool:
        with self._close_retry_lock:
            if self._close_retry is not None and self._close_retry.is_alive():
                return True

            def finish_after_drain() -> None:
                try:
                    self._close_source(
                        release_writer=True,
                        timeout=None,
                        reopen_on_timeout=False,
                    )
                finally:
                    with self._close_retry_lock:
                        if self._close_retry is threading.current_thread():
                            self._close_retry = None

            try:
                retry = threading.Thread(
                    target=finish_after_drain,
                    name="plate-close-retry",
                    daemon=True,
                )
                self._close_retry = retry
                retry.start()
            except Exception:
                self._close_retry = None
                return False
            return True

    def close_for_trash(self, timeout: float = 30.0) -> PlateWriterLock | None:
        """Synchronously drain reads and transfer this source's writer lease."""
        drained, writer = self._close_source(
            release_writer=False,
            timeout=timeout,
            reopen_on_timeout=True,
        )
        if not drained:
            raise RuntimeError("timed out waiting for plate reads to finish")
        return writer

    def close(self, timeout: float | None = 300.0) -> None:
        self._close_source(
            release_writer=True,
            timeout=timeout,
            reopen_on_timeout=False,
        )
