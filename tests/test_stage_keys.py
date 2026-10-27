"""The W/A/S/D key follower observes passively and never records other keys."""

from __future__ import annotations

import math
import time

import pytest

from nd2wsi import session_log, stage_keys
from nd2wsi.stage_follow import StageFollowReceiver
from nd2wsi.stage_keys import (
    SlacsKeyFollower,
    _ModifierTracker,
    _RawInputHub,
    _RawInputKeyObserver,
    _SharedKeyObserver,
)

VK_W, VK_A, VK_S, VK_D = 0x57, 0x41, 0x53, 0x44


class ManualObserver:
    """A test observer: the test delivers key events synchronously."""

    def __init__(self):
        self.on_event = None
        self.started = False
        self.stopped = False

    def start(self, on_event):
        self.on_event = on_event
        self.started = True
        return True

    def stop(self):
        self.stopped = True

    def send(self, vk, down=True, modifiers=()):
        self.on_event(vk, down, modifiers)


class FailingObserver:
    def start(self, on_event):
        return False

    def stop(self):
        self.stopped = True


class RecordingReceiver:
    def __init__(self, responder=None):
        self.submissions = []
        self._responder = responder or (lambda payload: {"ok": True})

    def submit(self, payload):
        self.submissions.append(payload)
        return self._responder(payload)


@pytest.fixture
def log_events(monkeypatch):
    events = []
    monkeypatch.setattr(session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    return events


def make_follower(receiver, *, in_front=True, step_um=10, **kwargs):
    observer = ManualObserver()
    follower = SlacsKeyFollower(
        receiver, "sid", step_um=step_um, observer=observer,
        foreground_provider=lambda: {"pid": 1, "created": 10} if in_front[0] else {"pid": 2, "created": 20},
        identifier=lambda pid, created: pid == 1,
        heartbeat_interval=None, **kwargs)
    return follower, observer


def test_each_wasd_key_down_submits_one_commanded_delta(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], step_um=10)
    follower.start()
    for vk in (VK_D, VK_D, VK_D, VK_W):
        observer.send(vk)
    deltas = [p["delta_um"] for p in receiver.submissions]
    assert deltas == [[10.0, 0.0], [10.0, 0.0], [10.0, 0.0], [0.0, -10.0]]
    assert [p["seq"] for p in receiver.submissions] == [1, 2, 3, 4]
    snapshot = follower.snapshot()
    assert snapshot["counted"] == 4 and snapshot["last_key"] == "W"
    assert snapshot["ignored_not_slacs"] == 0 and snapshot["error"] is None
    assert [kind for kind, _ in log_events].count("follow_key") == 4
    follower.stop()


def test_direction_table_maps_all_four_keys_to_screen_directions(log_events):
    # Screen right is +x and screen down is +y: W moves the view up, S down, A left, D right.
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], step_um=2)
    follower.start()
    for vk in (VK_W, VK_S, VK_A, VK_D):
        observer.send(vk)
    assert [p["delta_um"] for p in receiver.submissions] == [
        [0.0, -2.0], [0.0, 2.0], [-2.0, 0.0], [2.0, 0.0]]
    follower.stop()


def test_only_keyboard_input_is_observed():
    # Follow counts W/A/S/D key presses only: one Raw Input registration, for the
    # generic-desktop keyboard (usage page 1, usage 6), and no mouse input of any kind.
    import re
    from pathlib import Path

    source = Path(stage_keys.__file__).read_text(encoding="utf-8")
    registrations = re.findall(r"RAWINPUTDEVICE\((0x[0-9A-Fa-f]+), (0x[0-9A-Fa-f]+),", source)
    assert registrations and set(registrations) == {("0x01", "0x06")}
    for absent in ("RIM_TYPEMOUSE", "WM_LBUTTON", "WM_RBUTTON", "WH_MOUSE", "SetWindowsHookEx", "mouse_event"):
        assert absent not in source
    assert sorted(letter for letter, _ in stage_keys.KEY_BINDINGS.values()) == ["A", "D", "S", "W"]


def test_one_sign_reverses_all_four_keys_at_once(log_events, monkeypatch):
    assert stage_keys.KEY_VIEW_SIGN == 1
    monkeypatch.setattr(stage_keys, "KEY_VIEW_SIGN", -1)
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], step_um=2)
    follower.start()
    for vk in (VK_W, VK_S, VK_A, VK_D):
        observer.send(vk)
    assert [p["delta_um"] for p in receiver.submissions] == [
        [0.0, 2.0], [0.0, -2.0], [2.0, 0.0], [-2.0, 0.0]]
    follower.stop()


