"""Native LUT clipboard contracts; every OS clipboard operation is mocked."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import lut_clipboard


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(lut_clipboard.sys, "platform", "darwin")
    clipboard = SimpleNamespace(
        clearContents=Mock(),
        stringForType_=Mock(),
        setString_forType_=Mock(return_value=True),
    )
    pasteboard = SimpleNamespace(generalPasteboard=Mock(return_value=clipboard))
    queue = SimpleNamespace(addOperationWithBlock_=Mock(side_effect=lambda callback: callback()))
    thread = SimpleNamespace(isMainThread=Mock(return_value=False))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSPasteboard=pasteboard, NSPasteboardTypeString="public.utf8-plain-text",
    ))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: queue), NSThread=thread,
    ))
    return SimpleNamespace(clipboard=clipboard, pasteboard=pasteboard, queue=queue, thread=thread)


def _text(channels=None):
    return json.dumps({"format": "nd2wsi-lut/1", "channels": channels or []})


def test_copy_and_read_roundtrip_dispatches_to_main_queue(native):
    text = _text([{"name": "DAPI", "min": 100, "max": 4500, "gamma": 0.37}])
    assert lut_clipboard.write_lut_clipboard(text) == {"ok": True}
    native.clipboard.clearContents.assert_called_once_with()
    native.clipboard.setString_forType_.assert_called_once_with(text, "public.utf8-plain-text")
    native.clipboard.stringForType_.return_value = text
    assert lut_clipboard.read_lut_clipboard() == {"ok": True, "text": text}
    assert native.queue.addOperationWithBlock_.call_count == 2


def test_main_thread_runs_without_queue_or_wait(native):
    native.thread.isMainThread.return_value = True
    assert lut_clipboard.write_lut_clipboard(_text()) == {"ok": True}
    native.queue.addOperationWithBlock_.assert_not_called()


@pytest.mark.parametrize("text", [
    None, "", "private clipboard text", "{}", "[]", "null",
    '{"format":"nd2wsi-lut/2","channels":[]}',
    '{"format":"nd2wsi-lut/1","channels":{}}',
    _text([{}] * 513),
    _text([{"name": "x" * (128 * 1024)}]),
    _text([{"name": "한" * (64 * 1024)}]),
    "\ud800",
], ids=[
    "none", "empty", "plain-text", "empty-object", "array", "null",
    "wrong-format", "wrong-channels", "too-many-channels", "too-large-ascii",
    "too-large-multibyte", "invalid-surrogate",
])
def test_invalid_write_never_touches_clipboard(native, text):
    assert lut_clipboard.write_lut_clipboard(text)["ok"] is False
    native.pasteboard.generalPasteboard.assert_not_called()
    native.queue.addOperationWithBlock_.assert_not_called()


@pytest.mark.parametrize("text", [None, "private unrelated clipboard text", "[]", _text([{}] * 513)])
def test_invalid_read_never_returns_unrelated_clipboard_text(native, text):
    native.clipboard.stringForType_.return_value = text
    result = lut_clipboard.read_lut_clipboard()
    assert result["ok"] is False
    assert "text" not in result
    assert "private unrelated" not in result["message"]
    native.clipboard.clearContents.assert_not_called()


@pytest.mark.parametrize("operation", ["read", "write"])
def test_timeout_is_bounded_and_late_queue_work_is_cancelled(native, monkeypatch, operation):
    native.queue.addOperationWithBlock_.side_effect = None
    monkeypatch.setattr(lut_clipboard, "MAIN_QUEUE_TIMEOUT", 0.001)
    result = (lut_clipboard.read_lut_clipboard() if operation == "read"
              else lut_clipboard.write_lut_clipboard(_text()))
    assert result["ok"] is False
    assert "time" in result["message"]
    native.queue.addOperationWithBlock_.call_args.args[0]()
    native.pasteboard.generalPasteboard.assert_not_called()


@pytest.mark.parametrize("platform", ["linux", "freebsd"])
def test_other_platforms_do_not_import_or_use_appkit(native, monkeypatch, platform):
    monkeypatch.setattr(lut_clipboard.sys, "platform", platform)
    assert "unsupported" in lut_clipboard.read_lut_clipboard()["message"]
    assert "unsupported" in lut_clipboard.write_lut_clipboard(_text())["message"]
    native.pasteboard.generalPasteboard.assert_not_called()
    native.queue.addOperationWithBlock_.assert_not_called()


def test_native_error_is_safe_and_write_failure_reported(native):
    native.clipboard.setString_forType_.return_value = False
    assert lut_clipboard.write_lut_clipboard(_text())["ok"] is False
    native.clipboard.stringForType_.side_effect = RuntimeError("private clipboard details")
    assert lut_clipboard.read_lut_clipboard() == {
        "ok": False, "message": "Could not access the system clipboard.",
    }


def test_missing_native_framework_is_reported_without_exception(native, monkeypatch):
    monkeypatch.setitem(sys.modules, "Foundation", None)
    assert lut_clipboard.read_lut_clipboard()["ok"] is False
    assert lut_clipboard.write_lut_clipboard(_text())["ok"] is False
    native.pasteboard.generalPasteboard.assert_not_called()


def test_api_delegates_only_to_lut_clipboard_helpers(monkeypatch):
    monkeypatch.setattr(lut_clipboard.sys, "platform", "darwin")
    from nd2wsi.app import Api

    read = Mock(return_value={"ok": True, "text": _text()})
    write = Mock(return_value={"ok": True})
    monkeypatch.setattr(lut_clipboard, "read_lut_clipboard", read)
    monkeypatch.setattr(lut_clipboard, "write_lut_clipboard", write)
    api = Api(None)
    assert api.read_lut_clipboard() == read.return_value
    assert api.write_lut_clipboard(_text()) == {"ok": True}
    read.assert_called_once_with()
    write.assert_called_once_with(_text())


@pytest.fixture
def windows_native(monkeypatch):
    monkeypatch.setattr(lut_clipboard.sys, "platform", "win32")
    clipboard = SimpleNamespace(GetText=Mock(), SetText=Mock())

    def store(text, _format):
        clipboard.GetText.return_value = text

    clipboard.SetText.side_effect = store
    action = Mock(side_effect=lambda callback: callback)
    native = SimpleNamespace(
        IsHandleCreated=True, IsDisposed=False, Disposing=False, InvokeRequired=True,
        BeginInvoke=Mock(side_effect=lambda callback: callback()),
    )
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=action))
    monkeypatch.setitem(sys.modules, "System.Windows.Forms", SimpleNamespace(
        Clipboard=clipboard, TextDataFormat=SimpleNamespace(UnicodeText="UnicodeText"),
    ))
    # A Windows transport must never import/access Cocoa, even in Mac-hosted tests.
    monkeypatch.setitem(sys.modules, "AppKit", None)
    monkeypatch.setitem(sys.modules, "Foundation", None)
    return SimpleNamespace(clipboard=clipboard, native=native, action=action,
                           window=SimpleNamespace(native=native))


def test_windows_unicode_copy_read_roundtrip_uses_only_owning_window_ui(windows_native):
    win = windows_native
    text = json.dumps({"format": "nd2wsi-lut/1", "channels": [
        {"label": "핵 DAPI 🧪", "lo": 127.25, "hi": 4363.75, "gamma": 0.37},
    ]}, ensure_ascii=False)
    assert lut_clipboard.write_lut_clipboard(text, window=win.window) == {"ok": True}
    assert lut_clipboard.read_lut_clipboard(window=win.window) == {"ok": True, "text": text}
    win.clipboard.SetText.assert_called_once_with(text, "UnicodeText")
    win.clipboard.GetText.assert_called_once_with("UnicodeText")
    assert win.native.BeginInvoke.call_count == win.action.call_count == 2


def test_windows_immediate_ui_thread_does_not_enqueue_or_wait(windows_native):
    win = windows_native
    win.native.InvokeRequired = False
    assert lut_clipboard.write_lut_clipboard(_text(), window=win.window) == {"ok": True}
    assert lut_clipboard.read_lut_clipboard(window=win.window) == {"ok": True, "text": _text()}
    win.native.BeginInvoke.assert_not_called()
    win.action.assert_not_called()


@pytest.mark.parametrize("text", [None, "", "private text", "{}", _text([{}] * 513), "\ud800"],
                         ids=["none", "empty", "plain-text", "empty-object", "too-many-channels", "surrogate"])
def test_windows_invalid_write_never_dispatches_or_touches_clipboard(windows_native, text):
    win = windows_native
    assert lut_clipboard.write_lut_clipboard(text, window=win.window)["ok"] is False
    win.native.BeginInvoke.assert_not_called()
    win.action.assert_not_called()
    win.clipboard.GetText.assert_not_called()
    win.clipboard.SetText.assert_not_called()


@pytest.mark.parametrize("text", [None, "private unrelated text", "[]", _text([{}] * 513)])
def test_windows_invalid_read_never_returns_clipboard_content(windows_native, text):
    win = windows_native
    win.clipboard.GetText.return_value = text
    assert lut_clipboard.read_lut_clipboard(window=win.window) == {
        "ok": False, "message": "Clipboard does not contain compatible LUT settings.",
    }
    win.clipboard.SetText.assert_not_called()


@pytest.mark.parametrize("operation", ["read", "write"])
def test_windows_bounded_timeout_cancels_queued_late_clipboard_access(windows_native, monkeypatch, operation):
    win = windows_native
    win.native.BeginInvoke.side_effect = None
    monkeypatch.setattr(lut_clipboard, "MAIN_QUEUE_TIMEOUT", 0)
    result = (lut_clipboard.read_lut_clipboard(window=win.window) if operation == "read" else
              lut_clipboard.write_lut_clipboard(_text(), window=win.window))
    assert result == {"ok": False, "message": "The system clipboard did not respond in time."}
    win.native.BeginInvoke.call_args.args[0]()
    win.clipboard.GetText.assert_not_called()
    win.clipboard.SetText.assert_not_called()


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("invoke_required", [False, True])
def test_windows_clipboard_errors_are_safe_on_ui_and_marshaled_paths(windows_native, operation, invoke_required):
    win = windows_native
    win.native.InvokeRequired = invoke_required
    win.clipboard.GetText.side_effect = RuntimeError("private clipboard details")
    win.clipboard.SetText.side_effect = RuntimeError("private clipboard details")
    result = (lut_clipboard.read_lut_clipboard(window=win.window) if operation == "read" else
              lut_clipboard.write_lut_clipboard(_text(), window=win.window))
    assert result == {"ok": False, "message": "Could not access the system clipboard."}


@pytest.mark.parametrize("state", ["missing-window", "missing-native", "no-handle", "disposed", "disposing"])
def test_windows_missing_or_closing_owner_never_uses_any_clipboard(windows_native, state):
    win = windows_native
    owner = win.window
    if state == "missing-window":
        owner = None
    elif state == "missing-native":
        owner = SimpleNamespace()
    elif state == "no-handle":
        win.native.IsHandleCreated = False
        win.native.InvokeRequired = False  # False does not prove a UI thread before creation.
    elif state == "disposed":
        win.native.IsDisposed = True
    else:
        win.native.Disposing = True
    for result in (lut_clipboard.read_lut_clipboard(window=owner),
                   lut_clipboard.write_lut_clipboard(_text(), window=owner)):
        assert result == {"ok": False, "message": "The viewer window is not ready for clipboard access."}
    win.native.BeginInvoke.assert_not_called()
    win.clipboard.GetText.assert_not_called()
    win.clipboard.SetText.assert_not_called()


def test_windows_owner_closed_before_dispatch_never_accesses_clipboard(windows_native):
    win = windows_native

    def close_then_dispatch(callback):
        win.native.IsDisposed = True
        callback()

    win.native.BeginInvoke.side_effect = close_then_dispatch
    assert lut_clipboard.write_lut_clipboard(_text(), window=win.window)["ok"] is False
    win.clipboard.SetText.assert_not_called()


@pytest.mark.parametrize("missing_framework", [False, True])
def test_windows_dispatch_or_framework_failure_is_reported_safely(windows_native, monkeypatch, missing_framework):
    win = windows_native
    if missing_framework:
        monkeypatch.setitem(sys.modules, "System", None)
    else:
        win.native.BeginInvoke.side_effect = RuntimeError("private native window details")
    assert lut_clipboard.read_lut_clipboard(window=win.window) == {
        "ok": False, "message": "Could not access the system clipboard.",
    }
    win.clipboard.GetText.assert_not_called()
