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

import io
import struct
import threading
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

import numpy as np

from .contrast import auto_window


def parse_channels(param: str | None, n: int) -> list[int]:
    # Explicit all-hidden handoffs must not be mistaken for omitted defaults.
    if param == "none":
        return []
    if not param:
        return list(range(n))
    out = []
    for tok in param.split(","):
        tok = tok.strip()
        if tok == "":
            continue
        i = int(tok)
        if 0 <= i < n:
            out.append(i)
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
        if hi <= lo:
            hi = lo + 1.0
        windows[i] = (lo, hi)
        gammas[i] = min(max(g, 0.1), 10.0)
    return windows, gammas


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
) -> np.ndarray:
    """(C, h, w) raw values -> (h, w, 3) uint8 display image.

    ``gammas`` follows the NIS convention: gamma > 1 brightens midtones
    (``v ** (1/gamma)`` after windowing).
    """
    c, h, w = region.shape
    if h == 0 or w == 0:
        return np.zeros((h, w, 3), dtype=np.uint8)
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


def encode_image(img: np.ndarray, fmt: str, quality: int = 87) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    pil = Image.fromarray(img)
    if fmt in ("jpg", "jpeg"):
        pil.save(buf, format="JPEG", quality=quality)
    elif fmt == "png":
        pil.save(buf, format="PNG")
    else:
        raise ValueError(f"unsupported image format {fmt}")
    return buf.getvalue()


def render_tile(
    root: Any,
    attrs: dict[str, Any],
    level: int,
    tx: int,
    ty: int,
    channels: list[int],
    fmt: str = "jpg",
    win: str | None = None,
) -> bytes:
    meta = attrs["nd2wsi"]
    tile = int(meta["tile"])
    lv = level_entry(meta["levels"], level)
    lw, lh = lv["width"], lv["height"]
    x, y = tx * tile, ty * tile
    if x >= lw or y >= lh or tx < 0 or ty < 0:
        raise KeyError("tile out of range")
    w, h = min(tile, lw - x), min(tile, lh - y)
    region = _read_region(root, lv["path"], x, y, w, h)
    windows, colors = display_params(attrs)
    windows, gammas = parse_windows(win, windows)
    img = composite(region, channels, windows, colors, meta["rgb"], gammas)
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
    data = np.asarray(root[pick["path"]][:])  # (C, h, w)

    def histogram(values, count, low, high):
        if np.issubdtype(values.dtype, np.floating):
            # NumPy cannot construct thousands of distinct edges when a float
            # window is only a few representable steps wide at a large offset.
            # Count in normalized coordinates, then map the edges back.
            scaled = (values.astype(np.float64) - low) / (high - low)
            counts, edges = np.histogram(scaled, bins=count, range=(0.0, 1.0))
            return counts, low + edges * (high - low)
        return np.histogram(values, bins=count, range=(low, high))

    dtype = np.dtype(meta.get("dtype", data.dtype))
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
) -> tuple[list[tuple[float, float]], list[tuple[int, int, int]]]:
    windows: list[tuple[float, float]] = []
    colors: list[tuple[int, int, int]] = []
    for ch in attrs["omero"]["channels"]:
        win = ch.get("window", {})
        windows.append((float(win.get("start", 0)), float(win.get("end", 255))))
        hexcol = ch.get("color", "FFFFFF")
        colors.append(tuple(int(hexcol[i : i + 2], 16) for i in (0, 2, 4)))
    return windows, colors


# --------------------------------------------------------------------------
# ROI export
# --------------------------------------------------------------------------


def _roi_tile_iter(
    root: Any,
    level_path: str,
    x: int,
    y: int,
    w: int,
    h: int,
    channels: list[int],
    tile: int,
) -> Iterator[np.ndarray]:
    """Yield (tile, tile[, ...]) blocks of the ROI in tifffile's expected order.

    For multi-channel output (separate planes) tifffile expects all tiles of
    plane 0 row-major, then plane 1, ...; for a single plane just row-major.
    """
    for ci in channels:
        for ty0 in range(0, h, tile):
            th = min(tile, h - ty0)
            for tx0 in range(0, w, tile):
                tw = min(tile, w - tx0)
                block = _read_region(root, level_path, x + tx0, y + ty0, tw, th)[ci]
                if th < tile or tw < tile:
                    pad = np.zeros((tile, tile), dtype=block.dtype)
                    pad[:th, :tw] = block
                    block = pad
                yield block


def _roi_rgb_tile_iter(
    root: Any, level_path: str, x: int, y: int, w: int, h: int, tile: int
) -> Iterator[np.ndarray]:
    for ty0 in range(0, h, tile):
        th = min(tile, h - ty0)
        for tx0 in range(0, w, tile):
            tw = min(tile, w - tx0)
            block = _read_region(root, level_path, x + tx0, y + ty0, tw, th)
            block = np.moveaxis(block, 0, -1)  # (h, w, 3)
            if th < tile or tw < tile:
                pad = np.zeros((tile, tile, block.shape[-1]), dtype=block.dtype)
                pad[:th, :tw] = block
                block = pad
            yield block


