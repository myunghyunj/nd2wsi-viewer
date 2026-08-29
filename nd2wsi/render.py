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
from typing import Any, Iterator

import numpy as np


def parse_channels(param: str | None, n: int) -> list[int]:
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


def _read_region(root: Any, level_path: str, x: int, y: int, w: int, h: int) -> np.ndarray:
    """(C, h, w) region from one pyramid level, clipped to bounds."""
    arr = root[level_path]
    _, H, W = arr.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((arr.shape[0], 0, 0), dtype=arr.dtype)
    return np.asarray(arr[:, y0:y1, x0:x1])


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

    if rgb and len(channels) == 3 and channels == [0, 1, 2]:
        lo, hi = windows[0]
        img = region.astype(np.float32)
        img = (img - lo) / max(hi - lo, 1e-6)
        np.clip(img, 0, 1, out=img)
        if gammas[0] != 1.0:
            img **= 1.0 / gammas[0]
        return (np.moveaxis(img, 0, -1) * 255).astype(np.uint8)

    out = np.zeros((h, w, 3), dtype=np.float32)
    for ci in channels:
        lo, hi = windows[ci]
        v = (region[ci].astype(np.float32) - lo) / max(hi - lo, 1e-6)
        np.clip(v, 0, 1, out=v)
        if gammas[ci] != 1.0:
            v **= 1.0 / gammas[ci]
        col = np.asarray(colors[ci], dtype=np.float32) / 255.0
        out += v[..., None] * col
    return (np.clip(out, 0, 1) * 255).astype(np.uint8)


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
    levels = meta["levels"]
    if not 0 <= level < len(levels):
        raise KeyError(f"level {level} out of range")
    lw, lh = levels[level]["width"], levels[level]["height"]
    x, y = tx * tile, ty * tile
    if x >= lw or y >= lh or tx < 0 or ty < 0:
        raise KeyError("tile out of range")
    w, h = min(tile, lw - x), min(tile, lh - y)
    region = _read_region(root, levels[level]["path"], x, y, w, h)
    windows, colors = display_params(attrs)
    windows, gammas = parse_windows(win, windows)
    img = composite(region, channels, windows, colors, meta["rgb"], gammas)
    return encode_image(img, fmt)


def compute_histograms(
    root: Any, attrs: dict[str, Any], bins: int = 256, min_pixels: int = 300_000
) -> list[dict[str, Any]]:
    """Per-channel intensity histograms for the LUT panel.

    Computed from the smallest pyramid level with at least ``min_pixels``
    (a few-MPx read at most).  The axis max is robust to lone hot pixels:
    the 99.9th percentile with 30% headroom, clamped to the data max, and
    never below the stored display window.  Overflow lands in the last bin.
    """
    meta = attrs["nd2wsi"]
    levels = meta["levels"]
    pick = levels[0]
    for lv in reversed(levels):
        if lv["width"] * lv["height"] >= min_pixels:
            pick = lv
            break
    data = np.asarray(root[pick["path"]][:])  # (C, h, w)
    out = []
    for ci in range(data.shape[0]):
        ch = data[ci].ravel()
        dmax = float(ch.max()) if ch.size else 1.0
        vmax = min(float(np.percentile(ch, 99.9)) * 1.3, dmax)
        win = attrs["omero"]["channels"][ci].get("window", {})
        vmax = max(vmax, float(win.get("end", 0)) * 1.1, 1.0)
        counts, _ = np.histogram(np.minimum(ch, vmax), bins=bins, range=(0, vmax))
        out.append({"bins": [int(c) for c in counts], "vmax": vmax, "level": pick["path"]})
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
) -> None:
    """Stream a raw-dtype tiled TIFF of the ROI to a file object or path."""
    import tifffile

    meta = attrs["nd2wsi"]
    levels = meta["levels"]
    level_path = levels[level]["path"]
    tile = min(int(meta["tile"]), 512)
    dtype = np.dtype(meta["dtype"])
    py, px = meta["pixel_size_um"]
    factor = levels[level]["downsample"]
    # pixels per centimeter at this level
    res = (1e4 / (px * factor), 1e4 / (py * factor))

    rgb = meta["rgb"] and channels == [0, 1, 2]
    with tifffile.TiffWriter(out_file, bigtiff=True) as tw:
        if rgb:
            tw.write(
                _roi_rgb_tile_iter(root, level_path, x, y, w, h, tile),
                shape=(h, w, 3),
                dtype=dtype,
                tile=(tile, tile),
                photometric="rgb",
                resolution=res,
                resolutionunit="CENTIMETER",
                compression="zlib",
                description=(
                    f"nd2wsi ROI from {meta['source']} level {level} "
                    f"(downsample {factor}) x={x} y={y} w={w} h={h}"
                ),
            )
        else:
            multi = len(channels) > 1
            kwargs = dict(
                dtype=dtype,
                tile=(tile, tile),
                photometric="minisblack",
                resolution=res,
                resolutionunit="CENTIMETER",
                compression="zlib",
                description=(
                    f"nd2wsi ROI from {meta['source']} level {level} "
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
                _roi_tile_iter(root, level_path, x, y, w, h, channels, tile),
                **kwargs,
            )


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
) -> bytes:
    meta = attrs["nd2wsi"]
    levels = meta["levels"]
    region = _read_region(root, levels[level]["path"], x, y, w, h)
    windows, colors = display_params(attrs)
    windows, gammas = parse_windows(win, windows)
    img = composite(region, channels, windows, colors, meta["rgb"], gammas)
    return encode_image(img, fmt, quality=92)
