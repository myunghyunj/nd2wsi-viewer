"""Private, reversible Shading Correction for one verified stitched ND2 raster.

The registered raw root remains authoritative. Fit inputs are unrounded native
8x/32x means; corrected display means are made only *after* native correction.
Temporary arrays belong to this controller, never to the shared slide cache.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from .direct import _Lifecycle, _parse_cyx_index

_ALIASES = {
    "FITC": "FITC",
    "CY3": "CY3",
    "DAPI": "DAPI",
    "BF": "BF",
    "BRIGHTFIELD": "BF",
    "BRIGHT FIELD": "BF",
}
_ACTIVE = {"preparing", "fitting", "preparing_display"}
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
        arr = state.root["0"]
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
        native = state.root["0"]
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


def _prepare_coarse(state, model, indices, directory, read_native, check, progress):
    """Store enabled channels only, starting with native-corrected 8x means."""
    levels = state.attrs["nd2wsi"]["levels"]
    max_level = max(int(level["path"]) for level in levels)
    maps = {}
    if max_level < 3:
        return maps
    _, h, w = state.root["0"].shape
    rows, cols = _block_geometry(state)
    bh, bw = h // 8, w // 8
    try:
        coarse = _new_map(directory / "corrected3.npy", (len(indices), bh, bw))
        maps["3"] = coarse
        for y in range(0, bh * 8, rows):
            check()
            for x in range(0, bw * 8, cols):
                for slot, ci in enumerate(indices):
                    check()
                    block = read_native(ci, y, min(y + rows, bh * 8), x, min(x + cols, bw * 8))
                    corrected = model.correct(ci, block, y, x)
                    mean = _mean_blocks(corrected, 8)
                    coarse[
                        slot, y // 8 : y // 8 + mean.shape[0], x // 8 : x // 8 + mean.shape[1]
                    ] = mean
            progress(55 + 40 * min(y + rows, bh * 8) / (bh * 8))
        coarse.flush()
        for level in range(4, max_level + 1):
            check()
            previous = maps[str(level - 1)]
            _, ph, pw = previous.shape
            nh, nw = max(1, ph // 2), max(1, pw // 2)
            target = _new_map(directory / f"corrected{level}.npy", (len(indices), nh, nw))
            maps[str(level)] = target
            for y in range(0, nh, _BLOCK_ROWS):
                check()
                y1 = min(y + _BLOCK_ROWS, nh)
                for x in range(0, nw, _BLOCK_COLS):
                    x1 = min(x + _BLOCK_COLS, nw)
                    for slot in range(len(indices)):
                        check()
                        block = np.asarray(
                            previous[slot, y * 2 : min(y1 * 2, ph), x * 2 : min(x1 * 2, pw)]
                        )
                        # Carry a collapsed axis forward exactly, as the raw
                        # viewer pyramid does, instead of averaging with zero.
                        if ph == 1:
                            block = np.repeat(block, 2, axis=0)
                        if pw == 1:
                            block = np.repeat(block, 2, axis=1)
                        target[slot, y:y1, x:x1] = _mean_blocks(block, 2)
            target.flush()
        return maps
    except BaseException:
        _close_maps(maps.values())
        raise


class _CorrectedLevel:
    def __init__(self, owner, path, shape, factor):
        self.owner, self.path, self.shape, self.factor = owner, path, tuple(shape), factor
        self.dtype = np.dtype("float32")

    def __getitem__(self, idx):
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        owner = self.owner
        with owner.life, owner.state.busy:
            owner.check_source()
            out = np.array(
                owner.state.root[self.path][cs, y0:y1, x0:x1], dtype=np.float32, copy=True
            )
            selected = np.arange(self.shape[0])[cs]
            scalar = np.ndim(selected) == 0
            selected = np.atleast_1d(selected)
            result = out[None, ...] if scalar else out
            for slot, ci in enumerate(selected):
                ci = int(ci)
                if ci not in owner.indices or y1 == y0 or x1 == x0:
                    continue
                if self.path in owner.coarse:
                    result[slot] = owner.coarse[self.path][owner.indices.index(ci), y0:y1, x0:x1]
                    continue
                factor = self.factor
                rows, cols = max(1, _BLOCK_ROWS // factor), max(1, _BLOCK_COLS // factor)
                for y in range(y0, y1, rows):
                    for x in range(x0, x1, cols):
                        ey, ex = min(y + rows, y1), min(x + cols, x1)
                        native = owner.state.root["0"][
                            ci, y * factor : ey * factor, x * factor : ex * factor
                        ]
                        corrected = owner.model.correct(ci, native, y * factor, x * factor)
                        result[slot, y - y0 : ey - y0, x - x0 : ex - x0] = (
                            corrected if factor == 1 else _mean_blocks(corrected, factor)
                        )
            return out


class _CorrectedRoot(dict):
    """An immutable revision. Every returned array owns its pixel memory."""

    def __init__(self, state, model, indices, coarse, temporary, stamp, generation):
        self.state, self.model, self.indices = state, model, list(indices)
        self.coarse, self.temporary = coarse, temporary
        self.stamp, self.generation = stamp, generation
        self.life = _Lifecycle()
        self._close_lock = threading.Lock()
        self._closed = False
        super().__init__(
            {
                str(lv["path"]): _CorrectedLevel(
                    self, str(lv["path"]), state.root[str(lv["path"])].shape, int(lv["downsample"])
                )
                for lv in state.attrs["nd2wsi"]["levels"]
            }
        )

    def check_source(self):
        try:
            current = _source_stamp(Path(self.state.source_path))
        except OSError as exc:
            raise ValueError(
                "The Shading Correction source is unavailable; reopen the slide"
            ) from exc
        if self.state.generation != self.generation or current != self.stamp:
            raise ValueError("The Shading Correction source changed; reopen the slide")

    def _cleanup(self):
        _close_maps(self.coarse.values())
        data = getattr(self.model, "data", None)
        if data is not None:
            data.close()
        self.temporary.cleanup()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self.life.close(timeout=0):
            self._cleanup()
        else:

            def drain():
                self.life.close(timeout=None)
                self._cleanup()

            threading.Thread(target=drain, daemon=True, name="h-cache-close").start()


class HCorrectionController:
    """One asynchronous fit and private display revision per open slide."""

    def __init__(self, state):
        self.state = state
        self.generation = state.generation
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._root = None
        self._closed = False
        self._started_at = None
        self._finished_at = None
        reason = _basic_reason(state)
        self._estimated_seconds = None if reason else _estimated_seconds(state)
        self._status = {
            "state": "unsupported" if reason else "idle",
            "supported": not bool(reason),
            "reason": reason,
            "pct": 0,
            "correction_id": None,
            "channels": [],
            "generation": self.generation,
            "frame": None,
        }

    def status(self) -> dict:
        with self._lock:
            # All public values are JSON-compatible, including fit results.
            result = json.loads(json.dumps(self._status))
            # Approximate total duration for the first-click confirmation.
            result["estimated_seconds"] = self._estimated_seconds if result["supported"] else None
            elapsed = (
                0
                if self._started_at is None
                else max(0, (self._finished_at or time.monotonic()) - self._started_at)
            )
            result["elapsed_seconds"] = round(elapsed, 1)
            pct = result["pct"]
            result["eta_seconds"] = (
                round(elapsed * (100 - pct) / pct)
                if result["state"] in _ACTIVE and pct >= 5 and elapsed >= 2
                else None
            )
            return result

    def _update(self, event, **values):
        with self._lock:
            if event is self._cancel and not self._closed and not event.is_set():
                self._status.update(values)
                if self._status["state"] not in _ACTIVE:
                    self._finished_at = time.monotonic()

    def start(self) -> dict:
        with self._lock:
            if self._closed:
                raise ValueError("This slide's Shading Correction controller is closed")
            if self._status["state"] in _ACTIVE | {"ready", "unchanged", "unsupported"}:
                return self.status()
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._cancel = threading.Event()
            self._started_at, self._finished_at = time.monotonic(), None
            self._status.update(
                state="preparing", supported=True, reason="", pct=0, correction_id=None, channels=[]
            )
            self._status.pop("channel_results", None)
            self._thread = threading.Thread(
                target=self._run, args=(self._cancel,), daemon=True, name="h-correction"
            )
            self._thread.start()
            return self.status()

    def cancel(self) -> dict:
        with self._lock:
            if self._status["state"] in _ACTIVE:
                self._cancel.set()
                self._finished_at = time.monotonic()
                self._status.update(
                    state="cancelled", reason="Correction cancelled", correction_id=None
                )
            return self.status()

    def root_for(self, correction_id: str):
        with self._lock:
            if (
                self._closed
                or self._status["state"] != "ready"
                or not correction_id
                or correction_id != self._status["correction_id"]
                or self._root is None
            ):
                raise ValueError(
                    "This Shading Correction revision is unavailable; return to RAW or refit"
                )
            self._root.check_source()
            return self._root

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel.set()
            self._finished_at = time.monotonic()
            self._status.update(state="cancelled", reason="Slide closed", correction_id=None)
            root, self._root = self._root, None
        if root is not None:
            root.close()
        # A fitting worker owns its maps and cleans them in finally. It reads
        # the source only inside a short busy guard and checks cancellation
        # again after admission. Closing never waits for an optimizer.

    def _run(self, event):
        temporary, model, root = None, None, None
        inputs, coarse = [], {}
        stamp = None

        def check():
            if event.is_set() or self._closed:
                raise _Cancelled()

        def check_source():
            check()
            if self.state.generation != self.generation or (
                stamp is not None and _source_stamp(Path(self.state.source_path)) != stamp
            ):
                raise UnsupportedCorrection("The ND2 source changed; reopen the slide")

        def read_native(ci, y0, y1, x0, x1):
            check_source()
            with self.state.busy:
                check()
                return np.asarray(self.state.root["0"][ci, y0:y1, x0:x1]).copy()

        try:
            check()
            meta = _metadata_for_state(self.state)
            stamp = tuple(meta["source_stamp"])
            check_source()
            c, h, w = self.state.root["0"].shape
            estimated = (
                c * (-(-h // 8) * -(-w // 8) + -(-h // 32) * -(-w // 32))
                + 2 * (h // 8) * (w // 8) * 4 / 3
            ) * 4 + 16 * 1024**2
            if estimated > _WORKSPACE_LIMIT:
                raise UnsupportedCorrection(
                    "This acquisition exceeds the private correction workspace limit"
                )
            temporary = tempfile.TemporaryDirectory(prefix="nd2wsi-h-")
            directory = Path(temporary.name)
            if shutil.disk_usage(directory).free < estimated + 256 * 1024**2:
                raise UnsupportedCorrection(
                    "There is insufficient temporary space for Shading Correction"
                )
            raw8, raw32 = _prepare_inputs(
                self.state,
                directory,
                read_native,
                check_source,
                lambda pct: self._update(event, pct=round(pct)),
            )
            inputs = [raw8, raw32]
            check_source()
            self._update(event, state="fitting", pct=40)
            model, result = _fit_model(
                raw8,
                raw32,
                meta,
                directory / "model.npz",
                check,
                lambda fraction: self._update(event, pct=round(40 + 15 * fraction)),
            )
            _close_maps(inputs)
            inputs = []
            for name in ("raw8.npy", "raw32.npy"):
                (directory / name).unlink()
            check_source()
            indices = [ci for ci, enabled in zip(model.indices, model.enabled) if enabled]
            channels = [
                name for name in ("FITC", "CY3") if meta["channel_mapping"].get(name) in indices
            ]
            self._update(event, channel_results=result["selected"], channels=channels)
            if not indices:
                self._update(
                    event,
                    state="unchanged",
                    pct=100,
                    reason="No reproducible, valid FITC/CY3 correction was found",
                )
                return
            self._update(event, state="preparing_display", pct=55)
            coarse = _prepare_coarse(
                self.state,
                model,
                indices,
                directory,
                read_native,
                check_source,
                lambda pct: self._update(event, pct=round(pct)),
            )
            check_source()
            root = _CorrectedRoot(
                self.state, model, indices, coarse, temporary, stamp, self.generation
            )
            # Ownership transfers as one unit; cancellation cannot delete a
            # published revision or publish a partially prepared cache.
            model, coarse, temporary = None, {}, None
            with self._lock:
                check()
                self._root = root
                self._status.update(
                    state="ready", pct=100, reason="", correction_id=uuid.uuid4().hex
                )
                self._finished_at = time.monotonic()
                root = None
        except _Cancelled:
            with self._lock:
                if event is self._cancel:
                    self._status.update(
                        state="cancelled",
                        reason="Slide closed" if self._closed else "Correction cancelled",
                        correction_id=None,
                    )
        except UnsupportedCorrection as exc:
            self._update(
                event, state="unsupported", supported=False, reason=str(exc), correction_id=None
            )
        except Exception as exc:
            self._update(
                event, state="failed", reason=f"{type(exc).__name__}: {exc}", correction_id=None
            )
        finally:
            _close_maps(inputs)
            _close_maps(coarse.values())
            if model is not None:
                data = getattr(model, "data", None)
                if data is not None:
                    data.close()
            if root is not None:
                root.close()
            if temporary is not None:
                temporary.cleanup()