def test_autorepeat_down_events_each_count(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    for _ in range(5):  # holding the key autorepeats key-down with no key-up
        observer.send(VK_D, down=True)
    assert follower.snapshot()["counted"] == 5
    follower.stop()


def test_key_up_is_ignored(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    observer.send(VK_D, down=False)
    observer.send(VK_W, down=False)
    assert receiver.submissions == [] and follower.snapshot()["counted"] == 0
    follower.stop()


@pytest.mark.parametrize("modifier", ["ctrl", "alt", "win"])
def test_modified_jog_keys_are_ignored(log_events, modifier):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    observer.send(VK_D, modifiers=(modifier,))
    observer.send(VK_W, modifiers={modifier: True, "shift": True})
    assert receiver.submissions == [] and follower.snapshot()["counted"] == 0
    follower.stop()


def test_shift_alone_still_counts(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    observer.send(VK_D, modifiers=("shift",))
    assert follower.snapshot()["counted"] == 1
    follower.stop()


def test_presses_outside_slacs_are_only_tallied_as_ignored(log_events):
    in_front = [False]
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=in_front)
    follower.start()
    observer.send(VK_D)
    observer.send(VK_W)
    snapshot = follower.snapshot()
    assert receiver.submissions == []
    assert snapshot["ignored_not_slacs"] == 2 and snapshot["counted"] == 0
    assert snapshot["last_key"] is None and snapshot["slacs_in_front"] is False
    # No individual key is recorded for ignored presses.
    assert "follow_key" not in [kind for kind, _ in log_events]
    follower.stop()


def test_foreground_transition_is_logged_with_pid_and_identified(log_events):
    in_front = [True]
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=in_front)
    follower.start()
    observer.send(VK_D)  # in front -> counted, logs slacs_foreground(identified=True)
    in_front[0] = False
    observer.send(VK_D)  # not in front -> ignored, logs slacs_foreground(identified=False)
    foreground = [fields for kind, fields in log_events if kind == "slacs_foreground"]
    assert foreground == [{"pid": 1, "identified": True}, {"pid": 2, "identified": False}]
    follower.stop()


def test_non_wasd_keys_never_reach_the_log_or_snapshot(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    # Every other key the observer sees must be discarded immediately: typing a
    # password, pressing Enter, arrows, etc. None may be counted, submitted, or logged.
    for vk in (0x42, 0x50, 0x0D, 0x1B, 0x25, 0x26, 0x51, 0x5A, 0x31, 0x20):
        observer.send(vk, down=True)
        observer.send(vk, down=False)
    assert receiver.submissions == []
    snapshot = follower.snapshot()
    assert snapshot["counted"] == 0 and snapshot["ignored_not_slacs"] == 0
    assert snapshot["last_key"] is None
    # No key event of any kind was logged, and no vk appears in any logged field.
    assert [kind for kind, _ in log_events if kind in ("follow_key", "slacs_foreground")] == []
    for _, fields in log_events:
        assert all(0x42 not in (value,) for value in fields.values())


def test_receiver_pause_stops_counting_and_reports(log_events):
    def responder(payload):
        if "type" in payload and payload["seq"] == 2:
            return {"ok": False, "status": 409, "message": "Stage observation sequence gap."}
        return {"ok": True}

    receiver = RecordingReceiver(responder)
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    observer.send(VK_D)  # accepted
    observer.send(VK_D)  # rejected with 409 -> stop counting
    observer.send(VK_D)  # must not submit after a terminal rejection
    snapshot = follower.snapshot()
    assert snapshot["counted"] == 1 and snapshot["running"] is False
    assert "sequence gap" in snapshot["error"].lower()
    assert len([p for p in receiver.submissions if "type" in p]) == 2
    assert "follow_keys_error" in [kind for kind, _ in log_events]
    follower.stop()


def test_closed_receiver_is_terminal(log_events):
    receiver = RecordingReceiver(lambda payload: {"ok": False, "status": 410, "message": "Receiver is closed"})
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    observer.send(VK_D)
    assert follower.snapshot()["running"] is False
    assert follower.snapshot()["counted"] == 0
    follower.stop()


def test_heartbeat_tick_sends_heartbeat_and_refreshes_foreground(log_events):
    in_front = [True]
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=in_front)
    follower.start()
    follower._heartbeat_tick()
    assert receiver.submissions[-1] == {"version": 1, "session_id": "sid"}
    assert follower.snapshot()["slacs_in_front"] is True
    in_front[0] = False
    follower._heartbeat_tick()
    assert follower.snapshot()["slacs_in_front"] is False
    follower.stop()


def test_heartbeat_thread_runs_and_stop_joins_it():
    receiver = RecordingReceiver()
    observer = ManualObserver()
    follower = SlacsKeyFollower(
        receiver, "sid", step_um=5, observer=observer,
        foreground_provider=lambda: {"pid": 1, "created": 1},
        identifier=lambda pid, created: True, heartbeat_interval=0.02)
    follower.start()
    deadline = time.monotonic() + 3
    while not receiver.submissions and time.monotonic() < deadline:
        time.sleep(0.01)
    assert any(set(p) == {"version", "session_id"} for p in receiver.submissions)
    thread = follower._heartbeat_thread
    follower.stop()
    assert thread is not None and not thread.is_alive()


def test_stop_is_idempotent_and_safe(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    follower.stop()
    follower.stop("again")
    assert observer.stopped is True
    # Events delivered after stop do nothing.
    observer.send(VK_D)
    assert follower.snapshot()["counted"] == 0
    assert [kind for kind, _ in log_events].count("follow_keys_stop") == 1


def test_start_failure_closes_cleanly(log_events):
    receiver = RecordingReceiver()
    follower = SlacsKeyFollower(receiver, "sid", step_um=5, observer=FailingObserver(),
                                heartbeat_interval=None)
    snapshot = follower.start()
    assert snapshot["running"] is False and snapshot["error"]
    assert ("follow_keys_start", {"step_um": 5.0, "step_source": "manual", "ok": False,
                                  "error": snapshot["error"]}) in log_events
    follower.stop()


@pytest.mark.parametrize("step", [0, -1, True, float("nan"), float("inf"), 10001, "10", None])
def test_step_um_is_validated(step):
    receiver = RecordingReceiver()
    with pytest.raises(ValueError):
        SlacsKeyFollower(receiver, "sid", step_um=step, observer=ManualObserver())


@pytest.mark.parametrize("step", [0.001, 1, 10, 9999.9, 10000])
def test_valid_step_sizes_are_accepted(step):
    receiver = RecordingReceiver()
    follower = SlacsKeyFollower(receiver, "sid", step_um=step, observer=ManualObserver(),
                                heartbeat_interval=None)
    assert follower.snapshot()["step_um"] == float(step)


def test_non_windows_without_injection_is_unsupported(log_events):
    receiver = RecordingReceiver()
    follower = SlacsKeyFollower(receiver, "sid", step_um=5, platform="linux")
    snapshot = follower.start()
    assert snapshot["supported"] is False and snapshot["running"] is False
    assert snapshot["source"] == "keys"
    follower.stop()


def test_integration_with_real_receiver_accumulates_displacement():
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    observer = ManualObserver()
    follower = SlacsKeyFollower(
        receiver, descriptor["session_id"], step_um=4, observer=observer,
        foreground_provider=lambda: {"pid": 1, "created": 1},
        identifier=lambda pid, created: True, heartbeat_interval=None)
    try:
        follower.start()
        for vk in (VK_D, VK_D, VK_D, VK_W):
            observer.send(vk)
        snapshot = receiver.snapshot()
        assert snapshot["displacement_um"] == [12.0, -4.0] and snapshot["seq"] == 4
        assert snapshot["status"] == "following"
    finally:
        follower.stop()
        receiver.close()


# -- SLACS Roughness and Mirror ------------------------------------------------


def controls_reading(**overrides):
    """A reading shaped like SlacsControlsMonitor.latest()."""
    return {"ok": True, "roughness_um": 10.0, "mirror_h": False, "mirror_v": False,
            "age_ms": 120.4, "busy": False, "error": None, "reads": 3, **overrides}


class FakeControls:
    """Stands in for SlacsControlsMonitor; the test sets what latest() returns."""

    def __init__(self, reading=None, *, start_result=True, start_error=None, stop_error=None,
                 latest_error=None):
        self.reading = controls_reading() if reading is None else reading
        self.start_result = start_result
        self.start_error = start_error
        self.stop_error = stop_error
        self.latest_error = latest_error
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def stop(self):
        self.calls.append("stop")
        if self.stop_error is not None:
            raise self.stop_error

    def latest(self):
        self.calls.append("latest")
        if self.latest_error is not None:
            raise self.latest_error
        return dict(self.reading) if isinstance(self.reading, dict) else self.reading

    def lifecycle(self):
        return [call for call in self.calls if call != "latest"]


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


def slacs_follower(receiver, controls, **kwargs):
    kwargs.setdefault("in_front", [True])
    return make_follower(receiver, step_source="slacs", controls=controls, **kwargs)


def logged(log_events, kind):
    return [fields for logged_kind, fields in log_events if logged_kind == kind]


def test_default_step_source_is_manual_and_never_touches_controls(log_events):
    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=1.0, mirror_h=True, mirror_v=True))
    follower, observer = make_follower(receiver, in_front=[True], step_um=10, controls=controls)
    follower.start()
    observer.send(VK_D)
    observer.send(VK_W)
    assert [p["delta_um"] for p in receiver.submissions] == [[10.0, 0.0], [0.0, -10.0]]
    snapshot = follower.snapshot()
    follower.stop()
    assert controls.calls == []  # a manual step neither starts, reads nor stops the reader
    assert snapshot["step_source"] == "manual" and snapshot["step_um_last"] == 10.0
    assert all(snapshot[key] is None for key in (
        "roughness_um", "mirror_h", "mirror_v", "controls_ok", "slacs_busy", "reading_age_ms"))
    first = logged(log_events, "follow_key")[0]
    assert first["step_from"] == "manual" and first["step_um"] == 10.0
    assert first["roughness_um"] is None and first["mirror_h"] is None and first["slacs_busy"] is None
    assert logged(log_events, "follow_keys_start")[0]["step_source"] == "manual"
    assert logged(log_events, "follow_keys_start")[0]["controls_started"] is None
    assert logged(log_events, "follow_keys_stop")[0]["step_source"] == "manual"


