"""Export lifetime and HTTP compatibility at the transport/service boundary."""
from __future__ import annotations

import io
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
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


@pytest.mark.parametrize("error", [
    BrokenPipeError("gone"),
    # Windows sockets report a dropped download as aborted or reset.
    ConnectionAbortedError("aborted"),
    ConnectionResetError("reset"),
    OSError("disk failure"),
])
def test_failed_writer_and_disconnected_consumer_cleanup_without_done(monkeypatch, error):
    paths = []

    def write(root, attrs, destination, *args, on_progress):
        paths.append(Path(destination))
        Path(destination).write_bytes(b"partial pixels")
        if not isinstance(error, ConnectionError):
            raise error

    monkeypatch.setattr(render, "export_roi_tiff", write)
    events, update = progress_log()
    with pytest.raises(type(error), match=str(error)):
        with request().file(update):
            raise error
    assert not paths[0].exists()
    assert "done" not in [event["state"] for event in events]
    assert events[-1]["error"] == (
        "client disconnected" if isinstance(error, ConnectionError) else "OSError: disk failure"
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


@pytest.mark.parametrize("disconnect", [BrokenPipeError, ConnectionResetError])
def test_window_slide_cannot_close_during_transmission_and_lease_releases_on_disconnect(
        monkeypatch, disconnect):
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
            raise disconnect("gone")

    handler.wfile = DisconnectedSink()

    def run():
        try:
            handler._roi(st, query(job="disconnect-lifetime"))
        except disconnect as exc:
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
    ({"level": "abc"}, "invalid level"),
    ({"level": "inf"}, "invalid level"),
    ({"x": "invalid"}, "could not convert string to float"),
    ({"w": "nan"}, "invalid w"),
    ({"format": "bmp"}, "unknown format bmp"),
    ({"format": "png", "scalebar": 1}, "Scale bar export supports SVG and JPEG"),
    ({"c": "abc"}, "invalid channel list c='abc'"),
    ({"c": "abc", "format": "png"}, "invalid channel list c='abc'"),
    ({"c": "none"}, "Select at least one channel to export"),
    ({"c": "none", "format": "tif"}, "Select at least one channel to export"),
    ({"c": "none", "format": "nd2"}, "Select at least one channel to export"),
])
def test_adapter_preserves_bad_request_status_and_message(params, message):
    handler_type = make_handler(SlideRegistry())
    handler = handler_type.__new__(handler_type)
    handler._error = lambda status, error: (status, error)
    st = state()
    status, error = handler._roi(st, query(**params))
    assert status == 400
    assert message in error
    assert st.busy.active == 0


def test_rendered_export_still_accepts_the_explicit_all_hidden_selection():
    # Rendered exports keep today's behaviour for c=none until that is decided.
    assert request(format="png", c="none").channels == []
    assert request(format="jpg", c="none").channels == []
    assert request(format="tiff", c="1").channels == [1]


