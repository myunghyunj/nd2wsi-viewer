"""Headless contracts for independent macOS windows and safe native close."""

import os
import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app, window_launch
from nd2wsi.window_sessions import create_window_session


@pytest.mark.parametrize("role,flag", [("user", "--new-window"), ("agent", "--agent-window")])
def test_frozen_child_forwards_only_role(role, flag, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/Beta.app/Contents/MacOS/beta", "slide.nd2", "--gui-smoke"])
    assert window_launch.build_window_command(role, executable="/Beta.app/Contents/MacOS/beta", frozen=True) == [
        "/Beta.app/Contents/MacOS/beta", flag,
    ]


def test_source_child_module_and_explicit_beta_launcher(monkeypatch, tmp_path):
    monkeypatch.delenv("ND2WSI_WINDOW_LAUNCHER", raising=False)
    assert window_launch.build_window_command(executable="/venv/bin/python", frozen=False) == [
        "/venv/bin/python", "-m", "nd2wsi.app", "--agent-window",
    ]
    launcher = tmp_path / "Metal Beta launcher.py"
    monkeypatch.setenv("ND2WSI_WINDOW_LAUNCHER", str(launcher))
    assert window_launch.build_window_command("user", executable="python", frozen=False) == [
        "python", str(launcher), "--new-window",
    ]
    with pytest.raises(ValueError, match="role"):
        window_launch.build_window_command("shared")


def test_child_environment_resets_pyinstaller_without_mutating_parent(monkeypatch):
    monkeypatch.setenv("PYINSTALLER_RESET_ENVIRONMENT", "0")
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "1")
    monkeypatch.delenv("ND2WSI_WINDOW_CHILD", raising=False)
    environment = window_launch.child_environment(app_name="Metal Beta")
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert environment["ND2WSI_WINDOW_CHILD"] == "1"
    assert environment["ND2WSI_APP_NAME"] == "Metal Beta"
    assert environment["ND2WSI_GPU_PYRAMID"] == "1"
    assert os.environ["PYINSTALLER_RESET_ENVIRONMENT"] == "0"
    assert "ND2WSI_WINDOW_CHILD" not in os.environ


