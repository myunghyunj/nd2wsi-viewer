"""Server request leases, lock scope and response streaming."""
from __future__ import annotations

import email.message
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import astromap_export as am
from nd2wsi import render
from nd2wsi.server import (
    SlideRegistry,
    ViewerState,
    _job_get,
    make_handler,
)

SID = "12345678"
ASTROMAP_REQUEST = {
    "generation": "current", "rois": [{"x": 5, "y": 6, "w": 12, "h": 9, "note": "A"}],
    "channels": [0, 1, 2], "window": None, "frame": None,
}


def state(*, width=80, height=60, annotations_path=None):
    attrs = {
        "nd2wsi": {"source": "example.nd2",
                   "levels": [{"path": "0", "width": width, "height": height}]},
        "omero": {"channels": [{}, {}]},
    }
    return ViewerState({}, attrs, annotations_path=annotations_path)


def roi_query(**kwargs):
    params = {"x": 0, "y": 0, "w": 10, "h": 20, "format": "tiff", "job": "lease-test"}
    return {key: [str(value)] for key, value in (params | kwargs).items()}


def window_registry(st):
    registry = SlideRegistry()
    registry.window_session = SimpleNamespace(role="user")
    registry.slides[SID] = st
    return registry


def bare_handler(registry):
    """A handler without a socket; callers stub whatever it would send."""
    handler_type = make_handler(registry)
    return handler_type.__new__(handler_type)


class _Wire:
    """Records socket writes; fails every write after the header block."""

    def __init__(self, error=None):
        self.error = error
        self.writes = []

    def write(self, data):
        if self.error is not None and self.writes:
            raise self.error
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass


def socket_handler(registry, path, wire):
    handler = bare_handler(registry)
    handler.command = "GET"
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.close_connection = False
    handler.headers = email.message.Message()
    handler.headers["Host"] = "127.0.0.1"
    handler.wfile = wire
    return handler


@pytest.mark.parametrize("error", [
    ConnectionResetError("reset by peer"),
    ConnectionAbortedError("aborted by the host"),
    OSError("disk failure"),
])
def test_mid_stream_failure_writes_no_second_response(monkeypatch, error):
    def write(root, attrs, destination, *args, **kwargs):
        Path(destination).write_bytes(b"pixels" * 1000)

    monkeypatch.setattr(render, "export_roi_tiff", write)
    st = state()
    registry = window_registry(st)
    wire = _Wire(error)
    handler = socket_handler(registry, f"/s/{SID}/api/roi?x=0&y=0&w=10&h=20&format=tiff&job=midstream", wire)
    handler.do_GET()
    assert len(wire.writes) == 1  # the header block, and nothing after the failure
    assert wire.writes[0].startswith(b"HTTP/1.1 200")
    assert wire.writes[0].count(b"HTTP/1.1") == 1
    assert handler.close_connection is True
    assert _job_get("midstream")["state"] == "error"
    assert st.busy.active == 0
    # The next request on a reused handler can answer errors normally again.
    handler.wfile = second = _Wire()
    handler.path = "/s/ffffffff/api/info"
    handler.do_GET()
    assert second.writes and second.writes[0].startswith(b"HTTP/1.1 404")


def test_streamed_roi_download_carries_the_security_headers(monkeypatch):
    def write(root, attrs, destination, *args, **kwargs):
        Path(destination).write_bytes(b"pixels")

    monkeypatch.setattr(render, "export_roi_tiff", write)
    registry = window_registry(state())
    wire = _Wire()
    handler = socket_handler(registry, f"/s/{SID}/api/roi?x=0&y=0&w=10&h=20&format=tiff", wire)
    handler.do_GET()
    head, body = wire.writes[0], b"".join(wire.writes[1:])
    assert head.startswith(b"HTTP/1.1 200")
    for header in (b"Content-Type: image/tiff", b"Content-Length: 6", b"Cache-Control: no-store",
                   b"X-Content-Type-Options: nosniff", b"Referrer-Policy: no-referrer",
                   b"Content-Disposition: attachment"):
        assert header in head
    assert body == b"pixels"
    assert handler.close_connection is False


def astromap_state():
    channels = [{"label": name, "color": color, "window": {"start": 0, "end": 255}}
                for name, color in zip(("R", "G", "B"), ("FF0000", "00FF00", "0000FF"))]
    attrs = {"nd2wsi": {"source": "example.nd2", "levels": [{"path": "0", "width": 80, "height": 60}],
                        "rgb": False},
             "omero": {"channels": channels}}
    root = {"0": np.stack([np.full((60, 80), value, np.uint8) for value in (40, 100, 160)])}
    return ViewerState(root, attrs, generation="current")


def build_astromap(registry, st):
    """Run the AstroMap POST route; return the handler and the download token."""
    handler = bare_handler(registry)
    handler._body_json = lambda limit: dict(ASTROMAP_REQUEST)
    replies = []
    handler._json = lambda obj, code=200: replies.append(obj)
    handler._astromap(st)
    return handler, replies[0]["download_url"].split("token=", 1)[1]


def test_astromap_download_streams_with_the_security_headers(tmp_path):
    st = astromap_state()
    registry = window_registry(st)
    registry.astromap_downloads = am.AstromapDownloads(tmp_path)
    handler, token = build_astromap(registry, st)
    headers, chunks = {}, []
    handler.send_response = lambda code: headers.setdefault(":status", code)
    handler.send_header = headers.__setitem__
    handler.end_headers = lambda: None
    handler.wfile = SimpleNamespace(write=chunks.append)
    handler._astromap_download(st, {"token": [token]})
    assert headers[":status"] == 200
    assert headers["Content-Type"] == "application/zip"
    assert "attachment" in headers["Content-Disposition"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    body = b"".join(chunks)
    assert body.startswith(b"PK") and int(headers["Content-Length"]) == len(body)
    registry.astromap_downloads.close()
