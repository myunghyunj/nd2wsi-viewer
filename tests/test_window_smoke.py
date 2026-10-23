"""Owned-child and evidence contracts for the packaged Windows launch check."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from nd2wsi import app, window_launch, window_smoke
from nd2wsi.window_sessions import create_window_session


@pytest.mark.parametrize("failure", [None, "unclosed", "reused-identity", "wrong-pid", "timeout"])
def test_independent_smoke_uses_real_bridge_launch_and_only_owns_its_child(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    parent = create_window_session("agent", tmp_path / "parent-sessions")
    api = app.Api(None, window_session=parent)
    child = Mock(pid=os.getpid() + 1000)
    child.poll.return_value = None if failure == "timeout" else 0

    def spawn(command, **kwargs):
        assert command[:2] == [sys.executable, "--agent-window"]
        assert command[2:4] == ["--gui-smoke", "--smoke-report"]
        environment = kwargs["env"]
        child_root = Path(environment["ND2WSI_WINDOW_SESSION_ROOT"]) / ("1" * 32)
        child_root.mkdir(parents=True)
        context = {"enabled": True, "role": "agent", "id": child_root.name,
                   "pid": child.pid, "root": str(child_root)}
        if failure == "reused-identity":
            context["id"] = parent.id
        if failure == "wrong-pid":
            context["pid"] += 1
        record = {**context, "state": "ready" if failure == "unclosed" else "closed"}
        (child_root / "session.json").write_text(json.dumps(record), encoding="utf-8")
        Path(command[4]).write_text(json.dumps({"ok": True, "boot": True, "slide": False,
                                              "window_context": context}), encoding="utf-8")
        child.wait.side_effect = [subprocess.TimeoutExpired(command, 1), -15] if failure == "timeout" else None
        child.wait.return_value = 0
        return child

    real_popen = Mock(side_effect=spawn)
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    try:
        result = window_smoke.exercise_independent_window(api, timeout=1)
        assert result["ok"] is (failure is None)
        assert window_launch.subprocess.Popen is real_popen
        real_popen.assert_called_once()
        assert result["launch"]["command"] == [sys.executable, "--agent-window"]
        assert result["launch"]["creationflags"] == 520
        assert result["launch"]["independent_runtime"] is True
        if failure is None:
            assert result["child_closed"] and result["distinct_process_and_session"]
            assert result["child_session"]["state"] == "closed"
        if failure == "timeout":
            child.terminate.assert_called_once()
            assert result["owned_child_cleanup"] is True
        else:
            child.terminate.assert_not_called()
        child.kill.assert_not_called()
    finally:
        parent.mark_closed()
