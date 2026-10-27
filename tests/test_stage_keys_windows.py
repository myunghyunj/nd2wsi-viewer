"""End-to-end key following on real Windows: real Raw Input, real foreground ID.

This spawns a genuine Tk process named like the SLACS GUI, brings it to the
foreground, injects real W/A/S/D keystrokes with SendInput, and asserts the
overlay receiver moved by the commanded amount, that Ctrl+D is not counted, and
that the same key is not counted when a different program is in front. It never
patches foreground detection, the identifier, or the keyboard observer. On Windows
it fails with diagnostics rather than skipping when the foreground cannot be
arranged, because a green-by-skip here would hide a broken observer on the one
platform that matters.
"""

from __future__ import annotations

import ntpath
import subprocess
import sys
import time

import pytest

from nd2wsi.slacs_identity import SCRIPT_NAME
from nd2wsi.stage_follow import StageFollowReceiver
from nd2wsi.stage_keys import (
    SlacsKeyFollower,
    _windows_foreground_process,
    _windows_process_is_slacs,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows Raw Input + foreground")

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    # Private instances with prototypes: never change calls other code shares.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for _name, _restype, _argtypes in (
            ("SendInput", wintypes.UINT, [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]),
            ("AllowSetForegroundWindow", wintypes.BOOL, [wintypes.DWORD]),
            ("SetForegroundWindow", wintypes.BOOL, [wintypes.HWND]),
            ("BringWindowToTop", wintypes.BOOL, [wintypes.HWND]),
            ("ShowWindow", wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
            ("GetForegroundWindow", wintypes.HWND, []),
            ("GetWindowThreadProcessId", wintypes.DWORD, [wintypes.HWND, wintypes.LPDWORD]),
            ("AttachThreadInput", wintypes.BOOL, [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]),
            ("PostMessageW", wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                             wintypes.LPARAM]),
            ("GetWindowTextW", ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int])):
        _function = getattr(_user32, _name)
        _function.restype, _function.argtypes = _restype, _argtypes
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetCurrentThreadId.argtypes = []

VK_W, VK_D, VK_CONTROL, VK_MENU = 0x57, 0x44, 0x11, 0x12
KEYEVENTF_KEYUP = 0x0002
ASFW_ANY = 0xFFFFFFFF
SW_SHOW = 5
WM_CLOSE = 0x0010
STEP_UM = 7.0

# The child reports its top-level HWND, then waits for "go" so the test can grant
# foreground rights before the window asks for the foreground.
_WINDOW_SCRIPT = """
import sys
import tkinter as tk
root = tk.Tk()
root.title(__TITLE__)
root.geometry(__GEOMETRY__)
# Ignore every key: the observer must not depend on the program acting on it.
root.bind("<Key>", lambda event: "break")
root.update()
print("ready", root.wm_frame(), flush=True)
sys.stdin.readline()
root.attributes("-topmost", True)
root.lift()
root.focus_force()
root.after(300, lambda: root.attributes("-topmost", False))
root.mainloop()
"""


def _send_keys(events):
    """Inject (vk, key_up) events in one SendInput call, so they stay in order."""
    array = (INPUT * len(events))()
    for slot, (vk, key_up) in zip(array, events):
        slot.type = 1  # INPUT_KEYBOARD
        slot.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if key_up else 0, 0, 0)
    sent = _user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(f"SendInput delivered {sent} of {len(events)} events "
                      f"(Windows error {ctypes.get_last_error()})")


def _press(vk):
    _send_keys([(vk, False), (vk, True)])


def _window_pid(hwnd):
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _foreground_description():
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return "no foreground window"
    title = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, title, len(title))
    return f"hwnd={hwnd:#x} pid={_window_pid(hwnd)} title={title.value!r}"


def _is_foreground(pid):
    foreground = _windows_foreground_process()  # the follower's own provider
    return bool(foreground) and foreground.get("pid") == pid


