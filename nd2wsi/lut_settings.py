"""Source-bound display preferences, separate from pixels and annotations.

User windows share last-successful-write settings. Agent windows take a private
snapshot on first access and can never save into the user's settings directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import threading
from pathlib import Path

from .cache import quick_fingerprint
from .platform_io import BINARY
from .session_lock import SessionFileLock
from .window_sessions import _atomic_json

FORMAT = "nd2wsi-lut-settings/1"
RECORD_FORMAT = "nd2wsi-lut-settings-record/1"
MAX_BYTES = 128 * 1024
MAX_CHANNELS = 512
_INTEGER = re.compile(r"^(?:u?int\d+|bool|[<>=|]?[iu]\d+)$")


class LutSettingsUnavailable(ValueError):
    """The registered source is missing, changed, or cannot be identified."""


def settings_root() -> Path:
    override = os.environ.get("ND2WSI_LUT_SETTINGS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "nd2wsi-viewer" / "lut-settings"


def _json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_settings(payload, channels, dtype) -> dict:
    if (not isinstance(payload, dict) or set(payload) != {"format", "luts"}
            or payload.get("format") != FORMAT):
        raise ValueError("Unsupported LUT settings format")
    luts = payload.get("luts")
    if (not isinstance(luts, list) or not 1 <= len(luts) <= MAX_CHANNELS
            or len(luts) != len(channels)):
        raise ValueError("LUT settings must match every channel (maximum 512)")
    clean = []
    discrete = bool(_INTEGER.fullmatch(str(dtype).strip()))
    for lut, channel in zip(luts, channels):
        if not isinstance(lut, dict) or set(lut) != {"lo", "hi", "gamma"}:
            raise ValueError("Each LUT must contain minimum, maximum and gamma")
        lo, hi, gamma = (lut[key] for key in ("lo", "hi", "gamma"))
        if (not all(_finite_number(value) for value in (lo, hi, gamma))
                or not hi > lo or not _finite_number(hi - lo) or not 0.25 <= gamma <= 4):
            raise ValueError("LUT values must be finite, minimum below maximum, and gamma 0.25–4")
        default = channel["window"]
        if discrete and hi - lo < 1 and not (
                lo == default.get("start") and hi == default.get("end") and gamma == 1):
            raise ValueError("Integer LUT ranges must span at least one intensity unit")
        clean.append({"lo": lo, "hi": hi, "gamma": gamma})
    result = {"format": FORMAT, "luts": clean}
    if len(_json_bytes(result)) > MAX_BYTES:
        raise ValueError("LUT settings are too large")
    return result


def _stat_signature(path: Path) -> tuple:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("LUT settings require an original source file")
    signature = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    if sys.platform == "win32":
        from .windows_fs import file_change_time, open_guard

        # Windows ctime is creation time. Keep the cheap native change stamp
        # instead of rehashing the source on every contrast adjustment.
        stamp = file_change_time(path, info)
        if stamp is not None:
            signature += ("change-time", stamp)
        else:
            # FAT/exFAT/unknown filesystems may alias ChangeTime to mtime.
            # Keep their LUT persistence using the existing bounded (three
            # sampled MiB, not whole-image) fingerprint under a no-rename guard.
            fd = open_guard(path)
            try:
                def matches(opened):
                    return (stat.S_ISREG(opened.st_mode)
                            and not getattr(opened, "st_file_attributes", 0) & 0x400
                            and (opened.st_dev, opened.st_ino, opened.st_size,
                                 opened.st_mtime_ns, opened.st_ctime_ns) == signature)

                if not matches(os.fstat(fd)):
                    raise OSError("source identity changed before sampled fingerprint")
                fingerprint = quick_fingerprint(path)
                if not matches(os.fstat(fd)):
                    raise OSError("source identity changed during sampled fingerprint")
                signature += ("sampled-content", fingerprint["quick_sha256"])
            finally:
                os.close(fd)
    return signature


def _context(attrs: dict) -> dict:
    meta = attrs["nd2wsi"]
    level = meta["levels"][0]
    channels = attrs["omero"]["channels"]
    if not 1 <= len(channels) <= MAX_CHANNELS:
        raise ValueError("Unsupported channel count")
    value = {
        "selection": meta.get("selection", {}), "dtype": meta["dtype"],
        "dimensions": [level["width"], level["height"]],
        "rgb": bool(meta.get("rgb")), "kind": "plate" if meta.get("plate") else "slide",
        "channels": [{"label": channel["label"], "window": channel["window"]} for channel in channels],
    }
    # Detach nested metadata: later renderer mutations must not change identity.
    encoded = _json_bytes(value)
    if len(encoded) > MAX_BYTES // 2:
        raise ValueError("LUT channel metadata is too large")
    return json.loads(encoded)


class LutSettingsStore:
    def __init__(self, source_path, attrs, window_session=None):
        self._lock = threading.RLock()
        self._snapshot_ready = False
        self._warning = None
        self.identity = None
        self.path = None
        self.shared_path = None
        self._session = window_session
        self.agent = window_session is not None and window_session.role == "agent"
        try:
            context = _context(attrs)
            if source_path is None:
                raise ValueError("Original source is unavailable")
            self.source = Path(source_path).expanduser().resolve(strict=True)
            before = _stat_signature(self.source)
            fingerprint = quick_fingerprint(self.source)
            self._source_stat = _stat_signature(self.source)
            if self._source_stat != before:
                raise ValueError("Original source changed while reading its identity")
            self.identity = {"path": str(self.source), "source": fingerprint, "context": context}
            key = hashlib.sha256(_json_bytes({"path": str(self.source),
                                             "selection": context["selection"]})).hexdigest()
            self.shared_path = settings_root() / (key + ".json")
            self.path = (Path(window_session.root) / "lut-settings" / (key + ".json")
                         if self.agent else self.shared_path)
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            self.identity = None
            self._warning = "LUT settings unavailable: " + str(exc)

    def _require_current(self) -> None:
        try:
            current = self.identity is not None and _stat_signature(self.source) == self._source_stat
        except (OSError, ValueError):
            current = False
        if not current:
            raise LutSettingsUnavailable("Original source is missing or changed; LUT settings were not saved")

    def _validate(self, payload) -> dict:
        context = self.identity["context"]
        return validate_settings(payload, context["channels"], context["dtype"])

    def _read(self, path: Path):
        try:
            flags = os.O_RDONLY | BINARY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError("Saved LUT settings are not a bounded regular file")
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Saved LUT settings are too large")
            record = json.loads(raw)
            if (not isinstance(record, dict) or record.get("format") != RECORD_FORMAT
                    or record.get("identity") != self.identity):
                raise ValueError("Saved LUT settings do not match this source and channel metadata")
            return self._validate(record.get("settings")), None
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            # Never delete, rewrite or migrate an unreadable/mismatched record.
            return None, "Saved LUT settings were not restored: " + str(exc)

    def _write(self, settings, *, only_if_missing=False) -> None:
        record = {"format": RECORD_FORMAT, "identity": self.identity, "settings": settings}
        if len((json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")) > MAX_BYTES:
            raise ValueError("LUT settings record is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = SessionFileLock(self.path.with_suffix(".lock"))
        lock.acquire(timeout=10)
        try:
            self._require_current()
            if only_if_missing and self.path.exists():
                return
            _atomic_json(self.path, record)
        finally:
            lock.release()

    def _snapshot(self) -> None:
        if not self.agent or self._snapshot_ready:
            return
        with self._session._lock:
            seen = getattr(self._session, "_lut_settings_snapshots", None)
            if seen is None:
                seen = self._session._lut_settings_snapshots = set()
            if self.path not in seen and not self.path.exists():
                settings, warning = self._read(self.shared_path)
                if settings is not None:
                    self._write(settings, only_if_missing=True)
                self._warning = warning
            # Also remember an absent/invalid snapshot without copying it. A
            # later reopen in this Agent session cannot adopt new user edits.
            seen.add(self.path)
            self._snapshot_ready = True

    def read(self) -> dict:
        with self._lock:
            try:
                self._require_current()
                self._snapshot()
                settings, warning = self._read(self.path)
                result = {"ok": True, "available": True, "settings": settings}
                if warning or self._warning:
                    result["warning"] = warning or self._warning
                return result
            except (OSError, ValueError) as exc:
                return {"ok": True, "available": False, "settings": None,
                        "warning": self._warning or str(exc)}

    def write(self, payload) -> dict:
        with self._lock:
            self._require_current()
            settings = self._validate(payload)
            self._snapshot()
            self._write(settings)
            self._warning = None
            return {"ok": True}
