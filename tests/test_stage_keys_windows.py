"""End-to-end key following on real Windows: real Raw Input, real foreground ID.

This spawns a genuine Tk process named like the SLACS GUI, brings it to the
foreground, injects real W/A/S/D keystrokes with SendInput, and asserts the
overlay receiver moved by the commanded amount, then proves that the same key is
not counted when a different program is in front. It never patches foreground
detection, the identifier, or the keyboard observer. On Windows it fails with
diagnostics rather than skipping when the foreground cannot be arranged, because a
green-by-skip here would hide a broken observer on the one platform that matters.
"""

from __future__ import annotations

import ntpath
import subprocess
import sys
import time

import pytest

from nd2wsi.slacs_identity import SCRIPT_NAME
from nd2wsi.stage_follow import StageFollowReceiver
from nd2wsi.stage_keys import SlacsKeyFollower, _windows_foreground_process

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows Raw Input + foreground")

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

VK_W, VK_D = 0x57, 0x44
STEP_UM = 7.0

_SLACS_SCRIPT = """
import tkinter as tk
root = tk.Tk()
root.title("SLACS stage test")
root.geometry("320x200+120+120")
# Ignore every key: the observer must not depend on SLACS acting on the key.
root.bind("<Key>", lambda event: "break")
root.attributes("-topmost", True)
root.lift()
root.focus_force()
root.after(200, lambda: root.attributes("-topmost", False))
print("ready", flush=True)
root.mainloop()
"""

_OTHER_SCRIPT = """
import tkinter as tk
root = tk.Tk()
root.title("Other window")
root.geometry("320x200+500+120")
root.attributes("-topmost", True)
root.lift()
root.focus_force()
root.after(200, lambda: root.attributes("-topmost", False))
print("ready", flush=True)
root.mainloop()
"""


def _send_key(vk):
    """Inject one real key-down/up pair with SendInput."""
    PUL = ctypes.POINTER(ctypes.c_ulong)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", PUL)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", PUL)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    def event(flags):
        value = INPUT(type=1)
        value.u.ki = KEYBDINPUT(vk, 0, flags, 0, None)
        return value

    array = (INPUT * 2)(event(0), event(0x0002))  # down, KEYEVENTF_KEYUP
    sent = ctypes.windll.user32.SendInput(2, array, ctypes.sizeof(INPUT))
    if sent != 2:
        raise OSError("SendInput did not deliver both key events")


def _wait_for_foreground(pid, *, timeout=10):
    deadline = time.monotonic() + timeout
    seen = None
    while time.monotonic() < deadline:
        foreground = _windows_foreground_process()
        seen = foreground
        if foreground and foreground.get("pid") == pid:
            return True
        time.sleep(0.1)
    return False, seen


def _allow_foreground(pid):
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(pid)
    except Exception:
        pass


def _spawn(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    # Wait for the Tk window to be up ("ready").
    if child.stdout is not None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            line = child.stdout.readline()
            if line.strip() == "ready" or child.poll() is not None:
                break
    return child


def _terminate(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def test_real_wasd_follows_only_while_slacs_is_in_front(tmp_path):
    if ntpath.basename(sys.executable).lower() not in ("python.exe", "pythonw.exe"):
        pytest.skip("requires a normally named Windows Python interpreter")

    slacs = _spawn(tmp_path, SCRIPT_NAME, _SLACS_SCRIPT)
    other = None
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    follower = SlacsKeyFollower(receiver, descriptor["session_id"], step_um=STEP_UM)
    try:
        assert slacs.poll() is None, "the SLACS test process exited before the test ran"
        _allow_foreground(slacs.pid)
        ready = _wait_for_foreground(slacs.pid)
        if ready is not True:
            pytest.fail(f"SLACS test window never became foreground; last foreground={ready[1]}")

        snapshot = follower.start()
        assert snapshot["running"] and snapshot["supported"], snapshot

        for vk in (VK_D, VK_D, VK_D, VK_W):
            _send_key(vk)
            time.sleep(0.05)

        deadline = time.monotonic() + 5
        displacement = receiver.snapshot()["displacement_um"]
        while displacement != [3 * STEP_UM, 1 * STEP_UM] and time.monotonic() < deadline:
            time.sleep(0.1)
            displacement = receiver.snapshot()["displacement_um"]
        assert displacement == [3 * STEP_UM, 1 * STEP_UM], (displacement, follower.snapshot())
        counted = follower.snapshot()["counted"]
        assert counted == 4

        # A different foreground program: the same key must not be counted.
        other = _spawn(tmp_path, "other.py", _OTHER_SCRIPT)
        _allow_foreground(other.pid)
        ready = _wait_for_foreground(other.pid)
        if ready is not True:
            pytest.fail(f"Other test window never became foreground; last foreground={ready[1]}")
        _send_key(VK_D)
        time.sleep(0.5)
        after = follower.snapshot()
        assert after["counted"] == counted, "a key was counted while SLACS was not in front"
        assert after["ignored_not_slacs"] >= 1
        assert receiver.snapshot()["displacement_um"] == [3 * STEP_UM, 1 * STEP_UM]
    finally:
        follower.stop()
        receiver.close()
        _terminate(slacs)
        if other is not None:
            _terminate(other)
