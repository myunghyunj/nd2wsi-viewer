"""SLACS identity comes from process metadata, never from running legacy code."""

import json
import ntpath
import os
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

    monitor = discovery.SlacsDiscovery(process_query=query, on_change=notifications.append, autostart=False,
                                       failure_grace=0)
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


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def logged(monkeypatch):
    events = []
    monkeypatch.setattr(discovery.session_log, "event",
                        lambda kind, **fields: events.append({"kind": kind, **fields}))
    return events


def counting(rows):
    calls = []

    def query():
        calls.append(1)
        value = rows[0]
        if isinstance(value, Exception):
            raise value
        return value
    return query, calls


def test_a_found_slacs_is_then_checked_by_liveness_not_by_searching_again(monkeypatch):
    events = logged(monkeypatch)
    rows, alive, checked = [[process()]], [True], []
    query, calls = counting(rows)

    def liveness(pid, created):
        checked.append((pid, created))
        return alive[0]

    monitor = discovery.SlacsDiscovery(process_query=query, liveness=liveness, autostart=False)
    first = monitor.poll_once()
    assert first["status"] == "running" and len(calls) == 1 and monitor.slacs_pid() == 123
    for _ in range(5):
        assert monitor.poll_once() == first
    assert len(calls) == 1 and checked == [(123, BIRTH)] * 5
    # A confirmed exit searches again at once, in the same check.
    alive[0], rows[0] = False, []
    assert monitor.poll_once()["status"] == "not_running" and len(calls) == 2
    assert monitor.slacs_pid() is None
    assert [e["kind"] for e in events] == ["slacs_exit_seen"]


def test_checks_that_cannot_tell_keep_the_session_through_the_grace(monkeypatch):
    events = logged(monkeypatch)
    clock, rows, alive = Clock(), [[process()]], [True]
    query, calls = counting(rows)
    notifications = []
    monitor = discovery.SlacsDiscovery(process_query=query, liveness=lambda pid, created: alive[0],
                                       on_change=notifications.append, autostart=False,
                                       failure_grace=15, clock=clock)
    first = monitor.poll_once()
    alive[0] = None
    for step in range(7):  # 14 s of "cannot tell": still the same running session
        clock.now += 2
        assert monitor.poll_once() == first
    assert len(calls) == 1 and len(notifications) == 1
    alive[0] = True
    clock.now += 2
    assert monitor.poll_once() == first
    kinds = [e["kind"] for e in events]
    assert kinds == ["slacs_check_failing", "slacs_check_recovered"]
    assert events[1]["failed_checks"] == 7 and events[1]["seconds"] == 14.0
    # Past the grace a failed search finally reports an error.
    alive[0], rows[0] = None, RuntimeError("no metadata")
    for _ in range(8):  # 2-14 s of failures: still kept
        clock.now += 2
        assert monitor.poll_once() == first
    clock.now += 2
    state = monitor.poll_once()
    assert state["status"] == "error" and not state["available"]
    assert events[-1]["kind"] == "slacs_check_gave_up" and events[-1]["seconds"] == 16.0


def test_a_failed_search_also_keeps_a_found_session_through_the_grace(monkeypatch):
    logged(monkeypatch)
    clock, rows = Clock(), [[process()]]
    query, _ = counting(rows)
    monitor = discovery.SlacsDiscovery(process_query=query, autostart=False, failure_grace=15, clock=clock)
    first = monitor.poll_once()
    rows[0] = RuntimeError("PowerShell timed out")
    clock.now += 10
    assert monitor.poll_once() == first
    rows[0] = []  # a successful search that finds nothing is definitive
    clock.now += 2
    assert monitor.poll_once()["status"] == "not_running"


def test_a_reused_pid_is_a_new_session(monkeypatch):
    logged(monkeypatch)
    rows = [[process()]]
    query, _ = counting(rows)
    monitor = discovery.SlacsDiscovery(process_query=query, liveness=lambda pid, created: False,
                                       autostart=False)
    first = monitor.poll_once()
    rows[0] = [process(created="2026-10-27T03:00:00.1234567Z")]
    second = monitor.poll_once()
    assert second["status"] == "running" and second["session_id"] != first["session_id"]


