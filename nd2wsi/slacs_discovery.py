"""Read-only discovery of an existing SLACS Python process on Windows.

This does not start/import SLACS, inspect hardware, or open controller sockets.
Process metadata is read directly in-process through Win32 ``kernel32``
(``CreateToolhelp32Snapshot``, ``Process32FirstW``/``Process32NextW``,
``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)``, ``QueryFullProcessImageNameW``,
``GetProcessTimes``) and ``ntdll`` (``NtQueryInformationProcess`` with
``ProcessCommandLineInformation``) — zero PowerShell or WMI subprocesses, so it
runs in milliseconds and works under AppLocker / Constrained Language Mode.

Once SLACS is found, later checks only ask Windows whether that exact process
(its pid and creation time) is still alive, which takes well under a millisecond.
A check that cannot tell keeps the found session for ``FAILURE_GRACE`` seconds
before it reports an error. If a Python process rejects ``OpenProcess`` with
``ERROR_ACCESS_DENIED (5)`` (indicating an elevated/Administrator SLACS blocked
by Windows UIPI), discovery exposes ``status="elevated"`` with a diagnostic hint.
"""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import re
import stat
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from . import session_log
from . import slacs_identity as _slacs_identity

SCRIPT_NAME = _slacs_identity.SCRIPT_NAME
_script_argument = _slacs_identity._script_argument
_split_windows_command_line = _slacs_identity._split_windows_command_line

_PYTHON_NAMES = frozenset(("python.exe", "pythonw.exe"))
# Kept for backwards compatibility with callers/tests configuring query budgets.
QUERY_TIMEOUT = 10.0
# Checks that cannot tell (rather than a confirmed exit) keep a found SLACS this long.
FAILURE_GRACE = 15.0
# Window close waits at most this long for a query in progress; a late result is dropped.
STOP_JOIN_SECONDS = 2.5
# How often each monitor summarises its liveness checks in the session log.
LIVENESS_SUMMARY_SECONDS = 60.0

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_COMMAND_LINE_INFORMATION = 60
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0, _WAIT_TIMEOUT = 0x0, 0x102
_ERROR_ACCESS_DENIED = 5
_ERROR_NO_MORE_FILES = 18
_ERROR_INVALID_PARAMETER = 87  # OpenProcess on a pid that no longer exists
_STATUS_ACCESS_DENIED_UNSIGNED = 0xC0000022
_STATUS_ACCESS_DENIED_SIGNED = -1073741790
# FILETIME counts 100 ns ticks; tolerate up to 1 s rounding/skew.
_CREATION_TOLERANCE_TICKS = 10_000_000
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)

ELEVATED_HINT = (
    "SLACS appears to be running as Administrator (elevated). Windows UIPI blocks "
    "a non-elevated viewer from inspecting or following an elevated SLACS process; "
    "run SLACS without Administrator privileges or run both at the same privilege level."
)


def _state(available=False, session_id=None, status="not_running", *, hint: str | None = None):
    result = {"available": available, "session_id": session_id, "status": status}
    if hint is not None:
        result["hint"] = hint
    return result


def _identify_processes(rows) -> dict:
    """Fail closed when a Python candidate's identity cannot be established."""
    return _identify(rows)[0]


def _identify(rows) -> tuple[dict, dict | None]:
    """The detection state and, for exactly one match, its ``pid`` and ``created`` time."""
    if not isinstance(rows, list):
        return _state(status="error"), None
    matches = []
    access_denied = False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            return _state(status="error"), None
        if row["name"].lower() not in _PYTHON_NAMES:
            continue
        if row.get("access_denied") is True or (
            row.get("winerror") == _ERROR_ACCESS_DENIED and not row.get("command_line")
        ):
            access_denied = True
            continue
        command_line = row.get("command_line")
        if not isinstance(command_line, str) or not command_line.strip():
            return _state(status="error"), None
        try:
            script = _script_argument(command_line)
        except ValueError:
            return _state(status="error"), None
        if script is None or ntpath.basename(script).lower() != SCRIPT_NAME.lower():
            continue
        pid, created = row.get("pid"), row.get("created")
        if type(pid) is not int or not 0 < pid < 2**32 or not isinstance(created, str):
            return _state(status="error"), None
        try:
            birth = datetime.fromisoformat(created)
        except ValueError:
            return _state(status="error"), None
        if birth.tzinfo is None:
            return _state(status="error"), None
        identity = f"{pid}\0{created}\0{ntpath.normcase(ntpath.normpath(script))}"
        matches.append(("slacs-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
                        {"pid": pid, "created": created}))
    if len(matches) > 1:
        return _state(status="ambiguous"), None
    if access_denied:
        return _state(status="elevated", hint=ELEVATED_HINT), None
    if not matches:
        return _state(), None
    session_id, process = matches[0]
    return _state(True, session_id, "running"), process