def export_roi_tiff(
    root: Any,
    attrs: dict[str, Any],
    out_file: Any,
    level: int,
    x: int,
    y: int,
    w: int,
    h: int,
    channels: list[int],
    on_progress=None,
) -> None:
    """Stream a raw-dtype tiled TIFF of the ROI to a file object or path."""
    import tifffile

    meta = attrs["nd2wsi"]
    # TIFF ImageDescription is ASCII, unlike the file path and HTTP download
    # name. Keep non-ASCII source names recoverable without rejecting exports.
    source_description = str(meta["source"]).encode("ascii", "backslashreplace").decode("ascii")
    lv = level_entry(meta["levels"], level)
    level_path = lv["path"]
    tile = min(int(meta["tile"]), 512)
    dtype = np.dtype(meta["dtype"])
    factor = lv["downsample"]
    # pixels per centimeter at this level; an uncalibrated store gets no
    # resolution tags at all rather than tags built from an invented value
    if meta.get("pixel_size_um"):
        py, px = meta["pixel_size_um"]
        res_kw = {
            "resolution": (1e4 / (px * factor), 1e4 / (py * factor)),
            "resolutionunit": "CENTIMETER",
        }
    else:
        res_kw = {}

    def counted(it: Iterator[np.ndarray], total: int) -> Iterator[np.ndarray]:
        for n, block in enumerate(it, 1):
            yield block
            if on_progress and (n % 8 == 0 or n == total):
                on_progress(n / total)

    ny = -(-h // tile)
    nx = -(-w // tile)
    rgb = meta["rgb"] and channels == [0, 1, 2]
    with tifffile.TiffWriter(out_file, bigtiff=True) as tw:
        if rgb:
            tw.write(
                counted(_roi_rgb_tile_iter(root, level_path, x, y, w, h, tile), ny * nx),
                shape=(h, w, 3),
                dtype=dtype,
                tile=(tile, tile),
                photometric="rgb",
                **res_kw,
                compression="zlib",
                description=(
                    f"nd2wsi ROI from {source_description} level {level} "
                    f"(downsample {factor}) x={x} y={y} w={w} h={h}"
                ),
            )
        else:
            multi = len(channels) > 1
            kwargs = dict(
                dtype=dtype,
                tile=(tile, tile),
                photometric="minisblack",
                **res_kw,
                compression="zlib",
                description=(
                    f"nd2wsi ROI from {source_description} level {level} "
                    f"(downsample {factor}) x={x} y={y} w={w} h={h} "
                    f"channels={channels}"
                ),
            )
            if multi:
                kwargs["shape"] = (len(channels), h, w)
                kwargs["planarconfig"] = "separate"
            else:
                kwargs["shape"] = (h, w)
            tw.write(
                counted(
                    _roi_tile_iter(root, level_path, x, y, w, h, channels, tile),
                    ny * nx * len(channels),
                ),
                **kwargs,
            )


RENDER_STRIP_ROWS = 1024


def export_roi_rendered(
    root: Any,
    attrs: dict[str, Any],
    level: int,
    x: int,
    y: int,
    w: int,
    h: int,
    channels: list[int],
    fmt: str,
    win: str | None = None,
    *,
    scale_bar: bool = False,
) -> bytes:
    """Render a region to PNG/JPEG or a PNG-backed SVG, strip by strip.

    Compositing goes through float32, which at hundreds of megapixels used
    to mean several times the output size in transients. Each strip is
    composited alone into one preallocated uint8 canvas, so the peak is
    the canvas plus one strip — the canvas itself is checked against free
    memory before anything is read. Encoding still needs the whole image;
    this is bounded, not streaming.
    """
    meta = attrs["nd2wsi"]
    lv = level_entry(meta["levels"], level)
    windows, colors = display_params(attrs)
    windows, gammas = parse_windows(win, windows)

    bar = None
    if scale_bar or fmt == "svg":
        from .scalebar import layout

        bar = layout(w, h, meta.get("pixel_size_um"), lv["downsample"])

    canvas_bytes = h * w * 3
    budget = _available_memory() // 2
    # SVG additionally holds the lossless PNG and its base64/XML representation.
    required_bytes = canvas_bytes * (4 if fmt == "svg" else 1)
    if required_bytes > budget:
        raise ValueError(
            f"a {w} x {h} render needs {required_bytes / 1e9:.1f} GB for its "
            f"output buffers but only {budget / 1e9:.1f} GB is safely available — "
            "export a coarser level, or use TIFF, which streams"
        )

    canvas = np.empty((h, w, 3), np.uint8)
    for y0 in range(0, h, RENDER_STRIP_ROWS):
        th = min(RENDER_STRIP_ROWS, h - y0)
        region = _read_region(root, lv["path"], x, y + y0, w, th)
        canvas[y0 : y0 + th] = composite(
            region, channels, windows, colors, meta["rgb"], gammas
        )
    if bar is not None:
        from PIL import Image

        from . import scalebar

        if fmt == "svg":
            return scalebar.svg(encode_image(canvas, "png"), w, h, bar,
                                roi={"level": level, "x": x, "y": y, "width": w, "height": h})
        if fmt in ("jpg", "jpeg"):
            return scalebar.jpeg(Image.fromarray(canvas), bar)
        raise ValueError("Scale bar export supports SVG and JPEG")
    return encode_image(canvas, fmt, quality=92)


def _available_memory() -> int:
    """Bytes this process can reasonably claim right now."""
    try:
        import subprocess

        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        total = int(out.stdout.strip())
    except Exception:
        total = 8 << 30
    return total // 2