def _wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def _bring_to_front(hwnd, pid, *, timeout=10):
    """Make ``hwnd`` the foreground window with the documented workarounds."""
    attempts = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_foreground(pid):
            return True, attempts
        # An ALT tap makes this process the last to receive input, which the
        # foreground lock requires before SetForegroundWindow may succeed.
        _send_keys([(VK_MENU, False), (VK_MENU, True)])
        foreground = _user32.GetForegroundWindow()
        foreground_thread = _user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        own_thread = _kernel32.GetCurrentThreadId()
        attached = bool(foreground_thread and foreground_thread != own_thread
                        and _user32.AttachThreadInput(own_thread, foreground_thread, True))
        try:
            _user32.ShowWindow(hwnd, SW_SHOW)
            _user32.BringWindowToTop(hwnd)
            accepted = bool(_user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                _user32.AttachThreadInput(own_thread, foreground_thread, False)
        attempts.append({"attached": attached, "accepted": accepted})
        if _wait_until(lambda: _is_foreground(pid), 1.0):
            return True, attempts
    return _is_foreground(pid), attempts


def _spawn_window(tmp_path, name, title, geometry):
    """Start a Tk window process; return (process, top-level hwnd, owning pid)."""
    script = tmp_path / name
    script.write_text(_WINDOW_SCRIPT.replace("__TITLE__", repr(title))
                      .replace("__GEOMETRY__", repr(geometry)), encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = child.stdout.readline() if child.stdout is not None else ""
    parts = line.split()
    if len(parts) != 2 or parts[0] != "ready":
        stderr = child.stderr.read() if child.poll() is not None and child.stderr else ""
        _terminate(child, None)
        pytest.fail(f"{name} did not report its window: {line!r} {stderr!r}")
    hwnd = int(parts[1], 16)
    # The window's owner, not Popen.pid: a venv python.exe may be a launcher that
    # runs the base interpreter as a child process.
    return child, hwnd, _window_pid(hwnd)


def _activate(child, hwnd, pid):
    """Grant foreground rights, let the child ask, then force it if needed."""
    _user32.AllowSetForegroundWindow(ASFW_ANY)
    child.stdin.write("go\n")
    child.stdin.flush()
    if _wait_until(lambda: _is_foreground(pid), 3.0):
        return
    ok, attempts = _bring_to_front(hwnd, pid)
    if not ok:
        pytest.fail(f"window pid {pid} never became foreground; "
                    f"foreground is {_foreground_description()}; attempts={attempts}")


def _terminate(child, hwnd):
    if hwnd is not None and child.poll() is None:
        _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # also ends a launcher's child
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
    for stream in (child.stdin, child.stdout, child.stderr):
        if stream is not None:
            stream.close()


def _wait_for_displacement(receiver, expected, timeout=5):
    _wait_until(lambda: receiver.snapshot()["displacement_um"] == expected, timeout)
    return receiver.snapshot()["displacement_um"]


def test_real_wasd_follows_only_while_slacs_is_in_front(tmp_path):
    if ntpath.basename(sys.executable).lower() not in ("python.exe", "pythonw.exe"):
        pytest.skip("requires a normally named Windows Python interpreter")

    slacs, slacs_hwnd, slacs_pid = _spawn_window(tmp_path, SCRIPT_NAME, "SLACS stage test",
                                                 "320x200+120+120")
    other = other_hwnd = None
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    follower = SlacsKeyFollower(receiver, descriptor["session_id"], step_um=STEP_UM)
    try:
        _activate(slacs, slacs_hwnd, slacs_pid)
        assert _windows_process_is_slacs(slacs_pid), "the SLACS test process was not identified"

        snapshot = follower.start()
        assert snapshot["running"] and snapshot["supported"], snapshot

        for vk in (VK_D, VK_D, VK_D, VK_W):
            _press(vk)
            time.sleep(0.05)
        expected = [3 * STEP_UM, 1 * STEP_UM]
        displacement = _wait_for_displacement(receiver, expected)
        assert displacement == expected, (displacement, follower.snapshot(), _foreground_description())
        assert follower.snapshot()["counted"] == 4

        # Ctrl+D is a shortcut, not a jog; a plain D after it counts again.
        _send_keys([(VK_CONTROL, False), (VK_D, False), (VK_D, True), (VK_CONTROL, True)])
        time.sleep(0.3)
        _press(VK_D)
        expected = [4 * STEP_UM, 1 * STEP_UM]
        displacement = _wait_for_displacement(receiver, expected)
        assert displacement == expected, (displacement, follower.snapshot())
        counted = follower.snapshot()["counted"]
        assert counted == 5

        # A different foreground program: the same key must not be counted.
        other, other_hwnd, other_pid = _spawn_window(tmp_path, "other.py", "Other window",
                                                     "320x200+500+120")
        _activate(other, other_hwnd, other_pid)
        _press(VK_D)
        time.sleep(0.5)
        after = follower.snapshot()
        assert after["counted"] == counted, "a key was counted while SLACS was not in front"
        assert after["ignored_not_slacs"] >= 1
        assert receiver.snapshot()["displacement_um"] == expected
    finally:
        follower.stop()
        receiver.close()
        _terminate(slacs, slacs_hwnd)
        if other is not None:
            _terminate(other, other_hwnd)
