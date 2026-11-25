"""Tracing overlays own their window, lock state and display-only context."""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app
from nd2wsi import translucent_window as overlay


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self

    def is_set(self):
        return False


@pytest.fixture
def store():
    return SimpleNamespace(
        attrs={"nd2wsi": {"rgb": False, "levels": [{"width": 8000, "height": 6000}]},
               "omero": {"channels": [{}, {}]}}, generation="pixels-2", plate=None,
    )


@pytest.fixture
def payload():
    return {"version": 1, "query": "?c=1&win=1%3A500%3A0.25%2C2%3A600&g=pixels-2",
            "center": [4000, 3000], "zoom": 0.75, "rotation": 90, "flip": True}


def test_context_preserves_display_query_and_image_coordinates(store, payload):
    before = copy.deepcopy(store.attrs)
    result = overlay.validate_context(store, payload, slide_url="http://local/token/s/abc/")
    assert result["query"] == payload["query"]
    assert result["center"] == [4000, 3000]
    assert result["zoom"] == 0.75
    assert result["rotation"] == 90
    assert result["flip"] is True
    assert result["dimensions"] == [8000, 6000]
    assert result["opacity"] == overlay.OPACITY
    assert store.attrs == before


@pytest.mark.parametrize("change", [
    {"query": "?c=9&g=pixels-2"}, {"query": "?c=0&c=1&g=pixels-2"},
    {"query": "?g=old"}, {"query": "?g=pixels-2&url=https%3A%2F%2Fexample.com"},
    {"query": "?g=pixels-2&correction=old"}, {"query": "?g=pixels-2&t=0"},
    {"query": "?g=pixels-2&win=NaN:100"}, {"query": "?g=pixels-2&win=2:1"},
    {"query": "?g=pixels-2&win=1:100:0"}, {"query": "?g=pixels-2&win=1:100,2:100,3:100"},
    {"center": [float("inf"), 1]}, {"zoom": 0}, {"zoom": True},
    {"flip": "true"}, {"version": True}, {"roi": {"x": 7999, "y": 1, "w": 2, "h": 1}},
])
def test_rejects_unbounded_mismatched_or_unrelated_context(store, payload, change):
    with pytest.raises(ValueError):
        overlay.validate_context(store, {**payload, **change}, slide_url="http://local/token/s/abc/")


def test_plate_requires_explicit_valid_site_and_planes(store, payload):
    store.plate = SimpleNamespace(T=3, P=4, Z=5)
    with pytest.raises(ValueError, match="plate site"):
        overlay.validate_context(store, payload, slide_url="local/")
    payload["query"] += "&t=2&p=3&z=4"
    assert overlay.validate_context(store, payload, slide_url="local/")["query"].endswith("t=2&p=3&z=4")
    payload["query"] = payload["query"].replace("p=3", "p=4")
    with pytest.raises(ValueError, match="plate site"):
        overlay.validate_context(store, payload, slide_url="local/")


def test_rgb_is_not_a_fluorescence_overlay(store, payload):
    store.attrs["nd2wsi"]["rgb"] = True
    with pytest.raises(ValueError, match="fluorescence"):
        overlay.validate_context(store, payload, slide_url="local/")


def test_registered_raster_jpg_can_open_with_its_current_display_state(store, payload):
    store.attrs["nd2wsi"].update(rgb=True, raster=True)
    result = overlay.validate_context(store, payload, slide_url="local/")
    assert result["query"] == payload["query"]
    assert result["center"] == payload["center"]


def test_existing_imported_roi_snapshot_preserves_source_geometry_without_selecting(store, payload):
    rois = [{"x": 5028, "y": 4464, "w": 7, "h": 4, "index": 9, "label": "9"},
            {"x": 10, "y": 20, "w": 1, "h": 1, "index": 10, "label": "single pixel"}]
    result = overlay.validate_context(store, {**payload, "rois": rois}, slide_url="local/")
    assert result["rois"] == rois
    assert "roiIndex" not in result
    result["rois"][0]["x"] = 1
    assert rois[0]["x"] == 5028
    selected = overlay.validate_context(store, {**payload, "rois": rois, "roiIndex": 1}, slide_url="local/")
    assert selected["roiIndex"] == 1


@pytest.mark.parametrize("change", [
    {"x": -1}, {"w": 0}, {"x": 7999, "w": 2}, {"y": float("nan")},
    {"index": True}, {"index": 0}, {"index": 2**53}, {"label": None}, {"label": "x" * 1001},
])
def test_imported_roi_snapshot_rejects_invalid_geometry_or_metadata(store, payload, change):
    roi = {"x": 1, "y": 2, "w": 3, "h": 4, "index": 1, "label": "ROI 1", **change}
    with pytest.raises(ValueError):
        overlay.validate_context(store, {**payload, "rois": [roi]}, slide_url="local/")


@pytest.mark.parametrize("rois,selected", [
    (None, 0), ([], 0), ("unexpected", 0),
    ([{"x": 1, "y": 2, "w": 3, "h": 4, "index": 1, "label": "ROI 1"}], True),
    ([{"x": 1, "y": 2, "w": 3, "h": 4, "index": 1, "label": "ROI 1"}], 1),
])
def test_imported_roi_snapshot_rejects_invalid_selection(store, payload, rois, selected):
    with pytest.raises(ValueError):
        overlay.validate_context(store, {**payload, "rois": rois, "roiIndex": selected}, slide_url="local/")


def test_imported_roi_snapshot_rejects_duplicates_or_more_than_2000_rois(store, payload):
    roi = {"x": 1, "y": 2, "w": 3, "h": 4, "index": 1, "label": "ROI 1"}
    for rois in ([roi, roi], [roi] * 2001):
        with pytest.raises(ValueError):
            overlay.validate_context(store, {**payload, "rois": rois}, slide_url="local/")


@pytest.fixture
def raw_roi_pair():
    return {"ok": True, "name": "roi.txt", "text": "INDEX\tX1\tY1\tX2\tY2\tNOTE\n9\t1\t2\t1\t2\t9\n",
            "coordinateSize": {"width": 800, "height": 600},
            "reference": {"name": "roi.jpg", "width": 800, "height": 600}}


@pytest.fixture
def cocoa_api(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    queued, logs = [], []
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: SimpleNamespace(addOperationWithBlock_=queued.append)),
    ))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSMakePoint=lambda x, y: (x, y), NSWindowStyleMaskBorderless=0,
        NSFloatingWindowLevel=3, NSWindowCollectionBehaviorFullScreenAuxiliary=256,
        NSColor=SimpleNamespace(clearColor=lambda: "clear"),
    ))
    frame = SimpleNamespace(origin=SimpleNamespace(x=-1400, y=900),
                            size=SimpleNamespace(width=640, height=480))
    native = Mock(frame=lambda: frame)
    window = SimpleNamespace(native=native, destroy=Mock(), create_file_dialog=Mock(),
                             evaluate_js=Mock())
    closed = Mock()
    api = overlay.TranslucentApi({"dimensions": [8000, 6000], "center": [1, 2]},
                                logger=logs.append, on_closed=closed)
    api._window = window
    return SimpleNamespace(**locals())


