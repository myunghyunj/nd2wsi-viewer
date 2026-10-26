"""The local stage-follow receiver handles observations, never hardware control."""

from __future__ import annotations

import http.client
import json
import socket
import time
from urllib.parse import urlsplit

import pytest

from nd2wsi.stage_follow import MAX_BODY_BYTES, MAX_SEQUENCE, StageFollowReceiver


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def running():
    clock = Clock()
    receiver = StageFollowReceiver(clock=clock)
    descriptor = receiver.start()
    try:
        yield receiver, descriptor, clock
    finally:
        receiver.close()


def request(descriptor, payload, *, path="/motion", headers=None, method="POST"):
    url = urlsplit(descriptor["url"])
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=2)
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    supplied = {"Authorization": f"Bearer {descriptor['token']}", "Content-Type": "application/json"}
    supplied.update(headers or {})
    try:
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        connection.close()


def motion(descriptor, seq, values, kind="delta"):
    return {"version": 1, "session_id": descriptor["session_id"], "seq": seq, "type": kind,
            "delta_um" if kind == "delta" else "position_um": values}


def heartbeat(descriptor):
    return {"version": 1, "session_id": descriptor["session_id"]}


def test_loopback_descriptor_and_commanded_micrometre_steps(running):
    receiver, descriptor, _ = running
    assert descriptor["url"].startswith("http://127.0.0.1:")
    assert descriptor["protocol"] == "nd2wsi-stage-follow/1"
    assert receiver.start() == descriptor
    assert receiver.snapshot()["status"] == "waiting"
    total = 0
    for seq, delta in enumerate((1, 50, 10, 100), 1):
        status, result, headers = request(descriptor, motion(descriptor, seq, [delta, -delta]))
        total += delta
        assert status == 200
        assert result["seq"] == seq
        assert result["displacement_um"] == [total, -total]
        assert result["position_kind"] == "commanded"
        assert result["status"] == "following"
        assert "commanded_position_um" not in result
        assert "token" not in result
        assert not any(name.lower().startswith("access-control-") for name in headers)
    assert receiver.snapshot()["displacement_um"] == [161, -161]


def test_reference_and_absolute_roi_target_reconcile_with_relative_steps(running):
    receiver, descriptor, _ = running
    cases = [
        (1, "reference", [1200, -400], [0, 0], [1200, -400]),
        (2, "delta", [1, 50], [1, 50], [1201, -350]),
        (3, "target", [1300, -200], [100, 200], [1300, -200]),
        (4, "delta", [-10, -100], [90, 100], [1290, -300]),
        (5, "target", [1200, -400], [0, 0], [1200, -400]),
    ]
    for seq, kind, vector, expected_delta, expected_position in cases:
        status, result, _ = request(descriptor, motion(descriptor, seq, vector, kind))
        assert status == 200
        assert result["displacement_um"] == expected_delta
        assert result["commanded_position_um"] == expected_position
    assert receiver.snapshot()["displacement_um"] == [0, 0]


def test_retries_and_older_events_do_not_apply_motion_twice(running):
    receiver, descriptor, _ = running
    first = motion(descriptor, 1, [1, 0])
    request(descriptor, first)
    request(descriptor, motion(descriptor, 2, [50, 0]))
    for event in (first, motion(descriptor, 2, [50, 0])):
        status, result, _ = request(descriptor, event)
        assert status == 200
        assert result["result"] == "duplicate_or_stale"
        assert result["displacement_um"] == [51, 0]
        assert result["seq"] == 2
    assert receiver.snapshot()["status"] == "following"


@pytest.mark.parametrize("gap_sequence", [2, 7])
def test_sequence_gap_latches_pause_and_late_events_cannot_resume(running, gap_sequence):
    receiver, descriptor, _ = running
    status, _, _ = request(descriptor, motion(descriptor, gap_sequence, [1, 0]))
    assert status == 409
    assert receiver.snapshot()["status"] == "paused"
    for payload, path in [(motion(descriptor, 1, [1, 0]), "/motion"),
                          (heartbeat(descriptor), "/heartbeat")]:
        assert request(descriptor, payload, path=path)[0] == 409
    assert receiver.snapshot()["displacement_um"] == [0, 0]
    assert receiver.snapshot()["seq"] == 0


def test_target_needs_reference_and_reference_cannot_rezero_mid_session(running):
    receiver, descriptor, _ = running
    assert request(descriptor, motion(descriptor, 1, [100, 0], "target"))[0] == 400
    assert receiver.snapshot()["seq"] == 0
    assert request(descriptor, motion(descriptor, 1, [10, 0]))[0] == 200
    assert request(descriptor, motion(descriptor, 2, [100, 0], "reference"))[0] == 400
    assert request(descriptor, motion(descriptor, 2, [100, 0], "target"))[0] == 400
    assert receiver.snapshot()["displacement_um"] == [10, 0]
    assert receiver.snapshot()["seq"] == 1


def test_no_timeout_before_contact_heartbeat_keeps_session_alive_without_motion(running):
    receiver, descriptor, clock = running
    clock.now += 1000
    assert receiver.snapshot()["status"] == "waiting"
    for _ in range(3):
        assert request(descriptor, heartbeat(descriptor), path="/heartbeat")[0] == 200
        clock.now += 2
        assert receiver.snapshot()["status"] == "waiting"
        assert receiver.snapshot()["seq"] == 0
        assert receiver.snapshot()["displacement_um"] == [0, 0]


