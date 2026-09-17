"""Coordinate live windows before handing application replacement to Sparkle."""
from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

from .session_lock import SessionFileLock


def _coordination_root(base: Path) -> Path:
    # Packaged windows share one gate even when diagnostics use private session
    # roots. Development/test runs coordinate only inside their supplied root.
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        return Path.home() / "Library" / "Application Support" / "nd2wsi-viewer" / "window-sessions"
    return base


def _installation_lock(base: Path) -> SessionFileLock:
    return SessionFileLock(_coordination_root(base) / ".update-install.lock")


def session_lifetime(base: Path, session_id: str) -> SessionFileLock:
    """Register liveness while the caller holds window_admission's gate."""
    lock = SessionFileLock(_coordination_root(base) / f".window-live-{session_id}.lock")
    lock.acquire()
    return lock


@contextmanager
def window_admission(base: Path):
    gate = _installation_lock(base)
    try:
        gate.acquire(timeout=2)
    except TimeoutError:
        raise RuntimeError("An application update is being installed. Open the viewer again after it finishes.") from None
    try:
        yield
    finally:
        gate.release()


def running_bundle_peers() -> list[int]:
    """Include older installed processes and Metal windows using another root."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return []
    from AppKit import NSRunningApplication
    from Foundation import NSBundle

    identifier = NSBundle.mainBundle().bundleIdentifier()
    if not identifier:
        raise RuntimeError("cannot identify the application for safe installation")
    return [int(app.processIdentifier()) for app in
            NSRunningApplication.runningApplicationsWithBundleIdentifier_(identifier)
            if int(app.processIdentifier()) != os.getpid() and not app.isTerminated()]


class UpdateInstallGuard:
    """Hold launch admission from the final peer check through handoff/cancel.

    Existing windows must close normally; this guard never kills a process or
    changes its saved-state record. Dead/closed recovery folders are retained.
    Kernel liveness avoids treating a reused PID as an old, crashed window.
    After this process exits, Sparkle owns replacement and relaunch.
    """
    def __init__(self, session, *, bundle_peers=running_bundle_peers):
        self.session = session
        self.base = _coordination_root(Path(session.root).parent)
        self.gate = SessionFileLock(self.base / ".update-install.lock")
        self.bundle_peers = bundle_peers

    def acquire(self) -> str | None:
        if self.gate.acquired:
            return None
        try:
            self.gate.acquire()
        except TimeoutError:
            return "Another viewer window is preparing an update."
        try:
            for path in self.base.glob(".window-live-*.lock"):
                match = re.fullmatch(r"\.window-live-([0-9a-f]{32})\.lock", path.name)
                if match is None or match[1] == self.session.id:
                    continue
                probe = SessionFileLock(path)
                try:
                    probe.acquire()
                except TimeoutError:
                    self.release()
                    return "Close the other viewer windows before installing. Their unsaved work will be checked when you close them."
                finally:
                    probe.release()
            # Older builds have no lifetime lock. AppKit's current process list
            # is authoritative for them, not retained session.json PID values.
            if self.bundle_peers():
                self.release()
                return "Close the other viewer windows before installing this update."
            return None
        except Exception:
            self.release()
            return "Could not confirm that the other viewer windows are closed. Installation is waiting."

    def release(self) -> None:
        self.gate.release()
