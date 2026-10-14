"""Windows shell contracts without requiring Cocoa or a desktop test session."""

import io
import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app

NODE = shutil.which("node")


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
    assert "DRAG_REGION_SELECTOR" not in webview.settings
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
        {"bridge": True, "version": "test", "boot": False, "slide": True, "lut_toolbar": {"pending": True}},
        {"bridge": True, "version": "test", "boot": False, "slide": True, "lut_toolbar": {"ok": True}},
    ]
    report = app.gui_smoke(api, window, expect_slide=True)
    assert report["ok"] is True and report["slide"] is True
    assert report["renderer"] == "edgechromium"
    assert window.evaluate_js.call_count == 3
    window.destroy.assert_called_once()
    assert api._gui_smoke_report is report


def test_gui_smoke_fails_promptly_on_lut_asset_or_clipboard_error(fake_webview):
    _, window = fake_webview
    window.evaluate_js.return_value = {
        "bridge": True, "version": "test", "slide": True,
        "lut_toolbar": {"ok": False, "error": "LUT clipboard paste failed: Read permission denied"},
    }
    report = app.gui_smoke(app.Api(None), window, expect_slide=True)
    assert report["ok"] is False
    assert report["error"] == "LUT clipboard paste failed: Read permission denied"
    assert window.evaluate_js.call_count == 1
    window.destroy.assert_called_once()


@pytest.mark.parametrize("platform, opt_in, enabled", [
    ("win32", "1", True), ("win32", "true", False), ("win32", "0", False), ("darwin", "1", False),
])
def test_clipboard_smoke_is_strictly_opt_in_and_never_runs_on_mac(fake_webview, monkeypatch, platform, opt_in, enabled):
    _, window = fake_webview
    monkeypatch.setattr(app.sys, "platform", platform)
    monkeypatch.setenv("ND2WSI_SMOKE_LUT_CLIPBOARD", opt_in)
    window.evaluate_js.return_value = {"bridge": True, "version": "test", "boot": True}
    assert app.gui_smoke(app.Api(None), window)["ok"] is True
    script = window.evaluate_js.call_args.args[0]
    assert f"if ({str(enabled).lower()} && view.eval('state.lutAutoPending.some(Boolean)'))" in script
    assert "__CLIPBOARD_CHECK__" not in script


GUI_LUT_SCRIPT = r"""
const fs=require('fs'),vm=require('vm');
const {script,mode}=JSON.parse(fs.readFileSync(0,'utf8'));
const fetches=[],calls={copy:0,paste:0};let toast='',copied=false,encoded='exact effective LUTs';
const buttons={
  'lut-type':{onclick(){}},
  'lut-copy':{disabled:false,classList:{contains:()=>copied},async onclick(){
    calls.copy++;copied=mode!=='copy-failure';toast=copied?'Channel LUTs copied':'Write permission denied';
  }},
  'lut-paste':{disabled:false,async onclick(){
    calls.paste++;toast=mode==='paste-failure'?'Read permission denied':'Pasted 2 channel LUTs';
    if(mode==='changed-values')encoded='changed LUTs';
  }},
  'lut-dialog':{},'lut-copy-tooltip':{},'toast':{get textContent(){return toast}},
};
if(mode==='missing-handler')delete buttons['lut-paste'].onclick;
const view={Nd2LutTransfer:mode==='missing-module'?null:{encode(){},planPaste(){},fitsChannel(){}},
  navigator:{clipboard:{writeText(){},readText(){}}},
  fetch:async url=>{fetches.push(String(url));return {ok:mode!=='missing-icon',status:404,text:async()=>mode==='invalid-icon'?'bad':'svg'}},
  DOMParser:class{parseFromString(text){return {documentElement:{tagName:text==='svg'?'svg':'parsererror',getAttribute:()=> '0 0 20 20'},querySelector:()=>({})}}},
  eval(expression){
    if(expression==='state.lutAutoPending.some(Boolean)')return false;
    if(expression.includes('planPaste'))return 2;
    return encoded;
  },
};
const page={baseURI:'http://viewer.test/private-token/s/test/',
  querySelector:selector=>selector==='#stage canvas'?{width:100,height:100}:null,
  getElementById:id=>buttons[id]};
const frame={dataset:{sid:'test'},contentDocument:page,contentWindow:view};
const context={window:{pywebview:{api:{update_status(){}}},__nd2wsiSmokeBridge:{version:'test'}},
  document:{readyState:'complete',querySelector:()=>frame,getElementById:()=>null},readyFrames:new Set(['test']),URL};
vm.createContext(context);
(async()=>{
  const first=vm.runInContext(script,context);
  await frame.__nd2wsiSmokeLutTask;
  const result=vm.runInContext(script,context);
  process.stdout.write(JSON.stringify({first,result,fetches,calls}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize("mode, clipboard, error", [
    ("ready", False, None), ("ready", True, None),
    ("missing-module", False, "transfer module is unavailable"),
    ("missing-handler", False, "toolbar is not wired"),
    ("missing-icon", False, "HTTP 404"), ("invalid-icon", False, "Invalid packaged LUT icon"),
    ("copy-failure", True, "copy failed: Write permission denied"),
    ("paste-failure", True, "paste failed: Read permission denied"),
    ("changed-values", True, "changed effective channel values"),
])
def test_real_gui_smoke_script_checks_assets_and_opt_in_clipboard(fake_webview, monkeypatch, mode, clipboard, error):
    if NODE is None:
        pytest.skip("node is not installed")
    _, window = fake_webview
    monkeypatch.setenv("ND2WSI_SMOKE_LUT_CLIPBOARD", "1" if clipboard else "0")
    window.evaluate_js.return_value = {
        "bridge": True, "version": "test", "slide": True, "lut_toolbar": {"ok": True},
    }
    assert app.gui_smoke(app.Api(None), window, expect_slide=True)["ok"] is True
    script = window.evaluate_js.call_args.args[0]
    result = subprocess.run(
        [NODE, "-e", GUI_LUT_SCRIPT], input=json.dumps({"script": script, "mode": mode}),
        check=True, capture_output=True, text=True, timeout=20,
    )
    out = json.loads(result.stdout)
    assert out["first"]["lut_toolbar"] == {"pending": True}
    result = out["result"]["lut_toolbar"]
    if error:
        assert result["ok"] is False and error in result["error"]
    else:
        assert result["ok"] is result["module"] is result["handlers"] is True
        assert result["clipboard_api"] == {"write": True, "read": True}
        assert out["fetches"] == [
            "http://viewer.test/private-token/static/icons/" + name
            for name in ["fluent-copy-20-regular.svg", "fluent-clipboard-paste-20-regular.svg",
                         "fluent-checkmark-20-regular.svg"]
        ]
        if clipboard:
            assert result["clipboard_roundtrip"] == {"ok": True, "channels": 2, "effective_values_unchanged": True}
            assert out["calls"] == {"copy": 1, "paste": 1}
        else:
            assert "clipboard_roundtrip" not in result
    if not clipboard:
        assert out["calls"] == {"copy": 0, "paste": 0}


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
