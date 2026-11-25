"""Unit tests for Pillar 3 (W/A/S/D focus/click-through fix) and translucent facade split."""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import (
    stage_follow as stage_follow_mod,
)
from nd2wsi import (
    stage_keys as stage_keys_mod,
)
from nd2wsi import (
    translucent_stage_follow as tsf,
)
from nd2wsi import (
    translucent_validation as tv,
)
from nd2wsi import (
    translucent_window as tw,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeIntPtr64:
    """Simulates a 64-bit .NET System.IntPtr whose ToInt32() overflows."""

    def __init__(self, value: int):
        self._value = int(value)

    def ToInt64(self) -> int:
        return self._value

    def ToInt32(self) -> int:
        raise OverflowError("64-bit HWND overflows 32-bit IntPtr.ToInt32()")


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self, *args):
        for fn in list(self.handlers):
            fn(*args)

    def is_set(self):
        return False


class FakeWin32Overlay:
    """In-memory Win32 user32 harness for deterministic off-Windows testing."""

    def __init__(
        self,
        *,
        overlay_hwnd: int = 0x00007FF812340001,
        slacs_hwnd: int = 0x00007FF812340002,
        slacs_pid: int = 5150,
        dpi: int = 96,
        initial_exstyle: int = 0,
    ):
        self.overlay_hwnd = overlay_hwnd
        self.slacs_hwnd = slacs_hwnd
        self.slacs_pid = slacs_pid
        self.dpi = dpi
        self.styles: dict[int, int] = {overlay_hwnd: initial_exstyle, slacs_hwnd: 0}
        self.pids: dict[int, int] = {overlay_hwnd: os.getpid(), slacs_hwnd: slacs_pid}
        self.alive: set[int] = {overlay_hwnd, slacs_hwnd}
        self.visible: set[int] = {overlay_hwnd, slacs_hwnd}
        self.enum_order: list[int] = [overlay_hwnd, slacs_hwnd]
        self.foreground_hwnd: int = os.getpid() and overlay_hwnd
        self.cursor_screen: tuple[int, int] = (0, 0)
        self.client_origin: tuple[int, int] = (0, 0)
        self.cursor_pos_ok: bool = True
        self.screen_to_client_ok: bool = True

        self.calls: list[tuple] = []
        self.show_window_calls: list[tuple[int, int]] = []
        self.set_window_pos_calls: list[tuple] = []
        self.set_foreground_calls: list[int] = []
        self.set_exstyle_calls: list[tuple[int, int, int]] = []

    def build_api(self):
        harness = self

        def get_long_ptr(hwnd, index):
            assert index == tsf.GWL_EXSTYLE
            return harness.styles.get(int(hwnd), 0)

        def set_long_ptr(hwnd, index, value):
            assert index == tsf.GWL_EXSTYLE
            prev = harness.styles.get(int(hwnd), 0)
            harness.styles[int(hwnd)] = int(value)
            harness.set_exstyle_calls.append((int(hwnd), index, int(value)))
            harness.calls.append(("SetWindowLongPtrW", int(hwnd), index, int(value)))
            return prev

        def show_window(hwnd, cmd):
            harness.show_window_calls.append((int(hwnd), int(cmd)))
            harness.calls.append(("ShowWindow", int(hwnd), int(cmd)))
            return 1

        def set_window_pos(hwnd, insert_after, x, y, cx, cy, flags):
            entry = (int(hwnd), int(insert_after), int(x), int(y), int(cx), int(cy), int(flags))
            harness.set_window_pos_calls.append(entry)
            harness.calls.append(("SetWindowPos", *entry))
            return 1

        def get_foreground_window():
            return harness.foreground_hwnd

        def set_foreground_window(hwnd):
            harness.set_foreground_calls.append(int(hwnd))
            harness.calls.append(("SetForegroundWindow", int(hwnd)))
            harness.foreground_hwnd = int(hwnd)
            return 1

        def get_window_thread_process_id(hwnd, pid_ptr):
            pid = harness.pids.get(int(hwnd), 0)
            if not pid:
                return 0
            ctypes.cast(pid_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = pid
            return 1

        def is_window(hwnd):
            return 1 if int(hwnd) in harness.alive else 0

        def is_window_visible(hwnd):
            return 1 if int(hwnd) in harness.visible else 0

        def enum_windows(callback, lparam):
            for hwnd in list(harness.enum_order):
                if not callback(hwnd, lparam):
                    break
            return 1

        def get_cursor_pos(pt_ptr):
            if not harness.cursor_pos_ok:
                return 0
            pt = ctypes.cast(pt_ptr, ctypes.POINTER(wintypes.POINT)).contents
            pt.x, pt.y = harness.cursor_screen
            return 1

        def screen_to_client(hwnd, pt_ptr):
            if not harness.screen_to_client_ok:
                return 0
            pt = ctypes.cast(pt_ptr, ctypes.POINTER(wintypes.POINT)).contents
            ox, oy = harness.client_origin
            pt.x -= ox
            pt.y -= oy
            return 1

        def get_dpi_for_window(hwnd):
            return harness.dpi

        user32 = SimpleNamespace(
            ShowWindow=show_window,
            SetWindowPos=set_window_pos,
            GetForegroundWindow=get_foreground_window,
            SetForegroundWindow=set_foreground_window,
            GetWindowThreadProcessId=get_window_thread_process_id,
            IsWindow=is_window,
            IsWindowVisible=is_window_visible,
            EnumWindows=enum_windows,
            GetCursorPos=get_cursor_pos,
            ScreenToClient=screen_to_client,
        )
        return SimpleNamespace(
            ctypes=ctypes,
            wintypes=wintypes,
            user32=user32,
            get_window_long_ptr=get_long_ptr,
            set_window_long_ptr=set_long_ptr,
            get_dpi_for_window=get_dpi_for_window,
            WNDENUMPROC=lambda fn: fn,
        )


class _RecordingNative:
    """WinForms BrowserForm stand-in tracking Opacity changes and Handle recreation."""

    def __init__(self, handle, harness: FakeWin32Overlay, *, scale: float = 1.0, opacity: float = 0.55):
        self.Handle = handle
        self.InvokeRequired = False
        self._scale = scale
        self._opacity = opacity
        self._harness = harness
        self._show_in_taskbar = True
        self.TopMost = False
        self.Left = 100
        self.Top = 100
        self.Width = 640
        self.Height = 480
        self.Show = Mock(side_effect=lambda: harness.calls.append(("native.Show",)))

    @property
    def Opacity(self):
        return self._opacity

    @Opacity.setter
    def Opacity(self, value):
        self._opacity = float(value)
        self._harness.calls.append(("native.Opacity", self._opacity))
        # Simulate the real WinForms trap: Opacity == 1.0 strips WS_EX_LAYERED.
        hwnd = tw._hwnd_from_native(self)
        if hwnd is not None:
            if round(self._opacity * 255) >= 255:
                self._harness.styles[hwnd] = self._harness.styles.get(hwnd, 0) & ~tsf.WS_EX_LAYERED
            else:
                self._harness.styles[hwnd] = self._harness.styles.get(hwnd, 0) | tsf.WS_EX_LAYERED

    @property
    def ShowInTaskbar(self):
        return self._show_in_taskbar

    @ShowInTaskbar.setter
    def ShowInTaskbar(self, value):
        self._show_in_taskbar = bool(value)
        # Simulate WinForms handle recreation dropping pre-existing WS_EX_TOOLWINDOW.
        hwnd = tw._hwnd_from_native(self)
        if hwnd is not None:
            self._harness.styles[hwnd] = self._harness.styles.get(hwnd, 0) & ~tsf.WS_EX_TOOLWINDOW


@pytest.fixture
def win32_env(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    harness = FakeWin32Overlay()
    monkeypatch.setattr(tw, "_win32_overlay_api", harness.build_api)

    def make_overlay(
        *,
        opacity: float = tw.OPACITY,
        handle=None,
        slacs_pid: int | None = None,
        scale: float = 1.0,
    ):
        resolved_handle = _FakeIntPtr64(harness.overlay_hwnd) if handle is None else handle
        native = _RecordingNative(resolved_handle, harness, scale=scale, opacity=opacity)
        window = SimpleNamespace(
            native=native,
            destroy=Mock(),
            load_url=Mock(),
            show=Mock(side_effect=lambda: harness.calls.append(("window.show",))),
            evaluate_js=Mock(return_value=True),
            events=SimpleNamespace(
                before_show=_FakeEvent(),
                loaded=_FakeEvent(),
                closed=_FakeEvent(),
            ),
        )
        ctx = {
            "version": 1,
            "slide_url": "http://127.0.0.1:8123/tok/s/1/",
            "query": "?g=1",
            "pixel_size_um": [0.5, 0.5],
            "dimensions": [2048, 2048],
            "image_name": "slide.nd2",
            "opacity": opacity,
        }
        api = tw.TranslucentApi(ctx, logger=Mock(), on_closed=Mock())
        api._window = window
        api._page_url = "http://127.0.0.1:8123/tok/static/translucent.html"
        api._round_windows_region = Mock()
        if slacs_pid is not None:
            api._discovery = SimpleNamespace(
                slacs_pid=lambda: slacs_pid,
                slacs_creation_filetime=lambda: None,
                stop=Mock(),
            )
            api._slacs_snapshot = {"available": True, "session_id": "slacs-1", "status": "running"}
        return api, native, window

    return harness, make_overlay


# ---------------------------------------------------------------------------
# 1. 64-bit Win32 helpers & WS_EX_TOOLWINDOW on before_show + _configure_and_show
# ---------------------------------------------------------------------------


def test_64bit_hwnd_uses_to_int64_never_to_int32_and_32bit_fallback(monkeypatch):
    ptr = _FakeIntPtr64(0x00007FF89ABCDEF0)
    assert tw._hwnd_from_native(SimpleNamespace(Handle=ptr)) == 0x00007FF89ABCDEF0
    assert tw._hwnd_from_native(SimpleNamespace(Handle=0x123456789)) == 0x123456789
    assert tw._hwnd_from_native(SimpleNamespace(Handle=True)) is None
    assert tw._hwnd_from_native(SimpleNamespace(Handle=0)) is None
    assert tw._hwnd_from_native(Mock()) is None
    assert tw._hwnd_from_native(None) is None

    # Verify 32-bit fallback when GetWindowLongPtrW / SetWindowLongPtrW are absent.
    tsf._win32_overlay_api.cache_clear()

    class _FakeFunc:
        restype = None
        argtypes = None

    class _FakeUser32_32bit:
        def __init__(self, name, use_last_error=False):
            assert name == "user32"
            assert use_last_error is True
            for fn_name in (
                "GetWindowLongW",
                "SetWindowLongW",
                "ShowWindow",
                "SetWindowPos",
                "GetForegroundWindow",
                "SetForegroundWindow",
                "GetWindowThreadProcessId",
                "IsWindow",
                "IsWindowVisible",
                "EnumWindows",
                "GetCursorPos",
                "ScreenToClient",
            ):
                setattr(self, fn_name, _FakeFunc())

    monkeypatch.setattr(ctypes, "WinDLL", _FakeUser32_32bit, raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *a: object(), raising=False)
    try:
        loaded = tsf._win32_overlay_api()
        assert loaded.get_window_long_ptr is loaded.user32.GetWindowLongW
        assert loaded.set_window_long_ptr is loaded.user32.SetWindowLongW
        assert loaded.get_dpi_for_window is None
    finally:
        tsf._win32_overlay_api.cache_clear()


def test_ws_ex_toolwindow_applied_on_before_show_and_configure_and_show(win32_env, monkeypatch):
    harness, make_overlay = win32_env
    # Pretend WinForms started with WS_EX_APPWINDOW set.
    harness.styles[harness.overlay_hwnd] = tw.WS_EX_APPWINDOW
    api, native, window = make_overlay()
    harness.foreground_hwnd = harness.overlay_hwnd  # SLACS is not foreground

    try:
        # 1. before_show sets WS_EX_TOOLWINDOW and strips WS_EX_APPWINDOW.
        api._on_before_show()
        style_after_before_show = harness.styles[harness.overlay_hwnd]
        assert style_after_before_show & tw.WS_EX_TOOLWINDOW
        assert not (style_after_before_show & tw.WS_EX_APPWINDOW)

        # 2. _configure_and_show sets ShowInTaskbar=False (which drops TOOLWINDOW in our
        #    _RecordingNative simulation) and MUST re-apply WS_EX_TOOLWINDOW afterward!
        api._configure_and_show()
        style_after_configure = harness.styles[harness.overlay_hwnd]
        assert style_after_configure & tw.WS_EX_TOOLWINDOW
        assert not (style_after_configure & tw.WS_EX_APPWINDOW)
        assert native.ShowInTaskbar is False
        native.Show.assert_called_once()
    finally:
        api.close_translucent()

    # 3. create_translucent_window wires window.events.before_show when present.
    before_show_event = _FakeEvent()
    fake_win = SimpleNamespace(
        native=native,
        events=SimpleNamespace(
            before_show=before_show_event,
            loaded=_FakeEvent(),
            closed=_FakeEvent(),
        ),
        destroy=Mock(),
    )
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=Mock(return_value=fake_win)))
    harness.styles[harness.overlay_hwnd] = 0
    created = tw.create_translucent_window(
        {}, url="http://127.0.0.1:9/static/translucent.html", title="Tracing", logger=Mock(), on_closed=Mock()
    )
    try:
        assert created._on_before_show in before_show_event.handlers
        before_show_event.fire()
        assert harness.styles[harness.overlay_hwnd] & tw.WS_EX_TOOLWINDOW
    finally:
        created._did_close()


