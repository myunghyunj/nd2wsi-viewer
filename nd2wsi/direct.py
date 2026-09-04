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


def _parse_cyx_index(idx: Any, shape: tuple[int, int, int]):
    """Normalize a ``(C, Y, X)`` getitem index to ``(cs, y0, y1, x0, x1)``.

    Accepts what the render and export paths actually use: a channel
    index or slice plus contiguous spatial slices. The channel part is
    returned as given so the caller can apply it after windowing.
    """
    _, H, W = shape
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
        raise IndexError(f"index out of range for shape {shape}")
    return cs, y0, y1, x0, x1


class _Lifecycle:
    """Counts in-flight reads so close can wait for them, then bar the door.

    A memory-mapped view read after its file closes is not an error, it is
    a segfault. Every level-0 read enters here; close waits until the
    count drains before the file handle goes away, and any read arriving
    after that gets a clean exception instead of dead memory.
    """

    def __init__(self) -> None:
        self._n = 0
        self._cv = threading.Condition()
        self.closed = False

    def __enter__(self) -> _Lifecycle:
        with self._cv:
            if self.closed:
                raise ValueError("slide is closed")
            self._n += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        with self._cv:
            self._n -= 1
            self._cv.notify_all()

    @property
    def active(self) -> int:
        with self._cv:
            return self._n

    def close(self, timeout: float | None = 300.0) -> bool:
        """Bar new reads, wait for running ones. True when fully drained."""
        with self._cv:
            self.closed = True
            return self._cv.wait_for(lambda: self._n == 0, timeout)

    def reopen(self) -> None:
        """Admit reads again after a close attempt timed out.

        Callers may only use this when no backing handle was closed.  Active
        readers can remain counted: they still refer to the same live handle,
        and a later close attempt will wait for them as usual.
        """
        with self._cv:
            self.closed = False
            self._cv.notify_all()


class _Root(dict):
    """A virtual store root: level adapters by name, plus deferred close.

    The registry pops the slide before calling ``close``, so no new
    request can start; ones already running still hold the underlying
    file for a moment, so the actual teardown runs after a short delay
    (and, for memory-mapped sources, after the lifecycle drains).
    """

    def __init__(self, levels: dict[str, Any], closer: Any = None):
        super().__init__(levels)
        self._closer = closer
        self._closed = False

    def close(self, delay: float = 5.0) -> None:
        if self._closed or self._closer is None:
            return
        self._closed = True
        if delay <= 0:
            self._closer()
            return
        t = threading.Timer(delay, self._closer)
        t.daemon = True
        t.start()


class _TileCache:
    """Bytes-bounded LRU of decoded tiles, one budget per process."""

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

    def purge(self, owner: str) -> None:
        """Drop every tile whose tag names this owner (a closed slide)."""
        with self._lock:
            for key in [
                k
                for k in self._d
                if k[0] == owner or (isinstance(k[0], tuple) and k[0][0] == owner)
            ]:
                self.used -= self._d.pop(key).nbytes


# one decode budget for the whole process, not one per open slide
_TILE_CACHE = _TileCache()


def _cached_reader(page: Any, fd: int, cache: _TileCache, tag: Any):
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

    def __init__(self, read, h: int, w: int, extra: int, life: _Lifecycle | None = None):
        self._read = read
        self.shape = (3, h, w)
        self.dtype = np.dtype(np.uint8)
        self._extra = extra
        self._life = life

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
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        if self._life is None:
            return self._window(y0, y1, x0, x1)[cs]
        with self._life:  # the fd must not close under an in-flight pread
            return self._window(y0, y1, x0, x1)[cs]


