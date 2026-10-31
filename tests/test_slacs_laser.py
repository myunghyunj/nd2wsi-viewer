"""Delivery is ordered and bounded; the Raw Input callback never takes the overlay lock."""
import threading
import time
from unittest.mock import Mock

import pytest

from nd2wsi import slacs_laser as laser
from nd2wsi.translucent_window import TranslucentApi


def wait_for(predicate):
    end = time.monotonic() + 2
    while not predicate() and time.monotonic() < end:
        time.sleep(.005)
    assert predicate()


def test_ordered_delivery_accepts_only_current_ack_and_relays_confirmed_items():
    sent, confirmed = [], []
    worker = None

    def deliver(payload):
        sent.append(payload["press"])
        reply = {"token": payload["token"], "press": payload["press"], "decision": "added", "reason": "new_burst",
                 "id": "laser", "item": {"id": "laser", "type": "box", "slacs": {"kind": "laser_shot"}}}
        assert not worker.accept({**reply, "token": "old"})["ok"]
        assert worker.accept(reply)["ok"]
        assert not worker.accept(reply)["ok"]
        return True

    worker = laser.LaserDelivery(prepare=lambda r: (r, None), deliver=deliver,
                                 confirmed=lambda r: confirmed.append(r["press"]))
    try:
        for press in range(1, 5):
            worker.enqueue({"press": press})
        wait_for(lambda: len(confirmed) == 4)
        assert sent == confirmed == [1, 2, 3, 4]
    finally:
        worker.close()


@pytest.mark.parametrize("stall_phase", ["delivery", "confirmation"])
def test_stall_logs_all_skipped_presses_then_rearms_without_overlapping_calls(monkeypatch, stall_phase):
    monkeypatch.setattr(laser, "DELIVERY_WAIT", .04)
    events, sent, fresh = [], [], threading.Event()
    hang = threading.Event()
    monkeypatch.setattr(laser.session_log, "event", lambda kind, **data: events.append(data))
    worker = None

    def deliver(payload):
        sent.append(payload["press"])
        if payload["press"] == 1 and stall_phase == "delivery":
            hang.wait(2)
        result = worker.accept({"token": payload["token"], "press": payload["press"], "decision": "added",
            "id": "laser", "item": {"id": "laser", "type": "box", "slacs": {"kind": "laser_shot"}}})
        if payload["press"] == 1 and stall_phase == "delivery":
            assert not result["ok"]  # the timed-out token is retired
        return True

    def confirm(payload):
        if payload["press"] == 1 and stall_phase == "confirmation":
            hang.wait(2)

    worker = laser.LaserDelivery(prepare=lambda r: (r, None) if fresh.is_set() else (None, "stale_view"),
                                 deliver=deliver, confirmed=confirm)
    try:
        worker.enqueue({"press": 1})
        worker.enqueue({"press": 2})
        assert not sent
        fresh.set()
        worker.view_changed()
        wait_for(lambda: worker.status == "delivery_disabled")
        for press in (3, 4):
            worker.enqueue({"press": press})
        wait_for(lambda: sum(e.get("status") == "delivery_disabled" for e in events) == 3)
        assert sent == [1] and not worker._closed.is_set()
        assert [e["press"] for e in events if e.get("status") == "delivery_disabled"] == [2, 3, 4]
        hang.set()
        wait_for(lambda: worker.status == "ready")
        worker.enqueue({"press": 5})
        wait_for(lambda: any(e.get("press") == 5 and e.get("decision") == "added" for e in events))
        assert sent == [1, 5]
    finally:
        hang.set()
        worker.close()


def test_prepare_uses_fresh_view_not_press_coordinates_and_checks_receiver_lock_sequence():
    overlay = TranslucentApi({"query": "?g=pixels&p=2", "pixel_size_um": [2, .5]}, logger=Mock(), on_closed=Mock())
    overlay._stage_follow = Mock()
    overlay._stage_follow_receiver_id = "receiver"
    overlay._locked = True
    record = {"receiver_id": "receiver", "press": 1, "follow_seq": 8, "locked": True,
              "at": "2026-01-01T00:00:00Z", "target_px": [-9, -9]}
    view = {"version": 1, "center_px": [20, 30], "zoom": 2, "locked": True,
            "follow_session": "receiver", "follow_seq": 7,
            "target_px": [25, 35], "target_offset_css": [10, 10], "target_side_um": 18,
            "target_rotation_deg": 45, "target_calibrated": False,
            "target_corners_px": [[20,30],[30,30],[30,40],[20,40]]}
    assert overlay.report_overlay_view(view)["ok"]
    assert overlay._prepare_laser_box(record) == (None, "stale_view")
    assert overlay.report_overlay_view({**view, "follow_seq": 8})["ok"]
    payload, reason = overlay._prepare_laser_box(record)
    assert reason is None and payload["target_px"] == [25, 35] and payload["p"] == 2
    assert payload["pixel_size_um"] == [2, .5]
    overlay.report_overlay_view({**view, "follow_seq": 9})
    assert overlay._prepare_laser_box(record) == (None, "view_moved")
    assert overlay._prepare_laser_box({**record, "receiver_id": "old"}) == (None, "session_changed")
    overlay._locked = False
    assert overlay._prepare_laser_box(record) == (None, "unlocked")
    overlay._locked = True
    overlay._is_active = lambda: False
    assert overlay._prepare_laser_box(record) == (None, "not_active_overlay")