def test_native_move_uses_relative_screen_coordinates_and_resize_keeps_top_left(cocoa_api):
    q = cocoa_api
    assert q.api.move_window(50, -25)
    q.native.setFrameOrigin_.assert_not_called()
    q.queued.pop(0)()
    q.native.setFrameOrigin_.assert_called_once_with((-1350, 925))
    assert q.api.resize_window(800, 600) == {"ok": True, "width": 800, "height": 600}
    q.queued.pop(0)()
    assert q.frame.origin.y == 780
    assert q.frame.size.width == 800
    assert q.frame.size.height == 600
    assert q.api.resize_window(1, 1) == {"ok": True, "width": 220, "height": 160}


def test_lock_cancels_queued_geometry_keeps_unlock_and_close_available(cocoa_api):
    q = cocoa_api
    q.api.move_window(40, 30)
    q.api.resize_window(700, 500)
    assert q.api.set_translucent_locked(True) == {"ok": True, "locked": True}
    assert not q.api.move_window(1, 1)
    assert not q.api.resize_window(700, 500)["ok"]
    assert not q.api.start_window_drag()
    assert q.api.set_translucent_locked(False) == {"ok": True, "locked": False}
    for operation in q.queued:
        operation()
    q.native.setFrameOrigin_.assert_not_called()
    q.native.setFrame_display_.assert_not_called()
    q.api.set_translucent_locked(True)
    assert q.api.close_translucent()
    assert q.api.close_translucent()
    q.window.destroy.assert_called_once()
    q.closed.assert_called_once_with(q.api)


def test_native_configuration_uses_standalone_transparent_top_level(cocoa_api):
    q = cocoa_api
    q.api._configure_and_show()
    q.queued.pop(0)()
    q.native.setOpaque_.assert_called_once_with(False)
    q.native.setBackgroundColor_.assert_called_once_with("clear")
    q.native.setHidesOnDeactivate_.assert_called_once_with(False)
    q.native.setLevel_.assert_called_once_with(3)
    q.native.setStyleMask_.assert_called_once_with(0)
    q.native.makeKeyAndOrderFront_.assert_called_once()
    q.native.addChildWindow_ordered_.assert_not_called()
    q.api._configure_and_show()
    q.queued.pop(0)()
    q.native.makeKeyAndOrderFront_.assert_called_once()


@pytest.mark.parametrize("value", [True, False, None, ".3", float("nan"), float("inf"), -.1, 0, .099, 1.01])
def test_opacity_rejects_invalid_values_without_changing_display(cocoa_api, value):
    q = cocoa_api
    result = q.api.set_translucent_opacity(value)
    assert result["ok"] is False and result["opacity"] == overlay.OPACITY
    assert q.api.translucent_context()["opacity"] == overlay.OPACITY
    assert not q.queued and not q.native.mock_calls


def test_mac_opacity_persists_image_layer_value_and_rejects_locked_or_closed_changes(cocoa_api):
    q = cocoa_api
    for value in (.1, .3, .8, 1):
        assert q.api.set_translucent_opacity(value) == {"ok": True, "opacity": value}
        context = q.api.translucent_context()
        assert context["opacity"] == value and context["opacity_mode"] == "layer"
        context["opacity"] = .4
        assert q.api.translucent_context()["opacity"] == value
    q.native.setAlphaValue_.assert_not_called()
    q.api.set_translucent_locked(True)
    assert not q.api.set_translucent_opacity(.3)["ok"]
    assert q.api.translucent_context()["opacity"] == 1
    q.api.set_translucent_locked(False)
    q.api.close_translucent()
    assert not q.api.set_translucent_opacity(.3)["ok"]


@pytest.fixture
def windows_opacity_api(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda operation: operation))
    native = SimpleNamespace(InvokeRequired=False, Opacity=overlay.OPACITY)
    api = overlay.TranslucentApi({}, logger=Mock(), on_closed=Mock())
    api._window = SimpleNamespace(native=native)
    return api, native


def test_windows_opacity_applies_native_alpha_before_returning_actual_value(windows_opacity_api):
    api, native = windows_opacity_api
    for value in (.1, .3, .8, 1):
        assert api.set_translucent_opacity(value) == {"ok": True, "opacity": value}
        assert native.Opacity == value
        assert api.translucent_context()["opacity"] == value
        assert api.translucent_context()["opacity_mode"] == "native"
    api.set_translucent_locked(True)
    assert not api.set_translucent_opacity(.3)["ok"]
    assert native.Opacity == 1


def test_windows_opacity_discards_queued_request_after_lock_cycle(windows_opacity_api, monkeypatch):
    api, native = windows_opacity_api

    def lock_cycle_then_apply(callback):
        # A lock/unlock between the bridge call and its UI callback must cancel
        # that request even though the current lock value is false again.
        api._epoch += 2
        callback()

    monkeypatch.setattr(api, "_dispatch", lock_cycle_then_apply)
    assert not api.set_translucent_opacity(.3)["ok"]
    assert native.Opacity == overlay.OPACITY
    assert api.translucent_context()["opacity"] == overlay.OPACITY


def test_windows_opacity_timeout_cancels_late_native_callback(windows_opacity_api, monkeypatch):
    api, native = windows_opacity_api
    queued = []
    monkeypatch.setattr(api, "_dispatch", queued.append)
    monkeypatch.setattr(overlay, "OPACITY_TIMEOUT", .001)
    result = api.set_translucent_opacity(.3)
    assert not result["ok"] and "timed out" in result["message"]
    queued.pop()()
    assert native.Opacity == overlay.OPACITY
    assert api.translucent_context()["opacity"] == overlay.OPACITY


def test_windows_opacity_native_failure_preserves_confirmed_context(windows_opacity_api):
    api, _ = windows_opacity_api

    class UnavailableNative:
        InvokeRequired = False

        @property
        def Opacity(self):
            return overlay.OPACITY

        @Opacity.setter
        def Opacity(self, value):
            raise RuntimeError("native window is unavailable")

    api._window.native = UnavailableNative()
    result = api.set_translucent_opacity(.3)
    assert not result["ok"] and "unavailable" in result["message"]
    assert result["opacity"] == api.translucent_context()["opacity"] == overlay.OPACITY


