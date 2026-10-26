"""The receiver's Windows close path: a stalled client must not hold close()."""

import socket
import time
from urllib.parse import urlsplit

from nd2wsi import stage_follow
from nd2wsi.stage_follow import StageFollowReceiver


def test_windows_close_path_interrupts_a_client_stalled_mid_body(monkeypatch):
    # On Windows, shutdown() does not wake the worker's blocked recv(); the receiver
    # also closes the OS socket. Exercise that branch here (it must not break the
    # close on any platform); real Windows behaviour is covered by Windows CI running
    # tests/test_stage_follow.py::test_close_interrupts_client_stalled_mid_body.
    monkeypatch.setattr(stage_follow.sys, "platform", "win32")
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
        assert receiver._server._active_connection is not None
        started = time.monotonic()
        receiver.close()
        assert time.monotonic() - started < 1
        assert not worker.is_alive()
        assert receiver.snapshot()["enabled"] is False
    finally:
        connection.close()
        receiver.close()
