"""Rendering: zarr regions -> display RGB, plus native-resolution ROI export.

Tiles are composited server-side (additive blending of windowed channels with
their assigned colors), so the browser only ever handles ordinary JPEGs.
ROI export comes in two flavors:

* ``tiff``: raw pixel values at the requested pyramid level, dtype preserved,
  written as a *tiled* TIFF from a generator so arbitrarily large regions
  stream to disk without being assembled in RAM;
* ``png`` / ``jpg``: the same server-side rendering as the viewer, size-capped.
"""

from __future__ import annotations

import math
import os
import re
import struct
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .contrast import auto_window
from .direct_common import LeaseClosed
from .platform_io import total_physical_memory_bytes
from .render_export import _OME_DTYPE_NAMES as _OME_DTYPE_NAMES
from .render_export import _OME_SCHEMA as _OME_SCHEMA
from .render_export import _XML_ILLEGAL_RE as _XML_ILLEGAL_RE
from .render_export import RENDER_STRIP_ROWS as RENDER_STRIP_ROWS
from .render_export import _available_memory as _available_memory
from .render_export import _build_roi_ome_xml as _build_roi_ome_xml
from .render_export import _clean_xml_text as _clean_xml_text
from .render_export import _read_channel_region as _read_channel_region
from .render_export import _rgb_to_ome_color_int as _rgb_to_ome_color_int
from .render_export import _roi_rgb_tile_iter as _roi_rgb_tile_iter
from .render_export import _roi_tile_iter as _roi_tile_iter
from .render_export import encode_image as encode_image
from .render_export import export_roi_rendered as export_roi_rendered
from .render_export import export_roi_tiff as export_roi_tiff

_HEX6_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


def parse_channels(param: str | None, n: int) -> list[int]:
    # Explicit all-hidden handoffs must not be mistaken for omitted defaults.
    if param == "none":
        return []
    if not param:
        return list(range(n))
    out: list[int] = []
    seen: set[int] = set()
    for tok in param.split(","):
        tok = tok.strip()
        if tok == "":
            continue
        i = int(tok)
        if 0 <= i < n and i not in seen:
            seen.add(i)
            out.append(i)
            if len(out) >= n:
                break
    return out or list(range(n))


def parse_windows(
    param: str | None,
    defaults: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], list[float]]:
    """Per-channel LUT overrides from a ``win=lo:hi[:gamma],...`` query param.

    One comma-separated slot per channel index; an empty slot keeps the
    store's default window.  Gamma defaults to 1 (linear).  Returns
    ``(windows, gammas)`` aligned with ``defaults``.
    """
    windows = list(defaults)
    gammas = [1.0] * len(defaults)
    if not param:
        return windows, gammas
    for i, tok in enumerate(param.split(",")):
        tok = tok.strip()
        if i >= len(defaults) or not tok:
            continue
        parts = tok.split(":")
        try:
            lo, hi = float(parts[0]), float(parts[1])
            g = float(parts[2]) if len(parts) > 2 else 1.0
        except (IndexError, ValueError):
            continue
        if not (math.isfinite(lo) and math.isfinite(hi) and math.isfinite(g)):
            continue
        if abs(lo) >= _FLOAT32_SAFE_BOUND or abs(hi) >= _FLOAT32_SAFE_BOUND:
            continue
        if hi <= lo:
            hi = lo + 1.0
        windows[i] = (lo, hi)
        gammas[i] = min(max(g, 0.1), 10.0)
    return windows, gammas


def _rgb_tuple_from_default(item: tuple[int, int, int] | str) -> tuple[int, int, int]:
    if isinstance(item, str):
        cleaned = item.strip().lstrip("#")
        if _HEX6_RE.fullmatch(cleaned):
            return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))
        return (255, 255, 255)
    return (int(item[0]), int(item[1]), int(item[2]))