def test_roi_picker_reads_only_own_window_pair_and_respects_lock(cocoa_api, monkeypatch, tmp_path, raw_roi_pair):
    q = cocoa_api
    load = Mock(return_value=raw_roi_pair)
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))
    foreign = SimpleNamespace(create_file_dialog=Mock())
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=10, windows=[foreign]))
    text, image = tmp_path / "roi.txt", tmp_path / "roi.jpg"
    q.window.create_file_dialog.return_value = [str(text), str(image)]
    assert q.api.choose_roi_file() == raw_roi_pair
    load.assert_called_once_with(text, width=8000, height=6000, image_path=image)
    assert q.window.create_file_dialog.call_args.kwargs["allow_multiple"] is True
    foreign.create_file_dialog.assert_not_called()
    q.api.set_translucent_locked(True)
    assert not q.api.choose_roi_file()["ok"]
    assert q.window.create_file_dialog.call_count == 1
    assert not hasattr(q.api, "read_roi_file")


@pytest.mark.parametrize("first_suffix,second_suffix", [(".txt", ".JpEg"), (".JPG", ".TXT")])
def test_single_file_picker_auto_finds_unique_case_insensitive_sibling(
    cocoa_api, monkeypatch, tmp_path, first_suffix, second_suffix, raw_roi_pair,
):
    q = cocoa_api
    load = Mock(return_value=raw_roi_pair)
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=10))
    first, second = tmp_path / ("roi" + first_suffix), tmp_path / ("ROI" + second_suffix)
    first.touch()
    second.touch()
    q.window.create_file_dialog.return_value = [str(first)]
    assert q.api.choose_roi_file()["ok"]
    assert q.window.create_file_dialog.call_count == 1
    text, image = (first, second) if first_suffix == ".txt" else (second, first)
    load.assert_called_once_with(text, width=8000, height=6000, image_path=image)


def test_single_txt_picker_requests_missing_jpg_then_imports_pair(cocoa_api, monkeypatch, tmp_path, raw_roi_pair):
    q = cocoa_api
    load = Mock(return_value=raw_roi_pair)
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=10))
    text, image = tmp_path / "roi.txt", tmp_path / "elsewhere" / "roi.jpg"
    text.touch()
    q.window.create_file_dialog.side_effect = [[str(text)], [str(image)]]
    assert q.api.choose_roi_file()["ok"]
    assert q.window.create_file_dialog.call_count == 2
    assert q.window.create_file_dialog.call_args.kwargs["allow_multiple"] is False
    assert q.window.create_file_dialog.call_args.kwargs["directory"] == str(tmp_path)
    load.assert_called_once_with(text, width=8000, height=6000, image_path=image)


def test_cancelled_second_picker_does_not_import_partial_roi_data(cocoa_api, monkeypatch, tmp_path):
    q = cocoa_api
    load = Mock()
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=10))
    text = tmp_path / "roi.txt"
    text.touch()
    q.window.create_file_dialog.side_effect = [[str(text)], None]
    assert q.api.choose_roi_file() == {"ok": False, "cancelled": True}
    load.assert_not_called()


@pytest.mark.parametrize("names", [
    ["a.txt", "b.jpg"], ["a.txt", "a.txt"], ["a.jpg", "a.jpeg"],
    ["a.txt", "a.png"], ["a.txt", "a.jpg", "a.jpeg"], [],
])
def test_pair_import_rejects_mismatched_extra_or_unsupported_files(cocoa_api, monkeypatch, tmp_path, names):
    load = Mock()
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))
    result = cocoa_api.api._load_roi_files([tmp_path / name for name in names])
    assert not result["ok"]
    assert result["message"]
    load.assert_not_called()


def test_drop_imports_pair_atomically_and_does_not_open_file_picker(cocoa_api, monkeypatch, tmp_path, raw_roi_pair):
    q = cocoa_api
    handlers = []
    load = Mock(return_value=raw_roi_pair)
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))

    def handler(callback, *, prevent_default):
        assert prevent_default
        handlers.append(callback)
        return callback

    monkeypatch.setitem(sys.modules, "webview.dom", SimpleNamespace(DOMEventHandler=handler))
    q.window.dom = SimpleNamespace(document=SimpleNamespace(events=SimpleNamespace(drop=Event())))
    q.api._wire_drop()
    q.api._wire_drop()
    assert len(handlers) == 1
    text, image = tmp_path / "roi.txt", tmp_path / "roi.jpg"
    text.touch()
    image.touch()
    handlers[0]({"dataTransfer": {"files": [{"pywebviewFullPath": str(image)},
                                             {"pywebviewFullPath": str(text)}]}})
    load.assert_called_once_with(text, width=8000, height=6000, image_path=image)
    assert "nd2TranslucentSetRois" in q.window.evaluate_js.call_args.args[0]
    q.window.create_file_dialog.assert_not_called()
    load.reset_mock()
    # One dropped JPG can name its unique sibling TXT, but a missing JPG may
    # never use rectangles directly against the ND2's different pixel grid.
    handlers[0]({"dataTransfer": {"files": [{"pywebviewFullPath": str(image)}]}})
    load.assert_called_once_with(text, width=8000, height=6000, image_path=image)
    load.reset_mock()
    image.unlink()
    handlers[0]({"dataTransfer": {"files": [{"pywebviewFullPath": str(text)}]}})
    load.assert_not_called()
    assert "missing" in q.window.evaluate_js.call_args.args[0]
    q.api.set_translucent_locked(True)
    handlers[0]({"dataTransfer": {"files": [{"pywebviewFullPath": str(text)}]}})
    load.assert_not_called()
    assert "Unlock" in q.window.evaluate_js.call_args.args[0]
    q.window.create_file_dialog.assert_not_called()


def test_single_dropped_txt_rejects_ambiguous_reference_jpg(cocoa_api, monkeypatch, tmp_path):
    load = Mock()
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))
    for name in ("roi.txt", "roi.jpg", "roi.jpeg"):
        (tmp_path / name).touch()
    result = cocoa_api.api._load_roi_files([tmp_path / "roi.txt"])
    assert not result["ok"]
    assert "More than one" in result["message"]
    load.assert_not_called()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_creation_uses_hidden_independent_window_and_waits_for_content(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    window = SimpleNamespace(events=SimpleNamespace(**{
        name: Event() for name in ("shown", "closed", "closing", "before_load", "loaded")
    }))
    create = Mock(return_value=window)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create))
    api = overlay.create_translucent_window({}, url="http://local/token/static/translucent.html",
                                            title="Overlay", logger=Mock(), on_closed=Mock())
    options = create.call_args.kwargs
    assert options["js_api"] is api
    assert options["hidden"] and options["on_top"]
    assert options["transparent"] is (platform == "darwin")
    assert options["frameless"]
    assert not options["resizable"] and not options["easy_drag"]
    assert "parent" not in options
    assert len(window.events.loaded.handlers) == 2
    # A display-only overlay closes at once; only the viewer window has a save veto.
    assert window.events.closing.handlers == []


