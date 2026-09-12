"""Private, durable identity for one macOS native window/process.

Automation contract: open a fresh Agent window before interacting with slides.
Never repurpose a User window. Annotation snapshots and conflict drafts belong
to this session; there is no automatic merge into the user's source sidecars.
This separates app state, not the operating system's mouse/keyboard focus.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIRECTIVE = (
    "Automated agents must open a NEW Agent window using new_window('agent') "
    "or --agent-window before opening slides or changing viewer state. Confirm "
    "window_context().role == 'agent' and the new session id. Never reuse, "
    "close, retarget, or edit an existing User window. Save generated files "
    "under exports_root, and keep annotation snapshots and conflict drafts "
    "separate. Do not merge results into user annotations without explicit "
    "approval. OS mouse and keyboard focus is shared; prefer this window's "
    "scoped bridge/server and coordinate any foreground interaction."
)


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class WindowSession:
    role: str
    id: str
    root: Path
    endpoint: str | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def annotation_root(self) -> Path:
        return self.root / "annotations"

    @property
    def exports_root(self) -> Path:
        return self.root / "exports"

    @property
    def drafts_root(self) -> Path:
        return self.root / "drafts"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "root": str(self.root),
            "annotation_root": str(self.annotation_root),
            "exports_root": str(self.exports_root),
            "drafts_root": str(self.drafts_root),
            "annotation_mode": "isolated-snapshot" if self.role == "agent" else "shared-with-conflict-check",
            "agent_directive": AGENT_DIRECTIVE,
            "new_window_supported": True,
        }

    def _record(self, **changes) -> None:
        with self._lock:
            path = self.root / "session.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record.update(changes)
            _atomic_json(path, record)

    def record_endpoint(self, endpoint: str) -> None:
        self._record(endpoint=endpoint, state="ready")
        self.endpoint = endpoint

    def mark_closed(self) -> None:
        # Keep snapshots/drafts/results for recovery; closing never deletes data.
        self._record(state="closed", closed_at=dt.datetime.now(dt.UTC).isoformat())


def create_window_session(role: str, base_path: str | Path | None = None) -> WindowSession:
    """Create a fresh identity, never reopen or overwrite another session."""
    if role not in ("user", "agent"):
        raise ValueError("window role must be 'user' or 'agent'")
    if base_path is None:
        base_path = os.environ.get("ND2WSI_WINDOW_SESSION_ROOT") or (
            Path.home() / "Library" / "Application Support" / "nd2wsi-viewer" / "window-sessions"
        )
    base = Path(base_path).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    session_id = uuid.uuid4().hex
    root = base / session_id
    root.mkdir(mode=0o700, exist_ok=False)
    session = WindowSession(role=role, id=session_id, root=root)
    for directory in (session.annotation_root, session.exports_root, session.drafts_root):
        directory.mkdir(mode=0o700)
    _atomic_json(root / "session.json", {
        "format": "nd2wsi-window-session/1",
        **session.as_dict(),
        "pid": os.getpid(),
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "state": "starting",
        "endpoint": None,
    })
    return session
