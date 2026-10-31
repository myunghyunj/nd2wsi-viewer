"""Tracing overlays own their window, lock state and display-only context."""

from __future__ import annotations

import copy
import os
import sys
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
    assert api.open_translucent_viewer("abc", payload) == {"ok": True, "id": "trace-1"}
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


def test_parent_registry_passes_one_flip_store_to_each_overlay(store, payload, monkeypatch, tmp_path):
    from nd2wsi.follow_flips import FollowFlipStore

    monkeypatch.setattr(sys, "platform", "darwin")
    created = []

    def create(context, **options):
        created.append(options)
        return SimpleNamespace(_window=SimpleNamespace(uid=f"trace-{len(created)}"), close_translucent=Mock(), refresh_context=Mock())

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
        claim.acquire()
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
                            report_slacs_laser_box=Mock(return_value={"ok": True}))
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
        child.report_slacs_laser_box.assert_called_once_with(sent)
    finally:
        options["on_closed"](child)
    assert not api.report_slacs_laser_box(sent)["ok"]


def test_reuse_refreshes_display_and_focuses_without_another_window():
    api = overlay.TranslucentApi({"rotation": 0, "flip": False, "query": "?c=0"}, logger=Mock(), on_closed=Mock())
    api._configured = True
    api._window = Mock()
    api._page_url = "http://local/trace"
    receiver = api._stage_follow = Mock()
    api._overlay_view = {"target_px": [1, 2]}
    updated = {"rotation": 90, "flip": True, "query": "?c=1&win=1:200", "center": [20, 30]}
    api.refresh_context(updated)
    receiver.close.assert_called_once()
    assert api._overlay_view is None
    for key, value in updated.items():
        assert api.translucent_context()[key] == value
    api._window.load_url.assert_called_once_with("http://local/trace")
    api._window.show.assert_called_once()
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
