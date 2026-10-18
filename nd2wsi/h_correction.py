"""Fit from original ND2 pixels, then persistently replace its display pyramid.

Fit inputs are unrounded native 8x/32x means. Applying and revoking prepare a
complete replacement before publication; cancellation preserves committed pixels.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

_ALIASES = {
    "FITC": "FITC",
    "CY3": "CY3",
    "DAPI": "DAPI",
    "BF": "BF",
    "BRIGHTFIELD": "BF",
    "BRIGHT FIELD": "BF",
}
_ACTIVE = {"preparing", "fitting", "preparing_display", "rebuilding", "publishing"}
_WORKSPACE_LIMIT = 4 * 1024**3
_FIT_MEMORY_LIMIT = 1024**3
_BLOCK_ROWS = 512
_BLOCK_COLS = 2048


class UnsupportedCorrection(ValueError):
    """An acquisition or operational limit requires leaving RAW unchanged."""


class _Cancelled(Exception):
    pass


def _mapping(names: list[str]) -> dict[str, int]:
    result = {}
    for index, name in enumerate(names):
        canonical = _ALIASES.get(name.strip().upper())
        if canonical:
            if canonical in result:
                raise UnsupportedCorrection(f"Ambiguous duplicate {canonical} channels")
            result[canonical] = index
    if "BF" not in result:
        raise UnsupportedCorrection("Shading Correction requires a BF channel")
    if not any(name in result for name in ("FITC", "CY3")):
        raise UnsupportedCorrection("Shading Correction requires a FITC or CY3 channel")
    return result


def _basic_reason(state: Any) -> str:
    """Cheap capability hint; the worker verifies original metadata later."""
    try:
        meta = state.attrs["nd2wsi"]
        if not getattr(state, "correction_write_allowed", True):
            return "Agent windows can replace pyramids only in their private exports folder"
        if state.plate is not None or meta.get("plate"):
            return "Shading Correction is available for stitched single-frame ND2 slides"
        path = state.source_path
        if path is None or Path(path).suffix.lower() != ".nd2" or not Path(path).is_file():
            return "Shading Correction needs the original ND2 source"
        if meta.get("rgb"):
            return "Shading Correction requires scalar fluorescence channels"
        if meta.get("selection", {}).get("z") == "max":
            return "Shading Correction is unavailable for Z projections"
        levels = meta["levels"]
        if not levels or str(levels[0]["path"]) != "0" or int(levels[0]["downsample"]) != 1:
            return "Shading Correction needs native level 0; an overview alone is insufficient"
        arr = getattr(state.root, "raw_level0", state.root["0"])
        if len(arr.shape) != 3 or min(arr.shape[1:]) < 32:
            return "The native raster is too small for Shading Correction"
        if np.dtype(arr.dtype) not in (np.dtype("uint8"), np.dtype("uint16")):
            return "Shading Correction requires uint8 or uint16 native ND2 values"
        c, h, w = arr.shape
        if len(state.attrs["omero"]["channels"]) != c:
            return "Native pixels and channel metadata disagree"
        for level in levels:
            k = int(level["path"])
            d = 2**k
            if (
                k < 0
                or level["downsample"] != d
                or (level["height"], level["width"]) != (max(1, h // d), max(1, w // d))
                or tuple(state.root[str(k)].shape) != (c, level["height"], level["width"])
            ):
                return "The pyramid geometry is unsupported for Shading Correction"
        _mapping([ch["label"] for ch in state.attrs["omero"]["channels"]])
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return (
            str(exc)
            if isinstance(exc, UnsupportedCorrection)
            else "Native slide metadata is unavailable"
        )
    return ""


def _source_stamp(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _estimated_seconds(state: Any) -> int:
    """Approximate preflight duration from metadata, without reading pixels.

    A verified source-backed 36,599 x 22,895 raster with four channels and
    two corrected targets took 173.3 s on the development Mac: 50.8 s for
    native means, 9.7 s fitting, and 112.8 s corrected display preparation.
    Scale these costs by pixel/channel work and add a 25% margin. Storage,
    hardware, fitting complexity and concurrent work can change the duration;
    this is an estimate for confirmation, never an execution-time guarantee.
    Runtime ETA remains based on progress observed in the current job.
    """
    channels, height, width = state.root["0"].shape
    mapping = _mapping([ch["label"] for ch in state.attrs["omero"]["channels"]])
    targets = sum(name in mapping for name in ("FITC", "CY3"))
    pixels = height * width / (36599 * 22895)
    seconds = pixels * (50.8 * channels / 4 + 9.7 + 112.8 * targets / 2)
    return max(30, math.ceil(1.25 * seconds / 15) * 15)


def _metadata_for_state(state: Any) -> dict:
    """Read metadata only; every pixel continues through the registered root."""
    import nd2

    from .cache import fingerprints_match, quick_fingerprint

    path = Path(state.source_path)
    before = _source_stamp(path)
    fingerprint = quick_fingerprint(path)
    registered = state.manifest.get("source", {})
    if not all(key in registered for key in ("size", "mtime_ns", "quick_sha256")):
        raise UnsupportedCorrection(
            "The registered source identity is unavailable; reopen the original ND2"
        )
    if not fingerprints_match(registered, fingerprint):
        raise UnsupportedCorrection("The ND2 source changed; reopen the slide")
    with nd2.ND2File(str(path)) as reader:
        sizes = dict(reader.sizes)
        if bool(reader.is_rgb) or sizes.get("S", 1) != 1:
            raise UnsupportedCorrection("Shading Correction requires scalar fluorescence channels")
        if any(value != 1 for axis, value in sizes.items() if axis not in ("C", "Y", "X", "S")):
            raise UnsupportedCorrection(
                "Shading Correction currently supports a single stitched frame"
            )
        if int(reader.attributes.sequenceCount) != 1:
            raise UnsupportedCorrection(
                "Shading Correction currently supports a single stitched frame"
            )
        names = [ch.channel.name for ch in reader.metadata.channels]
        displayed = [ch["label"] for ch in state.attrs["omero"]["channels"]]
        if names != displayed:
            raise UnsupportedCorrection(
                "The original ND2 channel order differs from the open slide"
            )
        mapping = _mapping(names)
        native = getattr(state.root, "raw_level0", state.root["0"])
        expected = (int(sizes.get("C", 1)), int(sizes["Y"]), int(sizes["X"]))
        if tuple(native.shape) != expected or np.dtype(reader.dtype) != np.dtype(native.dtype):
            raise UnsupportedCorrection(
                "The original ND2 pixels differ from the registered native root"
            )
        raw = reader.unstructured_metadata(strip_prefix=False)
        picture = raw.get("ImageMetadataSeqLV|0", {}).get("SLxPictureMetadata", {})
        samples = picture.get("sPicturePlanes", {}).get("sSampleSetting", {})
        settings = []
        for i, name in enumerate(names):
            sample = samples.get("a" + str(i), {})
            fmt = sample.get("pCameraSetting", {}).get("FormatQuality", {}).get("fmtDesc", {})
            settings.append({"channel": name, "sensor_pixels_xy": fmt.get("sizeSensorPixels")})
        sensor = settings[mapping["BF"]]["sensor_pixels_xy"]
        if not isinstance(sensor, dict) or not all(
            isinstance(sensor.get(k), (int, float)) and math.isfinite(sensor[k]) and sensor[k] > 0
            for k in ("cx", "cy")
        ):
            raise UnsupportedCorrection("BF camera sensor dimensions are unavailable")
        if sizes["Y"] <= 1.5 * sensor["cy"] or sizes["X"] <= 1.5 * sensor["cx"]:
            raise UnsupportedCorrection(
                "The source does not contain enough stitched fields for Shading Correction"
            )
    if _source_stamp(path) != before:
        raise UnsupportedCorrection("The ND2 source changed during metadata inspection")
    return {
        "sizes": sizes,
        "dtype": str(native.dtype),
        "channel_names": names,
        "channel_mapping": mapping,
        "channel_acquisition_settings": settings,
        "source_stamp": before,
    }


def _mean_blocks(values: np.ndarray, factor: int) -> np.ndarray:
    """Float64 sums / true edge counts, cast once; fit keeps partial bins."""
    h, w = values.shape
    yy, xx = np.arange(0, h, factor), np.arange(0, w, factor)
    sums = np.add.reduceat(
        np.add.reduceat(values, xx, axis=1, dtype=np.float64), yy, axis=0, dtype=np.float64
    )
    areas = np.minimum(factor, h - yy)[:, None] * np.minimum(factor, w - xx)[None, :]
    return (sums / areas).astype(np.float32)


def _close_maps(arrays) -> None:
    for array in arrays:
        mmap = getattr(array, "_mmap", None)
        if mmap is not None:
            mmap.close()


def _new_map(path: Path, shape: tuple) -> np.memmap:
    return np.lib.format.open_memmap(path, mode="w+", dtype=np.float32, shape=shape)


def _block_geometry(state):
    """Align common cached chunks without allowing unbounded strip reads."""
    rows, cols = _BLOCK_ROWS, _BLOCK_COLS
    chunks = getattr(state.root["0"], "chunks", None)
    if isinstance(chunks, tuple) and len(chunks) == 3:
        cy, cx = chunks[-2:]
        if isinstance(cy, int) and 32 <= cy <= 1024 and cy % 32 == 0:
            rows = max(rows, cy)
        if isinstance(cx, int) and 32 <= cx <= _BLOCK_COLS and cx % 32 == 0:
            cols = (_BLOCK_COLS // cx) * cx
    return rows, cols


def _fit_model(raw8, raw32, metadata, output, check, progress):
    from .h_model import Unsupported, fit

    config = json.loads(Path(__file__).with_name("h_config.json").read_text(encoding="utf-8"))
    try:
        return fit(
            raw8,
            raw32,
            metadata,
            config,
            output,
            check=check,
            max_working_bytes=_FIT_MEMORY_LIMIT,
            progress=progress,
        )
    except Unsupported as exc:
        raise UnsupportedCorrection(str(exc)) from exc


def _prepare_inputs(state, directory: Path, read_native, check, progress):
    c, h, w = state.root["0"].shape
    rows, cols = _block_geometry(state)
    arrays = []
    try:
        raw8 = _new_map(directory / "raw8.npy", (c, -(-h // 8), -(-w // 8)))
        arrays.append(raw8)
        raw32 = _new_map(directory / "raw32.npy", (c, -(-h // 32), -(-w // 32)))
        arrays.append(raw32)
        for y in range(0, h, rows):
            check()
            for x in range(0, w, cols):
                for ci in range(c):
                    check()
                    block = read_native(ci, y, min(y + rows, h), x, min(x + cols, w))
                    for factor, array in ((8, raw8), (32, raw32)):
                        mean = _mean_blocks(block, factor)
                        array[
                            ci,
                            y // factor : y // factor + mean.shape[0],
                            x // factor : x // factor + mean.shape[1],
                        ] = mean
            progress(5 + 35 * min(y + rows, h) / h)
        raw8.flush()
        raw32.flush()
        return raw8, raw32
    except BaseException:
        _close_maps(arrays)
        raise


class HCorrectionController:
    """One asynchronous, source-bound cache transaction for an open slide."""

    def __init__(self, state):
        self.state = state
        self.generation = state.generation
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._closed = False
        self._started_at = self._finished_at = None
        reason = _basic_reason(state)
        self._estimated_seconds = None if reason else _estimated_seconds(state)
        self._status = {
            "state": "unsupported" if reason else "idle", "supported": not bool(reason),
            "reason": reason, "pct": 0, "operation": None, "frame": None,
        }
        self._sync_committed()
        if self._status["applied"] and not reason:
            self._status.update(state="ready", pct=100)

    def _sync_committed(self):
        correction = self.state.manifest.get("h_correction") or {}
        indices = correction.get("channel_indices", [])
        names = self.state.attrs.get("omero", {}).get("channels", [])
        self._status.update(
            applied=bool(correction.get("applied")),
            correction_id=correction.get("correction_id"),
            channels=[names[i]["label"] for i in indices if 0 <= i < len(names)],
            generation=self.state.generation,
        )

    def status(self) -> dict:
        with self._lock:
            active = self._status["state"] in _ACTIVE
        if not active and not self._closed and not (self._thread and self._thread.is_alive()):
            refresh = getattr(self.state, "refresh_pyramid", None)
            if refresh is not None:
                refresh()
        with self._lock:
            old_generation = self._status.get("generation")
            self._sync_committed()
            if old_generation != self.state.generation and not active:
                self.generation = self.state.generation
                self._status.update(state="ready" if self._status["applied"] else "idle",
                                    reason="", pct=100 if self._status["applied"] else 0,
                                    operation=None)
            result = json.loads(json.dumps(self._status))
            estimate = self._estimated_seconds
            if result["applied"] and estimate is not None:
                # Rebuild avoids model fitting; use the same conservative I/O estimate.
                estimate = max(30, math.ceil(estimate * 0.75 / 15) * 15)
            result["estimated_seconds"] = estimate if result["supported"] else None
            elapsed = (0 if self._started_at is None else
                       max(0, (self._finished_at or time.monotonic()) - self._started_at))
            result["elapsed_seconds"] = round(elapsed, 1)
            pct = result["pct"]
            result["eta_seconds"] = (round(elapsed * (100 - pct) / pct)
                if result["state"] in _ACTIVE and pct >= 5 and elapsed >= 2 else None)
            result["can_cancel"] = result["state"] in _ACTIVE - {"publishing"}
            return result

    def _update(self, event, **values):
        with self._lock:
            if event is self._cancel and not self._closed and not event.is_set():
                self._status.update(values)
                if self._status["state"] not in _ACTIVE:
                    self._finished_at = time.monotonic()

    def start(self):
        return self._start("apply")

    def revoke(self):
        return self._start("revoke")

    def _start(self, operation):
        self.status()  # Observe another window's committed cache first.
        with self._lock:
            if self._closed:
                raise ValueError("This slide's Shading Correction controller is closed")
            if self._status["state"] in _ACTIVE or (self._thread and self._thread.is_alive()):
                return self.status()
            if not self._status["supported"]:
                return self.status()
            if (operation == "apply") == self._status["applied"]:
                raise ValueError("Revoke the applied correction first" if operation == "apply"
                                 else "There is no applied Shading Correction to revoke")
            self.generation = self.state.generation
            self._cancel = threading.Event()
            self._started_at, self._finished_at = time.monotonic(), None
            self._status.update(state="preparing" if operation == "apply" else "rebuilding",
                                operation=operation, reason="", pct=0)
            self._status.pop("channel_results", None)
            self._thread = threading.Thread(target=self._run, args=(self._cancel, operation),
                                            daemon=True, name="h-correction")
            self._thread.start()
            return self.status()

    def cancel(self):
        with self._lock:
            if self._status["state"] in _ACTIVE - {"publishing"}:
                self._cancel.set()
                self._finished_at = time.monotonic()
                self._status.update(state="cancelled", reason="Pyramid replacement cancelled")
            return self.status()

    def close(self):
        with self._lock:
            self._closed = True
            if self._status["state"] != "publishing":
                self._cancel.set()
            self._finished_at = time.monotonic()
            self._status.update(state="cancelled", reason="Slide closed")

    def _run(self, event, operation):
        from .h_persistence import PreparationCancelled, prepare_apply, prepare_revoke

        temporary = model = replacement = None
        inputs = []
        stamp = None

        def check():
            if event.is_set() or self._closed:
                raise _Cancelled()

        def check_source():
            check()
            if self.state.generation != self.generation or (
                    stamp is not None and _source_stamp(Path(self.state.source_path)) != stamp):
                raise UnsupportedCorrection("The ND2 source changed; reopen the slide")

        def read_native(ci, y0, y1, x0, x1):
            check_source()
            with self.state.busy:
                check()
                native = getattr(self.state.root, "raw_level0", self.state.root["0"])
                return np.array(native[ci, y0:y1, x0:x1], copy=True)

        try:
            check()
            if operation == "revoke":
                replacement = prepare_revoke(self.state, event,
                    lambda fraction: self._update(event, pct=round(95 * fraction)))
            else:
                meta = _metadata_for_state(self.state)
                stamp = tuple(meta["source_stamp"])
                check_source()
                c, h, w = self.state.root["0"].shape
                estimated = c * (-(-h // 8) * -(-w // 8) + -(-h // 32) * -(-w // 32)) * 4 + 16 * 1024**2
                if estimated > _WORKSPACE_LIMIT:
                    raise UnsupportedCorrection("This acquisition exceeds the correction fitting workspace limit")
                temporary = tempfile.TemporaryDirectory(prefix="nd2wsi-h-")
                directory = Path(temporary.name)
                if shutil.disk_usage(directory).free < estimated + 256 * 1024**2:
                    raise UnsupportedCorrection("There is insufficient temporary space for Shading Correction")
                raw8, raw32 = _prepare_inputs(self.state, directory, read_native, check_source,
                    lambda pct: self._update(event, pct=round(pct)))
                inputs = [raw8, raw32]
                check_source()
                self._update(event, state="fitting", pct=40)
                model, result = _fit_model(raw8, raw32, meta, directory / "model.npz", check,
                    lambda fraction: self._update(event, pct=round(40 + 15 * fraction)))
                _close_maps(inputs)
                inputs = []
                for name in ("raw8.npy", "raw32.npy"):
                    (directory / name).unlink()
                check_source()
                self._update(event, channel_results=result["selected"])
                if not any(model.enabled):
                    self._update(event, state="unchanged", pct=100,
                                 reason="No reproducible, valid FITC/CY3 correction was found")
                    return
                self._update(event, state="preparing_display", pct=55)
                replacement = prepare_apply(self.state, model, result, event,
                    lambda fraction: self._update(event, pct=round(55 + 40 * fraction)))
            check_source()
            with self._lock:
                check()
                self._status.update(state="publishing", pct=98)
            self.state.replace_pyramid(replacement)
            with self._lock:
                self.generation = self.state.generation
                self._sync_committed()
                self._status.update(state="ready" if operation == "apply" else "idle", pct=100,
                                    reason="", operation=None)
                self._finished_at = time.monotonic()
        except (_Cancelled, PreparationCancelled):
            with self._lock:
                if event is self._cancel:
                    self._status.update(state="cancelled",
                        reason="Slide closed" if self._closed else "Pyramid replacement cancelled")
        except UnsupportedCorrection as exc:
            self._update(event, state="unsupported", supported=False, reason=str(exc))
        except Exception as exc:
            self._update(event, state="failed", reason=f"{type(exc).__name__}: {exc}")
        finally:
            _close_maps(inputs)
            if replacement is not None:
                replacement.close()
            if model is not None and getattr(model, "data", None) is not None:
                model.data.close()
            if temporary is not None:
                temporary.cleanup()
