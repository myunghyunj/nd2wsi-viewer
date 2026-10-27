"""Passively observe the W/A/S/D keys SLACS sends to the ACS stage.

The user jogs the sample stage from the legacy SLACS program with W/A/S/D. This
module watches those keystrokes and moves the translucent overlay to match, using
the same commanded-displacement path as the HTTP receiver. It is a strict
observer: on Windows it uses Raw Input (``RIDEV_INPUTSINK``), which never consumes,
delays, alters, or blocks a keystroke, and it never calls into SLACS or the ACS
controller. SLACS's own keystrokes and its commands to the stage are untouched.

Privacy: Raw Input delivers every keystroke to this process. Anything that is not
one of the four jog keys is discarded the instant it arrives; it is never stored,
counted individually, or written to the session log. W/A/S/D pressed while SLACS
is not the foreground program are only tallied as ``ignored_not_slacs``, never
recorded individually.

The native Raw Input plumbing is Windows-only, but this module imports cleanly on
every platform: all ``ctypes.windll`` use is deferred into functions that run only
on Windows or when a test injects its own observer, foreground provider, and
identifier.
"""

from __future__ import annotations

import math
import sys
import threading
import time
from collections.abc import Callable

from . import session_log
from .slacs_identity import SCRIPT_NAME, command_line_is_slacs

# SLACS's exact jog bindings are still being confirmed by another team, so the
# whole policy lives in one place that is easy to change: which virtual key is
# which jog letter, and the unit stage step each produces (scaled by step_um).
# W/S move in +Y/-Y, A/D in -X/+X, matching the receiver's commanded axes.
KEY_BINDINGS: dict[int, tuple[str, tuple[float, float]]] = {
    0x57: ("W", (0.0, 1.0)),
    0x53: ("S", (0.0, -1.0)),
    0x41: ("A", (-1.0, 0.0)),
    0x44: ("D", (1.0, 0.0)),
}
# A jog key held together with any of these is a shortcut, not a stage jog.
BLOCKING_MODIFIERS = frozenset(("ctrl", "alt", "win"))
MAX_STEP_UM = 10000.0

# Receiver rejections that mean following can no longer continue (paused/closed).
_TERMINAL_STATUSES = frozenset((409, 410))


def _validate_step_um(step_um) -> float:
    if isinstance(step_um, bool) or not isinstance(step_um, (int, float)):
        raise ValueError("step_um must be a number")
    value = float(step_um)
    if not math.isfinite(value) or not 0 < value <= MAX_STEP_UM:
        raise ValueError(f"step_um must be a finite number in (0, {MAX_STEP_UM:g}]")
    return value