def test_slacs_step_follows_the_current_roughness_per_press(log_events):
    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=1))
    follower, observer = slacs_follower(receiver, controls, step_um=10)
    follower.start()
    observer.send(VK_D)
    controls.reading = controls_reading(roughness_um=10.0)  # the user moves the slider
    observer.send(VK_D)
    controls.reading = controls_reading(roughness_um=1000.0)  # Coarse
    observer.send(VK_W)
    assert [p["delta_um"] for p in receiver.submissions] == [[1.0, 0.0], [10.0, 0.0], [0.0, -1000.0]]
    keys = logged(log_events, "follow_key")
    assert [k["step_from"] for k in keys] == ["slacs", "slacs", "slacs"]
    assert [k["step_um"] for k in keys] == [1.0, 10.0, 1000.0]
    assert [k["roughness_um"] for k in keys] == [1.0, 10.0, 1000.0]
    snapshot = follower.snapshot()
    assert snapshot["step_um_last"] == 1000.0 and snapshot["step_um"] == 10.0
    assert snapshot["step_source"] == "slacs"
    follower.stop()


@pytest.mark.parametrize("roughness", [None, 0, -1, True, False, float("nan"), float("inf"),
                                       10000.5, 10**400, "10", [10]])
def test_unreadable_roughness_falls_back_to_the_manual_step(log_events, roughness):
    import json

    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=roughness))
    follower, observer = slacs_follower(receiver, controls, step_um=7)
    follower.start()
    observer.send(VK_A)
    assert [p["delta_um"] for p in receiver.submissions] == [[-7.0, 0.0]]
    record = logged(log_events, "follow_key")[0]
    assert record["step_from"] == "fallback" and record["step_um"] == 7.0
    assert record["roughness_um"] is None
    json.dumps(record, allow_nan=False)  # nothing unusable leaks into the session log
    assert follower.snapshot()["roughness_um"] is None
    follower.stop()


def test_slacs_step_without_a_reader_falls_back(log_events):
    receiver = RecordingReceiver()
    follower, observer = slacs_follower(receiver, None, step_um=5)
    follower.start()
    observer.send(VK_S)
    assert [p["delta_um"] for p in receiver.submissions] == [[0.0, 5.0]]
    record = logged(log_events, "follow_key")[0]
    assert record["step_from"] == "fallback" and record["mirror_h"] is None
    snapshot = follower.snapshot()
    assert snapshot["step_source"] == "slacs" and snapshot["controls_ok"] is None
    assert logged(log_events, "follow_keys_start")[0]["controls_started"] is None
    follower.stop()


