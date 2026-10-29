"""Server request leases, lock scope and response streaming."""
from __future__ import annotations

import email.message
import errno
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
    stopped = []
    st._shading = SimpleNamespace(cancel=lambda: stopped.append("cancel"), close=lambda: stopped.append("close"))
    with st.busy, st.exports:
        started = time.monotonic()
        with pytest.raises(ValueError, match="export"):
            registry.remove(SID)
        assert time.monotonic() - started < 0.5  # no drain wait
        assert not st.busy.closed  # tile reads stay admitted
        assert stopped == []
        assert registry.get(SID) is st
    assert registry.remove(SID) is True
    assert stopped == ["close"]  # closing a controller also cancels its work
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
        assert registry.get(SID) is None and calls == ["close"]
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


class _RacedLease(server._Lifecycle):
    """An export lease another request enters right after close looks at it.

    That is the moment a check made apart from the bar would miss: the
    first look at the lease (its count, or close_if_idle) is followed at
    once by an export request that resolved the slide before the close.
    """

    def __init__(self):
        super().__init__()
        self.arrivals = []

    def _arrive(self):
        if self.arrivals:
            return
        try:
            self.__enter__()
        except ValueError:
            self.arrivals.append("refused")
        else:
            self.arrivals.append("entered")

    @property
    def active(self):
        count = super().active
        self._arrive()
        return count

    def close_if_idle(self):
        idle = super().close_if_idle()
        self._arrive()
        return idle


def test_window_close_checks_and_bars_exports_in_one_step():
    st = state()
    st.exports = lease = _RacedLease()
    registry = window_registry(st)
    try:
        removed = registry.remove(SID)
    except ValueError as exc:  # the close may refuse for the export ...
        assert "export" in str(exc) and lease.arrivals == ["entered"]
        assert registry.get(SID) is st and not st.busy.closed
        # a refused close leaves later exports on the open tab possible
        assert not lease.closed
        return
    # ... or the export must be refused; never a close with it running.
    assert removed is True and lease.arrivals == ["refused"]
    assert lease.active == 0 and registry.active_export_count() == 0


def test_window_close_never_runs_the_correction_status_refresh():
    # The controller's cancel() reports status, and an idle controller's
    # status refreshes the pyramid, which waits up to 30 s for slide reads.
    # A close must not do that, least of all under the registry lock.
    st = state()
    calls = []

    class Controller:
        def cancel(self):
            calls.append("cancel")
            time.sleep(2)

        def close(self):
            calls.append("close")

    st._shading = Controller()
    registry = window_registry(st)
    started = time.monotonic()
    assert registry.remove(SID) is True
    assert time.monotonic() - started < 0.5
    assert calls == ["close"]


@pytest.mark.parametrize("where", ["headers", "body"])
def test_an_astromap_download_failure_is_not_reported_as_a_closed_slide(monkeypatch, tmp_path, where):
    st = astromap_state()
    registry = window_registry(st)
    registry.astromap_downloads = am.AstromapDownloads(tmp_path)
    handler, token = build_astromap(registry, st)
    if where == "headers":
        def no_header(filename):
            raise ValueError("header value unavailable")

        monkeypatch.setattr(server, "content_disposition", no_header)
    errors = []
    handler._error = lambda code, message: errors.append((code, message))
    handler.send_response = lambda code: None
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None

    def failing_write(chunk):
        raise ValueError("write to closed file")

    handler.wfile = SimpleNamespace(write=failing_write)
    # Not the export lease refusing: the request's catch-all handles it.
    with pytest.raises(ValueError, match="header value|closed file"):
        handler._astromap_download(st, {"token": [token]})
    assert errors == []
    assert st.exports.active == 0 and not st.exports.closed
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
    # The close finishes in the background once the replacement lets go.
    assert _wait_until(lambda: st._closed and st.busy.closed)
    assert st.generation == "corrected"
    # The close, not the replacement, has the last word.
    with pytest.raises(ValueError, match="closed"):
        st.busy.__enter__()


