"""A closed app process exits even while a pywebview bridge reply is stranded.

pywebview runs every JS-to-Python call on a new thread whose reply goes back
through ``window.evaluate_js``, which waits without a deadline (Cocoa for a
main-queue block, WinForms for an ExecuteScriptAsync continuation). A reply
that races the window's close never returns. These tests strand one on
purpose, with the real ``webview.util``, and check the process still exits.
"""

import os
import runpy
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from nd2wsi import app

ROOT = Path(__file__).resolve().parents[1]

STRANDED_BRIDGE_CALL = textwrap.dedent("""
    import sys
    import threading

    import webview
    from webview import util

    from nd2wsi import app

    if sys.argv[1] == "daemonize":
        app._daemonize_pywebview_bridge_threads(webview)


    class ClosedWindow:
        # The reply to a bridge call made just before the window closed.
        _functions = {"set_native_gesture_scopes": lambda payload: {"ok": True}}
        _js_api = None

        def evaluate_js(self, script):
            threading.Event().wait()  # the lost continuation never arrives


    util.js_bridge_call(ClosedWindow(), "set_native_gesture_scopes", [{"scopes": []}], "reply-1")
    print("report written", flush=True)
""")


def stranded_process(mode):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    return subprocess.Popen([sys.executable, "-c", STRANDED_BRIDGE_CALL, mode], env=env, cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")


def test_a_bridge_reply_stranded_at_close_no_longer_keeps_the_process_alive():
    pytest.importorskip("webview.util")
    process = stranded_process("daemonize")
    try:
        stdout, stderr = process.communicate(timeout=60)
    finally:
        process.kill()
    assert process.returncode == 0, stderr
    assert stdout.strip() == "report written"


def test_without_daemon_bridge_threads_the_same_process_lingers_after_its_report():
    # The defect the fix removes: pywebview's own non-daemon thread is joined
    # at interpreter shutdown, so the finished process never exits.
    pytest.importorskip("webview.util")
    process = stranded_process("stock")
    try:
        assert process.stdout.readline().strip() == "report written"
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=2)
    finally:
        process.kill()
        process.communicate()


def test_installed_bridge_calls_start_daemon_threads(monkeypatch):
    webview = pytest.importorskip("webview")
    from webview import util

    monkeypatch.setattr(util, "Thread", util.Thread)  # restored after the test
    app._daemonize_pywebview_bridge_threads(webview)
    assert util.Thread is app._PywebviewDaemonThread
    released, entered = threading.Event(), threading.Event()

    class Window:
        _functions = {"ping": lambda: "pong"}
        _js_api = None

        def evaluate_js(self, script):
            entered.set()
            released.wait(5)

    before = set(threading.enumerate())
    util.js_bridge_call(Window(), "ping", [], "reply-1")
    try:
        assert entered.wait(5)
        calls = [thread for thread in threading.enumerate() if thread not in before]
        assert calls and all(thread.daemon for thread in calls)
        assert any(thread.name.endswith("(_call)") for thread in calls)
    finally:
        released.set()
    assert app._PywebviewDaemonThread(target=lambda: None, daemon=False).daemon is True


def test_daemonizing_leaves_a_webview_without_util_unchanged():
    fake = Mock(spec=[])
    app._daemonize_pywebview_bridge_threads(fake)  # no util attribute: nothing to do
    app._daemonize_pywebview_bridge_threads(type("W", (), {"util": object()})())


def test_run_ends_a_diagnostic_process_although_a_thread_still_waits():
    script = textwrap.dedent("""
        import sys
        import threading

        from nd2wsi import app

        def main(arguments):
            threading.Thread(target=threading.Event().wait, name="stranded").start()  # non-daemon
            print("report written")  # block-buffered on a pipe: run() must flush it
            return 4

        app.main = main
        sys.exit(app.run(["--gui-smoke", "--smoke-report", "gui-smoke.json"]))
    """)
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=str(ROOT), capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 4, result.stderr
    assert result.stdout.strip() == "report written"
    assert time.monotonic() - started < 60


@pytest.mark.parametrize("argv,diagnostic", [
    ([], False), (["sample.nd2"], False), (["--new-window"], False),
    (["--gui-smoke", "sample.nd2"], True), (["--handoff-check-report=report.json"], True),
    (["--browser-replay-report", "replay.json"], True),
])
def test_run_returns_interactive_results_and_exits_diagnostic_ones(monkeypatch, argv, diagnostic):
    main = Mock(return_value=3)
    exit_now = Mock()
    monkeypatch.setattr(app, "main", main)
    monkeypatch.setattr(app.os, "_exit", exit_now)
    assert app.run(argv) == 3
    main.assert_called_once_with(argv)
    if diagnostic:
        exit_now.assert_called_once_with(3)
    else:
        exit_now.assert_not_called()


def test_packaged_launcher_enters_through_run(monkeypatch):
    launcher = ROOT / "packaging" / "launch.py"
    run = Mock(return_value=0)
    monkeypatch.setattr(app, "run", run)
    monkeypatch.setattr(app, "main", Mock(side_effect=AssertionError("the launcher must enter through run()")))
    monkeypatch.setattr(sys, "argv", ["nd2wsi-viewer", "--gui-smoke"])
    monkeypatch.setattr(sys, "platform", "darwin")  # keep the test's own streams
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(launcher), run_name="__main__")
    assert stopped.value.code == 0
    run.assert_called_once_with()
