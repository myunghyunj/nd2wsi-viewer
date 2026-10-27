"""Continuous session log for one viewer process, kept for review after a run.

Interface used across the app (the implementation fills in the details):

- ``start(app_name=..., version=..., folder=None)`` opens this run's log file and
  returns its path, or None when logging could not start.
- ``event(kind, **fields)`` appends one record. It is thread-safe, never raises,
  and does nothing before ``start`` or after ``stop``.
- ``path()`` returns the current log file, or None.
- ``stop(reason)`` writes a final record and closes the file.
"""

from __future__ import annotations

import threading
from pathlib import Path

_lock = threading.Lock()
_path: Path | None = None


def start(*, app_name: str, version: str, folder: Path | None = None) -> Path | None:
    return None


def event(kind: str, **fields) -> None:
    return None


def path() -> Path | None:
    with _lock:
        return _path


def stop(reason: str = "exit") -> None:
    return None