@pytest.mark.parametrize("frozen", [False, True])
def test_launch_detaches_child_and_does_not_reuse_parent_io(monkeypatch, frozen):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.delenv("ND2WSI_WINDOW_LAUNCHER", raising=False)
    child = Mock(pid=4321)
    popen = Mock(return_value=child)
    monkeypatch.setattr(window_launch.subprocess, "Popen", popen)
    assert window_launch.launch_window(app_name="Beta") == {"ok": True, "pid": 4321, "role": "agent"}
    options = popen.call_args.kwargs
    assert options["start_new_session"] is True and options["close_fds"] is True
    assert all(options[key] == window_launch.subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert options["env"]["ND2WSI_APP_NAME"] == "Beta"
    if not frozen:
        assert options["env"]["PYTHONPATH"].split(os.pathsep)[0] == options["cwd"]
    else:
        assert "cwd" not in options
    child.wait.assert_not_called()
    child.terminate.assert_not_called()


def test_launch_is_platform_gated_and_errors_are_reported(monkeypatch):
    popen = Mock(side_effect=OSError("launch blocked"))
    monkeypatch.setattr(window_launch.subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "platform", "win32")
    assert window_launch.launch_window()["ok"] is False
    popen.assert_not_called()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert "launch blocked" in window_launch.launch_window()["message"]


def test_api_default_agent_and_scoped_context(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    session = create_window_session("agent", tmp_path)
    api = app.Api(None, window_session=session)
    launch = Mock(return_value={"ok": True})
    monkeypatch.setattr(window_launch, "launch_window", launch)
    assert api.new_window()["ok"] is True
    launch.assert_called_once_with("agent", app_name=app.APP_NAME)
    context = api.window_context()
    assert context["role"] == "agent" and context["id"] == session.id
    assert context["exports_root"] == str(session.exports_root)
    assert "NEW Agent window" in context["agent_directive"]
    assert api.update_status()["mode"] == "manual-download"
    check = Mock(return_value={"ok": True, "message": "Close every window before installing."})
    monkeypatch.setattr("nd2wsi.release_selection.check_for_updates", check)
    assert "every window" in api.check_for_updates()["message"]
    check.assert_called_once_with()


def test_api_server_is_bound_to_its_session(monkeypatch, tmp_path):
    session = create_window_session("user", tmp_path)
    server = SimpleNamespace(registry=SimpleNamespace(add_store=Mock()))
    start = Mock(return_value=(server, "http://127.0.0.1:8123"))
    monkeypatch.setattr(app, "open_or_convert", lambda path, **_: path)
    monkeypatch.setattr(app, "start_server", start)
    api = app.Api(None, window_session=session)
    assert api._launch(Path("sample.nd2")) == "http://127.0.0.1:8123"
    start.assert_called_once_with(Path("sample.nd2"), window_session=session)
    start.reset_mock()
    second = app.Api(None, window_session=session)
    second._launch_many([Path("sample.nd2")])
    start.assert_called_once_with(Path("sample.nd2"), window_session=session)


@pytest.mark.parametrize("many", [False, True])
def test_agent_open_uses_policy_registry_before_any_cache_conversion(monkeypatch, tmp_path, many):
    session = create_window_session("agent", tmp_path)
    registry = SimpleNamespace(open_path=Mock(), add_store=Mock())
    server = SimpleNamespace(registry=registry)
    start = Mock(return_value=(server, "http://127.0.0.1:8123"))
    convert = Mock(side_effect=AssertionError("Agent must not build or repair shared cache"))
    monkeypatch.setattr(app, "open_or_convert", convert)
    monkeypatch.setattr(app, "start_server", start)
    monkeypatch.setattr(app, "_server_url", lambda _: "http://127.0.0.1:8123")
    api = app.Api(None, window_session=session)
    if many:
        result = api._launch_many([Path("sample.nd2"), Path("sample2.nd2")])
        assert registry.open_path.call_count == 2
    else:
        result = api._launch(Path("sample.nd2"))
        assert registry.open_path.call_count == 1
    assert result == "http://127.0.0.1:8123"
    start.assert_called_once_with([], window_session=session)
    assert registry.open_path.call_args.args[0].suffix == ".nd2"
    convert.assert_not_called()
    registry.add_store.assert_not_called()


def test_agent_cache_refusal_keeps_bootstrap_without_conversion(monkeypatch, tmp_path):
    session = create_window_session("agent", tmp_path)
    server = SimpleNamespace(registry=SimpleNamespace(open_path=Mock(side_effect=PermissionError("shared cache unavailable"))))
    monkeypatch.setattr(app, "start_server", Mock(return_value=(server, "http://127.0.0.1:8123")))
    convert = Mock(side_effect=AssertionError("no conversion"))
    monkeypatch.setattr(app, "open_or_convert", convert)
    api = app.Api(None, window_session=session)
    assert api._launch(Path("sample.nd2")) is None
    assert "shared cache unavailable" in api._status
    assert api._httpd is server
    convert.assert_not_called()


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self


def test_mac_window_title_and_close_binding(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    session = create_window_session("agent", tmp_path)
    window = SimpleNamespace(events=SimpleNamespace(**{name: Event() for name in (
        "before_load", "closed", "loaded", "shown", "closing", "initialized",
    )}))
    webview = SimpleNamespace(create_window=Mock(return_value=window), settings={})
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr(app, "_wire_file_drop", Mock())
    monkeypatch.setattr(app, "_inline_traffic_lights", Mock())
    monkeypatch.setattr(app, "_install_open_files_handler", Mock())
    monkeypatch.setattr("nd2wsi.native_gestures.wire_native_trackpad_bridge", Mock())
    menu = Mock()
    monkeypatch.setattr(app, "_install_window_menu", menu)
    api, actual = app.create_app_window(None, window_session=session)
    assert actual is window
    assert "Agent" in webview.create_window.call_args.args[0]
    assert session.id in webview.create_window.call_args.args[0]
    assert window.events.closing.handlers == [api._close_coordinator.on_closing]
    for callback in window.events.shown.handlers:
        callback()
    menu.assert_called_once_with(api, window)


@pytest.mark.parametrize("argv,role", [([], "user"), (["--new-window"], "user"), (["--agent-window"], "agent")])
def test_mac_main_creates_fresh_session_private_webview_and_marks_closed(monkeypatch, tmp_path, argv, role):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path))
    api = SimpleNamespace(_startup_error=None, stop_server_for_update=Mock())
    create = Mock(return_value=(api, object()))
    monkeypatch.setattr(app, "create_app_window", create)
    webview = SimpleNamespace(start=Mock())
    monkeypatch.setitem(sys.modules, "webview", webview)
    assert app.main(argv) == 0
    session = create.call_args.kwargs["window_session"]
    assert session.role == role
    assert '"state": "closed"' in (session.root / "session.json").read_text()
    webview.start.assert_called_once_with(private_mode=True)
    api.stop_server_for_update.assert_called_once()


class MainQueue:
    def __init__(self):
        self.owner = threading.current_thread()
        self.pending = queue.Queue()

    def addOperationWithBlock_(self, callback):
        self.pending.put(callback)

    def until(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, "close worker timed out"
            try:
                self.pending.get(timeout=0.02)()
            except queue.Empty:
                pass


@pytest.fixture()
def close_fixture(monkeypatch):
    main = MainQueue()
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: main),
    ))
    calls = []
    replies = [{"ok": True, "panes": 1}]

    def evaluate(script, callback=None):
        assert threading.current_thread() is not main.owner, "browser call would deadlock Cocoa"
        calls.append(script)
        if callback is not None and replies:
            main.pending.put(lambda: callback(replies[0]))

    window = SimpleNamespace(evaluate_js=evaluate, destroy=Mock())
    api = SimpleNamespace(_httpd=object(), _frac=-1, _status="", update_block_reason=Mock(return_value=None))
    guard = app.WindowCloseCoordinator(api, window)
    guard._FLUSH_TIMEOUT = 0.05
    guard._DRAIN_TIMEOUT = 0.05
    guard._DRAIN_POLL = 0.005
    monkeypatch.setattr(app, "_dlog", Mock())
    return main, calls, replies, window, api, guard


