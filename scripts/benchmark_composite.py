"""Time nd2wsi.render.composite against the pre-LUT reference implementation.

Synthetic tiles only; prints the median wall time of each case and whether the
two outputs are identical. No timing is asserted.

    python scripts/benchmark_composite.py [--reps N]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

# Run from a checkout without relying on the editable install: on iCloud-synced
# folders macOS can mark the venv's .pth files hidden, and Python 3.13 skips them.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nd2wsi import render  # noqa: E402


def reference_composite(region, channels, windows, colors, rgb, gammas=None):
    """composite() as of v2.5.3: one dense (h, w, 3) float32 accumulator."""
    c, h, w = region.shape
    if h == 0 or w == 0:
        return np.zeros((h, w, 3), dtype=np.uint8)
    gammas = gammas or [1.0] * len(windows)
    floating = np.issubdtype(region.dtype, np.floating)
    working_dtype = np.float64 if floating else np.float32
    if rgb:
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
    out = np.zeros((h, w, 3), dtype=np.float32)
    for ci in channels:
        lo, hi = windows[ci]
        span = (hi - lo if hi > lo else 1.0) if floating else max(hi - lo, 1e-6)
        v = (region[ci].astype(working_dtype) - lo) / span
        np.clip(v, 0, 1, out=v)
        if gammas[ci] != 1.0:
            v **= 1.0 / gammas[ci]
        col = np.asarray(colors[ci], dtype=np.float32) / 255.0
        out += v[..., None] * col
    return (np.clip(out, 0, 1) * 255).astype(np.uint8)


def median_ms(fn, args, reps: int) -> float:
    for _ in range(5):
        fn(*args)
    times = []
    for _ in range(reps):
        start = time.perf_counter()
        fn(*args)
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def cases() -> list[tuple[str, tuple]]:
    rng = np.random.default_rng(20261024)
    shape = (4, 512, 512)
    sparse = rng.random(shape) < 0.2
    u16 = (rng.poisson(600, shape) + rng.integers(0, 8000, shape) * sparse).astype(np.uint16)
    windows = [(1667.17578125, 4480.0), (7488.70749999999, 11812.41499999998),
               (548.66568359375, 4905.80499999999), (300.0, 9000.0)]
    primaries = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255)]
    mixed_colours = [(255, 255, 255), (255, 200, 100), (90, 255, 30), (128, 64, 255)]
    chans = [0, 1, 2, 3]
    rgb8 = rng.integers(0, 256, (3, 512, 512), dtype=np.uint8)
    f32 = (u16.astype(np.float32) * np.float32(1.0137)).astype(np.float32)
    return [
        ("4x512x512 uint16, gamma 1.0", (u16, chans, windows, primaries, False, [1.0] * 4)),
        ("4x512x512 uint16, gamma 1.6", (u16, chans, windows, primaries, False, [1.6] * 4)),
        ("4x512x512 uint16, gamma mixed", (u16, chans, windows, primaries, False,
                                           [1.0, 1.6, 0.5, 2.2])),
        ("4x512x512 uint16, gamma 1.6, no zero colour components",
         (u16, chans, windows, mixed_colours, False, [1.6] * 4)),
        ("3x512x512 uint8 RGB, gamma 1.0", (rgb8, [0, 1, 2], [(0.0, 255.0)] * 3,
                                            [(255, 0, 0), (0, 255, 0), (0, 0, 255)], True,
                                            [1.0] * 3)),
        ("3x512x512 uint8 RGB, gamma 1.6", (rgb8, [0, 1, 2], [(10.0, 240.0)] * 3,
                                            [(255, 0, 0), (0, 255, 0), (0, 0, 255)], True,
                                            [1.6] * 3)),
        ("4x512x512 float32, gamma 1.0", (f32, chans, windows, primaries, False, [1.0] * 4)),
        ("4x512x512 float32, gamma 1.6", (f32, chans, windows, primaries, False, [1.6] * 4)),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reps", type=int, default=60)
    args = parser.parse_args()
    print(f"numpy {np.__version__}; median of {args.reps} runs after 5 warm-ups")
    print(f"{'case':58s} {'reference':>10s} {'composite':>10s} {'speedup':>8s}  identical")
    for name, call in cases():
        render._clear_gamma_lut_cache()
        identical = np.array_equal(reference_composite(*call), render.composite(*call))
        ref = median_ms(reference_composite, call, args.reps)
        new = median_ms(render.composite, call, args.reps)
        print(f"{name:58s} {ref:8.2f}ms {new:8.2f}ms {ref / new:7.2f}x  {identical}")


if __name__ == "__main__":
    main()
