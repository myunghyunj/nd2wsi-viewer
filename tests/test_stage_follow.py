"""The local stage-follow receiver handles observations, never hardware control."""

from __future__ import annotations

import http.client
import json
import select
import socket
import time
from urllib.parse import urlsplit

import pytest

from nd2wsi import stage_follow
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


def _rejected_request(descriptor, body_length):
    """Raw headers of a wrong-token POST whose body has not been sent yet."""
    url = urlsplit(descriptor["url"])
    connection = socket.create_connection((url.hostname, url.port), timeout=2)
    connection.sendall((f"POST /heartbeat HTTP/1.0\r\nHost: {url.netloc}\r\n"
                        "Authorization: Bearer wrong\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {body_length}\r\n\r\n").encode())
    return connection


def _read_reply(connection):
    response = http.client.HTTPResponse(connection)
    response.begin()
    reply = response.status, json.loads(response.read())
    response.close()
    return reply


def _closed_within(connection, seconds):
    """True once the peer has closed (EOF) within ``seconds``."""
    readable, _, _ = select.select([connection], [], [], seconds)
    return bool(readable) and connection.recv(1) == b""


def test_error_reply_takes_in_the_rejected_body_before_closing(monkeypatch):
    # Closing with the body still unread makes Windows reset the connection, which
    # can discard the 401 before the client reads it (WinError 10053).
    monkeypatch.setattr(stage_follow, "ERROR_DRAIN_SECONDS", 5)
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    body = json.dumps(heartbeat(descriptor)).encode()
    connection = _rejected_request(descriptor, len(body))
    try:
        status, reply = _read_reply(connection)
        assert status == 401 and "token" in reply["error"]
        # The reply came first; the connection stays open for the body ...
        assert not _closed_within(connection, 0.2)
        connection.sendall(body)
        # ... and closes as soon as it arrives, long before the deadline.
        assert _closed_within(connection, 2)
    finally:
        connection.close()
        receiver.close()


def test_error_reply_takes_in_a_body_of_unknown_length(monkeypatch):
    # A chunked body has no trusted length; the reply still waits for what the
    # client sends, ending at the first step in which nothing arrives.
    monkeypatch.setattr(stage_follow, "ERROR_DRAIN_SECONDS", 5)
    monkeypatch.setattr(stage_follow._CancellableReader, "STEP", 0.5)
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    url = urlsplit(descriptor["url"])
    connection = socket.create_connection((url.hostname, url.port), timeout=2)
    try:
        connection.sendall((f"POST /heartbeat HTTP/1.0\r\nHost: {url.netloc}\r\n"
                            f"Authorization: Bearer {descriptor['token']}\r\n"
                            "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n").encode())
        status, reply = _read_reply(connection)
        assert status == 400 and "Transfer-Encoding" in reply["error"]
        assert not _closed_within(connection, 0.2)
        connection.sendall(json.dumps(heartbeat(descriptor)).encode())
        assert _closed_within(connection, 3)
    finally:
        connection.close()
        receiver.close()


def test_a_rejected_body_that_never_arrives_is_dropped_at_the_deadline(monkeypatch):
    monkeypatch.setattr(stage_follow, "ERROR_DRAIN_SECONDS", 0.2)
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    connection = _rejected_request(descriptor, MAX_BODY_BYTES)
    try:
        assert _read_reply(connection)[0] == 401
        assert _closed_within(connection, 2)
        # The single worker is free again.
        assert request(descriptor, heartbeat(descriptor), path="/heartbeat")[0] == 200
    finally:
        connection.close()
        receiver.close()


def test_close_interrupts_an_error_reply_waiting_for_its_body(monkeypatch):
    monkeypatch.setattr(stage_follow, "ERROR_DRAIN_SECONDS", 30)
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    worker = receiver._thread
    connection = _rejected_request(descriptor, MAX_BODY_BYTES)
    try:
        assert _read_reply(connection)[0] == 401
        started = time.monotonic()
        receiver.close()
        assert time.monotonic() - started < 1
        assert not worker.is_alive()
    finally:
        connection.close()
        receiver.close()


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


def test_submit_routes_motion_and_heartbeat_like_http(running):
    receiver, descriptor, _ = running
    accepted = receiver.submit(motion(descriptor, 1, [5, -3]))
    assert accepted["ok"] and accepted["result"] == "accepted"
    assert accepted["displacement_um"] == [5, -3] and accepted["seq"] == 1
    beat = receiver.submit(heartbeat(descriptor))
    assert beat["ok"] and beat["result"] == "heartbeat"
    assert "token" not in accepted and "token" not in beat


def test_submit_returns_errors_instead_of_raising(running):
    receiver, descriptor, _ = running
    receiver.submit(motion(descriptor, 1, [1, 1]))
    gap = receiver.submit(motion(descriptor, 3, [1, 1]))  # sequence gap -> paused (409)
    assert gap == {"ok": False, "status": 409, "message": gap["message"]}
    assert "sequence gap" in gap["message"].lower()
    # Once paused, further motion is refused with a status code, never an exception.
    after = receiver.submit(motion(descriptor, 2, [1, 1]))
    assert after["ok"] is False and after["status"] == 409


def test_submit_rejects_bad_payloads_without_raising(running):
    receiver, descriptor, _ = running
    wrong_session = receiver.submit({"version": 1, "session_id": "nope", "seq": 1,
                                     "type": "delta", "delta_um": [1, 1]})
    assert wrong_session == {"ok": False, "status": 409, "message": wrong_session["message"]}
    not_a_dict = receiver.submit("oops")
    assert not_a_dict["ok"] is False and not_a_dict["status"] == 400
    bad_type = receiver.submit({"version": 1, "session_id": descriptor["session_id"],
                                "seq": 1, "type": "spin", "delta_um": [1, 1]})
    assert bad_type["ok"] is False and bad_type["status"] == 400


def test_submit_on_closed_receiver_reports_closed():
    receiver = StageFollowReceiver()
    descriptor = receiver.start()
    receiver.close()
    result = receiver.submit(heartbeat(descriptor))
    assert result == {"ok": False, "status": 410, "message": "Receiver is closed"}
