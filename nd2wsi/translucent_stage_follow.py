"""Stage-follow coordination, laser-target persistence, and Win32 focus/click-through.

Owns the overlay's SLACS detection/follow session lifecycle, laser-shot delivery
and reporting, follow-flip persistence, client diagnostic logging, and the
Windows-only hover-gated click-through / non-activating show / focus-yield
controller (Pillar 3).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from . import session_log
from .translucent_validation import (
    CLIENT_LOG_FIELD_LIMIT,
    CLIENT_LOG_MAX,
    CLIENT_LOG_STACK_LIMIT,
    CLIENT_LOG_WINDOW,
    MARK_CONTEXT_KEYS,
    TARGET_KEYS,
    format_client_log_fields,
    laser_scope,
    validate_follow_flips,
    validate_laser_target_request,
    validate_locked_interactive_rects,
    validate_overlay_view,
    validate_pixel_size_um,
    validate_stage_follow_options,
)

# -- Win32 extended-style, non-activating show, and hover-gate constants -------

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

LOCKED_CLICKTHROUGH_EXSTYLE = (
    WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
)

SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040

# WinForms clears WS_EX_LAYERED when Form.Opacity rounds to 255 (1.0), which
# disables WS_EX_TRANSPARENT hit-test pass-through. Cap at 254/255 while locked
# and restore the exact requested opacity upon unlock.
LOCKED_MAX_OPACITY = 254.0 / 255.0
HOVER_POLL_INTERVAL_S = 0.030
HOVER_RECT_PADDING_CSS = 3.0
_LOGPIXELSX = 88


@lru_cache(maxsize=1)
def _win32_has_per_window_dpi() -> bool:
    """True when ``user32.GetDpiForWindow`` (Windows 10 build 14393+) is exported."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        windll_cls = getattr(ctypes, "WinDLL", None)
        if windll_cls is None:
            return False
        user32 = windll_cls("user32", use_last_error=True)
        return getattr(user32, "GetDpiForWindow", None) is not None
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=1)
def _win32_system_dpi_scale() -> float:
    """Cached logon-session system DPI scale (LOGPIXELSX / 96) for pre-1607 Windows 10."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        from ctypes import wintypes

        windll_cls = getattr(ctypes, "WinDLL", None)
        if windll_cls is None:
            return 1.0
        user32 = windll_cls("user32", use_last_error=True)
        gdi32 = windll_cls("gdi32", use_last_error=True)
        get_dc = user32.GetDC
        get_dc.argtypes = [wintypes.HWND]
        get_dc.restype = wintypes.HDC
        release_dc = user32.ReleaseDC
        release_dc.argtypes = [wintypes.HWND, wintypes.HDC]
        release_dc.restype = ctypes.c_int
        get_caps = gdi32.GetDeviceCaps
        get_caps.argtypes = [wintypes.HDC, ctypes.c_int]
        get_caps.restype = ctypes.c_int
        hdc = get_dc(0)
        if not hdc or isinstance(hdc, bool):
            return 1.0
        try:
            dpi = get_caps(hdc, _LOGPIXELSX)
        finally:
            release_dc(0, hdc)
        if (
            not isinstance(dpi, bool)
            and isinstance(dpi, (int, float))
            and math.isfinite(dpi)
            and dpi > 0
        ):
            return float(dpi) / 96.0
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def win32_window_scale(native) -> float:
    """Physical-per-CSS pixel scale for a Windows BrowserForm (including pre-1607 Win10)."""
    if sys.platform == "win32" and not _win32_has_per_window_dpi():
        sys_scale = _win32_system_dpi_scale()
        if sys_scale != 1.0:
            return sys_scale
    scale = getattr(native, "_scale", None)
    if (
        not isinstance(scale, bool)
        and isinstance(scale, (int, float))
        and math.isfinite(scale)
        and scale > 0
    ):
        return float(scale)
    return 1.0


@lru_cache(maxsize=1)
def _win32_overlay_api():
    """Private, pointer-sized user32 prototypes for overlay focus/click-through.

    Uses ``GetWindowLongPtrW`` / ``SetWindowLongPtrW`` on 64-bit Windows with
    fallback to ``GetWindowLongW`` / ``SetWindowLongW`` on 32-bit Windows.
    Never mutates ``ctypes.windll``. Windows only.
    """
    import ctypes
    from ctypes import wintypes

    windll_cls = getattr(ctypes, "WinDLL", None)
    if windll_cls is None:
        raise OSError("Win32 user32 is unavailable on this platform")

    user32 = windll_cls("user32", use_last_error=True)
    LONG_PTR = ctypes.c_ssize_t
    HWND = wintypes.HWND
    UINT = wintypes.UINT
    BOOL = wintypes.BOOL
    WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)

    def declare(function, restype, *argtypes):
        function.restype = restype
        function.argtypes = list(argtypes)

    get_long_ptr = getattr(user32, "GetWindowLongPtrW", None)
    if get_long_ptr is None:
        get_long_ptr = user32.GetWindowLongW
        declare(get_long_ptr, wintypes.LONG, HWND, ctypes.c_int)
    else:
        declare(get_long_ptr, LONG_PTR, HWND, ctypes.c_int)

    set_long_ptr = getattr(user32, "SetWindowLongPtrW", None)
    if set_long_ptr is None:
        set_long_ptr = user32.SetWindowLongW
        declare(set_long_ptr, wintypes.LONG, HWND, ctypes.c_int, wintypes.LONG)
    else:
        declare(set_long_ptr, LONG_PTR, HWND, ctypes.c_int, LONG_PTR)

    declare(user32.ShowWindow, BOOL, HWND, ctypes.c_int)
    declare(
        user32.SetWindowPos,
        BOOL,
        HWND,
        HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        UINT,
    )
    declare(user32.GetForegroundWindow, HWND)
    declare(user32.SetForegroundWindow, BOOL, HWND)
    declare(user32.GetWindowThreadProcessId, wintypes.DWORD, HWND, wintypes.LPDWORD)
    declare(user32.IsWindow, BOOL, HWND)
    declare(user32.IsWindowVisible, BOOL, HWND)
    declare(user32.EnumWindows, BOOL, WNDENUMPROC, wintypes.LPARAM)
    declare(user32.GetCursorPos, BOOL, ctypes.POINTER(wintypes.POINT))
    declare(user32.ScreenToClient, BOOL, HWND, ctypes.POINTER(wintypes.POINT))

    get_dpi_for_window = getattr(user32, "GetDpiForWindow", None)
    if get_dpi_for_window is not None:
        declare(get_dpi_for_window, UINT, HWND)

    return SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        user32=user32,
        get_window_long_ptr=get_long_ptr,
        set_window_long_ptr=set_long_ptr,
        get_dpi_for_window=get_dpi_for_window,
        WNDENUMPROC=WNDENUMPROC,
    )


def _hwnd_from_native(native) -> int | None:
    """Extract a 64-bit-safe integer HWND from a WinForms Form; never calls ToInt32()."""
    if native is None:
        return None
    handle = getattr(native, "Handle", None)
    if handle is None or isinstance(handle, bool):
        return None
    if type(handle) is int:
        return handle if handle > 0 else None
    to_int64 = getattr(handle, "ToInt64", None)
    if callable(to_int64):
        try:
            raw = to_int64()
        except Exception:  # noqa: BLE001
            return None
        if type(raw) is int and raw > 0:
            return raw
    return None


def _window_pid(api, hwnd: int) -> int | None:
    if not hwnd:
        return None
    pid = api.wintypes.DWORD(0)
    if not api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid)):
        return None
    return int(pid.value) or None


def _window_dpi_scale(api, hwnd: int, native=None) -> float:
    """Return display scale factor (physical px per CSS px) for ``hwnd``."""
    get_dpi = getattr(api, "get_dpi_for_window", None)
    if callable(get_dpi):
        try:
            dpi = get_dpi(hwnd)
            if not isinstance(dpi, bool) and isinstance(dpi, (int, float)) and math.isfinite(dpi) and dpi > 0:
                return float(dpi) / 96.0
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32" and not _win32_has_per_window_dpi():
        sys_scale = _win32_system_dpi_scale()
        if sys_scale != 1.0:
            return sys_scale
    scale = getattr(native, "_scale", None)
    if not isinstance(scale, bool) and isinstance(scale, (int, float)) and math.isfinite(scale) and scale > 0:
        return float(scale)
    return 1.0


def point_in_interactive_rects(
    css_x: float,
    css_y: float,
    rects,
    *,
    padding: float = HOVER_RECT_PADDING_CSS,
) -> bool:
    """Whether ``(css_x, css_y)`` falls inside any interactive rect expanded by ``padding``."""
    for rect in rects:
        rw = rect["width"]
        rh = rect["height"]
        if rw <= 0 or rh <= 0:
            continue
        rx = rect["x"]
        ry = rect["y"]
        if (rx - padding) <= css_x <= (rx + rw + padding) and (ry - padding) <= css_y <= (ry + rh + padding):
            return True
    return False


def _find_visible_window_for_pid(api, pid: int) -> int | None:
    """Find the topmost visible top-level HWND owned by ``pid``, or None."""
    found: list[int] = []

    def collect(hwnd, _lparam):
        if hwnd and api.user32.IsWindow(hwnd) and api.user32.IsWindowVisible(hwnd):
            if _window_pid(api, int(hwnd)) == pid:
                found.append(int(hwnd))
                return False
        return True

    callback = api.WNDENUMPROC(collect)
    api.user32.EnumWindows(callback, 0)
    return found[0] if found else None


class Win32OverlayFocusController:
    """Windows-only focus, tool-window, non-activating show, and hover-gated click-through."""

    def __init__(
        self,
        *,
        slacs_pid_provider,
        logger,
        api_factory=None,
        poll_interval: float = HOVER_POLL_INTERVAL_S,
        padding: float = HOVER_RECT_PADDING_CSS,
    ):
        self._slacs_pid_provider = slacs_pid_provider
        self._logger = logger
        self._api_factory = api_factory or (lambda: _win32_overlay_api())
        self._poll_interval = float(poll_interval)
        self._padding = float(padding)
        self._state_lock = threading.Lock()
        self._rects: tuple[dict[str, float], ...] = ()
        self._locked = False
        self._hwnd: int | None = None
        self._native = None
        self._opacity_capped = False
        self._slacs_hwnd: int | None = None
        self._hover_stop: threading.Event | None = None
        self._hover_thread: threading.Thread | None = None

    def _get_api(self):
        if sys.platform != "win32":
            return None
        try:
            return self._api_factory()
        except (AttributeError, OSError, ImportError):
            return None

    @property
    def slacs_hwnd(self) -> int | None:
        with self._state_lock:
            return self._slacs_hwnd

    @slacs_hwnd.setter
    def slacs_hwnd(self, value: int | None) -> None:
        with self._state_lock:
            self._slacs_hwnd = value if type(value) is int and value > 0 else None

    def clear_slacs_hwnd(self) -> None:
        with self._state_lock:
            self._slacs_hwnd = None

    def set_interactive_rects(self, rects: list[dict[str, float]]) -> None:
        with self._state_lock:
            self._rects = tuple(dict(rect) for rect in rects)

    def interactive_rects(self) -> list[dict[str, float]]:
        with self._state_lock:
            return [dict(rect) for rect in self._rects]

    def can_control(self, native) -> bool:
        return (
            sys.platform == "win32"
            and _hwnd_from_native(native) is not None
            and self._get_api() is not None
        )

    def apply_toolwindow(self, native) -> bool:
        """Apply ``WS_EX_TOOLWINDOW`` (and clear ``WS_EX_APPWINDOW``) on ``native``'s HWND."""
        hwnd = _hwnd_from_native(native)
        if hwnd is None:
            return False
        api = self._get_api()
        if api is None:
            return False
        with self._state_lock:
            old_style = int(api.get_window_long_ptr(hwnd, GWL_EXSTYLE))
            new_style = (old_style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
            if new_style != old_style:
                api.set_window_long_ptr(hwnd, GWL_EXSTYLE, new_style)
        return True

    def is_slacs_foreground(self) -> bool:
        """True if SLACS currently owns the Windows foreground window."""
        api = self._get_api()
        if api is None:
            return False
        fg = api.user32.GetForegroundWindow()
        if not fg:
            return False
        fg_hwnd = int(fg)
        fg_pid = _window_pid(api, fg_hwnd)
        if fg_pid is None or fg_pid == os.getpid():
            return False
        slacs_pid = self._slacs_pid_provider()
        if slacs_pid is not None:
            if fg_pid == slacs_pid:
                with self._state_lock:
                    self._slacs_hwnd = fg_hwnd
                return True
            return False
        with self._state_lock:
            cached = self._slacs_hwnd
        if cached is not None and fg_hwnd == cached and api.user32.IsWindow(fg_hwnd):
            return True
        try:
            from .stage_keys import _windows_process_is_slacs

            if _windows_process_is_slacs(fg_pid):
                with self._state_lock:
                    self._slacs_hwnd = fg_hwnd
                return True
        except Exception:  # noqa: BLE001 - best-effort detection off Windows
            pass
        return False

    def show_noactivate(self, native) -> bool:
        """Show and raise the overlay topmost without stealing keyboard activation."""
        hwnd = _hwnd_from_native(native)
        if hwnd is None:
            return False
        api = self._get_api()
        if api is None:
            return False
        api.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        api.user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        return True

    def apply_locked(self, native, requested_opacity: float) -> bool:
        """Enable click-through + non-activating styles, opacity cap, and 30 ms hover gate."""
        hwnd = _hwnd_from_native(native)
        if hwnd is None:
            return False
        api = self._get_api()
        if api is None:
            return False
        self.stop_hover_timer()
        with self._state_lock:
            if requested_opacity > LOCKED_MAX_OPACITY:
                try:
                    native.Opacity = LOCKED_MAX_OPACITY
                    self._opacity_capped = True
                except AttributeError:
                    self._opacity_capped = False
            else:
                self._opacity_capped = False
            old_style = int(api.get_window_long_ptr(hwnd, GWL_EXSTYLE))
            new_style = (old_style & ~WS_EX_APPWINDOW) | LOCKED_CLICKTHROUGH_EXSTYLE
            if new_style != old_style:
                api.set_window_long_ptr(hwnd, GWL_EXSTYLE, new_style)
            self._locked = True
            self._hwnd = hwnd
            self._native = native
            self._start_hover_timer_locked()
        return True

    def apply_unlocked(self, native, requested_opacity: float) -> bool:
        """Stop hover gate, clear click-through/noactivate (keep toolwindow), restore opacity."""
        self.stop_hover_timer()
        hwnd = _hwnd_from_native(native)
        if hwnd is None:
            return False
        api = self._get_api()
        if api is None:
            return False
        with self._state_lock:
            if self._locked:
                return False
            old_style = int(api.get_window_long_ptr(hwnd, GWL_EXSTYLE))
            new_style = (
                (old_style & ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_APPWINDOW))
                | WS_EX_TOOLWINDOW
            )
            if new_style != old_style:
                api.set_window_long_ptr(hwnd, GWL_EXSTYLE, new_style)
            if self._opacity_capped:
                try:
                    native.Opacity = requested_opacity
                except AttributeError:
                    pass
                self._opacity_capped = False
            self._hwnd = None
            self._native = None
        return True

    def _start_hover_timer_locked(self) -> None:
        if self._hover_stop is not None:
            self._hover_stop.set()
        stop_event = threading.Event()
        self._hover_stop = stop_event
        interval = self._poll_interval

        def loop():
            while not stop_event.wait(interval):
                try:
                    result = self.poll_hover_once()
                except Exception as exc:  # noqa: BLE001 - timer must never crash UI
                    self._logger(f"translucent hover-gate poll failed: {exc!r}")
                    continue
                if result is None:
                    with self._state_lock:
                        if not self._locked or self._hwnd is None:
                            break

        thread = threading.Thread(
            target=loop, name="nd2wsi-translucent-hover-gate", daemon=True
        )
        self._hover_thread = thread
        thread.start()

    def stop_hover_timer(self) -> None:
        with self._state_lock:
            self._locked = False
            stop_event, self._hover_stop = self._hover_stop, None
            thread, self._hover_thread = self._hover_thread, None
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.2)

    def poll_hover_once(self) -> bool | None:
        """One hover-gate evaluation: clear ``WS_EX_TRANSPARENT`` only over interactive rects."""
        with self._state_lock:
            if not self._locked or self._hwnd is None:
                return None
            hwnd = self._hwnd
            native = self._native
            rects = self._rects
            api = self._get_api()
            if api is None:
                return None
            if not api.user32.IsWindow(hwnd):
                self._locked = False
                self._hwnd = None
                self._native = None
                if self._hover_stop is not None:
                    self._hover_stop.set()
                return None
            pt = api.wintypes.POINT()
            if (
                not api.user32.GetCursorPos(api.ctypes.byref(pt))
                or not api.user32.ScreenToClient(hwnd, api.ctypes.byref(pt))
            ):
                inside = False
            else:
                scale = _window_dpi_scale(api, hwnd, native)
                css_x = pt.x / scale
                css_y = pt.y / scale
                inside = point_in_interactive_rects(
                    css_x, css_y, rects, padding=self._padding
                )
            old_style = int(api.get_window_long_ptr(hwnd, GWL_EXSTYLE))
            if inside:
                new_style = (
                    (old_style & ~(WS_EX_TRANSPARENT | WS_EX_APPWINDOW))
                    | WS_EX_LAYERED
                    | WS_EX_NOACTIVATE
                    | WS_EX_TOOLWINDOW
                )
            else:
                new_style = (old_style & ~WS_EX_APPWINDOW) | LOCKED_CLICKTHROUGH_EXSTYLE
            if new_style != old_style:
                api.set_window_long_ptr(hwnd, GWL_EXSTYLE, new_style)
            return inside

    def _resolve_slacs_hwnd(self, api) -> int | None:
        slacs_pid = self._slacs_pid_provider()
        with self._state_lock:
            cached = self._slacs_hwnd
        if cached is not None:
            if api.user32.IsWindow(cached) and (
                slacs_pid is None or _window_pid(api, cached) == slacs_pid
            ):
                return cached
            with self._state_lock:
                if self._slacs_hwnd == cached:
                    self._slacs_hwnd = None
        if slacs_pid is None:
            return None
        found = _find_visible_window_for_pid(api, slacs_pid)
        if found is not None:
            with self._state_lock:
                self._slacs_hwnd = found
        return found

    def yield_focus_to_slacs(self) -> bool:
        """Return foreground keyboard focus to SLACS iff this process currently holds it."""
        api = self._get_api()
        if api is None:
            return False
        fg = api.user32.GetForegroundWindow()
        if not fg:
            return False
        fg_pid = _window_pid(api, int(fg))
        if fg_pid != os.getpid():
            return False
        slacs_hwnd = self._resolve_slacs_hwnd(api)
        if not slacs_hwnd or not api.user32.IsWindow(slacs_hwnd):
            return False
        return bool(api.user32.SetForegroundWindow(slacs_hwnd))

    def close(self) -> None:
        self.stop_hover_timer()
        with self._state_lock:
            self._hwnd = None
            self._native = None
            self._opacity_capped = False
            self._slacs_hwnd = None