def test_window_close_does_not_wait_for_a_replacement_draining_reads():
    st = state()
    closes = []

    class Root(dict):
        def __init__(self, name):
            super().__init__()
            self.name = name

        def close(self, delay=None):
            closes.append((self.name, st.busy.active))

    class Replacement:
        staging = final = "staged"
        manifest = prior_manifest = {"generation": "corrected"}

        def publish(self):
            pass

    st.root = Root("old")
    opened = iter(["probe", "new"])
    st._open_pyramid = lambda path, manifest: (Root(next(opened)), st.attrs)
    registry = window_registry(st)
    reading, release = threading.Event(), threading.Event()

    def slow_read():  # a cold tile the replacement must wait for
        with st.busy:
            reading.set()
            assert release.wait(10)

    reader = threading.Thread(target=slow_read)
    reader.start()
    assert reading.wait(5)
    replacing = threading.Thread(target=st.replace_pyramid, args=(Replacement(),))
    replacing.start()
    # The replacement holds its lock while it drains the read.
    assert _wait_until(lambda: st.busy.closed)
    timer = threading.Timer(2.0, release.set)  # the read ends after 2 s
    timer.start()
    try:
        started = time.monotonic()
        assert registry.remove(SID) is True
        assert time.monotonic() - started < 0.5  # the tab close does not wait
        assert registry.get(SID) is None
        with pytest.raises(ValueError, match="closed"):
            with st.exports:  # exports are barred at once all the same
                pass
        assert registry.active_export_count() == 0
    finally:
        release.set()
        timer.cancel()
        reader.join(5)
        replacing.join(5)
    assert not replacing.is_alive() and st.generation == "corrected"
    # The background close waits for the replacement, marks the slide
    # closed, bars reads and closes the new root once no read is left.
    assert _wait_until(lambda: len(closes) == 3)
    assert closes == [("probe", 1), ("old", 0), ("new", 0)]
    assert st._closed and st.busy.closed


class _RecordedRoot(dict):
    def __init__(self, name, closes):
        super().__init__()
        self.name, self.closes = name, closes

    def close(self, delay=None):
        self.closes.append(self.name)


def _admissions_after(st, left):
    """Reads busy admits after ``left()`` says the slide has left the registry."""
    admitted, reopen = [], st.busy.reopen

    def spy():
        reopen()
        if not left():
            return
        try:
            st.busy.__enter__()
        except ValueError:
            return
        admitted.append(True)
        st.busy.__exit__(None, None, None)

    st.busy.reopen = spy
    return admitted


def test_window_close_while_a_replacement_holds_its_lock_bars_reads_at_once():
    st = state()
    closes = []
    st.root = _RecordedRoot("old", closes)
    registry = window_registry(st)
    probing, release = threading.Event(), threading.Event()
    opened = iter(["probe", "new"])

    def open_pyramid(path, manifest):
        name = next(opened)
        if name == "probe":  # validating the staged pyramid, before any drain
            probing.set()
            assert release.wait(5)
        return _RecordedRoot(name, closes), st.attrs

    class Replacement:
        staging = final = "staged"
        manifest = prior_manifest = {"generation": "corrected"}

        def publish(self):
            pass

    st._open_pyramid = open_pyramid
    replacing = threading.Thread(target=st.replace_pyramid, args=(Replacement(),))
    replacing.start()
    assert probing.wait(5)
    admitted = _admissions_after(st, lambda: registry.get(SID) is None)
    try:
        assert registry.remove(SID) is True
        # A request that resolved the slide before the close cannot read now.
        assert st._closed and st.busy.closed
        with pytest.raises(ValueError, match="closed"):
            st.busy.__enter__()
    finally:
        release.set()
        replacing.join(5)
    assert not replacing.is_alive() and st.generation == "corrected"
    # The replacement finished but never readmitted reads, and the new root
    # closes once it has let go.
    assert _wait_until(lambda: closes == ["probe", "old", "new"])
    assert admitted == [] and st.busy.closed


