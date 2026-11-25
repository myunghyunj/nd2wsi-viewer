"""Tracing overlays own their window, lock state and display-only context."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app
from nd2wsi import translucent_window as overlay


@pytest.fixture(autouse=True)
def tracing_claims_stay_in_the_test(monkeypatch, tmp_path):
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path / "window-sessions"))




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


@pytest.mark.parametrize("extra_meta", [
    {"rgb": True},
    {"rgb": True, "source": "slide.svs"},
    {"rgb": True, "source": "scan.sdpc", "source_format": "sdpc"},
    {"rgb": True, "source": "brightfield.czi"},
    {"rgb": True, "source": "brightfield.lif"},
    {"rgb": True, "raster": True},
])
def test_rgb_and_raster_slides_of_any_format_can_open_with_current_display_state(store, payload, extra_meta):
    store.attrs["nd2wsi"].update(extra_meta)
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
    {"index": True}, {"index": 0}, {"index": 2**53}, {"label": None}, {"label": "x" * 4097},
])
def test_imported_roi_snapshot_rejects_invalid_geometry_or_metadata(store, payload, change):
    roi = {"x": 1, "y": 2, "w": 3, "h": 4, "index": 1, "label": "ROI 1", **change}
    with pytest.raises(ValueError):
        overlay.validate_context(store, {**payload, "rois": [roi]}, slide_url="local/")


def test_validate_roi_allows_4096_label_and_clamps_two_axis_epsilon_overshoot(store, payload):
    roi = {
        "x": 7999.0000005,
        "y": 5999.0000005,
        "w": 1.0,
        "h": 1.0,
        "index": 1,
        "label": "x" * 4096,
    }
    res = overlay.validate_context(store, {**payload, "rois": [roi]}, slide_url="local/")
    cleaned = res["rois"][0]
    assert len(cleaned["label"]) == 4096
    assert cleaned["x"] + cleaned["w"] <= 8000
    assert cleaned["y"] + cleaned["h"] <= 6000
    assert cleaned["w"] > 0 and cleaned["h"] > 0

    # Overshoot beyond 1e-6 on either axis is rejected.
    for bad in (
        {"x": 7999.0, "y": 1.0, "w": 1.000002, "h": 1.0},
        {"x": 1.0, "y": 5999.0, "w": 1.0, "h": 1.000002},
        # Clamping to zero width/height at the exact slide edge must still be rejected.
        {"x": 8000.0, "y": 1.0, "w": 1e-7, "h": 1.0},
        {"x": 1.0, "y": 6000.0, "w": 1.0, "h": 1e-7},
    ):
        with pytest.raises(ValueError, match="outside the slide"):
            overlay.validate_roi(store, bad)


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
    q.closed.assert_not_called()
    q.api._did_close()
    q.api._did_close()
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
    ["a.txt", "a.bmp"], ["a.txt", "a.jpg", "a.jpeg"], [],
])
def test_pair_import_rejects_mismatched_extra_or_unsupported_files(cocoa_api, monkeypatch, tmp_path, names):
    load = Mock()
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))
    result = cocoa_api.api._load_roi_files([tmp_path / name for name in names])
    assert not result["ok"]
    assert result["message"]
    load.assert_not_called()


def test_png_pair_import_and_picker_filters(cocoa_api, monkeypatch, tmp_path, raw_roi_pair):
    q = cocoa_api
    load = Mock(return_value=raw_roi_pair)
    monkeypatch.setitem(sys.modules, "nd2wsi.roi_list", SimpleNamespace(load_roi_file=load))
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=10))
    text, image = tmp_path / "roi.txt", tmp_path / "elsewhere" / "roi.png"
    text.touch()
    q.window.create_file_dialog.side_effect = [[str(text)], [str(image)]]
    assert q.api.choose_roi_file()["ok"]
    first_types = q.window.create_file_dialog.call_args_list[0].kwargs["file_types"]
    second_types = q.window.create_file_dialog.call_args_list[1].kwargs["file_types"]
    assert any("*.png" in entry for entry in first_types)
    assert any("*.png" in entry for entry in second_types)
    load.assert_called_once_with(text, width=8000, height=6000, image_path=image)

    load.reset_mock()
    sibling_png = tmp_path / "ROI.PNG"
    sibling_png.touch()
    assert q.api._load_roi_files([text])["ok"]
    load.assert_called_once_with(text, width=8000, height=6000, image_path=sibling_png)


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
    api._did_close()


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
    create.call_args.kwargs["on_closed"](child)
    assert api._translucent_windows == {}
    assert not api.open_translucent_viewer("abc", payload)["ok"]
    assert source_window.mock_calls == []


@pytest.mark.parametrize("source, source_path, expected", [
    ("slide.nd2", None, "nd2"), ("slide.nd2", "/cache/slide--t0-p0-zmid.nd2svs", "nd2"),
    ("brain.czi", None, "czi"), (None, "/cache/brain.czi--t0-p0-zmid.nd2svs", "czi"),
    ("sample.lif", None, "lif"), (None, "/cache/sample.lif--s0--t0-p0-zmid.nd2svs", "lif"),
    ("mosaic.ome.tif", None, "ometiff"), (None, "/cache/mosaic.ome.tif--s0--t0-p0-zmid.nd2svs", "ometiff"),
    ("stack.tif", None, "tiff"), ("multiplex.qptiff", None, "qptiff"),
    ("scan.sdpc", None, "sdpc"),
    (None, "/data/x.PNG", "png"), ("scan.svs", None, "svs"), (None, None, "other"),
])
def test_context_names_the_follow_format(store, payload, source, source_path, expected):
    if source is not None:
        store.attrs["nd2wsi"]["source"] = source
    store.source_path = source_path
    context = overlay.validate_context(store, payload, slide_url="http://local/token/s/abc/")
    assert context["source_format"] == expected
    # The format only picks the remembered flips; the orientation is the handoff's.
    assert context["rotation"] == 90 and context["flip"] is True


def test_context_accepts_custom_channel_colors_and_rejects_malformed_col(store, payload):
    custom = {**payload, "query": f"?c=0,1&win=0:100,10:200&col=FF00FF,&g={store.generation}"}
    context = overlay.validate_context(store, custom, slide_url="http://local/token/s/abc/")
    assert "col=FF00FF%2C" in context["query"]
    for bad_col in ("ZZZZZZ", "FF00", "FF00FF,00FFFF,112233,445566"):
        with pytest.raises(ValueError, match="channel color"):
            overlay.validate_context(
                store, {**payload, "query": f"?c=0&col={bad_col}&g={store.generation}"}, slide_url="http://local/token/s/abc/"
            )


def test_parent_registry_passes_one_flip_store_to_each_overlay(store, payload, monkeypatch, tmp_path):
    from nd2wsi.follow_flips import FollowFlipStore

    monkeypatch.setattr(sys, "platform", "darwin")
    created = []

    def create(context, **options):
        created.append(options)
        return SimpleNamespace(_window=SimpleNamespace(uid=f"trace-{len(created)}"), close_translucent=Mock(), _refresh_context=Mock())

    monkeypatch.setattr(overlay, "create_translucent_window", create)
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    registry = SimpleNamespace(get=lambda sid: store if sid in ("abc", "def") else None)
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=registry)
    assert api.open_translucent_viewer("abc", payload)["ok"]
    assert api.open_translucent_viewer("def", payload)["reason"] == "another_open"
    assert api.open_translucent_viewer("abc", payload)["ok"]  # reused
    created[0]["on_closed"](api._translucent_windows["abc"])
    assert api.open_translucent_viewer("def", payload)["ok"]
    stores = [options["flip_store"] for options in created]
    assert isinstance(stores[0], FollowFlipStore) and all(item is stores[0] for item in stores)
    assert stores[0].path.parent == Path(os.environ["ND2WSI_FOLLOW_FLIPS_ROOT"]).resolve()
    created[-1]["on_closed"](api._translucent_windows["def"])
    api._close_translucent_windows()
    # An Agent window's overlays remember into its own session folder only.
    created.clear()
    agent = app.Api(None, window_session=SimpleNamespace(role="agent", root=tmp_path / "agent-session"))
    agent._httpd = SimpleNamespace(registry=registry)
    assert agent.open_translucent_viewer("abc", payload)["ok"]
    assert created[0]["flip_store"].path == tmp_path / "agent-session" / "stage-follow" / "flips.json"
    created[-1]["on_closed"](agent._translucent_windows["abc"])
    agent._close_translucent_windows()


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


def test_two_user_apis_share_claim_but_agent_is_private(store, payload, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://local/token/")
    created = []

    def create(context, **options):
        child = SimpleNamespace(_window=SimpleNamespace(uid=str(len(created))))
        child.close_translucent = lambda: options["on_closed"](child)
        created.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", create)
    server = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store))
    first, second = app.Api(None), app.Api(None)
    agent = app.Api(None, window_session=SimpleNamespace(role="agent", root=tmp_path / "agent"))
    for api in (first, second, agent):
        api._httpd = server
    try:
        assert first.open_translucent_viewer("a", payload)["ok"]
        assert second.open_translucent_viewer("a", payload)["reason"] == "another_open"
        assert agent.open_translucent_viewer("a", payload)["ok"]
        first._close_translucent_windows()
        assert second.open_translucent_viewer("b", payload)["ok"]
    finally:
        for api in (first, second, agent):
            api._close_translucent_windows()


def test_tracing_claim_released_by_os_after_crash(tmp_path):
    import subprocess

    from nd2wsi.translucent_claim import tracing_claim

    script = "from nd2wsi.translucent_claim import tracing_claim; c=tracing_claim(); c.acquire(); print('ready', flush=True); input()"
    process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    claim = tracing_claim()
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(TimeoutError):
            claim.acquire()
        process.kill()
        process.wait(timeout=5)
        claim.acquire(timeout=2.0)
        assert claim.acquired and claim.path.is_file()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        claim.release()


def test_laser_route_stamps_owner_sid_and_only_returns_ack_to_that_overlay(store, payload, monkeypatch):
    import json

    monkeypatch.setattr(sys, "platform", "darwin")
    child = SimpleNamespace(_window=SimpleNamespace(uid="trace"), close_translucent=Mock(),
                            _report_slacs_laser_box=Mock(return_value={"ok": True}))
    create = Mock(return_value=child)
    monkeypatch.setattr(overlay, "create_translucent_window", create)
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/token/")
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store))
    owner = Mock()
    owner.evaluate_js.return_value = True
    monkeypatch.setattr(api, "_lut_clipboard_owner", lambda: owner)
    assert api.open_translucent_viewer("slide", payload)["ok"]
    options = create.call_args.kwargs
    try:
        assert not options["is_active"]()
        options["on_lock"](True)
        assert options["is_active"]()
        message = {"sid": "spoofed", "token": "token", "press": 4}
        assert options["on_laser_box"](message) is True
        script = owner.evaluate_js.call_args.args[0]
        sent = json.loads(script.split("?.(", 1)[1].rsplit(") === true", 1)[0])
        assert sent == {**message, "sid": "slide"}
        assert not api.report_slacs_laser_box(None)["ok"]
        assert not api.report_slacs_laser_box({**message, "sid": "missing"})["ok"]
        assert api.report_slacs_laser_box(sent)["ok"]
        child._report_slacs_laser_box.assert_called_once_with(sent)
    finally:
        options["on_closed"](child)
    assert not api.report_slacs_laser_box(sent)["ok"]


def test_reuse_refreshes_display_and_focuses_without_another_window():
    api = overlay.TranslucentApi({"slide_url": "http://local/s/1/", "rotation": 0, "flip": False, "query": "?c=0"},
                                 logger=Mock(), on_closed=Mock())
    api._configured = True
    api._window = Mock()
    api._page_url = "http://local/trace"
    receiver = api._stage_follow = Mock()
    api._overlay_view = {"target_px": [1, 2]}
    item = {"id": "laser-1", "type": "box", "x": 1, "y": 2, "w": 3, "h": 4, "text": "SLACS_ROI #1",
            "slacs": {"kind": "laser_shot", "shots": 1}}
    api._confirm_laser_box({"item": item})
    updated = {"slide_url": "http://local/s/1/", "rotation": 90, "flip": True, "query": "?c=1&win=1:200", "center": [20, 30]}
    api._refresh_context(updated)
    receiver.close.assert_called_once()
    assert api._overlay_view is None
    for key, value in updated.items():
        assert api.translucent_context()[key] == value
    assert api.translucent_context()["laser_items"] == [item]
    api._refresh_context({**updated, "query": "?c=1&p=1&win=1:200"})
    assert api.translucent_context()["laser_items"] == []
    api._confirm_laser_box({"item": item})
    api._refresh_context({**updated, "slide_url": "http://local/s/2/"})
    assert api.translucent_context()["laser_items"] == []
    assert api._window.load_url.call_count == 3
    assert api._window.show.call_count == 3
    api._did_close()


def test_load_timeout_releases_claim_and_late_load_cannot_show_window(store, payload, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(overlay.threading, "Timer", Mock())
    window = Mock()
    window.events = SimpleNamespace(loaded=Event(), closed=Event())
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=Mock(return_value=window)))
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/token/")
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store))
    assert api.open_translucent_viewer("one", payload)["ok"]
    child = api._translucent_windows["one"]
    child._initial_load_timeout()
    assert not api._translucent_windows and api._translucent_claim is None and child._closed
    monkeypatch.setattr(child, "_dispatch", lambda fn: fn())
    child._configure_and_show()
    window.native.Show.assert_not_called()
    assert api.open_translucent_viewer("two", payload)["ok"]
    api._translucent_windows["two"]._initial_load_timeout()


def test_create_window_failure_disposes_the_api_without_allocating_delivery(monkeypatch):
    from nd2wsi import slacs_laser

    factory = Mock()
    monkeypatch.setattr(slacs_laser, "LaserDelivery", factory)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=Mock(side_effect=RuntimeError("failed"))))
    closed = Mock()
    with pytest.raises(RuntimeError, match="failed"):
        overlay.create_translucent_window({}, url="http://local/trace", title="Tracing", logger=Mock(), on_closed=closed,
                                          on_laser_box=Mock())
    factory.assert_not_called()
    closed.assert_called_once()
    assert closed.call_args.args[0]._closed


def test_cancelled_update_and_individual_tab_close_manage_translucent_windows(store, payload, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    children = {}

    def make_child(context, **kwargs):
        sid = context["slide_url"].rstrip("/").split("/")[-1]
        child = SimpleNamespace(_window=SimpleNamespace(uid=f"trace-{sid}"))
        child.close_translucent = Mock(side_effect=lambda: kwargs["on_closed"](child))
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
    assert set(opened1) == {"ok", "id", "launch_token"}
    assert opened1["ok"] is True and opened1["id"] == "trace-s1"
    assert isinstance(opened1["launch_token"], str) and opened1["launch_token"]
    assert api.close_translucent_viewer("s1") == {"ok": True, "closed": True}
    children["s1"].close_translucent.assert_called_once()
    assert "s1" not in api._translucent_windows
    opened2 = api.open_translucent_viewer("s2", payload)
    assert set(opened2) == {"ok", "id", "launch_token"}
    assert opened2["ok"] is True and opened2["id"] == "trace-s2"
    assert isinstance(opened2["launch_token"], str) and opened2["launch_token"]
    assert opened1["launch_token"] != opened2["launch_token"]
    api._stop_server_for_update()
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


def test_image_name_falls_back_to_source_meta_before_default(store, payload):
    assert overlay.validate_context(store, payload, slide_url="local/")["image_name"] == "image"
    store.attrs["nd2wsi"]["source"] = "/data/confocal/brain.czi"
    assert overlay.validate_context(store, payload, slide_url="local/")["image_name"] == "brain.czi"
    store.source_path = Path("/Users/lab/slides/override.nd2")
    assert overlay.validate_context(store, payload, slide_url="local/")["image_name"] == "override.nd2"


def test_refresh_context_updates_source_format_flips_and_resets_lock_realign_state(tmp_path):
    from nd2wsi.follow_flips import FollowFlipStore

    flip_store = FollowFlipStore(root=tmp_path)
    flip_store.save("nd2", True, False)
    flip_store.save("czi", False, True)
    lock_events = []
    api = overlay.TranslucentApi(
        {"slide_url": "http://local/s/1/", "rotation": 0, "flip": False, "query": "?c=0", "source_format": "nd2"},
        logger=Mock(), on_closed=Mock(), flip_store=flip_store, on_lock=lock_events.append,
    )
    api._configured = True
    api._window = Mock()
    api._page_url = "http://local/trace"
    assert api.translucent_context()["source_format"] == "nd2"
    assert api.translucent_context()["follow_flips"] == {"flip_x": True, "flip_y": False}
    api._locked = True
    api._lock_reported = True
    api._last_locked_center = [100.0, 200.0]
    api._realign_from = [100.0, 200.0]
    api._refresh_context({
        "slide_url": "http://local/s/2/", "rotation": 0, "flip": False,
        "query": "?c=0&t=0&p=0&z=0", "source_format": "czi", "annotation_scope": "plane",
    })
    assert api._locked is False
    assert api._lock_reported is None
    assert api._last_locked_center is None
    assert api._realign_from is None
    assert lock_events == [False]
    assert api.translucent_context()["source_format"] == "czi"
    assert api.translucent_context()["follow_flips"] == {"flip_x": False, "flip_y": True}
    api._did_close()


def test_plane_scoped_laser_items_sync_on_open_refresh_deletion_and_plane_change(store, payload):
    store.plate = SimpleNamespace(T=3, P=2, Z=4)
    store.attrs["nd2wsi"]["plate"] = {"annotationScope": "plane", "T": 3, "P": 2, "Z": 4}
    payload = {**payload, "query": "?c=1&g=pixels-2&t=1&p=0&z=2"}
    laser_1 = {
        "id": "slacs-laser-r-1", "kind": "laser_shot", "type": "box",
        "x": 100, "y": 200, "w": 20, "h": 30, "text": "SLACS_ROI #1",
        "color": "#a0a6ae", "slacs": {"kind": "laser_shot", "shots": 2, "isolation": 1},
    }
    laser_2 = {
        "id": "slacs-laser-r-2", "kind": "laser_shot", "type": "box",
        "x": 300, "y": 400, "w": 25, "h": 25, "label": "SLACS_ROI #2",
        "color": "#a0a6ae",
    }
    initial = overlay.validate_context(store, {**payload, "laser_items": [laser_1, laser_2]}, slide_url="http://local/s/1/")
    assert initial["annotation_scope"] == "plane"
    assert len(initial["laser_items"]) == 2
    api = overlay.TranslucentApi(initial, logger=Mock(), on_closed=Mock())
    api._configured = True
    api._window = Mock()
    api._page_url = "http://local/trace"
    assert [item["id"] for item in api.translucent_context()["laser_items"]] == ["slacs-laser-r-1", "slacs-laser-r-2"]

    # Deleting laser_1 in the main viewer and refreshing on the same plane removes it.
    after_delete = overlay.validate_context(store, {**payload, "laser_items": [laser_2]}, slide_url="http://local/s/1/")
    api._refresh_context(after_delete)
    assert [item["id"] for item in api.translucent_context()["laser_items"]] == ["slacs-laser-r-2"]

    # Changing Z or T on a plane-scoped slide changes _laser_scope and rejects late confirmations.
    api._pending_laser_scope = api._laser_scope(api._context)
    z_changed = overlay.validate_context(
        store, {**payload, "query": "?c=1&g=pixels-2&t=1&p=0&z=3"}, slide_url="http://local/s/1/"
    )
    api._refresh_context(z_changed)
    assert api.translucent_context()["laser_items"] == []
    assert api._confirm_laser_box({"item": laser_1}) is False
    assert api.translucent_context()["laser_items"] == []

    # By contrast, a site-scoped (ND2) plate keeps _laser_items across Z changes on the same site.
    store.attrs["nd2wsi"]["plate"]["annotationScope"] = "site"
    site_ctx = overlay.validate_context(store, {**payload, "laser_items": [laser_1]}, slide_url="http://local/s/1/")
    assert site_ctx["annotation_scope"] == "site"
    api._refresh_context(site_ctx)
    assert len(api.translucent_context()["laser_items"]) == 1
    site_z_changed = overlay.validate_context(
        store, {**payload, "query": "?c=1&g=pixels-2&t=1&p=0&z=3"}, slide_url="http://local/s/1/"
    )
    api._refresh_context(site_z_changed)
    assert [item["id"] for item in api.translucent_context()["laser_items"]] == ["slacs-laser-r-1"]
    api._did_close()


@pytest.mark.parametrize("bad_laser", [
    "not-a-list",
    [{"id": "a", "kind": "not_laser", "type": "box", "x": 1, "y": 1, "w": 2, "h": 2}],
    [{"id": "", "kind": "laser_shot", "type": "box", "x": 1, "y": 1, "w": 2, "h": 2}],
    [{"id": "a", "kind": "laser_shot", "type": "pin", "x": 1, "y": 1, "w": 2, "h": 2}],
    [{"id": "a", "kind": "laser_shot", "type": "box", "x": 7999, "y": 1, "w": 5, "h": 2}],
    [{"id": "a", "kind": "laser_shot", "type": "box", "x": 1, "y": 1, "w": 0, "h": 2}],
    [{"id": "a", "kind": "laser_shot", "type": "box", "x": 1, "y": 1, "w": 2, "h": 2, "color": "red"}],
    [{"id": "dup", "kind": "laser_shot", "type": "box", "x": 1, "y": 1, "w": 2, "h": 2},
     {"id": "dup", "kind": "laser_shot", "type": "box", "x": 4, "y": 4, "w": 2, "h": 2}],
    [{"id": "a", "kind": "laser_shot", "type": "box", "x": 1, "y": 1, "w": 2, "h": 2, "text": 123}],
    [{"id": f"i-{i}", "kind": "laser_shot", "type": "box", "x": 1, "y": 1, "w": 2, "h": 2} for i in range(10001)],
])
def test_validate_context_rejects_malformed_laser_items(store, payload, bad_laser):
    with pytest.raises(ValueError):
        overlay.validate_context(store, {**payload, "laser_items": bad_laser}, slide_url="local/")


def test_slacs_1_1_and_1_2_refresh_interleaving_and_large_annotation_history(store, payload):
    import threading
    from unittest.mock import patch

    # slacs-1-2: 513 saved laser annotations and a 1001-char edited label must
    # succeed in validate_context (with the overlay display label bounded to 1000 chars).
    many_items = [
        {
            "id": f"slacs-laser-r-{i}",
            "kind": "laser_shot",
            "type": "box",
            "x": 10 + (i % 100) * 20,
            "y": 10 + (i // 100) * 20,
            "w": 18,
            "h": 18,
            "text": "x" * 1001 if i == 0 else f"SLACS_ROI #{i + 1}",
            "color": "#a0a6ae",
            "slacs": {"kind": "laser_shot", "shots": 1, "isolation": i + 1},
        }
        for i in range(513)
    ]
    ctx_many = overlay.validate_context(store, {**payload, "laser_items": many_items}, slide_url="http://local/s/1/")
    assert len(ctx_many["laser_items"]) == 513
    assert len(ctx_many["laser_items"][0]["text"]) == 1000
    assert len(many_items[0]["text"]) == 1001

    # slacs-1-1: Interleaving _prepare_laser_box with refresh_context(z=0 -> z=1)
    # must never tag old z=0 target coordinates with the new z=1 plane.
    race_ctx = {
        "slide_url": "http://local/s/slide/",
        "query": "?g=pixels&t=0&p=0&z=0",
        "pixel_size_um": [1.0, 1.0],
        "annotation_scope": "plane",
        "source_format": "czi",
    }
    delivered = []
    api = overlay.TranslucentApi(
        race_ctx, logger=Mock(), on_closed=Mock(), on_laser_box=lambda p: (delivered.append(p) or True)
    )
    api._configured = True
    api._window = Mock(native=None)
    api._dispatch = Mock(return_value=True)
    api._page_url = "http://local/trace"
    api._stage_follow = Mock()
    api._stage_follow_receiver_id = "receiver-old"
    api._locked = True
    view = {
        "version": 1,
        "center_px": [100, 100],
        "zoom": 1,
        "locked": True,
        "follow_session": "receiver-old",
        "follow_seq": 8,
        "target_px": [100, 100],
        "target_offset_css": [0, 0],
        "target_side_um": 18,
        "target_rotation_deg": 0,
        "target_calibrated": True,
        "target_corners_px": [[91, 91], [109, 91], [109, 109], [91, 109]],
    }
    assert api.report_overlay_view(view)["ok"]
    record = {
        "receiver_id": "receiver-old",
        "press": 1,
        "follow_seq": 8,
        "locked": True,
        "at": "2026-11-08T10:00:00Z",
        "slacs_session": "synthetic",
    }
    reached, resume = threading.Event(), threading.Event()

    class BarrierView(dict):
        def get(self, key, default=None):
            if key == "target_corners_px":
                reached.set()
                assert resume.wait(5)
            return super().get(key, default)

    api._overlay_view = BarrierView(api._overlay_view)
    result = {}

    def prepare():
        result["payload"], result["reason"] = api._prepare_laser_box(record)

    with patch("nd2wsi.translucent_window.session_log.event"):
        worker = threading.Thread(target=prepare)
        worker.start()
        assert reached.wait(5)
        try:
            api._refresh_context({**race_ctx, "query": "?g=pixels&t=0&p=0&z=1", "laser_items": []})
        finally:
            resume.set()
            worker.join(5)
    assert result["payload"]["z"] == 0
    assert api._deliver_laser_box(result["payload"]) is False
    assert delivered == []
    api._did_close()


def test_format_neutral_translucent_wording(payload, monkeypatch):
    monkeypatch.setattr(app.sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda _sid: None))
    res = api.open_translucent_viewer("missing", payload)
    assert not res["ok"]
    assert "Open an image before opening the translucent viewer" in res["message"]
    assert "fluorescence" not in res["message"]
    html = (Path(__file__).resolve().parents[1] / "nd2wsi/static/translucent.html").read_text(encoding="utf-8")
    assert 'aria-label="Translucent viewer"' in html
    assert "fluorescence" not in html


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

    def make_orphan(uid, on_closed):
        child = SimpleNamespace(_window=SimpleNamespace(uid=uid))
        child.close_translucent = Mock(side_effect=lambda: on_closed(child))
        return child

    # 1. Concurrent close_translucent_viewer during create_translucent_window
    api = app.Api(None)
    stores = {"abc": store}
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: stores.get(sid)))
    orphans = []

    def create_during_close(context, **kwargs):
        assert api.close_translucent_viewer("abc") == {"ok": True, "closed": True}
        orphan = make_orphan("orphan-1", kwargs["on_closed"])
        orphans.append(orphan)
        return orphan

    monkeypatch.setattr(overlay, "create_translucent_window", create_during_close)
    res = api.open_translucent_viewer("abc", payload)
    assert res["ok"] is False and "superseded" in res["message"]
    orphans[-1].close_translucent.assert_called_once()
    assert api._translucent_windows == {}
    assert api._translucent_pending_launches == {}
    assert api._translucent_claim is None

    # 2. Concurrent registry removal + on_remove during create_translucent_window
    def create_during_remove(context, **kwargs):
        stores.pop("abc", None)
        api.close_translucent_viewer("abc")
        orphan = make_orphan("orphan-2", kwargs["on_closed"])
        orphans.append(orphan)
        return orphan

    monkeypatch.setattr(overlay, "create_translucent_window", create_during_remove)
    res2 = api.open_translucent_viewer("abc", payload)
    assert res2["ok"] is False and "slide was closed" in res2["message"]
    orphans[-1].close_translucent.assert_called_once()
    assert api._translucent_windows == {}
    assert api._translucent_pending_launches == {}
    assert api._translucent_claim is None

    # 3. Concurrent fresh launch while exclusive claim is in-flight returns another_open without clobbering launch 1
    stores["abc"] = store
    first_entered = threading.Event()
    allow_first_return = threading.Event()
    created_children = []

    def create_racing(context, **kwargs):
        child = make_orphan("trace-first", kwargs["on_closed"])
        created_children.append(child)
        first_entered.set()
        allow_first_return.wait(timeout=3)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", create_racing)
    first_result = {}

    def open_first():
        first_result["res"] = api.open_translucent_viewer("abc", payload)

    t = threading.Thread(target=open_first)
    t.start()
    assert first_entered.wait(timeout=2)
    second_result = api.open_translucent_viewer("abc", payload)
    assert second_result["ok"] is False and second_result["reason"] == "another_open"
    allow_first_return.set()
    t.join(timeout=2)
    assert first_result["res"]["ok"] is True and first_result["res"]["id"] == "trace-first"
    assert api._translucent_windows["abc"] is created_children[0]
    assert api._translucent_pending_launches == {}
    assert api.close_translucent_viewer("abc") == {"ok": True, "closed": True}
    assert api._translucent_claim is None

    # 4. Concurrent generation change during create aborts launch and releases claim
    def create_during_gen_change(context, **kwargs):
        store.generation = "pixels-3"
        orphan = make_orphan("orphan-3", kwargs["on_closed"])
        orphans.append(orphan)
        return orphan

    monkeypatch.setattr(overlay, "create_translucent_window", create_during_gen_change)
    res_gen = api.open_translucent_viewer("abc", payload)
    assert res_gen["ok"] is False and "image changed" in res_gen["message"]
    orphans[-1].close_translucent.assert_called_once()
    assert api._translucent_pending_launches == {}
    assert api._translucent_claim is None

    # 5. Exception in create_translucent_window cleans up pending reservation and claim
    store.generation = "pixels-2"
    monkeypatch.setattr(
        overlay, "create_translucent_window", Mock(side_effect=RuntimeError("window failed")),
    )
    res_err = api.open_translucent_viewer("abc", payload)
    assert res_err["ok"] is False
    assert api._translucent_pending_launches == {}
    assert api._translucent_claim is None


def test_update_translucent_display_routing_generation_expiry_and_multi_owner_isolation(
    store, payload, monkeypatch, tmp_path,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    created = []

    def make_real_api(context, *, url, title, logger, on_closed, launch_token=None, launch_generation="", **_extra):
        child = overlay.TranslucentApi(
            context,
            logger=logger,
            on_closed=on_closed,
            launch_token=launch_token,
            launch_generation=launch_generation,
        )
        child._window = SimpleNamespace(uid=f"win-{len(created) + 1}", destroy=Mock(side_effect=child._did_close))
        created.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", make_real_api)

    # Two independent Api owners (User + private Agent) sharing the same sid must never cross-talk.
    store1 = copy.deepcopy(store)
    store2 = copy.deepcopy(store)
    api1 = app.Api(None)
    api2 = app.Api(None, window_session=SimpleNamespace(role="agent", root=tmp_path / "agent"))
    api1._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store1 if sid == "shared" else None))
    api2._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store2 if sid == "shared" else None))

    try:
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
    finally:
        api1._close_translucent_windows()
        api2._close_translucent_windows()


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
    assert "update_block_reason" in parent_funcs
    assert "attach_updater" not in parent_funcs
    assert "stop_server_for_update" not in parent_funcs
    assert all(not part.startswith("_") for name in parent_funcs for part in name.split("."))

    child_api = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=Mock())
    child_funcs = exposed_bridge_functions(child_api)
    assert "translucent_context" in child_funcs
    assert "translucent_display_state" in child_funcs
    assert "wait_translucent_display" in child_funcs
    # Bridge security: Python-only SLACS helpers and internal display helpers must not be exposed.
    for forbidden_name in (
        "refresh_context",
        "report_slacs_laser_box",
        "_refresh_context",
        "_report_slacs_laser_box",
        "_is_closed",
        "_session_snapshot",
        "_apply_display_query",
        "_expire_display_session",
        "_did_close",
        "_configure_and_show",
    ):
        assert forbidden_name not in child_funcs
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

    def create_already_closed(context, *, url, title, logger, on_closed, launch_token=None, launch_generation="", **_extra):
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
    assert api._translucent_claim is None
    assert closed_children[0]._session_snapshot()[3] is True

    # 2. Stub child without _session_snapshot exposing _closed=True also aborts without wedging claim.
    stubs = []

    def create_closed_stub(context, **kwargs):
        stub = SimpleNamespace(_window=SimpleNamespace(uid="dead-stub"), _closed=True)
        stub.close_translucent = Mock(side_effect=lambda: kwargs["on_closed"](stub))
        stubs.append(stub)
        return stub

    monkeypatch.setattr(overlay, "create_translucent_window", create_closed_stub)
    res_stub = api.open_translucent_viewer("abc", payload)
    assert res_stub["ok"] is False
    assert "closed while opening" in res_stub["message"]
    stubs[0].close_translucent.assert_called_once()
    assert api._translucent_windows == {}
    assert api._translucent_pending_launches == {}
    assert api._translucent_claim is None

    # 3. Subsequent healthy launch succeeds (proving _translucent_claim was not wedged).
    live = []

    def create_healthy(context, **kwargs):
        child = SimpleNamespace(_window=SimpleNamespace(uid="live-ok"), _closed=False)
        child.close_translucent = Mock(side_effect=lambda: kwargs["on_closed"](child))
        live.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", create_healthy)
    ok_res = api.open_translucent_viewer("abc", payload)
    assert ok_res["ok"] is True and ok_res["id"] == "live-ok"
    assert api.close_translucent_viewer("abc") == {"ok": True, "closed": True}
    assert api._translucent_claim is None


def test_empty_display_query_after_generation_change_expires_and_wakes_waiter(
    store, payload, monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    created = []

    def make_real_api(context, *, url, title, logger, on_closed, launch_token=None, launch_generation="", **_extra):
        child = overlay.TranslucentApi(
            context,
            logger=logger,
            on_closed=on_closed,
            launch_token=launch_token,
            launch_generation=launch_generation,
        )
        child._window = SimpleNamespace(uid="win-expire", destroy=Mock(side_effect=child._did_close))
        created.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", make_real_api)
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store if sid == "abc" else None))
    try:
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
    finally:
        api._close_translucent_windows()


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


def test_slacs_same_window_reopen_atomically_swaps_session_wakes_waiter_and_retires_old_token(
    store, payload, monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    created = []

    def make_real_api(context, *, url, title, logger, on_closed, launch_token=None, launch_generation="", **_extra):
        child = overlay.TranslucentApi(
            context,
            logger=logger,
            on_closed=on_closed,
            launch_token=launch_token,
            launch_generation=launch_generation,
        )
        child._configured = True
        child._page_url = url
        child._window = SimpleNamespace(
            uid="slacs-reuse-1",
            load_url=Mock(),
            show=Mock(),
            destroy=Mock(side_effect=child._did_close),
        )
        created.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", make_real_api)
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store if sid == "abc" else None))
    try:
        first = api.open_translucent_viewer("abc", payload)
        assert first["ok"] is True and first["id"] == "slacs-reuse-1"
        child = created[0]
        old_token = first["launch_token"]

        # Waiter blocked on old_token must wake with closed=True as soon as reopen swaps the session.
        wait_out = {}
        started = threading.Event()

        def run_old_waiter():
            started.set()
            wait_out["res"] = child.wait_translucent_display(old_token, 0, timeout_ms=5000)

        wt = threading.Thread(target=run_old_waiter)
        wt.start()
        assert started.wait(timeout=2)
        time.sleep(0.02)

        reopened = api.open_translucent_viewer("abc", {**payload, "query": "?c=1&g=pixels-2"})
        assert reopened["ok"] is True
        assert reopened["id"] == "slacs-reuse-1"
        assert reopened["launch_token"] != old_token
        assert len(created) == 1  # reused existing native window

        wt.join(timeout=2)
        assert not wt.is_alive()
        assert wait_out["res"] == {"ok": False, "closed": True}

        # Late live-LUT updates for old_token are rejected as stale; new_token succeeds.
        assert api.update_translucent_display("abc", old_token, 1, "c=0") == {"ok": False, "stale": True}
        assert api.update_translucent_display("abc", reopened["launch_token"], 1, "win=0:128") == {
            "ok": True,
            "applied": True,
            "revision": 1,
        }
        assert child.translucent_context()["query"] == "?win=0%3A128&g=pixels-2"
        assert child.translucent_context()["launch_token"] == reopened["launch_token"]
    finally:
        api._close_translucent_windows()


def test_slacs_superseded_concurrent_reopen_preserves_follow_calibration_lock_and_newer_token(
    store, payload, monkeypatch,
):
    from nd2wsi import laser_target

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123/secret/")
    created = []

    def make_real_api(context, *, url, title, logger, on_closed, launch_token=None, launch_generation="", **_extra):
        child = overlay.TranslucentApi(
            context,
            logger=logger,
            on_closed=on_closed,
            launch_token=launch_token,
            launch_generation=launch_generation,
        )
        child._configured = True
        child._page_url = url
        child._dispatch = Mock()
        child._window = SimpleNamespace(
            uid="slacs-race-1",
            native=Mock(),
            load_url=Mock(),
            show=Mock(),
            destroy=Mock(side_effect=child._did_close),
        )
        created.append(child)
        return child

    monkeypatch.setattr(overlay, "create_translucent_window", make_real_api)
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: store if sid == "abc" else None))
    try:
        opened = api.open_translucent_viewer("abc", payload)
        assert opened["ok"] is True
        child = created[0]

        # Set a calibrated laser target that must remain intact across reopen and race aborts.
        calibrated = laser_target.validate({
            "offset_css": [12.5, -8.0],
            "side_um": 24.0,
            "rotation_deg": 30.0,
            "window": [100, 120, 640, 480],
            "dpr": 2.0,
        })
        child._laser_target = copy.deepcopy(calibrated)

        # Case A: Reopen 1 enters stop_stage_follow(); while blocked there, Reopen 2 arrives and
        # supersedes pending launch token. Reopen 1 must abort without overwriting context/session/token,
        # and Reopen 2 must win cleanly.
        in_stop = threading.Event()
        allow_stop_exit = threading.Event()
        stop_calls = []
        orig_stop = child.stop_stage_follow

        def blocking_stop():
            stop_calls.append(threading.get_ident())
            if len(stop_calls) == 1:
                in_stop.set()
                allow_stop_exit.wait(timeout=3)
            return orig_stop()

        child.stop_stage_follow = blocking_stop
        r1_out, r2_out = {}, {}

        t1 = threading.Thread(
            target=lambda: r1_out.__setitem__(
                "res", api.open_translucent_viewer("abc", {**payload, "query": "?c=0&g=pixels-2"})
            )
        )
        t1.start()
        assert in_stop.wait(timeout=2)

        t2 = threading.Thread(
            target=lambda: r2_out.__setitem__(
                "res", api.open_translucent_viewer("abc", {**payload, "query": "?c=1&g=pixels-2"})
            )
        )
        t2.start()
        # Wait until t2 has written its newer pending launch token before releasing t1.
        deadline = time.monotonic() + 2.0
        t1_pending = api._translucent_pending_launches.get("abc")
        while time.monotonic() < deadline:
            if api._translucent_pending_launches.get("abc") != t1_pending:
                break
            time.sleep(0.005)
        allow_stop_exit.set()
        t1.join(timeout=3)
        t2.join(timeout=3)

        assert r1_out["res"]["ok"] is False and "superseded" in r1_out["res"]["message"]
        assert r2_out["res"]["ok"] is True
        win_token = r2_out["res"]["launch_token"]
        assert child.translucent_context()["launch_token"] == win_token
        assert child.translucent_context()["query"] == "?c=1&g=pixels-2"
        assert child.translucent_context()["laser_target"] == calibrated
        assert child._window.load_url.call_count == 1

        # Case B: A stale reopen that was superseded BEFORE acquiring _refresh_lock must abort
        # BEFORE calling stop_stage_follow(), preserving active Follow, lock state, calibration, and token.
        active_follow = Mock()
        child._stage_follow = active_follow
        child._locked = True
        stop_spy = Mock(side_effect=AssertionError("superseded reopen must not call stop_stage_follow"))
        child.stop_stage_follow = stop_spy
        stale_context = {**child.translucent_context(), "query": "?c=0&g=pixels-2"}

        with pytest.raises(ValueError, match="superseded"):
            app._app_slacs.reopen_translucent_overlay(
                api,
                "abc",
                child,
                stale_context,
                launch_token="stale-token",
                launch_generation=store.generation,
            )
        stop_spy.assert_not_called()
        active_follow.close.assert_not_called()
        assert child._stage_follow is active_follow
        assert child._locked is True
        assert child.translucent_context()["launch_token"] == win_token
        assert child.translucent_context()["query"] == "?c=1&g=pixels-2"
        assert child.translucent_context()["laser_target"] == calibrated
    finally:
        api._close_translucent_windows()


def test_encode_validated_query_enforces_post_encode_16384_byte_cap(payload, monkeypatch):
    # Many channels with raw ':' and ',' pass the pre-parse 16384-char check but
    # expand past 16384 bytes once percent-encoded (%3A, %2C).
    many_channels = 2200
    big_store = SimpleNamespace(
        attrs={
            "nd2wsi": {"rgb": False, "levels": [{"width": 8000, "height": 6000}]},
            "omero": {"channels": [{} for _ in range(many_channels)]},
        },
        generation="pixels-2",
        plate=None,
    )
    raw_win = ",".join(["0:1"] * many_channels)
    raw_query = f"?win={raw_win}&g=pixels-2"
    assert len(raw_query) <= 16384
    assert len("?win=" + raw_win.replace(":", "%3A").replace(",", "%2C") + "&g=pixels-2") > 16384

    with pytest.raises(ValueError, match="Query too long"):
        overlay.validate_context(big_store, {**payload, "query": raw_query}, slide_url="http://local/s/abc/")

    with pytest.raises(ValueError, match="Query too long"):
        overlay.merge_display_query(big_store, "?g=pixels-2", f"win={raw_win}")

    # Live display update surfaces the post-encode cap cleanly as invalid.
    monkeypatch.setattr(sys, "platform", "darwin")
    child = overlay.TranslucentApi(
        {"dimensions": [8000, 6000], "query": "?g=pixels-2"},
        logger=Mock(),
        on_closed=Mock(),
        launch_token="tok-long",
        launch_generation="pixels-2",
    )
    api = app.Api(None)
    api._httpd = SimpleNamespace(registry=SimpleNamespace(get=lambda sid: big_store if sid == "s1" else None))
    api._translucent_windows["s1"] = child
    res = api.update_translucent_display("s1", "tok-long", 1, f"win={raw_win}")
    assert res == {"ok": False, "invalid": True, "message": "Query too long"}

    # Exact 16384-byte encoded query succeeds; 16385 fails.
    exact = overlay.encode_validated_query([("g", "a" * (16384 - len("?g=")))])
    assert len(exact) == 16384
    with pytest.raises(ValueError, match="Query too long"):
        overlay.encode_validated_query([("g", "a" * (16385 - len("?g=")))])
    assert overlay.encode_validated_query([]) == ""


def test_close_translucent_bypasses_shown_wait_on_confirmed_unshown_cocoa_window(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    queued = []
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: SimpleNamespace(addOperationWithBlock_=queued.append)),
    ))
    native = SimpleNamespace(close=Mock())
    window = SimpleNamespace(
        uid="ov-cocoa-1",
        native=native,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(side_effect=AssertionError("must not call @_shown_call destroy() on unshown window")),
    )
    on_closed = Mock()
    api = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed)
    api._window = window

    assert api.close_translucent() is True
    window.destroy.assert_not_called()
    assert len(queued) == 1
    native.close.assert_not_called()
    queued.pop()()
    native.close.assert_called_once_with()
    on_closed.assert_called_once_with(api)


def test_close_translucent_direct_destroy_winforms_and_gui_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda fn: fn))

    # 1. Worker thread (InvokeRequired=True) uses BeginInvoke and checks IsDisposed.
    invoked = []
    native = SimpleNamespace(
        IsDisposed=False,
        InvokeRequired=True,
        BeginInvoke=invoked.append,
        Close=Mock(),
    )
    window = SimpleNamespace(
        uid="ov-win-1",
        native=native,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(side_effect=AssertionError("must not block in destroy()")),
    )
    on_closed = Mock()
    api = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed)
    api._window = window
    assert api.close_translucent() is True
    assert len(invoked) == 1
    invoked.pop()()
    native.Close.assert_called_once_with()
    on_closed.assert_called_once_with(api)

    # 2. Already disposed native form skips Close() and still releases claim on unshown path.
    disposed_native = SimpleNamespace(IsDisposed=True, InvokeRequired=False, Close=Mock())
    on_closed2 = Mock()
    api2 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed2)
    api2._window = SimpleNamespace(native=disposed_native, events=SimpleNamespace(shown=Event()), destroy=Mock())
    assert api2.close_translucent() is True
    disposed_native.Close.assert_not_called()
    api2._window.destroy.assert_not_called()
    on_closed2.assert_called_once_with(api2)

    # 3. Missing native falls back to gui.destroy_window(uid) and releases claim on unshown path.
    gui = SimpleNamespace(destroy_window=Mock())
    on_closed3 = Mock()
    api3 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed3)
    api3._window = SimpleNamespace(uid="ov-fallback", native=None, gui=gui, events=SimpleNamespace(shown=Event()), destroy=Mock())
    assert api3.close_translucent() is True
    gui.destroy_window.assert_called_once_with("ov-fallback")
    api3._window.destroy.assert_not_called()
    on_closed3.assert_called_once_with(api3)

    # 4. Shown window uses normal window.destroy() and preserves native closed-event claim ownership.
    shown = Event()
    shown.is_set = lambda: True
    on_closed4 = Mock()
    api4 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed4)
    api4._window = SimpleNamespace(uid="ov-shown", native=native, events=SimpleNamespace(shown=shown), destroy=Mock())
    assert api4.close_translucent() is True
    api4._window.destroy.assert_called_once_with()
    on_closed4.assert_not_called()
    api4._did_close()
    on_closed4.assert_called_once_with(api4)

    # 5. Invoke-only WinForms native and InvokeRequired=False both call Close() on unshown path.
    invoke_only_native = SimpleNamespace(IsDisposed=False, InvokeRequired=True, Invoke=lambda fn: fn(), Close=Mock())
    on_closed5 = Mock()
    api5 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed5)
    api5._window = SimpleNamespace(native=invoke_only_native, events=SimpleNamespace(shown=Event()), destroy=Mock())
    assert api5.close_translucent() is True
    invoke_only_native.Close.assert_called_once_with()
    api5._window.destroy.assert_not_called()
    on_closed5.assert_called_once_with(api5)

    direct_native = SimpleNamespace(IsDisposed=False, InvokeRequired=False, Close=Mock())
    on_closed6 = Mock()
    api6 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed6)
    api6._window = SimpleNamespace(native=direct_native, events=SimpleNamespace(shown=Event()), destroy=Mock())
    assert api6.close_translucent() is True
    direct_native.Close.assert_called_once_with()
    on_closed6.assert_called_once_with(api6)

    # 6. Configured window uses normal window.destroy() and preserves native closed-event claim ownership.
    on_closed7 = Mock()
    api7 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed7)
    api7._configured = True
    api7._window = SimpleNamespace(native=native, events=SimpleNamespace(shown=Event()), destroy=Mock())
    assert api7.close_translucent() is True
    api7._window.destroy.assert_called_once_with()
    on_closed7.assert_not_called()
    api7._did_close()
    on_closed7.assert_called_once_with(api7)

    # 7. Failing direct-destroy on confirmed-unshown window still invokes _did_close() in finally.
    on_closed_err = Mock()
    api8 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed_err)
    failing_gui = SimpleNamespace(destroy_window=Mock(side_effect=RuntimeError("native boom")))
    api8._window = SimpleNamespace(uid="ov-fail", native=None, gui=failing_gui, events=SimpleNamespace(shown=Event()), destroy=Mock())
    with pytest.raises(RuntimeError, match="native boom"):
        api8.close_translucent()
    on_closed_err.assert_called_once_with(api8)


def test_close_translucent_winforms_uncreated_handle_falls_back_without_off_thread_close(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda fn: fn))

    # 1. IsHandleCreated=False with InvokeRequired=False (off-thread hidden Form) must NOT call Close().
    native = SimpleNamespace(IsDisposed=False, IsHandleCreated=False, InvokeRequired=False, Close=Mock())
    gui = SimpleNamespace(destroy_window=Mock())
    on_closed = Mock()
    api = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed)
    api._window = SimpleNamespace(
        uid="ov-uncreated",
        native=native,
        gui=gui,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(),
    )
    assert api.close_translucent() is True
    native.Close.assert_not_called()
    gui.destroy_window.assert_called_once_with("ov-uncreated")
    api._window.destroy.assert_not_called()
    on_closed.assert_called_once_with(api)

    # 2. IsHandleCreated=False with InvokeRequired=True also skips BeginInvoke/Close and falls back.
    native2 = SimpleNamespace(
        IsDisposed=False,
        IsHandleCreated=False,
        InvokeRequired=True,
        BeginInvoke=Mock(),
        Close=Mock(),
    )
    gui2 = SimpleNamespace(destroy_window=Mock())
    on_closed2 = Mock()
    api2 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed2)
    api2._window = SimpleNamespace(
        uid="ov-uncreated-invoke",
        native=native2,
        gui=gui2,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(),
    )
    assert api2.close_translucent() is True
    native2.BeginInvoke.assert_not_called()
    native2.Close.assert_not_called()
    gui2.destroy_window.assert_called_once_with("ov-uncreated-invoke")
    on_closed2.assert_called_once_with(api2)

    # 3. IsHandleCreated=True with InvokeRequired=False calls Close() directly without gui fallback.
    created_native = SimpleNamespace(IsDisposed=False, IsHandleCreated=True, InvokeRequired=False, Close=Mock())
    gui3 = SimpleNamespace(destroy_window=Mock())
    api3 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=Mock())
    api3._window = SimpleNamespace(
        uid="ov-created",
        native=created_native,
        gui=gui3,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(),
    )
    assert api3.close_translucent() is True
    created_native.Close.assert_called_once_with()
    gui3.destroy_window.assert_not_called()


def test_close_translucent_winforms_absorbs_disposal_exceptions_and_notifies_closed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda fn: fn))

    # 1. BeginInvoke raising concurrent ObjectDisposedException is absorbed; _notify_closed still runs.
    begin_native = SimpleNamespace(
        IsDisposed=False,
        IsHandleCreated=True,
        InvokeRequired=True,
        BeginInvoke=Mock(side_effect=RuntimeError("ObjectDisposedException")),
        Invoke=Mock(),
        Close=Mock(),
    )
    gui1 = SimpleNamespace(destroy_window=Mock())
    on_closed1 = Mock()
    api1 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed1)
    api1._window = SimpleNamespace(
        uid="ov-begin-disposed",
        native=begin_native,
        gui=gui1,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(),
    )
    assert api1.close_translucent() is True
    begin_native.BeginInvoke.assert_called_once()
    begin_native.Invoke.assert_not_called()
    gui1.destroy_window.assert_not_called()
    on_closed1.assert_called_once_with(api1)

    # 2. Invoke raising concurrent InvalidOperationException is absorbed; _notify_closed still runs.
    invoke_native = SimpleNamespace(
        IsDisposed=False,
        IsHandleCreated=True,
        InvokeRequired=True,
        Invoke=Mock(side_effect=RuntimeError("InvalidOperationException")),
        Close=Mock(),
    )
    on_closed2 = Mock()
    api2 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed2)
    api2._window = SimpleNamespace(
        uid="ov-invoke-disposed",
        native=invoke_native,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(),
    )
    assert api2.close_translucent() is True
    invoke_native.Invoke.assert_called_once()
    on_closed2.assert_called_once_with(api2)

    # 3. Direct Close() raising concurrent ObjectDisposedException is absorbed; _notify_closed still runs.
    direct_native = SimpleNamespace(
        IsDisposed=False,
        IsHandleCreated=True,
        InvokeRequired=False,
        Close=Mock(side_effect=RuntimeError("ObjectDisposedException")),
    )
    on_closed3 = Mock()
    api3 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed3)
    api3._window = SimpleNamespace(
        uid="ov-direct-disposed",
        native=direct_native,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(),
    )
    assert api3.close_translucent() is True
    direct_native.Close.assert_called_once_with()
    on_closed3.assert_called_once_with(api3)


def test_close_translucent_mid_reveal_skips_unshown_shortcut_and_clears_revealing_on_error(
    cocoa_api, monkeypatch
):
    direct_destroy = Mock()
    monkeypatch.setattr(overlay, "_direct_destroy_window", direct_destroy)

    # 1. Closing while native reveal is in flight (_revealing=True) must NOT take
    #    the unshown _direct_destroy_window shortcut; it must call window.destroy().
    q = cocoa_api
    q.window.events = SimpleNamespace(shown=Event())
    saw_revealing = []

    def close_during_reveal(_sender):
        saw_revealing.append((q.api._revealing, q.api._configured))
        assert q.api.close_translucent() is True

    q.native.makeKeyAndOrderFront_.side_effect = close_during_reveal
    q.api._configure_and_show()
    q.queued.pop(0)()
    assert saw_revealing == [(True, False)]
    assert q.api._revealing is False
    assert q.api._configured is True
    direct_destroy.assert_not_called()
    q.window.destroy.assert_called_once_with()
    q.closed.assert_not_called()
    q.api._did_close()
    q.closed.assert_called_once_with(q.api)

    # 2. Direct _revealing=True state also bypasses _direct_destroy_window and preserves
    #    native closed-event single-overlay claim ownership.
    direct_destroy.reset_mock()
    on_closed2 = Mock()
    api2 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=Mock(), on_closed=on_closed2)
    api2._revealing = True
    api2._window = SimpleNamespace(
        uid="ov-revealing",
        native=Mock(),
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(),
    )
    assert api2.close_translucent() is True
    direct_destroy.assert_not_called()
    api2._window.destroy.assert_called_once_with()
    on_closed2.assert_not_called()
    api2._did_close()
    on_closed2.assert_called_once_with(api2)

    # 3. If native reveal raises before completion, _revealing resets to False
    #    and _configured stays False so subsequent close uses _direct_destroy_window.
    direct_destroy.reset_mock()
    logs3 = []
    on_closed3 = Mock()
    failing_native = Mock(
        frame=lambda: q.frame,
        makeKeyAndOrderFront_=Mock(side_effect=RuntimeError("reveal failed")),
    )
    api3 = overlay.TranslucentApi({"dimensions": [100, 100]}, logger=logs3.append, on_closed=on_closed3)
    api3._window = SimpleNamespace(
        uid="ov-reveal-err",
        native=failing_native,
        events=SimpleNamespace(shown=Event()),
        destroy=Mock(),
    )
    api3._configure_and_show()
    q.queued.pop(0)()
    assert api3._revealing is False
    assert api3._configured is False
    assert any("reveal failed" in msg for msg in logs3)
    assert api3.close_translucent() is True
    direct_destroy.assert_called_once_with(api3._window)
    api3._window.destroy.assert_not_called()
    on_closed3.assert_called_once_with(api3)