def parse_colors(
    param: str | Sequence[str | None] | None,
    defaults: Sequence[tuple[int, int, int] | str],
) -> list[tuple[int, int, int]]:
    """Per-channel RGB overrides from a ``col=00FFFF,FF00FF,,FFFFFF`` param.

    Accepts a comma-separated string of 6-digit hex codes without ``#`` (or a
    sequence of tokens). Empty or invalid tokens keep the metadata default
    color for that channel.
    """
    colors = [_rgb_tuple_from_default(d) for d in defaults]
    if not param:
        return colors
    tokens: Sequence[Any]
    if isinstance(param, str):
        tokens = param.split(",")
    elif isinstance(param, Sequence) and not isinstance(param, (bytes, bytearray)):
        tokens = param
    else:
        return colors
    for i, tok in enumerate(tokens):
        if i >= len(colors) or not isinstance(tok, str):
            continue
        cleaned = tok.strip()
        if not _HEX6_RE.fullmatch(cleaned):
            continue
        colors[i] = (
            int(cleaned[0:2], 16),
            int(cleaned[2:4], 16),
            int(cleaned[4:6], 16),
        )
    return colors


def level_entry(levels: list, level: int) -> dict:
    """The level whose stored path names ``level``.

    Levels are addressed by their path, not their list position: a
    degraded overview starts at path "1", so positional indexing would
    serve the wrong level and 404 the deepest one.
    """
    for lv in levels:
        if lv["path"] == str(level):
            return lv
    raise KeyError(f"level {level} out of range")


def _read_region(root: Any, level_path: str, x: int, y: int, w: int, h: int) -> np.ndarray:
    """(C, h, w) region from one pyramid level, clipped to bounds."""
    arr = root[level_path]
    _, H, W = arr.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((arr.shape[0], 0, 0), dtype=arr.dtype)
    return np.asarray(arr[:, y0:y1, x0:x1])


# Coordinated process-wide raw-chunk cache budget:
# Capped at min(96 MiB, 2% physical RAM) with a 32 MiB floor so decoded Zarr
# tiles + Plate/Stack frame caches (256-640 MiB) + SQLite page caches (<=32 MiB
# per open .nd2svs) never compound into multi-GB RSS spikes across 4-pane Compare.
RAW_CHUNK_CACHE_MIN_BYTES = 32 * 1024 * 1024
RAW_CHUNK_CACHE_MAX_BYTES = 96 * 1024 * 1024


def _default_raw_chunk_cache_bytes(total_ram: int | None = None) -> int:
    if total_ram is None:
        raw_env = os.environ.get("ND2WSI_RAW_CHUNK_CACHE_BYTES")
        if raw_env is not None and raw_env.strip() != "":
            val = int(raw_env)
            if val > 0:
                return val
    ram = int(total_physical_memory_bytes() if total_ram is None else total_ram)
    return max(RAW_CHUNK_CACHE_MIN_BYTES, min(int(ram * 0.02), RAW_CHUNK_CACHE_MAX_BYTES))


RAW_CHUNK_CACHE_BYTES = _default_raw_chunk_cache_bytes()
_STAT_SIG_TTL_S = 0.1
_stat_sig_lock = threading.Lock()
_stat_sig_cache: dict[str, tuple[float, tuple[int, int, int] | None]] = {}


def _coalesced_file_sig(path: str | os.PathLike) -> tuple[int, int, int] | None:
    p_str = str(path)
    now = time.monotonic()
    with _stat_sig_lock:
        cached = _stat_sig_cache.get(p_str)
        if cached is not None and (now - cached[0]) < _STAT_SIG_TTL_S:
            return cached[1]
    try:
        st = os.stat(p_str)
        sig: tuple[int, int, int] | None = (int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))
    except OSError:
        sig = None
    with _stat_sig_lock:
        if len(_stat_sig_cache) > 512:
            _stat_sig_cache.clear()
        _stat_sig_cache[p_str] = (now, sig)
    return sig