@pytest.mark.parametrize("failure", ["raises", None, ["not", "a", "dict"], "text"])
def test_a_failing_reader_never_fails_a_press(log_events, failure):
    receiver = RecordingReceiver()
    controls = FakeControls()
    if failure == "raises":
        controls.latest_error = RuntimeError("reader crashed")
    else:
        controls.reading = failure
    follower, observer = slacs_follower(receiver, controls, step_um=4)
    follower.start()
    observer.send(VK_D)
    observer.send(VK_D)
    assert [p["delta_um"] for p in receiver.submissions] == [[4.0, 0.0], [4.0, 0.0]]
    assert [k["step_from"] for k in logged(log_events, "follow_key")] == ["fallback", "fallback"]
    snapshot = follower.snapshot()
    assert snapshot["counted"] == 2 and snapshot["error"] is None and snapshot["running"] is True
    assert snapshot["controls_ok"] is False and snapshot["roughness_um"] is None
    follower.stop()


@pytest.mark.parametrize(("mirror_h", "mirror_v", "expected"), [
    (False, False, [[0.0, -2.0], [0.0, 2.0], [-2.0, 0.0], [2.0, 0.0]]),
    (True, False, [[0.0, 2.0], [0.0, -2.0], [-2.0, 0.0], [2.0, 0.0]]),    # W/S reversed
    (False, True, [[0.0, -2.0], [0.0, 2.0], [2.0, 0.0], [-2.0, 0.0]]),    # A/D reversed
    (True, True, [[0.0, 2.0], [0.0, -2.0], [2.0, 0.0], [-2.0, 0.0]]),
    (None, None, [[0.0, -2.0], [0.0, 2.0], [-2.0, 0.0], [2.0, 0.0]]),     # unknown: not applied
    ("yes", 1, [[0.0, -2.0], [0.0, 2.0], [-2.0, 0.0], [2.0, 0.0]]),       # only True counts
])
def test_mirror_reverses_the_matching_screen_axis(log_events, mirror_h, mirror_v, expected):
    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=2.0, mirror_h=mirror_h, mirror_v=mirror_v))
    follower, observer = slacs_follower(receiver, controls)
    follower.start()
    for vk in (VK_W, VK_S, VK_A, VK_D):
        observer.send(vk)
    deltas = [p["delta_um"] for p in receiver.submissions]
    assert deltas == expected
    # No -0.0 reaches the receiver or the log.
    assert all(math.copysign(1.0, value) == 1.0 for delta in deltas for value in delta if value == 0)
    record = logged(log_events, "follow_key")[0]
    assert record["mirror_h"] is (mirror_h if isinstance(mirror_h, bool) else None)
    assert record["mirror_v"] is (mirror_v if isinstance(mirror_v, bool) else None)
    follower.stop()


def test_mirror_still_applies_on_the_fallback_step(log_events):
    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=None, mirror_h=True))
    follower, observer = slacs_follower(receiver, controls, step_um=3)
    follower.start()
    observer.send(VK_W)
    assert [p["delta_um"] for p in receiver.submissions] == [[0.0, 3.0]]
    follower.stop()


def test_mirror_and_the_view_sign_compose(log_events, monkeypatch):
    monkeypatch.setattr(stage_keys, "KEY_VIEW_SIGN", -1)
    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=2.0, mirror_v=True))
    follower, observer = slacs_follower(receiver, controls)
    follower.start()
    for vk in (VK_W, VK_D):
        observer.send(vk)
    assert [p["delta_um"] for p in receiver.submissions] == [[0.0, 2.0], [2.0, 0.0]]
    follower.stop()


def test_follow_key_record_carries_the_reading_timing_and_pixels(log_events):
    clock = FakeClock(50.0)
    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=10, age_ms=120.6, busy=False))
    follower, observer = slacs_follower(receiver, controls, clock=clock, pixel_size_um=(0.5, 0.3))
    follower.start()
    observer.send(VK_D)
    observer.send(VK_D, down=False)
    clock.now = 50.25
    controls.reading = controls_reading(roughness_um=1, mirror_h=True, ok=False, busy=True, age_ms=900)
    observer.send(VK_W)
    first, second = logged(log_events, "follow_key")
    assert first == {"key": "D", "seq": 1, "delta_um": [10.0, 0.0], "step_um": 10.0,
                     "step_from": "slacs", "roughness_um": 10.0, "mirror_h": False,
                     "mirror_v": False, "reading_age_ms": 121, "slacs_busy": False,
                     "repeat": False, "gap_ms": None, "delta_px": [20.0, 0.0]}
    # While SLACS is busy, its last good Roughness still drives the press.
    assert second == {"key": "W", "seq": 2, "delta_um": [0.0, 1.0], "step_um": 1.0,
                      "step_from": "slacs", "roughness_um": 1.0, "mirror_h": True,
                      "mirror_v": False, "reading_age_ms": 900, "slacs_busy": True,
                      "repeat": False, "gap_ms": 250, "delta_px": [0.0, 3.33]}
    follower.stop()


def test_delta_px_is_none_without_a_pixel_size(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True])
    follower.start()
    observer.send(VK_D)
    assert logged(log_events, "follow_key")[0]["delta_px"] is None
    follower.stop()


def test_delta_px_accepts_a_list_and_never_logs_negative_zero(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], step_um=0.001,
                                       pixel_size_um=[1.0, 1.0])
    follower.start()
    observer.send(VK_A)  # -0.001 px rounds to zero
    assert logged(log_events, "follow_key")[0]["delta_px"] == [0.0, 0.0]
    assert all(math.copysign(1.0, v) == 1.0 for v in logged(log_events, "follow_key")[0]["delta_px"])
    follower.stop()


@pytest.mark.parametrize("pixel_size", [(0, 1), (1, -1), (float("nan"), 1), (1, float("inf")),
                                        (1,), (1, 2, 3), "ab", (True, 1), [1, "2"], 1.0, {"x": 1}])
def test_pixel_size_um_is_validated(pixel_size):
    with pytest.raises(ValueError):
        SlacsKeyFollower(RecordingReceiver(), "sid", step_um=5, observer=ManualObserver(),
                         pixel_size_um=pixel_size)


@pytest.mark.parametrize("source", ["SLACS", "auto", "", None, 1])
def test_step_source_is_validated(source):
    with pytest.raises(ValueError):
        SlacsKeyFollower(RecordingReceiver(), "sid", step_um=5, observer=ManualObserver(),
                         step_source=source)


