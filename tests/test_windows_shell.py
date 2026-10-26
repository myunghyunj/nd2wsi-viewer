"""Windows shell contracts without requiring Cocoa or a desktop test session."""

import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from winforms_fake import FAKE_SYSTEM, FakeWinFormsWindow

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


@pytest.fixture()
def fake_webview(monkeypatch, tmp_path):
    # destroy() re-raises FormClosing and BeginInvoke runs on a UI thread, as
    # pywebview's WinForms backend does; Cocoa must never be imported.
    window = FakeWinFormsWindow()
    webview = SimpleNamespace(
        create_window=Mock(return_value=window), settings={}, renderer="edgechromium",
    )
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setitem(sys.modules, "System", FAKE_SYSTEM)
    monkeypatch.setitem(sys.modules, "Foundation", None)
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.delenv("ND2WSI_SMOKE_LUT_CLIPBOARD", raising=False)
    monkeypatch.setattr(app, "_wire_file_drop", Mock())
    monkeypatch.setattr(
        "nd2wsi.translucent_smoke.exercise_translucent_viewer", Mock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "nd2wsi.window_smoke.exercise_independent_window", Mock(return_value={"ok": True}),
    )
    # The WebView2 runtime proof needs a real CoreWebView2; nothing is required by default.
    monkeypatch.delenv("ND2WSI_WEBVIEW2_RUNTIME", raising=False)
    monkeypatch.setattr("nd2wsi.webview2_runtime.window_evidence",
                        Mock(return_value={"bundled": False, "error": "fake"}))
    monkeypatch.setattr("nd2wsi.webview2_runtime.default_app_root", lambda: tmp_path / "no-bundled-runtime")
    yield webview, window
    window.native.exit_loop()
    assert window.native.errors == []


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
    # No AppKit menu or Sparkle updater is installed on Windows.
    assert window.events.shown.handlers == []
    guard, = window.events.initialized.handlers
    assert guard("edgechromium") is True
    assert guard("mshtml") is False
    assert "WebView2 Runtime is required" in api._startup_error
    assert api.title_bar_double_click() == "none"
    # Native close (title-bar X, Alt+F4, taskbar) uses the same save veto as macOS.
    assert isinstance(api._close_coordinator, app.WindowCloseCoordinator)
    assert window.events.closing.handlers == [api._close_coordinator.on_closing]
    assert window.events.closed.handlers[-1] == api._close_coordinator.cancel_preparation
    assert api._close_translucent_windows in window.events.closed.handlers


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
    # Evidence is recorded even when the bundled runtime is not required.
    assert report["webview2_runtime"] == {"selection": None, "required": False,
                                          "main": {"bundled": False, "error": "fake"}, "translucent": None}


SLIDE_READY = {"bridge": True, "version": "test", "slide": True, "lut_toolbar": {"ok": True}}
BUNDLED = {"bundled": True, "browser_executable": "C:\\app\\_internal\\webview2-runtime\\msedgewebview2.exe",
           "browser_version": "156.0.4314.42", "error": None}
SYSTEM = {"bundled": False, "browser_executable":
          "C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\\156.0.4314.42\\msedgewebview2.exe",
          "browser_version": "156.0.4314.42", "error": None}


@pytest.mark.parametrize("evidence, error", [
    (SYSTEM, "Main window was not rendered by the bundled WebView2 runtime"),
    ({**SYSTEM, "error": "WebView2 probe failed: CoreWebView2 is None"}, "WebView2 probe failed: CoreWebView2 is None"),
])
def test_gui_smoke_fails_when_the_required_bundled_runtime_did_not_render_the_main_window(
        fake_webview, monkeypatch, evidence, error):
    from nd2wsi import translucent_smoke, webview2_runtime

    _, window = fake_webview
    monkeypatch.setenv("ND2WSI_WEBVIEW2_RUNTIME", "bundled")
    webview2_runtime.window_evidence.return_value = evidence
    window.evaluate_js.return_value = SLIDE_READY
    native_keyboard = Mock(return_value={"status": "not_assessed"})
    monkeypatch.setattr(app, "_gui_smoke_native_keyboard", native_keyboard)
    api, _ = app.create_app_window(None)
    report = app.gui_smoke(api, window, expect_slide=True)
    assert report["ok"] is False and report["phase"] == "webview2-runtime"
    assert report["error"] == error
    assert report["webview2_runtime"]["required"] is True
    assert report["webview2_runtime"]["main"] == evidence
    assert "translucent" not in report["webview2_runtime"]
    # The probe runs off the UI thread through the WinForms dispatcher, once.
    webview2_runtime.window_evidence.assert_called_once_with(window, translucent_smoke.native_call)
    native_keyboard.assert_not_called()
    translucent_smoke.exercise_translucent_viewer.assert_not_called()
    # A failed smoke never waits on the save handshake.
    assert report["close"]["path"] == "forced"
    window.destroy.assert_called_once()
    assert window.is_closed


