"""Plate thumbnail cache persistence, writer locking, manifest/window storage, and memory caches.

Reduced grid frames are persisted in a managed thumbnail cache beside the
source and also live in a process-wide, bytes-bounded memory cache. The
source file remains authoritative: an incomplete or unverifiable thumbnail
falls back to the corresponding source frame and is repaired by the one
viewer that owns the cache writer lease.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .cache import (
    CACHE_SUFFIX,
    CACHES_DIR,
    MANIFEST_NAME,
    CacheLock,
    fingerprints_match,
    is_file_container,
    managed_dir,
    names_match,
    normalize_name,
    quarantine,
    quick_fingerprint,
    read_manifest,
    resolve_existing_path,
    source_base,
    source_tag,
)
from .direct_common import _TileCache
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
from .plate_layout import THUMB_K
from .platform_io import filesystem_path, total_physical_memory_bytes
from .session_lock import SessionFileLock

if TYPE_CHECKING:
    from .plate import PlateSource

FRAME_CACHE_MIN_BYTES = 256 * 1024 * 1024
FRAME_CACHE_MAX_BYTES = 640 * 1024 * 1024


def _default_frame_cache_bytes(total_ram: int | None = None) -> int:
    if total_ram is None:
        raw_env = os.environ.get("ND2WSI_FRAME_CACHE_BYTES")
        if raw_env is not None and raw_env.strip() != "":
            val = int(raw_env)
            if val > 0:
                return val
    ram = int(total_physical_memory_bytes() if total_ram is None else total_ram)
    return max(FRAME_CACHE_MIN_BYTES, min(int(ram * 0.05), FRAME_CACHE_MAX_BYTES))


PLATE_CACHE_BYTES = 768 * 1024 * 1024  # reduced frames kept in memory
FRAME_CACHE_BYTES = _default_frame_cache_bytes()  # whole frames kept for the focused site
_INIT_FRAME_CACHE_BYTES = FRAME_CACHE_BYTES
WINDOWS_T = 0  # default display windows sample every site at this time point,
WINDOWS_K = 4  # at the home plane, reduced by this factor
# Names how PlateSource._measure_windows works. Change it whenever that
# measurement changes, so windows recorded by older builds are measured again.
WINDOWS_METHOD = "plate-low-percentile/1"
WINDOWS_KEY = "display_windows"  # additive manifest key; readers only use it
UNSCORED = -1.0  # a frame whose sharpness has not been measured yet
# Single-file containers use /2 at a new .nd2svs path. Legacy /1 directories
# remain read-only compatible and are never rewritten on ordinary open.
PLATE_FORMAT = "nd2wsi-plate/2"
LEGACY_PLATE_FORMAT = "nd2wsi-plate/1"
PLATE_TAG = "plate"
THUMBS_NAME = "thumbs.zarr"
READER_REFRESH_S = 0.5

# one reduction budget for the whole process, not one per open file
_REDUCED_CACHE = _TileCache(PLATE_CACHE_BYTES)
# whole frames read for the focused site or for a reduction, a few at a time
_FRAME_CACHE = _TileCache(FRAME_CACHE_BYTES)

_ORIG_QUARANTINE = quarantine
_ORIG_QUICK_FINGERPRINT = quick_fingerprint
_ORIG_READER_REFRESH_S = READER_REFRESH_S


def _quarantine(container: Path) -> Path | None:
    """Invoke ``quarantine`` while honoring monkeypatches on ``nd2wsi.plate``."""
    plate_mod = sys.modules.get("nd2wsi.plate")
    if plate_mod is not None:
        override = getattr(plate_mod, "quarantine", _ORIG_QUARANTINE)
        if override is not _ORIG_QUARANTINE:
            return override(container)
    return quarantine(container)


def _quick_fingerprint(path: str | Path) -> dict[str, Any]:
    """Invoke ``quick_fingerprint`` while honoring monkeypatches on ``nd2wsi.plate``."""
    plate_mod = sys.modules.get("nd2wsi.plate")
    if plate_mod is not None:
        override = getattr(plate_mod, "quick_fingerprint", _ORIG_QUICK_FINGERPRINT)
        if override is not _ORIG_QUICK_FINGERPRINT:
            return override(path)
    return quick_fingerprint(path)


def _reader_refresh_s() -> float:
    if READER_REFRESH_S != _ORIG_READER_REFRESH_S:
        return float(READER_REFRESH_S)
    plate_mod = sys.modules.get("nd2wsi.plate")
    if plate_mod is not None:
        override = getattr(plate_mod, "READER_REFRESH_S", _ORIG_READER_REFRESH_S)
        if override != _ORIG_READER_REFRESH_S:
            return float(override)
    return float(READER_REFRESH_S)


def _effective_frame_cache_budget() -> int:
    if _FRAME_CACHE.budget != _INIT_FRAME_CACHE_BYTES:
        return int(_FRAME_CACHE.budget)
    if FRAME_CACHE_BYTES != _INIT_FRAME_CACHE_BYTES:
        return int(FRAME_CACHE_BYTES)
    plate_mod = sys.modules.get("nd2wsi.plate")
    if plate_mod is not None:
        override = getattr(plate_mod, "FRAME_CACHE_BYTES", _INIT_FRAME_CACHE_BYTES)
        if override != _INIT_FRAME_CACHE_BYTES:
            return int(override)
    return int(FRAME_CACHE_BYTES)


def _has_integrity(manifest: dict[str, Any]) -> bool:
    integrity = manifest.get("integrity") or {}
    return (
        integrity.get("algorithm") == DIGEST_ALGORITHM
        and integrity.get("commit") == "payload-digest-done"
        and integrity.get("digest_name") == DIGEST_NAME
    )


_FINGERPRINT_KEYS = ("size", "mtime_ns", "quick_sha256")
_WINDOW_FIELDS = ("start", "end", "min", "max")


def _windows_record(
    manifest: dict[str, Any], source: Any, windows: list[dict[str, Any]]
) -> dict[str, Any]:
    """The manifest record of default display windows measured from ``source``."""
    fingerprint = manifest.get("source") or {}
    return {
        "method": WINDOWS_METHOD,
        "generation": manifest.get("generation"),
        "source": {key: fingerprint.get(key) for key in _FINGERPRINT_KEYS},
        "t": WINDOWS_T,
        "z": int(source.z_home),
        "k": WINDOWS_K,
        "dtype": str(source.dtype),
        "rgb": bool(source.rgb),
        "windows": [
            {field: float(window[field]) for field in _WINDOW_FIELDS}
            for window in windows
        ],
    }


def _finite_number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:  # a JSON integer too large for a float
        return False


def _stored_windows(manifest: dict[str, Any], source: Any) -> list[dict[str, float]] | None:
    """The default display windows a plate manifest records for ``source``.

    A record counts only for the cache generation and source fingerprint it
    was written with, the plane and reduction it sampled and the method that
    measured it. Anything else, or anything malformed, is measured again.
    """
    record = manifest.get(WINDOWS_KEY)
    if not isinstance(record, dict):
        return None
    fingerprint = record.get("source")
    if (
        record.get("method") != WINDOWS_METHOD
        or not manifest.get("generation")
        or record.get("generation") != manifest.get("generation")
        or not isinstance(fingerprint, dict)
        or any(fingerprint.get(key) is None for key in _FINGERPRINT_KEYS)
        or not fingerprints_match(fingerprint, manifest.get("source") or {})
    ):
        return None
    for name, expected in (("t", WINDOWS_T), ("z", source.z_home), ("k", WINDOWS_K)):
        if type(record.get(name)) is not int or record[name] != int(expected):
            return None
    if record.get("dtype") != str(source.dtype) or record.get("rgb") is not bool(source.rgb):
        return None
    entries = record.get("windows")
    if not isinstance(entries, list) or len(entries) != int(source.frame_shape[0]):
        return None
    expected_rgb_end = (
        float(np.iinfo(source.dtype).max)
        if source.rgb and np.issubdtype(source.dtype, np.integer) and source.dtype != np.dtype(np.uint8)
        else None
    )
    windows = []
    for entry in entries:
        values = [entry.get(field) for field in _WINDOW_FIELDS] if isinstance(entry, dict) else []
        if len(values) != len(_WINDOW_FIELDS) or not all(_finite_number(v) for v in values):
            return None
        win = {field: float(v) for field, v in zip(_WINDOW_FIELDS, values)}
        if expected_rgb_end is not None and (
            win["start"] != 0.0 or win["end"] != expected_rgb_end or win["max"] != expected_rgb_end
        ):
            return None
        windows.append(win)
    return windows


def _file_identity(st: os.stat_result) -> tuple[int, int, int, int]:
    """Stable identity used to detect same-size, timestamp-preserving swaps."""
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))


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
    return managed_dir(slide) / CACHES_DIR / f"{source_tag(slide)}--{PLATE_TAG}.nd2svs"


def _plate_group(container: Path, mode: str):
    if is_file_container(container):
        from .storage.single_file import open_zarr_group
        return open_zarr_group(container, prefix=THUMBS_NAME, mode=mode)
    if mode in ("r", "r+", "a") and (
        os.path.isdir(filesystem_path(container)) or os.path.lexists(filesystem_path(container))
    ):
        from .storage.zarr_v2 import UnsafeZarrArrayError, validate_zarr_v2_directory
        from .windows_fs import is_windows_name_surrogate_reparse

        try:
            container_st = os.lstat(filesystem_path(container))
        except OSError as exc:
            raise UnsafeZarrArrayError(f"cannot inspect plate cache directory: {container}") from exc
        if (
            stat.S_ISLNK(container_st.st_mode)
            or is_windows_name_surrogate_reparse(container_st)
            or not stat.S_ISDIR(container_st.st_mode)
        ):
            raise UnsafeZarrArrayError(f"refusing unsafe plate cache directory: {container}")
        thumbs_dir = container / THUMBS_NAME
        if os.path.lexists(filesystem_path(thumbs_dir)):
            validate_zarr_v2_directory(thumbs_dir)
    import zarr
    return zarr.open_group(Path(filesystem_path(container / THUMBS_NAME)), mode=mode, zarr_format=2)


def _close_plate_group(root: Any) -> None:
    if root is not None:
        close = getattr(root.store, "close", None)
        if close is not None:
            close()


def plate_session_lock_path(container: str | Path) -> Path:
    """Persistent inode used by current builds to arbitrate a plate writer."""
    container = Path(container)
    canonical = container.with_name(normalize_name(container.name) + ".writer-session.lock")
    return resolve_existing_path(canonical, kind="file")


def _empty_plate_status(n_t: int, n_p: int, n_z: int) -> dict[str, Any]:
    """Status payload when no thumbnail store is attached."""
    return {
        "done": 0,
        "total": n_t * n_p * n_z,
        "perT": [0] * n_t,
        "path": None,
        "format": None,
        "building": False,
        "writer": False,
        "integrityErrors": 0,
        "repairPending": 0,
    }


def _empty_focus_map(n_t: int, n_p: int, n_z: int, z_home: int) -> dict[str, Any]:
    """Focus map payload when no thumbnail store is attached."""
    home = int(z_home)
    return {
        "best": [[home] * n_p for _ in range(n_t)],
        "measured": 0,
        "total": n_t * n_p,
        "measuredZ": [[0] * n_p for _ in range(n_t)],
        "totalZ": n_z,
        "complete": [[False] * n_p for _ in range(n_t)],
        "completeCount": 0,
        "zHome": home,
        "scoring": {
            "channelMode": "all-channels-mean-image",
            "metric": "mean-squared-gradient-normalized-by-mean",
        },
    }


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
            self.container.with_name(normalize_name(self.container.name) + ".writer")
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
        self._repaired_chunks: set[tuple[int, int]] = set()  # see put_block
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
        # (t, z, sites) of the chunk the builder is staging for put_block
        self._staging: tuple[int, int, dict[int, np.ndarray]] | None = None
        self._staging_lock = threading.Lock()

    # ---- open or create -------------------------------------------------
    @staticmethod
    def _container_for(source: PlateSource, cache_path: str | Path | None) -> Path:
        if cache_path is not None:
            return Path(cache_path).expanduser().resolve()
        container = resolve_existing_path(plate_container(source.path), kind="file")
        if not container.exists():
            legacy = resolve_existing_path(
                container.with_name(container.name[:-len(".nd2svs")] + CACHE_SUFFIX),
                kind="dir",
            )
            if legacy.is_dir():
                return legacy
        return container

    @staticmethod
    def _shape(source: PlateSource) -> tuple[int, ...]:
        c, h, w = source.frame_shape
        return (source.T, source.P, source.Z, c, h // THUMB_K, w // THUMB_K)

    @classmethod
    def recorded_windows(
        cls, source: PlateSource, *, cache_path: str | Path | None = None
    ) -> list[dict[str, float]] | None:
        """The default display windows an existing cache records for ``source``.

        A read-only look taken before the store opens: nothing is created,
        locked, recovered or quarantined, so an open that fails afterwards
        leaves the disk as it found it. Any doubt means measuring again.
        """
        try:
            container = cls._container_for(source, cache_path)
            if not container.exists():
                return None
            manifest = cls._read(container)
            if manifest is None or not cls._matches(
                manifest, _quick_fingerprint(source.path), cls._shape(source), source
            ):
                return None
            return _stored_windows(manifest, source)
        except Exception:
            return None

    @classmethod
    def open_or_create(cls, source: PlateSource, *, cache_path: str | Path | None = None) -> PlateStore:
        explicit = cache_path is not None
        container = cls._container_for(source, cache_path)
        fingerprint = _quick_fingerprint(source.path)
        shape = cls._shape(source)
        manifest = cls._read(container, recover=True)
        if explicit and (manifest is None or not cls._matches(manifest, fingerprint, shape, source)):
            raise ValueError('explicit plate cache is missing or does not match its source')
        if container.is_dir():
            if manifest is not None and cls._matches(manifest, fingerprint, shape, source):
                root = None
                try:
                    root = _plate_group(container, 'r')
                    cls._validate_root(
                        root, shape, source.dtype, require_digest=_has_integrity(manifest)
                    )
                    return cls(source, container, root, manifest, None)
                except Exception:
                    _close_plate_group(root)
                    if explicit:
                        raise
                except BaseException:
                    _close_plate_group(root)
                    raise
            elif explicit:
                raise ValueError('legacy plate cache does not match its source')
            container = resolve_existing_path(plate_container(source.path), kind="file")
            manifest = cls._read(container, recover=True)
        container.parent.mkdir(parents=True, exist_ok=True)
        writer = PlateWriterLock(container)
        try:
            writer.acquire(timeout=0.0)
        except (TimeoutError, OSError):
            writer = None

        build_lock = CacheLock(container)
        root = None
        try:
            build_lock.acquire(timeout=30.0)
            manifest = cls._read(container, recover=True)
            if manifest is not None and not cls._matches(
                manifest, fingerprint, shape, source
            ):
                if writer is None or explicit:
                    raise RuntimeError(
                        "thumbnail cache changed while another viewer owns its writer"
                    )
                _quarantine(container)
                manifest = None
            if manifest is None:
                if writer is None or explicit:
                    raise RuntimeError(
                        "thumbnail cache is not ready while another viewer owns its writer"
                    )
                manifest = cls._create(source, container, fingerprint, shape)

            mode = "r+" if writer is not None else "r"
            expected = (1, shape[1], 1, shape[3], shape[4], shape[5])
            try:
                root = _plate_group(container, mode)
                chunks = tuple(root["thumbs"].chunks)
            except Exception:
                _close_plate_group(root)
                root = None
                chunks = None
            if chunks is not None and chunks != expected:
                if writer is None or explicit:
                    raise RuntimeError(
                        "thumbnail cache needs rebuilding by its writer"
                    )
                # The first release candidate wrote one allocation-heavy file
                # per site. Keep the whole superseded container rather than
                # recursively deleting it: an old build may have placed user
                # annotations inside what otherwise looks like cache data.
                _close_plate_group(root)
                root = None
                _quarantine(container)
                manifest = cls._create(source, container, fingerprint, shape)
                root = _plate_group(container, 'r+')
            elif root is None:
                if writer is None or explicit:
                    raise RuntimeError("thumbnail cache is structurally unreadable")
                _quarantine(container)
                manifest = cls._create(source, container, fingerprint, shape)
                root = _plate_group(container, 'r+')

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
                if writer is None or explicit:
                    raise RuntimeError("thumbnail cache has an invalid array schema") from None
                _close_plate_group(root)
                root = None
                _quarantine(container)
                manifest = cls._create(source, container, fingerprint, shape)
                root = _plate_group(container, 'r+')
                cls._validate_root(root, shape, source.dtype, require_digest=True)

            store = cls(source, container, root, manifest, writer)
            store.sweep()
        except BaseException:
            _close_plate_group(root)
            if writer is not None:
                writer.release()
            raise
        finally:
            build_lock.release()
        return store

    def sweep(self) -> None:
        """Drop the AppleDouble twins macOS leaves beside every file on
        exFAT and NTFS drives; each one costs a whole allocation block."""
        if not self.writable or is_file_container(self.container):
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
    def _read(container: Path, *, recover: bool = False) -> dict | None:
        if is_file_container(container):
            from .storage.single_file import newer_file_format
            newer = newer_file_format(container)
            if newer is not None:
                raise RuntimeError(f'thumbnail cache uses newer storage format {newer}; update nd2wsi-viewer')
        m = read_manifest(container, recover=recover)
        if not isinstance(m, dict):
            return None
        fmt = str(m.get("format") or "")
        if fmt in (PLATE_FORMAT, LEGACY_PLATE_FORMAT):
            return m
        match = re.fullmatch(r"nd2wsi-plate/(\d+)", fmt)
        if match and int(match.group(1)) > 2:
            raise RuntimeError(
                f"thumbnail cache uses newer format {fmt}; update nd2wsi-viewer"
            )
        return None

    @staticmethod
    def _matches(manifest: dict, fingerprint: dict, shape: tuple, source: PlateSource) -> bool:
        thumbs = manifest.get("thumbs") or {}
        ch_dtypes = [
            str(dt)
            for dt in (
                getattr(source, "_channel_dtypes", None)
                or [str(source.dtype)] * int(shape[3])
            )
        ]
        stored_ch_dtypes = thumbs.get("channel_dtypes")
        requires_ch_dtypes = (
            len(set(ch_dtypes)) > 1
            or any("+" in dt or ":" in dt or dt in ("Bgr96Float", "Bgra32") for dt in ch_dtypes)
            or bool(getattr(source, "_requires_manifest_channel_dtypes", False))
        )
        if requires_ch_dtypes:
            ch_dtypes_ok = stored_ch_dtypes == ch_dtypes
        else:
            ch_dtypes_ok = stored_ch_dtypes in (None, ch_dtypes)
        return (
            fingerprints_match(manifest.get("source") or {}, fingerprint)
            and names_match((manifest.get("source") or {}).get("name"), Path(source.path).name)
            and list(thumbs.get("shape") or []) == list(shape)
            and thumbs.get("dtype") == str(source.dtype)
            and ch_dtypes_ok
            and thumbs.get("k") == THUMB_K
            and (Path(source.path).parent / (container_name := manifest.get("thumbs_name", THUMBS_NAME)))
            is not None
            and container_name == THUMBS_NAME
        )

    @staticmethod
    def _create(source: PlateSource, container: Path, fingerprint: dict, shape: tuple) -> dict:
        if is_file_container(container):
            from .storage.single_file import SQLiteStore
            staging = container.with_name(container.name + '.building-' + uuid.uuid4().hex)
            SQLiteStore.create_exclusive(staging).close()
            root = None
            try:
                root = _plate_group(staging, 'w')
                manifest = PlateStore._create_contents(source, staging, fingerprint, shape, root)
                _close_plate_group(root)
                root = None
                PlateStore._write_manifest(staging, manifest)
                predecessor = _quarantine(container) if container.exists() else None
                try:
                    staging.replace(container)
                except OSError:
                    if predecessor is not None:
                        predecessor.rename(container)
                    raise
                return manifest
            except BaseException:
                _close_plate_group(root)
                staging.unlink(missing_ok=True)
                raise
        if container.exists():
            _quarantine(container)
        container.mkdir(parents=True, exist_ok=True)
        root = _plate_group(container, 'w')
        try:
            manifest = PlateStore._create_contents(source, container, fingerprint, shape, root)
        finally:
            _close_plate_group(root)
        PlateStore._write_manifest(container, manifest)
        return manifest

    @staticmethod
    def _create_contents(source: PlateSource, container: Path, fingerprint: dict, shape: tuple, root: Any) -> dict:
        from numcodecs import Blosc

        from . import __version__

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
        thumbs_meta: dict[str, Any] = {
            "k": THUMB_K,
            "shape": list(shape),
            "dtype": str(source.dtype),
        }
        ch_dtypes = getattr(source, "_channel_dtypes", None)
        if ch_dtypes:
            thumbs_meta["channel_dtypes"] = [str(dt) for dt in ch_dtypes]
        manifest = {
            "format": PLATE_FORMAT if is_file_container(container) else LEGACY_PLATE_FORMAT,
            "kind": "plate",
            "complete": True,
            "generation": uuid.uuid4().hex,
            "source": {
                **fingerprint,
                "relative_path": Path(os.path.relpath(source.path, source_base(container))).as_posix(),
            },
            "thumbs": thumbs_meta,
            "thumbs_name": THUMBS_NAME,
            "integrity": {
                "algorithm": DIGEST_ALGORITHM,
                "commit": "payload-digest-done",
                "digest_name": DIGEST_NAME,
            },
            "created_by": {"nd2wsi_version": __version__},
        }
        return manifest

    @staticmethod
    def _write_manifest(container: Path, manifest: dict) -> None:
        if is_file_container(container):
            from .storage.single_file import write_entry
            write_entry(container, MANIFEST_NAME, json.dumps(manifest, indent=1).encode('utf-8'))
            return
        tmp = container / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
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
        refresh_s = _reader_refresh_s()
        with self._lock:
            if (
                not force
                and now - self._last_generation_check < refresh_s
            ):
                return True
            self._last_generation_check = now
            try:
                current = self._read(self.container)
                matches = (
                    isinstance(current, dict)
                    and current.get("generation")
                    == self.manifest.get("generation")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
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
        refresh_s = _reader_refresh_s()
        if not force and now - self._last_refresh < refresh_s:
            return
        with self._lock:
            if not force and now - self._last_refresh < refresh_s:
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
            if is_file_container(self.container):
                store = self._root.store
                metadata = json.loads(store.read_bytes('thumbs/.zarray'))
                separator = metadata.get('dimension_separator', '.')
                key = 'thumbs/' + separator.join(map(str, (int(t), 0, int(z), 0, 0, 0)))
                with store.batch():
                    payload = store.read_bytes(key)
                    store.write_bytes(key + '.corrupt-' + uuid.uuid4().hex, payload)
                    store.delete_sync(key)
                return True
            path = zarr_v2_chunk_path(
                Path(filesystem_path(self.container / THUMBS_NAME / "thumbs")),
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

    def keep_display_windows(self, windows: list[dict[str, Any]]) -> None:
        """Record windows measured from the source for the next open.

        Only the writer persists them, as an additive manifest key in the
        same generation. A manifest that cannot be written only means the
        next open measures them again.
        """
        with self._lock:
            if self._stop or not self.writable:
                return
            manifest = dict(self.manifest)
            manifest[WINDOWS_KEY] = _windows_record(manifest, self._source, windows)
            if _stored_windows(manifest, self._source) is None:
                return  # a record the next open would refuse is not worth a write
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
            # Copies, not views: a view would keep the whole chunk of every
            # site alive while the byte budget counted one site.
            for p2 in np.flatnonzero(verified):
                key = (owner, int(t), int(p2), int(z), THUMB_K)
                if _REDUCED_CACHE.get(key) is None:
                    _REDUCED_CACHE.put(key, block[p2].copy())
                if self._focus_np[t, p2, z] < 0:
                    self._focus_np[t, p2, z] = _sharpness(block[p2])
                    scored += 1
            if scored:
                self._focus_dirty += scored
                self._flush_focus_if_block_complete(t, z)
            if not verified[p]:
                return None
            return block[p].copy()

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

    def offer(self, t: int, p: int, z: int, frame: np.ndarray) -> None:
        """Store a foreground reduction, sharing the builder's chunk write.

        While the builder is staging this site's ``(t, z)`` chunk, the frame
        joins that chunk and lands with its :meth:`put_block`, so a request
        thread never rewrites the shared chunk for one site the builder is
        about to commit anyway. Any other site is put on its own.
        """
        with self._staging_lock:
            staging = self._staging
            if staging is not None and staging[0] == t and staging[1] == z:
                staging[2].setdefault(p, frame)
                return
        self.put(t, p, z, frame)

    def put_block(self, t: int, z: int, frames: dict[int, np.ndarray]) -> bool:
        """Commit several sites of one shared ``(t, z)`` chunk at once.

        ``frames`` maps site index to its reduction. Every site of a time
        point and plane lives in one chunk, so :meth:`put` rewrites (decodes,
        encodes, commits and fsyncs) the whole chunk for each site; the
        background builder stages a chunk's missing sites and lands them here
        with one write per array instead of one per site. The order stays
        payload, digest, done, all under ``_lock``. Sites already committed
        keep their stored bytes, and so does every site not in ``frames``,
        unless the chunk cannot be decoded: then its payload is moved aside,
        its committed sites are invalidated and the given sites start a fresh
        chunk (at most once per chunk while this store is open). True when
        every given site is committed afterwards.
        """
        t, z = int(t), int(z)
        self._refresh()
        if not frames:
            return True
        if self._stop or not self.writable or self._digest is None:
            return bool(self._committed_mask()[t, sorted(frames), z].all())
        # Pure per-site work stays outside the lock that foreground reads wait on.
        digests = {p: frame_digest(f) for p, f in frames.items()}
        scores = {
            p: _sharpness(f) for p, f in frames.items() if self._focus_np[t, p, z] < 0
        }
        with self._lock:
            if self._stop or not self.writable or self._digest is None:
                return bool(self._committed_mask()[t, sorted(frames), z].all())
            committed = self._committed_mask()[t, :, z]
            todo = [p for p in sorted(frames) if not committed[p]]
            if not todo:
                return True
            try:
                if len(todo) == len(committed):
                    # The whole chunk is new: write it without reading it.
                    block = np.stack([frames[p] for p in todo])
                else:
                    try:
                        block = np.array(self._thumbs[t, :, z], copy=True)
                    except Exception:
                        # An undecodable shared chunk. As get() does, keep its
                        # payload and fail its sites closed; once it is moved
                        # aside the given sites start a fresh chunk, and the
                        # walk comes back for the invalidated ones. A chunk
                        # still undecodable after that rewrite (a drive that
                        # corrupts writes) stops the build instead of looping,
                        # each round keeping another copy of it.
                        if (t, z) in self._repaired_chunks:
                            return False
                        self._repaired_chunks.add((t, z))
                        moved = self._quarantine_shared_payload(t, z)
                        self._invalidate(t, z, committed)
                        if not moved:
                            return False
                        todo = sorted(frames)
                        block = np.array(self._thumbs[t, :, z], copy=True)
                    for p in todo:
                        block[p] = frames[p]
                self._thumbs[t, :, z] = block
                self._digest.oindex[t, todo, z] = np.array(
                    [digests[p] for p in todo], dtype=np.uint64
                )
                self._done.oindex[t, todo, z] = 1
            except Exception:
                return False  # a store that cannot be written is only slower
            for p in todo:
                self._digest_np[t, p, z] = digests[p]
                self._done_np[t, p, z] = True
                self._invalid[t, p, z] = False
                if self._focus_np[t, p, z] < 0:
                    score = scores.get(p)
                    self._focus_np[t, p, z] = (
                        _sharpness(frames[p]) if score is None else score
                    )
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
        """Fill the store one shared chunk at a time, then warm what is in it.

        From the first missing frame the walk finds, the rest of that time
        point and plane's missing sites are reduced in the order the walk
        would visit them anyway, then committed together by
        :meth:`put_block`. A foreground read of a site in that chunk hands
        its reduction over through :meth:`offer` instead of writing it, and
        the builder skips a site it was handed. A stop is honoured between
        sites, and a close loses at most the one chunk being staged.

        The pass gives up rather than looping when the frames stop
        arriving: a write the store cannot make is swallowed, so a container
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
            t, first, z = item
            staged: dict[int, np.ndarray] = {}
            made = 0
            failed = False
            with self._staging_lock:
                self._staging = (t, z, staged)
            try:
                for p in range(first, self._source.P):
                    if p in staged or self.is_committed(t, p, z):
                        continue  # handed over or stored by a foreground read
                    while src._fg > 0 and not self._stop:
                        time.sleep(0.02)
                    if self._stop:
                        return
                    try:
                        frame = src.reduced(t, p, z, THUMB_K, persist=False)
                    except Exception:
                        if self._stop or src._closed:
                            return
                        failed = True
                        break
                    with self._staging_lock:
                        staged.setdefault(p, frame)
                    made += 1
                    time.sleep(0.005)
            finally:
                # Sealed: from here a foreground read puts its site itself.
                with self._staging_lock:
                    self._staging = None
            if made:
                misses = 0
            if staged:
                if not self.put_block(t, z, staged):
                    break  # the reductions were made and the store would not take them
                self._since_sweep = getattr(self, "_since_sweep", 0) + len(staged)
                if self._since_sweep >= 64:
                    self._since_sweep = 0
                    self.sweep()
                    self.flush_focus()
            if failed:
                misses += 1
                if misses >= 8:
                    break  # the file keeps refusing this frame
                time.sleep(0.5)
                continue
            if not self.is_committed(*item):
                break
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
            with self._lock:
                _close_plate_group(self._root)
            writer, self._writer = self._writer, None
            if writer is not None and release_writer:
                writer.release()
                return None
            return writer
