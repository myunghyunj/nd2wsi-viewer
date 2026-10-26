"""Closing the receiver while a client is stalled mid-request.

The worker reads through ``_CancellableReader``: each wait for data runs in short
``select`` steps and ends once the receiver is stopping, so ``stop()`` never has to
close or shut down the worker's socket concurrently on Windows (Winsock forbids
``closesocket`` concurrent with another call on that socket).
"""

import socket
import time
from urllib.parse import urlsplit

import pytest

from nd2wsi import stage_follow
from nd2wsi.stage_follow import StageFollowReceiver


def _stalled_client(receiver, descriptor, request_bytes):
    url = urlsplit(descriptor["url"])
    connection = socket.create_connection((url.hostname, url.port), timeout=1)
    connection.sendall(request_bytes(url, descriptor))
    # Wait until the worker is blocked in a read that found no data, not merely
    # until the connection was accepted.
    deadline = time.monotonic() + 2
    while receiver._server._stalled_reads == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert receiver._server._stalled_reads > 0, "the worker never stalled in a read"
    return connection


def _incomplete_body(url, descriptor):
    return (f"POST /motion HTTP/1.1\r\nHost: {url.netloc}\r\n"
            f"Authorization: Bearer {descriptor['token']}\r\n"
            "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{").encode()


def _incomplete_headers(url, descriptor):
    return f"POST /motion HTTP/1.1\r\nHost: {url.netloc}\r\nAuthor".encode()


@pytest.mark.parametrize("request_bytes", [_incomplete_body, _incomplete_headers], ids=["body", "headers"])
@pytest.mark.parametrize("platform", ["native", "win32"])
def test_close_interrupts_a_stalled_read_without_touching_its_socket(monkeypatch, request_bytes, platform):
    if platform == "win32":
        monkeypatch.setattr(stage_follow.sys, "platform", "win32")
    touched = []
    if platform == "win32":
        # The Windows stop path must not call into the worker's socket.
        real_shutdown = socket.socket.shutdown

        def watching_shutdown(sock, how):
            touched.append(("shutdown", how))
            return real_shutdown(sock, how)

        monkeypatch.setattr(socket.socket, "shutdown", watching_shutdown)
    receiver = StageFollowReceiver(request_timeout=30)
    descriptor = receiver.start()
    worker = receiver._thread
    connection = _stalled_client(receiver, descriptor, request_bytes)
    try:
        started = time.monotonic()
        receiver.close()
        assert time.monotonic() - started < 1
        assert not worker.is_alive()
        assert receiver.snapshot()["enabled"] is False
        # socketserver's own shutdown_request(SHUT_WR) runs on the worker after its read returned.
        assert ("shutdown", socket.SHUT_RDWR) not in touched
    finally:
        connection.close()
        receiver.close()


def test_a_silent_client_still_times_out_after_the_request_timeout():
    receiver = StageFollowReceiver(request_timeout=0.4)
    descriptor = receiver.start()
    try:
        connection = _stalled_client(receiver, descriptor, _incomplete_body)
        connection.settimeout(3)
        started = time.monotonic()
        reply = connection.recv(4096)
        assert b"408" in reply.split(b"\r\n", 1)[0]
        assert 0.3 < time.monotonic() - started < 2
        connection.close()
    finally:
        receiver.close()


def test_the_reader_parses_lines_and_bodies_across_chunks():
    left, right = socket.socketpair()
    server = type("Server", (), {"_stopping": False, "_stalled_reads": 0})()
    reader = stage_follow._CancellableReader(left, server, timeout=2)
    try:
        right.sendall(b"GET / HTTP/1.0\r\nX: ")
        right.sendall(b"1\r\n\r\nbody-")
        right.sendall(b"rest")
        assert reader.readline(65537) == b"GET / HTTP/1.0\r\n"
        assert reader.readline(5) == b"X: 1\r"
        assert reader.readline() == b"\n"
        assert reader.readline() == b"\r\n"
        assert reader.read(9) == b"body-rest"
        right.close()
        assert reader.read(5) == b""
        assert reader.readline() == b""
    finally:
        left.close()
