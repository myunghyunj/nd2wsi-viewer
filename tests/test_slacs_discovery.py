"""SLACS identity comes from process metadata, never from running legacy code."""

import json
import ntpath
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from nd2wsi import slacs_discovery as discovery

SCRIPT = r"C:\Python27\Scripts\SniperCellSorterGUIv3.1.py"
BIRTH = "2026-10-27T02:00:00.1234567Z"


def process(command=None, **changes):
    row = {"pid": 123, "created": BIRTH, "name": "python.exe",
           "command_line": command or f'"C:\\Python27\\python.exe" "{SCRIPT}"'}
    row.update(changes)
    return row


@pytest.mark.parametrize("command, expected", [
    (f'python.exe "{SCRIPT}"', SCRIPT),
    (f'"C:\\Program Files\\Python\\pythonw.exe" -u -OO "{SCRIPT}" user', SCRIPT),
    (f'python.exe -Buv -W ignore -Qwarnall "{SCRIPT}"', SCRIPT),
    (f'python.exe -X utf8 --check-hash-based-pycs always -- "{SCRIPT}"', SCRIPT),
    (f'pythonw --check-hash-based-pycs=never -Wignore "{SCRIPT}"', SCRIPT),
    (f'python.exe -c "print(1)" "{SCRIPT}"', None),
    (f'python.exe -cprint(1) "{SCRIPT}"', None),
    (f'python.exe -m other "{SCRIPT}"', None),
    (f'python.exe -Imother "{SCRIPT}"', None),
    (f'python.exe other.py "{SCRIPT}"', "other.py"),
    (f'python.exe -W "{SCRIPT}"', None),
    (f'python.exe -X "{SCRIPT}"', None),
    (f'python.exe --help "{SCRIPT}"', None),
    (f'python.exe -V "{SCRIPT}"', None),
    (f'python.exe -z "{SCRIPT}"', None),
    (f'python.exe - "{SCRIPT}"', None),
    (f'cmd.exe /c python.exe "{SCRIPT}"', None),
    ("python.exe", None),
])
def test_only_the_python_script_operand_counts(command, expected):
    assert discovery._script_argument(command) == expected


@pytest.mark.parametrize("arguments", [
    [r"C:\Program Files\Python\python.exe", r"C:\Some Folder\SniperCellSorterGUIv3.1.py"],
    ["python.exe", "-W", 'ignore:a "quoted" warning', SCRIPT, ""],
    ["pythonw.exe", SCRIPT, "backslashes\\\\", 'ends in slash \\'],
])
def test_windows_quoting_round_trips_without_a_shell(arguments):
    assert discovery._split_windows_command_line(subprocess.list2cmdline(arguments)) == arguments


@pytest.mark.parametrize("rows, status", [
    ([], "not_running"),
    ([process(f'python.exe other.py "{SCRIPT}"')], "not_running"),
    ([process(f'python.exe "{SCRIPT}."')], "not_running"),
    ([process(f'python.exe "{SCRIPT}.bak"')], "not_running"),
    ([process(name="unrelated.exe")], "not_running"),
    ([process(), process(pid=124)], "ambiguous"),
    ([process(command_line=None)], "error"),
    ([process(command_line="")], "error"),
    ([process(command_line='python.exe "unterminated')], "error"),
    ([process(created=None)], "error"),
    ([process(created="2026-10-27T02:00:00")], "error"),
    ([process(created="not a creation date")], "error"),
    ([process(pid=True)], "error"),
    ([process(pid=0)], "error"),
    ([process(), process(command_line=None, pid=999)], "error"),
    ([None], "error"),
    ({}, "error"),
])
def test_absence_ambiguity_and_incomplete_metadata_fail_closed(rows, status):
    assert discovery._identify_processes(rows) == {
        "available": False, "session_id": None, "status": status}


def test_identity_includes_creation_time_and_never_exposes_command_lines():
    first = discovery._identify_processes([process()])
    same = discovery._identify_processes([process(f'PYTHON.EXE "{SCRIPT.upper()}"')])
    reused_pid = discovery._identify_processes([process(created="2026-10-27T02:00:01.1234567Z")])
    assert first["available"] and first["status"] == "running"
    assert same == first
    assert reused_pid["session_id"] != first["session_id"]
    assert set(first) == {"available", "session_id", "status"}
    assert "Python27" not in json.dumps(first) and SCRIPT not in json.dumps(first)


def test_cached_snapshot_transitions_loss_restart_and_query_error():
    rows = [[]]
    calls, notifications = [], []

    def query():
        calls.append(1)
        value = rows[0]
        if isinstance(value, Exception):
            raise value
        return value

    monitor = discovery.SlacsDiscovery(process_query=query, on_change=notifications.append, autostart=False)
    assert monitor.snapshot()["status"] == "checking" and not calls and not notifications
    monitor.poll_once()
    assert notifications[-1]["status"] == "not_running"
    rows[0] = [process()]
    first = monitor.poll_once()
    monitor.poll_once()
    assert len(notifications) == 2
    snapshot = monitor.snapshot()
    snapshot["available"] = False
    assert monitor.snapshot()["available"] and len(calls) == 3
    rows[0] = RuntimeError("private command line must not be exposed")
    assert monitor.poll_once()["status"] == "error"
    rows[0] = []
    assert not monitor.poll_once()["available"]
    rows[0] = [process(created="2026-10-27T02:00:02Z")]
    assert monitor.poll_once()["session_id"] != first["session_id"]
    assert monitor.stop() is True
    count = len(notifications)
    monitor.poll_once()
    assert monitor.stop() is True and len(notifications) == count
    assert monitor.snapshot()["status"] == "stopped"