def _filetime(created: str) -> int:
    """An ISO UTC creation time as a Windows FILETIME tick count."""
    birth = datetime.fromisoformat(created)
    if birth.tzinfo is None:
        raise ValueError("creation time has no time zone")
    return (birth - _FILETIME_EPOCH) // timedelta(microseconds=1) * 10


def _filetime_to_iso(ticks: int) -> str:
    """A Windows FILETIME 100-ns tick count as a UTC ISO-8601 string."""
    dt = _FILETIME_EPOCH + timedelta(microseconds=int(ticks) // 10)
    return dt.isoformat().replace("+00:00", "Z")


@lru_cache(maxsize=1)
def _kernel32():
    """A private, prototyped kernel32; never the shared ``ctypes.windll``. Windows only."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, restype, argtypes in (
            ("OpenProcess", wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
            ("CloseHandle", wintypes.BOOL, (wintypes.HANDLE,)),
            ("WaitForSingleObject", wintypes.DWORD, (wintypes.HANDLE, wintypes.DWORD)),
            ("GetProcessTimes", wintypes.BOOL, (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4)):
        function = getattr(kernel32, name)
        function.restype, function.argtypes = restype, list(argtypes)
    return ctypes, wintypes, kernel32


@lru_cache(maxsize=1)
def _win32_process_api():
    """Private prototyped kernel32 + ntdll handles and Win32 structs for zero-subprocess discovery."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_int32),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_uint16),
            ("MaximumLength", ctypes.c_uint16),
            ("Buffer", ctypes.c_void_p),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    for dll, name, restype, argtypes in (
        (kernel32, "CreateToolhelp32Snapshot", wintypes.HANDLE, (wintypes.DWORD, wintypes.DWORD)),
        (kernel32, "Process32FirstW", wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))),
        (kernel32, "Process32NextW", wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))),
        (kernel32, "OpenProcess", wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
        (kernel32, "QueryFullProcessImageNameW", wintypes.BOOL,
         (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))),
        (kernel32, "GetProcessTimes", wintypes.BOOL, (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4),
        (kernel32, "CloseHandle", wintypes.BOOL, (wintypes.HANDLE,)),
        (ntdll, "NtQueryInformationProcess", ctypes.c_int32,
         (wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))),
    ):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)

    return SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        kernel32=kernel32,
        ntdll=ntdll,
        PROCESSENTRY32W=PROCESSENTRY32W,
        UNICODE_STRING=UNICODE_STRING,
        get_last_error=ctypes.get_last_error,
        set_last_error=ctypes.set_last_error,
    )


def _is_invalid_handle(handle, ptr_size: int) -> bool:
    if not handle:
        return True
    mask = (1 << (ptr_size * 8)) - 1
    return handle in (-1, mask)


def _is_status_access_denied(status: int) -> bool:
    return status in (_STATUS_ACCESS_DENIED_SIGNED, _STATUS_ACCESS_DENIED_UNSIGNED) or (
        (status & 0xFFFFFFFF) == _STATUS_ACCESS_DENIED_UNSIGNED
    )


