"""Source- and plane-scoped annotation sidecar discovery and migration.

Filesystem naming and legacy migration are shared by the viewer backends;
window isolation and optimistic write conflicts remain in AnnotationWorkspace.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SLIDE_SUFFIXES = {".nd2", ".svs"}

def _manifest_source_path(container: Path, manifest: dict[str, Any]) -> Path | None:
    from .cache import source_base

    src = manifest.get("source") or {}
    rel = src.get("relative_path") if isinstance(src, dict) else None
    if not isinstance(rel, str) or not rel:
        return None
    candidate = (source_base(container) / rel).resolve()
    recorded_name = src.get("name")
    if recorded_name and candidate.name != Path(str(recorded_name)).name:
        return None
    return candidate


def _annotation_source_name(path: Path) -> str | None:
    """Return a legacy sidecar's declared source name, when present."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    if isinstance(source, dict):
        name = source.get("name")
        return Path(str(name)).name if name else None
    if isinstance(source, str):
        return Path(source).name
    return None


def _legacy_sidecar_matches(path: Path, source_name: str, home: Path) -> bool:
    """Whether an unscoped sidecar can be migrated without guessing."""
    declared = _annotation_source_name(path)
    if declared is not None:
        return declared == source_name
    stem = Path(source_name).stem
    try:
        siblings = [
            item
            for item in home.iterdir()
            if item.is_file()
            and item.stem == stem
            and item.suffix.lower() in SLIDE_SUFFIXES
        ]
    except OSError:
        siblings = []
    return len(siblings) <= 1


def annotations_sidecar(
    store_path: str | Path, attrs: dict[str, Any], *,
    migrate: bool = True, legacy_paths: list[Path] | None = None,
) -> Path:
    """Return a sidecar path scoped to the source file and selected plane.

    An annotation belongs to one level-0 coordinate space. T, P, and Z are
    therefore part of its identity, just as they are part of cache identity.
    An older unscoped sidecar is claimed by the default plane and copied
    for any other, so every selection keeps seeing its 0.9 work.
    """
    from .cache import (
        ANNOTATIONS_DIR,
        MANAGED_DIR,
        manifest_container,
        read_manifest,
        selection_tag,
    )
    from .convert import CACHE_DIR_NAME

    store_path = Path(store_path).resolve()
    meta = attrs["nd2wsi"]
    source_name = Path(meta["source"]).name
    stem = Path(source_name).stem

    container = manifest_container(store_path)
    home = container.parent if container is not None else store_path.parent
    if home.name in (CACHE_DIR_NAME, "caches"):
        home = home.parent
    if home.name == MANAGED_DIR:
        home = home.parent

    manifest = read_manifest(container) if container is not None else None
    if manifest and container is not None:
        source = _manifest_source_path(container, manifest)
        if source is not None and source.is_file() and source.name == source_name:
            home = source.parent
    raw_selection = (manifest or {}).get("selection") or meta.get("selection") or {}
    selection = {
        key: raw_selection[key]
        for key in ("t", "p")
        if key in raw_selection
    }
    if "z_resolved" in raw_selection:
        selection["z"] = raw_selection["z_resolved"]
    elif "z" in raw_selection:
        selection["z"] = raw_selection["z"]

    safe_source = re.sub(r'[\/:*?"<>|\x00-\x1f]+', "_", source_name)
    filename = f"annotations_{safe_source}"
    if selection:
        filename += f"--{selection_tag(selection)}"
    filename += ".json"

    target_dir = home / MANAGED_DIR / ANNOTATIONS_DIR
    if migrate:
        target_dir.mkdir(parents=True, exist_ok=True)
    new = target_dir / filename

    if not new.exists():
        import shutil

        old_paths = (
            target_dir / f"annotations_{stem}.json",
            home / f"annotations_{stem}.json",
            home / CACHE_DIR_NAME / f"annotations_{stem}.json",
            home / f"{stem}.annotations.json",
            store_path.parent / f"annotations_{stem}.json",
        )
        for old in old_paths:
            if old == new or not old.exists():
                continue
            if not _legacy_sidecar_matches(old, source_name, home):
                note = (
                    f"legacy annotation sidecar {old.name} was not imported "
                    "because its source is ambiguous"
                )
                if note not in meta.setdefault("notes", []):
                    meta["notes"].append(note)
                continue
            if not migrate:
                if legacy_paths is not None:
                    legacy_paths.append(old)
                break
            # the default plane inherits the unscoped file outright; any
            # other selection takes a copy, because one unscoped sidecar
            # used to serve every plane and the rest must keep finding it
            claim = not selection or (
                int(raw_selection.get("t", 0) or 0) == 0
                and int(raw_selection.get("p", 0) or 0) == 0
                and str(raw_selection.get("z", "mid")) in ("mid", "0")
            )
            try:
                if claim:
                    old.rename(new)
                else:
                    shutil.copy2(old, new)
                break
            except OSError:
                continue
    return new


def plate_annotations_sidecar(
    path: str | Path, attrs: dict[str, Any], p: int, *, create_directory: bool = True,
) -> Path:
    """The sidecar for one site of a plate file, beside the slide.

    Annotations on a plate are per site and shared across time and z, so
    the site index is the only scope in the name.
    """
    from .cache import ANNOTATIONS_DIR, MANAGED_DIR

    path = Path(path).resolve()
    source_name = Path(attrs["nd2wsi"]["source"]).name or path.name
    safe_source = re.sub(r'[\/:*?"<>|\x00-\x1f]+', "_", source_name)
    target_dir = path.parent / MANAGED_DIR / ANNOTATIONS_DIR
    if create_directory:
        target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"annotations_{safe_source}--site{int(p)}.json"