def test_each_powershell_query_is_logged_with_its_duration_and_reason(monkeypatch):
    events = logged(monkeypatch)
    failures = [subprocess.TimeoutExpired("powershell", 10), subprocess.CalledProcessError(1, "powershell"),
                json.JSONDecodeError("bad", "x", 0), FileNotFoundError("powershell")]
    outcomes = [[process(), process(pid=7, command_line="python.exe other.py")]] + failures
    now = [0.0]

    def query(**kwargs):
        value = outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    cache = discovery._ProcessQueryCache(clock=lambda: now[0], query=query)
    for _ in range(5):
        try:
            cache.read(timeout=10)
        except OSError:
            pass
        now[0] += 3
    assert [e["kind"] for e in events] == ["slacs_query"] * 5
    assert events[0]["ok"] is True and events[0]["python_processes"] == 2
    assert [e["reason"] for e in events[1:]] == [
        "timeout", "exit code 1", "invalid output", "could not start PowerShell (FileNotFoundError)"]
    assert all(isinstance(e["seconds"], float) for e in events)
    assert SCRIPT not in json.dumps(events) and "other.py" not in json.dumps(events)


def test_stop_never_waits_out_a_slow_search(monkeypatch):
    monkeypatch.setattr(discovery, "STOP_JOIN_SECONDS", 0.2)
    entered, release = threading.Event(), threading.Event()

    def query():
        entered.set()
        release.wait(5)
        return [process()]

    monitor = discovery.SlacsDiscovery(process_query=query, query_timeout=10)
    try:
        assert entered.wait(2)
        started = time.monotonic()
        assert monitor.stop() is False
        assert time.monotonic() - started < 1
    finally:
        release.set()
        monitor._thread.join(timeout=5)


def test_cim_creation_times_convert_to_filetime_ticks():
    assert discovery._filetime("1601-01-01T00:00:00Z") == 0
    assert discovery._filetime("1970-01-01T00:00:00+00:00") == 116444736000000000
    assert discovery._filetime("1970-01-01T00:00:00.0000017Z") == 116444736000000010  # microseconds
    with pytest.raises(ValueError):
        discovery._filetime("1970-01-01T00:00:00")


def test_default_search_timeout_allows_a_slow_powershell_start():
    assert discovery.QUERY_TIMEOUT >= 10 and discovery.FAILURE_GRACE >= 10


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), "15"])
def test_failure_grace_must_be_finite_and_not_negative(value):
    with pytest.raises(ValueError):
        discovery.SlacsDiscovery(process_query=lambda: [], failure_grace=value, autostart=False)


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


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows process liveness")
def test_windows_liveness_follows_our_own_child_by_pid_and_creation_time(tmp_path):
    if ntpath.basename(sys.executable).lower() not in {"python.exe", "pythonw.exe"}:
        pytest.skip("requires a normally named Windows Python interpreter")
    script = tmp_path / discovery.SCRIPT_NAME
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=0x08000000)
    queries = []

    def query():
        rows = [row for row in discovery._query_windows_processes(timeout=10) if row.get("pid") == child.pid]
        queries.append(len(rows))
        return rows

    monitor = discovery.SlacsDiscovery(process_query=query, liveness=discovery._windows_process_alive,
                                       autostart=False)
    try:
        deadline = time.monotonic() + 20
        state = monitor.poll_once()
        while not state["available"] and time.monotonic() < deadline:
            time.sleep(0.2)
            state = monitor.poll_once()
        assert state["available"] and monitor.slacs_pid() == child.pid
        searches = len(queries)
        found = monitor._found
        for _ in range(5):
            assert monitor.poll_once() == state
        assert len(queries) == searches  # liveness only, no PowerShell
        assert discovery._windows_process_alive(child.pid, found["created"]) is True
        assert discovery._windows_process_alive(child.pid, "2001-01-01T00:00:00Z") is False
        child.terminate()
        child.wait(timeout=5)
        assert discovery._windows_process_alive(child.pid, found["created"]) is False
        assert monitor.poll_once()["status"] == "not_running"
    finally:
        monitor.stop()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


SESSION_A, SESSION_B = "slacs-" + "a" * 32, "slacs-" + "b" * 32


def test_offer_claim_has_one_winner_across_windows_and_stays_with_it(tmp_path):
    folder = tmp_path / "offers" / "user"
    first, second = discovery.SlacsOfferClaims(folder, "window-1"), discovery.SlacsOfferClaims(folder, "window-2")
    assert first.claim(SESSION_A) is True
    assert second.claim(SESSION_A) is False
    assert first.claim(SESSION_A) is True  # the claimant keeps its offer on every poll
    assert second.claim(SESSION_B) is True  # a restarted SLACS process is offered again
    assert first.claim(SESSION_B) is False
    assert discovery.SlacsOfferClaims(folder, "window-3").claim(SESSION_A) is False  # later windows stay quiet


