"""Serve an SVS with no pyramid store at all.

An Aperio file already carries a pyramid (typically 1x, 4x, 16x, 32x), and
the parallel tile reader in :mod:`nd2wsi.svs` decodes it fast enough to
feed the viewer straight from the file. This module wraps those embedded
levels in the halving ladder the viewer expects. Ladder levels the file
holds are decoded directly, and the gaps between them (2x, 8x, and the
tail below the deepest embedded level) are box-means of the next finer
embedded level, computed per request. A bytes-bounded cache of decoded
native tiles keeps revisited fields instant.

Nothing is written to disk. Opening is immediate, and the trash button has
nothing to do.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from .reader import ChannelInfo, PlaneSource, level_shapes
from .svs import _aperio_meta, _require_imagecodecs, _window_reader

TILE_CACHE_BYTES = 512 * 1024 * 1024  # decoded native tiles kept in memory


class _TileCache:
    """Bytes-bounded LRU shared by every level of one open slide."""

    def __init__(self, budget: int = TILE_CACHE_BYTES):
        self.budget = budget
        self.used = 0
        self._d: OrderedDict[Any, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> np.ndarray | None:
        with self._lock:
            hit = self._d.get(key)
            if hit is not None:
                self._d.move_to_end(key)
            return hit

    def put(self, key: Any, value: np.ndarray) -> None:
        with self._lock:
            if key in self._d:
                return
            self._d[key] = value
            self.used += value.nbytes
            while self.used > self.budget and self._d:
                _, old = self._d.popitem(last=False)
                self.used -= old.nbytes


def _cached_reader(page: Any, fd: int, cache: _TileCache, tag: int):
    """A :func:`_window_reader` whose native-tile decodes go through
    ``cache``. Same window semantics, shared across server threads."""
    raw = _window_reader(page, fd)
    th, tw = page.tilelength, page.tilewidth

    def read(y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        # aligned single-tile windows are the cacheable unit
        key = (tag, y0 // th, x0 // tw)
        if (y1 - y0, x1 - x0) == (th, tw) and y0 % th == 0 and x0 % tw == 0:
            hit = cache.get(key)
            if hit is not None:
                return hit
            block = raw(y0, y1, x0, x1)
            cache.put(key, block)
            return block
        # larger windows assemble from cached tiles
        out = None
        for ty in range(y0 // th, -(-y1 // th)):
            for tx in range(x0 // tw, -(-x1 // tw)):
                t = read(ty * th, (ty + 1) * th, tx * tw, (tx + 1) * tw)
                if out is None:
                    out = np.zeros((y1 - y0, x1 - x0, t.shape[2]), t.dtype)
                oy0, oy1 = max(y0, ty * th), min(y1, (ty + 1) * th)
                ox0, ox1 = max(x0, tx * tw), min(x1, (tx + 1) * tw)
                out[oy0 - y0 : oy1 - y0, ox0 - x0 : ox1 - x0] = t[
                    oy0 - ty * th : oy1 - ty * th, ox0 - tx * tw : ox1 - tx * tw
                ]
        return out if out is not None else raw(y0, y1, x0, x1)

    return read


class _SvsLevel:
    """One halving-ladder level, sliced like a (3, H, W) zarr array.

    ``h`` and ``w`` are chosen by :func:`open_direct` so that every read,
    scaled by ``extra``, stays inside the source page. Nothing here can
    walk off the native tile grid or touch zero-filled padding.
    """

    def __init__(self, read, h: int, w: int, extra: int):
        self._read = read
        self.shape = (3, h, w)
        self.dtype = np.dtype(np.uint8)
        self._extra = extra

    def _window(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        e = self._extra
        block = self._read(y0 * e, y1 * e, x0 * e, x1 * e)[..., :3]  # drop alpha
        if e > 1:
            h2, w2 = block.shape[0] // e, block.shape[1] // e
            block = (
                block[: h2 * e, : w2 * e]
                .reshape(h2, e, w2, e, 3)
                .mean(axis=(1, 3), dtype=np.float32)
            )
            block = np.rint(block).astype(np.uint8)
        return np.ascontiguousarray(np.moveaxis(block, -1, 0))

    def __getitem__(self, idx: Any) -> np.ndarray:
        _, H, W = self.shape
        if not isinstance(idx, tuple):
            idx = (idx,)
        idx = idx + (slice(None),) * (3 - len(idx))
        cs, ys, xs = idx
        if isinstance(ys, slice):
            y0, y1, ystep = ys.indices(H)
        else:
            y0 = ys + H if ys < 0 else ys
            y1, ystep = y0 + 1, 1
        if isinstance(xs, slice):
            x0, x1, xstep = xs.indices(W)
        else:
            x0 = xs + W if xs < 0 else xs
            x1, xstep = x0 + 1, 1
        if ystep != 1 or xstep != 1:
            raise IndexError("strided reads are not supported")
        if not (0 <= y0 <= y1 <= H and 0 <= x0 <= x1 <= W):
            raise IndexError(f"index out of range for shape {self.shape}")
        block = self._window(y0, y1, x0, x1)  # (3, h, w)
        return block[cs]


MAX_EXTRA = 8  # deepest on-the-fly downsample of one embedded level


def open_direct(path: str | Path) -> tuple[Any, dict[str, Any]]:
    """(virtual root, attrs) for an SVS, shaped like ``open_store``'s.

    Raises ``NotImplementedError`` for files this cannot serve well -- an
    untiled baseline, an embedded pyramid that is missing or off the
    power-of-two ladder, or one so sparse that a gap level would have to
    decode a huge span per request. Callers fall back to a full
    conversion for those, which is what every SVS got before 0.7.0.
    """
    import tifffile

    from .convert import build_group_attrs

    _require_imagecodecs()
    path = Path(path)
    tf = tifffile.TiffFile(str(path))
    fd = -1
    try:
        series = tf.series[0]
        if series.ndim != 3 or series.shape[-1] not in (3, 4):
            raise NotImplementedError(f"unexpected SVS layout {series.shape}")
        fd = os.open(str(path), os.O_RDONLY)

        H, W = int(series.shape[0]), int(series.shape[1])
        shapes = level_shapes(H, W, 512)
        cache = _TileCache()

        # every embedded level that is tiled and sits on the halving ladder
        embedded: dict[int, Any] = {}
        for lv in series.levels:
            page = lv.pages[0]
            if not getattr(page, "is_tiled", False):
                continue
            d = max(1, round(H / lv.shape[0]))
            k = d.bit_length() - 1
            on_ladder = (
                2**k == d
                and k < len(shapes)
                and abs(shapes[k][0] - lv.shape[0]) <= 2
                and abs(shapes[k][1] - lv.shape[1]) <= 2
            )
            if on_ladder and k not in embedded:
                embedded[k] = (page, int(lv.shape[0]), int(lv.shape[1]))

        if 0 not in embedded:
            raise NotImplementedError("SVS baseline level is not tiled")
        if len(shapes) > 1 and len(embedded) == 1:
            raise NotImplementedError("SVS carries no usable embedded pyramid")

        readers = {
            k: _cached_reader(page, fd, cache, k)
            for k, (page, _, _) in embedded.items()
        }
        root: dict[str, _SvsLevel] = {}
        served: list[tuple[int, int]] = []
        for k in range(len(shapes)):
            src = max(j for j in embedded if j <= k)
            e = 2 ** (k - src)
            if e > MAX_EXTRA:
                raise NotImplementedError(
                    f"embedded pyramid too sparse (level {k} would need "
                    f"a {e}x downsample per request)"
                )
            sh, sw = embedded[src][1], embedded[src][2]
            # serve exactly what the source holds, so a read scaled by
            # ``e`` can never cross the source edge or its tile grid
            h, w = sh // e, sw // e
            root[str(k)] = _SvsLevel(readers[src], h, w, e)
            served.append((h, w))
        shapes = served

        meta = _aperio_meta(tf.pages[0].description or "")
    except Exception:
        tf.close()
        if fd >= 0:
            os.close(fd)
        raise
    mpp = float(meta.get("mpp", 1.0))
    src_info = PlaneSource(
        data=None,
        dtype=np.dtype(np.uint8),
        shape=(3, H, W),
        rgb=True,
        channels=[
            ChannelInfo("Red", (255, 0, 0)),
            ChannelInfo("Green", (0, 255, 0)),
            ChannelInfo("Blue", (0, 0, 255)),
        ],
        pixel_size_um=(mpp, mpp),
        source_name=str(path),
        selection={},
        notes=[f"served straight from the SVS ({len(embedded)} embedded levels)"],
        magnification=meta.get("magnification"),
    )
    windows = [{"start": 0.0, "end": 255.0, "min": 0.0, "max": 255.0}] * 3
    attrs = build_group_attrs(src_info, shapes, 512, windows)
    attrs["nd2wsi"]["direct"] = True

    class _Root(dict):
        _closed = False

        def close(self, delay: float = 5.0) -> None:
            """Release the file handles once in-flight requests are done.

            The registry pops the slide before calling this, so no new
            request can start. Ones already running still hold the fd for
            a moment; closing it under them could hand their reads a
            recycled descriptor, so the close waits a beat.
            """
            if self._closed:
                return
            self._closed = True

            def _do() -> None:
                tf.close()
                os.close(fd)

            t = threading.Timer(delay, _do)
            t.daemon = True
            t.start()

    out = _Root(root)
    return out, attrs