def test_follower_owns_the_controls_lifecycle(log_events):
    controls = FakeControls()
    follower, _observer = slacs_follower(RecordingReceiver(), controls)
    follower.start()
    follower.start()  # idempotent: the reader is started once
    assert controls.lifecycle() == ["start"]
    follower.stop()
    follower.stop("again")
    assert controls.lifecycle() == ["start", "stop"]
    assert logged(log_events, "follow_keys_start") == [
        {"step_um": 10.0, "step_source": "slacs", "ok": True, "controls_started": True}]
    stop = logged(log_events, "follow_keys_stop")
    assert len(stop) == 1 and stop[0]["step_source"] == "slacs"


def test_controls_start_exception_is_swallowed_and_logged(log_events):
    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=None),
                            start_error=RuntimeError("no trackbar"))
    follower, observer = slacs_follower(receiver, controls, step_um=6)
    snapshot = follower.start()
    assert snapshot["running"] is True and snapshot["error"] is None
    assert logged(log_events, "follow_keys_controls_error") == [
        {"message": "Could not start the SLACS controls reader: no trackbar"}]
    assert logged(log_events, "follow_keys_start")[0]["controls_started"] is False
    observer.send(VK_D)  # Follow keeps working on the manual step
    assert [p["delta_um"] for p in receiver.submissions] == [[6.0, 0.0]]
    follower.stop()
    assert controls.lifecycle() == ["start", "stop"]


def test_controls_that_cannot_start_are_reported(log_events):
    controls = FakeControls(start_result=False)
    follower, _observer = slacs_follower(RecordingReceiver(), controls)
    assert follower.start()["running"] is True
    assert logged(log_events, "follow_keys_start")[0]["controls_started"] is False
    assert logged(log_events, "follow_keys_controls_error") == []
    follower.stop()


def test_controls_stop_failure_never_escapes(log_events):
    controls = FakeControls(stop_error=RuntimeError("join failed"))
    follower, observer = slacs_follower(RecordingReceiver(), controls)
    follower.start()
    follower.stop()  # must not raise
    assert observer.stopped is True
    assert len(logged(log_events, "follow_keys_stop")) == 1


def test_observer_failure_stops_the_controls(log_events):
    controls = FakeControls()
    follower = SlacsKeyFollower(RecordingReceiver(), "sid", step_um=5, observer=FailingObserver(),
                                heartbeat_interval=None, step_source="slacs", controls=controls)
    snapshot = follower.start()
    assert snapshot["running"] is False and snapshot["error"]
    assert controls.lifecycle() == ["start", "stop"]
    assert ("follow_keys_start", {"step_um": 5.0, "step_source": "slacs", "ok": False,
                                  "error": snapshot["error"]}) in log_events
    follower.stop("start_failed")  # what the overlay does next; still safe


def test_unsupported_platform_never_starts_the_controls(log_events):
    controls = FakeControls()
    follower = SlacsKeyFollower(RecordingReceiver(), "sid", step_um=5, platform="linux",
                                step_source="slacs", controls=controls)
    snapshot = follower.start()
    assert snapshot["supported"] is False and snapshot["running"] is False
    assert "start" not in controls.calls
    assert logged(log_events, "follow_keys_start") == [
        {"step_um": 5.0, "step_source": "slacs", "supported": False}]
    follower.stop()
    assert "start" not in controls.calls


def test_stop_racing_controls_start_leaves_the_reader_stopped(log_events):
    follower_box = []

    class StopsDuringStart(FakeControls):
        def start(self):
            result = super().start()
            follower_box[0].stop("closed")  # another thread stops Follow meanwhile
            return result

    controls = StopsDuringStart()
    follower, _observer = slacs_follower(RecordingReceiver(), controls)
    follower_box.append(follower)
    follower.start()
    assert controls.lifecycle() == ["start", "stop", "stop"]  # stop()'s, then start()'s re-check
    assert follower.snapshot()["running"] is False


def test_snapshot_reports_the_controls_as_they_read_now(log_events):
    receiver = RecordingReceiver()
    controls = FakeControls(controls_reading(roughness_um=10, mirror_h=True, age_ms=33.6))
    follower, observer = slacs_follower(receiver, controls)
    follower.start()
    snapshot = follower.snapshot()
    assert {key: snapshot[key] for key in (
        "step_source", "step_um_last", "roughness_um", "mirror_h", "mirror_v", "controls_ok",
        "slacs_busy", "reading_age_ms")} == {
        "step_source": "slacs", "step_um_last": None, "roughness_um": 10.0, "mirror_h": True,
        "mirror_v": False, "controls_ok": True, "slacs_busy": False, "reading_age_ms": 34}
    # SLACS's UI stops answering: the reader keeps the last good values and says busy.
    controls.reading = controls_reading(roughness_um=10, mirror_h=True, ok=False, busy=True,
                                        age_ms=2500.2, error="timeout")
    observer.send(VK_D)
    snapshot = follower.snapshot()
    assert snapshot["slacs_busy"] is True and snapshot["controls_ok"] is False
    assert snapshot["roughness_um"] == 10.0 and snapshot["reading_age_ms"] == 2500
    assert snapshot["step_um_last"] == 10.0
    assert logged(log_events, "follow_key")[0]["slacs_busy"] is True
    # Unknown or malformed fields read as None, never as a guess.
    controls.reading = {"roughness_um": 10, "mirror_h": "on", "age_ms": float("nan"), "busy": 0}
    snapshot = follower.snapshot()
    assert snapshot["mirror_h"] is None and snapshot["reading_age_ms"] is None
    assert snapshot["slacs_busy"] is None and snapshot["controls_ok"] is None
    follower.stop()


# -- key holds and autorepeat --------------------------------------------------


