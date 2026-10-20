"""Shared runtime primitives for direct slide readers and their consumers.

Keep lifetime leases, CYX indexing, RGB level adapters, and the process-wide
native-tile cache independent of any format-specific reader. ``direct`` retains
aliases for existing callers; all readers share the same objects and cache.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

import numpy as np

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

MAX_EXTRA = 8  # deepest on-the-fly downsample of one embedded level


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