def test_parent_registry_owns_overlay_and_cleans_up_without_touching_source(store, payload, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    source_window = Mock()
    child = SimpleNamespace(_window=SimpleNamespace(uid="trace-1"), close_translucent=Mock())
    create = Mock(return_value=child)
    monkeypatch.setattr(overlay, "create_translucent_window", create)
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store if sid == "abc" else None))
    api._window = source_window
    opened = api.open_translucent_viewer("abc", payload)
    assert set(opened) == {"ok", "id", "launch_token"}
    assert opened["ok"] is True
    assert opened["id"] == "trace-1"
    assert isinstance(opened["launch_token"], str) and len(opened["launch_token"]) == 32
    assert create.call_args.kwargs["launch_token"] == opened["launch_token"]
    assert create.call_args.kwargs["launch_generation"] == "pixels-2"
    context = create.call_args.args[0]
    assert context["slide_url"] == "http://127.0.0.1:8123/secret/s/abc/"
    assert create.call_args.kwargs["url"] == "http://127.0.0.1:8123/secret/static/translucent.html"
    assert not api.open_translucent_viewer("foreign-slide", payload)["ok"]
    assert create.call_count == 1
    api._close_translucent_windows()
    child.close_translucent.assert_called_once()
    assert api._translucent_windows == {}
    assert not api.open_translucent_viewer("abc", payload)["ok"]
    assert source_window.mock_calls == []


def test_windows_applies_alpha_once_and_uses_dpi_scaled_geometry(store, payload, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda operation: operation))
    context = overlay.validate_context(store, payload, slide_url="local/")
    assert context["opacity"] == overlay.OPACITY
    assert context["opacity_mode"] == "native"
    native = SimpleNamespace(InvokeRequired=False, _scale=1.5, Left=100, Top=100,
                             Show=Mock(), Width=640, Height=480)
    api = overlay.TranslucentApi(context, logger=Mock(), on_closed=Mock())
    api._window = SimpleNamespace(native=native)
    api._round_windows_region = Mock()
    api._configure_and_show()
    assert native.Opacity == overlay.OPACITY
    assert native.TopMost and not native.ShowInTaskbar
    assert api.move_window(10, 20)
    assert (native.Left, native.Top) == (115, 130)
    assert api.resize_window(800, 600)["ok"]
    assert (native.Width, native.Height) == (1200, 900)
    assert not api.start_window_drag()