# ---------------------------------------------------------------------------
# 2. Locked click-through styles + WinForms 100% opacity trap + unlock restore
# ---------------------------------------------------------------------------


def test_lock_applies_clickthrough_and_caps_full_opacity_and_unlock_restores(win32_env):
    harness, make_overlay = win32_env
    api, native, _window = make_overlay(opacity=1.0)
    harness.foreground_hwnd = 999999  # unrelated app foreground -> no focus yield

    try:
        assert api.set_translucent_opacity(1.0) == {"ok": True, "opacity": 1.0}
        assert native.Opacity == 1.0
        # WinForms stripped WS_EX_LAYERED when Opacity == 1.0!
        assert not (harness.styles[harness.overlay_hwnd] & tw.WS_EX_LAYERED)
        harness.calls.clear()

        locked = api.set_translucent_locked(True)
        assert locked == {"ok": True, "locked": True}
        # Opacity is capped <= 254/255 on the native form while logical context stays 1.0!
        assert native.Opacity <= tw.LOCKED_MAX_OPACITY
        assert native.Opacity == pytest.approx(254.0 / 255.0)
        assert api.translucent_context()["opacity"] == 1.0

        # All four click-through bits MUST be set, including WS_EX_LAYERED!
        style = harness.styles[harness.overlay_hwnd]
        expected_bits = tw.WS_EX_LAYERED | tw.WS_EX_TRANSPARENT | tw.WS_EX_NOACTIVATE | tw.WS_EX_TOOLWINDOW
        assert (style & expected_bits) == expected_bits

        # Opacity cap MUST happen before SetWindowLongPtrW so WinForms doesn't wipe WS_EX_LAYERED.
        op_names = [c[0] for c in harness.calls]
        assert op_names.index("native.Opacity") < op_names.index("SetWindowLongPtrW")

        # Hover timer thread is active while locked.
        hover_thread = api._win32_focus._hover_thread
        assert hover_thread is not None and hover_thread.is_alive()

        # Unlock stops timer, clears WS_EX_TRANSPARENT & WS_EX_NOACTIVATE, keeps WS_EX_TOOLWINDOW,
        # and restores exact full opacity 1.0.
        unlocked = api.set_translucent_locked(False)
        assert unlocked == {"ok": True, "locked": False}
        assert api._win32_focus._hover_thread is None
        assert not hover_thread.is_alive()
        style_after = harness.styles[harness.overlay_hwnd]
        assert not (style_after & tw.WS_EX_TRANSPARENT)
        assert not (style_after & tw.WS_EX_NOACTIVATE)
        assert style_after & tw.WS_EX_TOOLWINDOW
        assert native.Opacity == 1.0
        assert api.translucent_context()["opacity"] == 1.0
    finally:
        api.close_translucent()