# -- Stage-follow, laser-target, and overlay reporting mixin -------------------


class StageFollowMixin:
    """Stage-follow session lifecycle, laser-shot delivery, and focus coordination."""

    @property
    def _slacs_hwnd(self) -> int | None:
        return self._win32_focus.slacs_hwnd

    @_slacs_hwnd.setter
    def _slacs_hwnd(self, value: int | None) -> None:
        self._win32_focus.slacs_hwnd = value

    def _yield_focus_to_slacs(self) -> bool:
        """Return Windows keyboard focus to SLACS if this process holds foreground."""
        if sys.platform != "win32" or self._closed:
            return False
        try:
            return self._win32_focus.yield_focus_to_slacs()
        except Exception as exc:  # noqa: BLE001 - best-effort focus handback
            self._logger(f"translucent focus yield failed: {exc!r}")
            return False

    def set_locked_interactive_rects(self, rects):
        """Register client-area CSS rects kept clickable while locked on Windows."""
        try:
            clean = validate_locked_interactive_rects(rects)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}
        with self._lock:
            if self._closed:
                return {"ok": False, "message": "This translucent viewer is closed."}
            self._win32_focus.set_interactive_rects(clean)
        return {"ok": True, "count": len(clean)}

    def _orientation(self):
        """The main viewer's rotation and flip at handoff, as Follow records name them."""
        return {
            "view_rotation": self._context.get("rotation", 0),
            "view_flip": self._context.get("flip", False),
        }

    def _restore_flips(self):
        """Start with the flips remembered for this image's format; never raises."""
        try:
            stored, warning = self._flip_store.load(self._source_format)
        except Exception as exc:  # noqa: BLE001 - a broken store only loses the memory
            stored, warning = None, f"Saved Follow flips were not restored: {exc}"
        if warning:
            session_log.event(
                "follow_flip_load_failed",
                source_format=self._source_format,
                message=warning,
            )
        if stored is None:
            return
        self._follow_flips = stored
        session_log.event(
            "follow_flip",
            source="remembered",
            source_format=self._source_format,
            flip_x=stored[0],
            flip_y=stored[1],
            image_name=self._context.get("image_name"),
            **self._orientation(),
        )

    def _ensure_slacs_discovery(self):
        from .slacs_discovery import SlacsDiscovery

        with self._lock:
            if not self._closed and self._discovery is None:
                self._discovery = SlacsDiscovery(
                    on_change=self._on_slacs_change, name="translucent"
                )
                self._slacs_snapshot = self._discovery.snapshot()
                logged = self._detection_to_log()
            else:
                logged = None
            discovery = self._discovery
        self._log_detection(logged)
        return discovery

    def _detection_to_log(self):
        """The detection state to record, once per change; call with the lock held."""
        current = self._slacs_snapshot
        state = (
            current.get("available") is True,
            current.get("session_id"),
            current.get("status"),
        )
        if state == self._slacs_logged:
            return None
        self._slacs_logged = state
        return state

    @staticmethod
    def _log_detection(state):
        # So a detection that fails on the instrument PC can be diagnosed afterwards.
        if state is not None:
            session_log.event(
                "slacs_detection",
                window="translucent",
                available=state[0],
                session_id=state[1],
                status=state[2],
            )

    def _on_slacs_change(self, snapshot):
        """Stop an active receiver as soon as its detected process is lost or replaced.

        Every stage_follow_state reply computed after this point reports the loss
        and carries no movement. A reply computed just before it may still be on its
        way to the page, so the page is also told at once; it drops any follow reply
        that arrives after this revocation (translucent-v1.js revokeStageFollow).
        """
        receiver = follower = source = revoked = message = None
        clear_hwnd = False
        with self._lock:
            if self._closed:
                return
            previous_session = self._slacs_snapshot.get("session_id")
            available = (
                snapshot.get("available") is True
                and snapshot.get("status") == "running"
                and isinstance(snapshot.get("session_id"), str)
                and bool(snapshot["session_id"])
            )
            session = snapshot.get("session_id") if available else None
            if not available or (previous_session is not None and session != previous_session):
                clear_hwnd = True
            self._slacs_snapshot = {
                "available": available,
                "session_id": session,
                "status": snapshot.get("status", "unavailable"),
            }
            logged = self._detection_to_log()
            # Every Follow is bound to the SLACS process detected when it started;
            # that process's loss or replacement ends it.
            if (
                self._stage_follow is not None
                and self._stage_follow_session is not None
                and session != self._stage_follow_session
            ):
                receiver, self._stage_follow = self._stage_follow, None
                follower, self._stage_follower = self._stage_follower, None
                source, self._stage_follow_source = self._stage_follow_source, None
                self._stage_follow_status = None
                revoked, self._stage_follow_receiver_id = self._stage_follow_receiver_id, None
                self._stage_follow_session = None
                message = self._stage_follow_lost = (
                    "SLACS program stopped or changed. Follow stopped; reconnect manually."
                )
        if clear_hwnd:
            self._win32_focus.clear_slacs_hwnd()
        self._log_detection(logged)
        if receiver is not None:
            if self._laser_delivery is not None:
                self._laser_delivery.view_changed()
            self._revoke_follow_in_page(revoked, message)
            if follower is not None:
                follower.stop("slacs_lost")
            receiver.close()
            session_log.event("follow_stop", source=source, reason="slacs_lost")

    def _revoke_follow_in_page(self, receiver_id, message):
        """Queue the revocation on the same script channel that carries bridge replies.

        pywebview delivers every bridge reply through ``evaluate_js``, so replies queued
        after this revocation reach the page after it. A separate thread keeps the
        discovery callback quick and never waits on the UI thread.
        """
        window = self._window
        if window is None:
            return
        script = (
            "window.nd2TranslucentRevokeFollow?.("
            + json.dumps({"session_id": receiver_id, "message": message}, allow_nan=False)
            + "); true"
        )
        try:
            threading.Thread(
                target=self._push_revocation,
                args=(window, script),
                name="nd2wsi-stage-follow-revoke",
                daemon=True,
            ).start()
        except RuntimeError as exc:  # the receiver must still be closed by the caller
            self._logger(f"stage follow revocation could not start: {exc!r}")

    def _push_revocation(self, window, script):
        if self._closed:
            return
        try:
            window.evaluate_js(script)
        except Exception as exc:
            self._logger(f"stage follow revocation could not reach the page: {exc!r}")

    def slacs_discovery_state(self):
        """Report program detection; this does not establish a motion connection."""
        self._ensure_slacs_discovery()
        with self._lock:
            return dict(self._slacs_snapshot)

    def save_laser_target(self, payload):
        """Validate and remember a completed calibration or resize/rotation gesture."""
        try:
            target, calibrate, zoom = validate_laser_target_request(payload)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "message": str(exc)}
        with self._lock:
            if self._closed or self._locked:
                return {"ok": False, "message": "Unlock to set the laser target."}
            self._laser_target = target
        saved, message = False, None
        with self._target_save_lock:
            if self._target_store is not None:
                try:
                    self._target_store.save(self._laser_target)
                    saved = True
                except (OSError, ValueError) as exc:
                    message = f"Applied for this window, but could not be remembered: {exc}"
        session_log.event(
            "laser_target_set",
            calibrated=calibrate,
            **copy.deepcopy(target),
            zoom=zoom,
        )
        if message:
            session_log.event("laser_target_save_failed", message=message)
        return {"ok": True, "saved": saved, "message": message, "target": copy.deepcopy(target)}

    def _stage_follow_options(self, options):
        return validate_stage_follow_options(options)

    def _slacs_pid(self):
        """The detected SLACS process ID for read-only control reads, or None; never raises.

        Runs on the controls monitor's thread, so it takes no overlay lock. The
        detector reports a pid only for the session it currently reports, and a
        lost or replaced session stops Follow (_on_slacs_change).
        """
        discovery = self._discovery
        if self._closed or discovery is None:
            return None
        try:
            pid = discovery.slacs_pid()
        except Exception:  # noqa: BLE001 - an unknown pid only skips a read
            return None
        return pid if type(pid) is int and pid > 0 else None

    def _slacs_identifier(self):
        """Process-identity callback bound to the detected SLACS at Follow start, or None."""
        from .slacs_discovery import _bound_process_identifier

        bound_pid = self._slacs_pid()
        if bound_pid is None:
            return None
        bound_created = None
        discovery = self._discovery
        if discovery is not None:
            try:
                created = discovery.slacs_creation_filetime()
            except Exception:  # noqa: BLE001 - optional creation timestamp
                created = None
            if type(created) is int and created >= 0:
                bound_created = created
        return _bound_process_identifier(bound_pid, bound_created)

    def _slacs_controls_monitor(self):
        """A read-only monitor of SLACS's Roughness and Mirror controls, or None and why not.

        Follow never depends on it: without one the key follower moves by the
        manual step, recorded as a fallback.
        """
        try:
            from .slacs_controls import SlacsControlsMonitor

            return SlacsControlsMonitor(self._slacs_pid), None
        except Exception as exc:  # noqa: BLE001 - a missing reader must not stop Follow
            self._logger(f"SLACS controls monitor unavailable: {exc!r}")
            return None, f"{type(exc).__name__}: {exc}"[:CLIENT_LOG_FIELD_LIMIT]

    def start_stage_follow(self, options=None):
        """Opt in to this overlay's receiver; never connect to a controller.

        ``options`` may set ``source`` ("keys" to observe SLACS W/A/S/D presses,
        "adapter" for an external in-process/HTTP sender), the per-key
        ``step_um`` and ``step_source`` ("slacs" to move each press by SLACS's
        Roughness, read from its window, or "manual"). The key follower is a
        strict observer and never blocks, delays, or alters a keystroke; it owns
        the Roughness monitor's start and stop. It starts with this overlay's
        Follow flips and logs SLACS's "," and "." presses with ``_mark_context``.
        Follow is refused unless SLACS is detected, and is bound to that
        detected process.
        """
        from .stage_follow import StageFollowReceiver
        from .stage_keys import SlacsKeyFollower

        source, step_um, step_source, option_error = self._stage_follow_options(options)
        reused_result = None
        with self._lock:
            if self._closed:
                return {"ok": False, "message": "This translucent viewer is closed."}
            if option_error is not None:
                return {"ok": False, "message": option_error}
            detected = self._slacs_snapshot
            if not (
                detected.get("available") is True
                and isinstance(detected.get("session_id"), str)
                and detected["session_id"]
            ):
                return {
                    "ok": False,
                    "message": "SLACS is not detected. Start SLACS, then turn on Follow.",
                }
            pixel_size_xy = validate_pixel_size_um(self._context.get("pixel_size_um"))
            if pixel_size_xy is None:
                return {"ok": False, "message": "Stage follow requires a calibrated image."}
            if self._stage_follow is not None:
                descriptor = self._stage_follow.start()
                reused_result = {"ok": True, "source": self._stage_follow_source, **descriptor}
            else:
                # Not re-read from the store: a toggle just before Follow may still be saving.
                flip_x, flip_y = self._follow_flips
                receiver = StageFollowReceiver()
                try:
                    descriptor = receiver.start()
                except (OSError, RuntimeError) as exc:
                    receiver.close()
                    return {
                        "ok": False,
                        "message": "Could not start the local motion receiver: " + str(exc),
                    }
                follower = controls_error = None
                made_delivery = False
                bound = detected["session_id"]

                def _rollback_stage_follow(*, stop_reason: str | None = None) -> None:
                    self._stage_follow = None
                    self._stage_follower = None
                    self._stage_follow_session = None
                    self._stage_follow_receiver_id = None
                    self._stage_follow_source = None
                    self._stage_follow_status = None
                    self._stage_follow_lost = ""
                    try:
                        if follower is not None and stop_reason is not None:
                            follower.stop(stop_reason)
                    finally:
                        try:
                            receiver.close()
                        finally:
                            if made_delivery and self._laser_delivery is not None:
                                self._laser_delivery.close()
                                self._laser_delivery = None

                if source == "keys":
                    if self._on_laser_box is not None and self._laser_delivery is None:
                        from .slacs_laser import LaserDelivery

                        made_delivery = True
                        self._laser_delivery = LaserDelivery(
                            prepare=self._prepare_laser_box,
                            deliver=self._deliver_laser_box,
                            confirmed=self._confirm_laser_box,
                        )
                    controls = None
                    if step_source == "slacs":
                        controls, controls_error = self._slacs_controls_monitor()
                    identifier = self._slacs_identifier()
                    identifier_kwargs = {"identifier": identifier} if identifier is not None else {}
                    try:
                        follower = SlacsKeyFollower(
                            receiver,
                            descriptor["session_id"],
                            step_um=step_um,
                            step_source=step_source,
                            controls=controls,
                            pixel_size_um=pixel_size_xy,
                            flip_x=flip_x,
                            flip_y=flip_y,
                            mark_context=self._mark_context,
                            on_mark=self._on_mark_record,
                            **identifier_kwargs,
                        )
                        # Bind all 7 attributes before starting the Raw Input thread so
                        # lock-free _mark_context / _on_mark_record callbacks never see
                        # unbound session or follower state.
                        self._stage_follow = receiver
                        self._stage_follower = follower
                        self._stage_follow_session = bound
                        self._stage_follow_receiver_id = descriptor.get("session_id")
                        self._stage_follow_source = source
                        self._stage_follow_status = None
                        self._stage_follow_lost = ""
                        snapshot = follower.start()
                    except (ValueError, OSError, RuntimeError) as exc:
                        snapshot = {"running": False, "error": str(exc)}
                    except BaseException:
                        _rollback_stage_follow(stop_reason="start_failed")
                        raise
                    if not snapshot.get("running"):
                        _rollback_stage_follow(stop_reason="start_failed")
                        message = snapshot.get("error") or "Could not start key following."
                        session_log.event(
                            "follow_start",
                            source=source,
                            step_source=step_source,
                            ok=False,
                            message=message,
                        )
                        return {"ok": False, "message": message}
                else:
                    self._stage_follow = receiver
                    self._stage_follower = None
                    self._stage_follow_session = bound
                    self._stage_follow_receiver_id = descriptor.get("session_id")
                    self._stage_follow_source = source
                    self._stage_follow_status = None
                    self._stage_follow_lost = ""
        if reused_result is not None:
            self._yield_focus_to_slacs()
            return reused_result
        if self._locked and self._on_lock is not None:
            self._on_lock(True)
        extra = {"controls_error": controls_error} if controls_error else {}
        session_log.event(
            "follow_start",
            source=source,
            step_source=step_source,
            step_um=step_um,
            ok=True,
            slacs_session=bound,
            source_format=self._source_format,
            flip_x=flip_x,
            flip_y=flip_y,
            **self._orientation(),
            **extra,
        )
        self._yield_focus_to_slacs()
        return {"ok": True, "source": source, **descriptor}

    def set_stage_follow_flips(self, payload):
        """Reverse Follow's on-screen A/D (flip_x) or W/S (flip_y) move; never the image.

        Applies from the next key press, also while Follow runs or the overlay
        is locked: the page applies only differences of the cumulative
        displacement, so its sequence and displacement stay consistent. The new
        pair is remembered for this image's format when the window has a store.
        """
        def current(ok, **extra):
            flip_x, flip_y = self._follow_flips
            return {
                "ok": ok,
                "flip_x": flip_x,
                "flip_y": flip_y,
                "source_format": self._source_format,
                **extra,
            }

        try:
            flips = validate_follow_flips(payload)
        except ValueError as exc:
            return current(False, message=str(exc))
        unchanged = False
        with self._lock:
            if self._closed:
                return current(False, message="This translucent viewer is closed.")
            previous = self._follow_flips
            if flips == previous:
                unchanged = True
            else:
                self._follow_flips = flips
                follower, receiver = self._stage_follower, self._stage_follow
                # Lock order: overlay, then follower, as in stage_follow_state.
                if follower is not None:
                    follower.set_flips(*flips)
                # Only the receiver's progress at the toggle: a press already on its way
                # may still arrive later with the old flips. Each follow_key names the
                # flips that press used.
                follow_seq = receiver.snapshot().get("seq") if receiver is not None else None
        if unchanged:
            self._yield_focus_to_slacs()
            return current(True, changed=False)
        saved, message = self._remember_flips()
        session_log.event(
            "follow_flip",
            source="user",
            source_format=self._source_format,
            flip_x=flips[0],
            flip_y=flips[1],
            previous=list(previous),
            following=follower is not None,
            follow_seq=follow_seq,
            saved=saved,
            image_name=self._context.get("image_name"),
            **self._orientation(),
        )
        self._yield_focus_to_slacs()
        result = {
            "ok": True,
            "changed": True,
            "flip_x": flips[0],
            "flip_y": flips[1],
            "source_format": self._source_format,
            "saved": saved,
        }
        if message:
            result["message"] = message
        return result

    def _remember_flips(self):
        """Save the current flips for this format: (saved, a message for the page or None).

        Saves the pair current when the save lock is taken, so the last toggle
        always wins on disk.
        """
        store = self._flip_store
        if store is None:
            return False, None
        with self._flip_save_lock:
            flip_x, flip_y = self._follow_flips
            try:
                store.save(self._source_format, flip_x, flip_y)
            except (OSError, ValueError) as exc:
                message = f"Applied for this window, but could not be remembered: {exc}"
                session_log.event(
                    "follow_flip_save_failed",
                    source_format=self._source_format,
                    flip_x=flip_x,
                    flip_y=flip_y,
                    message=message,
                )
                return False, message
        return True, None

    @staticmethod
    def _same_point_2d(a, b, tol: float) -> bool:
        if a is None and b is None:
            return True
        if not (
            isinstance(a, (list, tuple))
            and isinstance(b, (list, tuple))
            and len(a) == 2
            and len(b) == 2
        ):
            return False
        try:
            return abs(float(a[0]) - float(b[0])) <= tol and abs(float(a[1]) - float(b[1])) <= tol
        except (TypeError, ValueError):
            return False

    @classmethod
    def _same_overlay_geometry(cls, prev: dict | None, view: dict) -> bool:
        if not isinstance(prev, dict) or "at" not in prev:
            return False
        if (
            prev.get("follow_session") != view.get("follow_session")
            or prev.get("follow_seq") != view.get("follow_seq")
            or prev.get("locked") is not view.get("locked")
            or prev.get("flip") is not view.get("flip")
        ):
            return False
        if not cls._same_point_2d(prev.get("center_px"), view.get("center_px"), 1e-3):
            return False
        prev_zoom, view_zoom = prev.get("zoom"), view.get("zoom")
        if (
            isinstance(prev_zoom, bool)
            or isinstance(view_zoom, bool)
            or not isinstance(prev_zoom, (int, float))
            or not isinstance(view_zoom, (int, float))
            or abs(float(prev_zoom) - float(view_zoom)) > 1e-6
        ):
            return False
        prev_rot, view_rot = prev.get("rotation"), view.get("rotation")
        if (
            isinstance(prev_rot, bool)
            or isinstance(view_rot, bool)
            or not isinstance(prev_rot, (int, float))
            or not isinstance(view_rot, (int, float))
        ):
            return False
        rot_diff = abs((float(prev_rot) - float(view_rot) + 180.0) % 360.0 - 180.0)
        if rot_diff > 1e-4:
            return False
        if not cls._same_point_2d(prev.get("target_px"), view.get("target_px"), 1e-3):
            return False
        prev_corners = prev.get("target_corners_px")
        view_corners = view.get("target_corners_px")
        if prev_corners is None and view_corners is None:
            return True
        if not (
            isinstance(prev_corners, (list, tuple))
            and isinstance(view_corners, (list, tuple))
            and len(prev_corners) == len(view_corners) == 4
        ):
            return False
        return all(
            cls._same_point_2d(pa, pb, 1e-3) for pa, pb in zip(prev_corners, view_corners)
        )

    def report_overlay_view(self, payload):
        """Keep the page's current view for SLACS laser-shot records; never logged.

        A refused report forgets the kept view, so a later record shows no
        image centre rather than an old one as if it were current.
        """
        try:
            view = self._validated_view(payload)
        except (TypeError, ValueError) as exc:
            with self._lock:
                self._overlay_view = None
                self._overlay_view_epoch = None
            return {"ok": False, "message": str(exc)}
        with self._lock:
            if self._closed:
                return {"ok": False}
            now = time.monotonic()
            prev = self._overlay_view
            moved_at = (
                prev.get("moved_at", prev["at"])
                if self._same_overlay_geometry(prev, view)
                else now
            )
            self._overlay_view = {**view, "at": now, "moved_at": moved_at}
            self._overlay_view_epoch = self._epoch
            locked = view["locked"]
            changed = self._lock_reported is not None and self._lock_reported != locked
            if self._lock_reported is True and not locked:
                self._realign_from = self._last_locked_center
            realign = None
            if locked:
                if changed and self._realign_from is not None:
                    realign = {
                        "from_px": self._realign_from,
                        "to_px": view["center_px"],
                        "delta_px": [b - a for a, b in zip(self._realign_from, view["center_px"])],
                    }
                    self._realign_from = None
                self._last_locked_center = view["center_px"]
            self._lock_reported = locked
        if changed:
            session_log.event(
                "overlay_lock",
                locked=locked,
                center_px=view["center_px"],
                target_px=view.get("target_px"),
            )
        if realign is not None:
            session_log.event("overlay_realign", **realign)
        if self._laser_delivery is not None:
            self._laser_delivery.view_changed()
        return {"ok": True}

    def _validated_view(self, payload):
        return validate_overlay_view(payload)

    def _on_mark_record(self, record):
        # Raw Input: bounded enqueue only, no overlay lock, I/O or browser call.
        delivery = self._laser_delivery
        if delivery is not None:
            lock_epoch = self._epoch
            prior = self._overlay_view
            prior_seq = (
                prior.get("follow_seq")
                if isinstance(prior, dict) and prior.get("follow_session") == record.get("receiver_id")
                else None
            )
            delivery.enqueue({
                **record,
                "observed_at": time.monotonic(),
                "lock_epoch": lock_epoch,
                "prior_view_seq": prior_seq,
                "at": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            })

    def _prepare_laser_box(self, record):
        with self._lock:
            closed = self._closed
            stage_follow = self._stage_follow
            locked = self._locked
            receiver_id = self._stage_follow_receiver_id
            view = self._overlay_view
            view_epoch = self._overlay_view_epoch
            context = copy.deepcopy(self._context)
            epoch = self._epoch
            context_epoch = self._context_epoch
        if closed or stage_follow is None:
            return None, "follow_stopped"
        if not self._is_active():
            return None, "not_active_overlay"
        if record.get("locked") is not True or not locked:
            return None, "unlocked"
        if record.get("receiver_id") != receiver_id:
            return None, "session_changed"
        if type(record.get("press")) is not int or record["press"] <= 0:
            return None, "invalid_press"
        seq = record.get("follow_seq")
        if type(seq) is not int:
            return None, "stale_view"
        if view is None or view["follow_session"] != record["receiver_id"] or view["follow_seq"] is None:
            return None, "stale_view"
        if view["follow_seq"] > seq:
            return None, "view_moved"
        if view["follow_seq"] < seq:
            return None, "stale_view"
        if record.get("observed_at") is not None and float(view.get("moved_at", view["at"])) > record["observed_at"]:
            rec_epoch = record.get("lock_epoch")
            prior_seq = record.get("prior_view_seq")
            same_lock_catchup = (
                rec_epoch is not None
                and view_epoch == rec_epoch
                and rec_epoch == epoch
                and (prior_seq is None or prior_seq < seq)
            )
            if not same_lock_catchup:
                return None, "view_after_press"
        if not view["locked"]:
            return None, "unlocked"
        if view.get("target_px") is None or view.get("target_corners_px") is None:
            return None, "target_unavailable"
        scope = self._laser_scope(context)
        with self._lock:
            self._pending_laser_scope = scope
            self._pending_laser_epoch = epoch
            self._pending_laser_context_epoch = context_epoch
        query = dict(urllib.parse.parse_qsl(context.get("query", "").lstrip("?")))
        return {
            **{key: copy.deepcopy(view[key]) for key in TARGET_KEYS},
            "g": query.get("g", ""),
            "t": int(query["t"]) if "t" in query else None,
            "p": int(query["p"]) if "p" in query else None,
            "z": int(query["z"]) if "z" in query else None,
            "press": record["press"],
            "receiver_id": record["receiver_id"],
            "at": record["at"],
            "slacs_session": record.get("slacs_session"),
            "pixel_size_um": context["pixel_size_um"],
        }, None

    def _deliver_laser_box(self, payload):
        if self._on_laser_box is None:
            return False
        with self._lock:
            if (
                self._closed
                or self._stage_follow is None
                or not self._locked
                or payload.get("receiver_id") != self._stage_follow_receiver_id
                or (
                    self._pending_laser_epoch is not None
                    and self._pending_laser_epoch != self._epoch
                )
                or (
                    self._pending_laser_context_epoch is not None
                    and self._pending_laser_context_epoch != self._context_epoch
                )
                or (
                    self._pending_laser_scope is not None
                    and self._pending_laser_scope != self._laser_scope(self._context)
                )
            ):
                return False
        return self._on_laser_box(payload)

    @staticmethod
    def _laser_scope(context: dict) -> tuple:
        return laser_scope(context)

    def _confirm_laser_box(self, outcome):
        if self._closed or self._window is None:
            return False
        item = outcome.get("item")
        with self._lock:
            pending_context_epoch = self._pending_laser_context_epoch
            pending_scope = self._pending_laser_scope
            self._pending_laser_epoch = None
            self._pending_laser_context_epoch = None
            self._pending_laser_scope = None
            if (
                self._closed
                or self._window is None
                or (
                    pending_context_epoch is not None
                    and pending_context_epoch != self._context_epoch
                )
                or (
                    pending_scope is not None
                    and pending_scope != self._laser_scope(self._context)
                )
            ):
                return False
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                self._laser_items[item["id"]] = copy.deepcopy(item)
        script = (
            "window.nd2TranslucentLaserItem?.("
            + json.dumps(outcome["item"], allow_nan=False)
            + "); true"
        )
        return self._window.evaluate_js(script)

    def _report_slacs_laser_box(self, payload):
        return (
            self._laser_delivery.accept(payload)
            if self._laser_delivery is not None
            else {"ok": False}
        )

    def _mark_context(self):
        """This overlay's state for a SLACS "," or "." record, or None once closed; never raises.

        Runs on the Raw Input thread inside the key follower. Like ``_slacs_pid``
        it never takes the overlay lock, because ``start_stage_follow`` holds it
        across the follower's start and stop. References are read one by one and
        may straddle a concurrent start or stop, which is acceptable for a log
        record; the receiver and follower snapshots take only their own locks.
        """
        try:
            if self._closed:
                return None
            receiver, follower = self._stage_follow, self._stage_follower
            receiver_id, view = self._stage_follow_receiver_id, self._overlay_view
            flip_x, flip_y = self._follow_flips
            context = dict.fromkeys(MARK_CONTEXT_KEYS)
            context.update(
                slacs_session=self._stage_follow_session,
                image_name=self._context.get("image_name"),
                source_format=self._source_format,
                locked=self._locked,
                flip_x=flip_x,
                flip_y=flip_y,
            )
            if view is not None:
                context.update({key: copy.deepcopy(view.get(key)) for key in TARGET_KEYS})
                if context["target_px"] is not None:
                    context["target_px"] = [round(v, 1) + 0.0 for v in context["target_px"]]
                center_x, center_y = view["center_px"]
                context.update(
                    image_px=[round(center_x, 1) + 0.0, round(center_y, 1) + 0.0],
                    view={
                        key: view[key]
                        for key in ("zoom", "rotation", "flip", "container", "window")
                    },
                    view_age_ms=max(0, int(round((time.monotonic() - view["at"]) * 1000))),
                )
                if receiver_id is not None and view["follow_session"] == receiver_id:
                    context.update(
                        view_follow_seq=view["follow_seq"],
                        view_displacement_um=view["follow_displacement_um"],
                    )
            if receiver is not None:
                snapshot = receiver.snapshot()
                context.update(
                    follow_status=snapshot.get("status"),
                    follow_seq=snapshot.get("seq"),
                    displacement_um=snapshot.get("displacement_um"),
                )
            else:
                context["follow_status"] = "lost" if self._stage_follow_lost else "off"
            if follower is not None:
                keys = follower.snapshot()
                context.update({
                    key: keys.get(key)
                    for key in (
                        "counted",
                        "step_source",
                        "step_um",
                        "roughness_um",
                        "mirror_h",
                        "mirror_v",
                        "reading_age_ms",
                        "slacs_busy",
                    )
                })
                context["effective_step_um"] = (
                    keys.get("roughness_um")
                    if keys.get("step_source") == "slacs"
                    and keys.get("controls_ok") is True
                    and type(keys.get("roughness_um")) in (int, float)
                    and keys["roughness_um"] > 0
                    else keys.get("step_um")
                )
            return context
        except Exception as exc:  # noqa: BLE001 - a log record must never break the key observer
            return {"context_error": type(exc).__name__}

    def stage_follow_state(self):
        with self._lock:
            receiver = self._stage_follow
            follower = self._stage_follower
            source = self._stage_follow_source
            if self._closed or receiver is None:
                if not self._closed and self._stage_follow_lost:
                    return {
                        "enabled": False,
                        "status": "paused",
                        "message": self._stage_follow_lost,
                    }
                return {"enabled": False, "status": "off", "message": "Stage follow is off."}
            snapshot = receiver.snapshot()
            if follower is not None:
                snapshot = {**snapshot, "keys": follower.snapshot()}
            status = snapshot.get("status")
            changed = status != self._stage_follow_status
            self._stage_follow_status = status
        if changed:
            session_log.event("follow_state", source=source, status=status)
        if self._laser_delivery is not None:
            snapshot = {**snapshot, "laser_delivery": self._laser_delivery.status}
        return snapshot

    def stop_stage_follow(self):
        with self._lock:
            receiver, self._stage_follow = self._stage_follow, None
            follower, self._stage_follower = self._stage_follower, None
            source, self._stage_follow_source = self._stage_follow_source, None
            self._stage_follow_status = None
            self._stage_follow_session = None
            self._stage_follow_receiver_id = None
            self._stage_follow_lost = ""
        if self._laser_delivery is not None:
            self._laser_delivery.view_changed()
        if follower is not None:
            follower.stop("stopped")
        if receiver is not None:
            receiver.close()
            session_log.event("follow_stop", source=source)
        return {"ok": True}

    def log_client_event(self, payload):
        """Record a rate-limited overlay-page diagnostic in the session log."""
        with self._lock:
            if self._closed:
                return {"ok": False}
            now = time.monotonic()
            if (
                self._client_log_window_start is None
                or now - self._client_log_window_start > CLIENT_LOG_WINDOW
            ):
                self._client_log_window_start = now
                self._client_log_count = 0
            if self._client_log_count >= CLIENT_LOG_MAX:
                return {"ok": True, "rate_limited": True}
            self._client_log_count += 1
            slide_url = self._context.get("slide_url")
            page_url = self._page_url
        if not isinstance(payload, dict):
            return {"ok": False}
        fields = format_client_log_fields(
            payload,
            slide_url=slide_url,
            page_url=page_url,
            field_limit=CLIENT_LOG_FIELD_LIMIT,
            stack_limit=CLIENT_LOG_STACK_LIMIT,
        )
        session_log.event("client_error", **fields)
        return {"ok": True}

    def save_stage_follow_connection(self):
        """Export only an explicitly chosen local bridge connection file."""
        import webview

        with self._lock:
            receiver = self._stage_follow
            if self._closed or receiver is None:
                return {"ok": False, "message": "Enable stage follow first."}
            descriptor = {
                **receiver.start(),
                "version": 1,
                "image_name": self._context.get("image_name", "image"),
                "dimensions": self._context.get("dimensions"),
                "pixel_size_um_yx": self._context.get("pixel_size_um"),
                "motion_source": "commanded",
                "units": "um",
            }
        selected = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename="stage-follow-connection.json",
            file_types=("Stage follow connection (*.json)",),
        )
        if not selected:
            return {"ok": True, "cancelled": True}
        target = Path(selected if isinstance(selected, str) else selected[0])
        temporary = None
        try:
            with self._lock:
                if self._closed or self._stage_follow is not receiver:
                    return {
                        "ok": False,
                        "message": "The motion connection changed. Save it again.",
                    }
                fd, temporary = tempfile.mkstemp(prefix=".stage-follow-", dir=target.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(descriptor, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
                os.replace(temporary, target)
            return {"ok": True}
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "message": "Could not save the motion connection: " + str(exc),
            }
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
