"""Follow's two direction flips, remembered per image file format on this PC.

Follow's flips reverse the on-screen move of A/D (``flip_x``) or W/S
(``flip_y``) for SLACS's key source; they never change the image's
orientation. The pair last chosen for a format is kept in a small per-user
file, because the overlay page cannot remember anything across launches: it
runs in a private web view whose origin changes with every run. User windows
share the file, last write wins. An Agent window keeps a private copy under its
session root and never writes the user's file.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from .platform_io import BINARY
from .session_lock import SessionFileLock
from .window_sessions import _atomic_json

FORMAT_RECORD = "nd2wsi-follow-flips/1"
FORMATS = ("nd2", "svs", "sdpc", "jpeg", "png", "other")
MAX_BYTES = 16 * 1024
_SUFFIXES = {".nd2": "nd2", ".nd2svs": "nd2", ".svs": "svs", ".sdpc": "sdpc",
             ".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png"}
# A flips file that exists but whose content is not a record: the only kind a save replaces.
_INVALID = (ValueError, TypeError, UnicodeError, RecursionError)


def source_format(attrs, source_path=None) -> str:
    """The format whose remembered flips apply: the original file's, not its cache's.

    ``attrs["nd2wsi"]["source"]`` is the original file name for every reader,
    so an ``.nd2svs`` cache or an OME-Zarr built from an ND2 counts as ``nd2``
    even when its source is missing; ``source_path`` is only the fallback.
    """
    meta = attrs.get("nd2wsi", {}) if isinstance(attrs, Mapping) else {}
    if not isinstance(meta, Mapping):
        meta = {}
    if meta.get("source_format") == "sdpc":
        return "sdpc"
    for name in (meta.get("source"), source_path):
        if isinstance(name, (str, os.PathLike)):
            suffix = Path(name).suffix.lower()
            if suffix in _SUFFIXES:
                return _SUFFIXES[suffix]
    return "other"


def flips_root() -> Path:
    override = os.environ.get("ND2WSI_FOLLOW_FLIPS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # The same per-user base as lut_settings.settings_root(), without its override.
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "nd2wsi-viewer" / "stage-follow"


def _validate_flip(value, name) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false")
    return value


class FollowFlipStore:
    """The flips file of one window process. The constructor does no I/O."""

    def __init__(self, window_session=None, *, root=None):
        if window_session is not None and getattr(window_session, "role", None) == "agent":
            self.path = Path(window_session.root) / "stage-follow" / "flips.json"
        else:
            self.path = Path(root if root is not None else flips_root()) / "flips.json"

    def _read_record(self) -> dict:
        """The record's per-format flips.

        Raises FileNotFoundError when there is no file, one of ``_INVALID``
        when the file is there but its content is not a record (not a bounded
        regular file, not JSON, another format), and any other OSError when it
        could not be read at all (no permission, held by another program, a link).
        """
        flags = os.O_RDONLY | BINARY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(self.path, flags), "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("Saved Follow flips are not a bounded regular file")
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Saved Follow flips are too large")
        record = json.loads(raw)
        if (not isinstance(record, dict) or record.get("format") != FORMAT_RECORD
                or not isinstance(record.get("flips"), dict)):
            raise ValueError("Saved Follow flips have an unsupported format")
        return record["flips"]

    def _read(self):
        """(the record's per-format flips, or None, and a warning or None); never raises."""
        try:
            return self._read_record(), None
        except FileNotFoundError:
            return None, None
        except (OSError, *_INVALID) as exc:
            # Never delete, rewrite or migrate an unreadable record here.
            return None, "Saved Follow flips were not restored: " + str(exc)

    def load(self, fmt) -> tuple[tuple[bool, bool] | None, str | None]:
        """((flip_x, flip_y) remembered for ``fmt``, or None, and a warning or None); never raises."""
        flips, warning = self._read()
        if flips is None or not isinstance(fmt, str) or fmt not in flips:
            return None, warning
        entry = flips[fmt]
        if (not isinstance(entry, dict) or type(entry.get("flip_x")) is not bool
                or type(entry.get("flip_y")) is not bool):
            return None, "Saved Follow flips were not restored: the entry for this format is invalid"
        return (entry["flip_x"], entry["flip_y"]), None

    def save(self, fmt, flip_x, flip_y) -> None:
        """Remember one format's flips, keeping the other formats'.

        Raises ValueError for an unknown format or a non-bool flip, and OSError
        (TimeoutError included, when another viewer holds the file) on I/O failure,
        also when the existing file cannot be read; it is then left as it is.
        """
        if not isinstance(fmt, str) or fmt not in FORMATS:
            raise ValueError("Unknown image format for Follow flips")
        flip_x, flip_y = _validate_flip(flip_x, "flip_x"), _validate_flip(flip_y, "flip_y")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = SessionFileLock(self.path.with_suffix(".lock"))
        lock.acquire(timeout=2)
        try:
            # Re-read under the lock. Only a missing file or invalid content starts
            # afresh; a file that cannot be read now raises, so a passing failure
            # never drops the other formats' flips.
            try:
                flips = dict(self._read_record())
            except (FileNotFoundError, *_INVALID):
                flips = {}
            flips[fmt] = {"flip_x": flip_x, "flip_y": flip_y,
                          "changed": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
            _atomic_json(self.path, {"format": FORMAT_RECORD, "flips": flips})
        finally:
            lock.release()


__all__ = ["FORMAT_RECORD", "FORMATS", "MAX_BYTES", "FollowFlipStore", "flips_root", "source_format"]