def test_lock_preserves_partial_opacity_and_close_stops_hover_timer(win32_env):
    harness, make_overlay = win32_env
    api, native, _window = make_overlay(opacity=0.55)
    harness.foreground_hwnd = 999999

    assert api.set_translucent_locked(True)["ok"]
    assert native.Opacity == pytest.approx(0.55)
    assert api._win32_focus._opacity_capped is False
    hover_thread = api._win32_focus._hover_thread
    assert hover_thread is not None and hover_thread.is_alive()

    api.close_translucent()
    assert api._win32_focus._hover_thread is None
    assert not hover_thread.is_alive()


# ---------------------------------------------------------------------------
# 3. set_locked_interactive_rects validation + hover-gated click-through
# ---------------------------------------------------------------------------


def test_set_locked_interactive_rects_validation_and_normalization(win32_env):
    _harness, make_overlay = win32_env
    api, _native, _window = make_overlay()
    try:
        res = api.set_locked_interactive_rects([
            {"x": 10, "y": 12.5, "width": 80, "height": 28, "top": 12.5, "right": 90},
            {"x": 0, "y": 0, "width": 0, "height": 20},  # zero-width omitted
            {"x": 120, "y": 8, "width": 44, "height": 24},
        ])
        assert res == {"ok": True, "count": 2}
        assert api._win32_focus.interactive_rects() == [
            {"x": 10.0, "y": 12.5, "width": 80.0, "height": 28.0},
            {"x": 120.0, "y": 8.0, "width": 44.0, "height": 24.0},
        ]
        assert api.set_locked_interactive_rects([]) == {"ok": True, "count": 0}

        for bad in (
            None,
            "rects",
            {"x": 0, "y": 0, "width": 10, "height": 10},
            ["not-a-dict"],
            [{"x": 0, "y": 0, "width": -1, "height": 10}],
            [{"x": 0, "y": 0, "width": 10, "height": float("nan")}],
            [{"x": True, "y": 0, "width": 10, "height": 10}],
            [{"x": 0, "y": 0, "width": 10}],
            [{"x": 0, "y": 0, "width": 10, "height": 10}] * (tw.MAX_LOCKED_INTERACTIVE_RECTS + 1),
        ):
            rejected = api.set_locked_interactive_rects(bad)
            assert rejected["ok"] is False and rejected["message"]
    finally:
        api.close_translucent()

    assert api.set_locked_interactive_rects([])["ok"] is False


