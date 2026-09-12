"""Local, auditable RC2 selection and once-per-open failure bookkeeping.

No research filenames, paths or arbitrary error messages enter this database.
The sampled fingerprint is a stale-source identity, not a full content hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

# Changing a version number or supplying an environment variable cannot pass
# this gate. Comparable packaged end-to-end measurements are still unavailable.
PERFORMANCE_GATE = {
    "status": "unverified",
    "agent_auto_metal": False,
    "validated_hardware": [],
    "reason": "No equivalent packaged browser/Metal presentation endpoint comparison",
}
PERSISTENT_FAILURE_KINDS = frozenset({
    "gpu_device_unavailable", "gpu_pipeline_failure", "gpu_execution_failure",
})
TRANSIENT_FAILURE_KINDS = frozenset({
    "metadata_timeout", "metadata_invalid", "tile_unavailable", "memory_pressure",
    "source_unavailable", "cache_missing", "http_503", "cancelled",
})


def should_try_metal(*, role: str, renderer: str = "auto", prefer_metal: bool = False,
                     retry_metal: bool = False, source_info: bool = False,
                     fallback_consumed: bool = False) -> bool:
    if role not in ("user", "agent") or renderer not in ("auto", "browser", "metal"):
        raise ValueError("invalid renderer selection")
    if fallback_consumed or renderer == "browser":
        return False
    if renderer == "metal" or source_info or prefer_metal or retry_metal:
        return True
    # Neither User nor Agent becomes automatic merely because an RC ships.
    return False


def open_attempt_id(value: str | None = None) -> str:
    try:
        return uuid.UUID(value).hex if value else uuid.uuid4().hex
    except (ValueError, AttributeError) as error:
        raise ValueError("open_attempt_id must be a UUID") from error


def source_fingerprint(path: str | Path) -> str | None:
    """Reuse the head/middle/tail sampled hash; directories have no ledger key."""
    from .cache import quick_fingerprint

    path = Path(path)
    if not path.is_file():
        return None
    fingerprint = quick_fingerprint(path)
    anonymous = {key: fingerprint[key] for key in ("size", "mtime_ns", "quick_sha256")}
    return hashlib.sha256(json.dumps(anonymous, sort_keys=True).encode()).hexdigest()


class FailureLedger:
    """Per-operation SQLite transactions are safe across independent windows."""

    def __init__(self, root: str | Path | None = None):
        root = root or os.environ.get("ND2WSI_RENDERER_STATE_ROOT") or (
            Path.home() / "Library" / "Application Support" / "nd2wsi-viewer" / "renderer-state"
        )
        self.root = Path(root).expanduser()
        self.path = self.root / "failures.sqlite3"

    def _connect(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("renderer-state database must not be a symlink")
        # Reserve a private regular file before sqlite opens it (no default 0644
        # interval and no writes to source/cache locations).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("CREATE TABLE IF NOT EXISTS failures ("
                           "fingerprint TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL, "
                           "updated REAL NOT NULL, PRIMARY KEY(fingerprint, version, kind))")
        connection.execute("CREATE TABLE IF NOT EXISTS fallbacks ("
                           "attempt TEXT PRIMARY KEY, consumed REAL NOT NULL)")
        connection.commit()
        return connection

    def kinds(self, fingerprint: str | None, version: str) -> set[str]:
        if fingerprint is None:
            return set()
        connection = self._connect()
        try:
            return {row[0] for row in connection.execute(
                "SELECT kind FROM failures WHERE fingerprint=? AND version=?", (fingerprint, version)
            )} & PERSISTENT_FAILURE_KINDS
        finally:
            connection.close()

    def record(self, fingerprint: str | None, version: str, kind: str) -> bool:
        if fingerprint is None or kind not in PERSISTENT_FAILURE_KINDS:
            return False
        connection = self._connect()
        try:
            with connection:
                connection.execute("INSERT INTO failures VALUES (?,?,?,?) "
                                   "ON CONFLICT(fingerprint, version, kind) DO UPDATE SET updated=excluded.updated",
                                   (fingerprint, version, kind, time.time()))
            return True
        finally:
            connection.close()

    def clear(self, fingerprint: str | None, version: str) -> None:
        if fingerprint is None:
            return
        connection = self._connect()
        try:
            with connection:
                connection.execute("DELETE FROM failures WHERE fingerprint=? AND version=?",
                                   (fingerprint, version))
        finally:
            connection.close()

    def claim_fallback(self, attempt: str) -> bool:
        """Atomically consume the transition even if process launch then fails."""
        attempt = open_attempt_id(attempt)
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute("INSERT OR IGNORE INTO fallbacks VALUES (?,?)",
                                            (attempt, time.time()))
            return cursor.rowcount == 1
        finally:
            connection.close()

    def fallback_consumed(self, attempt: str) -> bool:
        connection = self._connect()
        try:
            return connection.execute("SELECT 1 FROM fallbacks WHERE attempt=?",
                                      (open_attempt_id(attempt),)).fetchone() is not None
        finally:
            connection.close()
