"""Server request leases, lock scope and response streaming."""
from __future__ import annotations

import email.message
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import annotation_workspace, cache, raster, render, server
from nd2wsi import astromap_export as am
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


class _ResetAfterHeaders:
    """Lets the header block through, then fails like a reset socket.

    Unlike _Wire it records failed writes too, so a 500 tried on the dead
    socket shows up as another attempt.
    """

    def __init__(self, error):
        self.error = error
        self.attempts = []

    def write(self, data):
        self.attempts.append(bytes(data))
        if len(self.attempts) > 1:
            raise self.error
        return len(data)

    def flush(self):
        pass


@pytest.mark.parametrize("error", [
    ConnectionResetError("reset by peer"),
    ConnectionAbortedError("aborted by the host"),
    BrokenPipeError("broken pipe"),
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_disconnect_during_a_plain_reply_closes_without_a_500(monkeypatch, method, error):
    st = state()
    registry = window_registry(st)
    wire = _ResetAfterHeaders(error)
    if method == "GET":
        monkeypatch.setattr(render, "render_tile", lambda *args: b"tile")
        handler = socket_handler(registry, f"/s/{SID}/api/tile/0/0/0.jpg", wire)
        handler.do_GET()
    else:
        idle = {"state": "idle"}
        st._shading = SimpleNamespace(status=lambda: {"generation": "g1"}, cancel=lambda: idle,
                                      revoke=lambda: idle, close=lambda: None)
        handler = socket_handler(registry, f"/s/{SID}/api/shading/cancel", wire)
        handler.command = "POST"
        handler.headers["Content-Type"] = "application/json"
        handler._body_json = lambda limit: {"generation": "g1"}
        handler.do_POST()
    head, *rest = wire.attempts
    assert head.startswith(b"HTTP/1.1 200")
    assert len(rest) == 1 and b"HTTP/1.1" not in rest[0]  # the failed body, no 500
    assert handler.close_connection is True  # the keep-alive loop stops here
    assert not getattr(handler, "_headers_buffer", [])  # no half-sent reply left behind
    assert st.busy.active == 0


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


def test_get_by_sid_does_not_wait_for_an_open_holding_the_registry_lock():
    st = state()
    registry = window_registry(st)
    found, first = [], []
    with registry._lock:  # a slow open of another slide
        by_sid = threading.Thread(target=lambda: found.append(registry.get(SID)))
        by_sid.start()
        by_sid.join(2)
        assert not by_sid.is_alive()
        assert found == [st]
        assert registry.get("") is None
        # The bare /api/ route iterates the registry and still serialises.
        default = threading.Thread(target=lambda: first.append(registry.get(None)))
        default.start()
        default.join(0.1)
        assert default.is_alive()
    default.join(2)
    assert first == [st]


def test_histogram_runs_once_without_holding_the_attrs_lock(monkeypatch):
    computing, release = threading.Event(), threading.Event()
    calls = []

    def compute(root, attrs):
        calls.append(attrs)
        computing.set()
        assert release.wait(5)
        return [{"bins": []}]

    monkeypatch.setattr(render, "compute_histograms", compute)
    st = state()
    registry = window_registry(st)
    handler = bare_handler(registry)
    replies = []
    handler._json = lambda obj, code=200: replies.append(obj)
    workers = [threading.Thread(target=handler._histogram, args=(st, {})) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert computing.wait(5)
        assert st.lock.acquire(timeout=1)  # LUT settings and saves do not wait
        st.lock.release()
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
    assert len(calls) == 1
    assert [reply["channels"] for reply in replies] == [[{"bins": []}]] * 2


def browser_save_handler(st, items):
    registry = SlideRegistry()  # browser mode: no window session
    registry.slides[SID] = st
    handler = bare_handler(registry)
    handler._body_json = lambda limit=None: {"items": items}
    replies = []
    handler._json = lambda obj, code=200: replies.append((code, obj))
    handler._error = lambda code, message: replies.append((code, message))
    return handler, replies


def test_browser_mode_annotation_save_is_atomic_and_durable(monkeypatch, tmp_path):
    sidecar = tmp_path / "example.annotations.json"
    synced = []
    real_fsync = annotation_workspace.os.fsync
    monkeypatch.setattr(annotation_workspace.os, "fsync",
                        lambda fd: (synced.append(fd), real_fsync(fd)))
    st = state(annotations_path=sidecar)
    handler, replies = browser_save_handler(st, [{"id": "a"}])
    handler._annotations_post(st, {})
    assert replies == [(200, {"ok": True, "path": str(sidecar), "count": 1})]
    assert synced
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    assert saved["items"] == [{"id": "a"}]
    assert saved["coordinate_space"] == "level-0-pixels"
    assert sorted(p.name for p in tmp_path.iterdir()) == [sidecar.name]


def test_failed_browser_mode_annotation_save_leaves_no_temp_file(monkeypatch, tmp_path):
    sidecar = tmp_path / "example.annotations.json"
    sidecar.write_text('{"items": ["kept"]}', encoding="utf-8")

    def disk_full(fd):
        raise OSError("disk full")

    monkeypatch.setattr(annotation_workspace.os, "fsync", disk_full)
    st = state(annotations_path=sidecar)
    handler, replies = browser_save_handler(st, [{"id": "lost"}])
    handler._annotations_post(st, {})
    assert replies == [(500, f"could not save {sidecar.name}: disk full")]
    assert sidecar.read_text(encoding="utf-8") == '{"items": ["kept"]}'
    assert sorted(p.name for p in tmp_path.iterdir()) == [sidecar.name]


class _ClosingRoot(dict):
    def __init__(self, number, closes):
        super().__init__()
        self.number, self.closes = number, closes

    def close(self, delay=5.0):
        self.closes.append((self.number, delay))


@pytest.mark.parametrize("failure", ["state", "annotations"])
def test_failed_direct_open_closes_each_unregistered_root_at_once(monkeypatch, tmp_path, failure):
    image = tmp_path / "slide.png"
    image.write_bytes(b"not decoded")
    closes, opened = [], []
    attrs = state().attrs

    def open_direct(path):
        opened.append(path)
        return _ClosingRoot(len(opened), closes), attrs

    # The file changes once between the first fingerprint and the first open.
    prints = iter([{"size": 1, "mtime_ns": 1, "quick_sha256": "a"},
                   {"size": 1, "mtime_ns": 2, "quick_sha256": "b"},
                   {"size": 1, "mtime_ns": 2, "quick_sha256": "b"}])
    monkeypatch.setattr(cache, "quick_fingerprint", lambda path: next(prints))
    monkeypatch.setattr(raster, "open_direct", open_direct)
    registry = SlideRegistry()
    if failure == "annotations":
        def no_sidecar(*args, **kwargs):
            raise OSError("sidecar unavailable")

        monkeypatch.setattr(registry, "annotation_path", no_sidecar)
        expected = OSError
    else:
        class Unbuildable(ViewerState):
            def __init__(self, *args, **kwargs):
                raise RuntimeError("state unavailable")

        monkeypatch.setattr(server, "ViewerState", Unbuildable)
        monkeypatch.setattr(registry, "annotation_path", lambda *args, **kwargs: None)
        expected = RuntimeError
    with pytest.raises(expected):
        registry.add_direct(image)
    # The stale first open and the failed registration both close without
    # _Root's 5 s delay, and nothing was registered.
    assert closes == [(1, 0), (2, 0)]
    assert registry.slides == {}


def _wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def test_window_close_lets_a_slow_read_finish_and_closes_the_root_after_it():
    st = state()
    closes, closed = [], threading.Event()

    class Root(dict):
        def close(self):
            closes.append(st.busy.active)
            closed.set()

    st.root = Root()
    registry = window_registry(st)
    calls = []
    st._shading = SimpleNamespace(cancel=lambda: calls.append("cancel"), close=lambda: calls.append("close"))
    reading, release = threading.Event(), threading.Event()

    def slow_read():  # a cold tile, a histogram, a Shading Correction strip
        with st.busy:
            reading.set()
            assert release.wait(5)

    reader = threading.Thread(target=slow_read)
    reader.start()
    try:
        assert reading.wait(5)
        started = time.monotonic()
        assert registry.remove(SID) is True  # a read is not an export
        assert time.monotonic() - started < 0.5  # and the close does not wait for it
        assert registry.get(SID) is None and calls == ["cancel", "close"]
        assert st.busy.closed and st.exports.closed  # nothing new is admitted
        assert not closed.wait(0.2)  # the read still uses the root
    finally:
        release.set()
        reader.join(5)
    assert closed.wait(5) and closes == [0]


def test_an_export_resolved_before_a_window_close_cannot_start_after_it(tmp_path):
    st = astromap_state()
    registry = window_registry(st)
    registry.astromap_downloads = am.AstromapDownloads(tmp_path)
    handler, token = build_astromap(registry, st)
    assert registry.remove(SID) is True
    errors, sent = [], []
    handler._error = lambda code, message: errors.append((code, message))
    handler.send_response = sent.append
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None
    handler.wfile = SimpleNamespace(write=sent.append)
    # The download request resolved the slide before the close removed it.
    handler._astromap_download(st, {"token": [token]})
    assert errors == [(409, "slide was closed")] and sent == []
    assert st.exports.active == 0
    with pytest.raises(ValueError, match="closed"):
        with st.exports:  # nor can a streamed ROI or rendered export
            pass
    registry.astromap_downloads.close()


def test_window_close_during_pyramid_publication_keeps_reads_barred():
    st = state()
    registry = window_registry(st)
    publishing, finish = threading.Event(), threading.Event()

    class Replacement:
        staging = final = "staged"
        manifest = prior_manifest = {"generation": "corrected"}

        def publish(self):
            publishing.set()
            assert finish.wait(5)

    st._open_pyramid = lambda path, manifest: ({}, st.attrs)
    replacing = threading.Thread(target=st.replace_pyramid, args=(Replacement(),))
    replacing.start()
    assert publishing.wait(5)  # busy is drained and closed by the replacement
    closing = threading.Thread(target=registry.remove, args=(SID,))
    closing.start()
    try:
        assert _wait_until(lambda: registry.get(SID) is None)
    finally:
        finish.set()  # the replacement reopens busy for its new pyramid
        replacing.join(5)
        closing.join(5)
    assert not replacing.is_alive() and not closing.is_alive()
    assert st._closed and st.generation == "corrected"
    assert st.busy.closed  # the close, not the replacement, has the last word
    with pytest.raises(ValueError, match="closed"):
        st.busy.__enter__()


def test_a_reopened_slide_closes_its_replaced_root_only_after_reads_end(monkeypatch, tmp_path):
    image = tmp_path / "slide.png"
    image.write_bytes(b"not decoded")
    closes, opened = [], []
    attrs = state().attrs

    def open_direct(path):
        opened.append(path)
        return _ClosingRoot(len(opened), closes), attrs

    # The file changes between the first open and the second.
    prints = iter([{"size": 1, "mtime_ns": n, "quick_sha256": sha}
                   for n, sha in ((1, "a"), (1, "a"), (2, "b"), (2, "b"))])
    monkeypatch.setattr(cache, "quick_fingerprint", lambda path: next(prints))
    monkeypatch.setattr(raster, "open_direct", open_direct)
    registry = SlideRegistry()
    monkeypatch.setattr(registry, "annotation_path", lambda *args, **kwargs: None)
    sid = registry.add_direct(image)
    stale = registry.get(sid)
    with stale.busy:  # a read that resolved the slide before it reopened
        assert registry.add_direct(image) == sid
        assert registry.get(sid) is not stale
        time.sleep(0.05)
        assert closes == []
    assert _wait_until(lambda: closes == [(1, 5.0)])  # then _Root's usual close


def test_no_shading_controller_is_made_for_a_closed_slide():
    st = state()
    registry = window_registry(st)
    assert registry.remove(SID) is True
    with pytest.raises(ValueError, match="closed"):
        st.shading
    replies = []
    handler = bare_handler(registry)
    handler.command, handler.path = "POST", f"/s/{SID}/api/shading/start"
    handler.headers = {"Content-Type": "application/json"}
    handler._gate = lambda: handler.path
    handler._resolve = lambda path: (st, "/api/shading/start")  # resolved before the close
    handler._body_json = lambda limit: {"generation": st.generation}
    handler._json = lambda obj, code=200: replies.append((code, obj))
    handler._error = lambda code, message: replies.append((code, message))
    handler.do_POST()
    assert replies == [(409, "This slide's Shading Correction controller is closed")]
    assert st._shading is None