def test_autorepeat_presses_are_marked_and_the_hold_is_logged_on_key_up(log_events):
    clock = FakeClock(10.0)
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], clock=clock)
    follower.start()
    for now in (10.0, 10.5, 10.53, 10.56):  # initial repeat delay, then the repeat rate
        clock.now = now
        observer.send(VK_D)
    clock.now = 10.6
    observer.send(VK_D, down=False)
    keys = logged(log_events, "follow_key")
    assert [k["repeat"] for k in keys] == [False, True, True, True]
    assert [k["gap_ms"] for k in keys] == [None, 500, 30, 30]
    # Timed from the first counted press to the last, not to the key-up.
    assert logged(log_events, "follow_key_hold") == [
        {"key": "D", "presses": 4, "seconds": 0.56, "key_up": True}]
    assert len(receiver.submissions) == 4  # the key-up itself moves nothing
    follower.stop()


def test_taps_are_not_logged_as_holds(log_events):
    clock = FakeClock()
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], clock=clock)
    follower.start()
    for _ in range(3):
        observer.send(VK_D)
        clock.now += 0.05
        observer.send(VK_D, down=False)
        clock.now += 0.1
    assert [k["repeat"] for k in logged(log_events, "follow_key")] == [False, False, False]
    assert logged(log_events, "follow_key_hold") == []
    follower.stop()


def test_a_hold_outside_slacs_is_never_logged(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[False], clock=FakeClock())
    follower.start()
    for _ in range(4):
        observer.send(VK_D)
    observer.send(VK_D, down=False)
    assert logged(log_events, "follow_key") == [] and logged(log_events, "follow_key_hold") == []
    assert follower.snapshot()["ignored_not_slacs"] == 4
    follower.stop()


def test_a_hold_counts_only_the_presses_made_in_slacs(log_events):
    in_front = [False]
    clock = FakeClock()
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=in_front, clock=clock)
    follower.start()
    observer.send(VK_D)  # outside SLACS: tallied only
    in_front[0] = True
    for _ in range(2):
        clock.now += 0.04
        observer.send(VK_D)
    clock.now += 0.04
    observer.send(VK_D, down=False)
    assert [k["repeat"] for k in logged(log_events, "follow_key")] == [True, True]
    # The key-down made outside SLACS does not start the hold's time either.
    assert logged(log_events, "follow_key_hold") == [
        {"key": "D", "presses": 2, "seconds": 0.04, "key_up": True}]
    follower.stop()


def test_a_modified_hold_counts_only_after_the_modifier_is_released(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], clock=FakeClock())
    follower.start()
    observer.send(VK_D, modifiers=("ctrl",))
    observer.send(VK_D, modifiers=("ctrl",))
    observer.send(VK_D)  # Ctrl released while D keeps repeating
    observer.send(VK_D, down=False, modifiers=("ctrl",))
    assert [k["repeat"] for k in logged(log_events, "follow_key")] == [True]
    assert logged(log_events, "follow_key_hold")[0]["presses"] == 1
    follower.stop()


def test_a_lost_key_up_ends_the_hold_at_the_next_press(log_events):
    clock = FakeClock(0.0)
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], clock=clock)
    follower.start()
    observer.send(VK_D)
    clock.now = 0.5
    observer.send(VK_D)
    clock.now = 0.5 + stage_keys.HOLD_RESET_SECONDS + 0.01  # its key-up never arrived
    observer.send(VK_D)
    assert [k["repeat"] for k in logged(log_events, "follow_key")] == [False, True, False]
    assert logged(log_events, "follow_key_hold") == [
        {"key": "D", "presses": 2, "seconds": 0.5, "key_up": False}]
    kinds = [kind for kind, _ in log_events if kind.startswith("follow_key")]
    assert kinds[-2:] == ["follow_key_hold", "follow_key"]  # logged before the new press
    clock.now += 0.03
    observer.send(VK_D)
    observer.send(VK_D, down=False)
    assert logged(log_events, "follow_key_hold")[-1]["key_up"] is True
    follower.stop()


def test_a_lost_hold_is_not_logged_at_a_press_outside_slacs(log_events):
    in_front = [True]
    clock = FakeClock(0.0)
    follower, observer = make_follower(RecordingReceiver(), in_front=in_front, clock=clock)
    follower.start()
    for now in (0.0, 0.5, 0.53):
        clock.now = now
        observer.send(VK_D)
    in_front[0] = False  # D's key-up was lost; much later a "d" is typed in another program
    clock.now = 60.0
    before = len(log_events)
    observer.send(VK_D)
    assert [kind for kind, _ in log_events[before:] if kind.startswith("follow_key")] == []
    assert follower.snapshot()["ignored_not_slacs"] == 1
    observer.send(VK_D, down=False)
    assert logged(log_events, "follow_key_hold") == []
    follower.stop()


def test_a_hold_released_in_another_program_is_not_logged(log_events):
    in_front = [True]
    clock = FakeClock(0.0)
    follower, observer = make_follower(RecordingReceiver(), in_front=in_front, clock=clock)
    follower.start()
    for now in (0.0, 0.5, 0.53):
        clock.now = now
        observer.send(VK_D)
    in_front[0] = False  # the foreground moves on while D is still held and repeating
    for now in (0.56, 0.59, 0.62):
        clock.now = now
        observer.send(VK_D)
    clock.now = 0.65
    observer.send(VK_D, down=False)
    assert logged(log_events, "follow_key_hold") == []
    assert follower.snapshot()["ignored_not_slacs"] == 3
    follower.stop()


def test_a_lost_hold_is_timed_by_its_presses_in_slacs(log_events):
    in_front = [True]
    clock = FakeClock(0.0)
    follower, observer = make_follower(RecordingReceiver(), in_front=in_front, clock=clock)
    follower.start()
    for now in (0.0, 0.5):
        clock.now = now
        observer.send(VK_D)
    in_front[0] = False
    for now in (0.53, 0.56):  # repeats delivered to another program
        clock.now = now
        observer.send(VK_D)
    in_front[0] = True  # the key-up was lost; D is pressed again in SLACS
    clock.now = 10.0
    observer.send(VK_D)
    assert logged(log_events, "follow_key_hold") == [
        {"key": "D", "presses": 2, "seconds": 0.5, "key_up": False}]
    follower.stop()


