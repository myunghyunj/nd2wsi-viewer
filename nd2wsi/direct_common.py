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

# Inputs whose k x k box sums the exact integer route accumulates, and in what.
# The largest sum, 65535 * 16 * 16 = 16,776,960, fits uint32 and stays under
# 2**24, so the float32 mean it replaces is exact as well (see box_mean).
_EXACT_ACCUMULATOR = {
    np.dtype(np.uint8): np.dtype(np.uint16),
    np.dtype(np.uint16): np.dtype(np.uint32),
}
_EXACT_K = (2, 4, 8, 16)


def box_mean(block: np.ndarray, k: int, out_dtype: Any = None) -> np.ndarray:
    """``k`` x ``k`` box mean over the last two axes (Y, X) of ``block``.

    The one definition every reducer shares (``box-mean-floor-v1`` at k=2):
    each box averaged in float32, an integer output rounded with ``np.rint``
    (ties-to-even), a floating output cast without rounding, and trailing
    rows or columns that do not fill a whole box dropped. ``out_dtype``
    defaults to the block's dtype. The input is never modified.

    uint8/uint16 input with k in 2, 4, 8, 16 into an integer dtype that holds
    every input value takes an exact integer route that is bit-identical:
    each sum is below 2**24 and k*k is a power of two, so the float32 mean is
    already the exact quotient and ``rint`` its round-half-even, which the
    shift below reproduces. Any other input keeps the float32 formula.
    """
    k = int(k)
    dtype = np.dtype(block.dtype if out_dtype is None else out_dtype)
    *lead, h, w = block.shape
    h2, w2 = h // k, w // k
    view = block[..., : h2 * k, : w2 * k]
    acc = _EXACT_ACCUMULATOR.get(np.dtype(block.dtype))
    if (
        acc is None
        or k not in _EXACT_K
        or not np.issubdtype(dtype, np.integer)
        or not np.can_cast(block.dtype, dtype)
    ):
        mean = view.reshape((*lead, h2, k, w2, k)).mean(axis=(-3, -1), dtype=np.float32)
        if np.issubdtype(dtype, np.integer):
            return np.rint(mean).astype(dtype)
        return mean.astype(dtype)
    # Sum k rows at a time over contiguous memory, then k neighbouring
    # columns as k strided adds: far cheaper than a float32 mean over a
    # 5-D view, whose innermost reduction is only k elements long.
    rows = view.reshape((*lead, h2, k, w2 * k)).sum(axis=-2, dtype=acc)
    total = rows[..., 0::k].copy()
    for i in range(1, k):
        total += rows[..., i::k]
    # Round half to even of total / 2**shift: add half - 1, plus 1 more when
    # the truncated quotient is odd, then truncate. At most 65,408 for uint8
    # input at k=16, so the uint16 accumulator cannot wrap.
    shift = 2 * (k.bit_length() - 1)
    odd = total >> shift
    odd &= 1
    total += odd
    total += (1 << (shift - 1)) - 1
    total >>= shift
    return total.astype(dtype)


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


class LeaseClosed(ValueError):
    """A closed ``_Lifecycle`` refusing a new entry; ``lease`` is the refuser.

    Still a ValueError with the same message, so existing callers keep
    working; a caller that must tell this refusal from other ValueErrors
    raised inside its ``with`` block checks ``lease``.
    """

    def __init__(self, lease: _Lifecycle) -> None:
        super().__init__("slide is closed")
        self.lease = lease


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
                raise LeaseClosed(self)
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

    def close_if_idle(self) -> bool:
        """Bar new entries only when none is running, without waiting.

        False leaves the door exactly as it was, so a refused close cannot
        turn away an entry that arrives meanwhile.
        """
        with self._cv:
            if self._n:
                return False
            self.closed = True
            return True

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

    A caller that cannot tell whether reads still hold the underlying file
    gets a short delay before the actual teardown (and, for memory-mapped
    sources, the teardown still waits for the lifecycle to drain). The
    registry drains a closed slide's reads first and closes with delay=0.
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
        # Channels first before reducing: one contiguous copy (the read may be
        # a cached native tile, never written to) makes the box sums stride
        # over whole rows instead of three interleaved samples.
        block = np.ascontiguousarray(np.moveaxis(block, -1, 0))
        if e > 1:
            block = box_mean(block, e)
        return block

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        if self._life is None:
            return self._window(y0, y1, x0, x1)[cs]
        with self._life:  # the fd must not close under an in-flight pread
            return self._window(y0, y1, x0, x1)[cs]