def test_gui_smoke_passes_when_main_and_overlay_render_with_the_bundled_runtime(fake_webview, monkeypatch):
    from nd2wsi import translucent_smoke, webview2_runtime, window_smoke

    _, window = fake_webview
    monkeypatch.setenv("ND2WSI_WEBVIEW2_RUNTIME", "bundled")
    webview2_runtime.window_evidence.return_value = BUNDLED
    overlay = {**BUNDLED, "browser_process_id": 7}
    monkeypatch.setattr(translucent_smoke, "exercise_translucent_viewer",
                        Mock(return_value={"ok": True, "webview2_runtime": overlay}))
    window.evaluate_js.return_value = SLIDE_READY
    api = app.Api(None)
    api._webview2_runtime = {"kind": "bundled", "mode": "bundled", "path": "C:\\app\\_internal\\webview2-runtime",
                             "system_version": "156.0.4314.42", "reason": "bundled runtime requested"}
    report = app.gui_smoke(api, window, expect_slide=True)
    assert report["ok"] is True and report["phase"] == "complete"
    assert report["webview2_runtime"] == {"selection": api._webview2_runtime, "required": True,
                                          "main": BUNDLED, "translucent": overlay}
    window_smoke.exercise_independent_window.assert_called_once()
    json.dumps(report)


@pytest.mark.parametrize("tracing, error", [
    ({"ok": True}, "Translucent overlay was not rendered by the bundled WebView2 runtime"),
    ({"ok": True, "webview2_runtime": SYSTEM}, "Translucent overlay was not rendered by the bundled WebView2 runtime"),
    # The overlay's own failure is never hidden behind the runtime message.
    ({"ok": False, "error": "Overlay was not rendered by the bundled WebView2 runtime", "webview2_runtime": SYSTEM},
     "Overlay was not rendered by the bundled WebView2 runtime"),
])
def test_gui_smoke_fails_when_the_required_overlay_proof_is_missing_or_not_bundled(
        fake_webview, monkeypatch, tracing, error):
    from nd2wsi import translucent_smoke, webview2_runtime, window_smoke

    _, window = fake_webview
    monkeypatch.setenv("ND2WSI_WEBVIEW2_RUNTIME", "bundled")
    webview2_runtime.window_evidence.return_value = BUNDLED
    monkeypatch.setattr(translucent_smoke, "exercise_translucent_viewer", Mock(return_value=tracing))
    window.evaluate_js.return_value = SLIDE_READY
    report = app.gui_smoke(app.Api(None), window, expect_slide=True)
    assert report["ok"] is False and report["phase"] == "translucent-viewer"
    assert report["error"] == error
    assert report["webview2_runtime"]["main"] == BUNDLED
    assert report["webview2_runtime"]["translucent"] == tracing.get("webview2_runtime")
    window_smoke.exercise_independent_window.assert_not_called()
    window.destroy.assert_called_once()


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


def test_gui_smoke_reports_translucent_failure_and_closes(fake_webview, monkeypatch):
    _, window = fake_webview
    window.evaluate_js.return_value = {
        "bridge": True, "version": "test", "slide": True, "lut_toolbar": {"ok": True},
    }
    monkeypatch.setattr(
        "nd2wsi.translucent_smoke.exercise_translucent_viewer",
        Mock(return_value={"ok": False, "error": "Overlay did not preserve pixel scale"}),
    )
    report = app.gui_smoke(app.Api(None), window, expect_slide=True)
    assert report["ok"] is False
    assert report["phase"] == "translucent-viewer"
    assert report["error"] == "Overlay did not preserve pixel scale"
    window.destroy.assert_called_once()


def test_gui_smoke_reports_independent_window_failure_and_never_recurses_from_boot(fake_webview, monkeypatch):
    _, window = fake_webview
    independent = Mock(return_value={"ok": False, "error": "Child did not close its own session"})
    monkeypatch.setattr("nd2wsi.window_smoke.exercise_independent_window", independent)
    window.evaluate_js.return_value = {"bridge": True, "version": "test", "boot": True}
    assert app.gui_smoke(app.Api(None), window)["ok"] is True
    independent.assert_not_called()
    window.evaluate_js.return_value = {
        "bridge": True, "version": "test", "slide": True, "lut_toolbar": {"ok": True},
    }
    report = app.gui_smoke(app.Api(None), window, expect_slide=True)
    assert report["ok"] is False and report["phase"] == "independent-window"
    assert report["error"] == "Child did not close its own session"
    independent.assert_called_once()


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
    assert f"if ({str(enabled).lower()}) await step('autolut'" in script
    assert "__CLIPBOARD_CHECK__" not in script


