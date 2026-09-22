"""Native adapters keep drop paths and menu actions on their owning window."""

from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app, window_chrome


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self


@pytest.fixture
def inline_chrome(monkeypatch):
    pending, toolbars, logs = [], [], []
    full_size, fullscreen, base_mask = 0x100, 0x200, 0x85
    bounds, layout_rect = object(), object()
    decoration = Mock(className=lambda: "NSTitlebarDecorationView")
    background = Mock(className=lambda: "NSTitlebarBackgroundView")
    titlebar_content = Mock(className=lambda: "NSTitlebarContentView")
    titlebar = Mock(
        className=lambda: "NSTitlebarContainerView",
        subviews=lambda: [decoration, background, titlebar_content],
    )
    unrelated_view = Mock(className=lambda: "NSContentView")
    frame = Mock(bounds=lambda: bounds, subviews=lambda: [titlebar, unrelated_view])
    content = Mock(superview=lambda: frame)
    button_frame = SimpleNamespace(
        origin=SimpleNamespace(x=10, y=12),
        size=SimpleNamespace(width=14, height=14),
    )
    button_parent = SimpleNamespace(
        frame=lambda: SimpleNamespace(size=SimpleNamespace(height=38)),
    )
    buttons = [Mock(frame=lambda: button_frame, superview=lambda: button_parent) for _ in range(3)]
    outer_frame = object()
    state = SimpleNamespace(mask=base_mask, toolbar=None, frame=outer_frame)

    def set_mask(mask):
        state.mask = mask
        state.frame = "resized by style-mask change"

    def set_frame(frame, display):
        assert display is True
        state.frame = frame

    def set_toolbar(toolbar):
        state.toolbar = toolbar

    class Toolbar:
        @classmethod
        def alloc(cls):
            bar = cls()
            bar.setShowsBaselineSeparator_ = Mock()
            # AppKit can change content geometry while attaching/showing a
            # toolbar; the adapter must apply its final frame afterwards.
            bar.setVisible_ = Mock(side_effect=lambda _: content.setFrame_("toolbar inset"))
            toolbars.append(bar)
            return bar

        def initWithIdentifier_(self, identifier):
            self.identifier = identifier
            return self

    native = Mock(
        styleMask=lambda: state.mask,
        frame=lambda: state.frame,
        setStyleMask_=Mock(side_effect=set_mask),
        setFrame_display_=Mock(side_effect=set_frame),
        toolbar=lambda: state.toolbar,
        setToolbar_=Mock(side_effect=set_toolbar),
        contentView=lambda: content,
        contentLayoutRect=lambda: layout_rect,
        standardWindowButton_=Mock(side_effect=lambda which: buttons[which]),
    )
    other_native = Mock()
    application = Mock(windows=lambda: [other_native, native])
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWindowStyleMaskFullSizeContentView=full_size,
        NSWindowStyleMaskFullScreen=fullscreen,
        NSWindowTitleHidden=1, NSTitlebarSeparatorStyleNone=1,
        NSToolbar=Toolbar, NSWindowToolbarStyleUnifiedCompact=4,
        NSViewWidthSizable=2, NSViewHeightSizable=16,
        NSWindowCloseButton=0, NSWindowMiniaturizeButton=1, NSWindowZoomButton=2,
        NSApplication=SimpleNamespace(sharedApplication=lambda: application),
    ))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(
            mainQueue=lambda: SimpleNamespace(addOperationWithBlock_=pending.append),
        ),
    ))
    window = SimpleNamespace(events=SimpleNamespace(**{
        name: Event() for name in ("shown", "loaded", "maximized", "restored")
    }))
    resolve = Mock(return_value=native)
    window_chrome.inline_traffic_lights(window, logger=logs.append, resolve_native_window=resolve)
    return SimpleNamespace(**locals())


