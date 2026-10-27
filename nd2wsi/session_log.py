"""Continuous session log for one viewer process, kept for review after a run.

Interface used across the app:

- ``start(app_name=..., version=..., folder=None)`` opens this run's log file and
  returns its path, or None when logging could not start.
- ``event(kind, **fields)`` appends one record. It is thread-safe, never raises,
  and does nothing before ``start`` or after ``stop``. ``kind`` may also be
  passed by keyword.
- ``path()`` returns the current log file, or None.
- ``stop(reason)`` writes a final record and closes the file.

Each process writes ``session-YYYYMMDD-HHMMSS-<pid>.jsonl``: UTF-8, one JSON
object per line, flushed line by line so a crash keeps everything written
before it. A native crash (segfault, abort) is written by ``faulthandler`` to
the matching ``-fatal.txt`` file, which is removed at a clean stop if empty.
Past MAX_SESSION_BYTES a session keeps only failures: the LIMIT_KINDS, kinds
ending in ``_error`` or ``_failed``, and records with ``ok=False`` or
``level="error"``.

The folder is ``ND2WSI_SESSION_LOG_DIR`` when set; otherwise ``logs`` beside a
portable frozen Windows executable when that folder is writable (the copy keeps
its logs with it); otherwise the per-user log folder. An installed copy's
folder stays exactly as Setup left it, so its uninstaller can remove it, and a
macOS app bundle is never written into, since that would break its signature.
Starting a session prunes old ones, deleting only regular files whose names
this module creates, in that folder only.
"""

from __future__ import annotations

import atexit
import datetime as dt
import faulthandler
import gc
import json
import math
import os
import platform
import re
import stat
import sys
import threading
import time
import traceback
from pathlib import Path

ENV_FOLDER = "ND2WSI_SESSION_LOG_DIR"
MAX_SESSION_BYTES = 50 * 1024 * 1024
# Past the cap, only failures are kept (these kinds, see _kept_when_limited),
# up to a small hard ceiling.
LIMIT_KINDS = frozenset({"error", "exception", "client_error", "crash", "stop"})
_HARD_CEILING = MAX_SESSION_BYTES + 5 * 1024 * 1024
KEEP_SESSIONS = 50
KEEP_TOTAL_BYTES = 500 * 1024 * 1024
# A session whose files changed this recently may belong to a live process
# (its health record refreshes the file every HEALTH_INTERVAL); never prune it.
_RECENT_SECONDS = 300.0
HEALTH_INTERVAL = 30.0
MAX_STRING = 8192
# A long traceback keeps its first frames and, above all, its end: the
# innermost frames and the exception that ended the run.
_TRACEBACK_HEAD = 1024
_MAX_ITEMS = 200
_MAX_DEPTH = 6
_RESERVED = ("t", "uptime", "kind", "thread")

# Setup writes these beside the executable; a portable ZIP has neither.
_INSTALLED_MARKERS = ("Uninstall.exe", ".nd2wsi-install.ini")
_SESSION_NAME = re.compile(r"(session-\d{8}-\d{6}-\d+(?:-\d+)?)\.jsonl")
_FATAL_NAME = re.compile(r"(session-\d{8}-\d{6}-\d+(?:-\d+)?)-fatal\.txt")

_lock = threading.RLock()  # re-entrant: a signal handler may log mid-write
_path: Path | None = None
_handle = None
_fatal_handle = None
_fatal_path: Path | None = None
_fatal_previously_enabled = False
_started_monotonic = 0.0
_bytes = 0
_limited = False
_dropped = 0
_write_errors = 0
_health_stop: threading.Event | None = None
_health_thread: threading.Thread | None = None
_previous_excepthook = None
_previous_threading_excepthook = None
_atexit_registered = False


def candidate_folders() -> list[Path]:
    """Where this process may keep its session log, in order of preference."""
    override = os.environ.get(ENV_FOLDER)
    if override:
        return [Path(override).expanduser()]
    folders = []
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        beside = Path(sys.executable).resolve().parent
        if not any((beside / name).exists() for name in _INSTALLED_MARKERS):
            folders.append(beside / "logs")
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        folders.append(root / "nd2wsi-viewer" / "Logs" / "sessions")
    else:
        folders.append(Path.home() / "Library" / "Logs" / "nd2wsi-viewer" / "sessions")
    return folders


