"""The W/A/S/D key follower observes passively and never records other keys."""

from __future__ import annotations

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
    assert ("follow_keys_start", {"step_um": 5.0, "ok": False, "error": snapshot["error"]}) in log_events
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