def test_cancelled_update_and_individual_tab_close_manage_translucent_windows(store, payload, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    children = {}

    def make_child(context, **kwargs):
        sid = context["slide_url"].rstrip("/").split("/")[-1]
        child = SimpleNamespace(_window=SimpleNamespace(uid=f"trace-{sid}"), close_translucent=Mock())
        children[sid] = child
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", make_child)
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    api = app.Api(None)
    api._window = Mock()
    api._httpd = SimpleNamespace(
        registry=SimpleNamespace(
            get=lambda sid: store if sid in ("s1", "s2") else None,
            close_all_for_relaunch=Mock(),
        ),
        shutdown=Mock(),
        server_close=Mock(),
    )
    opened1 = api.open_translucent_viewer("s1", payload)
    opened2 = api.open_translucent_viewer("s2", payload)
    assert set(opened1) == {"ok", "id", "launch_token"}
    assert set(opened2) == {"ok", "id", "launch_token"}
    assert opened1["ok"] is True and opened1["id"] == "trace-s1"
    assert opened2["ok"] is True and opened2["id"] == "trace-s2"
    assert isinstance(opened1["launch_token"], str) and opened1["launch_token"]
    assert isinstance(opened2["launch_token"], str) and opened2["launch_token"]
    assert opened1["launch_token"] != opened2["launch_token"]
    assert api.close_translucent_viewer("s1") == {"ok": True, "closed": True}
    children["s1"].close_translucent.assert_called_once()
    assert "s1" not in api._translucent_windows and "s2" in api._translucent_windows
    api.stop_server_for_update()
    children["s2"].close_translucent.assert_called_once()
    assert api._translucent_owner_closed is False


def test_boot_html_preserves_backend_error_status_and_hides_pill_under_card():
    assert "body:has(#update-notice:not([hidden])) #update{visibility:hidden;pointer-events:none}" in app.BOOT_HTML
    assert "window.nd2wsiUpdateCard.handoff()" in app.BOOT_HTML
    assert "pywebview.api.status()" in app.BOOT_HTML
    assert "else end('');" not in app.BOOT_HTML


def test_dialog_window_prefers_bound_window_and_open_paths_resets_stale_status(monkeypatch):
    webview = pytest.importorskip("webview")
    api = app.Api(None)
    assert api._window is None
    other = SimpleNamespace(uid="other", _js_api=object())
    owned = SimpleNamespace(uid="owned", _js_api=api, create_file_dialog=Mock(return_value=[]))
    monkeypatch.setattr(webview, "windows", [other, owned])
    assert api._dialog_window() is owned
    assert api.open_slide() is None
    owned.create_file_dialog.assert_called_once()
    bound = SimpleNamespace(uid="bound")
    api._window = bound
    assert api._dialog_window() is bound
    api._status = "stale error"
    assert api.open_paths([]) is None
    assert api._status == ""


def test_float32_safe_bound_matches_render_without_importing_render():
    from nd2wsi import render

    assert overlay._FLOAT32_SAFE_BOUND == render._FLOAT32_SAFE_BOUND
    assert overlay._HEX6_RE.pattern == render._HEX6_RE.pattern
    assert "render" not in overlay.__dict__


def test_validate_display_params_canonicalizes_colors_windows_and_channels(store):
    # Bare hex, optional single '#', original case and sparse trailing slots preserved.
    assert overlay.validate_display_params(
        store,
        {"col": "#ff00aa,#00FF88", "win": "1:500:0.25,2:600", "c": "1,0"},
    ) == [
        ("c", "1,0"),
        ("win", "1:500:0.25,2:600"),
        ("col", "ff00aa,00FF88"),
    ]
    assert overlay.validate_display_params(store, {"col": "FF00FF,"}) == [("col", "FF00FF,")]
    assert overlay.validate_display_params(store, {"col": ",00ff88"}) == [("col", ",00ff88")]
    assert overlay.validate_display_params(store, {"win": ",0:100:0.1"}) == [("win", ",0:100:0.1")]
    assert overlay.validate_display_params(store, {"win": "-2e38:2e38:10.0"}) == [
        ("win", "-2e38:2e38:10.0")
    ]
    assert overlay.validate_display_params(store, {"c": "none"}) == [("c", "none")]
    assert overlay.validate_display_params(store, {"c": "", "win": ",", "col": ","}) == []
    assert overlay.validate_display_params(store, {"c": None, "win": "", "col": ""}) == []

    # High-channel (> 32) slides are supported without a legacy 32-slot ceiling.
    many_store = SimpleNamespace(attrs={"omero": {"channels": [{} for _ in range(40)]}})
    many_cols = ",".join(f"{i:06x}" for i in range(40))
    assert overlay.validate_display_params(many_store, {"col": many_cols}) == [("col", many_cols)]


@pytest.mark.parametrize("params,match", [
    ({"c": "00"}, "channel selection"),
    ({"c": "01"}, "channel selection"),
    ({"c": "0,00"}, "channel selection"),
    ({"c": "0,0"}, "channel selection"),
    ({"c": "0,"}, "channel selection"),
    ({"c": "2"}, "channel selection"),
    ({"c": "٠"}, "channel selection"),
    ({"c": " 0"}, "channel selection"),
    ({"col": "ff00aa,00ff88,112233"}, "channel color"),
    ({"col": ",,"}, "channel color"),
    ({"col": "##ff00aa"}, "channel color"),
    ({"col": "fff"}, "channel color"),
    ({"col": "gggggg"}, "channel color"),
    ({"col": "ff00aa\n"}, "channel color"),
    ({"col": " ff00aa"}, "channel color"),
    ({"win": "0:10,0:10,0:10"}, "channel windows"),
    ({"win": ",,"}, "channel windows"),
    ({"win": " 10:20"}, "channel window"),
    ({"win": "10 :20"}, "channel window"),
    ({"win": "10:\t20"}, "channel window"),
    ({"win": "10:20 "}, "channel window"),
    ({"win": "1_000:2000"}, "channel window"),
    ({"win": "0:100:1_0"}, "channel window"),
    ({"win": ":100"}, "channel window"),
    ({"win": "0:"}, "channel window"),
    ({"win": "0:100:"}, "channel window"),
    ({"win": "0:100:1:2"}, "channel window"),
    ({"win": "NaN:100"}, "channel window"),
    ({"win": "0:inf"}, "channel window"),
    ({"win": "-inf:0"}, "channel window"),
    ({"win": "0:3e38"}, "channel window"),
    ({"win": "-3e38:0"}, "channel window"),
    ({"win": "0:1e39"}, "channel window"),
    ({"win": "10:10"}, "channel window"),
    ({"win": "20:10"}, "channel window"),
    ({"win": "0:100:0.099"}, "channel gamma"),
    ({"win": "0:100:10.001"}, "channel gamma"),
])
def test_validate_display_params_rejects_malformed_values(store, params, match):
    with pytest.raises(ValueError, match=match):
        overlay.validate_display_params(store, params)


def test_validate_display_query_and_merge_display_query(store):
    assert overlay.validate_display_query(store, "") == []
    assert overlay.validate_display_query(store, "?") == []
    assert overlay.validate_display_query(store, "?col=%23ff00aa%2C&c=0&win=0%3A128") == [
        ("c", "0"),
        ("win", "0:128"),
        ("col", "ff00aa,"),
    ]

    for bad in (
        None,
        123,
        "?" + "c=" + "0" * 16385,
        "??c=0",
        "c=0&c=1",
        "g=pixels-2",
        "c=0&g=pixels-2",
        "t=0",
        "p=0",
        "z=0",
        "c=0&win=0:10&col=ff0000&extra=1",
        "c=0&",
        "barekey",
    ):
        with pytest.raises(ValueError):
            overlay.validate_display_query(store, bad)

    # Merge replaces c/win/col while preserving immutable g, t, p, z in order.
    base = "?c=1&win=1%3A500&col=ff0000&z=4&t=2&p=3&g=pixels-2"
    assert (
        overlay.merge_display_query(store, base, "col=%2300FFaa%2C&c=0&win=0%3A128")
        == "?c=0&win=0%3A128&col=00FFaa%2C&g=pixels-2&t=2&p=3&z=4"
    )
    # Restoring defaults ("") strips c/win/col while retaining g/t/p/z.
    assert overlay.merge_display_query(store, base, "") == "?g=pixels-2&t=2&p=3&z=4"
    assert overlay.merge_display_query(store, base, "c=&win=,&col=,") == "?g=pixels-2&t=2&p=3&z=4"
    assert overlay.merge_display_query(store, "?c=0&win=0%3A100", "") == ""


def test_validate_context_strips_hash_col_preserves_order_and_rejects_bad_display(store, payload):
    with_hash = {**payload, "query": "?g=pixels-2&col=%23ff00aa%2C%2300FF88&c=1"}
    res = overlay.validate_context(store, with_hash, slide_url="http://local/s/abc/")
    assert res["query"] == "?g=pixels-2&col=ff00aa%2C00FF88&c=1"

    for bad_q in ("?c=0,00&g=pixels-2", "?win=1_000:2000&g=pixels-2", "?win=0:3e38&g=pixels-2"):
        with pytest.raises(ValueError):
            overlay.validate_context(store, {**payload, "query": bad_q}, slide_url="http://local/s/abc/")


def test_wait_translucent_display_wake_timeout_expiry_close_and_invariance(cocoa_api):
    q = cocoa_api
    api = overlay.TranslucentApi(
        {"dimensions": [8000, 6000], "query": "?c=0&g=pixels-2"},
        logger=q.logs.append,
        on_closed=q.closed,
        launch_token="tok-1",
        launch_generation="pixels-2",
    )
    api._window = q.window

    ctx = api.translucent_context()
    assert ctx["query"] == "?c=0&g=pixels-2"
    assert ctx["launch_token"] == "tok-1"
    assert ctx["display_revision"] == 0
    assert ctx["locked"] is False
    assert api.translucent_display_state() == {
        "ok": True,
        "launch_token": "tok-1",
        "revision": 0,
        "query": "?c=0&g=pixels-2",
        "expired": False,
        "reason": "",
    }

    # Invalid arguments rejected cleanly.
    for bad_args in (
        (123, 0, 50),
        ("tok-1", -1, 50),
        ("tok-1", True, 50),
        ("tok-1", 2**53, 50),
        ("tok-1", 0, 0),
        ("tok-1", 0, 60001),
        ("tok-1", 0, True),
        ("tok-1", 0, float("nan")),
    ):
        bad_res = api.wait_translucent_display(*bad_args)
        assert bad_res["ok"] is False and "Invalid" in bad_res["message"]

    # Token mismatch returns closed without blocking.
    assert api.wait_translucent_display("wrong-token", 0, timeout_ms=5000) == {
        "ok": False,
        "closed": True,
    }

    # Timeout returns unchanged=True.
    t0 = time.monotonic()
    timed_out = api.wait_translucent_display("tok-1", 0, timeout_ms=25)
    assert timed_out == {
        "ok": True,
        "launch_token": "tok-1",
        "revision": 0,
        "query": "?c=0&g=pixels-2",
        "expired": False,
        "unchanged": True,
    }
    assert time.monotonic() - t0 < 2.0

    # Locked overlay still accepts live display updates without bumping _epoch.
    assert api.set_translucent_locked(True) == {"ok": True, "locked": True}
    epoch_before = api._epoch

    # Blocking waiter wakes immediately when _apply_display_query runs.
    waiter_out = {}
    started = threading.Event()

    def run_waiter():
        started.set()
        waiter_out["res"] = api.wait_translucent_display("tok-1", 0, timeout_ms=5000)

    t = threading.Thread(target=run_waiter)
    t.start()
    started.wait(timeout=2)
    time.sleep(0.02)
    applied = api._apply_display_query("tok-1", 2, "?win=0%3A128&g=pixels-2")
    assert applied == {"ok": True, "applied": True, "revision": 2}
    t.join(timeout=2)
    assert not t.is_alive()
    assert waiter_out["res"] == {
        "ok": True,
        "launch_token": "tok-1",
        "revision": 2,
        "query": "?win=0%3A128&g=pixels-2",
        "expired": False,
        "unchanged": False,
    }
    assert api._locked is True
    assert api._epoch == epoch_before
    ctx2 = api.translucent_context()
    assert ctx2["query"] == "?win=0%3A128&g=pixels-2"
    assert ctx2["display_revision"] == 2
    assert ctx2["locked"] is True

    # Immediate return when revision is already ahead of after_revision.
    assert api.wait_translucent_display("tok-1", 1, timeout_ms=5000) == {
        "ok": True,
        "launch_token": "tok-1",
        "revision": 2,
        "query": "?win=0%3A128&g=pixels-2",
        "expired": False,
        "unchanged": False,
    }

    # Out-of-order / duplicate revision drops without changing state.
    assert api._apply_display_query("tok-1", 2, "?c=1&g=pixels-2") == {
        "ok": True,
        "applied": False,
        "revision": 2,
    }
    assert api._apply_display_query("tok-1", 1, "?c=1&g=pixels-2") == {
        "ok": True,
        "applied": False,
        "revision": 2,
    }
    assert api.translucent_context()["query"] == "?win=0%3A128&g=pixels-2"

    # Expiry wakes blocked waiter immediately.
    expire_out = {}
    started2 = threading.Event()

    def run_expire_waiter():
        started2.set()
        expire_out["res"] = api.wait_translucent_display("tok-1", 2, timeout_ms=5000)

    t2 = threading.Thread(target=run_expire_waiter)
    t2.start()
    started2.wait(timeout=2)
    time.sleep(0.02)
    assert api._expire_display_session("wrong-tok") is False
    assert api._expire_display_session("tok-1", "generation_changed") is True
    t2.join(timeout=2)
    assert not t2.is_alive()
    assert expire_out["res"] == {
        "ok": True,
        "launch_token": "tok-1",
        "revision": 2,
        "query": "?win=0%3A128&g=pixels-2",
        "expired": True,
        "reason": "generation_changed",
    }
    assert api._apply_display_query("tok-1", 3, "?c=1&g=pixels-2")["stale_generation"] is True


def test_close_translucent_wakes_waiter_before_destroy_and_notifies_once():
    events = []
    on_closed = Mock(side_effect=lambda _self: events.append("on_closed"))
    waiter_unblocked = threading.Event()
    waiter_out = {}

    api = overlay.TranslucentApi(
        {"dimensions": [100, 100], "query": "?g=g1"},
        logger=Mock(),
        on_closed=on_closed,
        launch_token="tok-close",
        launch_generation="g1",
    )

    def destroy_and_fire_did_close():
        # Waiter must already have been woken before destroy() completes.
        assert waiter_unblocked.wait(timeout=2)
        events.append("destroy")
        api._did_close()

    api._window = SimpleNamespace(destroy=Mock(side_effect=destroy_and_fire_did_close))

    started = threading.Event()

    def run_waiter():
        started.set()
        waiter_out["res"] = api.wait_translucent_display("tok-close", 0, timeout_ms=5000)
        events.append("waiter_done")
        waiter_unblocked.set()

    t = threading.Thread(target=run_waiter)
    t.start()
    started.wait(timeout=2)
    time.sleep(0.02)

    assert api.close_translucent() is True
    t.join(timeout=2)
    assert not t.is_alive()
    assert waiter_out["res"] == {"ok": False, "closed": True}
    assert api.translucent_display_state() == {"ok": False, "closed": True}
    assert api._apply_display_query("tok-close", 1, "?g=g1") == {"ok": False, "closed": True}

    # Repeated _did_close / close_translucent must never duplicate on_closed.
    api._did_close()
    assert api.close_translucent() is True
    on_closed.assert_called_once_with(api)
    assert events.index("waiter_done") < events.index("destroy")


def test_open_translucent_reservation_aborts_on_concurrent_close_remove_supersede_or_error(
    store, payload, monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")

    # 1. Concurrent close_translucent_viewer during create_translucent_window
    api = app.Api(None)
    stores = {"abc": store}
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: stores.get(sid)))
    orphan = SimpleNamespace(_window=SimpleNamespace(uid="orphan-1"), close_translucent=Mock())

    def create_during_close(context, **kwargs):
        assert api.close_translucent_viewer("abc") == {"ok": True, "closed": True}
        return orphan

    monkeypatch.setattr(overlay, "create_translucent_window", create_during_close)
    res = api.open_translucent_viewer("abc", payload)
    assert res["ok"] is False and "superseded" in res["message"]
    orphan.close_translucent.assert_called_once()
    assert api._translucent_windows == {}
    assert api._translucent_pending_launches == {}

    # 2. Concurrent registry removal + on_remove during create_translucent_window
    orphan2 = SimpleNamespace(_window=SimpleNamespace(uid="orphan-2"), close_translucent=Mock())

    def create_during_remove(context, **kwargs):
        stores.pop("abc", None)
        api.close_translucent_viewer("abc")
        return orphan2

    monkeypatch.setattr(overlay, "create_translucent_window", create_during_remove)
    res2 = api.open_translucent_viewer("abc", payload)
    assert res2["ok"] is False and "slide was closed" in res2["message"]
    orphan2.close_translucent.assert_called_once()
    assert api._translucent_windows == {}
    assert api._translucent_pending_launches == {}

    # 3. Concurrent newer open_translucent_viewer supersedes older in-flight launch
    stores["abc"] = store
    first_entered = threading.Event()
    allow_first_return = threading.Event()
    child1 = SimpleNamespace(_window=SimpleNamespace(uid="trace-old"), close_translucent=Mock())
    child2 = SimpleNamespace(_window=SimpleNamespace(uid="trace-new"), close_translucent=Mock())
    call_count = 0

    def create_racing(context, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_entered.set()
            allow_first_return.wait(timeout=3)
            return child1
        return child2

    monkeypatch.setattr(overlay, "create_translucent_window", create_racing)
    first_result = {}

    def open_first():
        first_result["res"] = api.open_translucent_viewer("abc", payload)

    t = threading.Thread(target=open_first)
    t.start()
    assert first_entered.wait(timeout=2)
    second_result = api.open_translucent_viewer("abc", payload)
    assert second_result["ok"] is True and second_result["id"] == "trace-new"
    allow_first_return.set()
    t.join(timeout=2)
    assert first_result["res"]["ok"] is False
    child1.close_translucent.assert_called_once()
    child2.close_translucent.assert_not_called()
    assert api._translucent_windows["abc"] is child2
    assert api._translucent_pending_launches == {}

    # 4. Concurrent generation change during create aborts launch
    orphan3 = SimpleNamespace(_window=SimpleNamespace(uid="orphan-3"), close_translucent=Mock())

    def create_during_gen_change(context, **kwargs):
        store.generation = "pixels-3"
        return orphan3

    monkeypatch.setattr(overlay, "create_translucent_window", create_during_gen_change)
    res_gen = api.open_translucent_viewer("abc", payload)
    assert res_gen["ok"] is False and "image changed" in res_gen["message"]
    orphan3.close_translucent.assert_called_once()
    assert api._translucent_pending_launches == {}

    # 5. Exception in create_translucent_window cleans up pending reservation
    store.generation = "pixels-2"
    monkeypatch.setattr(
        overlay, "create_translucent_window", Mock(side_effect=RuntimeError("window failed")),
    )
    res_err = api.open_translucent_viewer("abc", payload)
    assert res_err["ok"] is False
    assert api._translucent_pending_launches == {}


def test_update_translucent_display_routing_generation_expiry_and_multi_owner_isolation(
    store, payload, monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    created = []

    def make_real_api(context, *, url, title, logger, on_closed, launch_token=None, launch_generation=""):
        child = overlay.TranslucentApi(
            context,
            logger=logger,
            on_closed=on_closed,
            launch_token=launch_token,
            launch_generation=launch_generation,
        )
        child._window = SimpleNamespace(uid=f"win-{len(created) + 1}", destroy=Mock())
        created.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", make_real_api)

    # Two independent Api owners sharing the same sid must never cross-talk.
    store1 = copy.deepcopy(store)
    store2 = copy.deepcopy(store)
    api1, api2 = app.Api(None), app.Api(None)
    api1._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store1 if sid == "shared" else None))
    api2._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store2 if sid == "shared" else None))

    r1 = api1.open_translucent_viewer("shared", payload)
    r2 = api2.open_translucent_viewer("shared", payload)
    assert r1["ok"] and r2["ok"] and r1["launch_token"] != r2["launch_token"]
    ov1, ov2 = created[0], created[1]

    # Invalid arg types rejected without mutating state.
    for bad in (
        ("", r1["launch_token"], 1, ""),
        ("shared", "", 1, ""),
        ("shared", r1["launch_token"], 0, ""),
        ("shared", r1["launch_token"], True, ""),
        ("shared", r1["launch_token"], 2**53, ""),
        ("shared", r1["launch_token"], 1, None),
    ):
        bad_res = api1.update_translucent_display(*bad)
        assert bad_res["ok"] is False and bad_res["invalid"] is True
    assert ov1.translucent_display_state()["revision"] == 0

    # Invalid display query rejected without mutating state.
    bad_q = api1.update_translucent_display("shared", r1["launch_token"], 1, "g=tampered")
    assert bad_q["ok"] is False and bad_q["invalid"] is True
    assert ov1.translucent_display_state()["revision"] == 0

    # Cross-owner token rejected as stale; neither overlay mutates.
    assert api1.update_translucent_display("shared", r2["launch_token"], 1, "c=0") == {
        "ok": False,
        "stale": True,
    }
    assert ov1.translucent_display_state()["revision"] == 0
    assert ov2.translucent_display_state()["revision"] == 0

    # Valid update on api1 mutates only ov1, not ov2.
    upd = api1.update_translucent_display("shared", r1["launch_token"], 1, "c=0&col=%23ff00aa%2C")
    assert upd == {"ok": True, "applied": True, "revision": 1}
    assert ov1.translucent_context()["query"] == "?c=0&col=ff00aa%2C&g=pixels-2"
    assert ov2.translucent_context()["query"] == payload["query"]
    assert ov2.translucent_display_state()["revision"] == 0

    # Empty display query restores defaults while keeping g.
    reset = api1.update_translucent_display("shared", r1["launch_token"], 2, "")
    assert reset == {"ok": True, "applied": True, "revision": 2}
    assert ov1.translucent_context()["query"] == "?g=pixels-2"

    # Shading rebuild advancing store1.generation expires ov1 and wakes its waiter.
    wait_out = {}
    started = threading.Event()

    def waiter():
        started.set()
        wait_out["res"] = ov1.wait_translucent_display(r1["launch_token"], 2, timeout_ms=5000)

    wt = threading.Thread(target=waiter)
    wt.start()
    started.wait(timeout=2)
    time.sleep(0.02)

    store1.generation = "pixels-3"
    stale_res = api1.update_translucent_display("shared", r1["launch_token"], 3, "c=1")
    assert stale_res["ok"] is False and stale_res["stale_generation"] is True
    wt.join(timeout=2)
    assert not wt.is_alive()
    assert wait_out["res"]["expired"] is True
    assert wait_out["res"]["reason"] == "generation_changed"
    # ov2 remains healthy and unaffected.
    assert api2.update_translucent_display("shared", r2["launch_token"], 1, "c=1") == {
        "ok": True,
        "applied": True,
        "revision": 1,
    }

    # Closing ov1 returns closed on further updates.
    assert api1.close_translucent_viewer("shared") == {"ok": True, "closed": True}
    assert api1.update_translucent_display("shared", r1["launch_token"], 4, "c=0") == {
        "ok": False,
        "closed": True,
    }