def test_non_windows_never_queries_without_explicit_injection(monkeypatch):
    monkeypatch.setattr(discovery, "_query_windows_processes", lambda **kw: pytest.fail("must not query"))
    monitor = discovery.SlacsDiscovery(platform="darwin")
    assert monitor.snapshot()["status"] == "unsupported"
    assert monitor.poll_once()["status"] == "unsupported"
    assert monitor._thread is None
    assert monitor.stop() is True


def test_callback_can_read_state_and_stop_its_own_worker_without_deadlock():
    ready, monitor_box = threading.Event(), []
    seen = []

    def changed(state):
        monitor = monitor_box[0]
        assert monitor.snapshot() == state
        seen.append(state["status"])
        if state["available"]:
            assert monitor.stop() is False  # own worker is still executing this callback
            ready.set()

    monitor = discovery.SlacsDiscovery(process_query=lambda: [process()], on_change=changed, autostart=False)
    monitor_box.append(monitor)
    monitor.start()
    assert ready.wait(2)
    monitor._thread.join(timeout=2)
    assert not monitor._thread.is_alive() and seen == ["running", "stopped"]
    assert monitor.stop() is True


def test_pending_query_cannot_publish_after_stop():
    entered, release = threading.Event(), threading.Event()
    notifications = []

    def query():
        entered.set()
        assert release.wait(2)
        return [process()]

    monitor = discovery.SlacsDiscovery(process_query=query, on_change=notifications.append)
    assert entered.wait(2)
    result = []
    stopper = threading.Thread(target=lambda: result.append(monitor.stop()))
    stopper.start()
    assert monitor._stop.wait(2)
    release.set()
    stopper.join(timeout=2)
    assert result == [True] and not monitor._thread.is_alive()
    assert [entry["status"] for entry in notifications] == ["stopped"]


def test_default_query_is_hidden_bounded_and_read_only(monkeypatch):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(stdout=json.dumps([process()]))

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(discovery.subprocess, "run", run)
    assert discovery._query_windows_processes(timeout=1.25) == [process()]
    arguments, options = calls[0]
    assert arguments[0] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert "-NoProfile" in arguments and "-NonInteractive" in arguments
    assert options["timeout"] == 1.25 and options["creationflags"] == 0x08000000
    assert options["shell"] is False and options["stdin"] is subprocess.DEVNULL
    assert "Get-CimInstance" in arguments[-1] and "CreationDate" in arguments[-1]
    assert "ExecutionPolicy" not in " ".join(arguments)
    assert SCRIPT not in " ".join(arguments)


def test_default_cache_coalesces_success_and_error_without_reusing_stale_success():
    now, calls, value = [0.0], [], [[process()]]

    def query(**kwargs):
        calls.append(kwargs)
        if isinstance(value[0], Exception):
            raise value[0]
        return value[0]

    cache = discovery._ProcessQueryCache(clock=lambda: now[0], query=query)
    assert cache.read(timeout=2) == [process()]
    now[0] = 1.0
    assert cache.read(timeout=2) == [process()] and len(calls) == 1
    now[0], value[0] = 2.0, RuntimeError("query failed")
    with pytest.raises(OSError):
        cache.read(timeout=2)
    value[0] = []
    with pytest.raises(OSError):
        cache.read(timeout=2)
    assert len(calls) == 2
    now[0] = 4.0
    assert cache.read(timeout=2) == [] and len(calls) == 3


@pytest.mark.parametrize("option", ["poll_interval", "query_timeout"])
@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf")])
def test_monitor_waits_must_be_positive_finite(option, value):
    with pytest.raises(ValueError):
        discovery.SlacsDiscovery(**{option: value})


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows CIM process metadata")
def test_windows_cim_identifies_only_our_harmless_temporary_script_and_its_exit(tmp_path):
    if ntpath.basename(sys.executable).lower() not in {"python.exe", "pythonw.exe"}:
        pytest.skip("requires a normally named Windows Python interpreter")
    script = tmp_path / discovery.SCRIPT_NAME
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=0x08000000)
    # Restrict assertions to this test-owned child. Existing instrument processes
    # are never started, stopped, imported, or used as test fixtures.
    monitor = discovery.SlacsDiscovery(
        process_query=lambda: [row for row in discovery._query_windows_processes(timeout=5)
                               if row.get("pid") == child.pid], autostart=False)
    try:
        deadline = time.monotonic() + 15
        state = monitor.poll_once()
        while not state["available"] and time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
            state = monitor.poll_once()
        assert state["available"] and state["status"] == "running"
        child.terminate()
        child.wait(timeout=5)
        deadline = time.monotonic() + 10
        state = monitor.poll_once()
        while state["available"] and time.monotonic() < deadline:
            time.sleep(0.1)
            state = monitor.poll_once()
        assert state == {"available": False, "session_id": None, "status": "not_running"}
    finally:
        monitor.stop()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