class _Nd2Level0:
    """The slide's own pixels as level 0, sliced like a (C, Y, X) array.

    Wraps the zero-copy strided view :func:`nd2wsi.reader.plane_view`
    returns. Every read copies its window out of the memory map inside
    the lifecycle guard — ``.copy()``, never ``ascontiguousarray``, which
    hands the mapped view itself back for contiguous slices — so nothing
    mapped survives past ``close``. Each read also re-stats the source:
    a file changed or unreachable raises a clean error instead of
    serving garbage, and a vanished volume fails before the map is
    touched in almost every case.
    """

    def __init__(self, view: np.ndarray, life: _Lifecycle, path: str | Path):
        self._view = view
        self._life = life
        self._path = str(path)
        st = os.stat(path)
        self._ident = (st.st_size, st.st_mtime_ns)
        self.shape = tuple(view.shape)
        self.dtype = view.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        with self._life:
            try:
                st = os.stat(self._path)
            except OSError as e:
                raise ValueError(
                    f"the source file is unreachable ({e.strerror}); "
                    "close and reopen the slide"
                ) from e
            if (st.st_size, st.st_mtime_ns) != self._ident:
                raise ValueError(
                    "the source file changed while it was open; "
                    "close and reopen the slide"
                )
            return self._view[cs, y0:y1, x0:x1].copy()


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
        cache = _TILE_CACHE
        owner = str(path)
        # a replaced file must never alias tiles a previous open decoded:
        # sweep anything the deferred close has not purged yet, and fold
        # the file's identity into every new key
        cache.purge(owner)
        ident = path.stat().st_mtime_ns
        life = _Lifecycle()

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
            k: _cached_reader(page, fd, cache, (owner, ident, k))
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
            root[str(k)] = _SvsLevel(readers[src], h, w, e, life)
            served.append((h, w))
        shapes = served

        meta = _aperio_meta(tf.pages[0].description or "")
    except Exception:
        tf.close()
        if fd >= 0:
            os.close(fd)
        raise
    mpp = meta.get("mpp")
    calibrated = mpp is not None and mpp > 0
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
        pixel_size_um=(float(mpp), float(mpp)) if calibrated else None,
        source_name=str(path),
        selection={},
        notes=[f"served straight from the SVS ({len(embedded)} embedded levels)"],
        magnification=meta.get("magnification"),
        calibration_source="aperio-mpp" if calibrated else "unknown",
    )
    windows = [{"start": 0.0, "end": 255.0, "min": 0.0, "max": 255.0}] * 3
    attrs = build_group_attrs(src_info, shapes, 512, windows)
    attrs["nd2wsi"]["direct"] = True

    def _teardown() -> None:
        if not life.close():
            return  # a read still holds the fd; leaking beats EBADF under it
        tf.close()
        os.close(fd)
        _TILE_CACHE.purge(owner)

    return _Root(root, _teardown), attrs


def open_nd2_backed(
    slide: str | Path, store: str | Path
) -> tuple[Any, dict[str, Any]]:
    """(virtual root, attrs) serving level 0 from the ND2 file itself.

    ``store`` is an overview pyramid holding levels 1..n; the file's own
    memory map plays level 0. Raises ``NotImplementedError`` when the
    file cannot back a level at runtime — the caller falls back to a full
    store or a degraded overview-only view.
    """
    import copy

    import nd2
    import zarr

    from .cache import read_manifest
    from .reader import PlaneSelection, is_source_backable, plane_view

    slide, store = Path(slide), Path(store)
    manifest = read_manifest(store.parent) or {}
    sel = manifest.get("selection", {})
    selection = PlaneSelection(
        t=int(sel.get("t", 0)),
        p=int(sel.get("p", 0)),
        z=sel.get("z_resolved", sel.get("z", "mid")),
    )

    f = nd2.ND2File(str(slide))
    try:
        ok, why = is_source_backable(f, selection)
        if not ok:
            raise NotImplementedError(f"{slide.name}: {why}")
        view, _ = plane_view(f, selection)

        zroot = zarr.open_group(str(store), mode="r")
        attrs = copy.deepcopy(dict(zroot.attrs))
        meta = attrs.get("nd2wsi") or {}
        ov = meta.get("overview_of")
        if meta.get("kind") != "overview" or not ov:
            raise NotImplementedError(f"{store} is not an overview store")
        c, h, w = view.shape
        if [c, h, w] != [ov["channels"], ov["height"], ov["width"]]:
            raise NotImplementedError(
                f"{slide.name} no longer matches its overview "
                f"({w} x {h} vs {ov['width']} x {ov['height']})"
            )

        life = _Lifecycle()
        levels: dict[str, Any] = {"0": _Nd2Level0(view, life, slide)}
        for lv in meta["levels"]:
            levels[lv["path"]] = zroot[lv["path"]]

        meta["levels"] = [
            {"path": "0", "width": w, "height": h, "downsample": 1}
        ] + meta["levels"]
        meta["kind"] = "source-backed"
        for ms in attrs.get("multiscales", []):
            first = ms["datasets"][0]["coordinateTransformations"][0]["scale"]
            ms["datasets"].insert(
                0,
                {
                    "path": "0",
                    "coordinateTransformations": [
                        {"type": "scale", "scale": [1.0, first[1] / 2, first[2] / 2]}
                    ],
                },
            )
        attrs["nd2wsi"] = meta
    except BaseException:
        f.close()
        raise

    def _teardown() -> None:
        if not life.close():
            # a reader still holds the map after the drain timeout.
            # nd2's frames are ndarray(buffer=mmap) views, which raise no
            # BufferError on close — unmapping here would be a segfault in
            # that reader, so the handle leaks until process exit instead
            return
        try:
            f.close()
        except BufferError:  # pragma: no cover - defensive
            pass

    return _Root(levels, _teardown), attrs
