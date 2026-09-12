"""Bounded display-only handoff between independent macOS viewer processes.

No annotation, role override, executable, or arbitrary setting is accepted in
the view state. Handoff files are private, source-bound snapshots, not sessions.
"""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from pathlib import Path

MAX_BYTES = 65536


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} is outside the supported range")
    return value


def validate_view_state(payload, *, dimensions=None, channel_count=None) -> dict:
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Unsupported view-state version")
    dims = payload.get("source_dimensions")
    if not isinstance(dims, list) or len(dims) != 2 or any(type(v) is not int or not 1 <= v <= 2**31 for v in dims):
        raise ValueError("Invalid source dimensions")
    if dimensions is not None and list(dimensions) != dims:
        raise ValueError("View-state source dimensions do not match")
    center = payload.get("center")
    if not isinstance(center, list) or len(center) != 2:
        raise ValueError("Invalid view-state center")
    center = [_number(v, "center", 0, limit) for v, limit in zip(center, dims)]
    zoom = _number(payload.get("zoom"), "zoom", 1e-12, 32)
    channels = payload.get("channels")
    if not isinstance(channels, list) or not 1 <= len(channels) <= 8:
        raise ValueError("Invalid view-state channels")
    if channel_count is not None and channel_count != len(channels):
        raise ValueError("View-state channel count does not match")
    cleaned = []
    for channel in channels:
        if not isinstance(channel, dict):
            raise ValueError("Invalid view-state channel")
        window = channel.get("window")
        color = channel.get("color")
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("Invalid channel window")
        lo = _number(window[0], "window low", 0, 65535)
        hi = _number(window[1], "window high", 0, 65536)
        if hi <= lo:
            raise ValueError("Channel window must have positive width")
        if not isinstance(color, list) or len(color) != 3:
            raise ValueError("Invalid channel color")
        color = [int(round(_number(v, "color", 0, 255))) for v in color]
        if type(channel.get("visible")) is not bool:
            raise ValueError("Channel visibility must be boolean")
        cleaned.append({"window": [lo, hi], "gamma": _number(channel.get("gamma"), "gamma", 0.1, 10),
                        "color": color, "visible": channel["visible"]})
    return {"version": 1, "source_dimensions": dims, "center": center, "zoom": zoom, "channels": cleaned}


def _identity(source):
    path = Path(source).expanduser().resolve(strict=True)
    info = path.stat()
    return {"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def write_handoff(state, *, source, role, root=None) -> Path:
    if role not in ("user", "agent"):
        raise ValueError("Invalid handoff role")
    payload = {"format": "nd2wsi-view-handoff/1", "source": _identity(source),
               "role": role, "state": validate_view_state(state)}
    # One private directory per snapshot, avoiding shared-file write races.
    if root is not None:
        base = Path(root).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        base = None
    directory = Path(tempfile.mkdtemp(prefix="nd2wsi-handoff-", dir=base))
    path = directory / "view.json"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, allow_nan=False)
    return path


def read_handoff(path, *, source, role) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(Path(path).expanduser(), flags)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("Handoff must be an owned private bounded regular file")
        payload = json.loads(handle.read(MAX_BYTES + 1))
    if (not isinstance(payload, dict) or payload.get("format") != "nd2wsi-view-handoff/1"
            or payload.get("role") != role or payload.get("source") != _identity(source)):
        raise ValueError("Handoff source or role does not match this opening")
    return validate_view_state(payload.get("state"))


def matches_view_state(expected, observed) -> bool:
    """Compare actual renderer state with strict display and subpixel camera tolerances."""
    try:
        left, right = validate_view_state(expected), validate_view_state(observed)
    except (ValueError, TypeError):
        return False
    if left["source_dimensions"] != right["source_dimensions"] or len(left["channels"]) != len(right["channels"]):
        return False
    if not all(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-3) for a, b in zip(left["center"], right["center"])):
        return False
    if not math.isclose(left["zoom"], right["zoom"], rel_tol=1e-7, abs_tol=1e-10):
        return False
    for a, b in zip(left["channels"], right["channels"]):
        if a["visible"] != b["visible"] or a["color"] != b["color"]:
            return False
        if not math.isclose(a["gamma"], b["gamma"], abs_tol=1e-6):
            return False
        if not all(math.isclose(x, y, abs_tol=1e-5) for x, y in zip(a["window"], b["window"])):
            return False
    return True