def test_a_refresh_that_finishes_after_a_window_close_does_not_readmit_reads(monkeypatch, tmp_path):
    container = tmp_path / "slide.nd2svs"
    container.write_bytes(b"first")
    st = ViewerState({}, state().attrs, container_path=container, manifest={"generation": "g1"})
    closes = []
    swapping, release = threading.Event(), threading.Event()

    class OldRoot(dict):
        def close(self, delay=None):
            closes.append("old")
            swapping.set()  # the refresh has drained reads and holds busy closed
            assert release.wait(5)

    st.root = OldRoot()
    registry = window_registry(st)
    time.sleep(0.01)
    container.write_bytes(b"replaced by another window")
    monkeypatch.setattr(cache, "read_manifest", lambda path, recover=False: {"generation": "g2"})
    st._open_pyramid = lambda path, manifest: (_RecordedRoot("new", closes), st.attrs)
    refreshing = threading.Thread(target=st.refresh_pyramid)
    refreshing.start()
    assert swapping.wait(5)
    admitted = _admissions_after(st, lambda: registry.get(SID) is None)
    try:
        assert registry.remove(SID) is True
    finally:
        release.set()
        refreshing.join(5)
    assert not refreshing.is_alive() and st.generation == "g2"
    assert _wait_until(lambda: closes == ["old", "new"])
    assert admitted == [] and st.busy.closed


def test_cache_trash_bars_reads_for_good_when_a_refresh_runs_meanwhile(monkeypatch, tmp_path):
    container = tmp_path / "slide.nd2svs"
    container.write_bytes(b"first")
    closes = []
    st = ViewerState(_RecordedRoot("old", closes), state().attrs, trash_path=container,
                     container_path=container, manifest={"generation": "g1"})
    registry = SlideRegistry()  # cache removal is a browser-mode action
    registry.slides[SID] = st
    monkeypatch.setattr(server, "_validate_file_cache_for_trash", lambda path, manifest: None)
    time.sleep(0.01)
    container.write_bytes(b"replaced by another window")
    monkeypatch.setattr(cache, "read_manifest", lambda path, recover=False: {"generation": "g2"})
    opening, release = threading.Event(), threading.Event()

    def open_pyramid(path, manifest):
        opening.set()  # holding the replacement lock
        assert release.wait(5)
        return _RecordedRoot("new", closes), st.attrs

    st._open_pyramid = open_pyramid
    refreshing = threading.Thread(target=st.refresh_pyramid)  # a status poll
    refreshing.start()
    assert opening.wait(5)
    errors = []

    def trash():
        try:
            registry.trash_cache(SID)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    trashing = threading.Thread(target=trash)
    trashing.start()
    time.sleep(0.2)  # the trash reaches its drain while the refresh holds the lock
    release.set()
    refreshing.join(5)
    trashing.join(10)
    assert not refreshing.is_alive() and not trashing.is_alive() and errors == []
    assert registry.get(SID) is None and not container.exists()
    # The refreshed root is the one closed, and no read is admitted again.
    assert closes == ["old", "new"]
    assert st._closed and st.busy.closed
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
    # Then at once: nothing reads it any more, so _Root's delay would only
    # keep its file open (a Windows sharing lock, a plate writer).
    assert _wait_until(lambda: closes == [(1, 0)])


@pytest.mark.parametrize("kind", ["slide", "plate"])
def test_an_idle_window_close_releases_the_source_without_delay(kind):
    from nd2wsi.direct_common import _Root

    closed = []

    class Plate:  # its own close waits for frame reads and its cache builder
        def close(self, timeout=300.0):
            closed.append(("plate", timeout))

    st = state()
    if kind == "plate":
        st.plate = Plate()
        st.root = _Root({}, closer=st.plate.close)
    else:
        st.root = _Root({}, closer=lambda: closed.append("root"))
    registry = window_registry(st)
    assert registry.remove(SID) is True
    if kind == "slide":
        assert closed == ["root"]  # on the request thread: nothing to wait for
    # A plate closes on the closer thread, but at once too, so reopening it
    # right away gets its cache writer back.
    assert _wait_until(lambda: closed == (["root"] if kind == "slide" else [("plate", None)]),
                       timeout=1.0)


