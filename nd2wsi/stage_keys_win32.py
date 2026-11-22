"""Low-level Win32 ctypes, Raw Input observer, and foreground inspection helpers.

Extracted from :mod:`nd2wsi.stage_keys` so the Windows Raw Input sink
(``RIDEV_INPUTSINK``, hidden ``HWND_MESSAGE`` message pump, ``RAWINPUT`` struct
parsing, raw modifier tracking, shared hub) and read-only foreground/process
inspection helpers stay decoupled from the platform-agnostic stage-key follower
state machine.

This module imports cleanly on every platform: all ``ctypes`` / Win32 DLL
loading is deferred into :func:`_win32`, which runs only on Windows. Public and
test-referenced private symbols remain re-exported on :mod:`nd2wsi.stage_keys`,
and cross-calls honor monkeypatches installed on either module.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

from .slacs_identity import command_line_is_slacs


def _stage_keys_module() -> Any:
    """Resolve the sibling ``nd2wsi.stage_keys`` facade lazily.

    Uses ``sys.modules`` on the hot path so raw keystroke dispatch never takes
    the import lock, while keeping module imports acyclic.
    """
    mod = sys.modules.get("nd2wsi.stage_keys")
    if mod is not None:
        return mod
    from . import stage_keys

    return stage_keys


def _resolve_stage_keys_attr(name: str, local_value: Any, original_value: Any) -> Any:
    """Honor monkeypatches applied either directly here or on ``nd2wsi.stage_keys``."""
    if local_value is not original_value:
        return local_value
    mod = sys.modules.get("nd2wsi.stage_keys")
    if mod is not None:
        return getattr(mod, name, local_value)
    return local_value


# -- Win32 API with private prototypes -----------------------------------------

_RIDEV_INPUTSINK = 0x00000100
_RIDEV_REMOVE = 0x00000001
_RID_INPUT = 0x10000003
_RIM_TYPEKEYBOARD = 1
_RI_KEY_BREAK = 0x01
_RI_KEY_E0 = 0x02
_WM_INPUT = 0x00FF
_WM_QUIT = 0x0012
_WM_DESTROY = 0x0002
_HWND_MESSAGE = -3
_GET_RAW_INPUT_ERROR = 0xFFFFFFFF  # (UINT)-1


@lru_cache(maxsize=1)
def _win32():
    """user32/kernel32/ntdll with every call this module makes prototyped.

    Private ``WinDLL`` instances, never the shared ``ctypes.windll``: declaring
    prototypes here must not change calls other code makes. Every handle,
    pointer, WPARAM/LPARAM and LRESULT is pointer-sized, so nothing is truncated
    to a 32-bit C ``long`` on 64-bit Windows. Windows only.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    LRESULT = ctypes.c_ssize_t  # LONG_PTR
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM)

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

    class RAWINPUT_KEYBOARD(ctypes.Structure):
        _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    def declare(function, restype, *argtypes):
        function.restype = restype
        function.argtypes = list(argtypes)

    HWND, UINT, DWORD, HANDLE = wintypes.HWND, wintypes.UINT, wintypes.DWORD, wintypes.HANDLE
    PFILETIME = ctypes.POINTER(wintypes.FILETIME)
    declare(user32.DefWindowProcW, LRESULT, HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)
    declare(user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
    declare(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
    declare(user32.CreateWindowExW, HWND, DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, wintypes.HMENU,
            wintypes.HINSTANCE, wintypes.LPVOID)
    declare(user32.DestroyWindow, wintypes.BOOL, HWND)
    declare(user32.RegisterRawInputDevices, wintypes.BOOL, ctypes.POINTER(RAWINPUTDEVICE), UINT, UINT)
    declare(user32.GetRawInputData, UINT, HANDLE, UINT, wintypes.LPVOID, ctypes.POINTER(UINT), UINT)
    declare(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), HWND, UINT, UINT)
    declare(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
    declare(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
    declare(user32.PostQuitMessage, None, ctypes.c_int)
    declare(user32.PostThreadMessageW, wintypes.BOOL, DWORD, UINT, wintypes.WPARAM, wintypes.LPARAM)
    declare(user32.GetAsyncKeyState, wintypes.SHORT, ctypes.c_int)
    declare(user32.GetForegroundWindow, HWND)
    declare(user32.GetWindowThreadProcessId, DWORD, HWND, wintypes.LPDWORD)
    declare(kernel32.GetCurrentThreadId, DWORD)
    declare(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
    declare(kernel32.OpenProcess, HANDLE, DWORD, wintypes.BOOL, DWORD)
    declare(kernel32.CloseHandle, wintypes.BOOL, HANDLE)
    declare(kernel32.GetProcessTimes, wintypes.BOOL, HANDLE, PFILETIME, PFILETIME, PFILETIME, PFILETIME)
    declare(kernel32.QueryFullProcessImageNameW, wintypes.BOOL, HANDLE, DWORD, wintypes.LPWSTR,
            ctypes.POINTER(DWORD))
    declare(ntdll.NtQueryInformationProcess, wintypes.LONG, HANDLE, wintypes.ULONG, wintypes.LPVOID,
            wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))
    return SimpleNamespace(
        ctypes=ctypes, wintypes=wintypes, user32=user32, kernel32=kernel32, ntdll=ntdll,
        WNDPROC=WNDPROC, WNDCLASSW=WNDCLASSW, RAWINPUTDEVICE=RAWINPUTDEVICE,
        RAWINPUTHEADER=RAWINPUTHEADER, RAWINPUT_KEYBOARD=RAWINPUT_KEYBOARD,
        UNICODE_STRING=UNICODE_STRING)


def _win_error(api, call: str) -> OSError:
    code = api.ctypes.get_last_error()
    return OSError(f"{call} failed (Windows error {code})")


# -- Ctrl/Alt/Win state from the raw stream ------------------------------------

# Ctrl/Alt/Win as Raw Input reports them. The generic VK_CONTROL/VK_MENU carry
# RI_KEY_E0 for the right-hand key; the sided codes are accepted too, so a make
# and its break always land on the same side. GetAsyncKeyState takes the sided code.
_GENERIC_MODIFIERS = {0x11: "ctrl", 0x12: "alt"}
_SIDED_MODIFIERS = {0xA2: ("ctrl", "left"), 0xA3: ("ctrl", "right"),
                    0xA4: ("alt", "left"), 0xA5: ("alt", "right"),
                    0x5B: ("win", "left"), 0x5C: ("win", "right")}
_SIDE_VKS = {side: vk for vk, side in _SIDED_MODIFIERS.items()}


def _modifier_side(vk, e0) -> tuple[str, str] | None:
    name = _GENERIC_MODIFIERS.get(vk)
    if name is not None:
        return name, "right" if e0 else "left"
    return _SIDED_MODIFIERS.get(vk)


class _ModifierTracker:
    """Which of Ctrl/Alt/Win are held, followed from the keystroke stream itself.

    ``GetKeyState`` on the observer thread only reflects key messages that thread
    has read, and it reads none (it only gets ``WM_INPUT``), so held state comes
    from Raw Input's own make/break events, in order. Only one timestamp per
    held key side is kept. A break lost while another desktop had the input
    (Ctrl+Alt+Del, the lock screen) would leave a modifier stuck and block every
    jog, so a hold older than ``stale_after`` seconds is re-checked against the
    physical key state; ``physically_down(side)`` must answer conservatively.
    """

    def __init__(self, *, physically_down: Callable[[tuple[str, str]], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic, stale_after: float = 2.0):
        self._physically_down = physically_down
        self._clock = clock
        self._stale_after = stale_after
        self._held: dict[tuple[str, str], float] = {}  # key side -> time of its last make

    def update(self, vk, down, e0=False) -> bool:
        """Record one keystroke; True when it was a Ctrl/Alt/Win key."""
        side = _modifier_side(vk, e0)
        if side is None:
            return False
        if down:
            self._held[side] = self._clock()  # autorepeat refreshes the hold
        else:
            self._held.pop(side, None)
        return True

    def held(self) -> tuple[str, ...]:
        now = self._clock()
        names = set()
        for side, since in list(self._held.items()):
            if (now - since > self._stale_after and self._physically_down is not None
                    and not self._physically_down(side)):
                del self._held[side]  # its break never reached us; the key is up now
                continue
            names.add(side[0])
        return tuple(sorted(names))


def _windows_side_down(side) -> bool:
    """Physical state of one modifier key side; unknown counts as held."""
    try:
        win32 = _resolve_stage_keys_attr("_win32", _win32, _ORIG_WIN32)
        return bool(win32().user32.GetAsyncKeyState(_SIDE_VKS[side]) & 0x8000)
    except Exception:  # noqa: BLE001 - fail closed: a held modifier blocks the jog
        return True


# -- Windows Raw Input observer ------------------------------------------------


class _RawInputKeyObserver:
    """Raw Input keyboard sink on a hidden message-only window (Windows only).

    Runs its own message loop in a daemon thread. Raw Input is a sink
    (``RIDEV_INPUTSINK``): it observes keystrokes without consuming or delaying
    them. Only jog keys and SLACS's ``,``/``.`` are forwarded; Ctrl/Alt/Win only
    update held state and every other key is dropped here. ``stop`` is
    idempotent, safe from any thread, unregisters the device, destroys the
    window, ends the loop, and joins with a timeout. Raw Input allows one
    keyboard target per process, so use it only through ``_RawInputHub``.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._on_event: Callable[[int, bool, tuple], None] | None = None
        self._ready = threading.Event()
        self._started_ok = False
        self._wndproc = None  # keep the WNDPROC callback alive
        self._stopped = False
        self._lock = threading.Lock()
        tracker_cls = _resolve_stage_keys_attr(
            "_ModifierTracker", _ModifierTracker, _ORIG_MODIFIER_TRACKER
        )
        side_down = _resolve_stage_keys_attr(
            "_windows_side_down", _windows_side_down, _ORIG_WINDOWS_SIDE_DOWN
        )
        self._modifiers = tracker_cls(physically_down=side_down)
        self.error: str | None = None

    def start(self, on_event) -> bool:
        with self._lock:
            if self._thread is not None or self._stopped:
                return self._started_ok
            self._on_event = on_event
            self._thread = threading.Thread(target=self._run, name="nd2wsi-stage-keys", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            self.error = "The keystroke observer did not start in time."
            self.stop()
            return False
        return self._started_ok

    def alive(self) -> bool:
        thread = self._thread
        return (self._started_ok and not self._stopped and thread is not None
                and thread.is_alive())

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            thread, thread_id = self._thread, self._thread_id
        if thread_id is not None:
            try:
                # WM_QUIT ends GetMessageW in the loop thread; its finally clause
                # unregisters and destroys the window on that thread.
                win32 = _resolve_stage_keys_attr("_win32", _win32, _ORIG_WIN32)
                win32().user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001 - fall through to join
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _run(self) -> None:
        try:
            api = _resolve_stage_keys_attr("_win32", _win32, _ORIG_WIN32)()
        except Exception as exc:  # noqa: BLE001 - report failure through the ready event
            self.error = f"Win32 is unavailable: {exc}"
            self._ready.set()
            return
        ctypes, user32, kernel32 = api.ctypes, api.user32, api.kernel32
        make_device = _stage_keys_module()._keyboard_raw_input_device

        def wnd_proc(hwnd, message, wparam, lparam):
            if message == _WM_INPUT:
                try:
                    self._dispatch_raw_input(api, lparam)
                except Exception:  # noqa: BLE001 - never break the loop on one event
                    pass
            elif message == _WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            # WM_INPUT needs DefWindowProcW too, so the system can clean up.
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = api.WNDPROC(wnd_proc)
        hwnd = None
        hinstance = None
        class_name = None
        registered_class = False
        registered_device = False
        try:
            thread_id = kernel32.GetCurrentThreadId()
            hinstance = kernel32.GetModuleHandleW(None)
            class_name = f"Nd2wsiStageKeys{thread_id}"
            wnd_class = api.WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinstance,
                                      lpszClassName=class_name)
            if not user32.RegisterClassW(ctypes.byref(wnd_class)):
                raise _win_error(api, "RegisterClassW")
            registered_class = True
            hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0,
                                          _HWND_MESSAGE, None, hinstance, None)
            if not hwnd:
                raise _win_error(api, "CreateWindowExW")
            with self._lock:
                if self._stopped:
                    raise RuntimeError("stopped while starting")
                # The window gave this thread a message queue, so WM_QUIT posted
                # by stop() from now on is delivered.
                self._thread_id = thread_id
            device = make_device(api, _RIDEV_INPUTSINK, hwnd)
            if not user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device)):
                raise _win_error(api, "RegisterRawInputDevices")
            registered_device = True
            self._started_ok = True
        except Exception as exc:  # noqa: BLE001 - report failure through the ready event
            self._started_ok = False
            self.error = str(exc)
        finally:
            self._ready.set()

        try:
            if self._started_ok:
                msg = api.wintypes.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # 0 = WM_QUIT, -1 = error
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if registered_device:
                remove = make_device(api, _RIDEV_REMOVE, None)
                user32.RegisterRawInputDevices(ctypes.byref(remove), 1, ctypes.sizeof(remove))
            if hwnd:
                user32.DestroyWindow(hwnd)
            if registered_class:
                user32.UnregisterClassW(class_name, hinstance)

    def _dispatch_raw_input(self, api, lparam) -> None:
        ctypes = api.ctypes
        header_size = ctypes.sizeof(api.RAWINPUTHEADER)
        size = api.wintypes.UINT(0)
        if api.user32.GetRawInputData(lparam, _RID_INPUT, None, ctypes.byref(size), header_size) != 0:
            return
        if not header_size <= size.value <= 1024:
            return
        buffer = ctypes.create_string_buffer(size.value)
        read = api.user32.GetRawInputData(lparam, _RID_INPUT, buffer, ctypes.byref(size), header_size)
        if read == _GET_RAW_INPUT_ERROR or read < ctypes.sizeof(api.RAWINPUT_KEYBOARD):
            return
        raw = ctypes.cast(buffer, ctypes.POINTER(api.RAWINPUT_KEYBOARD)).contents
        if raw.header.dwType != _RIM_TYPEKEYBOARD:
            return
        self._handle_key(int(raw.keyboard.VKey), int(raw.keyboard.Flags))

    def _handle_key(self, vk: int, flags: int) -> None:
        """Route one raw keystroke, in arrival order (the loop thread only)."""
        down = not (flags & _RI_KEY_BREAK)
        if self._modifiers.update(vk, down, bool(flags & _RI_KEY_E0)):
            return  # Ctrl/Alt/Win only change held state
        stage_keys = _stage_keys_module()
        if vk not in stage_keys.KEY_BINDINGS and vk not in stage_keys.MARK_KEYS:
            return  # privacy: every key but W/A/S/D, "," and "." is dropped before any callback sees it
        on_event = self._on_event
        if on_event is not None:
            on_event(vk, down, self._modifiers.held())


class _RawInputHub:
    """The process's one Raw Input keyboard sink, shared by every key follower.

    ``RegisterRawInputDevices`` keeps one target window per device class per
    process (the last registration wins), and ``RIDEV_REMOVE`` drops the
    process's registration, so overlays following at once must not each
    register. The first subscriber starts the observer, events fan out to every
    subscriber, and the last one to leave stops it. It also numbers the SLACS
    ``,``/``.`` presses that followers log, so the records several overlays write
    for one press share a number.
    """

    def __init__(self, observer_factory: Callable[[], object] | None = None):
        self._factory = observer_factory
        # Held across native start/stop too: a new observer must never register
        # while an old one can still send RIDEV_REMOVE.
        self._lifecycle = threading.Lock()
        self._observer = None
        self._subscribers: tuple = ()  # (token, callback); replaced, never mutated
        self.error: str | None = None
        self._press_lock = threading.Lock()  # a leaf lock: nothing else is taken under it
        self._presses = 0
        self._event_press: int | None = None  # the number given to the event being dispatched

    def subscribe(self, token, callback) -> bool:
        with self._lifecycle:
            if self._observer is not None and not self._observer_alive():
                self._stop_observer_locked()  # its loop ended; start a fresh one
            if self._observer is None:
                factory = self._factory or _resolve_stage_keys_attr(
                    "_RawInputKeyObserver", _RawInputKeyObserver, _ORIG_RAW_INPUT_KEY_OBSERVER
                )
                observer = factory()
                error = None
                try:
                    started = bool(observer.start(self._dispatch))
                except Exception as exc:  # noqa: BLE001 - report, never raise
                    started, error = False, str(exc)
                if not started:
                    self.error = error or getattr(observer, "error", None)
                    try:
                        observer.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    return False
                self._observer = observer
                self.error = None
            self._subscribers = (*(s for s in self._subscribers if s[0] is not token),
                                 (token, callback))
            return True

    def unsubscribe(self, token) -> None:
        with self._lifecycle:
            remaining = tuple(s for s in self._subscribers if s[0] is not token)
            if len(remaining) == len(self._subscribers):
                return
            self._subscribers = remaining
            if not remaining:
                self._stop_observer_locked()

    def _observer_alive(self) -> bool:
        alive = getattr(self._observer, "alive", None)
        return True if alive is None else bool(alive())

    def _stop_observer_locked(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            try:
                observer.stop()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass

    def press_number(self) -> int:
        """The number of the press being dispatched; the same for every follower that logs it.

        Called only from a subscriber's callback. A number is given only to a
        press some follower logs, so the numbers count logged presses and say
        nothing about any other keystroke.
        """
        with self._press_lock:
            if self._event_press is None:
                self._presses += 1
                self._event_press = self._presses
            return self._event_press

    def _dispatch(self, vk, down, modifiers) -> None:
        with self._press_lock:
            self._event_press = None  # a new keystroke has no number yet
        for _token, callback in self._subscribers:
            try:
                callback(vk, down, modifiers)
            except Exception:  # noqa: BLE001 - one follower must not starve another
                pass


_RAW_INPUT_HUB = _RawInputHub()


class _SharedKeyObserver:
    """One follower's subscription to the process-wide Raw Input hub."""

    def __init__(self, hub: _RawInputHub | None = None):
        self._hub = (
            _resolve_stage_keys_attr("_RAW_INPUT_HUB", _RAW_INPUT_HUB, _ORIG_RAW_INPUT_HUB)
            if hub is None
            else hub
        )
        self._lock = threading.Lock()
        self._subscribed = False
        self._stopped = False
        self.error: str | None = None

    def start(self, on_event) -> bool:
        with self._lock:
            if self._stopped:
                return False
            if not self._subscribed:
                self._subscribed = self._hub.subscribe(self, on_event)
                if not self._subscribed:
                    self.error = self._hub.error
            return self._subscribed

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            subscribed, self._subscribed = self._subscribed, False
        if subscribed:
            self._hub.unsubscribe(self)

    def press_number(self) -> int:
        """The process-wide number of the SLACS press now being dispatched."""
        return self._hub.press_number()


# -- Windows foreground + identity providers (read-only) -----------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _windows_foreground_process() -> dict | None:
    """Return the foreground window's owning process, or None. Read-only."""
    api = _resolve_stage_keys_attr("_win32", _win32, _ORIG_WIN32)()
    ctypes, wintypes = api.ctypes, api.wintypes
    hwnd = api.user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = api.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not api.kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                            ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        api.kernel32.CloseHandle(handle)
    return {"pid": int(pid.value), "created": int(created)}


def _windows_process_image_name(api, handle) -> str | None:
    size = api.wintypes.DWORD(32768)
    buffer = api.ctypes.create_unicode_buffer(size.value)
    if not api.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, api.ctypes.byref(size)):
        return None
    return buffer.value


_PROCESS_COMMAND_LINE_INFORMATION = 60


def _windows_process_command_line(api, handle) -> str | None:
    ctypes = api.ctypes
    length = api.wintypes.ULONG(0)
    # STATUS_INFO_LENGTH_MISMATCH (0xC0000004) is expected on the sizing call.
    api.ntdll.NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE_INFORMATION,
                                        None, 0, ctypes.byref(length))
    if length.value == 0 or length.value > 1 << 20:
        return None
    buffer = ctypes.create_string_buffer(length.value)
    status = api.ntdll.NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE_INFORMATION,
                                                 buffer, length.value, ctypes.byref(length))
    if status != 0:
        return None
    unicode_string = ctypes.cast(buffer, ctypes.POINTER(api.UNICODE_STRING)).contents
    if not unicode_string.Buffer or unicode_string.Length == 0:
        return None
    return ctypes.wstring_at(unicode_string.Buffer, unicode_string.Length // 2)


def _windows_process_is_slacs(pid, created=None) -> bool:
    """True when ``pid`` runs the SLACS GUI script. Read-only; fails closed."""
    import ntpath

    api = _resolve_stage_keys_attr("_win32", _win32, _ORIG_WIN32)()
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    handle = api.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        image_name_fn = _resolve_stage_keys_attr(
            "_windows_process_image_name",
            _windows_process_image_name,
            _ORIG_WINDOWS_PROCESS_IMAGE_NAME,
        )
        image = image_name_fn(api, handle)
        if not image or ntpath.basename(image).lower() not in ("python.exe", "pythonw.exe"):
            return False
        cmd_line_fn = _resolve_stage_keys_attr(
            "_windows_process_command_line",
            _windows_process_command_line,
            _ORIG_WINDOWS_PROCESS_COMMAND_LINE,
        )
        command_line = cmd_line_fn(api, handle)
    finally:
        api.kernel32.CloseHandle(handle)
    if not command_line:
        return False
    matcher = _resolve_stage_keys_attr(
        "command_line_is_slacs", command_line_is_slacs, _ORIG_COMMAND_LINE_IS_SLACS
    )
    return matcher(command_line)


_ORIG_WIN32 = _win32
_ORIG_MODIFIER_TRACKER = _ModifierTracker
_ORIG_WINDOWS_SIDE_DOWN = _windows_side_down
_ORIG_RAW_INPUT_KEY_OBSERVER = _RawInputKeyObserver
_ORIG_RAW_INPUT_HUB = _RAW_INPUT_HUB
_ORIG_WINDOWS_PROCESS_IMAGE_NAME = _windows_process_image_name
_ORIG_WINDOWS_PROCESS_COMMAND_LINE = _windows_process_command_line
_ORIG_COMMAND_LINE_IS_SLACS = command_line_is_slacs