class SlacsKeyFollower:
    """Count SLACS W/A/S/D key-down events and drive the overlay receiver.

    Dependencies are injected so the logic is fully testable off Windows:

    - ``observer``: an object with ``start(on_event) -> bool`` and ``stop()``.
      ``on_event(vk, down, modifiers)`` is called for every keystroke the observer
      sees. When omitted, a real Raw Input observer is created on Windows and the
      follower reports ``supported=False`` elsewhere.
    - ``foreground_provider() -> dict | None`` returns ``{"pid", "created"}`` for
      the process owning the foreground window, or None on any error.
    - ``identifier(pid, created) -> bool`` says whether that process is SLACS.
    - ``clock`` and ``heartbeat_interval`` drive the idle heartbeat; pass
      ``heartbeat_interval=None`` to drive ``_heartbeat_tick`` manually in tests.
    """

    def __init__(self, receiver, session_id: str, *, step_um,
                 observer=None, foreground_provider: Callable[[], dict | None] | None = None,
                 identifier: Callable[[int, object], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 heartbeat_interval: float | None = 1.0, platform: str | None = None):
        self._receiver = receiver
        self._session_id = session_id
        self._step_um = _validate_step_um(step_um)
        self._clock = clock
        self._heartbeat_interval = heartbeat_interval
        self._lock = threading.Lock()
        self._started = False
        self._running = False
        self._stopped = False
        self._counted = 0
        self._ignored_not_slacs = 0
        self._last_key: str | None = None
        self._slacs_in_front = False
        self._error: str | None = None
        self._seq = 0
        self._identity_cache: dict[tuple, bool] = {}
        self._last_foreground: tuple | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()

        resolved_platform = sys.platform if platform is None else platform
        if observer is not None:
            self._observer = observer
            self._supported = True
        elif resolved_platform == "win32":
            self._observer = _RawInputKeyObserver()
            self._supported = True
        else:
            self._observer = None
            self._supported = False
        self._foreground_provider = foreground_provider
        self._identifier = identifier
        if self._supported and observer is None and resolved_platform == "win32":
            # Default to the real, read-only Windows providers.
            if self._foreground_provider is None:
                self._foreground_provider = _windows_foreground_process
            if self._identifier is None:
                self._identifier = _windows_process_is_slacs

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> dict:
        with self._lock:
            if self._started or self._stopped:
                return self._snapshot_locked()
            if not self._supported:
                self._error = "Key following is only available on Windows."
                self._started = True
                session_log.event("follow_keys_start", step_um=self._step_um,
                                  supported=False)
                return self._snapshot_locked()
            self._started = True
            self._running = True
        started = False
        try:
            started = bool(self._observer.start(self._on_event))
        except Exception as exc:  # noqa: BLE001 - never let a driver failure escape
            self._fail(f"Could not start the keystroke observer: {exc}")
        if not started:
            self._fail(self._error or "Could not start the keystroke observer.",
                       stop_counting=True)
            try:
                self._observer.stop()
            except Exception:  # noqa: BLE001
                pass
            session_log.event("follow_keys_start", step_um=self._step_um, ok=False,
                              error=self._error)
            return self.snapshot()
        if self._heartbeat_interval:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name="nd2wsi-stage-keys-heartbeat", daemon=True)
            self._heartbeat_thread.start()
        session_log.event("follow_keys_start", step_um=self._step_um, ok=True)
        return self.snapshot()

    def stop(self, reason: str = "stopped") -> None:
        """Stop observing. Idempotent, safe from any thread, and never hangs."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            was_active = self._running or self._started
            self._running = False
            heartbeat = self._heartbeat_thread
            counted, ignored = self._counted, self._ignored_not_slacs
        self._heartbeat_stop.set()
        observer = self._observer
        if observer is not None:
            try:
                observer.stop()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=2.0)
        if was_active and self._supported:
            session_log.event("follow_keys_stop", step_um=self._step_um, reason=reason,
                              counted=counted, ignored_not_slacs=ignored)

    # -- key handling ------------------------------------------------------

    def _on_event(self, vk, down, modifiers=()) -> None:
        """Handle one raw keystroke. Non-jog keys are dropped immediately."""
        binding = KEY_BINDINGS.get(vk)
        if binding is None:
            return  # privacy: every non-WASD key is discarded the instant it arrives
        if not down:
            return  # observe presses only; key-up is ignored
        if self._has_blocking_modifier(modifiers):
            return  # a jog key with Ctrl/Alt/Win is a shortcut, not a stage jog
        letter, (unit_x, unit_y) = binding
        with self._lock:
            if not self._running:
                return
            step = self._step_um
        if not self._check_foreground():
            with self._lock:
                self._ignored_not_slacs += 1
            return  # counted only in aggregate; the individual key is never recorded
        delta = [unit_x * step, unit_y * step]
        with self._lock:
            if not self._running:
                return
            seq = self._seq + 1
        result = self._receiver.submit({
            "version": 1, "session_id": self._session_id, "seq": seq,
            "type": "delta", "delta_um": delta})
        if result.get("ok"):
            with self._lock:
                self._seq = seq
                self._counted += 1
                self._last_key = letter
            session_log.event("follow_key", key=letter, seq=seq, delta_um=delta)
        else:
            self._handle_submit_failure(result)

    @staticmethod
    def _has_blocking_modifier(modifiers) -> bool:
        if not modifiers:
            return False
        if isinstance(modifiers, dict):
            held = {name for name, value in modifiers.items() if value}
        else:
            held = set(modifiers)
        return bool(BLOCKING_MODIFIERS & {str(name).lower() for name in held})

    # -- foreground identification ----------------------------------------

    def _check_foreground(self) -> bool:
        """Return whether SLACS owns the foreground window; fail closed."""
        provider = self._foreground_provider
        foreground = None
        if provider is not None:
            try:
                foreground = provider()
            except Exception:  # noqa: BLE001 - any failure means "not SLACS"
                foreground = None
        pid = None
        identified = False
        if isinstance(foreground, dict):
            pid = foreground.get("pid")
            identified = self._identify(pid, foreground.get("created"))
        with self._lock:
            self._slacs_in_front = identified
            marker = (pid, identified)
            changed = marker != self._last_foreground
            self._last_foreground = marker
        if changed:
            session_log.event("slacs_foreground", pid=pid, identified=identified)
        return identified

    def _identify(self, pid, created) -> bool:
        if pid is None or self._identifier is None:
            return False
        cache_key = (pid, created)
        with self._lock:
            if cache_key in self._identity_cache:
                return self._identity_cache[cache_key]
        try:
            result = bool(self._identifier(pid, created))
        except Exception:  # noqa: BLE001 - fail closed on any identification error
            result = False
        with self._lock:
            if len(self._identity_cache) > 256:
                self._identity_cache.clear()
            self._identity_cache[cache_key] = result
        return result

    # -- heartbeat ---------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        interval = self._heartbeat_interval or 1.0
        while not self._heartbeat_stop.wait(interval):
            self._heartbeat_tick()

    def _heartbeat_tick(self) -> None:
        """Keep the receiver fresh and the foreground status current while idle."""
        with self._lock:
            if not self._running:
                return
        self._check_foreground()
        with self._lock:
            if not self._running:
                return
        result = self._receiver.submit({"version": 1, "session_id": self._session_id})
        if not result.get("ok"):
            self._handle_submit_failure(result)

    # -- failure handling --------------------------------------------------

    def _handle_submit_failure(self, result: dict) -> None:
        status = result.get("status")
        message = result.get("message") or "Stage receiver rejected an observation."
        if status in _TERMINAL_STATUSES:
            self._fail(message, stop_counting=True)

    def _fail(self, message: str, *, stop_counting: bool = False) -> None:
        with self._lock:
            if stop_counting:
                self._running = False
            already = self._error == message
            self._error = message
        if not already:
            session_log.event("follow_keys_error", message=message)

    # -- introspection -----------------------------------------------------

    def _snapshot_locked(self) -> dict:
        return {
            "source": "keys",
            "running": self._running,
            "supported": self._supported,
            "step_um": self._step_um,
            "counted": self._counted,
            "ignored_not_slacs": self._ignored_not_slacs,
            "last_key": self._last_key,
            "slacs_in_front": self._slacs_in_front,
            "error": self._error,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()


# -- Windows Raw Input observer ------------------------------------------------

_RIDEV_INPUTSINK = 0x00000100
_RIDEV_REMOVE = 0x00000001
_RID_INPUT = 0x10000003
_RIM_TYPEKEYBOARD = 1
_RI_KEY_BREAK = 0x01
_WM_INPUT = 0x00FF
_WM_QUIT = 0x0012
_WM_DESTROY = 0x0002
_HWND_MESSAGE = -3


class _RawInputKeyObserver:
    """Raw Input keyboard sink on a hidden message-only window (Windows only).

    Runs its own message loop in a daemon thread. Raw Input is a sink
    (``RIDEV_INPUTSINK``): it observes keystrokes without consuming or delaying
    them. ``stop`` is idempotent, safe from any thread, unregisters the device,
    destroys the window, ends the loop, and joins with a timeout.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._hwnd = None
        self._on_event: Callable[[int, bool, tuple], None] | None = None
        self._ready = threading.Event()
        self._started_ok = False
        self._wndproc = None  # keep the WNDPROC callback alive
        self._stopped = False
        self._lock = threading.Lock()

    def start(self, on_event) -> bool:
        self._on_event = on_event
        self._thread = threading.Thread(target=self._run, name="nd2wsi-stage-keys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self._started_ok

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            thread, thread_id = self._thread, self._thread_id
        if thread_id is not None:
            try:
                import ctypes

                # A WM_QUIT thread message ends GetMessageW in the loop thread; the
                # loop's finally clause unregisters and destroys the window.
                ctypes.windll.user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001 - fall through to join
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                        ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

        class RAWINPUTHEADER(ctypes.Structure):
            _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                        ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]

        class RAWKEYBOARD(ctypes.Structure):
            _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT),
                        ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT),
                        ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]

        class RAWINPUT_KB(ctypes.Structure):
            _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        def wnd_proc(hwnd, message, wparam, lparam):
            if message == _WM_INPUT:
                try:
                    self._dispatch_raw_input(lparam, user32, ctypes, wintypes,
                                             RAWINPUT_KB, _RID_INPUT)
                except Exception:  # noqa: BLE001 - never break the loop on one event
                    pass
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)
            if message == _WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = WNDPROC(wnd_proc)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        hwnd = None
        registered_class = None
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            hinstance = kernel32.GetModuleHandleW(None)
            class_name = f"Nd2wsiStageKeys{self._thread_id}"
            wnd_class = WNDCLASS()
            wnd_class.lpfnWndProc = self._wndproc
            wnd_class.hInstance = hinstance
            wnd_class.lpszClassName = class_name
            user32.RegisterClassW.restype = wintypes.ATOM
            atom = user32.RegisterClassW(ctypes.byref(wnd_class))
            if not atom:
                raise OSError("RegisterClassW failed")
            registered_class = class_name
            user32.CreateWindowExW.restype = wintypes.HWND
            hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0,
                                          _HWND_MESSAGE, None, hinstance, None)
            if not hwnd:
                raise OSError("CreateWindowExW failed")
            self._hwnd = hwnd
            device = RAWINPUTDEVICE(0x01, 0x06, _RIDEV_INPUTSINK, hwnd)
            if not user32.RegisterRawInputDevices(ctypes.byref(device), 1,
                                                  ctypes.sizeof(RAWINPUTDEVICE)):
                raise OSError("RegisterRawInputDevices failed")
            self._started_ok = True
        except Exception:  # noqa: BLE001 - report failure through the ready event
            self._started_ok = False
            if hwnd:
                user32.DestroyWindow(hwnd)
                hwnd = None
            if registered_class is not None:
                user32.UnregisterClassW(registered_class, kernel32.GetModuleHandleW(None))
                registered_class = None
        finally:
            self._ready.set()
        if not self._started_ok:
            return

        try:
            msg = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            try:
                remove = RAWINPUTDEVICE(0x01, 0x06, _RIDEV_REMOVE, None)
                user32.RegisterRawInputDevices(ctypes.byref(remove), 1,
                                               ctypes.sizeof(RAWINPUTDEVICE))
            except Exception:  # noqa: BLE001
                pass
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            if registered_class is not None:
                user32.UnregisterClassW(registered_class, kernel32.GetModuleHandleW(None))

    def _dispatch_raw_input(self, lparam, user32, ctypes, wintypes, RAWINPUT_KB, rid_input):
        size = wintypes.UINT(0)
        header_size = RAWINPUT_KB.header.size  # sizeof(RAWINPUTHEADER)
        user32.GetRawInputData(wintypes.HANDLE(lparam), rid_input, None,
                               ctypes.byref(size), header_size)
        if size.value == 0 or size.value > 1024:
            return
        buffer = ctypes.create_string_buffer(size.value)
        read = user32.GetRawInputData(wintypes.HANDLE(lparam), rid_input, buffer,
                                      ctypes.byref(size), header_size)
        if read <= 0:
            return
        raw = ctypes.cast(buffer, ctypes.POINTER(RAWINPUT_KB)).contents
        if raw.header.dwType != _RIM_TYPEKEYBOARD:
            return
        vk = int(raw.keyboard.VKey)
        down = not (raw.keyboard.Flags & _RI_KEY_BREAK)
        on_event = self._on_event
        if on_event is not None:
            on_event(vk, down, _held_modifiers(user32))


