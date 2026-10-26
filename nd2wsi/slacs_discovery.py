"""Read-only discovery of an existing SLACS Python process on Windows.

This does not start/import SLACS, inspect hardware, or open controller sockets.
The viewer's own PowerShell child reads process metadata; the legacy Python
installation is untouched. Detection is not proof of a working motion stream.
"""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

SCRIPT_NAME = "SniperCellSorterGUIv3.1.py"
_PYTHON_NAMES = frozenset(("python.exe", "pythonw.exe"))
_CREATE_NO_WINDOW = 0x08000000
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
    if not isinstance(rows, list):
        return _state(status="error")
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            return _state(status="error")
        if row["name"].lower() not in _PYTHON_NAMES:
            continue
        command_line = row.get("command_line")
        if not isinstance(command_line, str) or not command_line.strip():
            return _state(status="error")
        try:
            script = _script_argument(command_line)
        except ValueError:
            return _state(status="error")
        if script is None or ntpath.basename(script).lower() != SCRIPT_NAME.lower():
            continue
        pid, created = row.get("pid"), row.get("created")
        if type(pid) is not int or not 0 < pid < 2**32 or not isinstance(created, str):
            return _state(status="error")
        try:
            birth = datetime.fromisoformat(created)
        except ValueError:
            return _state(status="error")
        if birth.tzinfo is None:
            return _state(status="error")
        identity = f"{pid}\0{created}\0{ntpath.normcase(ntpath.normpath(script))}"
        matches.append("slacs-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32])
    if len(matches) > 1:
        return _state(status="ambiguous")
    return _state(True, matches[0], "running") if matches else _state()


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


class _ProcessQueryCache:
    """Coalesce default monitors' CIM reads, including failed queries."""

    def __init__(self, *, lifetime=2.0, clock=time.monotonic, query=None):
        self._lifetime, self._clock, self._query = lifetime, clock, query
        self._lock = threading.Lock()
        self._expires = float("-inf")
        self._rows = None
        self._failed = False

    def read(self, *, timeout):
        with self._lock:
            if self._clock() >= self._expires:
                try:
                    self._rows = (self._query or _query_windows_processes)(timeout=timeout)
                    self._failed = False
                except Exception:
                    self._rows, self._failed = None, True
                self._expires = self._clock() + self._lifetime
            if self._failed:
                raise OSError("Process metadata query failed")
            return self._rows


_DEFAULT_QUERY_CACHE = _ProcessQueryCache()


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
    """

    def __init__(self, *, on_change=None, process_query=None, platform=None,
                 poll_interval=2.0, query_timeout=2.0, autostart=True):
        for name, value in (("poll_interval", poll_interval), ("query_timeout", query_timeout)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        self._supported = process_query is not None or (sys.platform if platform is None else platform) == "win32"
        self._query = process_query if process_query is not None else (
            lambda: _DEFAULT_QUERY_CACHE.read(timeout=query_timeout))
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
            try:
                snapshot = _identify_processes(self._query())
            except Exception:
                snapshot = _state(status="error")
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
            thread.join(timeout=self._query_timeout + 0.5)
        if changed:
            self._notify(stopped)
        return thread is None or not thread.is_alive()
