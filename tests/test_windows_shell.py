"""Windows shell contracts without requiring Cocoa or a desktop test session."""

import io
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app


def test_windows_launcher_uses_utf8_with_inherited_legacy_streams(tmp_path, monkeypatch):
    launcher = Path(__file__).resolve().parents[1] / "packaging" / "launch.py"
    log = tmp_path / "검증 log.txt"
    raw = io.BytesIO()
    inherited = io.TextIOWrapper(raw, encoding="cp1252")
    stream = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys, "platform", "win32")
            patch.setattr(sys, "argv", ["nd2wsi-viewer.exe"])
            patch.setattr(sys, "stdout", inherited)
            patch.setattr(sys, "stderr", inherited)
            patch.setenv("ND2WSI_LOG_FILE", str(log))
            runpy.run_path(str(launcher), run_name="__launcher_encoding_test__")
            stream = sys.stdout
            print("opening 세포 sample.nd2 …")
            print("한글 오류 기록", file=sys.stderr)
            stream.flush()
        assert log.read_text(encoding="utf-8") == "opening 세포 sample.nd2 …\n한글 오류 기록\n"
        assert raw.getvalue() == b""
    finally:
        if stream is not None:
            stream.close()
        inherited.close()


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self


@pytest.fixture()
def fake_webview(monkeypatch):
    window = SimpleNamespace(
        events=SimpleNamespace(**{name: Event() for name in (
            "before_load", "closed", "loaded", "shown", "initialized",
        )}),
        evaluate_js=Mock(), destroy=Mock(),
    )
    webview = SimpleNamespace(
        create_window=Mock(return_value=window), settings={}, renderer="edgechromium",
    )
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(app, "_wire_file_drop", Mock())
    return webview, window


def test_windows_preserves_native_window_controls_and_rejects_mshtml(fake_webview, monkeypatch):
    webview, window = fake_webview
    cocoa = Mock(side_effect=AssertionError("Cocoa must not be loaded on Windows"))
    monkeypatch.setattr(app, "_inline_traffic_lights", cocoa)
    monkeypatch.setattr(app, "_install_open_files_handler", cocoa)
    api, result = app.create_app_window(None)
    assert result is window
    assert webview.create_window.call_args.kwargs["frameless"] is False
    assert webview.settings["ALLOW_DOWNLOADS"] is True
    assert app._wire_file_drop.call_count == 1
    assert window.events.shown.handlers == []
    guard, = window.events.initialized.handlers
    assert guard("edgechromium") is True
    assert guard("mshtml") is False
    assert "WebView2 Runtime is required" in api._startup_error
    assert api.title_bar_double_click() == "none"


def test_windows_updates_open_release_downloads_without_sparkle(fake_webview, monkeypatch):
    check = Mock(return_value={"ok": True, "mode": "manual-download"})
    monkeypatch.setattr("nd2wsi.release_selection.check_for_updates", check)
    api = app.Api(None)
    assert api.update_status()["available"] is True
    assert api.update_status()["mode"] == "download"
    assert api.check_for_updates()["ok"] is True
    check.assert_called_once_with(channel="stable")
    check.return_value = {"ok": False}
    assert api.check_for_updates()["ok"] is False


def test_windows_diagnostic_log_uses_local_app_data(fake_webview, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "한글 User"))
    app._dlog("한글 slide.nd2")
    assert app.log_path().is_relative_to(tmp_path / "한글 User")
    assert "한글 slide.nd2" in app.log_path().read_text(encoding="utf-8")


def test_gui_smoke_requires_slide_readiness_and_always_closes(fake_webview):
    _, window = fake_webview
    api = app.Api(None)
    window.evaluate_js.side_effect = [
        {"bridge": True, "version": "test", "boot": True, "slide": False},
        {"bridge": True, "version": "test", "boot": False, "slide": True},
    ]
    report = app.gui_smoke(api, window, expect_slide=True)
    assert report["ok"] is True and report["slide"] is True
    assert report["renderer"] == "edgechromium"
    assert window.evaluate_js.call_count == 2
    window.destroy.assert_called_once()
    assert api._gui_smoke_report is report


def test_gui_smoke_timeout_is_a_failure_and_closes_the_window(fake_webview):
    _, window = fake_webview
    report = app.gui_smoke(app.Api(None), window, timeout=0)
    assert report["ok"] is False and "did not finish" in report["error"]
    window.destroy.assert_called_once()


def test_main_passes_edge_renderer_and_writes_gui_report(fake_webview, monkeypatch, tmp_path):
    webview, window = fake_webview
    window.evaluate_js.return_value = {"bridge": True, "version": "test", "boot": True}
    webview.start = Mock(side_effect=lambda callback, **_kwargs: callback())
    path = tmp_path / "gui-smoke.json"
    assert app.main(["--gui-smoke", "--smoke-report", str(path)]) == 0
    assert webview.start.call_args.kwargs == {"gui": "edgechromium"}
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is True


def test_main_reports_missing_webview2_without_blocking_ci(fake_webview, monkeypatch, tmp_path):
    webview, window = fake_webview
    webview.start = Mock(side_effect=lambda *_args, **_kwargs: window.events.initialized.handlers[0]("mshtml"))
    dialog = Mock()
    monkeypatch.setattr(app, "_startup_error", dialog)
    path = tmp_path / "gui-smoke.json"
    assert app.main(["--gui-smoke", "--smoke-report", str(path)]) == 4
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is False
    assert "WebView2" in dialog.call_args.args[0]
    assert dialog.call_args.kwargs["show_dialog"] is False
