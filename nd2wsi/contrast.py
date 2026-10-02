"""Robust display-only contrast; never modifies acquisition pixels."""
from __future__ import annotations

import numpy as np


def auto_window(
    channel: np.ndarray, *, dtype=None, background_mode: bool = True, rgb: bool = False,
) -> tuple[float, float]:
    """A sampled background-to-99.5% display window.

    For nonnegative images, zero padding does not determine the percentiles.
    Saturated values remain in the estimate: a bright-field background can
    legitimately occupy the sensor ceiling. A percentile limits the influence
    of rare bright pixels without claiming to identify biological artifacts.
    RGB callers pool the components and use zero as black to share one gain.
    """
    values = np.asarray(channel).ravel()
    dtype = np.dtype(dtype if dtype is not None else values.dtype)
    values = values[np.isfinite(values)].astype(np.float64, copy=False)
    discrete = np.issubdtype(dtype, np.integer) or np.issubdtype(dtype, np.bool_)
    limits = np.iinfo(dtype) if np.issubdtype(dtype, np.integer) else None
    if not values.size:
        return 0.0, 1.0
    minimum = float(values.min())
    sample = values[values > 0] if minimum == 0 and np.any(values > 0) else values
    low, high = (float(v) for v in np.percentile(sample, [0.1, 99.5]))
    origin = float(sample.min())
    if rgb and minimum >= 0:
        low = 0.0
    elif minimum == 0 and origin == float(sample.max()):
        # Sparse, constant-valued foreground must not become the black point.
        low = 0.0
    elif background_mode and high > origin:
        # Exclude tails rather than piling clipped pixels into the edge bin.
        inside = sample[(sample >= origin) & (sample <= high)]
        counts, edges = np.histogram((inside - origin) / (high - origin), bins=256,
                                    range=(0.0, 1.0))
        mode = origin + float(edges[int(np.argmax(counts))]) * (high - origin)
        if mode < low + 0.70 * (high - low):
            low = max(low, mode)
    width = 1.0 if discrete else max(1.0, abs(low), abs(high)) * np.finfo(float).eps * 256
    if high <= low:
        high = low + width
    if limits is not None:
        high = min(float(limits.max), high)
        low = max(float(limits.min), min(low, high - 1.0))
    elif np.issubdtype(dtype, np.bool_):
        low, high = 0.0, 1.0
    return low, high