GUI_LUT_SCRIPT = r"""
const fs=require('fs'),vm=require('vm');
const {script,mode,shadingModule}=JSON.parse(fs.readFileSync(0,'utf8'));
const Shading=require(shadingModule);
const fetches=[],calls={copy:0,paste:0};let toast='',copied=false,encoded='exact effective LUTs';
const infoCalls={load:0,open:0,close:0,toggled:[]};
let infoHidden=mode!=='info-already-open',inspectNames=null;
const associatedNames=mode==='info-empty'?[]:['thumbnail','label'];
const inspector={
  isHidden:()=>infoHidden,
  open(){infoCalls.open++;infoHidden=false;},
  close(){infoCalls.close++;infoHidden=true;},
};
const infoImages=associatedNames.map(name=>{
  let expanded=false;
  const image={complete:true,naturalWidth:160,naturalHeight:80,
    src:'http://viewer.test/private-token/s/test/api/associated/'+name+'.jpg?g=pyramid-test'};
  image.parentElement={
    getAttribute:attribute=>attribute==='aria-expanded'?String(expanded):null,
    click(){
      infoCalls.toggled.push(name);
      if(mode==='info-enlargement-missing')return;
      expanded=!expanded;
      if(expanded){image.src+='&detail=1';image.naturalWidth=1024;image.naturalHeight=512;}
    },
  };
  return image;
});
const buttons={
  'lut-type':{onclick(){}},
  'lut-copy':{disabled:false,classList:{contains:()=>copied},async onclick(){
    if(mode==='copy-timeout')return new Promise(()=>{});
    calls.copy++;copied=mode!=='copy-failure';toast=copied?'Channel LUTs copied':'Write permission denied';
  }},
  'lut-paste':{disabled:false,async onclick(){
    if(mode==='paste-timeout')return new Promise(()=>{});
    calls.paste++;toast=mode==='paste-failure'?'Read permission denied':'Pasted 2 channel LUTs';
    if(mode==='changed-values')encoded='changed LUTs';
  }},
  'lut-dialog':{},'lut-copy-tooltip':{},'toast':{get textContent(){return toast}},
};
if(mode==='missing-handler')delete buttons['lut-paste'].onclick;
const view={Nd2LutTransfer:mode==='missing-module'?null:{encode(){},planPaste(){},fitsChannel(){}},
  Nd2ShadingCorrection:mode==='missing-shading-module'?null:Shading,
  async loadSlideInspector(){infoCalls.load++;inspectNames=mode==='info-metadata-missing'?null:associatedNames;},
  parent:{pywebview:{platform:mode==='wrong-platform'?'cocoa':'edgechromium',api:{
    write_lut_clipboard:mode==='missing-native-write'?null:()=>{},
    read_lut_clipboard:mode==='missing-native-read'?null:()=>{}}}},
  navigator:{clipboard:{writeText(){},readText(){}}},
  fetch:async url=>{fetches.push(String(url));if(mode==='icons-timeout')return new Promise(()=>{});
    return {ok:mode!=='missing-icon',status:404,text:async()=>mode==='invalid-icon'?'bad':'svg'}},
  DOMParser:class{parseFromString(text){return {documentElement:{tagName:text==='svg'?'svg':'parsererror',getAttribute:()=> '0 0 20 20'},querySelector:()=>({})}}},
  eval(expression){
    if(expression.startsWith('state.shading && '))return true;
    if(expression==='state.shading.snapshot()')return mode==='legacy-shading-status'?{state:'idle',enabled:false}:
      {state:mode==='applied'?'ready':'idle',applied:mode==='applied',operation:null,can_cancel:false,generation:'pyramid-test'};
    if(expression==='state.info.generation')return 'pyramid-test';
    if(expression==='state.windows.info')return inspector;
    if(expression==='state.inspect && state.inspect.associated')return inspectNames;
    if(expression==='state.lutAutoPending.some(Boolean)')return mode==='autolut-timeout';
    if(expression.includes('planPaste'))return 2;
    return encoded;
  },
};
const page={baseURI:'http://viewer.test/private-token/s/test/',
  querySelector:selector=>selector==='#stage canvas'?{width:100,height:100}:null,
  querySelectorAll:selector=>selector==='#info-associated img'?
    (mode==='info-images-missing'?infoImages.slice(1):infoImages):[],
  getElementById:id=>buttons[id]};
const frame={dataset:{sid:'test'},contentDocument:page,contentWindow:view};
const shellControls={
  'shading-correction':mode==='missing-shading-control'?null:{title:mode==='applied'?'Shading Correction applied · Revoke…':'Apply Shading Correction'},
  'shading-progress-bar':{},
  'shading-progress-title':{},'shading-cancel':{},
  'shading-confirm':mode==='missing-shading-confirmation'?null:{showModal(){}},
  'shading-confirm-title':{},'shading-confirm-persistence':mode==='missing-shading-notice'?null:{},'shading-confirm-estimate':{},
  'shading-confirm-continue':{},'shading-confirm-cancel':{},
  'shading-methods':mode==='obsolete-shading-methods'?{}:null,
  'shading-confirm-message':mode==='obsolete-shading-explanation'?{}:null,
};
const context={window:{pywebview:{api:{update_status(){}}},__nd2wsiSmokeBridge:{version:'test'},Nd2ShadingCorrection:Shading},
  document:{readyState:'complete',querySelector:()=>frame,getElementById:id=>shellControls[id]||null},
  readyFrames:new Set(['test']),shadingStates:new Map([['test',{state:'idle'}]]),URL,
  setTimeout:(callback,delay)=>setTimeout(callback,mode.endsWith('-timeout')?Math.min(delay,10):delay),clearTimeout};
let fakeNow=0;
if(mode==='autolut-timeout')context.Date={now:()=>fakeNow+=8000};
vm.createContext(context);
(async()=>{
  const first=vm.runInContext(script,context);
  await frame.__nd2wsiSmokeLutTask;
  const result=vm.runInContext(script,context);
  process.stdout.write(JSON.stringify({first,result,fetches,calls,info:{calls:infoCalls,hidden:infoHidden,
    expanded:infoImages.map(image=>image.parentElement.getAttribute('aria-expanded'))}}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize("mode, clipboard, error", [
    ("ready", False, None), ("ready", True, None), ("applied", False, None),
    ("info-empty", False, None), ("info-already-open", False, None),
    ("info-metadata-missing", False, "Slide Info metadata did not load"),
    ("info-images-missing", False, "Slide Info omitted embedded images"),
    ("info-enlargement-missing", False, "Slide Info enlargement is unavailable"),
    ("missing-module", False, "transfer module is unavailable"),
    ("missing-handler", False, "toolbar is not wired"),
    ("missing-shading-control", False, "Shading Correction controls are unavailable"),
    ("missing-shading-module", False, "Shading Correction controls are unavailable"),
    ("missing-shading-confirmation", False, "Shading Correction controls are unavailable"),
    ("missing-shading-notice", False, "Shading Correction controls are unavailable"),
    ("obsolete-shading-methods", False, "obsolete method controls"),
    ("obsolete-shading-explanation", False, "obsolete method controls"),
    ("legacy-shading-status", False, "committed pyramid state"),
    ("missing-icon", False, "HTTP 404"), ("invalid-icon", False, "Invalid packaged LUT icon"),
    ("copy-failure", True, "copy failed: Write permission denied"),
    ("paste-failure", True, "paste failed: Read permission denied"),
    ("changed-values", True, "changed effective channel values"),
    ("wrong-platform", True, "Native Windows LUT clipboard bridge is unavailable"),
    ("missing-native-write", True, "Native Windows LUT clipboard bridge is unavailable"),
    ("missing-native-read", True, "Native Windows LUT clipboard bridge is unavailable"),
    ("autolut-timeout", True, "autolut timed out"),
    ("icons-timeout", False, "icons timed out"),
    ("copy-timeout", True, "copy timed out"),
    ("paste-timeout", True, "paste timed out"),
])
def test_real_gui_smoke_script_checks_assets_and_opt_in_clipboard(
        fake_webview, monkeypatch, mode, clipboard, error):
    if NODE is None:
        pytest.skip("node is not installed")
    _, window = fake_webview
    monkeypatch.setenv("ND2WSI_SMOKE_LUT_CLIPBOARD", "1" if clipboard else "0")
    window.evaluate_js.return_value = {
        "bridge": True, "version": "test", "slide": True, "lut_toolbar": {"ok": True},
    }
    api = app.Api(None)
    assert app.gui_smoke(api, window, expect_slide=True)["ok"] is True
    script = window.evaluate_js.call_args.args[0]
    result = subprocess.run(
        [NODE, "-e", GUI_LUT_SCRIPT], input=json.dumps({
            "script": script, "mode": mode,
            "shadingModule": str(Path(__file__).resolve().parents[1] / "nd2wsi/static/shading-correction-v1.js"),
        }),
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    out = json.loads(result.stdout)
    assert out["first"]["lut_toolbar"]["pending"] is True
    assert out["first"]["lut_toolbar"]["phase"]
    result = out["result"]["lut_toolbar"]
    if error:
        assert result["ok"] is False and error in result["error"]
        if mode.endswith("-timeout"):
            assert result["phase"] == mode.removesuffix("-timeout")
        if mode in {"wrong-platform", "missing-native-write", "missing-native-read"}:
            assert result["phase"] == "clipboard-transport"
            assert out["calls"] == {"copy": 0, "paste": 0}
        if mode in {"info-metadata-missing", "info-images-missing", "info-enlargement-missing"}:
            assert result["phase"] == "info-images"
            assert out["info"]["calls"]["load"] == 1
    else:
        assert result["ok"] is result["module"] is result["handlers"] is True
        assert result["phase"] == "complete"
        assert result["clipboard_api"] == {"write": True, "read": True}
        empty = mode == "info-empty"
        assert result["info_images"] == {
            "names": [] if empty else ["thumbnail", "label"],
            "dimensions": [] if empty else [[160, 80], [160, 80]],
            "enlarged": None if empty else [1024, 512],
        }
        assert out["info"] == {
            "calls": {"load": 1, "open": 1, "close": int(mode != "info-already-open"),
                      "toggled": [] if empty else ["label", "label"]},
            "hidden": mode != "info-already-open", "expanded": [] if empty else ["false", "false"],
        }
        assert result["shading_correction"] == {
            "state": "ready" if mode == "applied" else "idle", "applied": mode == "applied",
            "operation": None, "can_cancel": False, "generation": "pyramid-test", "toolbar": True, "confirmation": True,
            "actions": {"apply": "start", "revoke": "revoke", "unavailable": None, "cancel": "cancel", "publishing": None},
            "progress": {"visible": True, "title": "Shading Correction", "stage": "Building corrected pyramid", "pct": 63,
                         "canCancel": True, "eta": "About 2 min remaining"},
            "revoke_progress": {"visible": True, "title": "Revoke Shading Correction", "stage": "Rebuilding original pyramid", "pct": 41,
                                "canCancel": True, "eta": "About 2 min remaining"},
        }
        assert out["fetches"] == [
            "http://viewer.test/private-token/static/icons/" + name
            for name in ["fluent-edit-20-regular.svg", "fluent-copy-20-regular.svg", "fluent-clipboard-paste-20-regular.svg",
                         "fluent-checkmark-20-regular.svg", "fluent-circle-highlight-20-regular.svg"]
        ]
        if clipboard:
            assert result["clipboard_roundtrip"] == {
                "ok": True, "channels": 2, "effective_values_unchanged": True, "transport": "native-winforms",
            }
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


@pytest.fixture()
def fake_native_keys(monkeypatch):
    context = {"enabled": True, "role": "agent", "id": "fresh-agent", "pid": os.getpid()}
    api = SimpleNamespace(window_context=lambda: dict(context))
    native = SimpleNamespace(windowNumber=lambda: 47)
    window = SimpleNamespace(native=native, _js_api=api, evaluate_js=Mock())
    application = SimpleNamespace(keyWindow=Mock(return_value=native), sendEvent_=Mock())
    event_factory = Mock(side_effect=lambda *args: args)
    queue = SimpleNamespace(addOperationWithBlock_=Mock(side_effect=lambda callback: callback()))
    main_thread = Mock(return_value=False)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSApplication=SimpleNamespace(sharedApplication=lambda: application),
        NSEvent=SimpleNamespace(
            keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_=event_factory),
        NSEventTypeKeyDown=10, NSEventTypeKeyUp=11, NSEventModifierFlagCommand=1 << 20,
    ))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: queue),
        NSThread=SimpleNamespace(isMainThread=main_thread),
    ))
    monkeypatch.setattr(app.sys, "platform", "darwin")
    return SimpleNamespace(api=api, window=window, context=context, application=application,
                           event_factory=event_factory, queue=queue, main_thread=main_thread)


@pytest.mark.parametrize("key, code, command", [("a", 0, True), ("c", 8, True), ("e", 14, False), ("i", 34, True)])
@pytest.mark.parametrize("on_main_thread", [False, True])
def test_native_smoke_uses_appkit_menu_path_on_main_queue(fake_native_keys, key, code, command, on_main_thread):
    fake = fake_native_keys
    fake.main_thread.return_value = on_main_thread
    result = app._gui_smoke_appkit_key(fake.api, fake.window, fake.context, key, repeat=True)
    assert result == {"window_number": 47, "key": key, "command": command, "repeat": True}
    assert fake.queue.addOperationWithBlock_.call_count == int(not on_main_thread)
    down, up = [call.args for call in fake.event_factory.call_args_list]
    assert down[:3] == (10, (0, 0), (1 << 20) if command else 0)
    assert down[4:] == (47, None, key, key, True, code)
    assert up[0] == 11 and up[-2] is False
    assert [call.args[0] for call in fake.application.sendEvent_.call_args_list] == [down, up]


@pytest.mark.parametrize("change", ["user", "disabled", "session", "pid", "binding", "native", "key-window"])
def test_native_smoke_refuses_other_identity_or_window(fake_native_keys, change):
    fake = fake_native_keys
    expected = dict(fake.context)
    if change == "user":
        fake.context["role"] = "user"
    elif change == "disabled":
        fake.context["enabled"] = False
    elif change == "session":
        fake.context["id"] = "another-agent"
    elif change == "pid":
        fake.context["pid"] = -1
    elif change == "binding":
        fake.window._js_api = object()
    elif change == "native":
        fake.window.native = None
    else:
        fake.application.keyWindow.return_value = object()
    with pytest.raises(RuntimeError, match="Agent"):
        app._gui_smoke_appkit_key(fake.api, fake.window, expected, "a")
    fake.application.sendEvent_.assert_not_called()


def test_native_smoke_rechecks_key_window_before_key_up(fake_native_keys):
    fake = fake_native_keys
    fake.application.sendEvent_.side_effect = lambda _event: setattr(fake.application.keyWindow, "return_value", object())
    with pytest.raises(RuntimeError, match="lost its Agent key window"):
        app._gui_smoke_appkit_key(fake.api, fake.window, fake.context, "e")
    assert fake.application.sendEvent_.call_count == 1


def test_native_smoke_cancels_late_main_queue_callback(fake_native_keys, monkeypatch):
    fake = fake_native_keys
    fake.queue.addOperationWithBlock_.side_effect = None

    class ImmediateTimeout:
        def __init__(self):
            self.flag = False

        def is_set(self):
            return self.flag

        def set(self):
            self.flag = True

        def wait(self, _timeout):
            return False

    monkeypatch.setattr(app.threading, "Event", ImmediateTimeout)
    with pytest.raises(TimeoutError, match="main queue timed out"):
        app._gui_smoke_appkit_key(fake.api, fake.window, fake.context, "e")
    fake.queue.addOperationWithBlock_.call_args.args[0]()
    fake.application.sendEvent_.assert_not_called()


@pytest.mark.parametrize("condition", ["windows", "user", "no-slide"])
def test_native_keyboard_skips_unsupported_scope_without_dom_or_input(fake_native_keys, monkeypatch, condition):
    fake = fake_native_keys
    if condition == "windows":
        monkeypatch.setattr(app.sys, "platform", "win32")
    if condition == "user":
        fake.context["role"] = "user"
    result = app._gui_smoke_native_keyboard(fake.api, fake.window, slide=condition != "no-slide")
    assert result["status"] == "not_assessed"
    fake.window.evaluate_js.assert_not_called()
    fake.application.sendEvent_.assert_not_called()


def test_gui_smoke_native_failure_is_not_retried(fake_webview, monkeypatch):
    _, window = fake_webview
    window.evaluate_js.return_value = {"bridge": True, "slide": True, "lut_toolbar": {"ok": True}}
    native = Mock(return_value={"status": "failed", "error": "Native menu consumed Command-A"})
    monkeypatch.setattr(app, "_gui_smoke_native_keyboard", native)
    result = app.gui_smoke(app.Api(None), window, expect_slide=True)
    assert result["ok"] is False and result["phase"] == "native-keyboard"
    assert result["error"] == "Native menu consumed Command-A"
    native.assert_called_once()
    assert window.evaluate_js.call_count == 1
    window.destroy.assert_called_once()


def test_native_keyboard_dom_probe_observes_and_restores_agent_controls():
    if NODE is None:
        pytest.skip("node is not installed")
    harness = r"""