def test_native_close_vetoes_then_flushes_before_destroy(close_fixture):
    main, calls, _, window, _, guard = close_fixture
    assert guard.on_closing() is False
    assert guard.on_closing() is False
    main.until(lambda: not guard._running)
    window.destroy.assert_called_once()
    assert guard.on_closing() is True
    assert "nd2wsiPrepareForUpdate" in calls[0]
    assert not any("nd2wsiCancelUpdate" in call for call in calls)


@pytest.mark.parametrize("failure", ["save", "timeout", "export", "destroy"])
def test_native_close_failure_preserves_window_and_recovers_editing(close_fixture, failure):
    main, calls, replies, window, api, guard = close_fixture
    if failure == "save":
        replies[:] = [{"ok": False, "error": "annotation conflict: draft preserved"}]
    elif failure == "timeout":
        replies.clear()
    elif failure == "export":
        api.update_block_reason.return_value = "Waiting for 1 export to finish…"
    elif failure == "destroy":
        window.destroy.side_effect = RuntimeError("native close failed")
    assert guard.on_closing() is False
    main.until(lambda: not guard._running)
    assert guard._approved is False
    if failure != "destroy":
        window.destroy.assert_not_called()
    assert "Window remains open" in api._status
    assert any("nd2wsiCancelUpdate" in call for call in calls)


def test_close_missing_bridge_is_allowed_only_for_empty_bootstrap(close_fixture):
    _, _, _, _, api, guard = close_fixture
    assert "ok:false" in guard._missing_flush_result()
    api._httpd = None
    assert "ok:true" in guard._missing_flush_result()
    api._httpd = SimpleNamespace(registry=SimpleNamespace(slides={}))
    assert "ok:true" in guard._missing_flush_result()
    api._httpd.registry.slides["loading"] = object()
    assert "ok:false" in guard._missing_flush_result()
    api._frac = 0.2
    assert "ok:false" in guard._missing_flush_result()