def test_hover_gate_toggles_ws_ex_transparent_with_dpi_and_padding(win32_env):
    harness, make_overlay = win32_env
    harness.dpi = 144  # 1.5x Windows display scaling
    harness.client_origin = (100, 200)
    api, _native, _window = make_overlay(opacity=1.0)
    # Slow timer interval so we drive poll_hover_once deterministically without races.
    api._win32_focus._poll_interval = 60.0
    harness.foreground_hwnd = 999999

    try:
        assert api.set_locked_interactive_rects([
            {"x": 20, "y": 10, "width": 60, "height": 30},  # CSS x=[20..80], y=[10..40]
        ]) == {"ok": True, "count": 1}
        assert api.set_translucent_locked(True)["ok"]
        assert harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT

        # 1. Cursor in client image area (CSS 200, 200 -> physical +300, +300) -> click-through ON.
        harness.cursor_screen = (100 + 300, 200 + 300)
        assert api._win32_focus.poll_hover_once() is False
        assert harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT

        # 2. Cursor enters Lock button (CSS 50, 25 -> physical +75, +37.5 -> 175, 238) -> click-through OFF.
        harness.cursor_screen = (175, 238)
        assert api._win32_focus.poll_hover_once() is True
        style = harness.styles[harness.overlay_hwnd]
        assert not (style & tw.WS_EX_TRANSPARENT)
        assert (style & (tw.WS_EX_LAYERED | tw.WS_EX_NOACTIVATE | tw.WS_EX_TOOLWINDOW)) == (
            tw.WS_EX_LAYERED | tw.WS_EX_NOACTIVATE | tw.WS_EX_TOOLWINDOW
        )

        # 3. Cursor inside 3 px comfort padding (CSS x=18, y=8 -> physical +27, +12) -> still clickable.
        harness.cursor_screen = (100 + 27, 200 + 12)
        assert api._win32_focus.poll_hover_once() is True
        assert not (harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT)

        # 4. Cursor leaves beyond 3 px comfort padding (CSS x=16, y=25 -> physical +24, +38) -> click-through ON.
        harness.cursor_screen = (100 + 24, 200 + 38)
        assert api._win32_focus.poll_hover_once() is False
        assert harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT

        # 5. Live 30 ms timer polls automatically when cursor enters and leaves a rect.
        api.set_translucent_locked(False)
        api._win32_focus._poll_interval = 0.010
        harness.cursor_screen = (175, 238)  # inside rect
        api.set_translucent_locked(True)
        deadline = time.monotonic() + 1.0
        while (harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not (harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT)

        harness.cursor_screen = (100 + 300, 200 + 300)  # outside rect
        deadline = time.monotonic() + 1.0
        while not (harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT
    finally:
        api.close_translucent()


# ---------------------------------------------------------------------------
# 4. _yield_focus_to_slacs() gating and invocation sites
# ---------------------------------------------------------------------------


def test_yield_focus_to_slacs_only_when_own_process_is_foreground(win32_env):
    harness, make_overlay = win32_env
    api, _native, _window = make_overlay(slacs_pid=harness.slacs_pid)

    try:
        # 1. Foreground is another app (e.g. Notepad, pid 7777) -> NEVER steal focus.
        other_hwnd = 0x00007FF899990001
        harness.pids[other_hwnd] = 7777
        harness.alive.add(other_hwnd)
        harness.foreground_hwnd = other_hwnd
        assert api._yield_focus_to_slacs() is False
        assert harness.set_foreground_calls == []

        # 2. Foreground is already SLACS -> NEVER re-call SetForegroundWindow.
        harness.foreground_hwnd = harness.slacs_hwnd
        assert api._yield_focus_to_slacs() is False
        assert harness.set_foreground_calls == []

        # 3. Foreground belongs to os.getpid() -> discovers SLACS HWND via EnumWindows and yields.
        harness.foreground_hwnd = harness.overlay_hwnd
        assert api._yield_focus_to_slacs() is True
        assert harness.set_foreground_calls == [harness.slacs_hwnd]
        assert api._slacs_hwnd == harness.slacs_hwnd

        # 4. Dead SLACS HWND -> does NOT call SetForegroundWindow.
        harness.set_foreground_calls.clear()
        harness.alive.discard(harness.slacs_hwnd)
        harness.foreground_hwnd = harness.overlay_hwnd
        assert api._yield_focus_to_slacs() is False
        assert harness.set_foreground_calls == []

        # 5. SLACS session change clears cached _slacs_hwnd.
        harness.alive.add(harness.slacs_hwnd)
        api._slacs_hwnd = harness.slacs_hwnd
        api._on_slacs_change({"available": False, "session_id": None, "status": "not_running"})
        assert api._slacs_hwnd is None
    finally:
        api.close_translucent()


def test_yield_focus_called_on_lock_start_follow_and_flip_toggle(win32_env, monkeypatch):
    harness, make_overlay = win32_env

    class _FakeReceiver:
        def start(self):
            return {"url": "http://127.0.0.1:9/", "token": "t", "session_id": "rec-1", "protocol": 1}

        def snapshot(self):
            return {"enabled": True, "session_id": "rec-1", "seq": 0, "displacement_um": [0, 0], "status": "waiting"}

        def close(self):
            pass

    class _FakeFollower:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"running": True}

        def set_flips(self, fx, fy):
            pass

        def snapshot(self):
            return {"running": True}

        def stop(self, reason="stopped"):
            pass

    monkeypatch.setattr(stage_follow_mod, "StageFollowReceiver", _FakeReceiver)
    monkeypatch.setattr(stage_keys_mod, "SlacsKeyFollower", _FakeFollower)
    monkeypatch.setattr(tw.session_log, "event", lambda *a, **kw: None)

    api, _native, _window = make_overlay(slacs_pid=harness.slacs_pid)
    try:
        # 1. set_translucent_locked(True) yields focus; unlock does NOT.
        harness.foreground_hwnd = harness.overlay_hwnd
        assert api.set_translucent_locked(True)["ok"]
        assert harness.set_foreground_calls == [harness.slacs_hwnd]

        harness.foreground_hwnd = harness.overlay_hwnd
        harness.set_foreground_calls.clear()
        assert api.set_translucent_locked(False)["ok"]
        assert harness.set_foreground_calls == []

        # 2. start_stage_follow() yields focus on success.
        harness.foreground_hwnd = harness.overlay_hwnd
        harness.set_foreground_calls.clear()
        assert api.start_stage_follow({"source": "keys", "step_source": "manual", "step_um": 10.0})["ok"]
        assert harness.set_foreground_calls == [harness.slacs_hwnd]

        # 3. set_stage_follow_flips(...) yields focus on valid call, not on invalid payload.
        harness.foreground_hwnd = harness.overlay_hwnd
        harness.set_foreground_calls.clear()
        assert not api.set_stage_follow_flips({"flip_x": "bad", "flip_y": False})["ok"]
        assert harness.set_foreground_calls == []

        assert api.set_stage_follow_flips({"flip_x": True, "flip_y": False})["ok"]
        assert harness.set_foreground_calls == [harness.slacs_hwnd]
    finally:
        api.close_translucent()


def test_yield_focus_is_safe_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    api = tw.TranslucentApi({}, logger=Mock(), on_closed=Mock())
    assert api._yield_focus_to_slacs() is False
    api.close_translucent()


# ---------------------------------------------------------------------------
# 5. Non-activating _configure_and_show() and refresh_context()
# ---------------------------------------------------------------------------


def test_configure_and_show_uses_show_noactivate_when_slacs_is_foreground(win32_env):
    harness, make_overlay = win32_env
    api, native, _window = make_overlay(slacs_pid=harness.slacs_pid)
    harness.foreground_hwnd = harness.slacs_hwnd

    try:
        api._configure_and_show()
        native.Show.assert_not_called()
        assert harness.show_window_calls == [(harness.overlay_hwnd, tw.SW_SHOWNOACTIVATE)]
        expected_flags = tw.SWP_NOMOVE | tw.SWP_NOSIZE | tw.SWP_NOACTIVATE | tw.SWP_SHOWWINDOW
        assert harness.set_window_pos_calls == [
            (harness.overlay_hwnd, tw.HWND_TOPMOST, 0, 0, 0, 0, expected_flags)
        ]
        assert harness.styles[harness.overlay_hwnd] & tw.WS_EX_TOOLWINDOW
    finally:
        api.close_translucent()


def test_configure_and_show_uses_show_noactivate_when_already_locked(win32_env):
    harness, make_overlay = win32_env
    api, native, _window = make_overlay(opacity=1.0)
    harness.foreground_hwnd = harness.overlay_hwnd  # SLACS not foreground, but overlay is locked
    api._locked = True

    try:
        api._configure_and_show()
        native.Show.assert_not_called()
        assert harness.show_window_calls == [(harness.overlay_hwnd, tw.SW_SHOWNOACTIVATE)]
        assert native.Opacity == pytest.approx(254.0 / 255.0)
        assert (harness.styles[harness.overlay_hwnd] & tw.LOCKED_CLICKTHROUGH_EXSTYLE) == tw.LOCKED_CLICKTHROUGH_EXSTYLE
    finally:
        api.close_translucent()


def test_refresh_context_uses_show_noactivate_when_slacs_foreground_or_locked(win32_env):
    harness, make_overlay = win32_env
    api, native, window = make_overlay(opacity=1.0, slacs_pid=harness.slacs_pid)
    api._configured = True
    base_ctx = {
        "slide_url": "http://127.0.0.1:8123/tok/s/1/",
        "query": "?g=1",
        "rotation": 0,
        "flip": False,
        "pixel_size_um": [0.5, 0.5],
    }

    try:
        # 1. SLACS is foreground (overlay unlocked) -> SW_SHOWNOACTIVATE + SetWindowPos, NOT window.show().
        harness.foreground_hwnd = harness.slacs_hwnd
        api._refresh_context(base_ctx)
        window.load_url.assert_called_once()
        window.show.assert_not_called()
        assert harness.show_window_calls == [(harness.overlay_hwnd, tw.SW_SHOWNOACTIVATE)]
        expected_flags = tw.SWP_NOMOVE | tw.SWP_NOSIZE | tw.SWP_NOACTIVATE | tw.SWP_SHOWWINDOW
        assert harness.set_window_pos_calls == [
            (harness.overlay_hwnd, tw.HWND_TOPMOST, 0, 0, 0, 0, expected_flags)
        ]

        # 2. Overlay was locked at 100% opacity, another window foreground -> unlocks click-through,
        #    stops timer, restores opacity 1.0, AND refreshes without activating!
        harness.show_window_calls.clear()
        harness.set_window_pos_calls.clear()
        harness.foreground_hwnd = 999999
        assert api.set_translucent_locked(True)["ok"]
        assert native.Opacity == pytest.approx(254.0 / 255.0)
        hover_thread = api._win32_focus._hover_thread
        assert hover_thread is not None and hover_thread.is_alive()

        api._refresh_context(base_ctx)
        window.show.assert_not_called()
        assert harness.show_window_calls == [(harness.overlay_hwnd, tw.SW_SHOWNOACTIVATE)]
        assert not hover_thread.is_alive()
        assert native.Opacity == 1.0
        assert not (harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT)
        assert not (harness.styles[harness.overlay_hwnd] & tw.WS_EX_NOACTIVATE)
        assert harness.styles[harness.overlay_hwnd] & tw.WS_EX_TOOLWINDOW

        # 3. Neither SLACS foreground nor locked -> normal window.show().
        harness.show_window_calls.clear()
        harness.foreground_hwnd = harness.overlay_hwnd
        api._refresh_context(base_ctx)
        window.show.assert_called_once()
        assert harness.show_window_calls == []
    finally:
        api.close_translucent()


# ---------------------------------------------------------------------------
# 6. Facade re-exports & static architectural invariants
# ---------------------------------------------------------------------------


def test_hover_gate_dpi_fallback_and_destroyed_hwnd_stops_cleanly(win32_env, monkeypatch):
    harness, make_overlay = win32_env
    # Simulate pre-1607 Win10 without GetDpiForWindow: must fall back to native._scale.
    orig_build = harness.build_api

    def build_without_get_dpi():
        api_obj = orig_build()
        api_obj.get_dpi_for_window = None
        return api_obj

    monkeypatch.setattr(tw, "_win32_overlay_api", build_without_get_dpi)
    harness.client_origin = (0, 0)
    harness.foreground_hwnd = 999999
    api, _native, _window = make_overlay(opacity=0.6, scale=2.0)
    api._win32_focus._poll_interval = 60.0

    try:
        assert api.set_locked_interactive_rects([
            {"x": 10, "y": 10, "width": 40, "height": 20},  # CSS x=[10..50], y=[10..30]
        ]) == {"ok": True, "count": 1}
        assert api.set_translucent_locked(True)["ok"]
        # At 2.0x scale, physical (60, 40) -> CSS (30, 20), inside rect!
        harness.cursor_screen = (60, 40)
        assert api._win32_focus.poll_hover_once() is True
        assert not (harness.styles[harness.overlay_hwnd] & tw.WS_EX_TRANSPARENT)

        # Destroyed HWND causes poll_hover_once to bail cleanly and clear lock state.
        harness.alive.discard(harness.overlay_hwnd)
        assert api._win32_focus.poll_hover_once() is None
        assert api._win32_focus._locked is False
    finally:
        api.close_translucent()


def test_translucent_window_facade_reexports_and_no_tkinter_or_to_int32():
    import ast

    validation_symbols = (
        "OPACITY",
        "MIN_OPACITY",
        "OPACITY_TIMEOUT",
        "MIN_SIZE",
        "MAX_SIZE",
        "DEFAULT_STEP_UM",
        "MAX_STEP_UM",
        "CLIENT_LOG_MAX",
        "CLIENT_LOG_WINDOW",
        "CLIENT_LOG_FIELD_LIMIT",
        "CLIENT_LOG_STACK_LIMIT",
        "MAX_LOCKED_INTERACTIVE_RECTS",
        "TARGET_KEYS",
        "VIEW_KEYS",
        "MARK_CONTEXT_KEYS",
        "_HEX_COLOR_RE",
        "_number",
        "_optional_numbers",
        "_polygon_area",
        "validate_roi",
        "validate_laser_item",
        "validate_context",
        "validate_overlay_view",
        "validate_laser_target_request",
        "validate_stage_follow_options",
        "validate_follow_flips",
        "validate_locked_interactive_rects",
        "roi_pair_paths",
        "laser_scope",
    )
    for name in validation_symbols:
        assert getattr(tw, name) is getattr(tv, name), f"facade mismatch for {name}"

    stage_follow_symbols = (
        "GWL_EXSTYLE",
        "WS_EX_TRANSPARENT",
        "WS_EX_TOOLWINDOW",
        "WS_EX_APPWINDOW",
        "WS_EX_LAYERED",
        "WS_EX_NOACTIVATE",
        "LOCKED_CLICKTHROUGH_EXSTYLE",
        "SW_SHOWNOACTIVATE",
        "HWND_TOPMOST",
        "SWP_NOSIZE",
        "SWP_NOMOVE",
        "SWP_NOZORDER",
        "SWP_NOACTIVATE",
        "SWP_FRAMECHANGED",
        "SWP_SHOWWINDOW",
        "LOCKED_MAX_OPACITY",
        "HOVER_POLL_INTERVAL_S",
        "HOVER_RECT_PADDING_CSS",
        "_hwnd_from_native",
        "point_in_interactive_rects",
        "Win32OverlayFocusController",
        "StageFollowMixin",
    )
    for name in stage_follow_symbols:
        assert getattr(tw, name) is getattr(tsf, name), f"facade mismatch for {name}"

    for symbol in ("TranslucentApi", "create_translucent_window", "_OverlayDrag", "session_log", "sys", "threading"):
        assert hasattr(tw, symbol), f"missing facade symbol {symbol}"

    for rel in (
        "nd2wsi/translucent_window.py",
        "nd2wsi/translucent_validation.py",
        "nd2wsi/translucent_stage_follow.py",
    ):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "tkinter" not in src.lower(), f"{rel} must never import tkinter"
        tree = ast.parse(src, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "tkinter" not in alias.name.lower()
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert "tkinter" not in node.module.lower()
            elif isinstance(node, ast.Attribute):
                assert node.attr != "ToInt32", f"{rel} must never reference .ToInt32"
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "ToInt32", f"{rel} must never call ToInt32"