def test_create_app_window_assigns_api_window_and_private_helpers_not_exposed(monkeypatch, tmp_path):
    webview = pytest.importorskip("webview")
    window = SimpleNamespace(
        uid="main-win",
        events=SimpleNamespace(
            before_load=Event(),
            closed=Event(),
            closing=Event(),
            shown=Event(),
            initialized=Event(),
        ),
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(webview, "create_window", Mock(return_value=window))
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("nd2wsi.native_gestures.wire_native_trackpad_bridge", Mock())
    monkeypatch.setattr(app, "_wire_file_drop", Mock())
    monkeypatch.setattr(app, "_inline_traffic_lights", Mock())
    monkeypatch.setattr(app, "_install_open_files_handler", Mock())
    monkeypatch.setattr(app, "_guard_native_close", Mock())
    monkeypatch.setattr(app._window_chrome, "NativeTitlebarDrag", Mock())

    api, created_window = app.create_app_window(None)
    assert created_window is window
    assert api._window is window

    # Verify pywebview JS bridge exposes public methods and omits all private '_' helpers.
    def exposed_bridge_functions(js_api):
        if hasattr(webview.util, "get_functions"):
            return set(webview.util.get_functions(js_api))
        scripts = []
        loaded = threading.Event()
        fake_win = SimpleNamespace(
            _js_api=js_api,
            _functions={},
            _expose_lock=threading.Lock(),
            run_js=scripts.append,
            events=SimpleNamespace(
                before_load=threading.Event(),
                _pywebviewready=threading.Event(),
                loaded=loaded,
            ),
        )
        with monkeypatch.context() as patch:
            patch.setattr(webview.util, "load_js_files", lambda _w, _p: ("", "%(functions)s"))
            webview.util.inject_pywebview("cocoa", fake_win)
        assert loaded.wait(timeout=2)
        return {entry["func"] for entry in json.loads(scripts[-1])}

    parent_funcs = exposed_bridge_functions(api)
    assert "open_translucent_viewer" in parent_funcs
    assert "update_translucent_display" in parent_funcs
    assert "close_translucent_viewer" in parent_funcs
    assert all(not part.startswith("_") for name in parent_funcs for part in name.split("."))

    child_api = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=Mock())
    child_funcs = exposed_bridge_functions(child_api)
    assert "translucent_context" in child_funcs
    assert "translucent_display_state" in child_funcs
    assert "wait_translucent_display" in child_funcs
    for private_name in (
        "_session_snapshot",
        "_apply_display_query",
        "_expire_display_session",
        "_notify_closed",
        "_did_close",
        "_configure_and_show",
    ):
        assert private_name not in child_funcs
    assert all(not part.startswith("_") for name in child_funcs for part in name.split("."))