@pytest.fixture
def persisted_correction(tmp_path):
    """Expected RAW levels come from the real converter, including rounding."""
    import zarr

    from nd2wsi.convert import _downsample_into, build_group_attrs
    from nd2wsi.reader import ChannelInfo, PlaneSource

    raw = np.random.default_rng(19).integers(0, 48, (2, 131, 519), dtype=np.uint16)
    # Repeated integer rounding differs from a single 4x mean here.
    raw[:, :4, :4] = [[0, 0, 0, 0], [1, 1, 1, 1],
                       [0, 0, 1, 1], [1, 1, 2, 2]]
    raw[:, -1, :] = 65535
    raw[:, :, -1] = 65535
    store = zarr.open_group(str(tmp_path / "original.zarr"), mode="w", zarr_format=2)
    a = store.create_array("0", shape=raw.shape, chunks=(1, 64, 64), dtype=raw.dtype)
    a[:] = raw
    expected = {"0": raw}
    shapes = [raw.shape[1:]]
    for level in range(1, 9):
        ph, pw = shapes[-1]
        shape = (max(1, ph // 2), max(1, pw // 2))
        target = store.create_array(str(level), shape=(2, *shape),
                                    chunks=(1, 64, 64), dtype=raw.dtype)
        _downsample_into(store[str(level - 1)], target, 64, raw.dtype)
        expected[str(level)] = np.asarray(target[:])
        shapes.append(shape)

    class Native:
        shape, dtype = raw.shape, raw.dtype

        def __init__(self):
            self.read_shapes = []

        def __getitem__(self, index):
            values = raw[index].copy()
            self.read_shapes.append(values.shape[-2:])
            return values

    class Root(dict):
        pass

    root = Root({k: v.astype(np.float32) + 80 for k, v in expected.items()})
    root.raw_level0 = Native()
    src = PlaneSource(data=None, dtype=raw.dtype, shape=raw.shape, rgb=False,
                      channels=[ChannelInfo("FITC", (255, 255, 255)),
                                ChannelInfo("CY3", (255, 255, 255))],
                      pixel_size_um=(0.5, 0.5), source_name="example.nd2",
                      selection={"t": 0, "p": 0, "z": 0})
    attrs = build_group_attrs(src, shapes, 64,
                              [dict(start=0, end=255, min=0, max=255)] * 2)
    st = ViewerState(root, attrs, manifest={"h_correction": {"applied": True}})
    try:
        yield st, expected
    finally:
        store.store.close()


@pytest.mark.parametrize("fmt", ["png", "jpg", "svg"])
def test_persisted_rendered_exports_use_saved_display_and_suffix(persisted_correction, fmt):
    from PIL import Image

    st, expected = persisted_correction
    w, h = (128, 80) if fmt == "svg" else (25, 19)
    q = query(x=5, y=7, w=w, h=h, format=fmt)
    shown = prepare_export(st, q, resolve_frame=_frame_args)
    # A legacy URL cannot select a private correction revision anymore.
    legacy = prepare_export(st, q | {"correction": ["old-private-revision"]}, resolve_frame=_frame_args)
    assert shown.root is st.root and legacy.root is st.root
    result = shown.rendered()
    assert "_shading-corrected_" in result.filename
    if fmt == "png":
        raw_state = ViewerState(expected, st.attrs)
        uncorrected = prepare_export(raw_state, q, resolve_frame=_frame_args).rendered()
        assert np.asarray(Image.open(io.BytesIO(result.body))).mean() > np.asarray(
            Image.open(io.BytesIO(uncorrected.body))).mean()


@pytest.mark.parametrize("fmt", ["tiff", "nd2"])
@pytest.mark.parametrize("level", [0, 2])
def test_persisted_native_exports_roundtrip_original_values_at_requested_level(
        persisted_correction, fmt, level):
    st, expected = persisted_correction
    if fmt == "nd2":
        pytest.importorskip("limnd2")
        nd2 = pytest.importorskip("nd2")
    q = query(x=3, y=5, w=19, h=17, format=fmt, level=level, c="1,0")
    plan = prepare_export(st, q | {"correction": ["ignored-legacy-revision"]}, resolve_frame=_frame_args)
    with plan.file(lambda *args, **kwargs: None) as result:
        assert "shading-corrected" not in result.filename
        if fmt == "nd2":
            with nd2.ND2File(str(result.path)) as reader:
                exported = np.asarray(reader.read_frame(0)).copy()
                assert reader.voxel_size().x == pytest.approx(0.5 * 2**level)
                assert [ch.channel.name for ch in reader.metadata.channels] == ["CY3", "FITC"]
        else:
            import tifffile

            exported = tifffile.imread(result.path)
        assert exported.dtype == np.uint16
        np.testing.assert_array_equal(exported, expected[str(level)][[1, 0], 5:22, 3:22])


def test_original_reduction_matches_converter_rounding_odd_edges_and_collapsed_axes(persisted_correction):
    st, expected = persisted_correction
    for level in (1, 2, 8):
        plan = prepare_export(st, query(level=level), resolve_frame=_frame_args)
        actual = plan.root[str(level)][:, :, :]
        np.testing.assert_array_equal(actual, expected[str(level)])
    assert expected["2"][0, 0, 0] == 0
    assert np.rint(expected["0"][0, :4, :4].mean()) == 1
    assert max(max(shape) for shape in st.root.raw_level0.read_shapes) <= 256


@pytest.mark.parametrize("fmt", ["nd2", "tiff"])
def test_native_export_requires_original_source_when_corrected_cache_is_degraded(persisted_correction, fmt):
    st, _ = persisted_correction
    del st.root.raw_level0
    with pytest.raises(ExportRequestError, match="reopen the original ND2"):
        prepare_export(st, query(format=fmt, level=2), resolve_frame=_frame_args)
    assert prepare_export(st, query(format="png"), resolve_frame=_frame_args).root is st.root