const fs=require('fs'),vm=require('vm'),script=JSON.parse(fs.readFileSync(0,'utf8'));
const events={},handlers={},inputs=[];
const hidden={annot:true,channels:false,region:true,info:true};
const panels=Object.fromEntries(Object.keys(hidden).map(id=>[id,{
  isHidden:()=>hidden[id],open:()=>hidden[id]=false,close:()=>hidden[id]=true,
}]));
const buttons=Object.fromEntries(['annot','channels','region'].map(id=>['tb-'+id,{
  addEventListener:(_,callback)=>handlers[id]=callback,
  removeEventListener:()=>delete handlers[id],
}]));
const focused={id:'original',isConnected:true,focus(){document.activeElement=this;}};
const toolbar={id:'newtab',focus(){document.activeElement=this;}};
const document={activeElement:focused,
  getElementById:()=>toolbar,
  querySelector:()=>({contentDocument:{getElementById:id=>buttons[id]},
    contentWindow:{eval:()=>panels,getSelection:()=>({removeAllRanges(){}})}}),
  body:{appendChild:input=>inputs.push(input)},
  createElement:()=>({style:{},value:'',focus(){document.activeElement=this;},
    setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;},remove(){inputs.pop();}}),
};
const window={getSelection:()=>({removeAllRanges(){}}),
  addEventListener:(_,callback)=>events.keydown=callback,removeEventListener:()=>delete events.keydown};