def test_open_translucent_refuses_to_register_already_closed_or_retired_child(
    store, payload, monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store if sid == "abc" else None))

    # 1. Real TranslucentApi whose _did_close ran during create_translucent_window (retired=True).
    closed_children = []

    def create_already_closed(context, *, url, title, logger, on_closed, launch_token=None, launch_generation=""):
        child = overlay.TranslucentApi(
            context,
            logger=logger,
            on_closed=on_closed,
            launch_token=launch_token,
            launch_generation=launch_generation,
        )
        child._window = SimpleNamespace(uid="dead-real", destroy=Mock())
        child._did_close()
        closed_children.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", create_already_closed)
    res_real = api.open_translucent_viewer("abc", payload)
    assert res_real["ok"] is False
    assert "closed while opening" in res_real["message"]
    assert api._translucent_windows == {}
    assert api._translucent_pending_launches == {}
    assert closed_children[0]._session_snapshot()[3] is True

    # 2. Stub child without _session_snapshot exposing _closed=True also aborts and invokes close_translucent.
    closed_stub = SimpleNamespace(_window=SimpleNamespace(uid="dead-stub"), _closed=True, close_translucent=Mock())
    monkeypatch.setattr(overlay, "create_translucent_window", lambda *a, **k: closed_stub)
    res_stub = api.open_translucent_viewer("abc", payload)
    assert res_stub["ok"] is False
    assert "closed while opening" in res_stub["message"]
    closed_stub.close_translucent.assert_called_once()
    assert api._translucent_windows == {}
    assert api._translucent_pending_launches == {}