def start(*, app_name: str, version: str, folder: Path | None = None) -> Path | None:
    """Open this process's session log; returns its path, or None.

    A second call while a session is open returns the open session's path.
    """
    global _path, _handle, _started_monotonic, _bytes, _limited, _dropped, _write_errors
    try:
        with _lock:
            if _handle is not None:
                return _path
            folders = [Path(folder)] if folder is not None else candidate_folders()
            opened = None
            for candidate in folders:
                opened = _open_session_file(candidate)
                if opened is not None:
                    break
            if opened is None:
                return None
            _path, _handle = opened
            _started_monotonic = time.monotonic()
            _bytes = 0
            _limited = False
            _dropped = 0
            _write_errors = 0
            _write({
                "kind": "start", "app": app_name, "version": version, "pid": os.getpid(),
                "folder": str(_path.parent), "file": _path.name,
                "python": platform.python_version(), "platform": sys.platform,
                "machine": platform.machine(), "frozen": bool(getattr(sys, "frozen", False)),
                "executable": sys.executable, "argv": list(sys.argv),
            })
            session_path = _path
        _start_crash_capture(session_path)
        _prune(session_path.parent, keep=session_path)
        _start_health()
        _register_atexit()
        return session_path
    except Exception:
        return None


def event(*args, **fields) -> None:
    """``event(kind, **fields)``: append one record.

    Never raises, and is a no-op while no session is open. ``kind`` may be
    given by position or by keyword; with both, the keyword is kept as the
    ``field_kind`` field.
    """
    try:
        if args:
            kind = args[0]
            if len(args) > 1:
                fields["args"] = list(args[1:])
        else:
            kind = fields.pop("kind", "event")
        with _lock:
            if _handle is None:
                return
            record = {"kind": kind}
            for key, value in fields.items():
                key = str(key)
                record[f"field_{key}" if key in _RESERVED else key] = value
            _write(record)
    except Exception:
        pass


def path() -> Path | None:
    """This process's log file: the open one, or the last one after ``stop``."""
    with _lock:
        return _path


def stop(reason: str = "exit") -> None:
    """Write the final record and close the session; later calls do nothing."""
    global _handle, _health_thread, _health_stop
    try:
        with _lock:
            if _handle is None:
                return
            _write({"kind": "stop", "reason": reason, "dropped": _dropped, "write_errors": _write_errors})
            handle, _handle = _handle, None
            stopping, _health_stop = _health_stop, None
            health, _health_thread = _health_thread, None
        if stopping is not None:
            stopping.set()
        if health is not None and health is not threading.current_thread():
            health.join(timeout=2)
        _stop_crash_capture()
        try:
            handle.close()
        except OSError:
            pass
    except Exception:
        pass


# ---- records ---------------------------------------------------------------

def _timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _clip(text: str) -> str:
    if len(text) <= MAX_STRING:
        return text
    return text[:MAX_STRING] + f"…[+{len(text) - MAX_STRING} chars]"


def _clip_traceback(text: str) -> str:
    """At most MAX_STRING characters, cut from the middle of a Python traceback."""
    if len(text) <= MAX_STRING:
        return text
    marker = f"\n…[{len(text) - MAX_STRING} chars omitted]…\n"
    tail = MAX_STRING - _TRACEBACK_HEAD - len(marker)
    return text[:_TRACEBACK_HEAD] + marker + text[len(text) - tail:]


def _printable(value) -> str:
    try:
        return _clip(str(value))
    except Exception:
        return f"<unprintable {type(value).__name__}>"