@pytest.mark.parametrize("first_contact", ["heartbeat", "motion"])
def test_silence_latches_pause_on_snapshot(running, first_contact):
    receiver, descriptor, clock = running
    if first_contact == "heartbeat":
        request(descriptor, heartbeat(descriptor), path="/heartbeat")
    else:
        request(descriptor, motion(descriptor, 1, [1, 0]))
    clock.now += 3
    assert receiver.snapshot()["status"] == "paused"
    assert request(descriptor, heartbeat(descriptor), path="/heartbeat")[0] == 409
    assert receiver.snapshot()["status"] == "paused"


def test_late_motion_cannot_bypass_timeout_between_viewer_polls(running):
    receiver, descriptor, clock = running
    request(descriptor, motion(descriptor, 1, [1, 0]))
    clock.now += 3.1
    assert request(descriptor, motion(descriptor, 2, [50, 0]))[0] == 409
    assert receiver.snapshot()["displacement_um"] == [1, 0]
    assert receiver.snapshot()["seq"] == 1


@pytest.mark.parametrize("change", [
    {"version": True}, {"seq": True}, {"seq": 0}, {"seq": 1.0},
    {"seq": MAX_SEQUENCE + 1}, {"type": "feedback"}, {"extra": 1},
    {"delta_um": [True, 0]}, {"delta_um": ["1", 0]}, {"delta_um": [0]},
    {"delta_um": [float("nan"), 0]}, {"delta_um": [float("inf"), 0]},
    {"delta_um": [10**400, 0]},
])
def test_invalid_values_never_advance_or_move(running, change):
    receiver, descriptor, _ = running
    status, _, _ = request(descriptor, {**motion(descriptor, 1, [1, 0]), **change})
    assert status == 400
    assert receiver.snapshot()["seq"] == 0
    assert receiver.snapshot()["displacement_um"] == [0, 0]
    assert receiver.snapshot()["status"] == "waiting"


def test_finite_inputs_that_overflow_displacement_are_rejected_atomically(running):
    receiver, descriptor, _ = running
    assert request(descriptor, motion(descriptor, 1, [1e308, 0]))[0] == 200
    assert request(descriptor, motion(descriptor, 2, [1e308, 0]))[0] == 400
    assert receiver.snapshot()["seq"] == 1
    assert receiver.snapshot()["displacement_um"] == [1e308, 0]


@pytest.mark.parametrize("headers,expected", [
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Authorization": ""}, 401),
    ({"Origin": "http://127.0.0.1"}, 403),
    ({"Origin": ""}, 403),
    ({"Host": "attacker.example"}, 403),
    ({"Content-Type": "text/plain"}, 415),
    ({"Transfer-Encoding": "chunked"}, 400),
])
def test_http_auth_and_browser_boundary(running, headers, expected):
    receiver, descriptor, _ = running
    assert request(descriptor, motion(descriptor, 1, [1, 0]), headers=headers)[0] == expected
    assert receiver.snapshot()["seq"] == 0


def test_wrong_session_is_rejected_on_both_endpoints(running):
    receiver, descriptor, _ = running
    for event, path in [(motion(descriptor, 1, [1, 0]), "/motion"),
                        (heartbeat(descriptor), "/heartbeat")]:
        assert request(descriptor, {**event, "session_id": "another-session"}, path=path)[0] == 409
    assert receiver.snapshot()["status"] == "waiting"


def test_json_body_bounds_duplicate_fields_and_heartbeat_schema(running):
    receiver, descriptor, _ = running
    for body in (b"{}" * (MAX_BODY_BYTES // 2 + 1), b""):
        assert request(descriptor, body)[0] == 413
    for body in (b'{"version":1,"version":1}', b"[]", b"{", b'"text"', b"\xff"):
        assert request(descriptor, body)[0] == 400
    assert request(descriptor, {**heartbeat(descriptor), "seq": 1}, path="/heartbeat")[0] == 400
    assert request(descriptor, heartbeat(descriptor), path="/missing")[0] == 404
    assert request(descriptor, b"", method="OPTIONS")[0] == 405
    assert receiver.snapshot()["seq"] == 0


def test_close_releases_listener_and_receiver_cannot_restart():
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    worker = receiver._thread
    receiver.close()
    receiver.close()
    assert not worker.is_alive()
    assert receiver.snapshot()["enabled"] is False
    assert receiver.snapshot()["status"] == "paused"
    with pytest.raises(RuntimeError, match="cannot restart"):
        receiver.start()
    url = urlsplit(descriptor["url"])
    with pytest.raises(OSError):
        socket.create_connection((url.hostname, url.port), timeout=0.5)


def test_close_interrupts_client_stalled_mid_body():
    receiver = StageFollowReceiver(request_timeout=30)
    descriptor = receiver.start()
    url = urlsplit(descriptor["url"])
    connection = socket.create_connection((url.hostname, url.port), timeout=1)
    worker = receiver._thread
    try:
        connection.sendall((f"POST /motion HTTP/1.1\r\nHost: {url.netloc}\r\n"
                            f"Authorization: Bearer {descriptor['token']}\r\n"
                            "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{").encode())
        deadline = time.monotonic() + 1
        while receiver._server._active_connection is None and time.monotonic() < deadline:
            time.sleep(0.005)
        started = time.monotonic()
        receiver.close()
        assert time.monotonic() - started < 1
        assert not worker.is_alive()
    finally:
        connection.close()
        receiver.close()
