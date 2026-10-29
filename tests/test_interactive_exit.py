"""A closed window ends its process even while a slide read never returns.

After the GUI loop returns, ``main()`` releases the server with the strict
teardown an update uses, which waits for every reader before it closes a
memory map. A read from a share that has gone away never finishes, so an
interactive exit waits a bounded time, records the window session closed and
lets ``run()`` end the process. Diagnostic runs, Sparkle and a direct caller
of ``main()``, which nothing would end, keep the strict, unbounded wait.
"""

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image

from nd2wsi import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def unfinished(monkeypatch):
    flag = threading.Event()
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_UNFINISHED", flag)
    return flag


@pytest.fixture
def hard_exit_follows(monkeypatch):
    flag = threading.Event()
    monkeypatch.setattr(app, "_HARD_EXIT_FOLLOWS", flag)
    return flag


@pytest.fixture
def under_run(hard_exit_follows):
    """``main()`` is running inside ``run()``, which ends the process after it."""
    hard_exit_follows.set()
    return hard_exit_follows


def stalled_api(release):
    calls = []

    def stop_server_for_update():
        calls.append(threading.current_thread())
        release.wait(30)

    return SimpleNamespace(_startup_error=None, stop_server_for_update=stop_server_for_update), calls


def test_interactive_exit_stops_waiting_for_a_teardown_that_never_finishes(monkeypatch, unfinished, under_run):
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_TIMEOUT", 0.2)
    release = threading.Event()
    api, calls = stalled_api(release)
    try:
        started = time.monotonic()
        app._stop_server_at_exit(api, diagnostic=False)
        assert time.monotonic() - started < 10
        assert unfinished.is_set()
        # The strict teardown is left running, never forced: it still owns
        # the decision to close a memory map once its reader lets go.
        assert len(calls) == 1 and calls[0].daemon and calls[0].is_alive()
        assert calls[0].name == "exit-teardown"
    finally:
        release.set()
    calls[0].join(5)
    assert not calls[0].is_alive()


def test_interactive_exit_that_finishes_in_time_ends_normally(monkeypatch, unfinished, under_run):
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_TIMEOUT", 5.0)
    api = SimpleNamespace(stop_server_for_update=Mock())
    app._stop_server_at_exit(api, diagnostic=False)
    api.stop_server_for_update.assert_called_once_with()
    assert not unfinished.is_set()


def test_a_teardown_error_still_reaches_the_caller(unfinished, under_run):
    api = SimpleNamespace(stop_server_for_update=Mock(side_effect=OSError("socket already gone")))
    with pytest.raises(OSError, match="socket already gone"):
        app._stop_server_at_exit(api, diagnostic=False)
    assert not unfinished.is_set()


@pytest.mark.parametrize("diagnostic", [True, False], ids=["diagnostic", "direct-main"])
def test_exits_nothing_hard_ends_keep_the_strict_unbounded_teardown(monkeypatch, unfinished, hard_exit_follows,
                                                                    diagnostic):
    # A diagnostic run waits without limit so a hung teardown fails its CI
    # step; a direct caller of main() has no run() to end the process after
    # a bound, so a session recorded closed would outlive it.
    if diagnostic:
        hard_exit_follows.set()
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_TIMEOUT", 0.01)
    caller = threading.current_thread()
    seen = []

    def stop_server_for_update():
        time.sleep(0.3)  # longer than the interactive bound
        seen.append(threading.current_thread())

    app._stop_server_at_exit(SimpleNamespace(stop_server_for_update=stop_server_for_update), diagnostic=diagnostic)
    # Appended after the sleep, on the caller's own thread: the whole
    # teardown ran synchronously and finished before the call returned.
    assert seen == [caller]
    assert not unfinished.is_set()


def run_native_main(monkeypatch, tmp_path, api, argv):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path))
    # main() sets attributes on the window, as on pywebview's
    create = Mock(return_value=(api, SimpleNamespace()))
    monkeypatch.setattr(app, "create_app_window", create)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(start=Mock()))
    result = app.main(argv)
    return result, create.call_args.kwargs["window_session"]


def test_interactive_main_records_the_session_closed_although_a_read_stalls(monkeypatch, tmp_path, unfinished,
                                                                            under_run):
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_TIMEOUT", 0.2)
    release = threading.Event()
    api, calls = stalled_api(release)
    try:
        started = time.monotonic()
        result, session = run_native_main(monkeypatch, tmp_path, api, ["--agent-window"])
        assert time.monotonic() - started < 10
        assert result == 0
        record = json.loads((session.root / "session.json").read_text(encoding="utf-8"))
        assert record["state"] == "closed"
        assert unfinished.is_set() and len(calls) == 1
    finally:
        release.set()