@pytest.mark.parametrize("shutdown", ["relaunch", "immediate"])
def test_shutdown_waits_for_a_slide_closed_while_a_read_finishes(shutdown):
    from nd2wsi.direct_common import _Root

    st = state()
    closed = []
    st.root = _Root({}, closer=lambda: closed.append(st.busy.active))
    registry = window_registry(st)
    reading, release = threading.Event(), threading.Event()

    def slow_read():
        with st.busy:
            reading.set()
            assert release.wait(5)

    reader = threading.Thread(target=slow_read)
    reader.start()
    assert reading.wait(5)
    assert registry.remove(SID) is True
    assert closed == []  # the read still uses the root
    stop = (registry.close_all_for_relaunch if shutdown == "relaunch"
            else lambda: registry.close_all(immediate=True))
    stopped = threading.Event()
    stopping = threading.Thread(target=lambda: (stop(), stopped.set()))
    stopping.start()
    try:
        # The slide left the registry, but its root is still open: an update
        # or a stopped server must not go ahead past it.
        assert not stopped.wait(0.3)
    finally:
        release.set()
        reader.join(5)
    assert stopped.wait(5)
    stopping.join(5)
    assert closed == [0]


def test_a_window_close_without_a_closer_thread_still_closes_the_root(monkeypatch):
    st = state()
    closed = []

    class Root(dict):
        def close(self, delay=None):
            closed.append((delay, st.busy.active))

    st.root = Root()
    registry = window_registry(st)
    reading = threading.Event()

    def slow_read():
        with st.busy:
            reading.set()
            time.sleep(0.3)

    reader = threading.Thread(target=slow_read)
    reader.start()
    assert reading.wait(5)
    start = threading.Thread.start

    def no_closer_thread(thread):
        if thread.name == "nd2wsi-slide-close":
            raise RuntimeError("can't start new thread")
        return start(thread)

    monkeypatch.setattr(threading.Thread, "start", no_closer_thread)
    try:
        # Waiting here is the price; a close that is lost would leave the
        # slide's file open for the life of the process.
        assert registry.remove(SID) is True
        assert closed == [(0, 0)]
    finally:
        reader.join(5)
    assert registry._wait_for_closing(timeout=0)


def test_an_export_on_a_slide_reopened_from_a_changed_file_still_counts(monkeypatch, tmp_path):
    image = tmp_path / "slide.png"
    image.write_bytes(b"not decoded")
    closes, opened = [], []
    attrs = state().attrs

    def open_direct(path):
        opened.append(path)
        return _ClosingRoot(len(opened), closes), attrs

    prints = iter([{"size": 1, "mtime_ns": n, "quick_sha256": sha}
                   for n, sha in ((1, "a"), (1, "a"), (2, "b"), (2, "b"))])
    monkeypatch.setattr(cache, "quick_fingerprint", lambda path: next(prints))
    monkeypatch.setattr(raster, "open_direct", open_direct)
    registry = SlideRegistry()
    registry.window_session = SimpleNamespace(role="user")
    monkeypatch.setattr(registry, "annotation_path", lambda *args, **kwargs: None)
    sid = registry.add_direct(image)
    stale = registry.get(sid)
    with stale.busy, stale.exports:  # an export streaming from the old state
        assert registry.active_export_count() == 1
        assert registry.add_direct(image) == sid and registry.get(sid) is not stale
        # The update and close waits still see it ...
        assert registry.active_export_count() == 1
        with pytest.raises(ValueError, match="closed"):
            with stale.exports:  # ... and no later export starts on the old state
                pass
    assert registry.active_export_count() == 0
    assert registry._retired == []


