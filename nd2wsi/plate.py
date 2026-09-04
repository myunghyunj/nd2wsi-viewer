"""Plate mode. A time series of camera fields, served from one open ND2.

A multipoint time lapse with a z stack holds thousands of small frames,
one camera field each, and no stitching. Building a pyramid cache of one
plane would be useless for such a file, so this module keeps the ND2 open
and reads frames straight from its memory map. Reduced frames feed the
site grid, and one site at a time can be viewed at full resolution
through the same tile pipeline the stitched slides use.

Reduced grid frames are persisted in a managed thumbnail cache beside the
source and also live in a process-wide, bytes-bounded memory cache.  The
source ND2 remains authoritative: an incomplete or unverifiable thumbnail
falls back to the corresponding source frame and is repaired by the one
viewer that owns the cache writer lease.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
import warnings
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

import numpy as np

from . import render
from .cache import (
    CACHE_SUFFIX,
    CACHES_DIR,
    MANIFEST_NAME,
    CacheLock,
    fingerprints_match,
    managed_dir,
    quarantine,
    quick_fingerprint,
    source_tag,
)
from .direct import _Lifecycle, _parse_cyx_index, _Root, _TileCache
from .plate_integrity import (
    DIGEST_ALGORITHM,
    DIGEST_NAME,
    UNCOMMITTED_DIGEST,
    digest_matches,
    ensure_digest_array,
    frame_digest,
    quarantine_chunk,
    zarr_v2_chunk_path,
)
from .reader import (
    FRAME_AXES,
    PlaneSource,
    _channel_infos,
    _frame_to_cyx,
    _nd2_pixel_size,
    _seq_index,
    level_shapes,
    objective_magnification,
)
from .session_lock import SessionFileLock

PLATE_MAX_FRAME_PX = 4_500_000  # a camera field, not a stitched scan
PLATE_CACHE_BYTES = 768 * 1024 * 1024  # reduced frames kept in memory
FRAME_CACHE_BYTES = 320 * 1024 * 1024  # whole frames kept for the focused site
PLATE_K = (2, 4, 8, 16)  # box mean factors the grid may ask for
PREFETCH_PENDING_MAX = 256  # frames queued for warming, oldest dropped first
HISTOGRAM_LRU = 64
THUMB_K = 8  # the reduction the store keeps, the one the site grid shows
UNSCORED = -1.0  # a frame whose sharpness has not been measured yet
# Keep the container format readable by v1.1.0. That build quarantines any
# unfamiliar format before it checks the writer lock, so changing this string
# in place would let an old app replace a live new cache. Integrity is an
# additive manifest capability instead.
PLATE_FORMAT = "nd2wsi-plate/1"
PLATE_TAG = "plate"
THUMBS_NAME = "thumbs.zarr"
READER_REFRESH_S = 0.5

# one reduction budget for the whole process, not one per open file
_REDUCED_CACHE = _TileCache(PLATE_CACHE_BYTES)
# whole frames read for the focused site or for a reduction, a few at a time
_FRAME_CACHE = _TileCache(FRAME_CACHE_BYTES)


def _has_integrity(manifest: dict[str, Any]) -> bool:
    integrity = manifest.get("integrity") or {}
    return (
        integrity.get("algorithm") == DIGEST_ALGORITHM
        and integrity.get("commit") == "payload-digest-done"
        and integrity.get("digest_name") == DIGEST_NAME
    )


def _file_identity(st: os.stat_result) -> tuple[int, int, int, int]:
    """Stable identity used to detect same-size, timestamp-preserving swaps."""
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))


def is_plate_file(path: str | Path) -> bool:
    """True for an uncompressed ND2 of camera fields with a T, P or Z loop."""
    try:
        path = Path(path)
        if path.suffix.lower() != ".nd2" or not path.is_file():
            return False
        import nd2

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with nd2.ND2File(str(path)) as f:
                if getattr(f, "is_legacy", False):
                    return False
                if (getattr(f.attributes, "compressionType", None) or "none") != "none":
                    return False
                sizes = dict(f.sizes)
                area = int(sizes.get("Y", 0)) * int(sizes.get("X", 0))
                if area <= 0 or area > PLATE_MAX_FRAME_PX:
                    return False
                loops = sizes.get("T", 1) * sizes.get("P", 1) * sizes.get("Z", 1)
                return loops > 1
    except Exception:
        return False


def _cluster_1d(values: list[float]) -> list[int]:
    """Cluster index per value, ascending, split where the step between
    neighbours stands out.

    The threshold is half the widest step, not a fraction of the whole
    extent. Measured against the extent, five or more evenly spaced rows
    had no step wide enough to count as a gap and the plate collapsed
    into one track; half the widest step splits them and still holds a
    column together when its sites are spread over a few millimetres.
    The floor keeps the stage's own wobble, tens of micrometres, from
    splitting one row in two.
    """
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    steps = [d for d in (values[b] - values[a] for a, b in zip(order, order[1:])) if d > 0]
    widest = max(steps) if steps else 0.0
    gap = max(1000.0, 0.5 * widest)
    labels = [0] * n
    c = 0
    for prev, cur in zip(order, order[1:]):
        if values[cur] - values[prev] > gap:
            c += 1
        labels[cur] = c
    return labels


def site_layout(points: list[tuple[float, float] | None]) -> list[tuple[int, int]]:
    """(row, col) per stage point. Rows follow ascending Y, columns ascending X.

    Without usable coordinates every site lands in row 0 at its own index.
    """
    n = len(points)
    if n == 0:
        return []
    xs: list[float] = []
    ys: list[float] = []
    for pt in points:
        try:
            x, y = float(pt[0]), float(pt[1])  # type: ignore[index]
        except (TypeError, ValueError, IndexError):
            return [(0, i) for i in range(n)]
        if not (math.isfinite(x) and math.isfinite(y)):
            return [(0, i) for i in range(n)]
        xs.append(x)
        ys.append(y)
    rows = _cluster_1d(ys)
    cols = _cluster_1d(xs)
    return list(zip(rows, cols))


def _exposure_ms(text_info: dict) -> float | None:
    for key in ("capturing", "description"):
        text = text_info.get(key)
        if not isinstance(text, str):
            continue
        m = re.search(r"Exposure:\s*([0-9]+(?:\.[0-9]+)?)\s*ms", text)
        if m:
            return float(m.group(1))
    return None


def _finite_positive(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _loop(f: Any, kind: str) -> Any:
    try:
        for loop in f.experiment or []:
            if getattr(loop, "type", None) == kind:
                return loop
    except Exception:
        return None
    return None


def _sharpness(a: np.ndarray) -> float:
    """How much fine detail a reduced frame carries.

    The mean squared gradient over the mean level squared. Dividing by the
    level makes the number survive a lamp that drifts over a day, so the
    planes of one site stay comparable from the first hour to the last.
    """
    x = np.asarray(a, dtype=np.float32)
    if x.ndim == 3:
        x = x.mean(axis=0)
    if x.ndim != 2 or x.shape[0] < 2 or x.shape[1] < 2:
        return 0.0
    gy = np.diff(x, axis=0)
    gx = np.diff(x, axis=1)
    energy = float(np.mean(gy * gy)) + float(np.mean(gx * gx))
    level = float(x.mean())
    if not math.isfinite(energy) or not math.isfinite(level):
        return 0.0
    return energy / (level * level + 1e-6)


def plate_container(slide: str | Path) -> Path:
    """Where the thumbnail store of a plate file lives, beside the file."""
    slide = Path(slide)
    return managed_dir(slide) / CACHES_DIR / f"{source_tag(slide)}--{PLATE_TAG}{CACHE_SUFFIX}"


def plate_session_lock_path(container: str | Path) -> Path:
    """Persistent inode used by current builds to arbitrate a plate writer."""
    container = Path(container)
    return container.with_name(container.name + ".writer-session.lock")


class PlateWriterLock:
    """One plate-cache writer, including a bridge for v1.1.0 viewers.

    The kernel lock is authoritative between current processes.  The legacy
    ``CacheLock`` is also held and refreshed so a still-running v1.1.0 app sees
    a live writer rather than starting a second one after its four-hour lease.
    """

    def __init__(self, container: str | Path):
        self.container = Path(container)
        self._session = SessionFileLock(plate_session_lock_path(self.container))
        self._legacy = CacheLock(
            self.container.with_name(self.container.name + ".writer")
        )
        self._acquired = False
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self, timeout: float = 0.0) -> None:
        started = time.monotonic()
        self._session.acquire(timeout=timeout)
        try:
            remaining = max(0.0, float(timeout) - (time.monotonic() - started))
            self._legacy.acquire(timeout=remaining)
        except BaseException:
            self._session.release()
            raise
        self._acquired = True
        self._stop.clear()
        try:
            heartbeat = threading.Thread(
                target=self._renew_legacy,
                name="plate-writer-lock",
                daemon=True,
            )
            self._heartbeat = heartbeat
            heartbeat.start()
        except BaseException:
            # Thread/resource exhaustion must not strand either lease after a
            # caller observes a failed acquire.
            self.release()
            raise

    def _renew_legacy(self) -> None:
        while not self._stop.wait(60.0):
            try:
                if self._legacy.refresh():
                    continue
            except Exception:
                pass
            # A replaced or unrefreshable compatibility lease may mean an old
            # app started writing. Keep the kernel lock, but fail closed and
            # stop admitting writes from this process.
            self._acquired = False
            return

    def release(self) -> None:
        self._acquired = False
        self._stop.set()
        heartbeat, self._heartbeat = self._heartbeat, None
        try:
            if heartbeat is not None and heartbeat is not threading.current_thread():
                try:
                    heartbeat.join(timeout=1.0)
                except Exception:
                    pass
        finally:
            try:
                self._legacy.release()
            except Exception:
                pass
            try:
                self._session.release()
            except Exception:
                pass

    def __enter__(self) -> PlateWriterLock:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


class PlateStore:
    """The 8x reductions of every frame, kept on disk beside the file.

    One zarr array holds the reductions, chunked one frame at a time, and a
    small ``done`` array records which frames are in. Frames arrive as the
    viewer asks for them and, in the background, as a builder walks every
    frame that is still missing, starting near where the viewer is looking.
    The container is keyed by the source fingerprint like every other
    cache, so a drive that moves to another Mac brings the store along.
    """

    def __init__(
        self,
        source: PlateSource,
        container: Path,
        root: Any,
        manifest: dict,
        writer: PlateWriterLock | None,
    ):
        self._source = source
        self.container = container
        self._root = root
        self._thumbs = root["thumbs"]
        self._done = root["done"]
        self.manifest = manifest
        self.total = int(source.T * source.P * source.Z)
        self._lock = threading.RLock()
        self._done_np = np.asarray(self._done[:]).astype(bool)
        try:
            self._digest = root[DIGEST_NAME]
            self._digest_np = np.asarray(self._digest[:], dtype=np.uint64)
        except Exception:
            self._digest = None
            self._digest_np = np.zeros(self._done_np.shape, dtype=np.uint64)
        self._invalid = np.zeros(self._done_np.shape, dtype=bool)
        self._integrity_errors = 0
        self._container_current = True
        self._last_generation_check = time.monotonic()
        self._last_refresh = time.monotonic()
        self._focus_dirty = 0
        try:
            self._focus = root["focus"]
            self._focus_np = np.asarray(self._focus[:], dtype=np.float32)
        except Exception:
            self._focus = None
            self._focus_np = np.full(
                (source.T, source.P, source.Z), UNSCORED, dtype=np.float32
            )
        self._cursor = (0, source.z_home)
        self._warmed = False  # the warm pass has run to the end once
        self._writer = writer
        self._stop = False
        self._thread: threading.Thread | None = None
        self._start_stop_lock = threading.Lock()

    # ---- open or create -------------------------------------------------
    @classmethod
    def open_or_create(cls, source: PlateSource) -> PlateStore:
        import zarr

        container = plate_container(source.path)
        fingerprint = quick_fingerprint(source.path)
        c, h, w = source.frame_shape
        shape = (source.T, source.P, source.Z, c, h // THUMB_K, w // THUMB_K)
        container.parent.mkdir(parents=True, exist_ok=True)
        writer = PlateWriterLock(container)
        try:
            writer.acquire(timeout=0.0)
        except (TimeoutError, OSError):
            writer = None

        build_lock = CacheLock(container)
        try:
            build_lock.acquire(timeout=30.0)
            manifest = cls._read(container)
            if manifest is not None and not cls._matches(
                manifest, fingerprint, shape, source
            ):
                if writer is None:
                    raise RuntimeError(
                        "thumbnail cache changed while another viewer owns its writer"
                    )
                quarantine(container)
                manifest = None
            if manifest is None:
                if writer is None:
                    raise RuntimeError(
                        "thumbnail cache is not ready while another viewer owns its writer"
                    )
                manifest = cls._create(source, container, fingerprint, shape)

            mode = "r+" if writer is not None else "r"
            expected = (1, shape[1], 1, shape[3], shape[4], shape[5])
            try:
                root = zarr.open_group(
                    str(container / THUMBS_NAME), mode=mode, zarr_format=2
                )
                chunks = tuple(root["thumbs"].chunks)
            except Exception:
                root = None
                chunks = None
            if chunks is not None and chunks != expected:
                if writer is None:
                    raise RuntimeError(
                        "thumbnail cache needs rebuilding by its writer"
                    )
                # The first release candidate wrote one allocation-heavy file
                # per site. Keep the whole superseded container rather than
                # recursively deleting it: an old build may have placed user
                # annotations inside what otherwise looks like cache data.
                quarantine(container)
                manifest = cls._create(source, container, fingerprint, shape)
                root = zarr.open_group(
                    str(container / THUMBS_NAME), mode="r+", zarr_format=2
                )
            elif root is None:
                if writer is None:
                    raise RuntimeError("thumbnail cache is structurally unreadable")
                quarantine(container)
                manifest = cls._create(source, container, fingerprint, shape)
                root = zarr.open_group(
                    str(container / THUMBS_NAME), mode="r+", zarr_format=2
                )

            try:
                if writer is not None:
                    cls._ensure_focus(root, shape)
                    ensure_digest_array(root, shape[:3])
                cls._validate_root(
                    root,
                    shape,
                    source.dtype,
                    require_digest=_has_integrity(manifest),
                )
            except Exception:
                if writer is None:
                    raise RuntimeError("thumbnail cache has an invalid array schema") from None
                quarantine(container)
                manifest = cls._create(source, container, fingerprint, shape)
                root = zarr.open_group(
                    str(container / THUMBS_NAME), mode="r+", zarr_format=2
                )
                cls._validate_root(root, shape, source.dtype, require_digest=True)

            store = cls(source, container, root, manifest, writer)
            store.sweep()
        except BaseException:
            if writer is not None:
                writer.release()
            raise
        finally:
            build_lock.release()
        return store

    def sweep(self) -> None:
        """Drop the AppleDouble twins macOS leaves beside every file on
        exFAT and NTFS drives; each one costs a whole allocation block."""
        if not self.writable:
            return  # removing files belongs to the viewer that writes
        try:
            from .convert import sweep_appledouble

            sweep_appledouble(self.container)
        except Exception:
            pass

    @property
    def writable(self) -> bool:
        return (
            self._container_current
            and self._writer is not None
            and self._writer.acquired
        )

    @staticmethod
    def _make_focus(root: Any, shape: tuple) -> None:
        """The sharpness of every frame, one small chunk for the whole
        series. The single definition, used when a store is created and
        when an older one is migrated."""
        root.create_array(
            name="focus",
            shape=shape[:3],
            chunks=shape[:3],
            dtype=np.float32,
            overwrite=True,
            fill_value=UNSCORED,
        )

    @classmethod
    def _ensure_focus(cls, root: Any, shape: tuple) -> None:
        """A store written before autofocus carries no scores; add the array
        empty and the reads fill it in."""
        try:
            root["focus"]
            return
        except KeyError:
            pass
        try:
            cls._make_focus(root, shape)
        except Exception:
            pass  # a store that cannot hold scores only means no autofocus

    @staticmethod
    def _validate_root(
        root: Any, shape: tuple[int, ...], dtype: np.dtype, *, require_digest: bool
    ) -> None:
        thumbs = root["thumbs"]
        done = root["done"]
        if tuple(thumbs.shape) != tuple(shape):
            raise ValueError(
                f"thumbnail shape {tuple(thumbs.shape)!r} does not match {shape!r}"
            )
        if np.dtype(thumbs.dtype) != np.dtype(dtype):
            raise ValueError(
                f"thumbnail dtype {thumbs.dtype!r} does not match {np.dtype(dtype)!r}"
            )
        if tuple(done.shape) != tuple(shape[:3]) or np.dtype(done.dtype) != np.dtype(
            np.uint8
        ):
            raise ValueError("invalid plate done array")
        try:
            focus = root["focus"]
        except KeyError:
            focus = None
        if focus is not None and (
            tuple(focus.shape) != tuple(shape[:3])
            or np.dtype(focus.dtype) != np.dtype(np.float32)
        ):
            raise ValueError("invalid plate focus array")
        try:
            digest = root[DIGEST_NAME]
        except KeyError:
            digest = None
        if require_digest and digest is None:
            raise ValueError("integrity-enabled plate cache has no digest array")
        if digest is not None and (
            tuple(digest.shape) != tuple(shape[:3])
            or np.dtype(digest.dtype) != np.dtype(np.uint64)
        ):
            raise ValueError("invalid plate digest array")

    @staticmethod
    def _read(container: Path) -> dict | None:
        try:
            m = json.loads((container / MANIFEST_NAME).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(m, dict):
            return None
        fmt = str(m.get("format") or "")
        if fmt == PLATE_FORMAT:
            return m
        match = re.fullmatch(r"nd2wsi-plate/(\d+)", fmt)
        if match and int(match.group(1)) > 1:
            raise RuntimeError(
                f"thumbnail cache uses newer format {fmt}; update nd2wsi-viewer"
            )
        return None

    @staticmethod
    def _matches(manifest: dict, fingerprint: dict, shape: tuple, source: PlateSource) -> bool:
        thumbs = manifest.get("thumbs") or {}
        return (
            fingerprints_match(manifest.get("source") or {}, fingerprint)
            and list(thumbs.get("shape") or []) == list(shape)
            and thumbs.get("dtype") == str(source.dtype)
            and thumbs.get("k") == THUMB_K
            and (Path(source.path).parent / (container_name := manifest.get("thumbs_name", THUMBS_NAME)))
            is not None
            and container_name == THUMBS_NAME
        )

    @staticmethod
    def _create(source: PlateSource, container: Path, fingerprint: dict, shape: tuple) -> dict:
        import zarr
        from numcodecs import Blosc

        from . import __version__

        if container.exists():
            quarantine(container)
        container.mkdir(parents=True, exist_ok=True)
        root = zarr.open_group(str(container / THUMBS_NAME), mode="w", zarr_format=2)
        c, h8, w8 = shape[3], shape[4], shape[5]
        # one chunk per time point and z plane holding every site: that is
        # what the grid reads in one go, and it keeps the file count low on
        # exFAT drives whose allocation blocks are a megabyte each
        root.create_array(
            name="thumbs",
            shape=shape,
            chunks=(1, shape[1], 1, c, h8, w8),
            dtype=source.dtype,
            compressors=Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE),
            dimension_names=None,
            overwrite=True,
            fill_value=0,
        )
        root.create_array(
            name="done",
            shape=shape[:3],
            chunks=(1, shape[1], shape[2]),
            dtype=np.uint8,
            overwrite=True,
            fill_value=0,
        )
        ensure_digest_array(root, shape[:3])
        PlateStore._make_focus(root, shape)
        manifest = {
            "format": PLATE_FORMAT,
            "kind": "plate",
            "complete": True,
            "generation": uuid.uuid4().hex,
            "source": {**fingerprint, "relative_path": os.path.relpath(source.path, container)},
            "thumbs": {"k": THUMB_K, "shape": list(shape), "dtype": str(source.dtype)},
            "thumbs_name": THUMBS_NAME,
            "integrity": {
                "algorithm": DIGEST_ALGORITHM,
                "commit": "payload-digest-done",
                "digest_name": DIGEST_NAME,
            },
            "created_by": {"nd2wsi_version": __version__},
        }
        PlateStore._write_manifest(container, manifest)
        return manifest

    @staticmethod
    def _write_manifest(container: Path, manifest: dict) -> None:
        tmp = container / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(manifest, indent=1))
        tmp.replace(container / MANIFEST_NAME)

    # ---- frames -----------------------------------------------------------
    def _check_container_generation(self, *, force: bool = False) -> bool:
        """Whether this open still points at the cache generation it opened.

        Zarr's local store resolves files by path for every read.  If a cache
        directory is atomically moved aside and recreated, an older read-only
        viewer must not silently follow that new directory with stale array
        objects and assumptions.
        """
        if not self._container_current:
            return False
        # A held writer lease prevents an in-protocol replacement.  Readers
        # check at the same cadence as their Zarr progress snapshots so RAM
        # hits do not turn into one manifest read per site.
        if self._writer is not None and self._writer.acquired:
            return True
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and now - self._last_generation_check < READER_REFRESH_S
            ):
                return True
            self._last_generation_check = now
            try:
                current = json.loads((self.container / MANIFEST_NAME).read_text())
                matches = (
                    isinstance(current, dict)
                    and current.get("generation")
                    == self.manifest.get("generation")
                )
            except (OSError, json.JSONDecodeError):
                matches = False
            if matches:
                # Capabilities such as per-frame integrity are published in the
                # manifest last without changing the cache generation.
                self.manifest = current
                return True
            self._container_current = False
            self._done_np.fill(False)
            self._digest_np.fill(UNCOMMITTED_DIGEST)
            self._focus_np.fill(UNSCORED)
        return False

    def _refresh(self, *, force: bool = False) -> None:
        """Refresh a read-only viewer's progress without mutating the store."""
        if not self._check_container_generation(force=force):
            return
        if self.writable:
            return
        now = time.monotonic()
        if not force and now - self._last_refresh < READER_REFRESH_S:
            return
        with self._lock:
            if not force and now - self._last_refresh < READER_REFRESH_S:
                return
            try:
                done = np.asarray(self._done[:], dtype=bool)
                try:
                    digest_array = self._root[DIGEST_NAME]
                    digest = np.asarray(digest_array[:], dtype=np.uint64)
                except Exception:
                    digest_array = None
                    digest = np.zeros(done.shape, dtype=np.uint64)
                try:
                    focus = np.asarray(self._root["focus"][:], dtype=np.float32)
                except Exception:
                    focus = self._focus_np
            except Exception:
                self._last_refresh = now
                return
            self._digest = digest_array
            # Keep repaired candidates visible to ``get`` so they can be
            # revalidated and clear a reader-local invalid mark. Status and
            # ``is_committed`` still exclude invalid entries.
            self._done_np = done
            self._digest_np = digest
            self._focus_np = focus
            self._last_refresh = now

    def _committed_mask(self) -> np.ndarray:
        """Frames this open has source-authenticated commit metadata for."""
        return (
            self._done_np
            & (self._digest_np != UNCOMMITTED_DIGEST)
            & ~self._invalid
        )

    def is_committed(self, t: int, p: int, z: int) -> bool:
        """Whether this open has an effective commit for one frame."""
        self._refresh()
        return bool(self._committed_mask()[t, p, z])

    def _quarantine_shared_payload(self, t: int, z: int) -> bool:
        """Retain one undecodable chunk before a source-backed repair.

        The caller holds ``_lock``, which serializes the failed read and rename
        with every local ``put``. The session writer excludes other current
        processes from mutation, so a newly repaired chunk cannot be moved by
        a stale failure path.
        """
        if not self.writable:
            return False
        try:
            path = zarr_v2_chunk_path(
                self.container / THUMBS_NAME / "thumbs",
                (int(t), 0, int(z), 0, 0, 0),
            )
            return quarantine_chunk(path) is not None
        except Exception:
            return False

    def _invalidate(self, t: int, z: int, bad: np.ndarray) -> None:
        """Fail closed for invalid sites; only the writer persists repair work."""
        # Always detach from ``_done_np``: callers commonly pass a slice, and
        # invalidation mutates that source array before persisting the mask.
        bad = np.array(bad, dtype=bool, copy=True)
        if bad.shape != (self._source.P,) or not bad.any():
            return
        with self._lock:
            newly_bad = bad & ~self._invalid[t, :, z]
            if newly_bad.any():
                self._integrity_errors += 1
            self._invalid[t, bad, z] = True
            self._done_np[t, bad, z] = False
            self._digest_np[t, bad, z] = UNCOMMITTED_DIGEST
            self._focus_np[t, bad, z] = UNSCORED
            if not self.writable:
                return
            for p in np.flatnonzero(bad):
                try:
                    self._done[t, p, z] = 0
                    if self._digest is not None:
                        self._digest[t, p, z] = UNCOMMITTED_DIGEST
                    if self._focus is not None:
                        self._focus[t, p, z] = UNSCORED
                except Exception:
                    # Local invalidation is sufficient for correctness. A
                    # write failure leaves repair pending and source-backed.
                    pass

    def _commit_integrity_capability(self) -> None:
        if not self.writable or _has_integrity(self.manifest):
            return
        committed_without_digest = self._done_np & (
            self._digest_np == UNCOMMITTED_DIGEST
        )
        if committed_without_digest.any():
            return
        manifest = dict(self.manifest)
        manifest["integrity"] = {
            "algorithm": DIGEST_ALGORITHM,
            "commit": "payload-digest-done",
            "digest_name": DIGEST_NAME,
        }
        try:
            self._write_manifest(self.container, manifest)
        except Exception:
            return
        self.manifest = manifest

    def get(self, t: int, p: int, z: int) -> np.ndarray | None:
        """The stored reduction of one site, reading the whole chunk.

        All sites of a time point and plane share one chunk file, and a
        file costs a round trip on an external drive, so the read brings
        every finished site of that chunk into the memory cache at once.
        """
        self._refresh(force=bool(self._invalid[t, p, z]))
        with self._lock:
            # A legacy done bit has no evidence that its cached pixels match the
            # source. Only frames carrying a nonzero digest written from a
            # source-backed ``put`` are candidates; old caches rebuild lazily.
            candidates = np.array(
                self._done_np[t, :, z]
                & (self._digest_np[t, :, z] != UNCOMMITTED_DIGEST),
                dtype=bool,
                copy=True,
            )
            if not candidates[p]:
                return None
            expected = np.array(
                self._digest_np[t, :, z], dtype=np.uint64, copy=True
            )
            try:
                block = np.asarray(self._thumbs[t, :, z])
            except Exception:
                self._quarantine_shared_payload(t, z)
                self._invalidate(t, z, candidates)
                return None

            bad = np.zeros(self._source.P, dtype=bool)
            for p2 in np.flatnonzero(candidates):
                if not digest_matches(block[p2], expected[p2]):
                    bad[p2] = True
            if bad.any():
                # A decodable chunk can still contain wrong pixels. Preserve
                # the exact shared payload before source-backed repair; moving
                # the whole chunk requires invalidating every sibling it held.
                if self._quarantine_shared_payload(t, z):
                    self._invalidate(t, z, candidates)
                    return None
                self._invalidate(t, z, bad)

            verified = candidates & ~bad
            if verified.any():
                self._invalid[t, verified, z] = False

            owner = self._source._owner
            scored = 0
            for p2 in np.flatnonzero(verified):
                key = (owner, int(t), int(p2), int(z), THUMB_K)
                if _REDUCED_CACHE.get(key) is None:
                    _REDUCED_CACHE.put(key, np.ascontiguousarray(block[p2]))
                if self._focus_np[t, p2, z] < 0:
                    self._focus_np[t, p2, z] = _sharpness(block[p2])
                    scored += 1
            if scored:
                self._focus_dirty += scored
                self._flush_focus_if_block_complete(t, z)
            if not verified[p]:
                return None
            return np.ascontiguousarray(block[p])

    def put(self, t: int, p: int, z: int, frame: np.ndarray) -> bool:
        if self.is_committed(t, p, z):
            return True
        if self._stop or not self.writable or self._digest is None:
            return False
        # sites share a chunk, so writes are serialized: two writers
        # rewriting the same chunk would drop each other's site
        with self._lock:
            if self._stop or not self.writable or self._digest is None:
                return False
            if self.is_committed(t, p, z):
                return True
            digest = frame_digest(frame)
            try:
                self._thumbs[t, p, z] = frame
                self._digest[t, p, z] = digest
                self._done[t, p, z] = 1
            except Exception:
                return False  # a store that cannot be written is only slower
            self._digest_np[t, p, z] = digest
            self._done_np[t, p, z] = True
            self._invalid[t, p, z] = False
            if self._focus_np[t, p, z] < 0:
                self._focus_np[t, p, z] = _sharpness(frame)
                self._focus_dirty += 1
            self._commit_integrity_capability()
            self._flush_focus_if_block_complete(t, z)
            return True

    def _flush_focus_if_block_complete(self, t: int, z: int) -> None:
        """Publish focus progress when one shared ``(time, z)`` block lands."""
        committed = self._committed_mask()[t, :, z]
        measured = self._focus_np[t, :, z] >= 0
        if bool((committed & measured).all()):
            self.flush_focus()

    def flush_focus(self) -> None:
        """Write the sharpness scores out. One small file, so this is called
        at the ends of passes rather than on every frame."""
        if not self._focus_dirty or self._focus is None or not self.writable:
            return
        with self._lock:
            if not self.writable:
                return
            try:
                self._focus[:] = self._focus_np
                self._focus_dirty = 0
            except Exception:
                pass

    def focus_map(self) -> dict[str, Any]:
        """Focus progress and the best measured plane for each time and site.

        ``best`` stays provisionally useful for older clients from the first
        stored plane. Consumers applying autofocus must require ``complete``;
        an unmeasured site falls back to the home plane.
        """
        self._refresh()
        committed = self._committed_mask()
        scores = np.where(committed, self._focus_np, UNSCORED)
        home = int(self._source.z_home)
        measured = scores >= 0
        measured_z = measured.sum(axis=2)
        complete = measured_z == int(self._source.Z)
        any_z = measured.any(axis=2)
        best = np.where(any_z, np.where(measured, scores, -np.inf).argmax(axis=2), home)
        return {
            "best": [[int(v) for v in row] for row in best],
            "measured": int(any_z.sum()),
            "total": int(any_z.size),
            "measuredZ": [[int(v) for v in row] for row in measured_z],
            "totalZ": int(self._source.Z),
            "complete": [[bool(v) for v in row] for row in complete],
            "completeCount": int(complete.sum()),
            "zHome": home,
            "scoring": {
                "channelMode": "all-channels-mean-image",
                "metric": "mean-squared-gradient-normalized-by-mean",
            },
        }

    def count(self) -> int:
        self._refresh()
        return int(self._committed_mask().sum())

    def per_t(self) -> list[int]:
        self._refresh()
        committed = self._committed_mask()
        return [
            int(v)
            for v in committed.reshape(committed.shape[0], -1).sum(axis=1)
        ]

    def status(self) -> dict[str, Any]:
        self._refresh()
        with self._lock:
            committed = np.array(self._committed_mask(), copy=True)
            per_t = [
                int(v)
                for v in committed.reshape(committed.shape[0], -1).sum(axis=1)
            ]
            done = int(committed.sum())
            thread = self._thread
            building = bool(thread and thread.is_alive())
            writer = self.writable
            integrity_errors = int(self._integrity_errors)
            repair_pending = int(self._invalid.sum())
            cache_format = self.manifest.get("format")
        return {
            "done": done,
            "total": self.total,
            "perT": per_t,
            "path": str(self.container),
            "format": cache_format,
            "building": building,
            "writer": writer,
            "integrityErrors": integrity_errors,
            "repairPending": repair_pending,
        }

    # ---- builder ----------------------------------------------------------
    def steer(self, t: int, z: int) -> None:
        self._cursor = (int(t), int(z))

    def start(self) -> None:
        """Run the background pass: fill what is missing, then warm.

        A store that is already full still has work, since the warm pass is
        what puts the series in memory and measures the planes, so the
        thread starts whatever the count says. It runs at most once per
        open, so a request that arrives later never sets it going again.
        """
        with self._start_stop_lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    return
                self._thread = None
            if self._stop or self._source._closed:
                return
            if self._warmed and self.count() >= self.total:
                return
            thread = threading.Thread(
                target=self._build, name="plate-store", daemon=True
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                raise

    def _next_missing(self) -> tuple[int, int, int] | None:
        """The first frame not yet stored, walking from the viewer's
        position: this z at this t, then later times, then other planes."""
        T, P, Z = self._done_np.shape
        committed = self._committed_mask()
        t0, z0 = self._cursor
        order_z = [z0] + [z for d in range(1, Z) for z in (z0 + d, z0 - d) if 0 <= z < Z]
        for dt in range(T):
            t = (t0 + dt) % T
            for z in order_z:
                for p in range(P):
                    if not committed[t, p, z]:
                        return (t, p, z)
        return None

    def warm(self) -> None:
        """Read every finished chunk into the memory cache, one file each,
        yielding to requests, so a scrub never waits on the drive."""
        src = self._source
        T, P, Z = self._done_np.shape
        t0, z0 = self._cursor
        order_z = [z0] + [z for d in range(1, Z) for z in (z0 + d, z0 - d) if 0 <= z < Z]
        for dt in range(T):
            t = (t0 + dt) % T
            for z in order_z:
                if self._stop or src._closed:
                    return
                if not self._committed_mask()[t, :, z].all():
                    continue
                if _REDUCED_CACHE.get((src._owner, t, 0, z, THUMB_K)) is not None:
                    continue
                while src._fg > 0 and not self._stop:
                    time.sleep(0.02)
                try:
                    self.get(t, 0, z)
                except Exception:
                    pass
                time.sleep(0.002)
        self.flush_focus()
        if not self._stop and not src._closed:
            self._warmed = True

    def _build(self) -> None:
        """Fill the store frame by frame, then warm what is in it.

        The pass gives up rather than looping when the frames stop
        arriving: ``put`` swallows a write it cannot make, so a container
        that is read only or full would otherwise hand back the same
        missing frame forever, reading it from the ND2 each time.
        """
        src = self._source
        if not self.writable:
            # another viewer owns the container; this one only reads
            self.warm()
            return
        misses = 0
        while not self._stop:
            item = self._next_missing()
            if item is None:
                break
            while src._fg > 0 and not self._stop:
                time.sleep(0.02)
            if self._stop:
                return
            try:
                src.reduced(*item, THUMB_K)
            except Exception:
                if self._stop or src._closed:
                    return
                misses += 1
                if misses >= 8:
                    break  # the file keeps refusing this frame
                time.sleep(0.5)
                continue
            misses = 0
            if not self.is_committed(*item):
                break  # the reduction was made and the store would not take it
            self._since_sweep = getattr(self, "_since_sweep", 0) + 1
            if self._since_sweep >= 64:
                self._since_sweep = 0
                self.sweep()
                self.flush_focus()
            time.sleep(0.005)
        self.flush_focus()
        self.sweep()
        if not self._stop:
            self.warm()

    def close(
        self,
        *,
        release_writer: bool = True,
        timeout: float | None = 30.0,
    ) -> PlateWriterLock | None:
        """Stop cache work and optionally transfer the writer lease.

        Cache trashing detaches the lease and holds it through the atomic
        rename. Normal source closure releases it here.
        """
        with self._start_stop_lock:
            self._stop = True
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
                if thread.is_alive():
                    # A failed trash attempt retains the registered source and
                    # writer. Let its tracked worker continue once the source
                    # lifecycle is reopened; a dead worker can be restarted.
                    if not release_writer:
                        self._stop = False
                    raise RuntimeError("timed out stopping the plate cache worker")
            self.flush_focus()
            writer, self._writer = self._writer, None
            if writer is not None and release_writer:
                writer.release()
                return None
            return writer


class _ArrayLevel:
    """A whole frame held in memory, sliced like a (C, H, W) array."""

    def __init__(self, array: np.ndarray):
        self._a = array
        self.shape = tuple(array.shape)
        self.dtype = array.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        return self._a[cs, y0:y1, x0:x1].copy()


class _ReducedLevel:
    """One pyramid level of a site, sliced like a (C, H//k, W//k) array."""

    def __init__(self, source: PlateSource, t: int, p: int, z: int, k: int):
        self._source = source
        self._tpz = (t, p, z)
        self._k = k
        c, h, w = source.frame_shape
        self.shape = (c, h // k, w // k)
        self.dtype = source.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        t, p, z = self._tpz
        return self._source.reduced(t, p, z, self._k)[cs, y0:y1, x0:x1].copy()


class PlateSource:
    """An open ND2 of camera fields, addressed by (t, p, z).

    A cold frame is read with one sequential read of its bytes, never by
    faulting the memory map page by page, which on an external drive costs
    seconds per frame. The frames of the focused site and the reduced
    frames of the grid live in two process wide caches bounded by bytes.
    The memory map is only touched by ``frame_view`` for callers that ask
    for a zero copy view, inside the lifecycle guard.
    """

    def __init__(self, path: str | Path, tile: int = 512, store: bool = True):
        import nd2

        self.path = Path(path)
        self.tile = int(tile)
        self.store: PlateStore | None = None
        # Process-wide RAM caches must never alias two opens of the same path.
        # This is deliberately not the server-visible source generation.
        self._open_generation = uuid.uuid4().hex
        self._owner = (str(self.path.resolve()), self._open_generation)
        self._life = _Lifecycle()
        self._closed = False
        self._teardown_complete = False
        self._close_lock = threading.Lock()
        self._close_retry_lock = threading.Lock()
        self._close_retry: threading.Thread | None = None
        self._hist_lock = threading.Lock()
        self._hist: OrderedDict[tuple[int, int, int], list[dict]] = OrderedDict()
        self._pf_lock = threading.Lock()
        self._pf_cv = threading.Condition(self._pf_lock)
        self._pf_queue: deque[tuple[int, int, int, int]] = deque()
        self._pf_set: set[tuple[int, int, int, int]] = set()
        self._pf_thread: threading.Thread | None = None
        self._fd = -1
        self._fg = 0  # foreground reads in flight; the prefetch worker yields to them
        self._source_file = open(self.path, "rb")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self._f = nd2.ND2File(self._source_file)
            self._fd = os.dup(self._source_file.fileno())
            opened = os.fstat(self._fd)
            after = os.stat(self.path)
            if _file_identity(opened) != _file_identity(after):
                raise ValueError(
                    "the source file changed while it was opening; retry the open"
                )
            self._init_from_file(self._f)
            self._check_source()
            if store:
                opened_store = None
                try:
                    opened_store = PlateStore.open_or_create(self)
                    self.store = opened_store
                    opened_store.start()
                except Exception as e:  # a store that will not open is only a slower view
                    if opened_store is not None:
                        opened_store.close()
                    self.store = None
                    self.attrs["nd2wsi"].setdefault("notes", []).append(
                        f"thumbnail store unavailable ({type(e).__name__}); frames are read each time"
                    )
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            if hasattr(self, "_f"):
                self._f.close()
            self._source_file.close()
            raise

    # ---- metadata --------------------------------------------------------
    def _init_from_file(self, f: Any) -> None:
        self.sizes = dict(f.sizes)
        self._coord_axes = [a for a in self.sizes if a not in FRAME_AXES]
        self.T = int(self.sizes.get("T", 1))
        self.P = int(self.sizes.get("P", 1))
        self.Z = int(self.sizes.get("Z", 1))
        st = os.fstat(self._fd)
        self._ident = _file_identity(st)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw0 = f.read_frame(0)
        self._raw_shape = tuple(int(v) for v in raw0.shape)
        # the bytes on disk run (H, W, C, components); read_frame reshapes
        # to that and then transposes, so the fast read below must do the
        # same or a multi-channel frame comes out interleaved
        try:
            self._disk_shape: tuple[int, ...] | None = tuple(
                int(v) for v in f._raw_frame_shape
            )
        except Exception:
            self._disk_shape = None
        cyx, rgb = _frame_to_cyx(raw0, self.sizes)
        self.frame_shape = tuple(int(v) for v in cyx.shape)
        self.dtype = np.dtype(cyx.dtype)
        self.rgb = bool(rgb)
        self.channels = _channel_infos(f, self.frame_shape[0], self.rgb)
        self.pixel_size_um, self.calibration_source = _nd2_pixel_size(f)
        self.magnification = objective_magnification(f)

        # sites from the XYPosLoop
        self.sites: list[dict[str, Any]] = []
        points: list[tuple[float, float] | None] = []
        xy = _loop(f, "XYPosLoop")
        raw_points = []
        try:
            raw_points = list(xy.parameters.points) if xy is not None else []
        except Exception:
            raw_points = []
        for p in range(self.P):
            name = f"Site {p + 1}"
            stage = None
            if p < len(raw_points):
                pt = raw_points[p]
                pname = getattr(pt, "name", None)
                if pname:
                    name = str(pname)
                pos = getattr(pt, "stagePositionUm", None)
                try:
                    stage = [float(pos.x), float(pos.y), float(pos.z)]
                    if not all(math.isfinite(v) for v in stage):
                        stage = None
                except (AttributeError, TypeError, ValueError):
                    stage = None
            self.sites.append({"i": p, "name": name, "row": 0, "col": p, "stageUm": stage})
            points.append((stage[0], stage[1]) if stage else None)
        for site, (row, col) in zip(self.sites, site_layout(points)):
            site["row"], site["col"] = int(row), int(col)
        self.rows = 1 + max((s["row"] for s in self.sites), default=0)
        self.cols = 1 + max((s["col"] for s in self.sites), default=0)

        # z from the ZStackLoop
        zl = _loop(f, "ZStackLoop")
        self.z_home = self.Z // 2
        self.z_step_um: float | None = None
        self.bottom_to_top = True
        if zl is not None:
            params = getattr(zl, "parameters", None)
            home = getattr(params, "homeIndex", None)
            if isinstance(home, int) and 0 <= home < self.Z:
                self.z_home = home
            self.z_step_um = _finite_positive(getattr(params, "stepUm", None))
            self.bottom_to_top = bool(getattr(params, "bottomToTop", True))
        if self.z_step_um is None:
            try:
                self.z_step_um = _finite_positive(f.voxel_size().z)
            except Exception:
                self.z_step_um = None

        # time from the TimeLoop and the per frame metadata
        tl = _loop(f, "TimeLoop")
        self.period_ms: float | None = None
        if tl is not None:
            self.period_ms = _finite_positive(
                getattr(getattr(tl, "parameters", None), "periodMs", None)
            )
        self.times_ms: list[float] = []
        for t in range(self.T):
            value = None
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    fm = f.frame_metadata(self.seq(t, 0, 0))
                value = float(fm.channels[0].time.relativeTimeMs)
                if not math.isfinite(value):
                    value = None
            except Exception:
                value = None
            if value is None:
                value = t * self.period_ms if self.period_ms else float(t)
            self.times_ms.append(value)

        try:
            self.exposure_ms = _exposure_ms(dict(f.text_info or {}))
        except Exception:
            self.exposure_ms = None

        _, H, W = self.frame_shape
        shapes = level_shapes(H, W, self.tile)
        self.levels = [
            {"path": str(k), "width": w, "height": h, "downsample": 2**k}
            for k, (h, w) in enumerate(shapes)
        ]
        self.attrs = self._build_attrs(shapes)

    def _build_attrs(self, shapes: list[tuple[int, int]]) -> dict[str, Any]:
        from .convert import _background_mode_start, build_group_attrs

        notes = []
        if self.T > 1 and self.period_ms:
            notes.append(
                f"time series: {self.T} frames every {self.period_ms / 60000:g} min"
            )
        elif self.T > 1:
            notes.append(f"time series: {self.T} frames")
        notes.append(f"{self.P} sites")
        if self.z_step_um:
            notes.append(f"{self.Z} z planes, step {self.z_step_um:g} µm")
        else:
            notes.append(f"{self.Z} z planes")
        if self.pixel_size_um is None:
            notes.append("no pixel calibration in the file; measurements are in pixels")
        notes.append(
            "full-resolution pixels are served from the ND2; reduced grid frames "
            "use a verified managed cache"
        )

        src = PlaneSource(
            data=None,
            dtype=self.dtype,
            shape=self.frame_shape,
            rgb=self.rgb,
            channels=self.channels,
            pixel_size_um=self.pixel_size_um,
            source_name=str(self.path),
            selection={},
            notes=notes,
            magnification=self.magnification,
            calibration_source=self.calibration_source,
        )
        windows = self._windows(_background_mode_start)
        attrs = build_group_attrs(src, shapes, self.tile, windows)
        meta = attrs["nd2wsi"]
        meta["direct"] = True
        meta["kind"] = "plate"
        meta["plate"] = {
            "T": self.T,
            "P": self.P,
            "Z": self.Z,
            "sites": [dict(s) for s in self.sites],
            "rows": self.rows,
            "cols": self.cols,
            "zHome": self.z_home,
            "zStepUm": self.z_step_um,
            "bottomToTop": self.bottom_to_top,
            "timesMs": list(self.times_ms),
            "periodMs": self.period_ms,
            "exposureMs": self.exposure_ms,
            "frameW": self.frame_shape[2],
            "frameH": self.frame_shape[1],
        }
        return attrs

    def _windows(self, background_mode_start: Any) -> list[dict[str, float]]:
        """Display windows from the 4x reductions of every site at t 0, home z.

        A camera field of a plate is phase contrast or bright field as
        often as fluorescence, and its histogram peak is the sample, not a
        black background, so the black point is a low percentile rather
        than the background mode the stitched scans use.
        """
        samples = [self.reduced(0, p, self.z_home, 4) for p in range(self.P)]
        small = np.concatenate([s.reshape(s.shape[0], -1) for s in samples], axis=1)
        if np.issubdtype(self.dtype, np.integer):
            info = np.iinfo(self.dtype)
            lo_all, hi_all = float(info.min), float(info.max)
        else:
            finite = small[np.isfinite(small)]
            lo_all, hi_all = (
                (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
            )
        windows = []
        for ci in range(small.shape[0]):
            if self.rgb:
                start, end = 0.0, 255.0
            else:
                ch = small[ci]
                values = ch[np.isfinite(ch)] if np.issubdtype(ch.dtype, np.floating) else ch
                if values.size:
                    start = float(np.percentile(values, 0.5))
                    end = float(np.percentile(values, 99.8))
                    if end <= start:
                        end = start + 1.0
                else:
                    start, end = 0.0, 1.0
            windows.append({"start": start, "end": end, "min": lo_all, "max": hi_all})
        return windows

    # ---- frames ----------------------------------------------------------
    def seq(self, t: int, p: int, z: int) -> int:
        return _seq_index(self.sizes, self._coord_axes, {"T": t, "P": p, "Z": z})

    def _check_source(self) -> None:
        if self._closed:
            raise ValueError("slide is closed")
        try:
            st = os.stat(self.path)
        except OSError as e:
            raise ValueError(
                f"the source file is unreachable ({e.strerror}); close and reopen the slide"
            ) from e
        if _file_identity(st) != self._ident:
            raise ValueError(
                "the source file changed while it was open; close and reopen the slide"
            )

    def frame_view(self, t: int, p: int, z: int) -> np.ndarray:
        """(C, H, W) zero copy view onto the memory map. Valid until close."""
        with self._life:
            self._check_source()
            seq = self.seq(t, p, z)
            view, _ = _frame_to_cyx(self._f.read_frame(seq), self.sizes)
        return view

    def _read_raw(self, seq: int) -> np.ndarray:
        """The frame's pixels as an array that owns its memory.

        One ``pread`` of the frame's bytes: a whole frame arrives in one
        sequential read instead of thousands of page faults. Falls back to
        a copy out of the memory map when the file pads its rows or the
        reader gives no offset.
        """
        rdr = getattr(self._f, "_rdr", None)
        offset = None
        strides = None
        try:
            offset = rdr._frame_offsets.get(seq) if rdr is not None else None
            strides = rdr._strides if rdr is not None else None
        except Exception:
            offset = None
        nbytes = int(np.prod(self._raw_shape)) * int(self.dtype.itemsize)
        if offset is not None and strides is None and self._fd >= 0 and self._disk_shape:
            buf = os.pread(self._fd, nbytes, int(offset))
            if len(buf) == nbytes:
                flat = np.frombuffer(buf, dtype=self.dtype)
                arr = flat.reshape(self._disk_shape).transpose((2, 0, 1, 3)).squeeze()
                if arr.shape == self._raw_shape:
                    # already contiguous with one channel, copied only for
                    # the interleaved case, which is the one that reorders
                    return np.ascontiguousarray(arr)
        with self._life:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return np.array(self._f.read_frame(seq), copy=True)

    def frame(self, t: int, p: int, z: int) -> np.ndarray:
        """(C, H, W) frame that owns its memory, cached a few at a time."""
        if self._closed:
            raise ValueError("slide is closed")
        with self._life:
            self._check_source()
            key = (self._owner, "frame", int(t), int(p), int(z))
            hit = _FRAME_CACHE.get(key)
            if hit is not None:
                return hit
            seq = self.seq(t, p, z)
            self._fg += 1
            try:
                raw = self._read_raw(seq)
            finally:
                self._fg -= 1
            cyx, _ = _frame_to_cyx(raw, self.sizes)
            cyx = np.ascontiguousarray(cyx)
            if not self._closed:
                _FRAME_CACHE.put(key, cyx)
            return cyx

    def reduced(self, t: int, p: int, z: int, k: int) -> np.ndarray:
        """(C, H//k, W//k) box mean of one frame, rounded to the source dtype."""
        if self._closed:
            raise ValueError("slide is closed")
        with self._life:
            self._check_source()
            k = int(k)
            if k not in PLATE_K:
                raise ValueError(f"k must be one of {PLATE_K}")
            t, p, z = int(t), int(p), int(z)
            key = (self._owner, t, p, z, k)
            hit = _REDUCED_CACHE.get(key)
            if hit is not None:
                if (
                    k == THUMB_K
                    and self.store is not None
                    and not self.store.is_committed(t, p, z)
                    and not self._closed
                ):
                    self.store.put(t, p, z, hit)
                return hit
            if k == THUMB_K and self.store is not None:
                stored = self.store.get(t, p, z)
                if stored is not None:
                    if not self._closed:
                        _REDUCED_CACHE.put(key, stored)
                    return stored
            frame = self.frame(t, p, z)
            c, h, w = frame.shape
            h2, w2 = h // k, w // k
            block = frame[:, : h2 * k, : w2 * k].reshape(c, h2, k, w2, k)
            out = block.mean(axis=(2, 4), dtype=np.float32)
            if np.issubdtype(self.dtype, np.integer):
                out = np.rint(out).astype(self.dtype)
            else:
                out = out.astype(self.dtype)
            out = np.ascontiguousarray(out)
            if not self._closed:
                _REDUCED_CACHE.put(key, out)
                if k == THUMB_K and self.store is not None:
                    self.store.put(t, p, z, out)
            return out

    def status(self) -> dict[str, Any]:
        """How much of the thumbnail store is filled, for the time line."""
        if self.store is None:
            return {
                "done": 0,
                "total": self.T * self.P * self.Z,
                "perT": [0] * self.T,
                "path": None,
                "format": None,
                "building": False,
                "writer": False,
                "integrityErrors": 0,
                "repairPending": 0,
            }
        return self.store.status()

    def focus_map(self) -> dict[str, Any]:
        """Focus progress and provisional best plane per time point and site."""
        # Autofocus scores are derived from source pixels. A same-path source
        # replacement invalidates them just as it invalidates RAM histograms.
        with self._life:
            self._check_source()
            if self.store is None:
                home = int(self.z_home)
                return {
                    "best": [[home] * self.P for _ in range(self.T)],
                    "measured": 0,
                    "total": self.T * self.P,
                    "measuredZ": [[0] * self.P for _ in range(self.T)],
                    "totalZ": self.Z,
                    "complete": [[False] * self.P for _ in range(self.T)],
                    "completeCount": 0,
                    "zHome": home,
                    "scoring": {
                        "channelMode": "all-channels-mean-image",
                        "metric": "mean-squared-gradient-normalized-by-mean",
                    },
                }
            return self.store.focus_map()

    def root_for(self, t: int, p: int, z: int) -> _Root:
        """A virtual pyramid root for one frame, level 0 from the frame cache."""
        levels: dict[str, Any] = {"0": _ArrayLevel(self.frame(t, p, z))}
        for lv in self.levels[1:]:
            levels[lv["path"]] = _ReducedLevel(self, t, p, z, int(lv["downsample"]))
        return _Root(levels, closer=None)

    def histogram(self, t: int, p: int, z: int) -> list[dict]:
        key = (int(t), int(p), int(z))
        # A histogram is derived pixel data just like a frame. Validate the
        # still-open source before serving even a RAM hit so a same-path file
        # replacement cannot leak a result from the previous generation.
        with self._life:
            self._check_source()
            with self._hist_lock:
                hit = self._hist.get(key)
                if hit is not None:
                    self._hist.move_to_end(key)
                    return hit
        # the stored 8x reduction has 65 thousand samples per site, plenty
        # for a 256 bin display histogram, and it never touches the ND2
        # once the store holds it
        small = self.reduced(t, p, z, THUMB_K)
        attrs = dict(self.attrs)
        attrs["nd2wsi"] = {
            **self.attrs["nd2wsi"],
            "levels": [{"path": "0", "width": small.shape[2], "height": small.shape[1], "downsample": THUMB_K}],
        }
        hist = render.compute_histograms({"0": _ArrayLevel(small)}, attrs, min_pixels=1)
        for h in hist:
            h["level"] = str(THUMB_K)
        with self._hist_lock:
            self._hist[key] = hist
            while len(self._hist) > HISTOGRAM_LRU:
                self._hist.popitem(last=False)
        return hist

    def render_frame(
        self,
        t: int,
        p: int,
        z: int,
        k: int,
        channels: list[int],
        fmt: str = "jpg",
        win: str | None = None,
    ) -> bytes:
        self._fg += 1
        try:
            region = self.reduced(t, p, z, k)
        finally:
            self._fg -= 1
        windows, colors = render.display_params(self.attrs)
        windows, gammas = render.parse_windows(win, windows)
        img = render.composite(region, channels, windows, colors, self.rgb, gammas)
        return render.encode_image(img, fmt)

    # ---- prefetch --------------------------------------------------------
    def prefetch(self, t: int, z: int, k: int) -> None:
        """Queue the neighbouring frames for warming. Never blocks, never raises."""
        try:
            k = int(k)
            if k not in PLATE_K or self._closed:
                return
            if self.store is not None:
                self.store.steer(t, z)
                self.store.start()
            wanted: list[tuple[int, int, int, int]] = []
            for p in range(self.P):
                if t + 1 < self.T:
                    wanted.append((t + 1, p, z, k))
                for z2 in (z + 1, z - 1):
                    if 0 <= z2 < self.Z:
                        wanted.append((t, p, z2, k))
            with self._pf_cv:
                for key in wanted:
                    if key in self._pf_set:
                        continue
                    while len(self._pf_queue) >= PREFETCH_PENDING_MAX:
                        old = self._pf_queue.popleft()
                        self._pf_set.discard(old)
                    self._pf_queue.append(key)
                    self._pf_set.add(key)
                if self._pf_thread is None or not self._pf_thread.is_alive():
                    self._pf_thread = threading.Thread(
                        target=self._prefetch_worker, name="plate-prefetch", daemon=True
                    )
                    self._pf_thread.start()
                self._pf_cv.notify()
        except Exception:
            pass

    def _prefetch_worker(self) -> None:
        while True:
            with self._pf_cv:
                while not self._pf_queue and not self._closed:
                    self._pf_cv.wait()
                if self._closed:
                    self._pf_queue.clear()
                    self._pf_set.clear()
                    return
                t, p, z, k = self._pf_queue.popleft()
                self._pf_set.discard((t, p, z, k))
            # a request in flight owns the disk; warming waits its turn
            while self._fg > 0 and not self._closed:
                time.sleep(0.02)
            try:
                if _REDUCED_CACHE.get((self._owner, t, p, z, k)) is None:
                    self.reduced(t, p, z, k)
            except Exception:
                if self._closed:
                    return

    # ---- teardown --------------------------------------------------------
    def _close_source(
        self,
        *,
        release_writer: bool,
        timeout: float | None = 300.0,
        reopen_on_timeout: bool = False,
    ) -> tuple[bool, PlateWriterLock | None]:
        with self._close_lock:
            return self._close_source_locked(
                release_writer=release_writer,
                timeout=timeout,
                reopen_on_timeout=reopen_on_timeout,
            )

    def _close_source_locked(
        self,
        *,
        release_writer: bool,
        timeout: float | None,
        reopen_on_timeout: bool,
    ) -> tuple[bool, PlateWriterLock | None]:
        if self._teardown_complete:
            return True, None
        self._closed = True
        with self._pf_cv:
            self._pf_cv.notify_all()
        if not self._life.close(timeout=timeout):
            # a reader still holds the map after the drain timeout. nd2's
            # frames are views onto the mmap and raise no BufferError on
            # close, so unmapping or releasing the writer now could corrupt
            # data. A failed trash operation keeps the registered source and
            # therefore restores admission. Ordinary close stays barred and
            # lets a daemon finish teardown as soon as the readers drain.
            if reopen_on_timeout:
                self._closed = False
                self._life.reopen()
            else:
                if self._schedule_close_retry():
                    return False, None
                # If the fallback thread itself cannot start, synchronously
                # wait for existing readers. This can delay close, but it
                # guarantees native handles and the writer lease are not lost.
                self._life.close(timeout=None)
            if reopen_on_timeout:
                return False, None

        writer = None
        if self.store is not None:
            try:
                writer = self.store.close(
                    release_writer=release_writer,
                    timeout=timeout,
                )
            except RuntimeError:
                if reopen_on_timeout:
                    self._closed = False
                    self._life.reopen()
                    with self._pf_cv:
                        self._pf_cv.notify_all()
                    raise
                if self._schedule_close_retry():
                    return False, None
                # As above, thread creation failure turns the bounded close
                # into a synchronous safe close instead of leaking the writer.
                writer = self.store.close(
                    release_writer=release_writer,
                    timeout=None,
                )
        prefetch = self._pf_thread
        try:
            # Once store.close() transfers the writer lease, no ordinary
            # handle-cleanup exception may hide that lease from the caller.
            # Drain every resource best-effort and return the escrowed writer so
            # trash_cache retains exclusion through its rename.
            try:
                if prefetch is not None and prefetch.is_alive():
                    prefetch.join(timeout=3.0)
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                self._f.close()
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                self._source_file.close()
            except Exception:  # pragma: no cover - defensive
                pass
            if self._fd >= 0:
                raw_fd, self._fd = self._fd, -1
                try:
                    os.close(raw_fd)
                except OSError:  # pragma: no cover - defensive
                    pass
            try:
                _REDUCED_CACHE.purge(self._owner)
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                _FRAME_CACHE.purge(self._owner)
            except Exception:  # pragma: no cover - defensive
                pass
            self._teardown_complete = True
            return True, writer
        except BaseException:
            # KeyboardInterrupt/SystemExit must propagate, but never with an
            # unreturnable writer lease still held.
            if writer is not None:
                try:
                    writer.release()
                except Exception:
                    pass
            raise

    def _schedule_close_retry(self) -> bool:
        with self._close_retry_lock:
            if self._close_retry is not None and self._close_retry.is_alive():
                return True

            def finish_after_drain() -> None:
                try:
                    self._close_source(
                        release_writer=True,
                        timeout=None,
                        reopen_on_timeout=False,
                    )
                finally:
                    with self._close_retry_lock:
                        if self._close_retry is threading.current_thread():
                            self._close_retry = None

            try:
                retry = threading.Thread(
                    target=finish_after_drain,
                    name="plate-close-retry",
                    daemon=True,
                )
                self._close_retry = retry
                retry.start()
            except Exception:
                self._close_retry = None
                return False
            return True

    def close_for_trash(self, timeout: float = 30.0) -> PlateWriterLock | None:
        """Synchronously drain reads and transfer this source's writer lease."""
        drained, writer = self._close_source(
            release_writer=False,
            timeout=timeout,
            reopen_on_timeout=True,
        )
        if not drained:
            raise RuntimeError("timed out waiting for plate reads to finish")
        return writer

    def close(self, timeout: float | None = 300.0) -> None:
        self._close_source(
            release_writer=True,
            timeout=timeout,
            reopen_on_timeout=False,
        )