def test_main_called_directly_keeps_the_session_open_until_its_teardown_ends(monkeypatch, tmp_path, unfinished,
                                                                               hard_exit_follows):
    # The nd2wsi-viewer console script and desktop.py call main() itself, and
    # nothing ends their process after it: interpreter shutdown would still
    # join the stalled read. The session must not read "closed" meanwhile.
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_TIMEOUT", 0.05)
    release = threading.Event()
    api, calls = stalled_api(release)
    outcome = {}

    def call_main():
        outcome["result"], outcome["session"] = run_native_main(monkeypatch, tmp_path, api, ["--agent-window"])

    caller = threading.Thread(target=call_main, daemon=True)
    try:
        caller.start()
        deadline = time.monotonic() + 30
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == [caller]
        caller.join(0.5)  # ten times the interactive bound
        assert caller.is_alive()
        records = [json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.glob("*/session.json")]
        assert len(records) == 1 and records[0]["state"] != "closed"
    finally:
        release.set()
    caller.join(30)
    assert not caller.is_alive()
    assert outcome["result"] == 0
    assert json.loads((outcome["session"].root / "session.json").read_text(encoding="utf-8"))["state"] == "closed"
    assert not unfinished.is_set()


def test_gui_smoke_main_waits_for_the_whole_teardown_on_its_own_thread(monkeypatch, tmp_path, unfinished, under_run,
                                                                       capsys):
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_TIMEOUT", 0.01)
    caller = threading.current_thread()
    seen = []

    def stop_server_for_update():
        time.sleep(0.2)
        seen.append(threading.current_thread())

    api = SimpleNamespace(_startup_error=None, stop_server_for_update=stop_server_for_update)
    result, session = run_native_main(monkeypatch, tmp_path, api, ["--gui-smoke"])
    assert result == 4  # no smoke report: the fake GUI loop never ran it
    assert seen == [caller]
    assert not unfinished.is_set()
    assert json.loads((session.root / "session.json").read_text(encoding="utf-8"))["state"] == "closed"


def failing_teardown_api():
    def stop_server_for_update():
        raise OSError("plate writer lease already gone")

    return SimpleNamespace(_startup_error=None, stop_server_for_update=stop_server_for_update)


def test_a_failed_teardown_still_writes_the_smoke_report_and_fails_it(monkeypatch, tmp_path, unfinished, under_run,
                                                                        capsys):
    monkeypatch.setattr(app, "_dlog", Mock())
    api = failing_teardown_api()
    api._gui_smoke_report = {"ok": True, "checks": ["window shown"]}  # the GUI checks passed
    path = tmp_path / "report" / "smoke.json"
    result, session = run_native_main(monkeypatch, tmp_path / "sessions", api,
                                      ["--gui-smoke", "--smoke-report", str(path)])
    assert result == 4
    report = json.loads(path.read_text(encoding="utf-8"))
    failed = "Server teardown failed: OSError: plate writer lease already gone"
    assert report == {"ok": False, "checks": ["window shown"], "teardown_error": failed, "error": failed}
    assert json.loads(capsys.readouterr().out) == report
    assert json.loads((session.root / "session.json").read_text(encoding="utf-8"))["state"] == "closed"
    app._dlog.assert_any_call("exit: server teardown failed: OSError('plate writer lease already gone')")


def test_a_failed_teardown_after_a_finished_browser_replay_fails_the_run_visibly(monkeypatch, tmp_path, unfinished,
                                                                                 under_run, capsys):
    # The replay wrote its own report before the window closed; the run
    # must still fail, with the reason on stderr for the capturing script.
    monkeypatch.setattr(app, "_dlog", Mock())
    root = tmp_path / "root"
    root.mkdir()
    (root / "slide.nd2").write_bytes(b"")
    report = tmp_path / "replay.json"
    api = failing_teardown_api()
    api._browser_replay_report = {"ok": True, "samples": [1, 2]}
    result, _ = run_native_main(monkeypatch, tmp_path / "sessions", api, [
        "--agent-window", "--browser-replay-report", str(report), "--benchmark-context", str(tmp_path / "ctx.json"),
        "--benchmark-test-root", str(root), str(root / "slide.nd2")])
    # The replay ran and passed, so only the teardown fails the run.
    assert sys.modules["webview"].start.call_count == 1
    assert result == 4
    assert capsys.readouterr().err.splitlines() == ["Server teardown failed: OSError: plate writer lease already gone"]
    assert not report.exists()  # the replay's own file is left as it wrote it


def test_a_failed_teardown_after_an_interactive_close_is_logged_not_raised(monkeypatch, tmp_path, unfinished,
                                                                            under_run):
    monkeypatch.setattr(app, "_dlog", Mock())
    result, session = run_native_main(monkeypatch, tmp_path, failing_teardown_api(), ["--agent-window"])
    assert result == 0  # main() returns, so run() still decides how the process ends
    assert json.loads((session.root / "session.json").read_text(encoding="utf-8"))["state"] == "closed"
    app._dlog.assert_any_call("exit: server teardown failed: OSError('plate writer lease already gone')")