def _read_process_command_line(api, handle) -> tuple[str | None, bool]:
    """Read a process's command line via ``NtQueryInformationProcess(ProcessCommandLineInformation)``.

    Returns ``(command_line, access_denied)``. Read-only; requires only
    ``PROCESS_QUERY_LIMITED_INFORMATION`` on Windows 8.1 / 10 / 11.
    """
    ctypes = api.ctypes
    length = api.wintypes.ULONG(0)
    status = api.ntdll.NtQueryInformationProcess(
        handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(length),
    )
    if _is_status_access_denied(int(status)):
        return None, True
    if length.value == 0 or length.value > 1 << 20:
        return None, False
    buffer = ctypes.create_string_buffer(length.value)
    status = api.ntdll.NtQueryInformationProcess(
        handle, _PROCESS_COMMAND_LINE_INFORMATION, buffer, length.value, ctypes.byref(length),
    )
    if _is_status_access_denied(int(status)):
        return None, True
    if status != 0:
        return None, False
    unicode_string = ctypes.cast(buffer, ctypes.POINTER(api.UNICODE_STRING)).contents
    if not unicode_string.Buffer or unicode_string.Length == 0:
        return None, False
    return ctypes.wstring_at(unicode_string.Buffer, unicode_string.Length // ctypes.sizeof(ctypes.c_wchar)), False


def _query_windows_processes(*, timeout: float = QUERY_TIMEOUT, api=None) -> list[dict]:
    """Enumerate local Python processes directly via Win32 Toolhelp32 + NtQueryInformationProcess.

    Zero subprocesses (never spawns PowerShell or WMI). Detects elevated Python
    processes that return ``ERROR_ACCESS_DENIED (5)`` on ``OpenProcess`` (or
    ``STATUS_ACCESS_DENIED`` on command-line inspection) so UIPI blocks surface
    a clear ``status="elevated"`` diagnostic instead of a silent timeout/miss.
    """
    del timeout  # kept for signature compatibility; in-process Toolhelp32 finishes in <5 ms
    if api is None:
        if sys.platform != "win32":
            raise OSError("Win32 process enumeration is only available on Windows")
        api = _win32_process_api()

    ctypes = api.ctypes
    wintypes = api.wintypes
    kernel32 = api.kernel32
    ptr_size = ctypes.sizeof(ctypes.c_void_p)

    api.set_last_error(0)
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if _is_invalid_handle(snapshot, ptr_size):
        err = int(api.get_last_error())
        raise OSError(err, f"CreateToolhelp32Snapshot failed (error {err})")

    rows: list[dict] = []
    try:
        entry = api.PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(api.PROCESSENTRY32W)
        api.set_last_error(0)
        ok = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        if not ok:
            err = int(api.get_last_error())
            if err in (0, _ERROR_NO_MORE_FILES):
                return rows
            raise OSError(err, f"Process32FirstW failed (error {err})")

        while True:
            exe_name = str(entry.szExeFile)
            if exe_name.lower() in _PYTHON_NAMES:
                pid = int(entry.th32ProcessID)
                api.set_last_error(0)
                handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not handle:
                    err = int(api.get_last_error())
                    if err == _ERROR_INVALID_PARAMETER:
                        pass  # process exited between snapshot and OpenProcess
                    elif err == _ERROR_ACCESS_DENIED:
                        rows.append({
                            "pid": pid,
                            "created": None,
                            "name": exe_name,
                            "command_line": None,
                            "access_denied": True,
                            "winerror": _ERROR_ACCESS_DENIED,
                        })
                    else:
                        rows.append({"pid": pid, "created": None, "name": exe_name, "command_line": None})
                else:
                    try:
                        size = wintypes.DWORD(32768)
                        image_buf = ctypes.create_unicode_buffer(size.value)
                        image_ok = bool(kernel32.QueryFullProcessImageNameW(
                            handle, 0, image_buf, ctypes.byref(size),
                        ))
                        if image_ok and image_buf.value:
                            resolved_name = ntpath.basename(image_buf.value)
                            if resolved_name.lower() not in _PYTHON_NAMES:
                                resolved_name = None
                        else:
                            resolved_name = exe_name

                        if resolved_name is not None:
                            times = [wintypes.FILETIME() for _ in range(4)]
                            created_iso = None
                            if kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                                ticks = int((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
                                created_iso = _filetime_to_iso(ticks)
                            command_line, cmd_denied = _read_process_command_line(api, handle)
                            row: dict = {
                                "pid": pid,
                                "created": created_iso,
                                "name": resolved_name,
                                "command_line": command_line,
                            }
                            if cmd_denied:
                                row["access_denied"] = True
                                row["winerror"] = _ERROR_ACCESS_DENIED
                            rows.append(row)
                    finally:
                        kernel32.CloseHandle(handle)

            entry.dwSize = ctypes.sizeof(api.PROCESSENTRY32W)
            api.set_last_error(0)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                err = int(api.get_last_error())
                if err not in (0, _ERROR_NO_MORE_FILES):
                    raise OSError(err, f"Process32NextW failed (error {err})")
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def _query_failure(exc) -> str:
    """A short, private-data-free reason for a failed process query."""
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutExpired":
        return "timeout"
    returncode = getattr(exc, "returncode", None)
    if returncode is not None:
        return f"exit code {returncode}"
    if isinstance(exc, (ValueError, UnicodeError)):
        return "invalid output"
    if isinstance(exc, OSError):
        winerr = getattr(exc, "winerror", None)
        if winerr is not None:
            return f"win32 error {winerr}"
        return f"process query failed ({type(exc).__name__})"
    return type(exc).__name__


class _ProcessQueryCache:
    """Coalesce default monitors' process reads, including failed queries.

    Each query is logged with its duration, its outcome and, on failure, the
    reason, so the query cadence on an instrument PC can be read back.
    """

    def __init__(self, *, lifetime=2.0, clock=time.monotonic, query=None):
        self._lifetime, self._clock, self._query = lifetime, clock, query
        self._lock = threading.Lock()
        self._expires = float("-inf")
        self._rows = None
        self._failed = False

    def read(self, *, timeout):
        with self._lock:
            if self._clock() >= self._expires:
                started = time.monotonic()
                try:
                    self._rows = (self._query or _query_windows_processes)(timeout=timeout)
                    self._failed = False
                    session_log.event("slacs_query", ok=True, seconds=round(time.monotonic() - started, 3),
                                      python_processes=len(self._rows) if isinstance(self._rows, list) else None)
                except Exception as exc:
                    self._rows, self._failed = None, True
                    session_log.event("slacs_query", ok=False, seconds=round(time.monotonic() - started, 3),
                                      reason=_query_failure(exc))
                self._expires = self._clock() + self._lifetime
            if self._failed:
                raise OSError("Process metadata query failed")
            return self._rows


_DEFAULT_QUERY_CACHE = _ProcessQueryCache()


def _process_creation_filetime(pid: int) -> int | None:
    """100-ns Windows creation FILETIME of ``pid`` via ``GetProcessTimes``, or None."""
    if sys.platform != "win32" or type(pid) is not int or pid <= 0:
        return None
    try:
        ctypes, wintypes, kernel32 = _kernel32()
    except Exception:  # noqa: BLE001 - unavailable off Windows or when mocked out
        return None
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return int((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def _bound_process_identifier(bound_pid: int, bound_created: int | None = None):
    """Return an ``identifier(pid, created=None)`` callback pinned to one SLACS process."""
    expected_pid = int(bound_pid)
    expected_created = int(bound_created) if bound_created is not None else None

    def identify(pid, created=None) -> bool:
        if isinstance(pid, (bool, float)):
            return False
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return False
        if pid_int != expected_pid:
            return False
        if expected_created is not None and created is not None:
            if isinstance(created, (bool, float)):
                return False
            try:
                created_int = int(created)
            except (TypeError, ValueError):
                return False
            if created_int != expected_created:
                return False
        return True

    return identify


def _windows_process_alive(pid: int, created: str) -> bool | None:
    """Whether that exact process still runs: True, False once it has exited or its
    pid belongs to another process, None when Windows cannot tell. Read-only."""
    ctypes, wintypes, kernel32 = _kernel32()
    expected = _filetime(created)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER else None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        actual = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if abs(actual - expected) > _CREATION_TOLERANCE_TICKS:
            return False  # the pid now belongs to a different process
        waited = kernel32.WaitForSingleObject(handle, 0)
        if waited == _WAIT_TIMEOUT:
            return True
        return False if waited == _WAIT_OBJECT_0 else None
    finally:
        kernel32.CloseHandle(handle)


class SlacsDiscovery:
    """Cached process-session availability, independent of viewer/UI state.

    Construction starts a daemon poller by default. ``snapshot()`` only copies
    cached state. ``on_change(snapshot)`` runs outside locks on transitions, not
    at construction; it must be quick and must tolerate owner shutdown. ``stop``
    is idempotent, returns whether the worker has stopped, and never joins its
    own callback thread (returns False there until that thread exits). Queries
    have a deadline; injected test providers must also return in bounded time.

    ``process_query`` and ``autostart=False`` allow hardware-free tests through
    ``poll_once()``. A supplied provider explicitly enables testing on non-Windows.
    No process command line or path is exposed in snapshots or callbacks.

    Once a search finds SLACS, ``liveness(pid, created) -> True | False | None``
    checks that process instead of searching again (the Windows check by
    default; none with an injected ``process_query`` unless one is passed).
    False, a confirmed exit or a reused pid, starts a new search at once. While
    checks cannot tell (None, or a failed search), the found session is kept
    for ``failure_grace`` seconds.
    """

    def __init__(self, *, on_change=None, process_query=None, platform=None,
                 poll_interval=2.0, query_timeout=QUERY_TIMEOUT, autostart=True,
                 liveness=None, failure_grace=FAILURE_GRACE, clock=time.monotonic, name=None):
        for option, value in (("poll_interval", poll_interval), ("query_timeout", query_timeout)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{option} must be a positive finite number")
        if (isinstance(failure_grace, bool) or not isinstance(failure_grace, (int, float))
                or not math.isfinite(failure_grace) or failure_grace < 0):
            raise ValueError("failure_grace must be a finite number of seconds, zero or more")
        resolved_platform = sys.platform if platform is None else platform
        self._supported = process_query is not None or resolved_platform == "win32"
        self._query = process_query if process_query is not None else (
            lambda: _DEFAULT_QUERY_CACHE.read(timeout=query_timeout))
        if liveness is None and process_query is None and resolved_platform == "win32":
            liveness = _windows_process_alive
        self._liveness = liveness
        self._failure_grace = float(failure_grace)
        self._clock = clock
        self._name = name
        self._found = None  # {"session_id", "pid", "created"} of the SLACS a search found
        self._failing_since = None
        self._failed_checks = 0
        self._failure_reason = None
        self._checks = self._check_failures = 0
        self._check_max_ms = 0.0
        self._summary_at = clock()
        self._on_change = on_change
        self._poll_interval, self._query_timeout = float(poll_interval), float(query_timeout)
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._snapshot = _state(status="checking" if self._supported else "unsupported")
        self._thread = None
        if autostart and self._supported:
            self.start()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def slacs_pid(self) -> int | None:
        """The pid of the SLACS this monitor currently reports, for read-only window lookups."""
        with self._lock:
            found, snapshot = self._found, self._snapshot
        if found is None or snapshot.get("session_id") != found["session_id"]:
            return None
        return found["pid"]

    def slacs_creation_filetime(self) -> int | None:
        """The 100-ns creation FILETIME of the currently reported SLACS process, or None."""
        with self._lock:
            found, snapshot = self._found, self._snapshot
        if found is None or snapshot.get("session_id") != found["session_id"]:
            return None
        created = found.get("creation_filetime")
        return created if type(created) is int and created >= 0 else None

    def process_identifier(self):
        """Bound ``(pid, created=None) -> bool`` callback for the currently reported SLACS, or None."""
        with self._lock:
            found, snapshot = self._found, self._snapshot
        if found is None or snapshot.get("session_id") != found["session_id"]:
            return None
        pid = found.get("pid")
        if type(pid) is not int or pid <= 0:
            return None
        created = found.get("creation_filetime")
        bound_created = created if type(created) is int and created >= 0 else None
        return _bound_process_identifier(pid, bound_created)

    def _log(self, kind, **fields):
        session_log.event(kind, monitor=self._name, **fields)

    def _check_liveness(self, found):
        started = time.monotonic()
        try:
            alive = self._liveness(found["pid"], found["created"])
            reason = None if alive is not None else "Windows could not tell"
        except Exception as exc:  # noqa: BLE001 - a failed check is "cannot tell"
            alive, reason = None, type(exc).__name__
        elapsed_ms = (time.monotonic() - started) * 1000
        self._checks += 1
        self._check_max_ms = max(self._check_max_ms, elapsed_ms)
        if alive is None:
            self._check_failures += 1
        return (alive if alive in (True, False) else None), reason

    def _summarise_checks(self, now):
        if now - self._summary_at < LIVENESS_SUMMARY_SECONDS:
            return
        if self._checks:
            self._log("slacs_liveness_summary", checks=self._checks, failed=self._check_failures,
                      max_ms=round(self._check_max_ms, 2), seconds=round(now - self._summary_at, 1))
        self._checks = self._check_failures = 0
        self._check_max_ms = 0.0
        self._summary_at = now

    def _check_failed(self, now, reason):
        """Record a check that could not tell; True while the found session is still kept."""
        if self._failing_since is None:
            self._failing_since, self._failed_checks = now, 0
            self._log("slacs_check_failing", reason=reason, grace_seconds=self._failure_grace)
        self._failed_checks += 1
        self._failure_reason = reason
        return now - self._failing_since < self._failure_grace

    def _check_recovered(self, now):
        if self._failing_since is not None:
            self._log("slacs_check_recovered", failed_checks=self._failed_checks,
                      seconds=round(now - self._failing_since, 2), last_reason=self._failure_reason)
        self._failing_since = None

    def _search(self):
        try:
            return _identify(self._query())
        except Exception:
            return _state(status="error"), None

    def _check(self) -> dict:
        """One detection: a liveness check of the found SLACS, or a search."""
        now = self._clock()
        found = self._found
        if found is not None and self._liveness is not None:
            alive, reason = self._check_liveness(found)
            self._summarise_checks(now)
            if alive is True:
                self._check_recovered(now)
                return _state(True, found["session_id"], "running")
            if alive is None and self._check_failed(now, reason):
                return _state(True, found["session_id"], "running")
            if alive is False:
                self._log("slacs_exit_seen", session_id=found["session_id"])
        state, process = self._search()
        if state["status"] == "running":
            self._found = {"session_id": state["session_id"], **process,
                           "creation_filetime": _process_creation_filetime(process["pid"])}
            self._check_recovered(now)
            return state
        if state["status"] == "error" and found is not None:
            if self._check_failed(now, "search failed"):
                return _state(True, found["session_id"], "running")
            self._log("slacs_check_gave_up", failed_checks=self._failed_checks,
                      seconds=round(now - self._failing_since, 2), last_reason=self._failure_reason)
        self._found = None
        self._failing_since = None
        return state

    def _notify(self, snapshot):
        if self._on_change is not None:
            try:
                self._on_change(dict(snapshot))
            except Exception:
                # A consumer failure must not stop subsequent process observation.
                pass

    def poll_once(self) -> dict:
        """Perform one read; tests only. UI code must use cached ``snapshot``."""
        with self._poll_lock:
            if self._stop.is_set() or not self._supported:
                return self.snapshot()
            snapshot = self._check()
            with self._lock:
                if self._stop.is_set():
                    return dict(self._snapshot)
                changed = snapshot != self._snapshot
                self._snapshot = snapshot
        if changed and not self._stop.is_set():
            self._notify(snapshot)
        return self.snapshot()

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            if self._stop.wait(self._poll_interval):
                break

    def start(self):
        with self._lock:
            if not self._supported or self._stop.is_set() or self._thread is not None:
                return self
            self._thread = threading.Thread(target=self._run, name="nd2wsi-slacs-discovery", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        stopped = _state(status="stopped")
        with self._lock:
            changed = self._snapshot != stopped
            self._snapshot = stopped
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # Never wait out a slow query; its late result is dropped.
            thread.join(timeout=min(self._query_timeout + 0.5, STOP_JOIN_SECONDS))
        if changed:
            self._notify(stopped)
        return thread is None or not thread.is_alive()


_SESSION_ID = re.compile(r"slacs-[0-9a-f]{32}")
_CLAIM_NAME = re.compile(r"slacs-[0-9a-f]{32}\.claim")
OFFER_CLAIM_LIFETIME = 7 * 24 * 3600.0


class SlacsOfferClaims:
    """Offer each detected SLACS process once across this user's viewer windows.

    Every viewer window is its own OS process, so a claim is an exclusive file
    create in a folder those windows share; callers keep one folder per window
    role so an Agent window never takes a User window's offer. The window that
    created a claim keeps getting True for that session, and every other window,
    including ones opened later, gets False. A restarted SLACS process has a new
    session identity and is offered again. Claims older than ``lifetime`` are
    pruned, except the session being claimed, which is running right now;
    only regular files with this class's own names are removed. If the
    folder cannot be written, ``claim`` returns True: one offer per window.
    """

    def __init__(self, folder, claimant, *, lifetime=OFFER_CLAIM_LIFETIME, clock=time.time):
        self._folder = Path(folder)
        self._claimant = str(claimant)
        self._lifetime = float(lifetime)
        self._clock = clock

    def claim(self, session_id) -> bool:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            return False
        path = self._folder / f"{session_id}.claim"
        try:
            self._folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return True
        self._prune(keep=path.name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            return self._claimant_of(path) == self._claimant
        except OSError:
            return True
        record = {"format": "nd2wsi-slacs-offer/1", "session_id": session_id, "claimant": self._claimant}
        try:
            os.write(fd, json.dumps(record).encode("utf-8"))
        except OSError:
            pass  # the empty claim still keeps other windows from offering it
        finally:
            os.close(fd)
        return True

    def _claimant_of(self, path):
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            with open(path, "rb") as handle:
                record = json.loads(handle.read(4096).decode("utf-8"))
        except (OSError, ValueError):
            return None
        return record.get("claimant") if isinstance(record, dict) else None

    def _prune(self, keep):
        cutoff = self._clock() - self._lifetime
        try:
            entries = list(os.scandir(self._folder))
        except OSError:
            return
        for entry in entries:
            if entry.name == keep or not _CLAIM_NAME.fullmatch(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    os.unlink(entry.path)
            except OSError:
                pass
