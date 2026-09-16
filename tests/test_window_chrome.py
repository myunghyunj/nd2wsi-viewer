"""Native adapters keep drop paths and menu actions on their owning window."""

from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from nd2wsi import app, window_chrome


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self


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