def test_a_browser_close_or_trash_keeps_counting_a_running_download(tmp_path):
    def trashable():
        store = tmp_path / f"{len(list(tmp_path.iterdir()))}.ome.zarr"
        store.mkdir()
        (store / "zarr.json").write_text("{}")
        return ViewerState({}, state().attrs, trash_path=store)

    registry = SlideRegistry()  # browser mode: no window session
    closed, trashed = trashable(), trashable()
    registry.slides.update({"aaaaaaaa": closed, "bbbbbbbb": trashed})
    # AstroMap downloads hold only the export lease, never busy.
    with closed.exports, trashed.exports:
        assert registry.remove("aaaaaaaa") is True
        registry.trash_cache("bbbbbbbb")
        assert registry.slides == {}
        assert registry.active_export_count() == 2
        for st in (closed, trashed):
            with pytest.raises(ValueError, match="closed"):
                with st.exports:
                    pass
    assert registry.active_export_count() == 0


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


def shading_status_reply(registry, st):
    """GET status for a state the request resolved, whether or not it is still open."""
    replies = []
    handler = bare_handler(registry)
    handler.command, handler.path = "GET", f"/s/{SID}/api/shading/status"
    handler._gate = lambda: handler.path
    handler._resolve = lambda path: (st, "/api/shading/status")
    handler._json = lambda obj, code=200: replies.append((code, obj))
    handler._error = lambda code, message: replies.append((code, message))
    handler.do_GET()
    assert len(replies) == 1
    return replies[0]


def test_a_slide_closed_before_any_controller_reports_slide_closed():
    with_controller, without = state(), state()
    for st in (with_controller, without):
        st.generation = "g1"
    with_controller.shading  # a pane already asked for status
    registry = window_registry(with_controller)
    assert registry.remove(SID) is True
    registry.slides[SID] = without
    assert registry.remove(SID) is True
    code, closed = shading_status_reply(registry, with_controller)
    assert code == 200 and closed["state"] == "cancelled" and closed["reason"] == "Slide closed"
    # Never a server failure, and the same reply as the closed controller's.
    code, reply = shading_status_reply(registry, without)
    assert code == 200, reply
    assert without._shading is None
    assert set(reply) == set(closed)
    assert {key: reply[key] for key in ("state", "reason", "generation", "applied", "can_cancel")} == {
        "state": "cancelled", "reason": "Slide closed", "generation": "g1",
        "applied": False, "can_cancel": False,
    }


@pytest.mark.parametrize("error", [TimeoutError("timed out"), OSError(errno.ENETRESET, "network reset")])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_failed_plain_reply_body_writes_no_second_response(monkeypatch, method, error):
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
    assert len(rest) == 1 and b"HTTP/1.1" not in rest[0]  # the failed body, no 500 after it
    assert handler.close_connection is True
    # The next request on a reused handler answers errors normally again.
    handler.wfile = second = _Wire()
    handler.command, handler.path = "GET", "/s/ffffffff/api/info"
    handler.close_connection = False
    handler.do_GET()
    assert second.writes and second.writes[0].startswith(b"HTTP/1.1 404")


def test_failed_degraded_overview_open_closes_its_store_at_once(monkeypatch, tmp_path):
    from nd2wsi import convert

    closes = []
    # A malformed cache: its level has no downsample to halve.
    broken = {"nd2wsi": {"levels": [{"path": "0", "width": 8, "height": 8}]}, "multiscales": [{}]}
    monkeypatch.setattr(convert, "open_store", lambda path: (_ClosingRoot(1, closes), broken))
    store = tmp_path / "slide.nd2svs"
    store.write_bytes(b"")
    registry = SlideRegistry()
    with pytest.raises(KeyError):
        registry._add_overview(store, {"source": {"name": "slide.nd2"}}, source_path=tmp_path / "missing.nd2")
    assert closes == [(1, 0)]
    assert registry.slides == {}