def _clean(value, depth: int = 0):
    """A JSON-safe copy: strings capped, containers bounded, the rest stringified."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, (bytes, bytearray)):
        return _clip(repr(bytes(value)))
    if isinstance(value, os.PathLike):
        return _printable(value)
    if depth >= _MAX_DEPTH:
        return _printable(value)
    if isinstance(value, dict):
        items = list(value.items())
        cleaned = {_printable(key)[:200]: _clean(item, depth + 1) for key, item in items[:_MAX_ITEMS]}
        if len(items) > _MAX_ITEMS:
            cleaned["…"] = f"{len(items) - _MAX_ITEMS} more"
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        cleaned = [_clean(item, depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            cleaned.append(f"… {len(items) - _MAX_ITEMS} more")
        return cleaned
    return _printable(value)


def _write(record: dict) -> None:
    """Serialise and append one line. Caller holds ``_lock`` and an open handle."""
    global _limited, _dropped
    kind = _printable(record.pop("kind", "event"))[:100]
    kept = _kept_when_limited(kind, record)
    if _limited and (not kept or (_bytes >= _HARD_CEILING and kind != "stop")):
        _dropped += 1
        return
    line = _encode(kind, record)
    if not _limited and _bytes + len(line) > MAX_SESSION_BYTES and kind != "stop":
        _limited = True
        notice = _encode("log_limit", {
            "limit_mb": MAX_SESSION_BYTES // (1024 * 1024), "kept_kinds": sorted(LIMIT_KINDS),
            "also_kept": "kinds ending in _error or _failed; ok=false; level=error"})
        _append(notice)
        if not kept:
            _dropped += 1
            return
    _append(line)


def _kept_when_limited(kind: str, fields: dict) -> bool:
    """Whether a record is a failure, kept after the session reached its cap."""
    return (kind in LIMIT_KINDS or kind.endswith(("_error", "_failed"))
            or fields.get("ok") is False or fields.get("level") == "error")


def _encode(kind: str, fields: dict) -> bytes:
    record = {"t": _timestamp(), "uptime": round(time.monotonic() - _started_monotonic, 3),
              "kind": kind, "thread": threading.current_thread().name}
    for key, value in fields.items():
        key = _printable(key)[:200]
        record[key] = _clip_traceback(value) if key == "traceback" and isinstance(value, str) else _clean(value)
    text = json.dumps(record, ensure_ascii=False, allow_nan=False)
    # Lone surrogates (undecodable file names) become JSON \uXXXX escapes.
    return (text + "\n").encode("utf-8", "backslashreplace")


def _append(line: bytes) -> None:
    global _bytes, _write_errors
    try:
        _handle.write(line)
        _handle.flush()
        _bytes += len(line)
    except (OSError, ValueError):
        _write_errors += 1


# ---- files -----------------------------------------------------------------

def _open_session_file(folder: Path):
    """Create a new session file in ``folder``; (path, handle) or None."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stem = f"session-{stamp}-{os.getpid()}"
        for attempt in range(1, 100):
            name = stem if attempt == 1 else f"{stem}-{attempt}"
            candidate = folder / f"{name}.jsonl"
            try:
                return candidate, open(candidate, "xb")
            except FileExistsError:
                continue
    except OSError:
        return None
    return None


def _session_stem(name: str) -> str | None:
    match = _SESSION_NAME.fullmatch(name) or _FATAL_NAME.fullmatch(name)
    return match.group(1) if match else None


def _prune(folder: Path, *, keep: Path) -> None:
    """Keep the newest KEEP_SESSIONS sessions and at most KEEP_TOTAL_BYTES.

    Only regular files (by lstat, so never a symlink) named exactly as this
    module names them, directly inside ``folder``, are considered or deleted.
    """
    try:
        current = _session_stem(keep.name)
        sessions: dict[str, list] = {}
        with os.scandir(folder) as entries:
            for entry in entries:
                stem = _session_stem(entry.name)
                if stem is None:
                    continue
                try:
                    info = os.lstat(folder / entry.name)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                sessions.setdefault(stem, []).append((folder / entry.name, info))
        now = time.time()
        newest_first = sorted(
            sessions.items(),
            key=lambda item: (item[0] == current, max(info.st_mtime for _, info in item[1])),
            reverse=True,
        )
        kept = total = deleted = freed = 0
        full = False  # once a session does not fit, it and every older one go
        for stem, files in newest_first:
            size = sum(info.st_size for _, info in files)
            protected = stem == current or now - max(info.st_mtime for _, info in files) < _RECENT_SECONDS
            if not protected and (kept >= KEEP_SESSIONS or total + size > KEEP_TOTAL_BYTES):
                full = True
            if protected or not full:
                kept += 1
                total += size
                continue
            for file, info in files:
                try:
                    # Re-check just before deleting; unlink never follows a link.
                    if stat.S_ISREG(os.lstat(file).st_mode):
                        os.unlink(file)
                        deleted += 1
                        freed += info.st_size
                except OSError:
                    continue  # for example, still open in another process on Windows
        if deleted:
            event("log_retention", deleted_files=deleted, freed_mb=round(freed / (1024 * 1024), 1),
                  kept_sessions=kept)
    except Exception:
        pass


