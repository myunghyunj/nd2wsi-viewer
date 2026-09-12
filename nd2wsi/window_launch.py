"""Launch one independent macOS process per native viewer window.

Never forward the current process's argv: a child must not inherit an opening
slide, a smoke-test exit, or another window's session. The child creates its own
session and local server. There is intentionally no shutdown/kill ownership of
children in the parent process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def build_window_command(
    role: str = "agent", *, executable: str | None = None,
    frozen: bool | None = None, source_launcher: str | None = None,
) -> list[str]:
    if role not in ("user", "agent"):
        raise ValueError("Window role must be 'user' or 'agent'")
    executable = executable or sys.executable
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    flag = "--agent-window" if role == "agent" else "--new-window"
    if frozen:
        return [executable, flag]
    launcher = source_launcher or os.environ.get("ND2WSI_WINDOW_LAUNCHER")
    if launcher:
        return [executable, str(Path(launcher).expanduser().resolve()), flag]
    return [executable, "-m", "nd2wsi.app", flag]


def child_environment(*, app_name: str | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    # PyInstaller >=6.9 otherwise treats the same executable as a worker which
    # may reuse the parent's temporary runtime. A new top-level bootloader must
    # own its runtime so closing the parent cannot invalidate the child.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    environment["ND2WSI_WINDOW_CHILD"] = "1"
    if app_name:
        environment["ND2WSI_APP_NAME"] = app_name
    return environment


def launch_window(role: str = "agent", *, app_name: str | None = None) -> dict:
    if sys.platform != "darwin":
        return {"ok": False, "message": "Independent windows are macOS-only in this beta."}
    try:
        frozen = bool(getattr(sys, "frozen", False))
        command = build_window_command(role, frozen=frozen)
        environment = child_environment(app_name=app_name)
        kwargs = {}
        if not frozen:
            source_root = str(Path(__file__).resolve().parents[1])
            # Script launchers have packaging/ as sys.path[0]; explicitly keep
            # this checkout first even when another version is installed.
            inherited = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = source_root + (os.pathsep + inherited if inherited else "")
            kwargs["cwd"] = source_root
        process = subprocess.Popen(
            command, env=environment, start_new_session=True, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs,
        )
        return {"ok": True, "pid": process.pid, "role": role}
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": f"Could not open the new window: {exc}"}
