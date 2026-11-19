"""Thread-safe, atomic-write global UI settings and recent files persistence.

Stores ``ui-settings.json`` in the application support directory alongside
``lut-settings/``. Respects ``ND2WSI_APP_SUPPORT_DIR`` (or
``ND2WSI_UI_SETTINGS_ROOT``) for isolated testing, and strictly forbids Agent
windows from polluting user recent files or overwriting user preferences.
"""

from __future__ import annotations

import json
import math
import os
import stat
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .platform_io import BINARY
from .session_lock import SessionFileLock
from .window_sessions import _atomic_json

FORMAT = "nd2wsi-ui-settings/1"
MAX_BYTES = 128 * 1024
MAX_RECENT_STORED = 20
MAX_PANELS = 64
VALID_THEMES = frozenset({"auto", "light", "dark"})

_LOCK = threading.RLock()


def ui_settings_root() -> Path:
    for env_key in ("ND2WSI_UI_SETTINGS_ROOT", "ND2WSI_APP_SUPPORT_DIR"):
        override = os.environ.get(env_key)
        if override:
            return Path(override).expanduser().resolve()
    lut_override = os.environ.get("ND2WSI_LUT_SETTINGS_ROOT")
    if lut_override:
        return Path(lut_override).expanduser().resolve().parent
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "nd2wsi-viewer"


def ui_settings_path() -> Path:
    return ui_settings_root() / "ui-settings.json"


def _default_ui_settings() -> dict[str, Any]:
    return {
        "theme": "auto",
        "panels": {},
        "shellToolsExpanded": None,
    }


def _default_document() -> dict[str, Any]:
    return {
        "format": FORMAT,
        **_default_ui_settings(),
        "recentFiles": [],
    }


def _clean_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        raise ValueError("Nested UI settings value is too deep")
    if value is None or isinstance(value, bool):
        return value
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise ValueError("UI settings numbers must be finite")
        return value
    if isinstance(value, str):
        if len(value) > 512:
            raise ValueError("UI settings string is too long")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise ValueError("UI settings list is too long")
        return [_clean_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise ValueError("UI settings mapping has too many keys")
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str) or not 1 <= len(k) <= 64:
                raise ValueError("UI settings keys must be non-empty strings (<= 64 chars)")
            out[k] = _clean_json_value(v, depth=depth + 1)
        return out
    raise ValueError(f"Unsupported UI settings value type: {type(value).__name__}")


def _sanitize_panels(raw_panels: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_panels, Mapping):
        raise ValueError("panels must be a mapping")
    if len(raw_panels) > MAX_PANELS:
        raise ValueError("Too many panel entries in UI settings")
    clean: dict[str, dict[str, Any]] = {}
    for key, val in raw_panels.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 64:
            raise ValueError("Panel key must be a non-empty string (<= 64 chars)")
        if not isinstance(val, Mapping):
            raise ValueError("Each panel entry must be a mapping")
        clean[key] = _clean_json_value(val, depth=1)
    return clean


def _sanitize_recent_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip() or len(raw_path) > 4096:
        return None
    raw_name = entry.get("name")
    name = (
        raw_name.strip()
        if isinstance(raw_name, str) and raw_name.strip()
        else Path(raw_path).name or raw_path
    )
    opened_at = entry.get("openedAt", 0.0)
    if type(opened_at) not in (int, float) or not math.isfinite(opened_at):
        opened_at = 0.0
    return {
        "path": raw_path,
        "name": name[:512],
        "openedAt": float(opened_at),
    }