def test_two_held_keys_are_tracked_separately(log_events):
    clock = FakeClock(0.0)
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], clock=clock)
    follower.start()
    for now, vk in ((0.0, VK_D), (0.5, VK_D), (0.6, VK_W), (1.1, VK_W)):
        clock.now = now
        observer.send(vk)
    clock.now = 1.2
    observer.send(VK_W, down=False)
    clock.now = 1.5
    observer.send(VK_D, down=False)  # D stopped repeating once W went down, but stayed held
    assert [k["repeat"] for k in logged(log_events, "follow_key")] == [False, True, False, True]
    assert logged(log_events, "follow_key_hold") == [
        {"key": "W", "presses": 2, "seconds": 0.5, "key_up": True},
        {"key": "D", "presses": 2, "seconds": 0.5, "key_up": True}]
    follower.stop()


def test_a_hold_released_after_stop_is_not_logged(log_events):
    receiver = RecordingReceiver()
    follower, observer = make_follower(receiver, in_front=[True], clock=FakeClock())
    follower.start()
    observer.send(VK_D)
    observer.send(VK_D)
    follower.stop()
    observer.send(VK_D, down=False)
    assert logged(log_events, "follow_key_hold") == []


def test_non_jog_keys_are_dropped_before_any_state_is_touched(log_events):
    clock = FakeClock()
    controls = FakeControls()
    receiver = RecordingReceiver()
    follower, observer = slacs_follower(receiver, controls, clock=clock)
    follower.start()
    clock.calls = 0
    controls.calls.clear()
    logged_before = len(log_events)
    for vk in (0x42, 0x50, 0x0D, 0x1B, 0x25, 0x51, 0x45, 0x5A, 0x31, 0x20):
        observer.send(vk, down=True)
        observer.send(vk, down=False)
    # Not even timed: no clock read, no hold, no controls read, nothing logged.
    assert clock.calls == 0 and follower._held == {} and controls.calls == []
    assert receiver.submissions == [] and len(log_events) == logged_before
    follower.stop()


def test_slacs_step_with_the_real_receiver_accumulates_mirrored_roughness():
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    observer = ManualObserver()
    controls = FakeControls(controls_reading(roughness_um=1, mirror_v=True))
    follower = SlacsKeyFollower(
        receiver, descriptor["session_id"], step_um=10, observer=observer,
        foreground_provider=lambda: {"pid": 1, "created": 1},
        identifier=lambda pid, created: True, heartbeat_interval=None,
        step_source="slacs", controls=controls, pixel_size_um=(0.65, 0.65))
    try:
        follower.start()
        for vk in (VK_D, VK_D, VK_W):
            observer.send(vk)
        snapshot = receiver.snapshot()
        assert snapshot["displacement_um"] == [-2.0, -1.0] and snapshot["seq"] == 3
    finally:
        follower.stop()
        receiver.close()
    assert controls.lifecycle() == ["start", "stop"]


# -- Ctrl/Alt/Win tracking from the raw stream ---------------------------------

VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_LCONTROL = 0x11, 0x12, 0x5B, 0x5C, 0xA2
RI_KEY_BREAK, RI_KEY_E0 = 0x01, 0x02


def raw_observer():
    """A Raw Input observer driven through its keystroke router (no Win32)."""
    observer = _RawInputKeyObserver()
    delivered = []
    observer._on_event = lambda vk, down, modifiers: delivered.append((vk, down, modifiers))
    return observer, delivered


def test_modifiers_are_tracked_from_make_and_break_in_stream_order():
    observer, delivered = raw_observer()
    observer._handle_key(VK_CONTROL, 0)             # left Ctrl down
    observer._handle_key(VK_D, 0)                   # Ctrl+D
    observer._handle_key(VK_CONTROL, RI_KEY_BREAK)  # left Ctrl up
    observer._handle_key(VK_D, 0)                   # plain D
    observer._handle_key(VK_MENU, RI_KEY_E0)        # right Alt down
    observer._handle_key(VK_W, 0)
    observer._handle_key(VK_MENU, RI_KEY_E0 | RI_KEY_BREAK)
    observer._handle_key(VK_LWIN, 0)
    observer._handle_key(VK_S, 0)
    observer._handle_key(VK_LWIN, RI_KEY_BREAK)
    observer._handle_key(VK_A, RI_KEY_BREAK)
    assert delivered == [(VK_D, True, ("ctrl",)), (VK_D, True, ()), (VK_W, True, ("alt",)),
                         (VK_S, True, ("win",)), (VK_A, False, ())]


def test_one_side_released_keeps_the_other_side_held():
    observer, delivered = raw_observer()
    observer._handle_key(VK_CONTROL, 0)                          # left Ctrl down
    observer._handle_key(VK_CONTROL, RI_KEY_E0)                  # right Ctrl down
    observer._handle_key(VK_CONTROL, RI_KEY_BREAK)               # left Ctrl up
    observer._handle_key(VK_D, 0)
    observer._handle_key(VK_CONTROL, RI_KEY_E0 | RI_KEY_BREAK)   # right Ctrl up
    observer._handle_key(VK_LCONTROL, 0)                         # sided code, same left key
    observer._handle_key(VK_CONTROL, RI_KEY_BREAK)
    observer._handle_key(VK_D, 0)
    assert delivered == [(VK_D, True, ("ctrl",)), (VK_D, True, ())]


def test_raw_observer_forwards_only_jog_keys():
    observer, delivered = raw_observer()
    for vk in (0x42, 0x50, 0x0D, 0x1B, 0x25, 0x10, VK_CONTROL, VK_MENU, VK_RWIN, 0x31, 0x20):
        observer._handle_key(vk, 0)
        observer._handle_key(vk, RI_KEY_BREAK)
    assert delivered == []