@pytest.mark.parametrize("left_unfinished", [True, False])
def test_run_ends_an_interactive_process_only_when_its_teardown_was_left_running(
        monkeypatch, unfinished, hard_exit_follows, left_unfinished):
    bounded = []

    def main(arguments):
        bounded.append(hard_exit_follows.is_set())
        if left_unfinished:
            unfinished.set()
        return 0

    exit_now = Mock()
    monkeypatch.setattr(app, "main", main)
    monkeypatch.setattr(app.os, "_exit", exit_now)
    assert app.run(["slide.nd2"]) == 0
    # Only main() running inside run() may leave a teardown behind, and the
    # permission ends with it.
    assert bounded == [True]
    assert not hard_exit_follows.is_set()
    if left_unfinished:
        exit_now.assert_called_once_with(0)
    else:
        exit_now.assert_not_called()


def test_run_withdraws_the_bound_when_main_raises(monkeypatch, hard_exit_follows):
    monkeypatch.setattr(app, "main", Mock(side_effect=RuntimeError("no display")))
    with pytest.raises(RuntimeError, match="no display"):
        app.run([])
    assert not hard_exit_follows.is_set()


# The real entry point, Api, HTTP server and registry, with only the GUI loop
# replaced. The tile read stalls inside an executor worker, where zarr runs a
# store read from a share that has gone away: interpreter shutdown joins those
# workers, so it would wait forever and the process must end without it.
CLOSE_WITH_A_READ_IN_FLIGHT = textwrap.dedent("""
    import atexit
    import concurrent.futures
    import sys
    import threading
    import types
    import urllib.request
    from pathlib import Path

    from nd2wsi import app, render

    source, mode = Path(sys.argv[1]), sys.argv[2]
    app._EXIT_TEARDOWN_TIMEOUT = 1.0
    atexit.register(lambda: print("interpreter shutdown ran", flush=True))
    entered = threading.Event()
    render_tile = render.render_tile

    def stalling_render_tile(*args, **kwargs):
        if mode == "stall":
            entered.set()
            concurrent.futures.ThreadPoolExecutor(1).submit(threading.Event().wait).result()
        return render_tile(*args, **kwargs)

    render.render_tile = stalling_render_tile
    bridge = {}

    def create_app_window(initial, window_session=None):
        bridge["api"] = app.Api(initial, window_session=window_session)
        return bridge["api"], object()

    def start(**kwargs):
        # The window shows the slide, a tile read starts, the user closes it.
        api = bridge["api"]
        url = api.open_path(str(source))
        assert url, api._status
        sid = next(iter(api._httpd.registry.slides))
        tile = f"{url}s/{sid}/api/tile/0/0/0.jpg"
        if mode == "stall":
            threading.Thread(target=lambda: urllib.request.urlopen(tile, timeout=600), daemon=True).start()
            assert entered.wait(30)
        else:
            urllib.request.urlopen(tile, timeout=30).read()
        print("window closed", flush=True)

    app.create_app_window = create_app_window
    sys.modules["webview"] = types.SimpleNamespace(start=start)
    sys.exit(app.run([]))
""")


@pytest.mark.parametrize("mode", ["stall", "clean"])
def test_closing_the_window_ends_the_process_although_a_read_never_returns(tmp_path, mode):
    source = tmp_path / "slide.png"
    pixels = np.zeros((96, 128, 3), np.uint8)
    pixels[..., 0] = np.arange(128, dtype=np.uint8)
    Image.fromarray(pixels).save(source)
    sessions = tmp_path / "sessions"
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", ND2WSI_DISABLE_UPDATES="1",
               ND2WSI_WINDOW_SESSION_ROOT=str(sessions))
    process = subprocess.Popen([sys.executable, "-c", CLOSE_WITH_A_READ_IN_FLIGHT, str(source), mode],
                               env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
    try:
        stdout, stderr = process.communicate(timeout=90)
    finally:
        process.kill()
        process.communicate()
    assert process.returncode == 0, stderr
    lines = stdout.splitlines()
    assert lines[0] == "window closed"
    if mode == "stall":
        # Ended by run() once main() had closed the session: no atexit hooks.
        assert lines == ["window closed"]
    else:
        # A teardown that finishes keeps the ordinary interpreter shutdown.
        assert lines == ["window closed", "interpreter shutdown ran"]
    if sys.platform in ("darwin", "win32"):
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sessions.glob("*/session.json")]
        assert [record["state"] for record in records] == ["closed"]
