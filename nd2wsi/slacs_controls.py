"""Read SLACS's Roughness slider and Mirror checkboxes, strictly as an observer.

SLACS jogs the stage by Roughness micrometres per W/A/S/D press, and its
"Mirror Horizontal" / "Mirror Vertical" checkboxes reverse W/S and A/D. Key
following needs both, so this module reads them from SLACS's own window while
Follow runs. It never changes SLACS: windows are found by enumeration, class
name, ``InternalGetWindowText`` and window style, none of which sends SLACS a
message. The only messages ever sent are the read-only queries TBM_GETPOS,
TBM_GETRANGEMIN, TBM_GETRANGEMAX and BM_GETCHECK, each through
``SendMessageTimeoutW`` with ``SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT`` and a short
timeout, so a busy SLACS costs one timeout and is reported as busy, never waited
out. Nothing here touches the ACS controller or the keyboard.

A read fails closed. The Roughness slider is the one trackbar with SLACS's range
1..1000 (its other two sliders are 1..100); each Mirror checkbox is the one
Button with exactly that text and a checkbox style; all three share one
top-level window of the given process. A missing, duplicated or out-of-range
control, an indeterminate checkbox or any failed query makes the whole read
``ok=False`` with no values, never a guess.

The Win32 calls are Windows-only, but the module imports on every platform:
the reading logic runs against a small window adapter that tests replace, and
``SlacsControlsMonitor`` takes an injected reader.
"""

from __future__ import annotations

import math
import statistics
import sys
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from types import SimpleNamespace

from . import session_log

READ_TIMEOUT_MS = 100
POLL_INTERVAL = 0.2  # seconds between reads while Follow runs
BUSY_LATENCY_MS = 250  # a successful read slower than this also counts as "busy"
LATENCY_SUMMARY_SECONDS = 30.0
# stop() waits at most this long for a read in progress; a late result is dropped.
STOP_JOIN_SECONDS = 1.0
MAX_TIMEOUT_MS = 5000

# SLACS's Roughness slider: wx.Slider(panel, -1, 500, 1, 1000, ...).
ROUGHNESS_RANGE = (1, 1000)
MIRROR_LABELS = {"mirror_h": "Mirror Horizontal", "mirror_v": "Mirror Vertical"}
_TRACKBAR_CLASS = "msctls_trackbar32"
_BUTTON_CLASS = "button"

_TBM_GETPOS = 0x0400
_TBM_GETRANGEMIN = 0x0401
_TBM_GETRANGEMAX = 0x0402
_BM_GETCHECK = 0x00F0
_BST_UNCHECKED = 0
_BST_CHECKED = 1
# The only messages this module may send to SLACS; each only reads state.
READ_ONLY_MESSAGES = frozenset((_TBM_GETPOS, _TBM_GETRANGEMIN, _TBM_GETRANGEMAX, _BM_GETCHECK))
_SMTO_ABORTIFHUNG = 0x0002
_SMTO_ERRORONEXIT = 0x0020
_BS_TYPEMASK = 0x000F
# BS_CHECKBOX (wx's own), BS_AUTOCHECKBOX, BS_3STATE, BS_AUTO3STATE; owner-drawn is not readable.
_CHECKBOX_STYLES = frozenset((0x2, 0x3, 0x5, 0x6))
_GWL_STYLE = -16
_GA_ROOT = 2
_ERROR_TIMEOUT = 1460

# Kept sample latencies per summary window; the window's read count is exact.
_MAX_LATENCY_SAMPLES = 4096


class _Busy(Exception):
    """SLACS did not answer in time, or Windows reports its window as hung."""


class _Unreadable(Exception):
    """SLACS answered, but its controls cannot be read without guessing."""


def _empty_reading() -> dict:
    return {"ok": False, "roughness_um": None, "mirror_h": None, "mirror_v": None,
            "latency_ms": 0.0, "busy": False, "error": None}


def _is_checkbox(style) -> bool:
    return (int(style) & _BS_TYPEMASK) in _CHECKBOX_STYLES


