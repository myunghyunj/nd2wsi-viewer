"""macOS RC entry: measured defaults, explicit Metal and one safe fallback.

Agents must launch --agent-window, never adopt an existing User window. Every
window has an independent process/session. Standard viewing remains available
for annotations, measurements, export, RGB/SVS, plate and unsupported inputs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__
from .renderer_policy import FailureLedger, open_attempt_id, should_try_metal, source_fingerprint


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


def spawn_browser(arguments):
    """Never reuse an AppKit event loop or adopt an existing User window."""
    environment = os.environ.copy()
    environment.update(PYINSTALLER_RESET_ENVIRONMENT="1", ND2WSI_WINDOW_CHILD="1")
    for key in ("ND2WSI_VIEWPORT_REPLAY", "ND2WSI_VIEWPORT_AUTOQUIT",
                "ND2WSI_VIEWPORT_CAPTURE_AFTER_REPLAY", "MTL_CAPTURE_ENABLED"):
        environment.pop(key, None)
    return subprocess.Popen(command_prefix() + arguments, env=environment,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _ledger_call(operation, *arguments, default=None):
    try:
        return operation(*arguments)
    except (OSError, sqlite3.Error):
        # Local failure state never includes a slide path or arbitrary driver
        # error in remote/release logs. A broken ledger is not permission to
        # touch source data or migrate cache state.
        print("Renderer failure state is unavailable.", file=sys.stderr)
        return default


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
    parser.add_argument("--prefer-metal", action="store_true", help="try Metal with safe standard fallback")
    parser.add_argument("--retry-metal", action="store_true", help="explicitly bypass this file/version failure record once")
    parser.add_argument("--open-attempt-id")
    parser.add_argument("--fallback-consumed", action="store_true")
    parser.add_argument("--handoff-state", type=Path)
    parser.add_argument("--diagnostic-fault", choices=("metadata_timeout", "metadata_invalid", "gpu_fatal", "gpu_memory_pressure",
                                                       "tile_503", "all_tiles_failed", "close_race"))
    parser.add_argument("--diagnostic-close-after-present", action="store_true")
    parser.add_argument("--diagnostic-handoff-after-present", action="store_true")
    parser.add_argument("--browser-replay-report", type=Path)
    parser.add_argument("--benchmark-context", type=Path)
    parser.add_argument("--benchmark-test-root", type=Path)
    parser.add_argument("--handoff-check-report", type=Path)
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
    role = "agent" if args.agent_window else "user"
    if (args.diagnostic_fault or args.diagnostic_close_after_present or args.diagnostic_handoff_after_present) and role != "agent":
        parser.error("fault injection requires a new Agent window")
    attempt = open_attempt_id(args.open_attempt_id)
    source = args.source
    requested = should_try_metal(role=role, renderer=args.renderer, prefer_metal=args.prefer_metal,
                                retry_metal=args.retry_metal, source_info=args.source_info,
                                fallback_consumed=args.fallback_consumed)
    strict = args.renderer == "metal" or args.source_info
    use_metal = requested and (args.source_info or metal_available())
    if requested and strict and not use_metal:
        raise RuntimeError("Metal diagnostics require a supported Apple silicon device")
    if use_metal and source is None:
        from .metal_viewport.launcher import _choose_source

        source = _choose_source()
        if source is None:
            return 0
    role_flag = "--agent-window" if role == "agent" else "--new-window"
    browser_arguments = [role_flag, "--renderer", "browser", "--open-attempt-id", attempt]
    browser_diagnostics = []
    for option in ("browser_replay_report", "benchmark_context", "benchmark_test_root", "handoff_check_report"):
        if getattr(args, option):
            browser_diagnostics += ["--" + option.replace("_", "-"), str(getattr(args, option))]
    browser_arguments += browser_diagnostics
    if args.fallback_consumed:
        browser_arguments.append("--fallback-consumed")
    if args.handoff_state:
        browser_arguments += ["--handoff-state", str(args.handoff_state)]
    if source:
        browser_arguments.append(str(source))
    if not use_metal:
        return app.main(browser_arguments)
    initial_state = None
    if args.handoff_state:
        from .view_state import read_handoff

        initial_state = read_handoff(args.handoff_state, source=source, role=role)
    ledger = FailureLedger()
    # A replayed logical-open ID must not bypass the transition guard merely
    # because the first attempt has now created a persistent failure record.
    # The legitimate browser child carries --fallback-consumed and returned
    # through the explicit browser branch above, never this Metal entry.
    if _ledger_call(ledger.fallback_consumed, attempt, default=False):
        return 5
    fingerprint = None
    try:
        fingerprint = source_fingerprint(source)
    except OSError:
        pass  # unavailable source remains a non-persistent startup failure
    if not (strict or args.retry_metal or args.diagnostic_fault):
        if _ledger_call(ledger.kinds, fingerprint, __version__, default=set()):
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
        "standard": prefix + browser_arguments,
        "updates": prefix + ["--check-updates"],
    }
    try:
        result = launcher.main(native_arguments, role=role, window_commands=commands,
                               return_result=True, open_attempt_id=attempt,
                               fallback_consumed=args.fallback_consumed,
                               initial_view_state=initial_state,
                               prefer_metal=args.prefer_metal or args.retry_metal,
                               on_first_presented=(None if args.diagnostic_fault or args.source_info else
                                                   lambda: _ledger_call(ledger.clear, fingerprint, __version__)),
                               diagnostics=({"enabled": True, "fault": args.diagnostic_fault,
                                             "close_after_first_presented": args.diagnostic_close_after_present,
                                             "handoff_after_first_presented": args.diagnostic_handoff_after_present}
                                            if (args.diagnostic_fault or args.diagnostic_close_after_present
                                                or args.diagnostic_handoff_after_present) else {}))
    except launcher.NativeCleanupError:
        print("Metal source cleanup did not complete; no additional viewer was opened.", file=sys.stderr)
        return 5
    except (ValueError, OSError, RuntimeError) as error:
        if strict:
            raise
        # No arbitrary error message is persisted as a renderer failure kind.
        result = launcher.NativeLaunchResult(4, "fatal", "source_unavailable")
        print(f"Metal startup unavailable ({type(error).__name__}); using standard viewer.", file=sys.stderr)
    if isinstance(result, int):
        # Headless source inspection returns its normal exit status; it does
        # not render, clear failure records or create another process.
        return result
    if not args.diagnostic_fault:
        if result.first_presented and not result.presentation_notified:
            _ledger_call(ledger.clear, fingerprint, __version__)
        if result.kind == "fatal":
            _ledger_call(ledger.record, fingerprint, __version__, result.failure_kind)
    if result.kind == "closed" or (strict and result.kind != "handoff"):
        return result.code
    if result.kind not in ("fatal", "handoff") or args.fallback_consumed:
        return result.code or 5
    # This executes only after launcher has fully cleaned up its source/session.
    # SQLite also bars duplicate dispatch from the same logical-open ID in two
    # processes. If state cannot be recorded, fail closed instead of looping.
    if not _ledger_call(ledger.claim_fallback, attempt, default=False):
        return result.code or 5
    browser_arguments = [role_flag, "--renderer", "browser", "--open-attempt-id", attempt,
                         "--fallback-consumed", *browser_diagnostics]
    state = result.view_state or initial_state
    if state is not None:
        from .view_state import write_handoff

        # Use the failed window's private exports directory, never source/cache.
        state_root = result.report_path.parent if result.report_path else None
        try:
            state_path = write_handoff(state, source=source, role=role, root=state_root)
            browser_arguments += ["--handoff-state", str(state_path)]
        except (OSError, ValueError):
            # A disconnected source or full state disk must not cause a loop,
            # role change, or adoption of an unrelated user's existing window.
            print("Display state could not be preserved; opening the same source in the standard viewer.", file=sys.stderr)
    browser_arguments.append(str(source))
    try:
        spawn_browser(browser_arguments)
    except OSError:
        print("Could not start the standard viewer; this open request will not retry.", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
