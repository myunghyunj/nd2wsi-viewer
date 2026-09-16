"""Export lifetime and HTTP compatibility at the transport/service boundary."""
from __future__ import annotations

import io
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from nd2wsi import render
from nd2wsi.region_export import ExportRequestError, prepare_export
from nd2wsi.server import SlideRegistry, ViewerState, _frame_args, _job_get, make_handler


def state(*, plate=None):
    attrs = {
        "nd2wsi": {"source": "example.nd2", "levels": [{"path": "0", "width": 80, "height": 60}]},
        "omero": {"channels": [{}, {}]},
    }
    return ViewerState({}, attrs, plate=plate)


def query(**kwargs):
    params = {"x": 2, "y": 3, "w": 10, "h": 20, "format": "tiff", "job": "service-test"}
    return {key: [str(value)] for key, value in (params | kwargs).items()}


def request(**kwargs):
    return prepare_export(state(), query(**kwargs), resolve_frame=_frame_args)


def progress_log():
    events = []
    return events, lambda job, **values: events.append({"job": job, **values})


def test_stream_has_bounded_reads_and_keeps_temporary_file_until_consumer_finishes(monkeypatch):
    paths = []
    payload = b"pixels" * 400_000  # exceeds two transfer chunks

    def write(root, attrs, destination, *args, on_progress):
        paths.append(Path(destination))
        Path(destination).write_bytes(payload)
        on_progress(0.5)
        on_progress(1.0)

    monkeypatch.setattr(render, "export_roi_tiff", write)
    events, update = progress_log()
    sizes = []
    output = io.BytesIO()

    class Sink:
        def write(self, chunk):
            sizes.append(len(chunk))
            output.write(chunk)

    with request().file(update) as result:
        assert result.size == len(payload)
        assert result.path.exists()
        assert result.filename == "example_L0_x2_y3_10x20.tif"
        assert result.content_type == "image/tiff"
        result.copy_to(Sink())
        assert result.path.exists()
        assert events[-1]["state"] == "streaming"
    assert output.getvalue() == payload
    assert max(sizes) == 1024 * 1024
    assert not paths[0].exists()
    assert events[-1] == {"job": "service-test", "state": "done", "pct": 100}


@pytest.mark.parametrize("error", [BrokenPipeError("gone"), OSError("disk failure")])
def test_failed_writer_and_disconnected_consumer_cleanup_without_done(monkeypatch, error):
    paths = []

    def write(root, attrs, destination, *args, on_progress):
        paths.append(Path(destination))
        Path(destination).write_bytes(b"partial pixels")
        if not isinstance(error, BrokenPipeError):
            raise error

    monkeypatch.setattr(render, "export_roi_tiff", write)
    events, update = progress_log()
    with pytest.raises(type(error), match=str(error)):
        with request().file(update):
            raise error
    assert not paths[0].exists()
    assert "done" not in [event["state"] for event in events]
    assert events[-1]["error"] == (
        "client disconnected" if isinstance(error, BrokenPipeError) else "OSError: disk failure"
    )


def test_nd2_writer_rejection_remains_client_error_and_cleans_partial_file(monkeypatch):
    import nd2wsi.export_nd2 as module

    paths = []

    def reject(root, attrs, destination, *args, **kwargs):
        paths.append(Path(destination))
        Path(destination).write_bytes(b"partial")
        raise RuntimeError("export unavailable")

    monkeypatch.setattr(module, "export_roi_nd2", reject)
    events, update = progress_log()
    with pytest.raises(ExportRequestError, match="export unavailable"):
        with request(format="nd2").file(update):
            pytest.fail("a rejected file cannot be streamed")
    assert not paths[0].exists()
    assert events[-1]["error"] == "export unavailable"


def test_plate_scope_and_agent_filename_refer_to_the_same_requested_root():
    roots = []

    def root_for(*frame):
        root = {"frame": frame}
        roots.append(root)
        return root

    st = state(plate=SimpleNamespace(T=3, P=2, Z=4, z_home=1, root_for=root_for))
    with pytest.raises(ExportRequestError, match="choose a plate site"):
        prepare_export(st, query(), resolve_frame=_frame_args)
    assert not roots
    export = prepare_export(
        st, query(t=2, p=1, z=3, x=-2, y=59, w=100, h=30),
        resolve_frame=_frame_args, filename_prefix="agent-session_",
    )
    assert export.root is roots[0]
    assert export.root["frame"] == (2, 1, 3)
    assert (export.x, export.y, export.w, export.h) == (0, 59, 80, 1)
    assert export.filename == "agent-session_example_t2_p1_z3_L0_x0_y59_80x1"


def test_window_slide_cannot_close_during_transmission_and_lease_releases_on_disconnect(monkeypatch):
    paths = []

    def write(root, attrs, destination, *args, **kwargs):
        paths.append(Path(destination))
        Path(destination).write_bytes(b"pixels")

    monkeypatch.setattr(render, "export_roi_tiff", write)
    st = state()
    registry = SlideRegistry()
    registry.window_session = SimpleNamespace(role="user")
    registry.slides["12345678"] = st
    handler_type = make_handler(registry)
    handler = handler_type.__new__(handler_type)
    handler.send_response = lambda code: None
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None
    writing, release = threading.Event(), threading.Event()
    failures = []

    class DisconnectedSink:
        def write(self, chunk):
            writing.set()
            assert release.wait(5)
            raise BrokenPipeError("gone")

    handler.wfile = DisconnectedSink()

    def run():
        try:
            handler._roi(st, query(job="disconnect-lifetime"))
        except BrokenPipeError as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert writing.wait(5)
        assert st.busy.active == 1
        with pytest.raises(ValueError, match="export"):
            registry.remove("12345678")
        assert paths[0].exists()
        assert _job_get("disconnect-lifetime")["state"] == "streaming"
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and len(failures) == 1
    assert st.busy.active == 0
    assert not paths[0].exists()
    assert _job_get("disconnect-lifetime")["error"] == "client disconnected"
    assert registry.remove("12345678") is True


@pytest.mark.parametrize("params,message", [
    ({"level": 4}, "level 4 out of range"),
    ({"x": "invalid"}, "could not convert string to float"),
    ({"format": "bmp"}, "unknown format bmp"),
    ({"format": "png", "scalebar": 1}, "Scale bar export supports SVG and JPEG"),
])
def test_adapter_preserves_bad_request_status_and_message(params, message):
    handler_type = make_handler(SlideRegistry())
    handler = handler_type.__new__(handler_type)
    handler._error = lambda status, error: (status, error)
    status, error = handler._roi(state(), query(**params))
    assert status == 400
    assert message in error