def read_slacs_controls(pid: int, *, timeout_ms: int = READ_TIMEOUT_MS,
                        cache: dict | None = None) -> dict:
    """Windows only. One read of SLACS's Roughness slider and Mirror checkboxes in process ``pid``.

    Returns ``{"ok", "roughness_um", "mirror_h", "mirror_v", "latency_ms", "busy",
    "error"}``. Values are None unless ``ok``. ``busy`` is True when a
    ``SendMessageTimeoutW`` timed out or ``IsHungAppWindow`` says the window is
    hung. ``cache`` (optional dict owned by the caller) remembers the control
    HWNDs; they are re-validated (IsWindow, owning pid, class, text and style)
    on every read and re-discovered when stale. Never raises.
    """
    if sys.platform != "win32":
        reading = _empty_reading()
        reading["error"] = "Reading SLACS's controls is only available on Windows."
        return reading
    try:
        windows = _Win32Windows()
    except Exception as exc:  # noqa: BLE001 - report, never raise
        reading = _empty_reading()
        reading["error"] = f"Win32 is unavailable: {type(exc).__name__}"
        return reading
    return _read_controls(windows, pid, timeout_ms=timeout_ms, cache=cache)


def _read_controls(windows, pid, *, timeout_ms=READ_TIMEOUT_MS, cache=None) -> dict:
    """``read_slacs_controls`` against any window adapter (the Win32 one, or a test's)."""
    started = time.perf_counter()
    reading = _empty_reading()
    try:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise _Unreadable("No SLACS process to read.")
        if (isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int)
                or not 0 < timeout_ms <= MAX_TIMEOUT_MS):
            raise _Unreadable(f"timeout_ms must be a whole number of ms in (0, {MAX_TIMEOUT_MS}].")
        session = _ReadSession(windows, timeout_ms)
        controls = _validated_cache(windows, pid, cache)
        from_cache = controls is not None
        if controls is None:
            if cache is not None:
                cache.clear()
            controls = _discover(windows, session, pid)
            if cache is not None:
                cache.update(controls)
        roughness = controls["roughness"]
        if from_cache and session.range(roughness) != ROUGHNESS_RANGE:
            raise _Unreadable("The Roughness slider's range is no longer 1..1000.")
        position = session.send(roughness, _TBM_GETPOS)
        low, high = ROUGHNESS_RANGE
        if not low <= position <= high:
            raise _Unreadable(f"The Roughness slider reads {position}, outside 1..1000.")
        flags = {}
        for key, label in MIRROR_LABELS.items():
            state = session.send(controls[key], _BM_GETCHECK)
            if state not in (_BST_UNCHECKED, _BST_CHECKED):
                raise _Unreadable(f"'{label}' is neither checked nor unchecked.")
            flags[key] = state == _BST_CHECKED
        reading.update(ok=True, roughness_um=float(position), **flags)
    except _Busy as exc:
        # The control handles are still good; only SLACS's answer was late.
        reading["busy"] = True
        reading["error"] = str(exc)
    except _Unreadable as exc:
        reading["error"] = str(exc)
        if cache is not None:
            cache.clear()
    except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
        reading["error"] = f"Could not read SLACS's controls: {type(exc).__name__}"
        if cache is not None:
            cache.clear()
    reading["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return reading


class _ReadSession:
    """The messages of one read: read-only, each bounded, hung windows never asked."""

    def __init__(self, windows, timeout_ms: int):
        self._windows = windows
        self._timeout_ms = timeout_ms
        self._checked_roots: set = set()

    def send(self, hwnd, message) -> int:
        if message not in READ_ONLY_MESSAGES:
            raise ValueError(f"message {message:#x} is not a read-only query")
        root = self._windows.root(hwnd) or hwnd
        if root not in self._checked_roots:
            self._checked_roots.add(root)
            if self._windows.is_hung(root):
                raise _Busy("SLACS is not responding (Windows reports its window as hung).")
        return int(self._windows.send(hwnd, message, self._timeout_ms))

    def range(self, hwnd) -> tuple[int, int]:
        return self.send(hwnd, _TBM_GETRANGEMIN), self.send(hwnd, _TBM_GETRANGEMAX)


def _matches(windows, pid, hwnd, class_name, label=None) -> bool:
    """Whether ``hwnd`` is still a live ``class_name`` window of ``pid``. Sends no message."""
    if not hwnd or not windows.is_window(hwnd) or windows.owner_pid(hwnd) != pid:
        return False
    if windows.class_name(hwnd).lower() != class_name:
        return False
    if label is None:
        return True
    return windows.text(hwnd) == label and _is_checkbox(windows.style(hwnd))


def _validated_cache(windows, pid, cache) -> dict | None:
    if not cache or cache.get("pid") != pid:
        return None
    if not _matches(windows, pid, cache.get("roughness"), _TRACKBAR_CLASS):
        return None
    for key, label in MIRROR_LABELS.items():
        if not _matches(windows, pid, cache.get(key), _BUTTON_CLASS, label):
            return None
    return {"pid": pid, "roughness": cache["roughness"],
            **{key: cache[key] for key in MIRROR_LABELS}}


def _discover(windows, session, pid) -> dict:
    """Find the three controls in ``pid``'s windows; raise ``_Unreadable`` on any doubt.

    The checkboxes are matched first, by text and style alone, so no message is
    sent to a process whose window does not look like SLACS.
    """
    tops = list(windows.top_level_windows(pid))
    if not tops:
        raise _Unreadable(f"Process {pid} has no open window.")
    trackbars = []
    mirrors: dict[str, list] = {key: [] for key in MIRROR_LABELS}
    seen = set()
    for top in tops:
        for hwnd in windows.descendants(top):
            if hwnd in seen or windows.owner_pid(hwnd) != pid:
                continue
            seen.add(hwnd)
            name = windows.class_name(hwnd).lower()
            if name == _TRACKBAR_CLASS:
                trackbars.append(hwnd)
            elif name == _BUTTON_CLASS:
                text = windows.text(hwnd)
                for key, label in MIRROR_LABELS.items():
                    if text == label:
                        mirrors[key].append(hwnd)
    found = {"pid": pid}
    for key, label in MIRROR_LABELS.items():
        candidates = mirrors[key]
        if not candidates:
            raise _Unreadable(f"SLACS's '{label}' checkbox was not found.")
        if len(candidates) > 1:
            raise _Unreadable(f"More than one '{label}' checkbox; not guessing which is SLACS's.")
        if not _is_checkbox(windows.style(candidates[0])):
            raise _Unreadable(f"'{label}' is not a readable checkbox.")
        found[key] = candidates[0]
    if not trackbars:
        raise _Unreadable("SLACS's Roughness slider was not found.")
    roughness = [hwnd for hwnd in trackbars if session.range(hwnd) == ROUGHNESS_RANGE]
    if not roughness:
        raise _Unreadable("No slider has the Roughness range 1..1000.")
    if len(roughness) > 1:
        raise _Unreadable("More than one slider has the Roughness range 1..1000; "
                          "not guessing which is Roughness.")
    found["roughness"] = roughness[0]
    roots = {windows.root(found[key]) for key in ("roughness", *MIRROR_LABELS)}
    if len(roots) != 1:
        raise _Unreadable("The Roughness slider and the Mirror checkboxes are not in one window.")
    return found


# -- Win32 API with private prototypes -----------------------------------------


@lru_cache(maxsize=1)
def _win32():
    """user32 with every call this module makes prototyped.

    A private ``WinDLL`` instance, never the shared ``ctypes.windll``: declaring
    prototypes here must not change calls other code makes. Every handle,
    WPARAM/LPARAM, LRESULT and DWORD_PTR is pointer-sized, so nothing is
    truncated to a 32-bit C ``long`` on 64-bit Windows. Windows only.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t  # LONG_PTR
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def declare(function, restype, *argtypes):
        function.restype = restype
        function.argtypes = list(argtypes)

    HWND, UINT = wintypes.HWND, wintypes.UINT
    declare(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
    declare(user32.EnumChildWindows, wintypes.BOOL, HWND, WNDENUMPROC, wintypes.LPARAM)
    declare(user32.GetWindowThreadProcessId, wintypes.DWORD, HWND, wintypes.LPDWORD)
    declare(user32.IsWindow, wintypes.BOOL, HWND)
    declare(user32.IsHungAppWindow, wintypes.BOOL, HWND)
    declare(user32.GetAncestor, HWND, HWND, UINT)
    declare(user32.GetClassNameW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
    declare(user32.InternalGetWindowText, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
    declare(user32.SendMessageTimeoutW, LRESULT, HWND, UINT, wintypes.WPARAM, wintypes.LPARAM,
            UINT, UINT, ctypes.POINTER(ctypes.c_size_t))
    # 32-bit user32 exports only GetWindowLongW; GetWindowLongPtrW is its 64-bit form.
    get_long = getattr(user32, "GetWindowLongPtrW", None)
    if get_long is None:
        get_long = user32.GetWindowLongW
        declare(get_long, wintypes.LONG, HWND, ctypes.c_int)
    else:
        declare(get_long, LRESULT, HWND, ctypes.c_int)
    return SimpleNamespace(ctypes=ctypes, wintypes=wintypes, user32=user32,
                           get_window_long=get_long, WNDENUMPROC=WNDENUMPROC,
                           get_last_error=ctypes.get_last_error,
                           set_last_error=ctypes.set_last_error)


def _signed32(value: int) -> int:
    """A trackbar's int result from a pointer-sized LRESULT."""
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value & 0x80000000 else value


class _Win32Windows:
    """The window queries ``_read_controls`` makes, on real Win32. Read-only.

    Everything but ``send`` reads window-manager state and sends SLACS nothing.
    ``send`` is ``SendMessageTimeoutW`` with ``SMTO_ABORTIFHUNG |
    SMTO_ERRORONEXIT``; a timeout or hang raises ``_Busy``, any other failure
    ``_Unreadable``.
    """

    def __init__(self, api=None):
        self._api = _win32() if api is None else api

    def _enumerate(self, parent=None) -> list:
        api = self._api
        handles = []

        def collect(hwnd, _lparam):
            if hwnd:
                handles.append(int(hwnd))
            return True

        callback = api.WNDENUMPROC(collect)  # kept alive for the whole call
        if parent is None:
            api.user32.EnumWindows(callback, 0)
        else:
            api.user32.EnumChildWindows(parent, callback, 0)
        return handles

    def top_level_windows(self, pid) -> list:
        return [hwnd for hwnd in self._enumerate() if self.owner_pid(hwnd) == pid]

    def descendants(self, hwnd) -> list:
        return self._enumerate(hwnd)

    def is_window(self, hwnd) -> bool:
        return bool(self._api.user32.IsWindow(hwnd))

    def owner_pid(self, hwnd) -> int | None:
        pid = self._api.wintypes.DWORD(0)
        if not self._api.user32.GetWindowThreadProcessId(hwnd, self._api.ctypes.byref(pid)):
            return None
        return int(pid.value) or None

    def class_name(self, hwnd) -> str:
        buffer = self._api.ctypes.create_unicode_buffer(256)
        length = self._api.user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value[:length] if length > 0 else ""

    def text(self, hwnd) -> str:
        buffer = self._api.ctypes.create_unicode_buffer(256)
        length = self._api.user32.InternalGetWindowText(hwnd, buffer, len(buffer))
        return buffer.value[:length] if length > 0 else ""

    def style(self, hwnd) -> int:
        return int(self._api.get_window_long(hwnd, _GWL_STYLE)) & 0xFFFFFFFF

    def root(self, hwnd):
        root = self._api.user32.GetAncestor(hwnd, _GA_ROOT)
        return int(root) if root else None

    def is_hung(self, hwnd) -> bool:
        return bool(self._api.user32.IsHungAppWindow(hwnd))

    def send(self, hwnd, message, timeout_ms) -> int:
        if message not in READ_ONLY_MESSAGES:
            raise ValueError(f"message {message:#x} is not a read-only query")
        api = self._api
        result = api.ctypes.c_size_t(0)
        api.set_last_error(0)
        answered = api.user32.SendMessageTimeoutW(
            hwnd, message, 0, 0, _SMTO_ABORTIFHUNG | _SMTO_ERRORONEXIT, timeout_ms,
            api.ctypes.byref(result))
        if answered:
            return _signed32(int(result.value))
        code = api.get_last_error()
        if not self.is_window(hwnd):
            raise _Unreadable("A SLACS control closed while it was being read.")
        if code in (0, _ERROR_TIMEOUT):
            raise _Busy(f"SLACS did not answer within {timeout_ms} ms.")
        raise _Unreadable(f"Reading a SLACS control failed (Windows error {code}).")


# -- background monitor --------------------------------------------------------


def _finite(value) -> bool:
    """A real finite number; bools and NaN are not."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _positive_finite(name: str, value) -> float:
    if not _finite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _normalise(raw, elapsed_ms: float) -> dict:
    """A reader's result, checked: anything incomplete or odd is a failed read."""
    if not isinstance(raw, dict):
        reading = _empty_reading()
        reading.update(latency_ms=elapsed_ms,
                       error="The SLACS controls reader returned no reading.")
        return reading
    latency = raw.get("latency_ms")
    if not _finite(latency) or latency < 0:
        latency = elapsed_ms
    error = raw.get("error")
    reading = _empty_reading()
    reading.update(latency_ms=float(latency), busy=raw.get("busy") is True,
                   error=None if error is None else str(error))
    if raw.get("ok") is not True:
        if reading["error"] is None:
            reading["error"] = "SLACS's controls could not be read."
        return reading
    roughness = raw.get("roughness_um")
    mirror_h, mirror_v = raw.get("mirror_h"), raw.get("mirror_v")
    if (not _finite(roughness) or roughness <= 0
            or not isinstance(mirror_h, bool) or not isinstance(mirror_v, bool)):
        reading["error"] = "The SLACS controls reading was incomplete."
        return reading
    reading.update(ok=True, roughness_um=float(roughness), mirror_h=mirror_h, mirror_v=mirror_v,
                   error=None)
    return reading


class SlacsControlsMonitor:
    """Read SLACS's controls every ``interval`` seconds in a daemon thread while Follow runs.

    - ``pid_provider() -> int | None`` gives the detected SLACS pid; None (or
      any failure) skips that round.
    - ``reader(pid) -> dict`` returns a reading shaped like
      ``read_slacs_controls``'s. The default reads the real window on Windows
      and keeps the control HWNDs between reads; elsewhere the monitor is
      unsupported and ``start()`` returns False.
    - ``clock`` times ``age_ms``, busy spells and the latency summary.

    ``latest()`` keeps the last good values while reads fail, so a busy SLACS
    leaves the last Roughness in place. Session-log records: ``slacs_controls``
    when the values or their readability change (a busy failure is reported by
    ``slacs_ui_busy`` instead, since SLACS did not answer at all),
    ``slacs_ui_busy`` / ``slacs_ui_responsive`` around each busy spell, and a
    ``slacs_ui_latency`` summary every ``summary_seconds`` and at stop.
    ``poll_once()`` performs one read for tests that drive the clock.
    """

    def __init__(self, pid_provider: Callable[[], int | None], *, reader=None,
                 interval: float = POLL_INTERVAL, clock: Callable[[], float] = time.monotonic,
                 platform: str | None = None, summary_seconds: float = LATENCY_SUMMARY_SECONDS):
        self._pid_provider = pid_provider
        self._interval = _positive_finite("interval", interval)
        self._summary_seconds = _positive_finite("summary_seconds", summary_seconds)
        self._clock = clock
        resolved_platform = sys.platform if platform is None else platform
        self._cache: dict = {}
        if reader is not None:
            self._reader = reader
        elif resolved_platform == "win32":
            self._reader = lambda pid: read_slacs_controls(pid, cache=self._cache)
        else:
            self._reader = None
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._stopped = False
        self._thread: threading.Thread | None = None
        # Last attempt, and the last good values.
        self._reads = 0
        self._ok = False
        self._busy = False
        self._error: str | None = None
        self._roughness_um: float | None = None
        self._mirror_h: bool | None = None
        self._mirror_v: bool | None = None
        self._good_at: float | None = None
        self._logged: tuple | None = None
        # The current busy spell.
        self._busy_since: float | None = None
        self._busy_max_ms = 0.0
        self._busy_failed = 0
        # The current latency summary window.
        self._summary_at = clock()
        self._window_reads = self._window_failed = self._window_busy = 0
        self._window_max_ms = 0.0
        self._window_samples: list[float] = []

    @property
    def supported(self) -> bool:
        return self._reader is not None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Start the daemon reader thread. Idempotent; False when unsupported or stopped."""
        with self._lock:
            if self._reader is None or self._stopped:
                return False
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="nd2wsi-slacs-controls",
                                                daemon=True)
                self._thread.start()
            return True

    def stop(self) -> None:
        """Stop reading. Idempotent, safe from any thread, never raises or hangs."""
        try:
            self._stop.set()
            with self._lock:
                if self._stopped:
                    return
                self._stopped = True
                thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                # Never wait out a slow read; its late result is dropped.
                thread.join(timeout=STOP_JOIN_SECONDS)
            events: list = []
            with self._lock:
                self._summarise_locked(self._clock(), events, final=True)
            self._emit(events)
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - one bad round must not end the monitor
                pass
            if self._stop.wait(self._interval):
                break

    # -- reading -----------------------------------------------------------

    def poll_once(self) -> dict:
        """Perform one read now and return ``latest()``."""
        with self._poll_lock:
            if self._stop.is_set() or self._reader is None:
                return self.latest()
            try:
                pid = self._pid_provider()
            except Exception:  # noqa: BLE001 - an unknown pid skips the round
                pid = None
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                events: list = []
                with self._lock:
                    if not self._stop.is_set():
                        self._summarise_locked(self._clock(), events)
                self._emit(events)
                return self.latest()
            if self._stop.is_set():
                return self.latest()
            started = time.perf_counter()
            try:
                raw = self._reader(pid)
            except Exception as exc:  # noqa: BLE001 - a reader failure is a failed read
                raw = {"ok": False,
                       "error": f"Could not read SLACS's controls: {type(exc).__name__}"}
            reading = _normalise(raw, (time.perf_counter() - started) * 1000)
            events = []
            with self._lock:
                if self._stop.is_set():
                    return self._latest_locked()  # stopped mid-read: drop the late result
                self._apply_locked(reading, self._clock(), events)
            self._emit(events)
            return self.latest()

    def _apply_locked(self, reading: dict, now: float, events: list) -> None:
        ok, latency = reading["ok"], reading["latency_ms"]
        busy = reading["busy"] or (ok and latency > BUSY_LATENCY_MS)
        self._reads += 1
        self._ok, self._busy, self._error = ok, busy, reading["error"]
        if ok:
            self._roughness_um = reading["roughness_um"]
            self._mirror_h, self._mirror_v = reading["mirror_h"], reading["mirror_v"]
            self._good_at = now

        if ok or not reading["busy"]:
            marker = (ok, reading["roughness_um"], reading["mirror_h"], reading["mirror_v"],
                      None if ok else reading["error"])
            if marker != self._logged:
                self._logged = marker
                events.append(("slacs_controls", {
                    "roughness_um": reading["roughness_um"], "mirror_h": reading["mirror_h"],
                    "mirror_v": reading["mirror_v"], "ok": ok, "error": reading["error"]}))

        if busy:
            if self._busy_since is None:
                self._busy_since, self._busy_max_ms, self._busy_failed = now, 0.0, 0
                events.append(("slacs_ui_busy", {"latency_ms": round(latency, 1),
                                                 "error": reading["error"]}))
            self._busy_max_ms = max(self._busy_max_ms, latency)
            if not ok:
                self._busy_failed += 1
        elif self._busy_since is not None:
            if ok:
                events.append(("slacs_ui_responsive", {
                    "busy_seconds": round(now - self._busy_since, 2),
                    "max_latency_ms": round(self._busy_max_ms, 1),
                    "failed_reads": self._busy_failed}))
                self._busy_since = None
            else:
                # SLACS answered but the controls were unreadable: not yet shown responsive.
                self._busy_max_ms = max(self._busy_max_ms, latency)
                self._busy_failed += 1

        self._window_reads += 1
        self._window_failed += 0 if ok else 1
        self._window_busy += 1 if busy else 0
        self._window_max_ms = max(self._window_max_ms, latency)
        if len(self._window_samples) < _MAX_LATENCY_SAMPLES:
            self._window_samples.append(latency)
        self._summarise_locked(now, events)

    def _summarise_locked(self, now: float, events: list, *, final: bool = False) -> None:
        if not final and now - self._summary_at < self._summary_seconds:
            return
        if self._window_reads:
            events.append(("slacs_ui_latency", {
                "reads": self._window_reads, "failed": self._window_failed,
                "busy": self._window_busy,
                "p50_ms": round(statistics.median(self._window_samples), 1),
                "max_ms": round(self._window_max_ms, 1),
                "seconds": round(now - self._summary_at, 1)}))
        self._window_reads = self._window_failed = self._window_busy = 0
        self._window_max_ms = 0.0
        self._window_samples = []
        self._summary_at = now

    @staticmethod
    def _emit(events: list) -> None:
        for kind, fields in events:
            session_log.event(kind, **fields)

    # -- introspection -----------------------------------------------------

    def _latest_locked(self) -> dict:
        age_ms = None
        if self._good_at is not None:
            age_ms = round(max(0.0, (self._clock() - self._good_at) * 1000), 1)
        return {"ok": self._ok, "roughness_um": self._roughness_um, "mirror_h": self._mirror_h,
                "mirror_v": self._mirror_v, "age_ms": age_ms, "busy": self._busy,
                "error": self._error, "reads": self._reads}

    def latest(self) -> dict:
        """The last attempt's ok/busy/error and the last good values, with their age."""
        with self._lock:
            return self._latest_locked()


__all__ = ["READ_TIMEOUT_MS", "POLL_INTERVAL", "BUSY_LATENCY_MS", "LATENCY_SUMMARY_SECONDS",
           "ROUGHNESS_RANGE", "MIRROR_LABELS", "READ_ONLY_MESSAGES", "read_slacs_controls",
           "SlacsControlsMonitor"]
