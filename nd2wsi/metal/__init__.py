"""Opt-in, exact Apple-silicon pyramid reducer; never a disk-to-display zero-copy claim.

Decoded input is copied once into owned page-aligned shared staging memory.
Metal wraps that allocation and writes another owned shared allocation; the
returned NumPy view keeps its mmap alive. Compression remains the CPU writer's
job. All resources are idle before the native call returns. No reader, source
mapping, cache format, display path, or scientific export is changed.
"""
from __future__ import annotations

import ctypes
import json
import mmap
import os
import platform
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import numpy as np

from ..direct_common import box_mean

_lock = threading.RLock()
_library = None
_load_error = None
_warned = set()
_stats = {}
MAX_STAGING_BYTES = 128 << 20


def reset_diagnostics() -> None:
    with _lock:
        _stats.clear()
        _stats.update(gpu_calls=0, cpu_fallbacks=0, input_copy_bytes=0,
                      output_view_bytes=0, gpu_seconds=0.0, bridge_seconds=0.0,
                      max_staging_bytes=0, fallback_reasons={})


reset_diagnostics()


def enabled() -> bool:
    return (sys.platform == "darwin" and platform.machine() == "arm64"
            and os.environ.get("ND2WSI_GPU_PYRAMID") == "1")


def _load_library():
    global _library, _load_error
    if _library is not None:
        return _library
    if _load_error is not None:
        raise RuntimeError(_load_error)
    try:
        lib = ctypes.CDLL(str(Path(__file__).with_name("libnd2wsi_metal.dylib")))
        lib.nd2wsi_metal_reduce.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_double), ctypes.c_void_p, ctypes.c_size_t,
        ]
        lib.nd2wsi_metal_reduce.restype = ctypes.c_int
        lib.nd2wsi_metal_info.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.nd2wsi_metal_info.restype = ctypes.c_int
        _library = lib
        return lib
    except Exception as exc:
        _load_error = f"Metal library unavailable: {exc}"
        raise RuntimeError(_load_error) from exc


def device_info() -> dict:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return {"supported": False, "error": "requires native macOS arm64"}
    with _lock:
        try:
            raw = ctypes.create_string_buffer(4096)
            if _load_library().nd2wsi_metal_info(raw, len(raw)):
                raise RuntimeError("Metal device information failed")
            return json.loads(raw.value)
        except Exception as exc:
            return {"supported": False, "error": str(exc)}


def available() -> bool:
    return bool(device_info().get("supported"))


def diagnostics() -> dict:
    with _lock:
        return {**_stats, "fallback_reasons": dict(_stats["fallback_reasons"]),
                "enabled": enabled(), "copy_scope": "decoded input to shared staging only; not total system bandwidth",
                "disk_to_display_zero_copy": False}


def _fallback(reason: str, *, warn: bool = False):
    with _lock:
        _stats["cpu_fallbacks"] += 1
        reasons = _stats["fallback_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1
        if warn and reason not in _warned:
            _warned.add(reason)
            warnings.warn(f"Metal pyramid: {reason}; using exact CPU reducer", RuntimeWarning,
                          stacklevel=3)
    return None


def reduce2x(block: np.ndarray) -> np.ndarray | None:
    """Exact 2x2 ties-to-even mean for uint8/uint16, or None for CPU fallback.

    Only 2D YX and 3D CYX inputs are supported. Odd trailing samples are
    discarded; caller handles a collapsed one-pixel axis using edge repeat.
    One dispatch at a time bounds extra staging memory despite Dask workers.
    """
    if not enabled():
        return None
    if not isinstance(block, np.ndarray) or block.ndim not in (2, 3):
        return _fallback("unsupported shape")
    if block.dtype not in (np.dtype("uint8"), np.dtype("uint16")):
        return _fallback("unsupported dtype")
    c = 1 if block.ndim == 2 else block.shape[0]
    h, w = (int(n) // 2 * 2 for n in block.shape[-2:])
    if not c or not h or not w or max(c, h, w) > 0xFFFFFFFF:
        return _fallback("empty or unsupported dimensions")
    itemsize = block.dtype.itemsize
    input_bytes, output_bytes = c * h * w * itemsize, c * (h // 2) * (w // 2) * itemsize
    page = mmap.PAGESIZE
    input_len = (input_bytes + page - 1) // page * page
    output_len = (output_bytes + page - 1) // page * page
    if input_len + output_len > MAX_STAGING_BYTES:
        return _fallback("staging budget exceeded")
    # Allocation and copies occur inside the gate so only one staging pair
    # can be in flight. Completed output belongs to its returned ndarray.
    with _lock:
        start = time.perf_counter()
        incoming = outgoing = None
        try:
            lib = _load_library()
            incoming = mmap.mmap(-1, input_len)
            outgoing = mmap.mmap(-1, output_len)
            packed = np.ndarray((c, h, w), block.dtype, buffer=incoming)
            np.copyto(packed, block[..., :h, :w].reshape((c, h, w)))
            result = np.ndarray((c, h // 2, w // 2), block.dtype, buffer=outgoing)
            gpu_time = ctypes.c_double()
            error = ctypes.create_string_buffer(2048)
            _stats["input_copy_bytes"] += input_bytes
            _stats["max_staging_bytes"] = max(_stats["max_staging_bytes"], input_len + output_len)
            code = lib.nd2wsi_metal_reduce(
                packed.ctypes.data, input_len, result.ctypes.data, output_len,
                c, h, w, itemsize * 8, ctypes.byref(gpu_time), error, len(error),
            )
            if code:
                raise RuntimeError(error.value.decode("utf-8", "replace") or f"native error {code}")
            _stats["gpu_calls"] += 1
            _stats["gpu_seconds"] += max(0.0, gpu_time.value)
            _stats["output_view_bytes"] += output_bytes
            return result[0] if block.ndim == 2 else result
        except Exception as exc:
            return _fallback(str(exc), warn=True)
        finally:
            _stats["bridge_seconds"] += time.perf_counter() - start
            # NumPy holds buffer owners. Never close an output backing a
            # successful return; native wrappers no longer reference either.
            if incoming is not None:
                incoming.close()


def reduce2x_cpu(block: np.ndarray, out_dtype: Any = None) -> np.ndarray:
    """The exact CPU ``box-mean-floor-v1`` kernel shared by every 2x reducer.

    ``block`` is 2D YX or 3D CYX. Each 2x2 box is averaged in float32 (exact
    for four uint8/uint16 values) and an odd trailing row or column is
    dropped; a caller repeats an axis already collapsed to one pixel before
    calling. ``out_dtype`` defaults to the block's dtype: an integer output
    takes the mean through ``np.rint`` (ties-to-even) before the cast, a
    floating output is cast without any rounding. This is
    :func:`nd2wsi.direct_common.box_mean` at k=2, which reaches the same
    bits through integer sums for uint8/uint16 input.
    """
    return box_mean(block, 2, out_dtype)


def reduce2x_or_cpu(block: np.ndarray, out_dtype: Any = None) -> np.ndarray:
    """:func:`reduce2x` first, then the identical :func:`reduce2x_cpu` kernel.

    Same shapes and output-dtype rule as :func:`reduce2x_cpu`. The GPU kernel
    rounds like the integer rule, so it is consulted only for an integer
    output; a floating output always takes the unrounded CPU mean.
    """
    dtype = np.dtype(block.dtype if out_dtype is None else out_dtype)
    if np.issubdtype(dtype, np.integer):
        result = reduce2x(block)
        if result is not None:
            return result.astype(dtype, copy=False)
    return reduce2x_cpu(block, dtype)
