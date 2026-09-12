"""Agent-scoped launcher; the existing cross-platform browser entry is intact."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import sys
from pathlib import Path

from ..window_sessions import create_window_session

APP_NAME = "nd2wsi-viewer"
BUNDLE_ID = "com.nd2wsi.viewer"


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


def main(argv=None, *, role="agent", window_commands=None):
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
                   "window_commands": window_commands or {}}
        return library.nd2wsi_viewport_run(base_url.encode(), json.dumps(context).encode(),
                                          str(report.resolve()).encode())
    finally:
        try:
            if server is not None:
                server.close()
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
