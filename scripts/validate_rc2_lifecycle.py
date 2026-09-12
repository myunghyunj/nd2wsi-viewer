#!/usr/bin/env python3
"""Opt-in packaged RC2 Agent lifecycle checks, with an existing isolated source.

This invokes the actual app and lets each diagnostic window close itself. It
never targets an existing window, builds a cache, changes installed apps, or
deletes data. Browser replay times are diagnostic rAF proxies, not a performance
comparison. Supply the same explicit context used by the packaged benchmark.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

CASES = {
    "metadata_timeout": ("metadata_timeout", "fatal", "metadata_timeout", True),
    "metadata_invalid": ("metadata_invalid", "fatal", "metadata_invalid", True),
    "gpu_fatal": ("gpu_fatal", "fatal", "gpu_execution_failure", True),
    "gpu_memory_pressure": ("gpu_memory_pressure", "fatal", "memory_pressure", True),
    "all_tiles_failed": ("all_tiles_failed", "fatal", "tile_unavailable", True),
    "close_race": ("close_race", "closed", None, False),
    "tile_503": ("tile_503", "closed", None, False),
    "manual_handoff": (None, "handoff", None, True),
    "duplicate_attempt": ("metadata_invalid", "fatal", "metadata_invalid", True),
    "browser_failure": (None, None, None, False),
}


def read_json(path):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"Unexpected oversized diagnostic report: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_new(path, payload):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def session_records(root):
    return [read_json(path) for path in root.glob("*/session.json")]


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # A private atomic diagnostic has not been published yet.
        time.sleep(.2)
    raise TimeoutError(description)


def assert_closed_sessions(root, expected, timeout=15):
    def closed():
        sessions = session_records(root)
        if len(sessions) == expected and all(item.get("state") == "closed" for item in sessions):
            return sessions
        return None
    return wait_for(closed, timeout, f"Expected {expected} self-closed Agent sessions under {root}")


def ledger_counts(path):
    if not path.exists():
        return {"failures": 0, "fallbacks": 0}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("failures", "fallbacks")}


def run_case(name, args, executable, base_environment):
    directory = args.output / name
    directory.mkdir(mode=0o700, exist_ok=False)
    sessions = directory / "sessions"
    ledger = directory / "renderer-state"
    browser_report = directory / "browser.json"
    attempt = uuid.uuid4().hex
    fault, expected_kind, expected_failure, fallback = CASES[name]
    count = 2 if name == "duplicate_attempt" else 1
    environment = dict(base_environment, ND2WSI_WINDOW_SESSION_ROOT=str(sessions),
                       ND2WSI_RENDERER_STATE_ROOT=str(ledger))
    processes, handles, native_paths = [], [], []
    started = time.monotonic()
    try:
        if name == "browser_failure":
            return run_browser_failure(args, executable, environment, directory, sessions, ledger, attempt)
        for index in range(count):
            native_report = directory / f"native-{index + 1}.json"
            native_paths.append(native_report)
            command = [str(executable), "--agent-window", "--prefer-metal",
                       "--open-attempt-id", attempt, "--session-root", str(sessions),
                       "--report", str(native_report),
                       "--benchmark-context", str(args.context), "--benchmark-test-root", str(args.test_root)]
            command += ["--handoff-check-report", str(browser_report)]
            if fault:
                command += ["--diagnostic-fault", fault]
            if name == "tile_503":
                command.append("--diagnostic-close-after-present")
            if name == "manual_handoff":
                command.append("--diagnostic-handoff-after-present")
            command.append(str(args.source))
            log = (directory / f"parent-{index + 1}.log").open("x", encoding="utf-8")
            handles.append(log)
            processes.append(subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        # Parent processes run only the native stage. The fallback child is a
        # separate process, identified exclusively by this case's private reports.
        codes = [process.wait(timeout=args.timeout) for process in processes]
        expected_codes = [0, 5] if count == 2 else [0]
        assert sorted(codes) == expected_codes, f"Unexpected parent exits: {codes}"
        # A duplicate may be rejected before it creates a native session/report,
        # or after both native sessions started. Both are correct exactly-once.
        existing_native_paths = [path for path in native_paths if path.exists()]
        assert len(existing_native_paths) in ((1, 2) if count == 2 else (1,))
        native_reports = [read_json(path) for path in existing_native_paths]
        for report in native_reports:
            assert report["outcome"]["kind"] == expected_kind
            assert report["outcome"].get("failure_kind") == expected_failure
            assert report["session"]["role"] == "agent"
            assert report["open_attempt_id"] == attempt
            assert report["fallback_consumed"] is False
            assert report["lifecycle"]["active_gpu_commands"] == 0
            assert report["lifecycle"]["finalized"] is True
            assert read_json(Path(report["session"]["root"]) / "session.json")["state"] == "closed"
        browser = None
        if fallback:
            browser = wait_for(lambda: read_json(browser_report) if browser_report.stat().st_size else None,
                               args.timeout, f"Fallback child did not publish a result: {browser_report}")
            assert browser.get("ok") is True, browser.get("error")
            window = browser["window_context"]
            assert window["role"] == "agent"
            assert window["open_attempt_id"] == attempt
            assert window["fallback_consumed"] is True
            assert window["id"] not in {report["session"]["id"] for report in native_reports}
            if native_reports[0].get("view_state") is not None:
                assert browser["handoff_requested"] == native_reports[0]["view_state"]
                assert browser["handoff_applied"] is True
            else:
                assert browser["slide"] is True
        else:
            assert not browser_report.exists(), "Unexpected fallback report after close/recovered 503"
        records = assert_closed_sessions(sessions, len(native_reports) + int(fallback))
        assert len({item["id"] for item in records}) == len(records)
        assert all(item["role"] == "agent" for item in records)
        counts = ledger_counts(ledger / "failures.sqlite3")
        assert counts["failures"] == 0, "Diagnostic faults must never persist negative failure records"
        assert counts["fallbacks"] == int(fallback), "Expected exactly one durable fallback claim"
        if name == "tile_503":
            assert native_reports[0]["outcome"]["first_presented"] is True
            assert native_reports[0]["backpressure"]["http_503_responses"] > 0
            assert native_reports[0]["backpressure"]["exhausted_tile_retries"] == 0
        if name == "manual_handoff":
            assert native_reports[0]["view_state"] is not None
        for path in existing_native_paths:
            assert read_json(path.with_suffix(".source.json"))["preservation"]["unchanged"] is True
        result = {"ok": True, "case": name, "open_attempt_id": attempt, "parent_exit_codes": codes,
                  "native_reports": [str(path) for path in native_paths],
                  "browser_report": str(browser_report) if fallback else None,
                  "session_ids": [item["id"] for item in records], "ledger_counts": counts,
                  "elapsed_seconds": time.monotonic() - started}
    except Exception as error:
        result = {"ok": False, "case": name, "error": str(error),
                  "native_reports": [str(path) for path in native_paths],
                  "sessions_root": str(sessions), "elapsed_seconds": time.monotonic() - started}
        # Terminate only Popen objects this script itself launched. Detached
        # fallback children retain their own diagnostic close/deadline policy;
        # never kill a process found only by app name or a stale PID file.
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    result.setdefault("still_running_parent_pids", []).append(process.pid)
    finally:
        for handle in handles:
            handle.close()
    write_new(directory / "result.json", result)
    return result


def run_browser_failure(args, executable, environment, directory, sessions, ledger, attempt):
    """A consumed explicit browser failure ends in this one diagnostic process."""
    report_path = directory / "browser-failure.json"
    missing = args.test_root / f"absent-rc2-diagnostic-{uuid.uuid4().hex}.nd2"
    assert not missing.exists()
    command = [str(executable), "--agent-window", "--renderer", "browser", "--fallback-consumed",
               "--open-attempt-id", attempt, "--browser-replay-report", str(report_path),
               "--benchmark-context", str(args.context), "--benchmark-test-root", str(args.test_root), str(missing)]
    with (directory / "browser-failure.log").open("x", encoding="utf-8") as log:
        process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            raise
    report = read_json(report_path)
    assert code == 4 and report.get("ok") is False
    window = report["window_context"]
    assert window["role"] == "agent" and window["open_attempt_id"] == attempt
    assert window["fallback_consumed"] is True
    records = assert_closed_sessions(sessions, 1)
    assert ledger_counts(ledger / "failures.sqlite3") == {"failures": 0, "fallbacks": 0}
    result = {"ok": True, "case": "browser_failure", "open_attempt_id": attempt,
              "child_exit_code": code, "diagnostic_error": report.get("error"),
              "session_ids": [item["id"] for item in records], "browser_report": str(report_path)}
    write_new(directory / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True, help="Built candidate .app, never an active User app")
    parser.add_argument("--source", type=Path, required=True, help="Existing multichannel isolated source copy")
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True, help="Explicit packaged replay context JSON")
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing output is refused")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--timeout", type=float, default=150)
    args = parser.parse_args()
    args.app, args.source, args.test_root, args.context = (
        path.expanduser().resolve(strict=True) for path in (args.app, args.source, args.test_root, args.context))
    args.output = args.output.expanduser().resolve()
    if not args.source.is_relative_to(args.test_root):
        parser.error("Source must be inside the explicit isolated test root")
    if args.app.parent == Path("/Applications"):
        parser.error("Use an isolated candidate, not the installed app")
    executable = args.app / "Contents/MacOS/nd2wsi-viewer"
    if not executable.is_file():
        parser.error("Candidate has no nd2wsi-viewer executable")
    context = read_json(args.context)
    if not all(key in context for key in ("viewport_points", "backing_scale")):
        parser.error("Replay context needs explicit viewport_points and backing_scale")
    if len(args.cases) != len(set(args.cases)):
        parser.error("Duplicate case names would overwrite results")
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("ND2WSI_VIEWPORT_") and key != "MTL_CAPTURE_ENABLED"}
    results = []
    for name in args.cases:
        result = run_case(name, args, executable, environment)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if not result["ok"]:
            break  # Do not stack more windows on an unexplained failure.
    report = {"schema": "nd2wsi-rc2-packaged-lifecycle/1", "app": str(args.app),
              "source": str(args.source), "cases": results,
              "ok": len(results) == len(args.cases) and all(row["ok"] for row in results),
              "scope": "Packaged native/browser lifecycle and isolated session checks; not end-to-end presentation performance."}
    write_new(args.output / "summary.json", report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