@pytest.mark.parametrize("event", ["shown", "loaded", "maximized", "restored"])
@pytest.mark.parametrize("fullscreen", [False, True])
def test_inline_chrome_events_apply_actual_native_state_on_main_queue(inline_chrome, event, fullscreen):
    chrome = inline_chrome
    callback, = getattr(chrome.window.events, event).handlers
    callback()
    chrome.resolve.assert_not_called()
    assert chrome.native.mock_calls == []
    assert chrome.toolbars == []
    # Resolve state inside the queued operation, not on the event thread.
    chrome.state.mask = chrome.base_mask | chrome.full_size
    if fullscreen:
        chrome.state.mask |= chrome.fullscreen
    operation, = chrome.pending
    operation()
    chrome.resolve.assert_called_once_with(chrome.window)
    expected_mask = chrome.base_mask | (chrome.fullscreen if fullscreen else chrome.full_size)
    assert chrome.state.mask == expected_mask
    assert chrome.state.frame is chrome.outer_frame
    chrome.content.setFrame_.assert_called_with(chrome.layout_rect if fullscreen else chrome.bounds)
    chrome.content.setAutoresizingMask_.assert_called_once_with(2 | 16)
    chrome.state.toolbar.setVisible_.assert_called_once_with(not fullscreen)
    assert chrome.state.toolbar.identifier == "nd2wsi.titlebar.spacer"
    if fullscreen:
        chrome.native.standardWindowButton_.assert_not_called()
        chrome.decoration.setHidden_.assert_not_called()
        chrome.background.setHidden_.assert_not_called()
    else:
        for button in chrome.buttons:
            button.setHidden_.assert_called_once_with(False)
        chrome.decoration.setHidden_.assert_called_once_with(True)
        chrome.background.setHidden_.assert_called_once_with(True)
    chrome.titlebar_content.setHidden_.assert_not_called()
    chrome.unrelated_view.setHidden_.assert_not_called()
    assert chrome.other_native.mock_calls == []
    assert not any("failed" in line for line in chrome.logs)


def test_inline_chrome_restores_tabs_after_repeated_fullscreen_navigation(inline_chrome):
    chrome = inline_chrome

    def emit(event):
        callback, = getattr(chrome.window.events, event).handlers
        callback()
        chrome.pending.pop(0)()

    emit("shown")
    emit("loaded")
    for _ in range(2):
        chrome.state.mask |= chrome.fullscreen
        emit("maximized")
        assert chrome.state.mask == chrome.base_mask | chrome.fullscreen
        chrome.content.setFrame_.assert_called_with(chrome.layout_rect)
        chrome.state.toolbar.setVisible_.assert_called_with(False)
        native_button_calls = chrome.native.standardWindowButton_.call_count
        emit("loaded")  # Navigation must not reintroduce the hidden title-bar inset.
        assert chrome.state.mask == chrome.base_mask | chrome.fullscreen
        chrome.content.setFrame_.assert_called_with(chrome.layout_rect)
        chrome.state.toolbar.setVisible_.assert_called_with(False)
        assert chrome.native.standardWindowButton_.call_count == native_button_calls

        chrome.state.mask &= ~chrome.fullscreen
        emit("restored")
        assert chrome.state.mask == chrome.base_mask | chrome.full_size
        assert chrome.state.frame is chrome.outer_frame
        chrome.content.setFrame_.assert_called_with(chrome.bounds)
        chrome.state.toolbar.setVisible_.assert_called_with(True)
        for button in chrome.buttons:
            button.setHidden_.assert_called_with(False)
        chrome.decoration.setHidden_.assert_called_with(True)
        chrome.background.setHidden_.assert_called_with(True)
        emit("restored")  # Also emitted after deminiaturizing a normal window.

    assert len(chrome.toolbars) == 1
    assert chrome.native.setFrame_display_.call_count == 5  # Initial setup plus two enter/exit cycles.
    chrome.native.setToolbar_.assert_called_once_with(chrome.toolbars[0])
    assert chrome.other_native.mock_calls == []
    assert not any("failed" in line for line in chrome.logs)