class _RawChunkCache:
    """Thread-safe, byte-budgeted LRU cache for decoded multi-channel raw tiles.

    Enforces ``used <= budget`` strictly before every insertion and skips single
    chunks larger than 25% of ``budget`` so oversized multi-channel regions
    cannot flush the working viewport set.
    """

    def __init__(self, budget: int = RAW_CHUNK_CACHE_BYTES):
        self.budget = int(budget)
        self.used = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.skipped = 0
        self._d: OrderedDict[Any, tuple[Any, np.ndarray]] = OrderedDict()
        self._lock = threading.Lock()

    def _sweep_dead_locked(self) -> None:
        dead_keys = [
            k for k, (ref, _arr) in self._d.items() if ref is not None and ref() is None
        ]
        for k in dead_keys:
            self.used -= int(self._d.pop(k)[1].nbytes)

    def get(self, key: Any, owner_obj: Any = None) -> np.ndarray | None:
        with self._lock:
            entry = self._d.get(key)
            if entry is None:
                self.misses += 1
                return None
            ref, arr = entry
            if ref is not None:
                alive = ref()
                if alive is None or (owner_obj is not None and alive is not owner_obj):
                    self.used -= int(arr.nbytes)
                    del self._d[key]
                    self.misses += 1
                    return None
            self._d.move_to_end(key)
            self.hits += 1
            return arr

    def put(self, key: Any, value: np.ndarray, owner_obj: Any = None) -> np.ndarray:
        nbytes = int(value.nbytes)
        if nbytes <= 0:
            return value
        with self._lock:
            max_entry_bytes = self.budget // 4
            if self.budget <= 0 or nbytes > max_entry_bytes:
                self.skipped += 1
                return value
        if not value.flags.owndata:
            value = value.copy()
        value.setflags(write=False)
        nbytes = int(value.nbytes)
        ref = None
        if owner_obj is not None:
            try:
                ref = weakref.ref(owner_obj)
            except TypeError:
                ref = None
        with self._lock:
            max_entry_bytes = self.budget // 4
            if self.budget <= 0 or nbytes > max_entry_bytes:
                self.skipped += 1
                return value
            old = self._d.pop(key, None)
            if old is not None:
                self.used -= int(old[1].nbytes)
            if (self.used + nbytes) > self.budget:
                self._sweep_dead_locked()
            while (self.used + nbytes) > self.budget and self._d:
                _, evicted = self._d.popitem(last=False)
                self.used -= int(evicted[1].nbytes)
                self.evictions += 1
            if (self.used + nbytes) > self.budget:
                self.skipped += 1
                return value
            self._d[key] = (ref, value)
            self.used += nbytes
        return value

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._sweep_dead_locked()
            return {
                "budget": int(self.budget),
                "max_entry_bytes": int(self.budget // 4),
                "used": int(self.used),
                "entries": len(self._d),
                "hits": int(self.hits),
                "misses": int(self.misses),
                "evictions": int(self.evictions),
                "skipped": int(self.skipped),
            }

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self.used = 0
            self.hits = 0
            self.misses = 0
            self.evictions = 0
            self.skipped = 0

    def purge(self, owner: Any) -> None:
        owner_id = id(owner) if not isinstance(owner, (int, str, Path)) else owner
        owner_str = str(owner)
        with self._lock:
            to_drop = [
                k
                for k in self._d
                if (
                    isinstance(k, tuple)
                    and k
                    and (
                        k[0] == owner_id
                        or k[0] == owner_str
                        or (
                            isinstance(k[1], tuple)
                            and len(k[1]) >= 2
                            and (k[1][0] == owner_str or k[1][1] == owner_str)
                        )
                    )
                )
            ]
            for k in to_drop:
                self.used -= int(self._d.pop(k)[1].nbytes)

    invalidate = purge


_RAW_CHUNK_CACHE = _RawChunkCache()


def raw_chunk_cache_stats() -> dict[str, int]:
    """Return deterministic byte-accounting and hit/eviction telemetry for `_RAW_CHUNK_CACHE`."""
    return _RAW_CHUNK_CACHE.stats()


def clear_raw_chunk_cache() -> None:
    """Clear `_RAW_CHUNK_CACHE` and reset its counters."""
    _RAW_CHUNK_CACHE.clear()


def _store_generation_token(arr: Any, x: int, y: int) -> Any:
    from .storage.single_file import SQLiteStore, path_mutation_seq

    inner = getattr(arr, "_array", arr)
    store = getattr(inner, "store", None)
    if isinstance(store, SQLiteStore):
        p_str = str(store.path)
        return ("sqlite", p_str, path_mutation_seq(p_str), _coalesced_file_sig(p_str))
    store_root = getattr(store, "root", None)
    if store_root is not None:
        arr_path = getattr(inner, "path", "") or ""
        chunks = getattr(inner, "chunks", None)
        chunk_sig = None
        if isinstance(chunks, tuple) and len(chunks) == 3 and chunks[1] > 0 and chunks[2] > 0:
            cy, cx = y // int(chunks[1]), x // int(chunks[2])
            for sep in (".", "/"):
                cpath = Path(store_root) / arr_path / f"0{sep}{cy}{sep}{cx}"
                try:
                    st = cpath.stat()
                    chunk_sig = (int(st.st_size), int(st.st_mtime_ns))
                    break
                except OSError:
                    continue
        return ("dir", str(store_root), arr_path, chunk_sig)
    mem_dict = getattr(store, "_store_dict", None)
    if isinstance(mem_dict, dict):
        arr_path = getattr(inner, "path", "") or ""
        chunks = getattr(inner, "chunks", None)
        if isinstance(chunks, tuple) and len(chunks) == 3 and chunks[1] > 0 and chunks[2] > 0:
            cy, cx = y // int(chunks[1]), x // int(chunks[2])
            buf = mem_dict.get(f"{arr_path}/0.{cy}.{cx}") or mem_dict.get(f"{arr_path}/0/{cy}/{cx}")
            return ("mem", id(store), id(buf))
    mutation_token = getattr(arr, "generation", None)
    return ("obj", type(arr).__qualname__, mutation_token)


def _read_cached_tile_region(
    root: Any,
    attrs: dict[str, Any],
    level_path: str,
    tx: int,
    ty: int,
    x: int,
    y: int,
    w: int,
    h: int,
) -> np.ndarray:
    arr = root[level_path]
    life = getattr(arr, "_life", None)
    if life is not None and getattr(life, "closed", False):
        raise LeaseClosed(life)
    for candidate in (arr, getattr(arr, "raw", None)):
        verify = getattr(candidate, "_verify_source", None)
        if callable(verify):
            verify()
    # In-memory ndarrays and PlateSource _ArrayLevel wrappers are already raw RAM
    # buffers: slicing them directly is zero-copy / microsecond-fast and reflects
    # any in-place array edits immediately without duplicating memory.
    if isinstance(arr, np.ndarray) or type(arr).__name__ == "_ArrayLevel":
        return _read_region(root, level_path, x, y, w, h)

    meta = attrs.get("nd2wsi") or {}
    h_corr = meta.get("h_correction")
    h_token = repr(h_corr) if h_corr is not None else None
    attrs_token = (
        meta.get("generation"),
        h_token,
        meta.get("display_dtype"),
        meta.get("kind"),
        meta.get("dtype"),
    )
    store_token = _store_generation_token(arr, x, y)
    key = (id(arr), store_token, attrs_token, str(level_path), int(ty), int(tx), int(w), int(h))
    hit = _RAW_CHUNK_CACHE.get(key, owner_obj=arr)
    if hit is not None:
        return hit
    region = _read_region(root, level_path, x, y, w, h)
    return _RAW_CHUNK_CACHE.put(key, region, owner_obj=arr)


# Gamma lookup tables for 8/16-bit channels. A table holds, for every code the
# dtype can represent, the value the per-pixel path computes for that code
# (_windowed on np.arange of the same dtype, kept in the dtype it produces), so
# indexing it reproduces the per-pixel result bit for bit. Only exact Python
# int/float windows and gammas qualify: NumPy scalars are not "weak" under
# NEP 50 and would change the arithmetic precision.
_GAMMA_LUT_DTYPES = (np.dtype(np.uint8), np.dtype(np.uint16))
_GAMMA_LUT_CAPACITY = 32  # at most 32 x 256 KiB float32 uint16 tables = 8 MiB
_GAMMA_LUT_BUILD_RATIO = 4  # build only when the region has >= codes / 4 pixels
_PY_REALS = (int, float)
# Python windows strictly inside this bound stay finite in float32 working math.
_FLOAT32_SAFE_BOUND = 3e38
_gamma_luts: OrderedDict[tuple, tuple[np.ndarray, bool]] = OrderedDict()
_gamma_lut_lock = threading.Lock()


def _windowed(values: np.ndarray, lo, hi, gamma, floating: bool, working_dtype) -> np.ndarray:
    """Window, clip and gamma one channel plane: the per-pixel display math."""
    span = (hi - lo if hi > lo else 1.0) if floating else max(hi - lo, 1e-6)
    v = (values.astype(working_dtype) - lo) / span
    np.clip(v, 0, 1, out=v)
    if gamma != 1.0:
        v **= 1.0 / gamma
    return v


def _lut_key_part(x: int | float) -> int | bytes:
    # Floats key on their exact bits so -0.0/0.0 and NaN payloads never alias,
    # and an int never shares a table with the equal float.
    return struct.pack("<d", x) if type(x) is float else x


def _gamma_lut(dtype: np.dtype, lo, hi, gamma, build: bool) -> tuple[np.ndarray, bool] | None:
    """Cached ``(table, all_finite)`` for one window, or None when not cached
    and ``build`` is false. Keyed on values, never on channel index."""
    key = (dtype.str, _lut_key_part(lo), _lut_key_part(hi), _lut_key_part(gamma))
    with _gamma_lut_lock:
        entry = _gamma_luts.get(key)
        if entry is not None:
            _gamma_luts.move_to_end(key)
            return entry
    if not build:
        return None
    # Built outside the lock; a racing duplicate build is identical and harmless.
    codes = np.arange(np.iinfo(dtype).max + 1, dtype=dtype)
    table = _windowed(codes, lo, hi, gamma, False, np.float32)
    table.setflags(write=False)
    entry = (table, bool(np.isfinite(table).all()))
    with _gamma_lut_lock:
        _gamma_luts[key] = entry
        _gamma_luts.move_to_end(key)
        while len(_gamma_luts) > _GAMMA_LUT_CAPACITY:
            _gamma_luts.popitem(last=False)
    return entry


def _clear_gamma_lut_cache() -> None:
    with _gamma_lut_lock:
        _gamma_luts.clear()


def _bounded_real(x) -> bool:
    return type(x) in _PY_REALS and -_FLOAT32_SAFE_BOUND < x < _FLOAT32_SAFE_BOUND


def _dense(planes: list[np.ndarray | None], h: int, w: int) -> np.ndarray:
    """Interleave planar accumulators into the (h, w, 3) float32 layout;
    components that never received a contribution stay 0. The planes are
    released from ``planes`` as they are copied so they do not stay alive
    through the final clip/scale/cast."""
    out = np.zeros((h, w, 3), dtype=np.float32)
    for k in range(3):
        plane, planes[k] = planes[k], None
        if plane is not None:
            out[..., k] = plane
        del plane
    return out


def composite(
    region: np.ndarray,
    channels: list[int],
    windows: list[tuple[float, float]],
    colors: list[tuple[int, int, int]],
    rgb: bool,
    gammas: list[float] | None = None,
    col: str | Sequence[str | None] | None = None,
) -> np.ndarray:
    """(C, h, w) raw values -> (h, w, 3) uint8 display image.

    ``gammas`` follows the NIS convention: gamma > 1 brightens midtones
    (``v ** (1/gamma)`` after windowing).
    """
    c, h, w = region.shape
    if h == 0 or w == 0:
        return np.zeros((h, w, 3), dtype=np.uint8)
    if col is not None:
        colors = parse_colors(col, colors)
    gammas = gammas or [1.0] * len(windows)
    floating = np.issubdtype(region.dtype, np.floating)
    # Preserve both sub-micro windows and small float64 differences at large
    # offsets. Integer sources keep the existing float32 compositing path.
    working_dtype = np.float64 if floating else np.float32

    if rgb:
        # RGB components keep their physical R/G/B slots, not fluorescence
        # pseudocolours. Each component has its own display window and gamma;
        # hiding one leaves the other two unchanged. Defaults remain identity
        # for an 8-bit colour acquisition.
        out = np.zeros((h, w, 3), dtype=np.uint8)
        for ci in channels:
            lo, hi = windows[ci]
            span = (hi - lo if hi > lo else 1.0) if floating else max(hi - lo, 1e-6)
            v = (region[ci].astype(working_dtype) - lo) / span
            np.clip(v, 0, 1, out=v)
            if gammas[ci] != 1.0:
                v **= 1.0 / gammas[ci]
            out[..., ci] = (v * 255).astype(np.uint8)
        return out

    # Additive pseudocolour, accumulated per output component in planar (h, w)
    # float32 planes instead of one (h, w, 3) broadcast, then interleaved and
    # finished with the same clip/scale/cast. Each plane receives the same
    # products in the same channel order as the dense accumulator did, so it
    # holds the same floats; the only possible difference is the sign of an
    # exact zero (a plane starts from its first product, not 0.0 + product),
    # which the uint8 cast maps to 0 either way. A zero colour component is
    # skipped only when every windowed value of the channel is finite: v * 0
    # is then +-0 and leaves the sum unchanged, while NaN or inf would add NaN.
    # Only exact ndarrays and exact (h, w) windowed planes take the planar/LUT
    # path. Subclasses (np.ma.MaskedArray restores operand data under its mask
    # inside every ufunc, so a skipped component or a table lookup would
    # differ) and planes with extra unit dimensions (the dense += refuses to
    # broadcast them, a planar setitem would silently drop them) run the dense
    # accumulation, the pre-planar code, unchanged. Every production caller
    # passes an exact ndarray.
    exact = type(region) is np.ndarray
    lut_dtype = exact and region.dtype in _GAMMA_LUT_DTYPES
    lut_build = lut_dtype and h * w * _GAMMA_LUT_BUILD_RATIO >= np.iinfo(region.dtype).max + 1
    integer = region.dtype.kind in "biu"
    planes: list[np.ndarray | None] = [None, None, None]
    dense = None if exact else np.zeros((h, w, 3), dtype=np.float32)
    for ci in channels:
        lo, hi = windows[ci]
        gamma = gammas[ci]
        python_scalars = type(lo) in _PY_REALS and type(hi) in _PY_REALS and type(gamma) in _PY_REALS
        entry = None
        if lut_dtype and python_scalars and gamma != 1.0:
            entry = _gamma_lut(region.dtype, lo, hi, gamma, lut_build)
        if entry is not None:
            table, finite = entry
            v = table[region[ci]]
        else:
            v = _windowed(region[ci], lo, hi, gamma, floating, working_dtype)
            # Integer codes are finite in float32; with no gamma, bounded
            # Python windows keep (code - lo) / span NaN-free and clip bounds
            # it to [0, 1]. Anything else is checked when it matters.
            known = integer and python_scalars and gamma == 1.0
            finite = (known and _bounded_real(lo) and _bounded_real(hi)) or None
        col = np.asarray(colors[ci], dtype=np.float32) / 255.0
        if dense is None and (
            type(v) is not np.ndarray
            or v.shape != (h, w)
            or v.dtype not in (np.float32, np.float64)
            or col.shape != (3,)
        ):
            dense = _dense(planes, h, w)
        if dense is not None:
            # Subclasses, unusual plane or colour shapes and working dtypes:
            # the dense accumulation.
            dense += v[..., None] * col
            continue
        for k in range(3):
            ck = col[k]
            if ck == 0:
                if finite is None:
                    finite = bool(np.isfinite(v).all())
                if finite:
                    continue
            if planes[k] is None:
                planes[k] = (v * ck).astype(np.float32, copy=False)
            else:
                planes[k] += v * ck
    if dense is None:
        dense = _dense(planes, h, w)
    # Same clip/scale/cast as np.clip(dense, 0, 1) * 255 (float32 throughout),
    # done in place so only dense and the uint8 result are alive at the end.
    np.clip(dense, 0, 1, out=dense)
    dense *= 255
    return dense.astype(np.uint8)


def render_tile(
    root: Any,
    attrs: dict[str, Any],
    level: int,
    tx: int,
    ty: int,
    channels: list[int],
    fmt: str = "jpg",
    win: str | None = None,
    col: str | Sequence[str | None] | None = None,
) -> bytes:
    meta = attrs["nd2wsi"]
    tile = int(meta["tile"])
    lv = level_entry(meta["levels"], level)
    lw, lh = lv["width"], lv["height"]
    x, y = tx * tile, ty * tile
    if x >= lw or y >= lh or tx < 0 or ty < 0:
        raise KeyError("tile out of range")
    w, h = min(tile, lw - x), min(tile, lh - y)
    region = _read_cached_tile_region(root, attrs, lv["path"], tx, ty, x, y, w, h)
    windows, colors = display_params(attrs, col=col)
    windows, gammas = parse_windows(win, windows)
    img = composite(region, channels, windows, colors, meta["rgb"], gammas)
    return encode_image(img, fmt)


render_tile_bytes = render_tile


def render_frame_bytes(
    region: np.ndarray,
    attrs: dict[str, Any],
    channels: list[int],
    fmt: str = "jpg",
    win: str | None = None,
    col: str | Sequence[str | None] | None = None,
) -> bytes:
    """Composite and encode a pre-loaded ``(C, H, W)`` frame array to JPEG/PNG."""
    meta = attrs["nd2wsi"]
    windows, colors = display_params(attrs, col=col)
    windows, gammas = parse_windows(win, windows)
    img = composite(region, channels, windows, colors, bool(meta.get("rgb")), gammas)
    return encode_image(img, fmt)


def compute_histograms(
    root: Any, attrs: dict[str, Any], bins: int = 256, min_pixels: int = 300_000
) -> list[dict[str, Any]]:
    """Per-channel intensity histograms for the LUT panel.

    Computed from the smallest pyramid level with at least ``min_pixels``
    (a few-MPx read at most). Integer axes always cover the complete source
    dtype, regardless of the sampled signal or selected display window.
    Floating-point axes use the stored acquisition range, including any finite
    sampled extremes. Auto has a robust display window and a separate fitted histogram. The
    full histogram and native integer peaks remain available for inspection.
    """
    meta = attrs["nd2wsi"]
    levels = meta["levels"]
    pick = levels[0]
    for lv in reversed(levels):
        if lv["width"] * lv["height"] >= min_pixels:
            pick = lv
            break
    arr = root[pick["path"]]
    arr_h, arr_w = int(pick["height"]), int(pick["width"])
    max_hist_pixels = 16_000_000
    if arr_h * arr_w <= max_hist_pixels:
        data = np.asarray(arr[:])  # (C, h, w)
    else:
        # Sample an even grid of contiguous 512x512 patches (compatible with
        # non-strided direct readers) capped to ~4 MPx total.
        patch = 512
        grid_n = max(1, min(4, int(math.sqrt(4_000_000 / (patch * patch)))))
        ys = [int(i * max(0, arr_h - patch) / max(1, grid_n - 1)) for i in range(grid_n)]
        xs = [int(j * max(0, arr_w - patch) / max(1, grid_n - 1)) for j in range(grid_n)]
        patches = []
        for y0 in sorted(set(ys)):
            y1 = min(arr_h, y0 + patch)
            for x0 in sorted(set(xs)):
                x1 = min(arr_w, x0 + patch)
                block = np.asarray(arr[:, y0:y1, x0:x1])
                patches.append(block.reshape(block.shape[0], -1))
        data = np.concatenate(patches, axis=1)  # (C, N)

    def histogram(values, count, low, high):
        if np.issubdtype(values.dtype, np.floating):
            # NumPy cannot construct thousands of distinct edges when a float
            # window is only a few representable steps wide at a large offset,
            # and ``high - low`` can overflow to inf for extreme float64 ranges.
            # Count in normalized coordinates, then map the edges back safely.
            span = high - low
            if math.isfinite(span) and span != 0.0:
                scaled = (values.astype(np.float64) - low) / span
                counts, edges = np.histogram(scaled, bins=count, range=(0.0, 1.0))
                return counts, low + edges * span
            half_low = low * 0.5
            half_high = high * 0.5
            half_span = half_high - half_low
            if not math.isfinite(half_span) or half_span == 0.0:
                return np.zeros(count, dtype=np.int64), np.full(count + 1, low, dtype=np.float64)
            scaled = (values.astype(np.float64) * 0.5 - half_low) / half_span
            counts, edges = np.histogram(scaled, bins=count, range=(0.0, 1.0))
            return counts, low + edges * half_span + edges * half_span
        return np.histogram(values, bins=count, range=(low, high))

    stored_dtype = np.dtype(meta.get("dtype", data.dtype))
    display_dtype = np.dtype(meta["display_dtype"]) if meta.get("display_dtype") else data.dtype
    is_float_display = (
        np.issubdtype(display_dtype, np.floating)
        or np.issubdtype(data.dtype, np.floating)
    )
    dtype = display_dtype if is_float_display else stored_dtype
    rgb_window = auto_window(data, dtype=dtype, rgb=True) if meta.get("rgb") else None
    out = []
    for ci in range(data.shape[0]):
        ch = data[ci].ravel()
        if np.issubdtype(ch.dtype, np.bool_):
            ch = ch.astype(np.uint8)
        if np.issubdtype(ch.dtype, np.floating):
            ch = ch[np.isfinite(ch)]
        dmin = float(ch.min()) if ch.size else 0.0
        dmax = float(ch.max()) if ch.size else 1.0
        win = attrs["omero"]["channels"][ci].get("window", {})
        # Estimate from pixels, independently of coarse bins and cached LUTs.
        # Plate fields can be phase contrast, so retain a percentile black point.
        auto_low, auto_high = rgb_window or auto_window(
            ch, dtype=dtype, background_mode=meta.get("kind") != "plate")
        auto_min = min(dmin, auto_low)
        auto_max = max(auto_high, min(dmax, auto_min + (auto_high - auto_min) * 1.3))
        if np.issubdtype(dtype, np.integer):
            auto_min = max(float(np.iinfo(dtype).min), auto_min)
            auto_max = min(float(np.iinfo(dtype).max), auto_max)
        # Values outside the fitted axis are omitted, not moved into edge bins.
        auto_counts, _ = histogram(ch, bins, auto_min, auto_max)
        if np.issubdtype(dtype, np.integer):
            limits = np.iinfo(dtype)
            vmin, vmax = float(limits.min), float(limits.max)
        elif np.issubdtype(dtype, np.bool_):
            vmin, vmax = 0.0, 1.0
        else:
            bounds = [dmin, dmax]
            if np.issubdtype(stored_dtype, np.integer):
                limits = np.iinfo(stored_dtype)
                bounds.extend((float(limits.min), float(limits.max)))
            bounds.extend(float(win[key]) for key in ("min", "max", "start", "end")
                          if key in win and np.isfinite(float(win[key])))
            vmin, vmax = min(bounds), max(bounds)
            if vmax <= vmin:
                vmax = vmin + max(1.0, abs(vmin)) * np.finfo(float).eps * bins
        counts, _ = histogram(ch, bins, vmin, vmax)
        # Preserve native integer peaks for client-side zooming. Sparse arrays
        # avoid sending 65,536 mostly empty bins for dim uint16 acquisitions.
        if np.issubdtype(ch.dtype, np.integer) and ch.dtype.itemsize <= 2:
            values, detail_counts = np.unique(ch, return_counts=True)
        else:
            detail_counts, edges = histogram(ch, 65536, vmin, vmax)
            occupied = detail_counts > 0
            values = (edges[:-1] + (edges[1:] - edges[:-1]) / 2)[occupied]
            detail_counts = detail_counts[occupied]
        out.append(
            {
                "bins": [int(c) for c in counts],
                "vmin": vmin,
                "vmax": vmax,
                "level": pick["path"],
                "detail": {"values": values.tolist(), "counts": detail_counts.tolist()},
                "autoWindow": {"lo": auto_low, "hi": auto_high},
                "autoHistogram": {"bins": [int(c) for c in auto_counts],
                                  "vmin": auto_min, "vmax": auto_max},
            }
        )
    return out


def display_params(
    attrs: dict[str, Any],
    col: str | Sequence[str | None] | None = None,
) -> tuple[list[tuple[float, float]], list[tuple[int, int, int]]]:
    windows: list[tuple[float, float]] = []
    colors: list[tuple[int, int, int]] = []
    for ch in attrs["omero"]["channels"]:
        win = ch.get("window", {})
        windows.append((float(win.get("start", 0)), float(win.get("end", 255))))
        hexcol = str(ch.get("color", "FFFFFF")).strip().lstrip("#")
        if _HEX6_RE.fullmatch(hexcol):
            colors.append(tuple(int(hexcol[i : i + 2], 16) for i in (0, 2, 4)))
        else:
            colors.append((255, 255, 255))
    if col is not None:
        colors = parse_colors(col, colors)
    return windows, colors
