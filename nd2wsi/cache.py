"""Cache identity, atomic construction, and the managed data folder.

A viewing cache is an app-owned artifact derived from one slide and one
T/P/Z selection. Before 0.8 it was identified by the slide's stem alone,
written straight into its final path, and reused on nothing more than
existence — so a crash wedged the slide, a changed file was silently
served stale, and two selections fought over one directory.

Now every cache lives in a container named for its source and selection,

    <slide dir>/nd2wsi/caches/<stem>--t0-p0-zmid.nd2wsi-cache/
        manifest.json     identity, fingerprint, completion — written last
        store.ome.zarr/   the pyramid

built in a staging sibling under an interprocess lock and renamed into
place only when complete. A cache that fails validation is quarantined,
never deleted outright, and a store the user opened directly is never
touched at all. Annotations are work, not cache; they live one folder
over in nd2wsi/annotations/ and no cache operation deletes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

MANAGED_DIR = "nd2wsi"
CACHES_DIR = "caches"
ANNOTATIONS_DIR = "annotations"
CACHE_SUFFIX = ".nd2wsi-cache"
STORE_NAME = "store.ome.zarr"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "nd2wsi-cache/2"
# overview containers hold no level 0, which a pre-0.9 reader would serve
# as a full store at half resolution with level-0 calibration — so they
# carry a format that old readers reject (and quarantine) instead
OVERVIEW_FORMAT = "nd2wsi-cache/3"
KNOWN_FORMATS = (MANIFEST_FORMAT, OVERVIEW_FORMAT)
ALGORITHM = "box-mean-floor-v1"

_LOCK_STALE_S = 4 * 3600


def selection_tag(selection: Any) -> str:
    """``t0-p0-zmid`` — the requested selection, as named in the path."""
    sel = selection.describe() if hasattr(selection, "describe") else dict(selection)
    return f"t{sel.get('t', 0)}-p{sel.get('p', 0)}-z{sel.get('z', 'mid')}"


def managed_dir(slide: str | Path) -> Path:
    return Path(slide).parent / MANAGED_DIR


def annotations_dir(slide: str | Path) -> Path:
    return managed_dir(slide) / ANNOTATIONS_DIR


def cache_container(
    slide: str | Path, selection: Any = None, cache_dir: str | Path | None = None
) -> Path:
    from .reader import PlaneSelection

    slide = Path(slide)
    base = Path(cache_dir) if cache_dir else managed_dir(slide) / CACHES_DIR
    tag = selection_tag(selection or PlaneSelection())
    return base / f"{slide.stem}--{tag}{CACHE_SUFFIX}"


def container_store(container: str | Path) -> Path:
    return Path(container) / STORE_NAME


def quick_fingerprint(path: str | Path) -> dict[str, Any]:
    """Identity of a source file, cheap enough to take on every open.

    Size, mtime, and a hash over the head, middle and tail megabyte. This
    is a stale-cache detector, not a full content hash: a change that
    leaves size, mtime and all three sampled windows intact goes unseen.
    """
    path = Path(path)
    st = path.stat()
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for offset in (
            0,
            max(0, st.st_size // 2 - (1 << 19)),
            max(0, st.st_size - (1 << 20)),
        ):
            fh.seek(offset)
            h.update(fh.read(1 << 20))
    h.update(str(st.st_size).encode())
    return {
        "name": path.name,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "quick_sha256": h.hexdigest(),
    }


def fingerprints_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in ("size", "mtime_ns", "quick_sha256"))


def write_manifest(
    container: Path,
    slide: Path,
    fingerprint: dict[str, Any],
    selection: dict[str, Any],
    resolved_z: Any,
    tile: int,
    shape_cyx: tuple[int, int, int],
    dtype: str,
    kind: str = "full",
) -> None:
    from . import __version__

    manifest = {
        "format": MANIFEST_FORMAT if kind == "full" else OVERVIEW_FORMAT,
        "kind": kind,
        "complete": True,
        "generation": uuid.uuid4().hex,
        "source": {
            **fingerprint,
            "relative_path": os.path.relpath(slide, container),
        },
        "selection": {**selection, "z_resolved": resolved_z},
        "image": {"shape_cyx": list(shape_cyx), "dtype": dtype},
        "pyramid": {"algorithm": ALGORITHM, "tile": tile},
        "created_by": {"nd2wsi_version": __version__},
    }
    tmp = container / f".{MANIFEST_NAME}.tmp"
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.replace(container / MANIFEST_NAME)


def read_manifest(container: str | Path) -> dict[str, Any] | None:
    try:
        m = json.loads((Path(container) / MANIFEST_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return m if isinstance(m, dict) and m.get("complete") else None


def cache_matches(
    container: Path, slide: Path, selection: Any, kind: str | None = None
) -> bool:
    """A cache is valid for this slide and selection, or it is stale.

    ``kind`` narrows the match ("full" or "overview"); None takes either.
    """
    m = read_manifest(container)
    if m is None or m.get("format") not in KNOWN_FORMATS:
        return False
    if m.get("pyramid", {}).get("algorithm") != ALGORITHM:
        return False
    if kind is not None and m.get("kind", "full") != kind:
        return False
    sel = selection.describe() if hasattr(selection, "describe") else dict(selection)
    stored = m.get("selection", {})
    if any(str(stored.get(k)) != str(v) for k, v in sel.items()):
        return False
    try:
        return fingerprints_match(m.get("source", {}), quick_fingerprint(slide))
    except OSError:
        return False


def quarantine(path: Path) -> Path:
    """Set a broken artifact aside instead of destroying it."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 0
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
    path.rename(target)
    return target


