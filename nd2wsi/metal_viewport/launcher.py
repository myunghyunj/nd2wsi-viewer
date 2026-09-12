"""Agent-scoped launcher; the existing cross-platform browser entry is intact."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..window_sessions import create_window_session

APP_NAME = "nd2wsi-viewer"
BUNDLE_ID = "com.nd2wsi.viewer"


class NativeCleanupError(RuntimeError):
    """Cleanup did not complete; starting another reader would be unsafe."""


@dataclass(frozen=True)
class NativeLaunchResult:
    code: int
    kind: str = "closed"
    failure_kind: str | None = None
    first_presented: bool = False
    view_state: dict | None = None
    report_path: Path | None = None
    diagnostics: dict = field(default_factory=dict)
    presentation_notified: bool = False


class _PresentationObserver:
    """Observe the one atomic first-image report; never sample GUI state."""

    def __init__(self, report: Path, callback):
        self.report, self.callback = report, callback
        self.stopped, self.notified = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="metal-first-present")

    def _run(self):
        while not self.stopped.wait(.05):
            try:
                if self.report.stat().st_size > 32 * 1024 * 1024:
                    return
                payload = json.loads(self.report.read_text(encoding="utf-8"))
                if payload.get("outcome", {}).get("first_presented") is True:
                    self.notified.set()
                    self.callback()
                    return
            except (OSError, ValueError, AttributeError):
                continue  # atomic writer has not published a ready report yet

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)


def _launch_result(code: int, report: Path, diagnostics: dict) -> NativeLaunchResult:
    try:
        # This is the exclusively reserved, private report for this launch.
        if report.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("native report exceeds limit")
        payload = json.loads(report.read_text(encoding="utf-8"))
        outcome = payload.get("outcome", {})
        kind = outcome.get("kind", "fatal" if code in (4, 5) else "closed")
        if kind not in ("closed", "fatal", "handoff"):
            raise ValueError("invalid native outcome")
        state = outcome.get("view_state") or payload.get("view_state")
        if state is not None:
            from ..view_state import validate_view_state

            state = validate_view_state(state)
        return NativeLaunchResult(code, kind, outcome.get("failure_kind"),
                                  outcome.get("first_presented") is True, state, report, diagnostics)
    except (OSError, ValueError, TypeError, AttributeError):
        return NativeLaunchResult(code or 5, "fatal", "metadata_invalid", report_path=report,
                                  diagnostics=diagnostics)


def load_native():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("Metal viewport beta requires an Apple silicon Mac")
    path = Path(__file__).with_name("libnd2wsi_viewport.dylib")
    if not path.is_file():
        raise RuntimeError("Native viewport is not built; run metal_viewport/build_native.sh")
    library = ctypes.CDLL(str(path))
    library.nd2wsi_viewport_run.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    library.nd2wsi_viewport_run.restype = ctypes.c_int
    return library


def _choose_source():
    # The panel belongs only to this distinct native beta process. Never target
    # another running viewer or change its file selection through automation.
    from AppKit import NSApplication, NSModalResponseOK, NSOpenPanel

    application = NSApplication.sharedApplication()
    application.setActivationPolicy_(0)
    panel = NSOpenPanel.openPanel()
    panel.setTitle_("Open slide — new window")
    panel.setMessage_("Compatible fluorescence slides use Metal. Other slides open in the standard viewer.")
    panel.setCanChooseDirectories_(False)
    panel.setAllowsMultipleSelection_(False)
    application.activateIgnoringOtherApps_(True)
    if panel.runModal() != NSModalResponseOK:
        return None
    return Path(panel.URL().path())


def main(argv=None, *, role="agent", window_commands=None, return_result=False,
         open_attempt_id=None, fallback_consumed=False, initial_view_state=None,
         diagnostics=None, prefer_metal=False, on_first_presented=None):
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("source", nargs="?", type=Path)
    parser.add_argument("--agent-window", action="store_true", help="always implicit in this beta")
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--source-info", action="store_true", help="inspect read-only metadata without a GUI")
    parser.add_argument("--capture-enabled", action="store_true", help="enable diagnostic GPU capture; benchmark separately")
    args = parser.parse_args(argv)
    if role not in ("user", "agent"):
        parser.error("invalid window role")
    if diagnostics and role != "agent":
        parser.error("fault injection requires a new Agent window")
    from ..renderer_policy import open_attempt_id as normalize_attempt

    attempt = normalize_attempt(open_attempt_id)
    if sys.platform != "darwin" or platform.machine() != "arm64":
        parser.error("this optional native viewport requires macOS on Apple silicon")
    if args.capture_enabled:
        os.environ["MTL_CAPTURE_ENABLED"] = "1"
    library = None if args.source_info else load_native()
    source = args.source or _choose_source()
    if source is None:
        return 0
    if args.report and (args.report.exists() or args.report.with_suffix(".source.json").exists()):
        parser.error("choose a new report path; existing measurements are never overwritten")
    session = create_window_session(role, base_path=args.session_root)
    report = args.report or session.exports_root / "viewport-report.json"
    companion = report.with_suffix(".source.json")
    server = None
    evidence_handle = None
    observer = None
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        # Reserve both outputs exclusively before opening a source or window.
        # Native writes atomically replace only the report this launch owns.
        with report.open("x", encoding="utf-8") as handle:
            json.dump({"state": "starting", "session": session.as_dict()}, handle)
        evidence_handle = companion.open("x", encoding="utf-8")
        from .source import create_source_server

        if role == "user":
            server, base_url = create_source_server(source, session, allow_user=True)
        else:
            server, base_url = create_source_server(source, session)
        session.record_endpoint(base_url)
        if args.source_info:
            print(json.dumps(server.source.metadata(), indent=2, ensure_ascii=False))
            return 0
        context = {**session.as_dict(), "app_name": APP_NAME, "bundle_id": BUNDLE_ID,
                   "annotation_mode": "read-only-snapshot",
                   "source_path": str(source.resolve()), "report_path": str(report.resolve()),
                   "window_commands": window_commands or {},
                   "open_attempt_id": attempt, "fallback_consumed": bool(fallback_consumed),
                   "initial_view_state": initial_view_state, "diagnostics": diagnostics or {},
                   "prefer_metal": bool(prefer_metal)}
        if on_first_presented is not None:
            observer = _PresentationObserver(report, on_first_presented)
            observer.thread.start()
        code = library.nd2wsi_viewport_run(base_url.encode(), json.dumps(context).encode(),
                                         str(report.resolve()).encode())
        result = _launch_result(code, report, diagnostics or {}) if return_result else code
        if return_result and observer and observer.notified.is_set():
            from dataclasses import replace

            result = replace(result, presentation_notified=True)
        # Python does not hand control to desktop until the finally block below
        # has closed source requests/readers and this specific native session.
        return result
    finally:
        if observer is not None:
            observer.close()
        try:
            if server is not None:
                try:
                    server.close()
                except Exception as error:
                    raise NativeCleanupError(str(error)) from error
                evidence = {"source": server.source.metrics(),
                            "preservation": server.source.preservation_report(),
                            "session": session.as_dict()}
            else:
                evidence = {"state": "source_not_opened", "session": session.as_dict()}
            if evidence_handle is not None:
                json.dump(evidence, evidence_handle, indent=2, ensure_ascii=False)
                evidence_handle.write("\n")
        finally:
            try:
                if evidence_handle is not None:
                    evidence_handle.close()
            finally:
                session.mark_closed()


if __name__ == "__main__":
    raise SystemExit(main())
