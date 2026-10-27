"""Reading SLACS-like controls from a real Win32 window in another process.

This spawns a genuine Win32 program, named like the SLACS GUI, that builds the
controls SLACS's wxPython panel has on Windows: two trackbars with range 1..100,
the Roughness trackbar with range 1..1000, a "Roughness" label and the two
Mirror checkboxes. The program takes commands on stdin from a background thread
and posts them to its own window, so the test can move the slider, flip the
checkboxes and make the window procedure sleep. The reader must report the
values, report busy within its timeout while the window sleeps, and recover.
Nothing here is patched: real window enumeration, real SendMessageTimeoutW.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time

import pytest

from nd2wsi import session_log
from nd2wsi.slacs_controls import SlacsControlsMonitor, read_slacs_controls
from nd2wsi.slacs_identity import SCRIPT_NAME

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Win32 window controls")

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # A private instance with a prototype: never change calls other code shares.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM]

WM_CLOSE = 0x0010

# The SLACS stand-in. It prints "ready <pid> <hwnd>" once its window exists, and
# acknowledges each command from its window procedure (on the GUI thread).
_HARNESS = r'''
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

TWO_ROUGHNESS = "--two-roughness" in sys.argv[1:]

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
LRESULT = ctypes.c_ssize_t  # LONG_PTR: pointer-sized on 64-bit Windows
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                             wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class INITCOMMONCONTROLSEX(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("dwICC", wintypes.DWORD)]


def declare(function, restype, *argtypes):
    function.restype = restype
    function.argtypes = list(argtypes)


HWND, UINT, DWORD = wintypes.HWND, wintypes.UINT, wintypes.DWORD
WPARAM, LPARAM = wintypes.WPARAM, wintypes.LPARAM
declare(comctl32.InitCommonControlsEx, wintypes.BOOL, ctypes.POINTER(INITCOMMONCONTROLSEX))
declare(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
declare(user32.DefWindowProcW, LRESULT, HWND, UINT, WPARAM, LPARAM)
declare(user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
declare(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
declare(user32.CreateWindowExW, HWND, DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, wintypes.HMENU,
        wintypes.HINSTANCE, wintypes.LPVOID)
declare(user32.DestroyWindow, wintypes.BOOL, HWND)
declare(user32.IsWindow, wintypes.BOOL, HWND)
declare(user32.ShowWindow, wintypes.BOOL, HWND, ctypes.c_int)
declare(user32.SendMessageW, LRESULT, HWND, UINT, WPARAM, LPARAM)
declare(user32.PostMessageW, wintypes.BOOL, HWND, UINT, WPARAM, LPARAM)
declare(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), HWND, UINT, UINT)
declare(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
declare(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
declare(user32.PostQuitMessage, None, ctypes.c_int)

WM_DESTROY, WM_CLOSE, WM_APP = 0x0002, 0x0010, 0x8000
WM_APP_POS, WM_APP_HANG, WM_APP_MIRROR = WM_APP + 1, WM_APP + 2, WM_APP + 3
TBM_SETPOS, TBM_SETRANGEMIN, TBM_SETRANGEMAX = 0x0405, 0x0407, 0x0408
BM_SETCHECK, BST_CHECKED = 0x00F1, 1
TBS_AUTOTICKS, BS_AUTOCHECKBOX = 0x0001, 0x0003
WS_CHILD, WS_VISIBLE, WS_OVERLAPPEDWINDOW = 0x40000000, 0x10000000, 0x00CF0000
ICC_BAR_CLASSES, COLOR_BTNFACE, SW_SHOWNOACTIVATE = 0x0004, 15, 4
CLASS_NAME = "Nd2wsiSlacsControlsHarness"

state = {}


def say(*words):
    print(*words, flush=True)


def wnd_proc(hwnd, message, wparam, lparam):
    if hwnd and hwnd == state.get("frame"):
        if message == WM_APP_POS:
            user32.SendMessageW(state["roughness"], TBM_SETPOS, 1, lparam)
            say("pos", lparam)
            return 0
        if message == WM_APP_MIRROR:
            user32.SendMessageW(state["mirror_h"], BM_SETCHECK, wparam, 0)
            user32.SendMessageW(state["mirror_v"], BM_SETCHECK, lparam, 0)
            say("mirror", wparam, lparam)
            return 0
        if message == WM_APP_HANG:
            # Not pumping messages here is exactly what a busy SLACS looks like.
            say("hanging")
            time.sleep(wparam / 1000.0)
            say("awake")
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


WND_PROC = WNDPROC(wnd_proc)  # module-level, so the callback outlives the window


def child(parent, hinstance, class_name, text, style, x, y, width, height, ident):
    hwnd = user32.CreateWindowExW(0, class_name, text, WS_CHILD | WS_VISIBLE | style,
                                  x, y, width, height, parent, ident, hinstance, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


def trackbar(parent, hinstance, low, high, pos, y, ident):
    hwnd = child(parent, hinstance, "msctls_trackbar32", "", TBS_AUTOTICKS, 10, y, 240, 32, ident)
    user32.SendMessageW(hwnd, TBM_SETRANGEMIN, 0, low)
    user32.SendMessageW(hwnd, TBM_SETRANGEMAX, 1, high)
    user32.SendMessageW(hwnd, TBM_SETPOS, 1, pos)
    return hwnd


def commands(frame):
    """Read commands from stdin and post them to the window; never touch it directly."""
    try:
        for line in sys.stdin:
            words = line.split()
            if not words:
                continue
            if words[0] == "pos":
                user32.PostMessageW(frame, WM_APP_POS, 0, int(words[1]))
            elif words[0] == "mirror":
                user32.PostMessageW(frame, WM_APP_MIRROR, int(words[1]), int(words[2]))
            elif words[0] == "hang":
                user32.PostMessageW(frame, WM_APP_HANG, int(float(words[1]) * 1000), 0)
            elif words[0] == "quit":
                break
    finally:
        user32.PostMessageW(frame, WM_CLOSE, 0, 0)


def main():
    icc = INITCOMMONCONTROLSEX(ctypes.sizeof(INITCOMMONCONTROLSEX), ICC_BAR_CLASSES)
    if not comctl32.InitCommonControlsEx(ctypes.byref(icc)):
        raise RuntimeError("InitCommonControlsEx failed")
    hinstance = kernel32.GetModuleHandleW(None)
    window_class = WNDCLASSW(lpfnWndProc=WND_PROC, hInstance=hinstance,
                             hbrBackground=COLOR_BTNFACE + 1, lpszClassName=CLASS_NAME)
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise ctypes.WinError(ctypes.get_last_error())
    frame = None
    try:
        frame = user32.CreateWindowExW(0, CLASS_NAME, "SLACS controls test", WS_OVERLAPPEDWINDOW,
                                       140, 140, 560, 380, None, None, hinstance, None)
        if not frame:
            raise ctypes.WinError(ctypes.get_last_error())
        # A panel-like child, as wx.Panel is, holding every control.
        panel = child(frame, hinstance, CLASS_NAME, "", 0, 0, 0, 540, 340, 100)
        trackbar(panel, hinstance, 1, 100, 50, 10, 101)   # bfslider
        trackbar(panel, hinstance, 1, 100, 20, 50, 102)   # targetslider
        roughness = trackbar(panel, hinstance, 1, 1000, 10, 90, 103)
        if TWO_ROUGHNESS:
            trackbar(panel, hinstance, 1, 1000, 500, 130, 104)
        child(panel, hinstance, "Static", "Roughness", 0, 260, 96, 90, 20, 105)
        mirror_h = child(panel, hinstance, "Button", "Mirror Horizontal", BS_AUTOCHECKBOX,
                         10, 180, 200, 24, 106)
        mirror_v = child(panel, hinstance, "Button", "Mirror Vertical", BS_AUTOCHECKBOX,
                         10, 210, 200, 24, 107)
        user32.SendMessageW(mirror_h, BM_SETCHECK, BST_CHECKED, 0)
        user32.ShowWindow(frame, SW_SHOWNOACTIVATE)
        state.update(frame=frame, roughness=roughness, mirror_h=mirror_h, mirror_v=mirror_v)
        threading.Thread(target=commands, args=(frame,), daemon=True).start()
        say("ready", os.getpid(), frame)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # 0 = WM_QUIT, -1 = error
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        if frame and user32.IsWindow(frame):
            user32.DestroyWindow(frame)
        user32.UnregisterClassW(CLASS_NAME, hinstance)


main()
'''


class Harness:
    """The SLACS stand-in process, its output lines and its commands."""

    def __init__(self, tmp_path, *flags):
        script = tmp_path / SCRIPT_NAME
        script.write_text(_HARNESS, encoding="utf-8")
        self._stderr_path = tmp_path / "harness-stderr.txt"
        self._stderr = open(self._stderr_path, "w", encoding="utf-8")
        self.hwnd = None
        self.child = subprocess.Popen([sys.executable, str(script), *flags],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=self._stderr, text=True, bufsize=1)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        try:
            words = self.expect("ready", timeout=30).split()
            # The window's owner, not Popen.pid: a venv python.exe may be a
            # launcher that runs the base interpreter as a child process.
            self.pid, self.hwnd = int(words[1]), int(words[2])
        except BaseException:
            self.close()
            raise

    def _pump(self):
        try:
            for line in self.child.stdout:
                self._lines.put(line.strip())
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    def expect(self, word, timeout=10.0):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"the harness never said {word!r}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                self.fail(f"the harness exited before saying {word!r}")
            if line.split()[:1] == [word]:
                return line

    def fail(self, message):
        self._stderr.flush()
        try:
            stderr = self._stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stderr = ""
        pytest.fail(f"{message}; exit={self.child.poll()!r} stderr={stderr[-2000:]!r}")

    def command(self, text):
        self.child.stdin.write(text + "\n")
        self.child.stdin.flush()

    def close(self):
        child = self.child
        if child.poll() is None:
            try:
                self.command("quit")
                child.wait(timeout=8)  # a hang in progress finishes first
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if child.poll() is None and self.hwnd is not None:
            _user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)  # also ends a launcher's child
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._stderr.close()


def _wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_reads_real_controls_reports_a_hang_as_busy_and_recovers(tmp_path):
    harness = Harness(tmp_path)
    try:
        cache = {}
        reading = read_slacs_controls(harness.pid, cache=cache)
        assert reading["ok"] is True, reading
        assert reading["roughness_um"] == 10.0
        assert reading["mirror_h"] is True and reading["mirror_v"] is False
        assert reading["busy"] is False and reading["error"] is None
        assert cache["pid"] == harness.pid

        harness.command("pos 250")
        harness.expect("pos")
        assert read_slacs_controls(harness.pid, cache=cache)["roughness_um"] == 250.0
        fresh = read_slacs_controls(harness.pid)  # full discovery, no cache
        assert fresh["ok"] and fresh["roughness_um"] == 250.0, fresh

        harness.command("mirror 0 1")
        harness.expect("mirror")
        flipped = read_slacs_controls(harness.pid, cache=cache)
        assert flipped["ok"] and flipped["mirror_h"] is False and flipped["mirror_v"] is True

        harness.command("hang 3")
        harness.expect("hanging")
        started = time.monotonic()
        stuck = read_slacs_controls(harness.pid, cache=cache, timeout_ms=100)
        elapsed = time.monotonic() - started
        assert stuck["busy"] is True and stuck["ok"] is False, stuck
        assert stuck["roughness_um"] is None and stuck["error"]
        # One bounded timeout, not the three-second hang.
        assert elapsed < 1.0, (elapsed, stuck)
        assert stuck["latency_ms"] < 1000, stuck
        assert cache.get("pid") == harness.pid, "a busy read keeps the control handles"

        harness.expect("awake", timeout=10)
        recovered = read_slacs_controls(harness.pid, cache=cache)
        assert recovered["ok"] and not recovered["busy"], recovered
        assert recovered["roughness_um"] == 250.0 and recovered["mirror_v"] is True
    finally:
        harness.close()


def test_two_roughness_range_sliders_are_ambiguous(tmp_path):
    harness = Harness(tmp_path, "--two-roughness")
    try:
        reading = read_slacs_controls(harness.pid)
        assert reading["ok"] is False and reading["busy"] is False, reading
        assert reading["roughness_um"] is None and "More than one slider" in reading["error"]
    finally:
        harness.close()


def test_a_process_without_slacs_controls_is_unreadable():
    reading = read_slacs_controls(os.getpid())
    assert reading["ok"] is False and reading["roughness_um"] is None and reading["error"]


def test_monitor_follows_a_real_window_through_a_hang(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    harness = Harness(tmp_path)
    monitor = SlacsControlsMonitor(lambda: harness.pid, interval=0.05)
    try:
        assert monitor.start() is True
        assert _wait_until(lambda: monitor.latest()["roughness_um"] == 10.0, 5), monitor.latest()

        harness.command("pos 1")
        harness.expect("pos")
        assert _wait_until(lambda: monitor.latest()["roughness_um"] == 1.0, 5), monitor.latest()

        harness.command("hang 2")
        harness.expect("hanging")
        assert _wait_until(lambda: monitor.latest()["busy"], 1.5), monitor.latest()
        assert monitor.latest()["roughness_um"] == 1.0, "a busy SLACS keeps the last Roughness"

        harness.expect("awake", timeout=10)
        assert _wait_until(lambda: monitor.latest()["ok"] and not monitor.latest()["busy"], 5), \
            monitor.latest()
    finally:
        monitor.stop()
        harness.close()
    logged = [kind for kind, _fields in events]
    assert "slacs_controls" in logged and "slacs_ui_busy" in logged
    assert "slacs_ui_responsive" in logged and "slacs_ui_latency" in logged