def test_concurrent_offer_claims_have_exactly_one_winner(tmp_path):
    folder, barrier, results = tmp_path / "offers", threading.Barrier(8), []

    def race(index):
        claims = discovery.SlacsOfferClaims(folder, f"window-{index}")
        barrier.wait()
        results.append(claims.claim(SESSION_A))

    threads = [threading.Thread(target=race, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(results) == [False] * 7 + [True]


def test_expired_offer_claims_are_pruned_and_only_our_files_are_removed(tmp_path):
    folder, now = tmp_path / "offers", [1_000_000.0]
    old = discovery.SlacsOfferClaims(folder, "window-1", lifetime=60, clock=lambda: now[0])
    assert old.claim(SESSION_A) is True
    stale = folder / f"{SESSION_A}.claim"
    os.utime(stale, (now[0] - 120, now[0] - 120))
    keep = [folder / "notes.txt", folder / ("slacs-" + "c" * 32 + ".claim.bak")]
    for path in keep:
        path.write_text("not ours")
        os.utime(path, (0, 0))
    (folder / ("slacs-" + "d" * 32 + ".claim")).mkdir()
    os.utime(folder / ("slacs-" + "d" * 32 + ".claim"), (0, 0))
    newcomer = discovery.SlacsOfferClaims(folder, "window-2", lifetime=60, clock=lambda: now[0])
    assert newcomer.claim(SESSION_B) is True  # pruning runs before each claim
    assert not stale.exists()
    assert all(path.exists() for path in keep)
    assert (folder / ("slacs-" + "d" * 32 + ".claim")).is_dir()
    assert newcomer.claim(SESSION_A) is True  # an expired claim no longer blocks an offer


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs a privilege on Windows")
def test_offer_claims_never_follow_or_remove_symlinks(tmp_path):
    folder, target = tmp_path / "offers", tmp_path / "elsewhere.json"
    folder.mkdir()
    target.write_text(json.dumps({"claimant": "window-1"}))
    os.utime(target, (0, 0))  # old enough that pruning through the link would remove it
    link = folder / f"{SESSION_B}.claim"
    link.symlink_to(target)
    os.utime(link, (0, 0), follow_symlinks=False)
    own = folder / f"{SESSION_A}.claim"
    own.symlink_to(target)
    claims = discovery.SlacsOfferClaims(folder, "window-1", lifetime=60)
    assert claims.claim(SESSION_A) is False  # a link is never read as this window's claim
    assert link.is_symlink() and own.is_symlink() and target.exists()


@pytest.mark.parametrize("session", [None, 7, "", "slacs-", "slacs-" + "A" * 32, "../" + SESSION_A,
                                     SESSION_A + "/x", "process-one"])
def test_offer_claims_refuse_anything_but_a_discovery_session_id(tmp_path, session):
    folder = tmp_path / "offers"
    assert discovery.SlacsOfferClaims(folder, "window-1").claim(session) is False
    assert not folder.exists()


def test_unwritable_offer_folder_falls_back_to_one_offer_per_window(tmp_path):
    blocked = tmp_path / "offers"
    blocked.write_text("a file where the folder should be")
    assert discovery.SlacsOfferClaims(blocked / "user", "window-1").claim(SESSION_A) is True
    assert discovery.SlacsOfferClaims(blocked / "user", "window-2").claim(SESSION_A) is True


def test_a_still_running_session_keeps_its_claim_past_the_lifetime(tmp_path):
    folder, now = tmp_path / "offers", [1_000_000.0]
    first = discovery.SlacsOfferClaims(folder, "window-1", lifetime=60, clock=lambda: now[0])
    assert first.claim(SESSION_A) is True
    claim = folder / f"{SESSION_A}.claim"
    os.utime(claim, (now[0] - 600, now[0] - 600))  # SLACS has run longer than the lifetime
    later = discovery.SlacsOfferClaims(folder, "window-2", lifetime=60, clock=lambda: now[0])
    assert later.claim(SESSION_A) is False  # still offered only once
    assert claim.exists()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="POSIX permissions; root ignores them")
def test_a_read_only_offer_folder_falls_back_to_one_offer_per_window(tmp_path):
    folder = tmp_path / "offers"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        assert discovery.SlacsOfferClaims(folder, "window-1").claim(SESSION_A) is True
        assert discovery.SlacsOfferClaims(folder, "window-2").claim(SESSION_A) is True
    finally:
        folder.chmod(0o700)
