"""The per-process session log: files, records, limits, retention and crash capture."""

from __future__ import annotations

import faulthandler
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from nd2wsi import app, session_log

STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d")
OLD = time.time() - 30 * 24 * 3600


@pytest.fixture(autouse=True)
def closed_session():
    session_log.stop("test setup")
    yield
    session_log.stop("test teardown")


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_bytes().decode("utf-8").splitlines()]


def kinds(path: Path) -> list[str]:
    return [record["kind"] for record in records(path)]


def start(folder: Path) -> Path:
    path = session_log.start(app_name="nd2wsi-viewer", version="9.9.9", folder=folder)
    assert path is not None
    return path


# ---- folder choice -----------------------------------------------------------

def test_environment_folder_overrides_every_default(monkeypatch, tmp_path):
    monkeypatch.setenv(session_log.ENV_FOLDER, str(tmp_path / "chosen"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert session_log.candidate_folders() == [tmp_path / "chosen"]
    path = session_log.start(app_name="nd2wsi-viewer", version="9.9.9")
    assert path is not None and path.parent == tmp_path / "chosen"


def test_a_writable_frozen_windows_copy_keeps_logs_beside_the_executable(monkeypatch, tmp_path):
    monkeypatch.delenv(session_log.ENV_FOLDER, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    (tmp_path / "portable").mkdir()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "portable" / "nd2wsi-viewer.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = session_log.start(app_name="nd2wsi-viewer", version="9.9.9")
    assert path is not None and path.parent == (tmp_path / "portable" / "logs").resolve()
    assert not (tmp_path / "local").exists()


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions that bind the current user")
def test_an_unwritable_frozen_copy_falls_back_to_the_per_user_folder(monkeypatch, tmp_path):
    monkeypatch.delenv(session_log.ENV_FOLDER, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    install = tmp_path / "Program Files"
    install.mkdir()
    monkeypatch.setattr(sys, "executable", str(install / "nd2wsi-viewer.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    install.chmod(0o555)
    try:
        path = session_log.start(app_name="nd2wsi-viewer", version="9.9.9")
    finally:
        install.chmod(0o755)
    assert path is not None
    assert path.parent == tmp_path / "local" / "nd2wsi-viewer" / "Logs" / "sessions"
    assert not (install / "logs").exists()


@pytest.mark.parametrize("marker", ["Uninstall.exe", ".nd2wsi-install.ini"])
def test_an_installed_copy_leaves_its_install_folder_as_setup_left_it(monkeypatch, tmp_path, marker):
    monkeypatch.delenv(session_log.ENV_FOLDER, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    install = tmp_path / "Programs" / "nd2wsi-viewer"
    install.mkdir(parents=True)
    (install / marker).write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(install / "nd2wsi-viewer.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = session_log.start(app_name="nd2wsi-viewer", version="9.9.9")
    assert path is not None and path.parent == tmp_path / "local" / "nd2wsi-viewer" / "Logs" / "sessions"
    assert sorted(item.name for item in install.iterdir()) == [marker]


def test_source_runs_and_mac_app_bundles_use_the_per_user_folder(monkeypatch, tmp_path):
    monkeypatch.delenv(session_log.ENV_FOLDER, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert session_log.candidate_folders() == [tmp_path / "local" / "nd2wsi-viewer" / "Logs" / "sessions"]
    # Writing inside a signed macOS bundle would break its signature.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nd2wsi-viewer.app" / "Contents" / "MacOS" / "nd2wsi-viewer"))
    assert session_log.candidate_folders() == [Path.home() / "Library" / "Logs" / "nd2wsi-viewer" / "sessions"]


# ---- records -----------------------------------------------------------------

def test_records_are_timestamped_json_lines_with_a_start_and_a_stop(tmp_path):
    path = start(tmp_path)
    assert re.fullmatch(r"session-\d{8}-\d{6}-\d+\.jsonl", path.name)
    assert path.name.endswith(f"-{os.getpid()}.jsonl")
    assert session_log.path() == path
    session_log.event("file_open", path="한글 slide.nd2", ok=True, kind="shadowed", t=1, thread="x")
    session_log.stop("done")
    first, opened, last = records(path)
    for record in (first, opened, last):
        assert STAMP.fullmatch(record["t"])
        assert isinstance(record["uptime"], float) and record["uptime"] >= 0
        assert record["thread"] == threading.current_thread().name
    assert first["kind"] == "start" and first["app"] == "nd2wsi-viewer" and first["version"] == "9.9.9"
    assert first["pid"] == os.getpid()
    assert opened == {**opened, "kind": "file_open", "path": "한글 slide.nd2", "ok": True,
                      "field_kind": "shadowed", "field_t": 1, "field_thread": "x"}
    assert last["kind"] == "stop" and last["reason"] == "done" and last["dropped"] == 0


def test_each_line_is_on_disk_as_soon_as_event_returns(tmp_path):
    path = start(tmp_path)
    session_log.event("follow", state="waiting for stage")
    # A separate reader sees the line while the session is still open.
    with open(path, encoding="utf-8") as reader:
        lines = reader.read().splitlines()
    assert json.loads(lines[-1])["state"] == "waiting for stage"


def test_concurrent_events_never_interleave(tmp_path):
    path = start(tmp_path)

    def write(worker):
        for index in range(200):
            session_log.event("tick", worker=worker, index=index, text="x" * 300)

    threads = [threading.Thread(target=write, args=(worker,), name=f"writer-{worker}") for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    session_log.stop("done")
    ticks = [record for record in records(path) if record["kind"] == "tick"]
    assert len(ticks) == 1600
    assert {(record["worker"], record["index"]) for record in ticks} == {
        (worker, index) for worker in range(8) for index in range(200)}
    assert all(record["thread"] == f"writer-{record['worker']}" for record in ticks)


class Unprintable:
    def __str__(self):
        raise RuntimeError("no text")

    __repr__ = __str__


def test_unserialisable_values_are_stringified_and_long_strings_capped(tmp_path):
    path = start(tmp_path)
    loop: list = []
    loop.append(loop)
    session_log.event("odd", obj=object(), broken=Unprintable(), nan=float("nan"), raw=b"\x00\xff",
                      path=Path("slides") / "a.nd2", items={1, 2}, loop=loop, surrogate="\udc80",
                      long="y" * 20000, many=list(range(1000)), **{"spaced key": 1})
    session_log.event(Unprintable(), value=1)
    session_log.stop("done")
    odd = records(path)[1]
    assert odd["obj"].startswith("<object object")
    assert odd["broken"] == "<unprintable Unprintable>"
    assert odd["nan"] == "nan"
    assert odd["path"] == str(Path("slides") / "a.nd2")
    assert sorted(odd["items"]) == [1, 2]
    assert odd["surrogate"] == "\udc80"
    assert odd["long"].startswith("y" * session_log.MAX_STRING) and odd["long"].endswith("[+11808 chars]")
    assert len(odd["many"]) == 201 and odd["many"][-1] == "… 800 more"
    assert odd["spaced key"] == 1
    assert records(path)[2]["kind"] == "<unprintable Unprintable>"


def test_events_before_start_or_after_stop_do_nothing(tmp_path):
    session_log.event("early")
    path = start(tmp_path)
    assert session_log.start(app_name="other", version="0", folder=tmp_path / "other") == path
    session_log.stop("first")
    session_log.stop("second")
    session_log.event("late")
    assert kinds(path) == ["start", "stop"]
    assert records(path)[-1]["reason"] == "first"
    again = start(tmp_path)
    assert again != path and re.fullmatch(r"session-\d{8}-\d{6}-\d+(-\d+)?\.jsonl", again.name)


def test_an_unwritable_folder_or_failing_disk_never_raises(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("file", encoding="utf-8")
    assert session_log.start(app_name="nd2wsi-viewer", version="9.9.9", folder=blocker / "logs") is None
    session_log.event("ignored", value=1)
    session_log.stop("never started")
    assert session_log.path() is None or not str(session_log.path()).startswith(str(blocker))
    path = start(tmp_path / "logs")
    session_log._handle.close()  # every later write now fails underneath the module
    session_log.event("lost", value=1)
    session_log.stop("done")
    assert kinds(path) == ["start"]


def test_the_size_cap_keeps_only_errors_and_the_stop_record(monkeypatch, tmp_path):
    monkeypatch.setattr(session_log, "MAX_SESSION_BYTES", 4000)
    monkeypatch.setattr(session_log, "_HARD_CEILING", 6000)
    path = start(tmp_path)
    for index in range(40):
        session_log.event("health", index=index, padding="p" * 100)
    for kind in ("error", "exception", "client_error", "crash", "app_log"):
        session_log.event(kind, message=kind)
    session_log.stop("done")
    written = kinds(path)
    assert written.count("log_limit") == 1
    after = written[written.index("log_limit") + 1:]
    assert after == ["error", "exception", "client_error", "crash", "stop"]
    assert records(path)[-1]["dropped"] > 0
    assert path.stat().st_size < 6000


def test_past_the_hard_ceiling_only_the_stop_record_is_written(monkeypatch, tmp_path):
    monkeypatch.setattr(session_log, "MAX_SESSION_BYTES", 1000)
    monkeypatch.setattr(session_log, "_HARD_CEILING", 1500)
    path = start(tmp_path)
    for _ in range(50):
        session_log.event("error", message="e" * 200)
    session_log.stop("done")
    assert kinds(path)[-1] == "stop"
    assert path.stat().st_size < 1500 + 2 * 450


# ---- retention ---------------------------------------------------------------

def make_session(folder: Path, index: int, *, size: int = 100, fatal: bool = False, mtime: float = OLD) -> list[Path]:
    stem = f"session-20260101-{index:06d}-{1000 + index}"
    files = [folder / f"{stem}.jsonl"]
    files[0].write_bytes(b"x" * size)
    if fatal:
        files.append(folder / f"{stem}-fatal.txt")
        files[1].write_text("Fatal Python error", encoding="utf-8")
    for file in files:
        os.utime(file, (mtime + index, mtime + index))
    return files


def test_retention_keeps_fifty_sessions_and_touches_only_its_own_regular_files(tmp_path, symlink_support):
    folder = tmp_path / "logs"
    folder.mkdir()
    sessions = [make_session(folder, index, fatal=index % 2 == 0) for index in range(60)]
    outside = tmp_path / "outside.jsonl"
    outside.write_text("keep", encoding="utf-8")
    link = folder / "session-20200101-000000-1.jsonl"
    link.symlink_to(outside)
    os.utime(link, (OLD - 99, OLD - 99), follow_symlinks=False)
    unrelated = [folder / name for name in ("notes.txt", "session-20200101-000000-2.jsonl.bak",
                                            "session-old.jsonl", "Session-20200101-000000-3.jsonl")]
    for file in unrelated:
        file.write_text("keep", encoding="utf-8")
        os.utime(file, (OLD - 99, OLD - 99))
    (folder / "session-20200101-000000-4.jsonl").mkdir()
    (folder / "nested").mkdir()
    nested = folder / "nested" / "session-20200101-000000-5.jsonl"
    nested.write_text("keep", encoding="utf-8")
    os.utime(nested, (OLD - 99, OLD - 99))

    current = start(folder)

    # The new session plus the 49 newest old ones remain; the 11 oldest go
    # together with their fatal files.
    for files in sessions[:11]:
        assert not any(file.exists() for file in files)
    for files in sessions[11:]:
        assert all(file.exists() for file in files)
    assert current.exists()
    assert link.is_symlink() and outside.read_text(encoding="utf-8") == "keep"
    assert all(file.read_text(encoding="utf-8") == "keep" for file in unrelated)
    assert (folder / "session-20200101-000000-4.jsonl").is_dir()
    assert nested.exists()
    retention, = [record for record in records(current) if record["kind"] == "log_retention"]
    assert retention["deleted_files"] == 11 + 6 and retention["kept_sessions"] == 50


def test_retention_caps_the_total_size_from_the_newest_session_down(monkeypatch, tmp_path):
    # The new session's own start record counts too; it is far below 50 kB.
    monkeypatch.setattr(session_log, "KEEP_TOTAL_BYTES", 250_000)
    folder = tmp_path / "logs"
    folder.mkdir()
    sessions = [make_session(folder, index, size=100_000) for index in range(6)]
    # An older small session does not survive once a newer one did not fit.
    small = make_session(folder, 50, size=10, mtime=OLD - 1000)
    start(folder)
    assert [files[0].exists() for files in sessions] == [False, False, False, False, True, True]
    assert not small[0].exists()


def test_retention_never_deletes_a_session_that_is_still_being_written(monkeypatch, tmp_path):
    monkeypatch.setattr(session_log, "KEEP_SESSIONS", 2)
    folder = tmp_path / "logs"
    folder.mkdir()
    live = [make_session(folder, index, mtime=time.time() - 10 - index) for index in range(4)]
    stale = make_session(folder, 9)
    start(folder)
    assert all(files[0].exists() for files in live)
    assert not stale[0].exists()


# ---- crash capture -----------------------------------------------------------

def test_uncaught_exceptions_are_recorded_and_passed_to_the_previous_hooks(monkeypatch, tmp_path):
    seen = []
    previous_sys = lambda *args: seen.append(("sys", args[0]))  # noqa: E731
    previous_thread = lambda args: seen.append(("thread", args.exc_type, getattr(args.thread, "name", None)))  # noqa: E731
    monkeypatch.setattr(sys, "excepthook", previous_sys)
    monkeypatch.setattr(threading, "excepthook", previous_thread)
    path = start(tmp_path)
    assert sys.excepthook is session_log._excepthook
    assert threading.excepthook is session_log._threading_excepthook
    try:
        raise ValueError("main boom")
    except ValueError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)

    def fail():
        raise KeyError("worker boom")

    worker = threading.Thread(target=fail, name="stage-worker")
    worker.start()
    worker.join()
    quiet = threading.Thread(target=sys.exit, name="quiet-exit")
    quiet.start()
    quiet.join()
    session_log.stop("done")
    # Only this test's threads: a stray thread left by another test may fail meanwhile.
    ours = ("sys", "stage-worker", "quiet-exit")
    assert [item for item in seen if item[0] == "sys" or item[2] in ours] == [
        ("sys", ValueError), ("thread", KeyError, "stage-worker"), ("thread", SystemExit, "quiet-exit")]
    crash, exception = [record for record in records(path) if record["kind"] == "crash" or (
        record["kind"] == "exception" and record["thread_name"] in ours)]
    assert crash["type"] == "ValueError" and crash["message"] == "main boom"
    assert "raise ValueError" in crash["traceback"]
    assert exception["type"] == "KeyError" and exception["thread_name"] == "stage-worker"
    assert exception["thread"] == "stage-worker" and "worker boom" in exception["traceback"]
    # stop() hands the hooks back.
    assert sys.excepthook is previous_sys and threading.excepthook is previous_thread


def test_hooks_are_left_alone_when_someone_replaced_them_after_start(monkeypatch, tmp_path):
    start(tmp_path)
    replacement = lambda *args: None  # noqa: E731
    monkeypatch.setattr(sys, "excepthook", replacement)
    session_log.stop("done")
    assert sys.excepthook is replacement


def test_native_crashes_go_to_a_fatal_file_removed_only_when_empty(tmp_path):
    enabled_before = faulthandler.is_enabled()
    path = start(tmp_path / "clean")
    fatal = path.with_name(path.stem + "-fatal.txt")
    assert fatal.exists() and faulthandler.is_enabled()
    session_log.stop("done")
    assert not fatal.exists()
    assert faulthandler.is_enabled() == enabled_before

    path = start(tmp_path / "crashed")
    fatal = path.with_name(path.stem + "-fatal.txt")
    faulthandler.dump_traceback(file=session_log._fatal_handle)  # what a fatal signal would write
    session_log.stop("done")
    assert "File" in fatal.read_text(encoding="utf-8")


def test_health_records_arrive_from_a_daemon_thread_that_stop_ends(monkeypatch, tmp_path):
    monkeypatch.setattr(session_log, "HEALTH_INTERVAL", 0.02)
    path = start(tmp_path)
    deadline = time.monotonic() + 5
    while "health" not in kinds(path) and time.monotonic() < deadline:
        time.sleep(0.02)
    health = next(record for record in records(path) if record["kind"] == "health")
    assert health["thread"] == "session-log-health"
    assert health["threads"] >= 2 and len(health["gc_counts"]) == 3
    if sys.platform in ("darwin", "win32"):
        assert health["rss_mb"] > 0
    thread = session_log._health_thread
    assert thread.daemon
    session_log.stop("done")
    assert not thread.is_alive()


# ---- app wiring --------------------------------------------------------------

def test_every_diagnostic_line_is_mirrored_into_the_session_log(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "log_path", lambda: tmp_path / "app.log")
    path = start(tmp_path / "logs")
    app._dlog("open 한글 slide.nd2")
    session_log.stop("done")
    assert "open 한글 slide.nd2" in (tmp_path / "app.log").read_text(encoding="utf-8")
    mirrored, = [record for record in records(path) if record["kind"] == "app_log"]
    assert mirrored["message"] == "open 한글 slide.nd2"


@pytest.fixture
def client(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(app, "_CLIENT_EVENTS", app._RateLimit(20, 60.0, clock=lambda: clock[0]))
    api = app.Api(None)
    path = start(tmp_path / "logs")
    return api, path, clock


def client_records(path: Path) -> list[dict]:
    return [record for record in records(path) if record["kind"] == "client_error"]


def test_client_events_keep_only_known_text_fields_with_size_caps(client, monkeypatch):
    api, path, _ = client
    monkeypatch.setattr(app, "_server_url", lambda server: "http://127.0.0.1:5123/capability-token/")
    api._httpd = object()
    result = api.log_client_event({
        "source": "pane", "kind": "unhandledrejection", "message": "m" * 5000,
        "where": "http://127.0.0.1:5123/capability-token/static/app.js:12:3",
        "stack": "s" * 20000, "extra": "ignored",
    })
    assert result == {"ok": True}
    session_log.stop("done")
    record, = client_records(path)
    assert record["source"] == "pane" and record["client_kind"] == "unhandledrejection"
    assert record["message"] == "m" * 2000 and record["stack"] == "s" * 8000
    assert record["where"] == "static/app.js:12:3"
    assert "extra" not in record and "dropped" not in record


@pytest.mark.parametrize("payload", [
    None, "text", ["source", "shell"], {},
    {"source": "server", "kind": "error", "message": "x"},
    {"source": "shell", "kind": "Not A Kind!", "message": "x"},
    {"source": "shell", "kind": "", "message": "x"},
    {"source": "shell", "kind": "error", "message": 12},
    {"source": "shell", "kind": "error", "message": "x", "stack": {"frames": []}},
])
def test_client_events_reject_malformed_payloads(client, payload):
    api, path, _ = client
    result = api.log_client_event(payload)
    assert result["ok"] is False and result["message"]
    session_log.stop("done")
    assert client_records(path) == []


def test_client_events_are_rate_limited_per_process(client):
    api, path, clock = client
    payload = {"source": "shell", "kind": "error", "message": "flood"}
    results = [api.log_client_event(payload)["ok"] for _ in range(25)]
    assert results == [True] * 20 + [False] * 5
    # Another window's bridge in this process shares the same budget.
    assert app.Api(None).log_client_event(payload)["ok"] is False
    clock[0] = 60.0
    assert api.log_client_event({**payload, "message": "after"}) == {"ok": True}
    session_log.stop("done")
    recorded = client_records(path)
    assert len(recorded) == 21
    assert recorded[-1]["message"] == "after" and recorded[-1]["dropped"] == 6


def test_slides_opened_from_the_shell_are_recorded_with_their_outcome(tmp_path):
    import urllib.error
    import urllib.request

    from PIL import Image

    from nd2wsi.server import create_server, server_url

    image = tmp_path / "한글 sample.png"
    Image.new("RGB", (8, 6), (10, 20, 30)).save(image)
    path = start(tmp_path / "logs")
    httpd = create_server([], port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = server_url(httpd)

    def post(target):
        request = urllib.request.Request(base + "api/open", data=json.dumps({"path": str(target)}).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(request, timeout=10).read())
        except urllib.error.HTTPError as error:
            return json.loads(error.read())

    try:
        sid = post(image)["sid"]
        assert "error" in post(tmp_path / "missing.nd2")
    finally:
        httpd.shutdown_for_relaunch()
        httpd.server_close()
    session_log.stop("done")
    opened, failed = [record for record in records(path) if record["kind"] == "file_open"]
    assert opened["via"] == "shell" and opened["path"] == str(image) and opened["format"] == "png"
    assert opened["ok"] is True and opened["sid"] == sid and opened["seconds"] >= 0
    assert failed["path"] == str(tmp_path / "missing.nd2") and failed["format"] == "nd2"
    assert failed["ok"] is False and failed["error"]