vm.runInNewContext(script,{document,window});
const probe=window.__nd2wsiNativeKeyboardSmoke;
const before=probe.prepare(false,'a');
handlers.annot();hidden.annot=false;
events.keydown({key:'a',code:'KeyA',repeat:false,isTrusted:true,target:toolbar});
const after=probe.snapshot();
panels.info.open();
const infoOpened=probe.snapshot();
panels.info.close();
const infoClosed=probe.snapshot();
const editable=probe.prepare(true,'a');
probe.cleanup();
process.stdout.write(JSON.stringify({before,after,infoOpened,infoClosed,editable,restored:hidden,focused:document.activeElement.id,
  handlers:Object.keys(handlers),inputs:inputs.length,events:Object.keys(events),probe:!!window.__nd2wsiNativeKeyboardSmoke}));
"""
    result = subprocess.run([NODE, "-e", harness], input=json.dumps(app._GUI_SMOKE_KEYBOARD_SETUP),
                            text=True, capture_output=True, check=True, timeout=10)
    out = json.loads(result.stdout)
    assert out["before"]["focused"] == "newtab"
    assert out["before"]["counts"]["annot"] == 0 and out["after"]["counts"]["annot"] == 1
    assert out["before"]["hidden"]["annot"] is True and out["after"]["hidden"]["annot"] is False
    assert out["infoOpened"]["counts"]["info"] == 1 and out["infoOpened"]["hidden"]["info"] is False
    assert out["infoClosed"]["counts"]["info"] == 2 and out["infoClosed"]["hidden"]["info"] is True
    assert out["after"]["events"] == [{"key": "a", "code": "KeyA", "repeat": False, "trusted": True, "target": "newtab"}]
    assert out["editable"]["value"] == "native smoke" and out["editable"]["selection"] == [0, 0]
    assert out["restored"] == out["before"]["hidden"] and out["focused"] == "original"
    assert out["handlers"] == out["events"] == [] and out["inputs"] == 0 and out["probe"] is False


def test_gui_smoke_timeout_preserves_last_pending_phase(fake_webview, monkeypatch):
    _, window = fake_webview
    window.evaluate_js.return_value = {
        "bridge": True, "slide": True, "phase": "paste",
        "lut_toolbar": {"pending": True, "phase": "paste", "step_timeout_ms": 10000},
    }
    ticks = iter([0, 0, 2, 2])
    monkeypatch.setattr(app.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    report = app.gui_smoke(app.Api(None), window, expect_slide=True, timeout=1)
    assert report["ok"] is False and report["phase"] == "paste"
    assert "last phase: paste" in report["error"]
    assert report["last_result"]["lut_toolbar"]["step_timeout_ms"] == 10000
    window.destroy.assert_called_once()


def test_windows_lut_clipboard_api_uses_real_pywebview_owner_not_first(monkeypatch):
    from nd2wsi import lut_clipboard

    webview = pytest.importorskip("webview")
    api = app.Api(None)
    # Constructor only: no native GUI, renderer, server, or clipboard is opened.
    owner = webview.Window("lut-owner", "LUT owner", None, js_api=api)
    other = webview.Window("lut-other", "Other window", None, js_api=app.Api(None))
    assert owner.native is None and owner.gui is None
    monkeypatch.setattr(webview, "windows", [other, owner])
    monkeypatch.setattr(app.sys, "platform", "win32")
    read = Mock(return_value={"ok": True, "text": "portable LUT"})
    write = Mock(return_value={"ok": True})
    monkeypatch.setattr(lut_clipboard, "read_lut_clipboard", read)
    monkeypatch.setattr(lut_clipboard, "write_lut_clipboard", write)
    assert api.read_lut_clipboard() == read.return_value
    assert api.write_lut_clipboard("portable LUT") == write.return_value
    read.assert_called_once_with(window=owner)
    write.assert_called_once_with("portable LUT", window=owner)


@pytest.mark.parametrize("count", [0, 2])
def test_windows_lut_clipboard_api_rejects_missing_or_ambiguous_real_owner(monkeypatch, count):
    from nd2wsi import lut_clipboard

    webview = pytest.importorskip("webview")
    api = app.Api(None)
    other = webview.Window("lut-other", "Other window", None, js_api=app.Api(None))
    owners = [webview.Window(f"lut-owner-{index}", "LUT owner", None, js_api=api) for index in range(count)]
    monkeypatch.setattr(webview, "windows", [other, *owners])
    monkeypatch.setattr(app.sys, "platform", "win32")
    read, write = Mock(), Mock()
    monkeypatch.setattr(lut_clipboard, "read_lut_clipboard", read)
    monkeypatch.setattr(lut_clipboard, "write_lut_clipboard", write)
    for result in [api.read_lut_clipboard(), api.write_lut_clipboard("portable LUT")]:
        assert result["ok"] is False and "exactly one window" in result["message"]
    read.assert_not_called()
    write.assert_not_called()


def test_macos_lut_clipboard_api_keeps_original_call_contract(fake_webview, monkeypatch):
    from nd2wsi import lut_clipboard

    monkeypatch.setattr(app.sys, "platform", "darwin")
    read, write = Mock(return_value={"ok": True, "text": "LUT"}), Mock(return_value={"ok": True})
    monkeypatch.setattr(lut_clipboard, "read_lut_clipboard", read)
    monkeypatch.setattr(lut_clipboard, "write_lut_clipboard", write)
    api = app.Api(None)
    assert api.read_lut_clipboard() == read.return_value
    assert api.write_lut_clipboard("LUT") == write.return_value
    read.assert_called_once_with()
    write.assert_called_once_with("LUT")


def test_main_passes_edge_renderer_and_writes_gui_report(fake_webview, monkeypatch, tmp_path):
    webview, window = fake_webview
    window.evaluate_js.return_value = {"bridge": True, "version": "test", "boot": True}
    webview.util = SimpleNamespace(Thread=threading.Thread)
    bridge_threads = []
    webview.start = Mock(side_effect=lambda callback, **_kwargs: (
        bridge_threads.append(webview.util.Thread), callback()))
    path = tmp_path / "gui-smoke.json"
    assert app.main(["--gui-smoke", "--smoke-report", str(path)]) == 0
    # pywebview's bridge threads are daemons before the GUI loop starts, so a
    # reply stranded by the close cannot keep the finished process alive.
    assert bridge_threads == [app._PywebviewDaemonThread]
    assert webview.start.call_args.kwargs == {"gui": "edgechromium", "private_mode": True}
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["ok"] is True and report["phase"] == "complete"
    # Without a bundled copy the default auto mode keeps the system runtime
    # and never writes pywebview's runtime setting.
    assert report["webview2_runtime"]["selection"]["kind"] == "system"
    assert report["webview2_runtime"]["required"] is False
    assert "WEBVIEW2_RUNTIME_PATH" not in webview.settings
    # The packaged smoke closes through the real WinForms veto and handshake.
    assert report["close"]["path"] == "coordinated"
    assert window.is_closed and window.close_attempts == 2
    assert any("nd2wsiPrepareForUpdate" in script for script in window.scripts)


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


def test_main_selects_the_webview2_runtime_before_starting_the_gui(fake_webview, monkeypatch, tmp_path):
    webview, window = fake_webview
    window.evaluate_js.return_value = {"bridge": True, "version": "test", "boot": True}
    order = []
    selection = {"kind": "bundled", "mode": "auto", "path": "C:\\app\\_internal\\webview2-runtime",
                 "system_version": None, "reason": "no valid system runtime; using the bundled copy"}
    select = Mock(side_effect=lambda module: (order.append("select"), SimpleNamespace(report=lambda: selection))[1])
    monkeypatch.setattr("nd2wsi.webview2_runtime.select_runtime", select)
    webview.util = SimpleNamespace(Thread=threading.Thread)
    webview.start = Mock(side_effect=lambda callback, **_kwargs: (order.append("start"), callback()))
    path = tmp_path / "gui-smoke.json"
    assert app.main(["--gui-smoke", "--smoke-report", str(path)]) == 0
    # pywebview reads the runtime folder when start imports its WinForms backend.
    assert order == ["select", "start"]
    select.assert_called_once_with(webview)
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["webview2_runtime"]["selection"] == selection
    assert report["webview2_runtime"]["main"] == {"bundled": False, "error": "fake"}


def test_main_reports_an_unusable_bundled_runtime_without_blocking_ci(fake_webview, monkeypatch, tmp_path):
    webview, _ = fake_webview
    monkeypatch.setenv("ND2WSI_WEBVIEW2_RUNTIME", "bundled")  # this source tree has no bundled copy
    webview.start = Mock()
    dialog = Mock()
    monkeypatch.setattr(app, "_startup_error", dialog)
    path = tmp_path / "gui-smoke.json"
    assert app.main(["--gui-smoke", "--smoke-report", str(path)]) == 4
    webview.start.assert_not_called()
    assert "WEBVIEW2_RUNTIME_PATH" not in webview.settings
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert "does not contain the bundled Microsoft Edge WebView2 runtime" in report["error"]
    assert "does not contain the bundled" in dialog.call_args.args[0]
    assert dialog.call_args.kwargs["show_dialog"] is False


def test_gui_smoke_fails_fast_when_webview2_cannot_start(fake_webview):
    _, window = fake_webview
    api = app.Api(None)
    api._webview2_init = {"ok": False, "error": "0x80070002 runtime files not found"}
    window.evaluate_js.return_value = SLIDE_READY
    report = app.gui_smoke(api, window, expect_slide=True)
    # A WebView2 that cannot start leaves a blank window: report it, never wait out the timeout.
    assert report["ok"] is False
    assert report["error"] == "WebView2 failed to start: 0x80070002 runtime files not found"
    assert report["webview2_runtime"]["initialization"] == api._webview2_init
    window.evaluate_js.assert_not_called()
    window.destroy.assert_called_once()


@pytest.mark.parametrize("mode, readiness", [(None, 90), ("bundled", 180)])
def test_main_gives_a_forced_bundled_smoke_a_longer_readiness_budget(fake_webview, monkeypatch, tmp_path,
                                                                    mode, readiness):
    webview, _ = fake_webview
    if mode:
        monkeypatch.setenv("ND2WSI_WEBVIEW2_RUNTIME", mode)
    selection = {"kind": "bundled" if mode else "system", "mode": mode or "auto", "path": None,
                 "system_version": None, "reason": "test"}
    monkeypatch.setattr("nd2wsi.webview2_runtime.select_runtime",
                        Mock(return_value=SimpleNamespace(report=lambda: selection)))
    seen = {}

    def fake_smoke(api, window, *, expect_slide, timeout):
        seen["timeout"] = timeout
        api._gui_smoke_report = {"ok": True}
        return api._gui_smoke_report

    monkeypatch.setattr(app, "gui_smoke", fake_smoke)
    webview.start = Mock(side_effect=lambda callback, **_kwargs: callback())
    assert app.main(["--gui-smoke", "--smoke-report", str(tmp_path / "gui-smoke.json")]) == 0
    assert seen["timeout"] == readiness


def test_main_reports_a_webview2_that_failed_to_start_once_the_blank_window_closes(fake_webview, monkeypatch):
    webview, window = fake_webview
    watched = {}

    def watch(target, record):
        watched.update(window=target, record=record)
        return True

    monkeypatch.setattr("nd2wsi.webview2_runtime.watch_initialization", watch)
    webview.start = Mock(side_effect=lambda *_args, **_kwargs: watched["record"](False, "0x8007 access denied"))
    dialog = Mock()
    monkeypatch.setattr(app, "_startup_error", dialog)
    assert app.main([]) == 4
    assert watched["window"] is window
    assert dialog.call_args.args[0] == ("Could not start nd2wsi-viewer: Microsoft Edge WebView2 (system runtime) "
                                        "could not start: 0x8007 access denied")
    assert dialog.call_args.kwargs["show_dialog"] is True


def test_main_smoke_reports_a_webview2_start_failure_in_its_own_report(fake_webview, monkeypatch, tmp_path):
    webview, window = fake_webview
    watched = {}
    monkeypatch.setattr("nd2wsi.webview2_runtime.watch_initialization",
                        lambda target, record: watched.update(record=record) or True)

    def start(callback, **_kwargs):
        watched["record"](False, "0x8007 access denied")
        callback()

    webview.start = Mock(side_effect=start)
    dialog = Mock()
    monkeypatch.setattr(app, "_startup_error", dialog)
    path = tmp_path / "gui-smoke.json"
    assert app.main(["--gui-smoke", "--smoke-report", str(path)]) == 4
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["ok"] is False and report["error"] == "WebView2 failed to start: 0x8007 access denied"
    window.evaluate_js.assert_not_called()