class CacheLock:
    """One build per source and selection, across processes.

    An O_EXCL lockfile beside the container carries pid, start time and a
    random generation. The payload lands on the raw fd before anything
    else can matter, release removes only a lock this instance created,
    and a stale lock is reclaimed by atomically renaming it aside — so
    two waiters can never both count one reclaim, and nobody ever unlinks
    a live lock they do not own.
    """

    def __init__(self, container: Path):
        self.path = container.with_name(container.name + ".lock")
        self._gen: str | None = None

    def _read(self, path: Path | None = None) -> dict[str, Any] | None:
        try:
            return json.loads((path or self.path).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _stale(self) -> bool:
        info = self._read()
        if info is None:
            # unreadable or momentarily empty: only old age makes it stale
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False  # vanished: not ours to reclaim
            return age > 30
        if time.time() - info.get("time", 0) > _LOCK_STALE_S:
            return True
        pid = info.get("pid")
        try:
            os.kill(int(pid), 0)
            return False
        except (ProcessLookupError, TypeError, ValueError):
            return True
        except PermissionError:
            return False

    def _reclaim(self) -> None:
        """Take a stale lock out of play; only one contender can win."""
        aside = self.path.with_name(f"{self.path.name}.reclaim-{uuid.uuid4().hex[:8]}")
        try:
            self.path.rename(aside)
        except OSError:
            return  # someone else reclaimed it first
        aside.unlink(missing_ok=True)

    def acquire(self, timeout: float = 3600.0, poll: float = 0.5) -> None:
        deadline = time.time() + timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        gen = uuid.uuid4().hex
        payload = json.dumps({"pid": os.getpid(), "time": time.time(), "gen": gen})
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload.encode())
                finally:
                    os.close(fd)
                self._gen = gen
                return
            except FileExistsError:
                if self._stale():
                    self._reclaim()
                    continue
                if time.time() > deadline:
                    raise TimeoutError(
                        f"another process is building {self.path.stem}"
                    ) from None
                time.sleep(poll)

    def release(self) -> None:
        if self._gen is None:
            return
        info = self._read()
        if info is not None and info.get("gen") != self._gen:
            self._gen = None
            return  # reclaimed out from under us: that lock is not ours
        self.path.unlink(missing_ok=True)
        self._gen = None

    def __enter__(self) -> CacheLock:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def commit_container(staging: Path, final: Path) -> None:
    """Expose a finished staging container at the final path atomically.

    Same-directory renames only. If a previous container exists it is set
    aside first and removed after the new one is in place; a failure puts
    it back.
    """
    backup = None
    if final.exists():
        backup = final.with_name(final.name + f".replaced-{uuid.uuid4().hex[:8]}")
        final.rename(backup)
    try:
        staging.rename(final)
    except OSError:
        if backup is not None:
            backup.rename(final)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def sweep_stale_builds(caches_dir: Path) -> int:
    """Remove leftover staging and backup dirs whose builder is gone.

    Covers ``*.building-<pid>`` staging and ``*.replaced-*`` backups that
    a crash inside commit_container can strand. Liveness is judged from
    the family's exact lockfile — names are never fed to glob, so a
    bracketed NIS-Elements filename cannot inject wildcards.
    """
    n = 0
    if not caches_dir.is_dir():
        return 0
    for item in caches_dir.iterdir():
        name = item.name
        if ".building-" in name:
            family = name.split(".building-")[0].lstrip(".")
        elif ".replaced-" in name:
            family = name.split(".replaced-")[0]
        else:
            continue
        lock = CacheLock(caches_dir / family)
        info = lock._read()
        live = False
        if info:
            try:
                os.kill(int(info.get("pid", -1)), 0)
                live = True
            except (ProcessLookupError, ValueError, TypeError):
                pass
            except PermissionError:
                live = True
        if not live:
            shutil.rmtree(item, ignore_errors=True)
            n += 1
    return n
