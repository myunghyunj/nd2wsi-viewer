"""SLACS's Roughness and Mirror controls are read only, fail closed, and logged on change."""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from nd2wsi import session_log, slacs_controls
from nd2wsi.slacs_controls import (
    BUSY_LATENCY_MS,
    READ_ONLY_MESSAGES,
    SlacsControlsMonitor,
    _read_controls,
    _Win32Windows,
    read_slacs_controls,
)

TBM_GETPOS, TBM_GETRANGEMIN, TBM_GETRANGEMAX, TBM_SETPOS = 0x400, 0x401, 0x402, 0x405
BM_GETCHECK, BM_SETCHECK = 0xF0, 0xF1
BS_PUSHBUTTON, BS_CHECKBOX, BS_AUTOCHECKBOX, BS_OWNERDRAW = 0x0, 0x2, 0x3, 0xB
WS_CHILD_VISIBLE = 0x50000000
SLACS_PID, OTHER_PID = 4321, 999


@pytest.fixture
def log_events(monkeypatch):
    events = []
    monkeypatch.setattr(session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    return events


def kinds(events, kind):
    return [fields for name, fields in events if name == kind]


# -- a fake window manager -----------------------------------------------------


class FakeWindows:
    """Windows of several processes, answering the adapter calls the reader makes."""

    def __init__(self):
        self.windows: dict[int, dict] = {}
        self.sends: list[tuple[int, int, int]] = []
        self.enumerations = 0
        self.hung: set[int] = set()
        self.busy_on: set[int] = set()  # messages that time out
        self._next = 0x100

    def add(self, pid, parent, cls, text="", *, style=WS_CHILD_VISIBLE, low=0, high=100, pos=0,
            check=0):
        self._next += 2
        self.windows[self._next] = {"pid": pid, "parent": parent, "cls": cls, "text": text,
                                    "style": style, "low": low, "high": high, "pos": pos,
                                    "check": check, "alive": True}
        return self._next

    def destroy(self, hwnd):
        self.windows[hwnd]["alive"] = False

    def _live(self, hwnd):
        window = self.windows.get(hwnd)
        return window if window is not None and window["alive"] else None

    def top_level_windows(self, pid):
        self.enumerations += 1
        return [hwnd for hwnd, window in self.windows.items()
                if window["alive"] and window["parent"] is None and window["pid"] == pid]

    def descendants(self, hwnd):
        found = []
        for child, window in self.windows.items():
            if window["alive"] and window["parent"] == hwnd:
                found.append(child)
                found.extend(self.descendants(child))
        return found

    def is_window(self, hwnd):
        return self._live(hwnd) is not None

    def owner_pid(self, hwnd):
        window = self._live(hwnd)
        return None if window is None else window["pid"]

    def class_name(self, hwnd):
        window = self._live(hwnd)
        return "" if window is None else window["cls"]

    def text(self, hwnd):
        window = self._live(hwnd)
        return "" if window is None else window["text"]

    def style(self, hwnd):
        return self.windows[hwnd]["style"]

    def root(self, hwnd):
        while self.windows[hwnd]["parent"] is not None:
            hwnd = self.windows[hwnd]["parent"]
        return hwnd

    def is_hung(self, hwnd):
        return hwnd in self.hung

    def send(self, hwnd, message, timeout_ms):
        self.sends.append((hwnd, message, timeout_ms))
        if message in self.busy_on:
            raise slacs_controls._Busy(f"SLACS did not answer within {timeout_ms} ms.")
        window = self.windows[hwnd]
        return {TBM_GETPOS: window["pos"], TBM_GETRANGEMIN: window["low"],
                TBM_GETRANGEMAX: window["high"], BM_GETCHECK: window["check"]}[message]


def make_slacs(fake, pid=SLACS_PID, *, roughness=10, mirror_h=1, mirror_v=0):
    """SLACS's panel as wxPython builds it on Windows."""
    frame = fake.add(pid, None, "wxWindowNR", "SniperCellSorter")
    panel = fake.add(pid, frame, "wxWindowNR")
    handles = {"frame": frame, "panel": panel}
    handles["bf"] = fake.add(pid, panel, "msctls_trackbar32", low=1, high=100, pos=50)
    handles["target"] = fake.add(pid, panel, "msctls_trackbar32", low=1, high=100, pos=20)
    handles["roughness"] = fake.add(pid, panel, "msctls_trackbar32", low=1, high=1000,
                                    pos=roughness)
    fake.add(pid, panel, "Static", "Roughness")
    fake.add(pid, panel, "Static", "1000")  # wx.SL_LABELS adds labels beside the trackbar
    fake.add(pid, panel, "Button", "Start", style=WS_CHILD_VISIBLE | BS_PUSHBUTTON)
    handles["mirror_h"] = fake.add(pid, panel, "Button", "Mirror Horizontal",
                                   style=WS_CHILD_VISIBLE | BS_CHECKBOX, check=mirror_h)
    handles["mirror_v"] = fake.add(pid, panel, "Button", "Mirror Vertical",
                                   style=WS_CHILD_VISIBLE | BS_CHECKBOX, check=mirror_v)
    return handles


def slacs_and_neighbour():
    fake = FakeWindows()
    handles = make_slacs(fake)
    # Another program with an identical panel must never be read.
    make_slacs(fake, OTHER_PID, roughness=500, mirror_h=0, mirror_v=1)
    return fake, handles


# -- reading -------------------------------------------------------------------


def test_reads_roughness_and_mirrors_of_that_process_only():
    fake, _handles = slacs_and_neighbour()
    reading = _read_controls(fake, SLACS_PID)
    assert reading["ok"] is True, reading
    assert reading["roughness_um"] == 10.0 and isinstance(reading["roughness_um"], float)
    assert reading["mirror_h"] is True and reading["mirror_v"] is False
    assert reading["busy"] is False and reading["error"] is None
    assert reading["latency_ms"] >= 0
    other = _read_controls(fake, OTHER_PID)
    assert (other["roughness_um"], other["mirror_h"], other["mirror_v"]) == (500.0, False, True)


def test_only_read_only_queries_are_sent_with_the_timeout():
    fake, handles = slacs_and_neighbour()
    _read_controls(fake, SLACS_PID, timeout_ms=40)
    assert fake.sends, "the reader sent nothing"
    assert {message for _hwnd, message, _timeout in fake.sends} <= READ_ONLY_MESSAGES
    assert {timeout for _hwnd, _message, timeout in fake.sends} == {40}
    slacs_windows = {hwnd for hwnd, window in fake.windows.items() if window["pid"] == SLACS_PID}
    assert {hwnd for hwnd, _message, _timeout in fake.sends} <= slacs_windows
    assert READ_ONLY_MESSAGES == {TBM_GETPOS, TBM_GETRANGEMIN, TBM_GETRANGEMAX, BM_GETCHECK}
    assert (handles["roughness"], TBM_GETPOS, 40) in fake.sends


def test_a_state_changing_message_is_refused_before_it_is_sent():
    fake, handles = slacs_and_neighbour()
    session = slacs_controls._ReadSession(fake, 100)
    for message in (TBM_SETPOS, BM_SETCHECK, 0x0010):
        with pytest.raises(ValueError):
            session.send(handles["roughness"], message)
    assert fake.sends == []


def test_cached_handles_skip_discovery_and_send_five_queries():
    fake, handles = slacs_and_neighbour()
    cache = {}
    assert _read_controls(fake, SLACS_PID, cache=cache)["ok"]
    assert cache == {"pid": SLACS_PID, "roughness": handles["roughness"],
                     "mirror_h": handles["mirror_h"], "mirror_v": handles["mirror_v"]}
    enumerations, fake.sends = fake.enumerations, []
    fake.windows[handles["roughness"]]["pos"] = 1
    reading = _read_controls(fake, SLACS_PID, cache=cache)
    assert reading["ok"] and reading["roughness_um"] == 1.0
    assert fake.enumerations == enumerations
    assert [message for _hwnd, message, _timeout in fake.sends] == [
        TBM_GETRANGEMIN, TBM_GETRANGEMAX, TBM_GETPOS, BM_GETCHECK, BM_GETCHECK]


@pytest.mark.parametrize("staleness", ["destroyed", "other_pid", "other_class", "renamed",
                                       "restyled", "cache_of_other_pid"])
def test_stale_cached_handles_are_rediscovered(staleness):
    fake, handles = slacs_and_neighbour()
    cache = {}
    assert _read_controls(fake, SLACS_PID, cache=cache)["ok"]
    if staleness == "destroyed":
        fake.destroy(handles["roughness"])
        handles["roughness"] = fake.add(SLACS_PID, handles["panel"], "msctls_trackbar32",
                                        low=1, high=1000, pos=7)
    elif staleness == "other_pid":
        fake.windows[handles["mirror_h"]]["pid"] = OTHER_PID
        handles["mirror_h"] = fake.add(SLACS_PID, handles["panel"], "Button", "Mirror Horizontal",
                                       style=WS_CHILD_VISIBLE | BS_CHECKBOX, check=0)
    elif staleness == "other_class":
        fake.windows[handles["roughness"]]["cls"] = "Static"
        handles["roughness"] = fake.add(SLACS_PID, handles["panel"], "msctls_trackbar32",
                                        low=1, high=1000, pos=7)
    elif staleness == "renamed":
        fake.windows[handles["mirror_v"]]["text"] = "Something else"
        handles["mirror_v"] = fake.add(SLACS_PID, handles["panel"], "Button", "Mirror Vertical",
                                       style=WS_CHILD_VISIBLE | BS_CHECKBOX, check=1)
    elif staleness == "restyled":
        fake.windows[handles["mirror_v"]]["style"] = WS_CHILD_VISIBLE | BS_PUSHBUTTON
        fake.windows[handles["mirror_v"]]["text"] = "Pushed"
        handles["mirror_v"] = fake.add(SLACS_PID, handles["panel"], "Button", "Mirror Vertical",
                                       style=WS_CHILD_VISIBLE | BS_CHECKBOX, check=1)
    else:
        cache["pid"] = OTHER_PID
    enumerations = fake.enumerations
    reading = _read_controls(fake, SLACS_PID, cache=cache)
    assert reading["ok"], reading
    assert fake.enumerations == enumerations + 1
    assert cache["roughness"] == handles["roughness"]
    assert cache["mirror_h"] == handles["mirror_h"] and cache["mirror_v"] == handles["mirror_v"]


def test_a_cached_slider_whose_range_changed_fails_closed_then_rediscovers():
    fake, handles = slacs_and_neighbour()
    cache = {}
    assert _read_controls(fake, SLACS_PID, cache=cache)["ok"]
    fake.windows[handles["roughness"]]["high"] = 100
    fake.windows[handles["bf"]]["high"] = 1000
    fake.windows[handles["bf"]]["pos"] = 33
    reading = _read_controls(fake, SLACS_PID, cache=cache)
    assert not reading["ok"] and reading["roughness_um"] is None
    assert "range" in reading["error"] and cache == {}
    again = _read_controls(fake, SLACS_PID, cache=cache)
    assert again["ok"] and again["roughness_um"] == 33.0 and cache["roughness"] == handles["bf"]


def _drop(fake, handles, key):
    fake.destroy(handles[key])


@pytest.mark.parametrize("change, message", [
    (lambda fake, h: [fake.destroy(hwnd) for hwnd in list(fake.windows)
                      if fake.windows[hwnd]["pid"] == SLACS_PID], "no open window"),
    (lambda fake, h: fake.windows[h["roughness"]].update(high=999), "Roughness range"),
    (lambda fake, h: [fake.destroy(h[key]) for key in ("bf", "target", "roughness")],
     "slider was not found"),
    (lambda fake, h: fake.add(SLACS_PID, h["panel"], "msctls_trackbar32", low=1, high=1000,
                              pos=500), "More than one slider"),
    (lambda fake, h: fake.add(SLACS_PID, fake.add(SLACS_PID, None, "wxWindowNR"),
                              "msctls_trackbar32", low=1, high=1000, pos=1),
     "More than one slider"),
    (lambda fake, h: _drop(fake, h, "mirror_h"), "'Mirror Horizontal' checkbox was not found"),
    (lambda fake, h: fake.add(SLACS_PID, h["panel"], "Button", "Mirror Vertical",
                              style=WS_CHILD_VISIBLE | BS_AUTOCHECKBOX),
     "More than one 'Mirror Vertical'"),
    (lambda fake, h: fake.windows[h["mirror_h"]].update(style=WS_CHILD_VISIBLE | BS_PUSHBUTTON),
     "not a readable checkbox"),
    (lambda fake, h: fake.windows[h["mirror_v"]].update(style=WS_CHILD_VISIBLE | BS_OWNERDRAW),
     "not a readable checkbox"),
    (lambda fake, h: fake.windows[h["mirror_h"]].update(text="Mirror horizontal"),
     "'Mirror Horizontal' checkbox was not found"),
    (lambda fake, h: fake.windows[h["mirror_v"]].update(text="Mirror Vertical "),
     "'Mirror Vertical' checkbox was not found"),
    (lambda fake, h: fake.windows[h["mirror_h"]].update(check=2), "neither checked nor unchecked"),
    (lambda fake, h: fake.windows[h["mirror_v"]].update(check=-1), "neither checked nor unchecked"),
    (lambda fake, h: fake.windows[h["roughness"]].update(pos=1001), "outside 1..1000"),
    (lambda fake, h: fake.windows[h["roughness"]].update(pos=0), "outside 1..1000"),
    (lambda fake, h: fake.windows[h["mirror_h"]].update(
        parent=fake.add(SLACS_PID, None, "wxWindowNR")), "not in one window"),
])
def test_any_doubt_about_the_controls_fails_closed(change, message):
    fake, handles = slacs_and_neighbour()
    change(fake, handles)
    cache = {"pid": 1}
    reading = _read_controls(fake, SLACS_PID, cache=cache)
    assert reading["ok"] is False, reading
    assert (reading["roughness_um"], reading["mirror_h"], reading["mirror_v"]) == (None, None, None)
    assert reading["busy"] is False
    assert message in reading["error"]
    assert cache == {}


def test_no_message_is_sent_to_a_window_without_both_mirror_checkboxes():
    fake = FakeWindows()
    handles = make_slacs(fake)
    fake.destroy(handles["mirror_v"])
    reading = _read_controls(fake, SLACS_PID)
    assert not reading["ok"] and fake.sends == []


@pytest.mark.parametrize("pid", [None, 0, -5, True, "4321", 4321.0])
def test_a_missing_or_malformed_pid_reads_nothing(pid):
    fake, _handles = slacs_and_neighbour()
    reading = _read_controls(fake, pid)
    assert not reading["ok"] and reading["error"] and fake.sends == [] and fake.enumerations == 0


@pytest.mark.parametrize("timeout_ms", [0, -1, True, 1.5, 5001, None])
def test_a_bad_timeout_reads_nothing(timeout_ms):
    fake, _handles = slacs_and_neighbour()
    reading = _read_controls(fake, SLACS_PID, timeout_ms=timeout_ms)
    assert not reading["ok"] and "timeout_ms" in reading["error"] and fake.sends == []


def test_a_timed_out_query_is_busy_and_keeps_the_cache():
    fake, handles = slacs_and_neighbour()
    cache = {}
    assert _read_controls(fake, SLACS_PID, cache=cache)["ok"]
    fake.busy_on = {TBM_GETRANGEMIN}
    fake.sends = []
    reading = _read_controls(fake, SLACS_PID, cache=cache)
    assert reading["busy"] is True and reading["ok"] is False
    assert reading["roughness_um"] is None and "did not answer" in reading["error"]
    assert len(fake.sends) == 1, "a busy SLACS must cost one timeout, not one per query"
    assert cache["roughness"] == handles["roughness"]


def test_a_busy_discovery_is_busy_too():
    fake, _handles = slacs_and_neighbour()
    fake.busy_on = {TBM_GETRANGEMIN}
    reading = _read_controls(fake, SLACS_PID, cache={})
    assert reading["busy"] and not reading["ok"] and len(fake.sends) == 1


def test_a_hung_window_is_never_sent_a_message():
    fake, handles = slacs_and_neighbour()
    fake.hung = {handles["frame"]}
    reading = _read_controls(fake, SLACS_PID)
    assert reading["busy"] and not reading["ok"] and "not responding" in reading["error"]
    assert fake.sends == []


def test_an_unexpected_failure_fails_closed_and_forgets_the_handles():
    fake, _handles = slacs_and_neighbour()
    cache = {}
    assert _read_controls(fake, SLACS_PID, cache=cache)["ok"]

    def broken(_hwnd):
        raise RuntimeError("boom")

    fake.is_window = broken
    reading = _read_controls(fake, SLACS_PID, cache=cache)
    assert not reading["ok"] and not reading["busy"]
    assert reading["error"] == "Could not read SLACS's controls: RuntimeError"
    assert cache == {}


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows answer")
def test_reading_off_windows_reports_unsupported_without_win32():
    reading = read_slacs_controls(SLACS_PID, cache={})
    assert reading["ok"] is False and "only available on Windows" in reading["error"]
    assert set(reading) == {"ok", "roughness_um", "mirror_h", "mirror_v", "latency_ms", "busy",
                            "error"}


# -- the Win32 adapter, with a fake user32 -------------------------------------


class FakeUser32:
    def __init__(self):
        self.calls = []
        self.answer = 1
        self.result = 0
        self.error = 0
        self.alive = True
        self.owners = {}
        self.tops = []
        self.children = {}

    def SendMessageTimeoutW(self, hwnd, message, wparam, lparam, flags, timeout, result):
        self.calls.append(("SendMessageTimeoutW", hwnd, message, wparam, lparam, flags, timeout))
        result._obj.value = self.result
        return self.answer

    def IsWindow(self, hwnd):
        return self.alive

    def IsHungAppWindow(self, hwnd):
        return False

    def GetWindowThreadProcessId(self, hwnd, pid):
        if hwnd not in self.owners:
            return 0
        pid._obj.value = self.owners[hwnd]
        return 77

    def EnumWindows(self, callback, lparam):
        for hwnd in self.tops:
            if not callback(hwnd, lparam):
                break
        return True

    def EnumChildWindows(self, parent, callback, lparam):
        for hwnd in self.children.get(parent, []):
            callback(hwnd, lparam)
        return True

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = "msctls_trackbar32"
        return len(buffer.value)

    def InternalGetWindowText(self, hwnd, buffer, size):
        self.calls.append(("InternalGetWindowText", hwnd))
        buffer.value = "Mirror Vertical"
        return len(buffer.value)

    def GetAncestor(self, hwnd, flags):
        return 0x10


def fake_api(user32):
    errors = []

    def set_last_error(value):
        errors.append(("set", value))
        return 0

    def get_last_error():
        errors.append(("get", user32.error))
        return user32.error

    api = SimpleNamespace(ctypes=ctypes, wintypes=wintypes, user32=user32,
                          get_window_long=lambda hwnd, index: -0x6FFFFFFD,  # sign-extended style
                          WNDENUMPROC=lambda function: function, get_last_error=get_last_error,
                          set_last_error=set_last_error)
    return api, errors


def test_adapter_sends_with_abort_if_hung_and_error_on_exit():
    user32 = FakeUser32()
    user32.result = 1000
    api, errors = fake_api(user32)
    windows = _Win32Windows(api)
    assert windows.send(0x42, TBM_GETRANGEMAX, 80) == 1000
    assert user32.calls == [("SendMessageTimeoutW", 0x42, TBM_GETRANGEMAX, 0, 0, 0x22, 80)]
    assert errors == [("set", 0)]


def test_adapter_returns_a_negative_int_from_a_pointer_sized_result():
    user32 = FakeUser32()
    user32.result = (1 << 64) - 3
    windows = _Win32Windows(fake_api(user32)[0])
    assert windows.send(0x42, TBM_GETRANGEMIN, 80) == -3
    assert slacs_controls._signed32(0xFFFFFFFF) == -1 and slacs_controls._signed32(1000) == 1000


def test_adapter_refuses_any_message_that_is_not_a_read_only_query():
    user32 = FakeUser32()
    windows = _Win32Windows(fake_api(user32)[0])
    for message in (TBM_SETPOS, BM_SETCHECK, 0x0100):
        with pytest.raises(ValueError):
            windows.send(0x42, message, 80)
    assert user32.calls == []


@pytest.mark.parametrize("error, alive, outcome", [
    (1460, True, slacs_controls._Busy),       # ERROR_TIMEOUT
    (0, True, slacs_controls._Busy),          # aborted because the thread is hung
    (5, True, slacs_controls._Unreadable),    # any other failure is not "busy"
    (1460, False, slacs_controls._Unreadable),  # the control is gone
])
def test_adapter_classifies_a_failed_send(error, alive, outcome):
    user32 = FakeUser32()
    user32.answer, user32.error, user32.alive = 0, error, alive
    windows = _Win32Windows(fake_api(user32)[0])
    with pytest.raises(outcome):
        windows.send(0x42, BM_GETCHECK, 80)


def test_adapter_lists_windows_and_reads_names_without_messages():
    user32 = FakeUser32()
    user32.tops = [0x10, 0x20, 0x30]
    user32.owners = {0x10: SLACS_PID, 0x20: OTHER_PID, 0x30: SLACS_PID, 0x11: SLACS_PID}
    user32.children = {0x10: [0x11, 0x12]}
    windows = _Win32Windows(fake_api(user32)[0])
    assert windows.top_level_windows(SLACS_PID) == [0x10, 0x30]
    assert windows.descendants(0x10) == [0x11, 0x12]
    assert windows.owner_pid(0x11) == SLACS_PID and windows.owner_pid(0x99) is None
    assert windows.class_name(0x11) == "msctls_trackbar32"
    assert windows.text(0x11) == "Mirror Vertical"
    assert windows.style(0x11) == 0x90000003 and slacs_controls._is_checkbox(windows.style(0x11))
    assert windows.root(0x11) == 0x10
    assert not any(call[0] == "SendMessageTimeoutW" for call in user32.calls)


# -- the monitor ---------------------------------------------------------------


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def good(roughness=10.0, mirror_h=False, mirror_v=False, latency=2.0):
    return {"ok": True, "roughness_um": roughness, "mirror_h": mirror_h, "mirror_v": mirror_v,
            "latency_ms": latency, "busy": False, "error": None}


def busy(latency=100.0):
    return {"ok": False, "roughness_um": None, "mirror_h": None, "mirror_v": None,
            "latency_ms": latency, "busy": True, "error": "SLACS did not answer within 100 ms."}


def unreadable(error="SLACS's Roughness slider was not found.", latency=3.0):
    return {"ok": False, "roughness_um": None, "mirror_h": None, "mirror_v": None,
            "latency_ms": latency, "busy": False, "error": error}


class Scripted:
    """A reader that returns the queued readings in order, recording each pid."""

    def __init__(self, *readings):
        self.readings = list(readings)
        self.pids = []

    def queue(self, *readings):
        self.readings.extend(readings)

    def __call__(self, pid):
        self.pids.append(pid)
        return self.readings.pop(0)


def make_monitor(*readings, pid=SLACS_PID, clock=None, **kwargs):
    reader = Scripted(*readings)
    clock = clock or Clock()
    monitor = SlacsControlsMonitor(lambda: pid, reader=reader, clock=clock, **kwargs)
    return monitor, reader, clock


def test_latest_before_any_read_has_no_values():
    monitor, _reader, _clock = make_monitor()
    assert monitor.latest() == {"ok": False, "roughness_um": None, "mirror_h": None,
                                "mirror_v": None, "age_ms": None, "busy": False, "error": None,
                                "reads": 0}


def test_values_are_logged_only_when_they_or_their_readability_change(log_events):
    monitor, reader, clock = make_monitor(good(), good(), good(1.0), good(1.0, mirror_h=True),
                                          good(1.0, mirror_h=True, mirror_v=True), unreadable(),
                                          unreadable(), good(1.0, mirror_h=True, mirror_v=True))
    for _ in range(8):
        monitor.poll_once()
        clock.now += 0.2
    logged = kinds(log_events, "slacs_controls")
    assert [(e["roughness_um"], e["mirror_h"], e["mirror_v"], e["ok"]) for e in logged] == [
        (10.0, False, False, True), (1.0, False, False, True), (1.0, True, False, True),
        (1.0, True, True, True), (None, None, None, False), (1.0, True, True, True)]
    assert logged[4]["error"] == "SLACS's Roughness slider was not found."
    assert logged[0]["error"] is None
    assert reader.pids == [SLACS_PID] * 8
    assert monitor.latest()["reads"] == 8


def test_an_unreadable_read_keeps_the_last_good_values(log_events):
    monitor, _reader, clock = make_monitor(good(10.0, mirror_h=True), unreadable())
    monitor.poll_once()
    clock.now = 1.5
    latest = monitor.poll_once()
    assert latest["ok"] is False and latest["busy"] is False
    assert latest["error"] == "SLACS's Roughness slider was not found."
    assert latest["roughness_um"] == 10.0
    assert latest["mirror_h"] is True and latest["mirror_v"] is False
    assert latest["age_ms"] == 1500.0
    assert kinds(log_events, "slacs_ui_busy") == []


def test_a_busy_spell_is_logged_once_and_its_recovery_summarised(log_events):
    monitor, _reader, clock = make_monitor(good(), busy(100.0), busy(140.0), busy(100.0),
                                           good(latency=3.0))
    monitor.poll_once()
    for when in (1.0, 1.2, 1.4):
        clock.now = when
        latest = monitor.poll_once()
        assert latest["busy"] is True and latest["ok"] is False
        assert latest["roughness_um"] == 10.0, "a busy SLACS keeps the last good Roughness"
    assert latest["age_ms"] == 1400.0
    clock.now = 2.0
    latest = monitor.poll_once()
    assert latest["ok"] and not latest["busy"] and latest["age_ms"] == 0.0
    assert kinds(log_events, "slacs_ui_busy") == [
        {"latency_ms": 100.0, "error": "SLACS did not answer within 100 ms."}]
    assert kinds(log_events, "slacs_ui_responsive") == [
        {"busy_seconds": 1.0, "max_latency_ms": 140.0, "failed_reads": 3}]
    # SLACS not answering is not a change in what its controls say.
    assert len(kinds(log_events, "slacs_controls")) == 1


def test_a_slow_successful_read_counts_as_busy_but_its_values_are_used(log_events):
    slow = BUSY_LATENCY_MS + 50.0
    monitor, _reader, clock = make_monitor(good(10.0), good(1.0, latency=slow), good(1.0))
    monitor.poll_once()
    clock.now = 0.2
    latest = monitor.poll_once()
    assert latest["ok"] and latest["busy"] and latest["roughness_um"] == 1.0
    assert kinds(log_events, "slacs_ui_busy") == [{"latency_ms": slow, "error": None}]
    clock.now = 0.4
    latest = monitor.poll_once()
    assert latest["ok"] and not latest["busy"]
    assert kinds(log_events, "slacs_ui_responsive") == [
        {"busy_seconds": 0.2, "max_latency_ms": slow, "failed_reads": 0}]


def test_a_read_at_the_busy_threshold_is_not_busy(log_events):
    monitor, _reader, _clock = make_monitor(good(latency=float(BUSY_LATENCY_MS)))
    assert monitor.poll_once()["busy"] is False
    assert kinds(log_events, "slacs_ui_busy") == []


def test_an_unreadable_answer_does_not_end_a_busy_spell(log_events):
    monitor, _reader, clock = make_monitor(busy(), unreadable(latency=20.0), good())
    monitor.poll_once()
    clock.now = 0.5
    monitor.poll_once()
    assert kinds(log_events, "slacs_ui_responsive") == []
    clock.now = 1.0
    monitor.poll_once()
    assert kinds(log_events, "slacs_ui_responsive") == [
        {"busy_seconds": 1.0, "max_latency_ms": 100.0, "failed_reads": 2}]


def test_age_is_measured_when_latest_is_called():
    monitor, _reader, clock = make_monitor(good())
    clock.now = 5.0
    monitor.poll_once()
    clock.now = 5.25
    assert monitor.latest()["age_ms"] == 250.0
    clock.now = 7.0
    assert monitor.latest()["age_ms"] == 2000.0


@pytest.mark.parametrize("pid", [None, 0, -1, True, "4321", 1.5])
def test_an_unknown_pid_skips_the_round(pid, log_events):
    monitor, reader, _clock = make_monitor(good(), pid=pid)
    latest = monitor.poll_once()
    assert reader.pids == [] and latest["reads"] == 0 and latest["roughness_um"] is None
    assert log_events == []


def test_a_failing_pid_provider_skips_the_round():
    reader = Scripted(good())

    def provider():
        raise RuntimeError("discovery stopped")

    monitor = SlacsControlsMonitor(provider, reader=reader, clock=Clock())
    assert monitor.poll_once()["reads"] == 0 and reader.pids == []


def test_the_pid_is_asked_for_each_round():
    pids = iter([None, 11, None, 12])
    reader = Scripted(good(), good())
    monitor = SlacsControlsMonitor(lambda: next(pids), reader=reader, clock=Clock())
    for _ in range(4):
        monitor.poll_once()
    assert reader.pids == [11, 12]


@pytest.mark.parametrize("raw", [
    None, "ok", [], {"ok": True},
    {"ok": True, "roughness_um": float("nan"), "mirror_h": False, "mirror_v": False},
    {"ok": True, "roughness_um": float("inf"), "mirror_h": False, "mirror_v": False},
    {"ok": True, "roughness_um": 0, "mirror_h": False, "mirror_v": False},
    {"ok": True, "roughness_um": -10, "mirror_h": False, "mirror_v": False},
    {"ok": True, "roughness_um": True, "mirror_h": False, "mirror_v": False},
    {"ok": True, "roughness_um": "10", "mirror_h": False, "mirror_v": False},
    {"ok": True, "roughness_um": 10, "mirror_h": 1, "mirror_v": False},
    {"ok": True, "roughness_um": 10, "mirror_h": False, "mirror_v": None},
    {"ok": 1, "roughness_um": 10, "mirror_h": False, "mirror_v": False},
])
def test_a_malformed_reading_is_a_failed_read_that_keeps_good_values(raw, log_events):
    monitor, _reader, _clock = make_monitor(good(10.0), raw)
    monitor.poll_once()
    latest = monitor.poll_once()
    assert latest["ok"] is False and latest["error"] and latest["roughness_um"] == 10.0
    assert latest["reads"] == 2
    assert kinds(log_events, "slacs_controls")[-1]["ok"] is False


def test_a_reader_exception_is_a_failed_read():
    def reader(pid):
        raise OSError("access denied")

    monitor = SlacsControlsMonitor(lambda: SLACS_PID, reader=reader, clock=Clock())
    latest = monitor.poll_once()
    assert latest["ok"] is False and latest["reads"] == 1
    assert latest["error"] == "Could not read SLACS's controls: OSError"


def test_a_reading_without_latency_is_timed_by_the_monitor(log_events):
    reading = good()
    del reading["latency_ms"]
    monitor, _reader, _clock = make_monitor(reading)
    monitor.poll_once()
    monitor.stop()
    summary, = kinds(log_events, "slacs_ui_latency")
    assert summary["reads"] == 1 and 0 <= summary["max_ms"] < 1000


def test_latency_is_summarised_every_summary_seconds(log_events):
    latencies = [2.0, 4.0, 3.0, 50.0, 1.0, 5.0]
    readings = [good(latency=value) for value in latencies]
    readings[3] = busy(50.0)
    monitor, _reader, clock = make_monitor(*readings, summary_seconds=30.0)
    for when in (0.0, 10.0, 20.0, 29.9):
        clock.now = when
        monitor.poll_once()
    assert kinds(log_events, "slacs_ui_latency") == []
    clock.now = 30.0
    monitor.poll_once()
    assert kinds(log_events, "slacs_ui_latency") == [
        {"reads": 5, "failed": 1, "busy": 1, "p50_ms": 3.0, "max_ms": 50.0, "seconds": 30.0}]
    clock.now = 45.0
    monitor.poll_once()
    assert len(kinds(log_events, "slacs_ui_latency")) == 1
    monitor.stop()
    assert kinds(log_events, "slacs_ui_latency")[-1] == {
        "reads": 1, "failed": 0, "busy": 0, "p50_ms": 5.0, "max_ms": 5.0, "seconds": 15.0}


def test_skipped_rounds_still_close_a_summary_window(log_events):
    pids = iter([SLACS_PID, None])
    clock = Clock()
    monitor = SlacsControlsMonitor(lambda: next(pids), reader=Scripted(good()), clock=clock,
                                   summary_seconds=5.0)
    monitor.poll_once()
    clock.now = 6.0
    monitor.poll_once()
    assert kinds(log_events, "slacs_ui_latency") == [
        {"reads": 1, "failed": 0, "busy": 0, "p50_ms": 2.0, "max_ms": 2.0, "seconds": 6.0}]


def test_stop_without_reads_logs_nothing_and_is_idempotent(log_events):
    monitor, _reader, _clock = make_monitor()
    monitor.stop()
    monitor.stop()
    assert log_events == []
    assert monitor.start() is False, "a stopped monitor does not start again"


def test_stop_flushes_once_and_later_polls_do_nothing(log_events):
    monitor, reader, _clock = make_monitor(good(), good())
    monitor.poll_once()
    monitor.stop()
    monitor.stop()
    assert len(kinds(log_events, "slacs_ui_latency")) == 1
    monitor.poll_once()
    assert reader.pids == [SLACS_PID] and monitor.latest()["reads"] == 1


def test_unsupported_off_windows_without_a_reader():
    monitor = SlacsControlsMonitor(lambda: SLACS_PID, platform="darwin")
    assert monitor.supported is False
    assert monitor.start() is False
    assert monitor.poll_once()["reads"] == 0
    monitor.stop()
    assert monitor.latest()["ok"] is False


def test_the_default_windows_reader_keeps_its_handle_cache(monkeypatch):
    calls = []

    def fake_read(pid, *, timeout_ms=slacs_controls.READ_TIMEOUT_MS, cache=None):
        calls.append((pid, cache))
        cache["pid"] = pid
        return good()

    monkeypatch.setattr(slacs_controls, "read_slacs_controls", fake_read)
    monitor = SlacsControlsMonitor(lambda: SLACS_PID, platform="win32", clock=Clock())
    assert monitor.supported is True
    monitor.poll_once()
    monitor.poll_once()
    assert [pid for pid, _cache in calls] == [SLACS_PID, SLACS_PID]
    assert calls[0][1] is calls[1][1] and calls[0][1] == {"pid": SLACS_PID}


@pytest.mark.parametrize("option, value", [
    ("interval", 0), ("interval", -1), ("interval", float("nan")), ("interval", True),
    ("summary_seconds", 0), ("summary_seconds", float("inf")), ("summary_seconds", "30"),
])
def test_monitor_periods_must_be_positive_finite(option, value):
    with pytest.raises(ValueError):
        SlacsControlsMonitor(lambda: SLACS_PID, reader=Scripted(), **{option: value})


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _monitor_threads():
    return [thread for thread in threading.enumerate() if thread.name == "nd2wsi-slacs-controls"]


def test_start_is_idempotent_and_stop_is_prompt():
    before = len(_monitor_threads())
    reads = []

    def reader(pid):
        reads.append(pid)
        return good()

    monitor = SlacsControlsMonitor(lambda: SLACS_PID, reader=reader, interval=0.01)
    try:
        assert monitor.start() is True
        assert monitor.start() is True
        assert len(_monitor_threads()) == before + 1
        assert _wait_until(lambda: len(reads) >= 3)
        assert monitor.latest()["roughness_um"] == 10.0
    finally:
        started = time.monotonic()
        monitor.stop()
        assert time.monotonic() - started < 1.0
    count = len(reads)
    time.sleep(0.05)
    assert len(reads) == count, "the monitor kept reading after stop"
    assert _wait_until(lambda: len(_monitor_threads()) == before)


def test_stop_does_not_wait_out_a_slow_read_and_drops_its_result(log_events):
    before = len(_monitor_threads())
    entered, release = threading.Event(), threading.Event()

    def reader(pid):
        entered.set()
        release.wait(5.0)
        return good()

    monitor = SlacsControlsMonitor(lambda: SLACS_PID, reader=reader, interval=0.01)
    try:
        assert monitor.start()
        assert entered.wait(2.0)
        started = time.monotonic()
        monitor.stop()
        assert time.monotonic() - started < slacs_controls.STOP_JOIN_SECONDS + 0.5
    finally:
        release.set()
    assert _wait_until(lambda: len(_monitor_threads()) == before)
    assert monitor.latest()["reads"] == 0 and monitor.latest()["roughness_um"] is None
    assert log_events == []


def test_stop_from_inside_a_round_does_not_deadlock():
    before = len(_monitor_threads())
    holder = {}

    def provider():
        holder["monitor"].stop()
        return SLACS_PID

    reader = Scripted(good())
    monitor = SlacsControlsMonitor(provider, reader=reader, interval=0.01)
    holder["monitor"] = monitor
    assert monitor.start()
    assert _wait_until(lambda: len(_monitor_threads()) == before)
    assert monitor.latest()["reads"] == 0 and reader.pids == []
