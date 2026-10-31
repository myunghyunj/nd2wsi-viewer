"""Per-PC laser-target calibration; Agent windows have a private copy."""

from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path

from . import follow_flips
from .platform_io import BINARY
from .session_lock import SessionFileLock
from .window_sessions import _atomic_json

FORMAT = "nd2wsi-laser-target/1"
DEFAULT = {"offset_css": [0.0, 0.0], "side_um": 18.0, "rotation_deg": 45.0,
           "window": None, "dpr": None}
MAX_BYTES = 4096


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Invalid laser target value")
    return float(value)


def vector(value, count, low, high):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError("Invalid laser target coordinates")
    return [number(v, low, high) for v in value]


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValueError("Invalid laser target settings")
    frame = value["window"]
    dpr = value["dpr"]
    if (frame is None) != (dpr is None):
        raise ValueError("Incomplete laser calibration")
    if frame is not None:
        frame = vector(frame, 4, -1e6, 1e6)
        if not all(0 < size <= 16384 for size in frame[2:]):
            raise ValueError("Invalid calibration window size")
        dpr = number(dpr, .1, 16)
    return {"offset_css": vector(value["offset_css"], 2, -16384, 16384),
            "side_um": number(value["side_um"], 2, 200),
            "rotation_deg": number(value["rotation_deg"], -360000, 360000) % 90,
            "window": frame, "dpr": dpr}


class LaserTargetStore:
    def __init__(self, window_session=None):
        root = (Path(window_session.root) / "stage-follow"
                if getattr(window_session, "role", None) == "agent" else follow_flips.flips_root())
        self.path = root / "laser-target.json"

    def load(self):
        try:
            flags = os.O_RDONLY | BINARY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(self.path, flags), "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError("Laser calibration is not a bounded regular file")
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Laser calibration is too large")
            record = json.loads(raw)
            if not isinstance(record, dict) or record.get("format") != FORMAT:
                raise ValueError("Unsupported laser calibration")
            return validate(record.get("target")), None
        except FileNotFoundError:
            return validate(DEFAULT), None
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            return validate(DEFAULT), f"Laser calibration was not restored: {exc}"

    def save(self, value):
        target = validate(value)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = SessionFileLock(self.path.with_suffix(".lock"))
        lock.acquire(timeout=2)
        try:
            if self.path.is_symlink():
                raise OSError("Laser calibration is a link")
            _atomic_json(self.path, {"format": FORMAT, "target": target})
        finally:
            lock.release()
