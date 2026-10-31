"""One tracing window per user; a private claim for each Agent session."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .session_lock import SessionFileLock


def tracing_claim(session=None):
    if getattr(session, "role", None) == "agent":
        base = Path(session.root)
    elif session is not None:
        base = Path(session.root).parent
    else:
        override = os.environ.get("ND2WSI_WINDOW_SESSION_ROOT")
        if override:
            base = Path(override).expanduser().resolve()
        elif sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "nd2wsi-viewer" / "window-sessions"
        else:
            base = Path.home() / "Library" / "Application Support" / "nd2wsi-viewer" / "window-sessions"
    return SessionFileLock(base / ".tracing-window.lock")