def test_ctrl_jog_through_raw_observer_is_not_counted(log_events):
    receiver = RecordingReceiver()
    observer, _ = raw_observer()
    follower = SlacsKeyFollower(receiver, "sid", step_um=3, observer=ManualObserver(),
                                foreground_provider=lambda: {"pid": 1, "created": 1},
                                identifier=lambda pid, created: True, heartbeat_interval=None)
    follower.start()
    observer._on_event = follower._on_event
    observer._handle_key(VK_CONTROL, 0)
    observer._handle_key(VK_D, 0)
    observer._handle_key(VK_D, RI_KEY_BREAK)
    observer._handle_key(VK_CONTROL, RI_KEY_BREAK)
    observer._handle_key(VK_D, 0)
    assert [p["delta_um"] for p in receiver.submissions] == [[3.0, 0.0]]
    follower.stop()


def test_stale_modifier_hold_is_rechecked_against_the_physical_key():
    now = [100.0]
    physically = {"down": True}
    tracker = _ModifierTracker(physically_down=lambda side: physically["down"],
                               clock=lambda: now[0], stale_after=2.0)
    tracker.update(VK_CONTROL, True)
    now[0] += 1.0
    physically["down"] = False
    assert tracker.held() == ("ctrl",)   # recent hold: trust the stream, not the async state
    now[0] += 5.0
    physically["down"] = True
    assert tracker.held() == ("ctrl",)   # still physically down: keep it
    physically["down"] = False
    assert tracker.held() == ()          # its break was lost (e.g. lock screen): clear it
    assert tracker.held() == ()


def test_autorepeat_refreshes_a_modifier_hold():
    now = [0.0]
    tracker = _ModifierTracker(physically_down=lambda side: False, clock=lambda: now[0],
                               stale_after=2.0)
    tracker.update(VK_MENU, True)
    now[0] = 1.5
    tracker.update(VK_MENU, True)  # held: the key autorepeats
    now[0] = 3.0
    assert tracker.held() == ("alt",)


# -- one process-wide Raw Input registration ----------------------------------


class FakeNativeObserver:
    instances: list = []

    def __init__(self, ok=True):
        self.ok = ok
        self.on_event = None
        self.starts = 0
        self.stops = 0
        self.dead = False
        self.error = None if ok else "RegisterRawInputDevices failed (Windows error 5)"
        FakeNativeObserver.instances.append(self)

    def start(self, on_event):
        self.starts += 1
        self.on_event = on_event
        return self.ok

    def stop(self):
        self.stops += 1

    def alive(self):
        return not self.dead


def test_hub_registers_once_fans_out_and_stops_after_the_last_subscriber():
    FakeNativeObserver.instances = []
    hub = _RawInputHub(FakeNativeObserver)
    first_events, second_events = [], []
    first, second = _SharedKeyObserver(hub), _SharedKeyObserver(hub)
    assert first.start(lambda *event: first_events.append(event)) is True
    assert second.start(lambda *event: second_events.append(event)) is True
    assert len(FakeNativeObserver.instances) == 1  # one registration for the process
    native = FakeNativeObserver.instances[0]
    native.on_event(VK_D, True, ())
    assert first_events == second_events == [(VK_D, True, ())]
    first.stop()
    first.stop()  # idempotent
    assert native.stops == 0  # the other overlay still follows
    native.on_event(VK_W, True, ())
    assert first_events == [(VK_D, True, ())] and second_events[-1] == (VK_W, True, ())
    second.stop()
    assert native.stops == 1
    # A later follower starts a fresh registration.
    third = _SharedKeyObserver(hub)
    assert third.start(lambda *event: None) is True
    assert len(FakeNativeObserver.instances) == 2
    third.stop()


def test_two_followers_share_the_hub_and_both_count(log_events):
    FakeNativeObserver.instances = []
    hub = _RawInputHub(FakeNativeObserver)
    receivers = [RecordingReceiver(), RecordingReceiver()]
    followers = [SlacsKeyFollower(receiver, f"sid{i}", step_um=2, observer=_SharedKeyObserver(hub),
                                  foreground_provider=lambda: {"pid": 1, "created": 1},
                                  identifier=lambda pid, created: True, heartbeat_interval=None)
                 for i, receiver in enumerate(receivers)]
    for follower in followers:
        follower.start()
    native = FakeNativeObserver.instances[0]
    native.on_event(VK_D, True, ())
    followers[0].stop()
    native.on_event(VK_W, True, ())
    assert [p["delta_um"] for p in receivers[0].submissions] == [[2.0, 0.0]]
    assert [p["delta_um"] for p in receivers[1].submissions] == [[2.0, 0.0], [0.0, -2.0]]
    followers[1].stop()
    assert native.stops == 1


def test_hub_restarts_an_observer_whose_loop_ended():
    FakeNativeObserver.instances = []
    hub = _RawInputHub(FakeNativeObserver)
    first = _SharedKeyObserver(hub)
    first.start(lambda *event: None)
    FakeNativeObserver.instances[0].dead = True
    second = _SharedKeyObserver(hub)
    assert second.start(lambda *event: None) is True
    assert len(FakeNativeObserver.instances) == 2 and FakeNativeObserver.instances[0].stops == 1
    first.stop()
    second.stop()
    assert FakeNativeObserver.instances[1].stops == 1


def test_hub_start_failure_reports_the_native_error(log_events):
    hub = _RawInputHub(lambda: FakeNativeObserver(ok=False))
    follower = SlacsKeyFollower(RecordingReceiver(), "sid", step_um=5, observer=_SharedKeyObserver(hub),
                                heartbeat_interval=None)
    snapshot = follower.start()
    assert snapshot["running"] is False
    assert snapshot["error"] == ("Could not start the keystroke observer: "
                                 "RegisterRawInputDevices failed (Windows error 5)")
    follower.stop()


def test_default_windows_follower_subscribes_to_the_shared_hub():
    follower = SlacsKeyFollower(RecordingReceiver(), "sid", step_um=5, platform="win32",
                                foreground_provider=lambda: None, identifier=lambda pid, created: False)
    assert isinstance(follower._observer, _SharedKeyObserver)
    assert follower._observer._hub is stage_keys._RAW_INPUT_HUB
