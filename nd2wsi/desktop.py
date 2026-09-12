"""macOS RC entry: automatic compatible Metal display, intact browser fallback.

Agents must launch --agent-window, never adopt an existing User window. Every
window has an independent process/session. Standard viewing remains available
for annotations, measurements, export, RGB/SVS, plate and unsupported inputs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path


def metal_available():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    try:
        from .metal import device_info

        return bool(device_info().get("supported"))
    except (OSError, RuntimeError):
        return False


def command_prefix():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "nd2wsi.desktop"]


def main(argv=None):
    from . import app

    arguments = list(sys.argv[1:] if argv is None else argv)
    # Existing scientific/package smoke paths and all non-macOS behavior remain
    # exactly in the established application. This RC ships no Windows update.
    if sys.platform != "darwin" or any(x in arguments for x in ("--smoke", "--gui-smoke")):
        return app.main(arguments)
    parser = argparse.ArgumentParser(prog="nd2wsi-viewer")
    parser.add_argument("source", nargs="?", type=Path)
    parser.add_argument("--renderer", choices=("auto", "metal", "browser"), default="auto")
    roles = parser.add_mutually_exclusive_group()
    roles.add_argument("--agent-window", action="store_true")
    roles.add_argument("--new-window", action="store_true")
    parser.add_argument("--check-updates", action="store_true")
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--capture-enabled", action="store_true")
    parser.add_argument("--source-info", action="store_true")
    args, _ = parser.parse_known_args(arguments)
    if args.check_updates:
        from .release_selection import check_for_updates

        result = check_for_updates()
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("available"):
            from AppKit import NSAlert, NSApplication

            NSApplication.sharedApplication()
            alert = NSAlert.new()
            alert.setMessageText_("nd2wsi-viewer updates")
            alert.setInformativeText_(result.get("message", "No compatible update found."))
            alert.runModal()
        return 0 if result.get("ok") else 1
    os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer"
    os.environ["ND2WSI_WINDOW_LAUNCHER"] = str(Path(__file__).resolve().parents[1] / "packaging" / "launch_rc.py")
    if args.session_root:
        os.environ["ND2WSI_WINDOW_SESSION_ROOT"] = str(args.session_root)
    source = args.source
    use_metal = args.renderer != "browser" and metal_available()
    if use_metal and source is None:
        from .metal_viewport.launcher import _choose_source

        source = _choose_source()
        if source is None:
            return 0
    role_flag = "--agent-window" if args.agent_window else "--new-window"
    browser_arguments = [role_flag] + ([str(source)] if source else [])
    if not use_metal:
        return app.main(browser_arguments)
    os.environ.setdefault("ND2WSI_GPU_PYRAMID", "1")
    from .metal_viewport import launcher

    native_arguments = [str(source)]
    for option, value in (("--session-root", args.session_root), ("--report", args.report)):
        if value:
            native_arguments += [option, str(value)]
    for option in ("capture_enabled", "source_info"):
        if getattr(args, option):
            native_arguments.append("--" + option.replace("_", "-"))
    prefix = command_prefix()
    commands = {
        "user": prefix + ["--new-window"],
        "agent": prefix + ["--agent-window"],
        "standard": prefix + [role_flag, "--renderer", "browser", str(source)],
        "updates": prefix + ["--check-updates"],
    }
    try:
        result = launcher.main(native_arguments, role="agent" if args.agent_window else "user",
                               window_commands=commands)
        # Code 4 is emitted before the native event loop starts (device,
        # pipeline or metadata setup failed). It is safe to create the normal
        # viewer only after launcher.main has closed its source and session.
        if result == 4 and args.renderer == "auto" and not args.source_info:
            return app.main(browser_arguments)
        return result
    except (ValueError, OSError, RuntimeError) as error:
        if args.renderer == "metal" or args.source_info:
            raise
        # Only startup errors reach this branch; once a native window runs its
        # event loop we never transfer its state to another process/window.
        print(f"Metal source unavailable; opening standard viewer: {error}", file=sys.stderr)
        return app.main(browser_arguments)


if __name__ == "__main__":
    raise SystemExit(main())
