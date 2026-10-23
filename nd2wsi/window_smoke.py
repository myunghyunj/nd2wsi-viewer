"""Bounded independent-window verification for Windows package GUI smoke.

The real bridge and launcher start a child. Only this diagnostic adds the
existing blank-window smoke arguments so that child can verify and close
itself. It never discovers, adopts or closes another running viewer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def exercise_independent_window(api, *, timeout=100):
    from . import window_launch

    report = {"ok": False, "platform": sys.platform,
              "scope": "real new_window Agent launch with diagnostic auto-close arguments"}
    child = None
    if sys.platform != "win32":
        return {**report, "error": "This packaged independent-window check is Windows-only"}
    with tempfile.TemporaryDirectory(prefix="nd2wsi-window-smoke-") as temporary:
        root = Path(temporary)
        sessions = root / "sessions"
        child_report_path = root / "child.json"
        child_log_path = root / "child.log"
        original_popen = window_launch.subprocess.Popen
        expected = window_launch.build_window_command("agent")

        def launch_with_diagnostic_exit(command, **kwargs):
            nonlocal child
            if child is not None or list(command) != expected:
                raise ValueError("Independent-window smoke received an unexpected child command")
            environment = kwargs["env"]
            if (environment.get("PYINSTALLER_RESET_ENVIRONMENT") != "1"
                    or environment.get("ND2WSI_WINDOW_CHILD") != "1"):
                raise ValueError("Child does not own an independent application runtime")
            required = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            if kwargs.get("creationflags", 0) & required != required or not kwargs.get("close_fds"):
                raise ValueError("Windows child is not detached from its parent")
            if any(kwargs.get(key) != subprocess.DEVNULL for key in ("stdin", "stdout", "stderr")):
                raise ValueError("Child inherited parent input or output")
            report["launch"] = {
                "command": list(command), "creationflags": kwargs["creationflags"],
                "independent_runtime": True, "detached_io": True,
                "diagnostic_arguments": ["--gui-smoke", "--smoke-report", str(child_report_path)],
            }
            # Keep diagnostic reports/logs separate from the parent's evidence.
            kwargs["env"] = {**environment, "ND2WSI_WINDOW_SESSION_ROOT": str(sessions),
                             "ND2WSI_LOG_FILE": str(child_log_path), "ND2WSI_DISABLE_UPDATES": "1"}
            child = original_popen([*command, "--gui-smoke", "--smoke-report", str(child_report_path)], **kwargs)
            return child

        try:
            parent = api.window_context()
            if not parent.get("enabled") or not parent.get("id") or parent.get("pid") != os.getpid():
                raise ValueError("Parent has no process-bound native window identity")
            window_launch.subprocess.Popen = launch_with_diagnostic_exit
            try:
                result = api.new_window("agent")
            finally:
                window_launch.subprocess.Popen = original_popen
            if not result.get("ok") or child is None:
                raise RuntimeError(result.get("message", "Independent-window launch failed"))
            report["launch"]["pid"] = result["pid"]
            report["exit_code"] = child.wait(timeout=timeout)
            if report["exit_code"] != 0:
                raise RuntimeError(f"Independent child exited with code {report['exit_code']}")
            evidence = json.loads(child_report_path.read_text(encoding="utf-8"))
            context = evidence.get("window_context", {})
            child_root = Path(context.get("root", "")).resolve()
            if (not evidence.get("ok") or not evidence.get("boot") or evidence.get("slide")
                    or context.get("role") != "agent" or not context.get("enabled")
                    or context.get("id") == parent["id"] or context.get("pid") == parent["pid"]
                    or context.get("pid") != result["pid"] or result["pid"] != child.pid
                    or child_root.parent != sessions.resolve() or child_root.name != context.get("id")):
                raise ValueError("Child did not verify a fresh blank Agent window")
            record = json.loads((child_root / "session.json").read_text(encoding="utf-8"))
            if (record.get("id") != context["id"] or record.get("role") != "agent"
                    or record.get("pid") != context.get("pid") or record.get("state") != "closed"):
                raise ValueError("Child did not close its own isolated window session")
            report.update(ok=True, child_gui=evidence, child_session=record,
                          distinct_process_and_session=True, child_closed=True)
        except Exception as exc:
            report["error"] = str(exc)
            if child_log_path.exists():
                report["child_log_tail"] = child_log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        finally:
            window_launch.subprocess.Popen = original_popen
            if child is not None and child.poll() is None:
                # Only the handle created above is owned by this smoke check.
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                report["owned_child_cleanup"] = True
    return report
