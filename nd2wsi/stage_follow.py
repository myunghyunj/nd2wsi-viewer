"""Authenticated local receiver for normalized, commanded sample-stage observations.

This module never connects to a controller or interprets its wire protocol. A
separate, validated observer supplies XY values in micrometres. They describe
commanded motion, not measured stage feedback. Sequence gaps or lost contact
require a fresh receiver and viewer anchor before following can resume.
"""

from __future__ import annotations

import hmac
import json
import math
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

PROTOCOL = "nd2wsi-stage-follow/1"
MAX_BODY_BYTES = 4096
MAX_SEQUENCE = 2**53 - 1


class _RequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _vector(value: object, name: str) -> tuple[Decimal, Decimal]:
    if not isinstance(value, list) or len(value) != 2:
        raise _RequestError(400, f"{name} must be an XY array")
    values = []
    for component in value:
        try:
            finite = type(component) in (int, float) and math.isfinite(component)
        except OverflowError:
            finite = False
        if not finite:
            raise _RequestError(400, f"{name} must contain finite numbers")
        values.append(Decimal(str(component)))
    return values[0], values[1]


def _finite_vector(values: tuple[Decimal, Decimal]) -> list[float]:
    result = [float(value) for value in values]
    if not all(math.isfinite(value) for value in result):
        raise _RequestError(400, "Cumulative displacement is out of range")
    return result


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


