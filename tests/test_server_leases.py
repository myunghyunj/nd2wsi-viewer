"""Server request leases, lock scope and response streaming."""
from __future__ import annotations

import email.message
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import astromap_export as am
from nd2wsi import render
from nd2wsi.server import (
    EXPORT_COUNT_MIN_PIXELS,
    TILE_RE,
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
    assert st.busy.active == st.exports.active == 0
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


def test_tile_read_holds_busy_but_is_not_an_export(monkeypatch):
    reading, release = threading.Event(), threading.Event()

    def slow_tile(*args):
        reading.set()
        assert release.wait(5)
        return b"tile"

    monkeypatch.setattr(render, "render_tile", slow_tile)
    st = state()
    registry = window_registry(st)
    handler = bare_handler(registry)
    sent = []
    handler._send = lambda code, body, ctype, *args, **kwargs: sent.append((code, body))
    worker = threading.Thread(
        target=handler._tile, args=(st, TILE_RE.match("/api/tile/0/0/0.jpg"), {}),
    )
    worker.start()
    try:
        assert reading.wait(5)
        assert st.busy.active == 1
        assert registry.active_export_count() == 0
    finally:
        release.set()
        worker.join(5)
    assert sent == [(200, b"tile")]


def test_streamed_tiff_counts_as_one_export_until_transmission_ends(monkeypatch):
    def write(root, attrs, destination, *args, **kwargs):
        Path(destination).write_bytes(b"pixels")

    monkeypatch.setattr(render, "export_roi_tiff", write)
    st = state()
    registry = window_registry(st)
    handler = bare_handler(registry)
    handler.send_response = lambda code: None
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None
    writing, release = threading.Event(), threading.Event()
    received = []

    class SlowSink:
        def write(self, chunk):
            writing.set()
            assert release.wait(5)
            received.append(chunk)

    handler.wfile = SlowSink()
    worker = threading.Thread(target=handler._roi, args=(st, roi_query()))
    worker.start()
    try:
        assert writing.wait(5)
        assert registry.active_export_count() == 1
        assert st.busy.active == 1
    finally:
        release.set()
        worker.join(5)
    assert received == [b"pixels"]
    assert registry.active_export_count() == 0
    assert st.busy.active == st.exports.active == 0


@pytest.mark.parametrize("side,counted", [(512, 0), (2048, 1)])
def test_previews_and_size_samples_stay_reads_while_large_renders_count(monkeypatch, side, counted):
    assert (side * side >= EXPORT_COUNT_MIN_PIXELS) is bool(counted)
    st = state(width=4096, height=4096)
    registry = window_registry(st)
    seen = []

    def rendered(root, attrs, level, x, y, w, h, *args, **kwargs):
        seen.append((w, h, registry.active_export_count(), st.busy.active))
        return b"image"

    monkeypatch.setattr(render, "export_roi_rendered", rendered)
    handler = bare_handler(registry)
    sent = []
    handler._send = lambda code, body, ctype, *args, **kwargs: sent.append((code, body, ctype))
    handler._roi(st, roi_query(format="jpg", w=side, h=side))
    assert seen == [(side, side, counted, 1)]
    assert sent == [(200, b"image", "image/jpeg")]
    assert st.busy.active == st.exports.active == 0


def test_window_close_refuses_at_once_during_an_export_without_side_effects():
    st = state()
    registry = window_registry(st)
    cancelled = []
    st._shading = SimpleNamespace(cancel=lambda: cancelled.append(True), close=lambda: None)
    with st.busy, st.exports:
        started = time.monotonic()
        with pytest.raises(ValueError, match="export"):
            registry.remove(SID)
        assert time.monotonic() - started < 0.5  # no drain wait
        assert not st.busy.closed  # tile reads stay admitted
        assert cancelled == []
        assert registry.get(SID) is st
    assert registry.remove(SID) is True
    assert cancelled == [True]
    assert registry.get(SID) is None


def test_astromap_build_and_download_count_as_exports(monkeypatch, tmp_path):
    st = astromap_state()
    registry = window_registry(st)
    registry.astromap_downloads = am.AstromapDownloads(tmp_path)
    original_write = am.AstromapPlan.write
    during_build = []

    def counted_write(self, folder):
        during_build.append((registry.active_export_count(), st.busy.active))
        return original_write(self, folder)

    monkeypatch.setattr(am.AstromapPlan, "write", counted_write)
    handler, token = build_astromap(registry, st)
    assert during_build == [(1, 1)]
    assert registry.active_export_count() == 0
    during_download = []
    handler.send_response = lambda code: None
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None
    handler.wfile = SimpleNamespace(write=lambda chunk: during_download.append(
        (registry.active_export_count(), st.busy.active)))
    handler._astromap_download(st, {"token": [token]})
    # The zip is the window's own file: it counts, but holds no slide-read lease.
    assert during_download and set(during_download) == {(1, 0)}
    assert registry.active_export_count() == 0
    registry.astromap_downloads.close()
