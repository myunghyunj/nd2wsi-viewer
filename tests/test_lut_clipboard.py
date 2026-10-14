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


@pytest.mark.parametrize("platform", ["linux", "win32"])
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