def test_empty_display_query_after_generation_change_expires_and_wakes_waiter(
    store, payload, monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    created = []

    def make_real_api(context, *, url, title, logger, on_closed, launch_token=None, launch_generation=""):
        child = overlay.TranslucentApi(
            context,
            logger=logger,
            on_closed=on_closed,
            launch_token=launch_token,
            launch_generation=launch_generation,
        )
        child._window = SimpleNamespace(uid="win-expire", destroy=Mock())
        created.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", make_real_api)
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store if sid == "abc" else None))
    opened = api.open_translucent_viewer("abc", payload)
    assert opened["ok"] is True
    child = created[0]

    wait_out = {}
    started = threading.Event()

    def waiter():
        started.set()
        wait_out["res"] = child.wait_translucent_display(opened["launch_token"], 0, timeout_ms=5000)

    wt = threading.Thread(target=waiter)
    wt.start()
    assert started.wait(timeout=2)
    time.sleep(0.02)

    store.generation = "pixels-rebuilt"
    res = api.update_translucent_display("abc", opened["launch_token"], 1, "")
    assert res["ok"] is False and res["stale_generation"] is True
    wt.join(timeout=2)
    assert not wt.is_alive()
    assert wait_out["res"]["ok"] is True
    assert wait_out["res"]["expired"] is True
    assert wait_out["res"]["reason"] == "generation_changed"


def test_create_translucent_window_triggers_did_close_when_closed_event_already_set(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    class PreSetEvent(Event):
        def __init__(self, preset: bool):
            super().__init__()
            self._preset = preset

        def is_set(self):
            return self._preset

    window = SimpleNamespace(
        events=SimpleNamespace(
            shown=PreSetEvent(False),
            closed=PreSetEvent(True),
            closing=PreSetEvent(False),
            before_load=PreSetEvent(False),
            loaded=PreSetEvent(True),
        )
    )
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=Mock(return_value=window)))
    on_closed = Mock()
    api = overlay.create_translucent_window(
        {"dimensions": [100, 100], "query": "?g=g1"},
        url="http://local/token/static/translucent.html",
        title="Overlay",
        logger=Mock(),
        on_closed=on_closed,
    )
    assert api._closed is True
    assert api._session_snapshot()[3] is True
    assert api._configured is False
    on_closed.assert_called_once_with(api)