def _read_document_unlocked(path: Path) -> dict[str, Any]:
    doc = _default_document()
    try:
        flags = (
            os.O_RDONLY
            | BINARY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(os.open(path, flags), "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return doc
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return doc
        loaded = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return doc
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return doc

    if not isinstance(loaded, Mapping):
        return doc
    theme = loaded.get("theme")
    if theme in VALID_THEMES:
        doc["theme"] = theme
    expanded = loaded.get("shellToolsExpanded")
    if expanded is None or isinstance(expanded, bool):
        doc["shellToolsExpanded"] = expanded
    raw_panels = loaded.get("panels")
    if isinstance(raw_panels, Mapping):
        try:
            doc["panels"] = _sanitize_panels(raw_panels)
        except ValueError:
            pass
    raw_recent = loaded.get("recentFiles")
    if isinstance(raw_recent, list):
        seen: set[str] = set()
        recent: list[dict[str, Any]] = []
        for item in raw_recent:
            clean_item = _sanitize_recent_entry(item)
            if clean_item is not None and clean_item["path"] not in seen:
                seen.add(clean_item["path"])
                recent.append(clean_item)
                if len(recent) >= MAX_RECENT_STORED:
                    break
        doc["recentFiles"] = recent
    return doc


def _write_document_locked(path: Path, doc: dict[str, Any]) -> None:
    encoded = (json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("UI settings payload exceeds maximum allowed size")
    _atomic_json(path, doc)


def _public_ui_settings(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "theme": doc["theme"],
        "panels": json.loads(json.dumps(doc["panels"])),
        "shellToolsExpanded": doc["shellToolsExpanded"],
    }


def _apply_ui_patch(doc: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise ValueError("UI settings patch must be a mapping")
    updated = {
        "format": FORMAT,
        "theme": doc["theme"],
        "panels": dict(doc["panels"]),
        "shellToolsExpanded": doc["shellToolsExpanded"],
        "recentFiles": list(doc["recentFiles"]),
    }
    if "theme" in patch:
        theme = patch["theme"]
        if theme not in VALID_THEMES:
            raise ValueError("theme must be 'auto', 'light', or 'dark'")
        updated["theme"] = theme
    if "shellToolsExpanded" in patch:
        expanded = patch["shellToolsExpanded"]
        if expanded is not None and not isinstance(expanded, bool):
            raise ValueError("shellToolsExpanded must be a boolean or None")
        updated["shellToolsExpanded"] = expanded
    if "panels" in patch:
        raw_panels = patch["panels"]
        if not isinstance(raw_panels, Mapping):
            raise ValueError("panels must be a mapping")
        merged_panels = dict(updated["panels"])
        for key, val in raw_panels.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 64:
                raise ValueError("Panel key must be a non-empty string (<= 64 chars)")
            if val is None:
                merged_panels.pop(key, None)
            elif isinstance(val, Mapping):
                merged_panels[key] = _clean_json_value(val, depth=1)
            else:
                raise ValueError("Each panel entry must be a mapping or None")
        if len(merged_panels) > MAX_PANELS:
            raise ValueError("Too many panel entries in UI settings")
        updated["panels"] = merged_panels
    return updated


def load_ui_settings() -> dict[str, Any]:
    """Load validated UI preferences without creating directories or files."""
    with _LOCK:
        doc = _read_document_unlocked(ui_settings_path())
        return _public_ui_settings(doc)


def save_ui_settings(
    patch: Mapping[str, Any], *, window_role: str = "user"
) -> dict[str, Any]:
    """Validate and atomically merge ``patch`` into ``ui-settings.json``."""
    if not isinstance(patch, Mapping):
        raise ValueError("UI settings patch must be a mapping")
    path = ui_settings_path()
    with _LOCK:
        if window_role == "agent":
            doc = _read_document_unlocked(path)
            updated = _apply_ui_patch(doc, patch)
            return _public_ui_settings(updated)
        # Validate against a snapshot before creating directories or locks so an
        # invalid patch leaves zero filesystem side effects.
        preview = _apply_ui_patch(_read_document_unlocked(path), patch)
        _ = (json.dumps(preview, allow_nan=False)).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_lock = SessionFileLock(path.with_suffix(".lock"))
        file_lock.acquire(timeout=10)
        try:
            current = _read_document_unlocked(path)
            updated = _apply_ui_patch(current, patch)
            _write_document_locked(path, updated)
        finally:
            file_lock.release()
        return _public_ui_settings(updated)


def _is_unmounted_volume_path(path_str: str) -> bool:
    """True when ``path_str`` points inside ``/Volumes/<vol>/...`` whose volume
    root ``/Volumes/<vol>`` itself is currently unmounted."""
    if not path_str.startswith("/Volumes/"):
        return False
    remainder = path_str[len("/Volumes/") :].strip("/")
    if not remainder:
        return False
    vol_name = remainder.split("/", 1)[0]
    if not vol_name or vol_name in (".", ".."):
        return False
    vol_root = Path("/Volumes") / vol_name
    try:
        if not vol_root.exists():
            return True
        # On macOS, a stale empty stub directory in /Volumes that is not an
        # active mount point also represents an unmounted external volume.
        if vol_root.is_dir() and not os.path.ismount(vol_root):
            try:
                next(vol_root.iterdir())
            except StopIteration:
                return True
    except OSError:
        return True
    return False


def _evaluate_recent_files(
    stored: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    kept_stored: list[dict[str, Any]] = []
    evaluated: list[dict[str, Any]] = []
    pruned = False
    for item in stored:
        p_str = item["path"]
        exists = False
        try:
            exists = Path(p_str).exists()
        except OSError:
            exists = False
        if exists:
            kept_stored.append(item)
            evaluated.append({**item, "available": True})
        elif _is_unmounted_volume_path(p_str):
            kept_stored.append(item)
            evaluated.append({**item, "available": False})
        else:
            pruned = True
    return kept_stored, evaluated, pruned


def list_recent_files(limit: int = 12, *, prune: bool = True) -> list[dict[str, Any]]:
    """Return recent files, optionally pruning missing local paths while keeping
    unmounted ``/Volumes/<vol>/...`` entries with ``"available": False``."""
    cap = max(0, int(limit))
    path = ui_settings_path()
    with _LOCK:
        doc = _read_document_unlocked(path)
        kept_stored, evaluated, pruned = _evaluate_recent_files(doc["recentFiles"])
        if prune and pruned and path.exists():
            try:
                file_lock = SessionFileLock(path.with_suffix(".lock"))
                file_lock.acquire(timeout=5)
                try:
                    latest = _read_document_unlocked(path)
                    latest_kept, evaluated, latest_pruned = _evaluate_recent_files(
                        latest["recentFiles"]
                    )
                    if latest_pruned:
                        latest["recentFiles"] = latest_kept
                        _write_document_locked(path, latest)
                finally:
                    file_lock.release()
            except OSError:
                pass
        return evaluated[:cap]


def record_recent_file(
    path: str | Path,
    *,
    name: str | None = None,
    window_role: str = "user",
    role: str | None = None,
) -> list[dict[str, Any]]:
    """Record an opened file at the front of the recent-files list (capped at 20).

    Strictly a no-op when ``window_role == "agent"`` (or ``role == "agent"``);
    never reads or mutates the shared user ``ui-settings.json``.
    """
    effective_role = role if role is not None else window_role
    if effective_role == "agent":
        return []

    raw_p = Path(path).expanduser()
    resolved_str = str(raw_p.resolve())
    display_name = (
        name.strip()
        if isinstance(name, str) and name.strip()
        else Path(resolved_str).name or resolved_str
    )
    new_entry = {
        "path": resolved_str,
        "name": display_name[:512],
        "openedAt": float(time.time()),
    }

    settings_file = ui_settings_path()
    with _LOCK:
        settings_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_lock = SessionFileLock(settings_file.with_suffix(".lock"))
        file_lock.acquire(timeout=10)
        try:
            doc = _read_document_unlocked(settings_file)
            deduped = [
                item for item in doc["recentFiles"] if item["path"] != resolved_str
            ]
            doc["recentFiles"] = [new_entry, *deduped][:MAX_RECENT_STORED]
            _write_document_locked(settings_file, doc)
        finally:
            file_lock.release()
        return list_recent_files(limit=MAX_RECENT_STORED)