def test_raw_hook_never_needs_overlay_lock():
    overlay = TranslucentApi({}, logger=Mock(), on_closed=Mock())
    overlay._laser_delivery = Mock()
    complete = threading.Event()
    with overlay._lock:
        thread = threading.Thread(target=lambda: (overlay._on_mark_record({"press": 1}), complete.set()), daemon=True)
        thread.start()
        assert complete.wait(1)
    overlay._laser_delivery.enqueue.assert_called_once()


@pytest.mark.parametrize("value", [{}, {"isolation_highwater": True, "presses": {}},
    {"isolation_highwater": 1, "presses": {"receiver": float("nan")}},
    {"isolation_highwater": 1, "presses": {"bad/path": 1}}])
def test_sidecar_metadata_rejects_unbounded_or_malformed_values(value):
    with pytest.raises(ValueError):
        laser.metadata(value)


def test_lock_and_realign_baseline_survives_refused_report(monkeypatch):
    events = []
    monkeypatch.setattr(laser.session_log, "event", lambda kind, **data: events.append((kind, data)))
    overlay = TranslucentApi({}, logger=Mock(), on_closed=Mock())
    def report(center, locked):
        assert overlay.report_overlay_view({"version": 1, "center_px": center, "zoom": 1, "locked": locked})["ok"]
    report([10, 20], False)
    report([10, 20], True)
    report([15, 25], True)
    assert not overlay.report_overlay_view({"version": 100})["ok"]
    report([99, 99], False)
    report([30, 10], False)
    report([30, 10], True)
    assert [event[1]["locked"] for event in events if event[0] == "overlay_lock"] == [True, False, True]
    assert events[-1] == ("overlay_realign", {"from_px": [15,25], "to_px": [30,10], "delta_px": [15,-15]})


def test_receiver_urlsafe_identifier_is_preserved_in_metadata():
    value = {"isolation_highwater": 8, "presses": {"receiver_with-hyphen": 55}}
    assert laser.metadata(value)["slacs"] == value


def test_delayed_press_rejects_a_hand_realigned_view_with_the_same_follow_sequence():
    overlay = TranslucentApi({}, logger=Mock(), on_closed=Mock())
    overlay._stage_follow = Mock()
    overlay._stage_follow_receiver_id = "receiver"
    overlay._locked = True
    overlay._overlay_view = {"follow_session": "receiver", "follow_seq": 8, "locked": True, "at": 11}
    record = {"receiver_id": "receiver", "press": 1, "follow_seq": 8, "locked": True, "observed_at": 10}
    assert overlay._prepare_laser_box(record) == (None, "view_after_press")


@pytest.mark.parametrize("running", [True, False])
def test_laser_worker_is_lazy_reused_and_closed_on_failed_start_or_window_exit(monkeypatch, running):
    from nd2wsi import stage_follow, stage_keys

    receiver = Mock()
    receiver.start.return_value = {"session_id": "receiver"}
    follower = Mock()
    follower.start.return_value = {"running": running}
    monkeypatch.setattr(stage_follow, "StageFollowReceiver", lambda: receiver)
    monkeypatch.setattr(stage_keys, "SlacsKeyFollower", lambda *args, **kw: follower)
    worker = Mock()
    factory = Mock(return_value=worker)
    monkeypatch.setattr(laser, "LaserDelivery", factory)
    overlay = TranslucentApi({"pixel_size_um": [1, 1]}, logger=Mock(), on_closed=Mock(), on_laser_box=Mock())
    factory.assert_not_called()
    overlay._slacs_snapshot = {"available": True, "session_id": "process"}
    result = overlay.start_stage_follow({"source": "keys", "step_source": "manual"})
    assert result["ok"] is running
    factory.assert_called_once()
    if running:
        overlay.stop_stage_follow()
        worker.close.assert_not_called()  # keep one serialized channel across Follow runs
        overlay.start_stage_follow({"source": "keys", "step_source": "manual"})
        factory.assert_called_once()
        overlay._did_close()
    worker.close.assert_called_once()