# ---- crash capture ---------------------------------------------------------

def _start_crash_capture(session_path: Path) -> None:
    global _fatal_handle, _fatal_path, _fatal_previously_enabled
    global _previous_excepthook, _previous_threading_excepthook
    # Chain whatever is installed now; never chain to ourselves after a restart.
    if sys.excepthook is not _excepthook:
        _previous_excepthook = sys.excepthook
        sys.excepthook = _excepthook
    if threading.excepthook is not _threading_excepthook:
        _previous_threading_excepthook = threading.excepthook
        threading.excepthook = _threading_excepthook
    # faulthandler keeps writing to this handle until stop(). On Windows it can
    # also report an access violation that native code then handles itself, so
    # a non-empty fatal file is a lead, not proof that the process died.
    try:
        fatal = session_path.with_name(session_path.stem + "-fatal.txt")
        handle = open(fatal, "x", encoding="utf-8")
        _fatal_previously_enabled = faulthandler.is_enabled()
        faulthandler.enable(file=handle, all_threads=True)
        _fatal_handle, _fatal_path = handle, fatal
    except Exception:
        _fatal_handle = _fatal_path = None


def _stop_crash_capture() -> None:
    global _fatal_handle, _fatal_path
    if sys.excepthook is _excepthook:
        sys.excepthook = _previous_excepthook or sys.__excepthook__
    if threading.excepthook is _threading_excepthook:
        threading.excepthook = _previous_threading_excepthook or threading.__excepthook__
    handle, fatal = _fatal_handle, _fatal_path
    _fatal_handle = _fatal_path = None
    if handle is None:
        return
    try:
        faulthandler.disable()
        if _fatal_previously_enabled and sys.__stderr__ is not None:
            faulthandler.enable(file=sys.__stderr__, all_threads=True)
    except Exception:
        pass
    try:
        handle.close()
        info = os.lstat(fatal)
        if stat.S_ISREG(info.st_mode) and info.st_size == 0:
            os.unlink(fatal)
    except OSError:
        pass


def _exception_fields(exc_type, exc, tb) -> dict:
    return {
        "type": getattr(exc_type, "__name__", str(exc_type)),
        "message": _printable(exc),
        "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
    }


def _excepthook(exc_type, exc, tb) -> None:
    try:
        event("crash", hook="sys.excepthook", **_exception_fields(exc_type, exc, tb))
    finally:
        (_previous_excepthook or sys.__excepthook__)(exc_type, exc, tb)


def _threading_excepthook(args) -> None:
    try:
        if args.exc_type is not SystemExit:  # the default hook ignores it too
            name = args.thread.name if args.thread is not None else None
            event("exception", hook="threading.excepthook", thread_name=name,
                  **_exception_fields(args.exc_type, args.exc_value, args.exc_traceback))
    finally:
        (_previous_threading_excepthook or threading.__excepthook__)(args)


# ---- health ----------------------------------------------------------------

def _memory() -> dict:
    """Resident memory in MB; the platform's current value where it has one."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return {}
            mb = 1024 * 1024
            return {"rss_mb": round(counters.WorkingSetSize / mb, 1),
                    "rss_peak_mb": round(counters.PeakWorkingSetSize / mb, 1)}
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024  # bytes vs KiB
        # getrusage reports only the peak resident size.
        return {"rss_mb": round(peak_mb, 1), "rss_peak_mb": round(peak_mb, 1), "rss_basis": "peak"}
    except Exception:
        return {}


def health() -> dict:
    """One health sample: memory, threads and garbage-collector counts."""
    return {**_memory(), "threads": threading.active_count(), "gc_counts": list(gc.get_count())}


def _start_health() -> None:
    global _health_stop, _health_thread
    stopping = threading.Event()

    def run():
        while not stopping.wait(HEALTH_INTERVAL):
            event("health", **health())

    thread = threading.Thread(target=run, name="session-log-health", daemon=True)
    with _lock:
        if _handle is None:
            return
        _health_stop, _health_thread = stopping, thread
    try:
        thread.start()
    except RuntimeError as exc:  # no thread available; the log itself still works
        event("error", where="session log health", message=str(exc))


def _register_atexit() -> None:
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(stop, "exit")
        _atexit_registered = True