def test_drop_paths_survive_navigation_and_target_only_the_owning_window(monkeypatch):
    handlers = []

    def handler(callback, *, prevent_default):
        assert prevent_default is True
        handlers.append(callback)
        return callback

    monkeypatch.setitem(sys.modules, "webview.dom", SimpleNamespace(DOMEventHandler=handler))
    other_window = SimpleNamespace(evaluate_js=Mock())
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(windows=[other_window]))
    window = SimpleNamespace(
        evaluate_js=Mock(), get_current_url=lambda: "http://127.0.0.1:8123",
        events=SimpleNamespace(loaded=Event()),
        dom=SimpleNamespace(document=SimpleNamespace(events=SimpleNamespace(drop=Event()))),
    )
    logs = []
    window_chrome.wire_file_drop(window, logger=logs.append)
    attach, = window.events.loaded.handlers
    attach()
    assert len(handlers) == 1
    handlers[0]({"dataTransfer": {"files": [{"name": "path-not-provided.nd2"}]}})
    window.evaluate_js.assert_not_called()

    # A new navigation replaces the document; its new drop handler must still
    # route Unicode, Windows backslashes, and quotes through JSON, not JS code.
    window.dom.document = SimpleNamespace(events=SimpleNamespace(drop=Event()))
    attach()
    assert len(window.dom.document.events.drop.handlers) == 1
    paths = [r'C:\한글 slides\cell "quoted".nd2', "/tmp/second slide.svs"]
    handlers[1]({"dataTransfer": {"files": [
        {"pywebviewFullPath": paths[0]}, {"name": "ignore.nd2"},
        {"pywebviewFullPath": paths[1]},
    ]}})
    script, = window.evaluate_js.call_args.args
    assert f"window.__pydrop_multi({json.dumps(paths)})" in script
    assert f"window.__pydrop({json.dumps(paths[0])})" in script
    other_window.evaluate_js.assert_not_called()
    assert "3 file(s), 2 with paths" in logs[-1]


def test_menu_keeps_role_targets_and_dispatches_browser_actions_off_main(monkeypatch):
    pending, items, evaluated = [], [], []
    main_thread = threading.current_thread()
    evaluated_event = threading.Event()

    class NativeObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    class Item:
        def __init__(self, title, action, key):
            self.title, self.action, self.key = title, action, key
            self.modifiers = None
            self.target = None

        def setKeyEquivalentModifierMask_(self, value):
            self.modifiers = value

        def setTarget_(self, value):
            self.target = value

    def add_item(title, action, key):
        item = Item(title, action, key)
        items.append(item)
        return item

    file_menu = SimpleNamespace(addItemWithTitle_action_keyEquivalent_=add_item)
    main_menu = SimpleNamespace(itemWithTitle_=lambda title: SimpleNamespace(submenu=lambda: file_menu))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSObject=NativeObject, NSEventModifierFlagCommand=1, NSEventModifierFlagShift=2,
        NSApplication=SimpleNamespace(sharedApplication=lambda: SimpleNamespace(mainMenu=lambda: main_menu)),
    ))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: SimpleNamespace(addOperationWithBlock_=pending.append)),
    ))

    def evaluate(script):
        evaluated.append((script, threading.current_thread()))
        evaluated_event.set()

    api = SimpleNamespace(new_window=Mock(return_value={"ok": True}), _status="")
    window = SimpleNamespace(evaluate_js=evaluate)
    window_chrome.install_window_menu(api, window, logger=Mock())
    window_chrome.install_window_menu(api, window, logger=Mock())
    assert items == []
    for install in pending:
        install()
    assert [(item.title, item.key, item.modifiers) for item in items[:2]] == [
        ("New Window", "n", 1), ("New Agent Window", "n", 3),
    ]
    assert len(items) == 4  # Duplicate queued install cannot add duplicate actions.
    assert all(item.target is api._window_menu_target for item in items)
    target = api._window_menu_target
    target.newUserWindow_(None)
    target.newAgentWindow_(None)
    assert [call.args for call in api.new_window.call_args_list] == [("user",), ("agent",)]

    for method, script in (
        (target.openInMetal_, "window.nd2OpenActiveInMetal?.(); true"),
        (target.retryInMetal_, "window.nd2OpenActiveInMetal?.(true); true"),
    ):
        evaluated_event.clear()
        method(None)
        assert evaluated_event.wait(2)
        assert evaluated[-1][0] == script
        assert evaluated[-1][1] is not main_thread

    api.new_window.return_value = {"ok": False, "message": "Could not create isolated window"}
    target.newAgentWindow_(None)
    assert api._status == "Could not create isolated window"


def test_app_close_guard_keeps_late_bound_diagnostics(monkeypatch):
    window = SimpleNamespace(evaluate_js=Mock(side_effect=RuntimeError("closed renderer")))
    guard = app.WindowCloseCoordinator(SimpleNamespace(), window)
    logs = []
    monkeypatch.setattr(app, "_dlog", logs.append)
    guard._notice("Save failed; window remains open")
    assert len(logs) == 1
    assert "closed renderer" in logs[0]