class _LocalServer(HTTPServer):
    """One worker, finite socket waits, and interruptible shutdown."""

    allow_reuse_address = False
    request_queue_size = 4

    def __init__(self, receiver: StageFollowReceiver):
        self.receiver = receiver
        self._connection_lock = threading.Lock()
        self._active_connection = None
        self._stopping = False
        super().__init__(("127.0.0.1", 0), _Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.receiver.request_timeout)
        with self._connection_lock:
            if self._stopping:
                connection.close()
                raise OSError("Receiver is closing")
            self._active_connection = connection
        return connection, address

    def shutdown_request(self, request):
        with self._connection_lock:
            if self._active_connection is request:
                self._active_connection = None
        super().shutdown_request(request)

    def stop(self):
        with self._connection_lock:
            self._stopping = True
            connection = self._active_connection
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                if sys.platform == "win32":
                    # Windows does not wake a recv() blocked in the worker thread on
                    # shutdown, so a stalled client would hold close() for the whole
                    # request timeout. Closing the OS socket aborts that recv. The
                    # handler's makefile() objects keep socket.close() from doing so,
                    # hence the base close; the later close_request() is then a no-op.
                    try:
                        socket.socket.__base__.close(connection)
                    except OSError:
                        pass
        self.shutdown()
        self.server_close()

    def handle_error(self, request, client_address):
        # Malformed local requests and interrupted clients must not expose tokens
        # or flood the native application's log.
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "StageFollow"
    sys_version = ""

    def log_message(self, format, *args):
        return

    def _reply(self, status: int, body: dict):
        self.close_connection = True
        encoded = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)
        except OSError:
            pass

    def do_POST(self):
        receiver = self.server.receiver
        try:
            if self.headers.get_all("Origin"):
                raise _RequestError(403, "Browser origins are not allowed")
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host") != [expected_host]:
                raise _RequestError(403, "Expected the receiver's loopback host")
            auth = self.headers.get_all("Authorization", [])
            expected = f"Bearer {receiver._token}"
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), expected.encode()):
                raise _RequestError(401, "A valid receiver token is required")
            if self.path not in ("/motion", "/heartbeat"):
                raise _RequestError(404, "Unknown receiver endpoint")
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
                raise _RequestError(400, "Expected one Content-Length without Transfer-Encoding")
            length = lengths[0]
            if not length.isascii() or not length.isdecimal() or len(length) > 5:
                raise _RequestError(400, "Invalid Content-Length")
            length = int(length)
            if not 0 < length <= MAX_BODY_BYTES:
                raise _RequestError(413, "Request body is empty or too large")
            content_types = self.headers.get_all("Content-Type", [])
            if (len(content_types) != 1
                    or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                raise _RequestError(415, "Expected application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise _RequestError(400, "Incomplete request body")
            try:
                payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object,
                                     parse_constant=_reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise _RequestError(400, "Invalid JSON object") from None
            if self.path == "/motion":
                result = receiver._motion(payload)
            else:
                result = receiver._heartbeat(payload)
            self._reply(200, result)
        except _RequestError as exc:
            self._reply(exc.status, {"error": str(exc)})
        except (TimeoutError, OSError):
            self._reply(408, {"error": "Request timed out"})

    def do_GET(self):
        self._reply(405, {"error": "POST is required"})

    do_OPTIONS = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET


class StageFollowReceiver:
    """Receive ordered XY command observations without any hardware access.

    ``start`` returns the private loopback URL, bearer token and fresh session ID.
    ``snapshot`` never includes the token. A receiver cannot restart after close
    or recover from a paused state; create a new receiver and viewer anchor.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 stale_timeout: float = 3.0, request_timeout: float = 1.0):
        for name, value in (("stale_timeout", stale_timeout), ("request_timeout", request_timeout)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self._clock = clock
        self.stale_timeout = float(stale_timeout)
        self.request_timeout = float(request_timeout)
        self._lock = threading.RLock()
        self._token = secrets.token_urlsafe(32)
        self._session_id = secrets.token_urlsafe(24)
        self._server = None
        self._thread = None
        self._descriptor = None
        self._closed = False
        self._enabled = False
        self._seq = 0
        self._displacement = (Decimal(0), Decimal(0))
        self._reference = None
        self._position = None
        self._last_seen = None
        self._status = "waiting"
        self._message = "Waiting for commanded stage observations."

    def start(self) -> dict:
        with self._lock:
            if self._closed:
                raise RuntimeError("A closed receiver cannot restart")
            if self._descriptor is not None:
                return dict(self._descriptor)
            server = _LocalServer(self)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                      name="nd2wsi-stage-follow", daemon=True)
            self._server = server
            self._thread = thread
            self._enabled = True
            self._descriptor = {
                "url": f"http://127.0.0.1:{server.server_port}",
                "token": self._token,
                "session_id": self._session_id,
                "protocol": PROTOCOL,
            }
            try:
                thread.start()
            except Exception:
                server.server_close()
                self._server = self._thread = self._descriptor = None
                self._enabled = False
                raise
            return dict(self._descriptor)

    def _expire(self, now: float):
        if (self._enabled and self._status != "paused" and self._last_seen is not None
                and now - self._last_seen >= self.stale_timeout):
            self._status = "paused"
            self._message = "Stage observation connection was lost. Reconnect and align again."

    def _snapshot(self) -> dict:
        result = {
            "enabled": self._enabled,
            "session_id": self._session_id,
            "seq": self._seq,
            "displacement_um": _finite_vector(self._displacement),
            "status": self._status,
            "message": self._message,
            "position_kind": "commanded",
        }
        if self._position is not None:
            result["commanded_position_um"] = _finite_vector(self._position)
        return result

    def snapshot(self) -> dict:
        with self._lock:
            self._expire(self._clock())
            return self._snapshot()

    def _validate_session(self, payload: object):
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            raise _RequestError(400, "Expected a version 1 JSON object")
        if payload.get("session_id") != self._session_id:
            raise _RequestError(409, "Receiver session does not match")

    def _check_active(self, now: float):
        if not self._enabled:
            raise _RequestError(410, "Receiver is closed")
        self._expire(now)
        if self._status == "paused":
            raise _RequestError(409, self._message)

    def _heartbeat(self, payload: object) -> dict:
        self._validate_session(payload)
        if set(payload) != {"version", "session_id"}:
            raise _RequestError(400, "Unexpected heartbeat fields")
        with self._lock:
            now = self._clock()
            self._check_active(now)
            self._last_seen = now
            return {**self._snapshot(), "result": "heartbeat"}

    def _motion(self, payload: object) -> dict:
        self._validate_session(payload)
        kind = payload.get("type")
        if kind not in ("delta", "reference", "target"):
            raise _RequestError(400, "Expected delta, reference or target commanded motion")
        field = "delta_um" if kind == "delta" else "position_um"
        if set(payload) != {"version", "session_id", "seq", "type", field}:
            raise _RequestError(400, "Unexpected motion fields")
        seq = payload["seq"]
        if type(seq) is not int or not 1 <= seq <= MAX_SEQUENCE:
            raise _RequestError(400, "Sequence must be a positive safe integer")
        vector = _vector(payload[field], field)
        with self._lock:
            now = self._clock()
            self._check_active(now)
            if seq <= self._seq:
                self._last_seen = now
                return {**self._snapshot(), "result": "duplicate_or_stale"}
            if seq != self._seq + 1:
                self._status = "paused"
                self._message = "Stage observation sequence gap. Reconnect and align again."
                raise _RequestError(409, self._message)
            if kind == "reference":
                if self._seq != 0:
                    raise _RequestError(400, "Reference is only allowed as the first observation")
                displacement = self._displacement
                position = vector
            elif kind == "target":
                if self._reference is None:
                    raise _RequestError(400, "A target requires an initial reference")
                position = vector
                displacement = tuple(value - origin for value, origin in zip(vector, self._reference))
            else:
                displacement = tuple(value + delta for value, delta in zip(self._displacement, vector))
                position = (tuple(value + delta for value, delta in zip(self._position, vector))
                            if self._position is not None else None)
            _finite_vector(displacement)
            if position is not None:
                _finite_vector(position)
            if kind == "reference":
                self._reference = vector
            self._displacement = displacement
            self._position = position
            self._seq = seq
            self._last_seen = now
            self._status = "following"
            self._message = "Following commanded stage observations; physical motion is not verified."
            return {**self._snapshot(), "result": "accepted"}

    def close(self):
        with self._lock:
            self._closed = True
            self._enabled = False
            self._status = "paused"
            self._message = "Stage following is stopped."
            server, thread = self._server, self._thread
            self._server = self._thread = None
        if server is not None:
            server.stop()
            thread.join(timeout=self.request_timeout + 1.0)