def _held_modifiers(user32) -> tuple:
    """Modifier keys currently held, sampled at the keystroke."""
    held = []
    for name, vk in (("ctrl", 0x11), ("alt", 0x12), ("win", 0x5B), ("win", 0x5C)):
        if user32.GetKeyState(vk) & 0x8000:
            held.append(name)
    return tuple(held)


# -- Windows foreground + identity providers (read-only) -----------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _windows_foreground_process() -> dict | None:
    """Return the foreground window's owning process, or None. Read-only."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                        ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)
    return {"pid": int(pid.value), "created": int(created)}


def _windows_process_image_name(handle) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value


_PROCESS_COMMAND_LINE_INFORMATION = 60


def _windows_process_command_line(handle) -> str | None:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.windll.ntdll

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    length = wintypes.ULONG(0)
    status = ntdll.NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE_INFORMATION,
                                             None, 0, ctypes.byref(length))
    # STATUS_INFO_LENGTH_MISMATCH (0xC0000004) is expected on the sizing call.
    if length.value == 0 or length.value > 1 << 20:
        return None
    buffer = ctypes.create_string_buffer(length.value)
    status = ntdll.NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE_INFORMATION,
                                             buffer, length.value, ctypes.byref(length))
    if status != 0:
        return None
    unicode_string = ctypes.cast(buffer, ctypes.POINTER(UNICODE_STRING)).contents
    if not unicode_string.Buffer or unicode_string.Length == 0:
        return None
    return ctypes.wstring_at(unicode_string.Buffer, unicode_string.Length // 2)


def _windows_process_is_slacs(pid, created=None) -> bool:
    """True when ``pid`` runs the SLACS GUI script. Read-only; fails closed."""
    import ctypes
    import ntpath

    kernel32 = ctypes.windll.kernel32
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        image = _windows_process_image_name(handle)
        if not image or ntpath.basename(image).lower() not in ("python.exe", "pythonw.exe"):
            return False
        command_line = _windows_process_command_line(handle)
    finally:
        kernel32.CloseHandle(handle)
    if not command_line:
        return False
    return command_line_is_slacs(command_line)


__all__ = ["SlacsKeyFollower", "KEY_BINDINGS", "BLOCKING_MODIFIERS", "MAX_STEP_UM",
           "SCRIPT_NAME"]
