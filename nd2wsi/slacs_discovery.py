"""Read-only discovery of an existing SLACS Python process on Windows.

This does not start/import SLACS, inspect hardware, or open controller sockets.
The viewer's own PowerShell child reads process metadata; the legacy Python
installation is untouched. Detection is not proof of a working motion stream.

A PowerShell search finds SLACS. Once found, later checks only ask Windows
whether that exact process (its pid and creation time) is still alive, which
takes well under a millisecond, so a slow PowerShell start on an old PC can no
longer make a running SLACS look lost. A check that cannot tell keeps the found
session for ``FAILURE_GRACE`` seconds before it reports an error. Every
PowerShell run and every failed or recovered check is written to the session log.
"""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from . import session_log

SCRIPT_NAME = "SniperCellSorterGUIv3.1.py"
_PYTHON_NAMES = frozenset(("python.exe", "pythonw.exe"))
_CREATE_NO_WINDOW = 0x08000000
# A PowerShell search can take several seconds to start on an old instrument PC.
QUERY_TIMEOUT = 10.0
# Checks that cannot tell (rather than a confirmed exit) keep a found SLACS this long.
FAILURE_GRACE = 15.0
# Window close waits at most this long for a query in progress; a late result is dropped.
STOP_JOIN_SECONDS = 2.5
# How often each monitor summarises its liveness checks in the session log.
LIVENESS_SUMMARY_SECONDS = 60.0
_PROCESS_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$rows = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop | ForEach-Object {
    $created = $null
    if ($null -ne $_.CreationDate) { $created = $_.CreationDate.ToUniversalTime().ToString('o') }
    [PSCustomObject]@{pid = $_.ProcessId; created = $created; name = $_.Name; command_line = $_.CommandLine}
})
ConvertTo-Json -InputObject $rows -Compress -Depth 3
"""


def _split_windows_command_line(command_line: str) -> list[str]:
    """Split Windows C-runtime quoting, without invoking a shell or interpreter.

    Backslashes are special only before quotes. Reject an unclosed quote instead
    of guessing a script identity from incomplete metadata.
    """
    if not isinstance(command_line, str) or "\0" in command_line:
        raise ValueError("Invalid process command line")
    result = []
    index, length = 0, len(command_line)
    while index < length:
        while index < length and command_line[index] in " \t":
            index += 1
        if index == length:
            break
        value, quoted = [], False
        while index < length and (quoted or command_line[index] not in " \t"):
            slashes = 0
            while index < length and command_line[index] == "\\":
                slashes += 1
                index += 1
            if index < length and command_line[index] == '"':
                value.append("\\" * (slashes // 2))
                if slashes % 2:
                    value.append('"')
                    index += 1
                elif quoted and index + 1 < length and command_line[index + 1] == '"':
                    value.append('"')
                    index += 2
                else:
                    quoted = not quoted
                    index += 1
            else:
                value.append("\\" * slashes)
                if index < length and (quoted or command_line[index] not in " \t"):
                    value.append(command_line[index])
                    index += 1
        if quoted:
            raise ValueError("Incomplete process command line")
        result.append("".join(value))
    return result


def _script_argument(command_line: str) -> str | None:
    """Return only Python's script operand, never a later application argument."""
    arguments = _split_windows_command_line(command_line)
    if not arguments or ntpath.basename(arguments[0]).lower() not in (
            "python", "pythonw", "python.exe", "pythonw.exe"):
        return None
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            return arguments[index] if index < len(arguments) and arguments[index] != "-" else None
        if not argument or argument == "-":
            return None
        if not argument.startswith("-"):
            return argument
        if argument == "--check-hash-based-pycs":
            if index >= len(arguments):
                return None
            index += 1
            continue
        if argument.startswith("--check-hash-based-pycs="):
            continue
        if argument.startswith("--"):
            return None
        # Python 2.7 and Python 3 interpreter flags. Options taking a value
        # consume that value even if it happens to be the target script's name.
        options = argument[1:]
        position = 0
        while position < len(options):
            option = options[position]
            if option in "cmhV?":
                return None
            if option in "WXQ":
                if position + 1 == len(options):
                    if index >= len(arguments):
                        return None
                    index += 1
                break
            if option not in "bBdEiIOPqRsStuvx3":
                return None
            position += 1
    return None


def _state(available=False, session_id=None, status="not_running"):
    return {"available": available, "session_id": session_id, "status": status}


def _identify_processes(rows) -> dict:
    """Fail closed when a Python candidate's identity cannot be established."""
    return _identify(rows)[0]


def _identify(rows) -> tuple[dict, dict | None]:
    """The detection state and, for exactly one match, its ``pid`` and ``created`` time."""
    if not isinstance(rows, list):
        return _state(status="error"), None
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            return _state(status="error"), None
        if row["name"].lower() not in _PYTHON_NAMES:
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
    if not matches:
        return _state(), None
    session_id, process = matches[0]
    return _state(True, session_id, "running"), process


def _query_windows_processes(*, timeout: float):
    """Read local CIM metadata with a bounded, hidden viewer-owned child process."""
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    executable = ntpath.join(system_root, r"System32\WindowsPowerShell\v1.0\powershell.exe")
    result = subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PROCESS_QUERY],
        stdin=subprocess.DEVNULL, capture_output=True,
        shell=False, check=True, text=True, encoding="utf-8-sig", errors="strict",
        timeout=timeout, creationflags=_CREATE_NO_WINDOW,
    )
    return json.loads(result.stdout)


def _query_failure(exc) -> str:
    """A short, private-data-free reason for a failed PowerShell query."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"exit code {exc.returncode}"
    if isinstance(exc, (ValueError, UnicodeError)):
        return "invalid output"
    if isinstance(exc, OSError):
        return f"could not start PowerShell ({type(exc).__name__})"
    return type(exc).__name__


class _ProcessQueryCache:
    """Coalesce default monitors' CIM reads, including failed queries.

    Each real PowerShell run is logged with its duration, its outcome and, on
    failure, the reason, so the query cadence on an instrument PC can be read back.
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

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0, _WAIT_TIMEOUT = 0x0, 0x102
_ERROR_INVALID_PARAMETER = 87  # OpenProcess on a pid that no longer exists
# CIM reports creation times in microseconds; FILETIME counts 100 ns ticks.
_CREATION_TOLERANCE_TICKS = 10_000_000


def _filetime(created: str) -> int:
    """CIM's ISO creation time as a Windows FILETIME tick count."""
    birth = datetime.fromisoformat(created)
    if birth.tzinfo is None:
        raise ValueError("creation time has no time zone")
    return (birth - _FILETIME_EPOCH) // timedelta(microseconds=1) * 10


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
            self._found = {"session_id": state["session_id"], **process}
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
            # Never wait out a slow PowerShell run; its late result is dropped.
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
