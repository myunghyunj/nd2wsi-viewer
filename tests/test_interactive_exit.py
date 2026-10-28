"""A closed window ends its process even while a slide read never returns.

After the GUI loop returns, ``main()`` releases the server with the strict
teardown an update uses, which waits for every reader before it closes a
memory map. A read from a share that has gone away never finishes, so an
interactive exit waits a bounded time, records the window session closed and
lets ``run()`` end the process. Diagnostic runs and Sparkle keep the strict,
unbounded wait.
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


def stalled_api(release):
    calls = []

    def stop_server_for_update():
        calls.append(threading.current_thread())
        release.wait(30)

    return SimpleNamespace(_startup_error=None, stop_server_for_update=stop_server_for_update), calls


def test_interactive_exit_stops_waiting_for_a_teardown_that_never_finishes(monkeypatch, unfinished):
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


def test_interactive_exit_that_finishes_in_time_ends_normally(monkeypatch, unfinished):
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_TIMEOUT", 5.0)
    api = SimpleNamespace(stop_server_for_update=Mock())
    app._stop_server_at_exit(api, diagnostic=False)
    api.stop_server_for_update.assert_called_once_with()
    assert not unfinished.is_set()


def test_a_teardown_error_still_reaches_the_caller(unfinished):
    api = SimpleNamespace(stop_server_for_update=Mock(side_effect=OSError("socket already gone")))
    with pytest.raises(OSError, match="socket already gone"):
        app._stop_server_at_exit(api, diagnostic=False)
    assert not unfinished.is_set()


def test_diagnostic_exit_keeps_the_strict_unbounded_teardown(monkeypatch, unfinished):
    monkeypatch.setattr(app, "_EXIT_TEARDOWN_TIMEOUT", 0.01)
    caller = threading.current_thread()
    seen = []

    def stop_server_for_update():
        time.sleep(0.3)  # longer than the interactive bound
        seen.append(threading.current_thread())

    started = time.monotonic()
    app._stop_server_at_exit(SimpleNamespace(stop_server_for_update=stop_server_for_update), diagnostic=True)
    assert time.monotonic() - started >= 0.3
    assert seen == [caller]
    assert not unfinished.is_set()


def run_native_main(monkeypatch, tmp_path, api, argv):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path))
    create = Mock(return_value=(api, object()))
    monkeypatch.setattr(app, "create_app_window", create)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(start=Mock()))
    result = app.main(argv)
    return result, create.call_args.kwargs["window_session"]


def test_interactive_main_records_the_session_closed_although_a_read_stalls(monkeypatch, tmp_path, unfinished):
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


def test_gui_smoke_main_waits_for_the_whole_teardown_on_its_own_thread(monkeypatch, tmp_path, unfinished, capsys):
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


@pytest.mark.parametrize("left_unfinished", [True, False])
def test_run_ends_an_interactive_process_only_when_its_teardown_was_left_running(
        monkeypatch, unfinished, left_unfinished):
    def main(arguments):
        if left_unfinished:
            unfinished.set()
        return 0

    exit_now = Mock()
    monkeypatch.setattr(app, "main", main)
    monkeypatch.setattr(app.os, "_exit", exit_now)
    assert app.run(["slide.nd2"]) == 0
    if left_unfinished:
        exit_now.assert_called_once_with(0)
    else:
        exit_now.assert_not_called()


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
