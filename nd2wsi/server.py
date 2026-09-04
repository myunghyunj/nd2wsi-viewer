"""Local viewer server (stdlib only -- no web framework dependency).

Multiple slides are served at once, browser-tab style:

GET  /                          tab shell (one tab per open slide)
GET  /static/<path>             js / css / vendored OpenSeadragon (global)
GET  /api/slides                open slides: [{sid, name, width, height}]
POST /api/open                  {"path": "/…/slide.nd2|.svs|store.ome.zarr"}
                                converts if needed, registers, -> {sid}
POST /api/close                 {"sid": "…"} unregister a slide

Per slide (also reachable bare as /api/… for the first slide, so scripts
keep working):

GET  /s/<sid>/                  viewer page
GET  /s/<sid>/api/info          store metadata as JSON
GET  /s/<sid>/api/inspect       slide provenance and storage details
GET  /s/<sid>/api/pixel         one raw sample at displayed-base x/y
GET  /s/<sid>/api/associated/<thumbnail|label|macro>.jpg
GET  /s/<sid>/api/histogram     per-channel LUT histograms
GET  /s/<sid>/api/tile/<L>/<x>/<y>.jpg?c=0,1&win=…
GET  /s/<sid>/api/plate/frame/<t>/<p>/<z>.jpg?k=8&c=&win=   reduced frame of one site
GET  /s/<sid>/api/plate/status  how much of the thumbnail store is filled
GET  /s/<sid>/api/plate/focus   the sharpest plane per time point and site
GET  /s/<sid>/api/roi?level=&x=&y=&w=&h=&format=nd2|tiff|png|jpg&c=&win=
GET/POST /s/<sid>/api/annotations   sidecar annotations
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import render
from .direct import _Lifecycle

STATIC_DIR = Path(__file__).parent / "static"
TILE_RE = re.compile(r"^/api/tile/(\d+)/(\d+)/(\d+)\.(jpg|jpeg|png)$")
PLATE_FRAME_RE = re.compile(r"^/api/plate/frame/(\d+)/(\d+)/(\d+)\.(jpg|jpeg|png)$")
ASSOCIATED_RE = re.compile(
    r"^/api/associated/(thumbnail|label|macro)\.jpg$"
)
SLIDE_RE = re.compile(r"^/s/([0-9a-f]{8})(/.*)?$")
JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# region-export progress, polled by the viewer's status bar
EXPORT_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _job_update(job: str | None, **kw) -> None:
    if not job:
        return
    now = time.time()
    with _JOBS_LOCK:
        for stale in [k for k, v in EXPORT_JOBS.items() if now - v.get("t", 0) > 600]:
            EXPORT_JOBS.pop(stale, None)
        d = EXPORT_JOBS.setdefault(job, {})
        d.update(kw)
        d["t"] = now


def _job_get(job: str) -> dict:
    with _JOBS_LOCK:
        d = dict(EXPORT_JOBS.get(job) or {})
    d.pop("t", None)
    return d or {"state": "unknown", "pct": 0}

SLIDE_SUFFIXES = {".nd2", ".svs"}


_LIMND2_OK: bool | None = None


def _limnd2_available() -> bool:
    """True only when limnd2 actually imports (find_spec lies in bundles)."""
    global _LIMND2_OK
    if _LIMND2_OK is None:
        try:
            import limnd2  # noqa: F401

            _LIMND2_OK = True
        except Exception:
            _LIMND2_OK = False
    return _LIMND2_OK


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}


class ViewerState:
    def __init__(
        self,
        root: Any,
        attrs: dict[str, Any],
        max_render_mpx: float = 400.0,
        annotations_path: Path | None = None,
        generation: str = "",
        trash_path: Path | None = None,
        source_path: Path | None = None,
        store_path: Path | None = None,
        container_path: Path | None = None,
        manifest: dict[str, Any] | None = None,
        plate: Any = None,
    ):
        self.root = root
        self.attrs = attrs
        self.plate = plate  # a PlateSource when the slide is a time series of sites
        self.max_render_mpx = max_render_mpx
        self.histograms: list | None = None  # computed lazily, once
        self.annotations_path = annotations_path
        self.generation = generation  # changes when the pixels could
        self.trash_path = trash_path
        # These paths are registered by the backend when the slide opens.
        # Handlers expose read-only properties and never accept a path from
        # the browser for reveal or associated-image operations.
        self._source_path = (
            Path(source_path).expanduser().resolve() if source_path is not None else None
        )
        self._store_path = (
            Path(store_path).expanduser().resolve() if store_path is not None else None
        )
        self._container_path = (
            Path(container_path).expanduser().resolve()
            if container_path is not None
            else None
        )
        self.manifest = dict(manifest or {})
        self.busy = _Lifecycle()  # in-flight exports, so trash can refuse
        self.lock = threading.Lock()  # zarr reads are thread-safe; lock kept for attrs
        self._inspect_lock = threading.Lock()
        self._cache_usage_ready = False
        self._cache_usage: dict[str, int] | None = None

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    @property
    def store_path(self) -> Path | None:
        return self._store_path

    @property
    def container_path(self) -> Path | None:
        return self._container_path

    @property
    def cache_path(self) -> Path | None:
        """The one registered path shown/revealed as on-disk viewer data."""
        return self._container_path or self._store_path

    def cache_usage(self) -> dict[str, int] | None:
        """Logical and allocated cache bytes, computed once per generation."""
        with self._inspect_lock:
            if not self._cache_usage_ready:
                self._cache_usage = _path_usage(self.cache_path)
                self._cache_usage_ready = True
            return dict(self._cache_usage) if self._cache_usage is not None else None


def _allocated_bytes(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    return int(blocks) * 512 if blocks is not None else int(st.st_size)


def _path_usage(path: Path | None) -> dict[str, int] | None:
    """Return logical file bytes and actual allocated bytes for ``path``.

    Logical directory size is the sum of file lengths. Allocated size also
    includes directory records, matching the important "space on disk"
    distinction on ExFAT caches containing many small chunk files.
    """
    if path is None:
        return None
    try:
        root_stat = path.lstat()
    except OSError:
        return None
    if not path.is_dir():
        return {
            "bytes": int(root_stat.st_size),
            "allocated_bytes": _allocated_bytes(root_stat),
        }

    logical = 0
    allocated = _allocated_bytes(root_stat)
    for walk_root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            try:
                allocated += _allocated_bytes((Path(walk_root) / name).lstat())
            except OSError:
                continue
        for name in files:
            try:
                st = (Path(walk_root) / name).lstat()
            except OSError:
                continue
            logical += int(st.st_size)
            allocated += _allocated_bytes(st)
    return {"bytes": logical, "allocated_bytes": allocated}


def _storage_mode(meta: dict[str, Any]) -> str:
    if meta.get("direct"):
        return "direct"
    return {
        "source-backed": "compact",
        "overview-degraded": "overview-degraded",
    }.get(meta.get("kind"), "full")


def _inspection_storage_details(
    st: ViewerState, meta: dict[str, Any]
) -> dict[str, Any]:
    """Merge the store descriptor with any managed-container manifest."""
    details: dict[str, Any] = {}
    embedded = meta.get("storage")
    if isinstance(embedded, dict):
        details.update(embedded)
    manifested = st.manifest.get("storage")
    if isinstance(manifested, dict):
        details.update(manifested)
    details["mode"] = _storage_mode(meta)
    return details


def _manifest_source_path(container: Path, manifest: dict[str, Any]) -> Path | None:
    src = manifest.get("source") or {}
    rel = src.get("relative_path") if isinstance(src, dict) else None
    if not isinstance(rel, str) or not rel:
        return None
    candidate = (container / rel).resolve()
    recorded_name = src.get("name")
    if recorded_name and candidate.name != Path(str(recorded_name)).name:
        return None
    return candidate


def _associated_source_path(st: ViewerState) -> Path | None:
    """A currently valid registered SVS source, never a request-supplied path."""
    source = st.source_path
    if source is None or source.suffix.lower() != ".svs" or not source.is_file():
        return None
    source_info = st.manifest.get("source") or {}
    fingerprint_keys = ("size", "mtime_ns", "quick_sha256")
    if not isinstance(source_info, dict) or not all(
        key in source_info for key in fingerprint_keys
    ):
        return None
    from .cache import fingerprints_match, quick_fingerprint

    try:
        if not fingerprints_match(source_info, quick_fingerprint(source)):
            return None
    except OSError:
        return None
    return source


def _associated_names(st: ViewerState) -> list[str]:
    source = _associated_source_path(st)
    if source is None:
        return []
    try:
        from .svs import associated_image_names

        names = associated_image_names(source)
        return names if _associated_source_path(st) == source else []
    except Exception:
        # An auxiliary image must never make the primary slide inspector fail.
        return []


def _frame_args(st: ViewerState, q: dict) -> tuple[int, int, int] | None:
    """The (t, p, z) a plate request names, or None for an ordinary slide.

    Missing values fall back to t 0, site 0 and the home z plane. Raises
    ``ValueError`` when a value is not an integer or lies out of range.
    """
    plate = st.plate
    if plate is None:
        return None

    def qi(name: str, default: int, size: int) -> int:
        raw = (q.get(name) or [None])[0]
        if raw is None or raw == "":
            return default
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer") from None
        if not 0 <= v < size:
            raise ValueError(f"{name}={v} out of range (0..{size - 1})")
        return v

    return (
        qi("t", 0, plate.T),
        qi("p", 0, plate.P),
        qi("z", plate.z_home, plate.Z),
    )


def _pixel_payload(
    st: ViewerState, x: float, y: float, root: Any = None
) -> dict[str, Any]:
    """Read one raw sample at the displayed base level.

    A degraded overview advertises stored path ``1`` as its first/displayed
    level. Its incoming coordinates already live in that coordinate system,
    so they must not be divided by two a second time.
    """
    meta = st.attrs["nd2wsi"]
    level = meta["levels"][0]
    width, height = int(level["width"]), int(level["height"])
    xi = max(0, min(int(x), width - 1))
    yi = max(0, min(int(y), height - 1))
    if root is None:
        root = st.root
    region = render._read_region(root, str(level["path"]), xi, yi, 1, 1)
    if region.shape[1:] != (1, 1):
        raise ValueError("pixel is outside the displayed image")

    channels = st.attrs["omero"]["channels"]
    values = []
    for i, raw in enumerate(region[:, 0, 0]):
        value = raw.item() if hasattr(raw, "item") else raw
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        values.append(
            {
                "name": channels[i].get("label") or f"Channel {i}",
                "value": value,
            }
        )

    pixel_size = meta.get("pixel_size_um")
    um = None
    if pixel_size:
        py, px = float(pixel_size[0]), float(pixel_size[1])
        um = [xi * px, yi * py]
    calibration = meta.get("calibration") or {}
    path = str(level["path"])
    probed_level: int | str = int(path) if path.isdigit() else path
    return {
        "x": xi,
        "y": yi,
        "um": um,
        "values": values,
        "probed_level": probed_level,
        "sample_kind": (
            "overview-mean" if meta.get("kind") == "overview-degraded" else "native"
        ),
        "calibration": {
            "status": calibration.get("status", "unknown"),
            "source": calibration.get("source", "unknown"),
            "pixel_size_um": pixel_size,
        },
    }


def reveal_in_file_manager(path: Path, *, platform: str | None = None, runner=None) -> None:
    """Reveal one registered path in Finder without invoking a shell."""
    import subprocess
    import sys

    if (platform or sys.platform) != "darwin":
        raise RuntimeError("Reveal is available only on macOS")
    if not path.exists():
        raise FileNotFoundError(path)
    run = runner or subprocess.run
    run(["/usr/bin/open", "-R", str(path)], check=True, timeout=10)


def _close_state(st: ViewerState | None) -> None:
    if st is None:
        return
    close = getattr(st.root, "close", None)
    if close:
        close()


def store_generation(store_path: str | Path) -> str:
    """A token that changes whenever this store's pixels could have.

    A managed cache carries its manifest's generation uuid; anything else
    falls back to the metadata file's mtime. Tile URLs carry it, which is
    what lets the browser cache tiles at all: a rebuilt cache mints new
    URLs instead of reviving stale renders.
    """
    from .cache import CACHE_SUFFIX, read_manifest

    store_path = Path(store_path)
    if store_path.parent.name.endswith(CACHE_SUFFIX):
        m = read_manifest(store_path.parent)
        if m and m.get("generation"):
            return str(m["generation"])
    probe = store_path / ".zattrs" if store_path.is_dir() else store_path
    try:
        return format(probe.stat().st_mtime_ns, "x")
    except OSError:
        return ""


def rescue_annotations(folder: str | Path, home: str | Path) -> list[Path]:
    """Lift annotation sidecars out of ``folder`` before it is deleted.

    Annotations belong beside the slide, but a store built by an older
    version may hold them, and they are work rather than cache. Returns the
    files moved to safety.
    """
    import shutil

    folder, home = Path(folder), Path(home)
    home.mkdir(parents=True, exist_ok=True)
    saved = []
    for path in folder.rglob("annotations_*.json"):
        target = home / path.name
        if target.exists():
            continue
        try:
            shutil.move(str(path), str(target))
        except OSError:
            continue
        saved.append(target)
    return saved


def _annotation_source_name(path: Path) -> str | None:
    """Return a legacy sidecar's declared source name, when present."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
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


def annotations_sidecar(store_path: str | Path, attrs: dict[str, Any]) -> Path:
    """Return a sidecar path scoped to the source file and selected plane.

    An annotation belongs to one level-0 coordinate space. T, P, and Z are
    therefore part of its identity, just as they are part of cache identity.
    An older unscoped sidecar is claimed by the default plane and copied
    for any other, so every selection keeps seeing its 0.9 work.
    """
    from .cache import (
        ANNOTATIONS_DIR,
        CACHE_SUFFIX,
        MANAGED_DIR,
        read_manifest,
        selection_tag,
    )
    from .convert import CACHE_DIR_NAME

    store_path = Path(store_path).resolve()
    meta = attrs["nd2wsi"]
    source_name = Path(meta["source"]).name
    stem = Path(source_name).stem

    home = store_path.parent
    container = home if home.name.endswith(CACHE_SUFFIX) else None
    if container is not None:
        home = home.parent
    if home.name in (CACHE_DIR_NAME, "caches"):
        home = home.parent
    if home.name == MANAGED_DIR:
        home = home.parent

    manifest = read_manifest(container) if container is not None else None
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


def plate_annotations_sidecar(path: str | Path, attrs: dict[str, Any], p: int) -> Path:
    """The sidecar for one site of a plate file, beside the slide.

    Annotations on a plate are per site and shared across time and z, so
    the site index is the only scope in the name.
    """
    from .cache import ANNOTATIONS_DIR, MANAGED_DIR

    path = Path(path).resolve()
    source_name = Path(attrs["nd2wsi"]["source"]).name or path.name
    safe_source = re.sub(r'[\/:*?"<>|\x00-\x1f]+', "_", source_name)
    target_dir = path.parent / MANAGED_DIR / ANNOTATIONS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"annotations_{safe_source}--site{int(p)}.json"


class SlideRegistry:
    """The set of slides this server has open, keyed by a stable short id."""

    def __init__(self, max_render_mpx: float = 400.0):
        self.slides: dict[str, ViewerState] = {}  # insertion-ordered
        self.max_render_mpx = max_render_mpx
        self._lock = threading.Lock()

    @staticmethod
    def sid_for(store_path: Path) -> str:
        return hashlib.sha1(str(store_path.resolve()).encode()).hexdigest()[:8]

    def add_store(
        self,
        store_path: str | Path,
        *,
        trash_path: str | Path | None = None,
        source_path: str | Path | None = None,
    ) -> str:
        from .cache import CACHE_SUFFIX, read_manifest
        from .convert import CACHE_DIR_NAME, is_nd2wsi_store, open_store
        from .svs import is_svs

        store_path = Path(store_path).resolve()
        registered_source = (
            Path(source_path).expanduser().resolve() if source_path is not None else None
        )
        if store_path.suffix.lower() == ".nd2" and store_path.is_file():
            from .plate import is_plate_file

            if is_plate_file(store_path):
                return self.add_plate(store_path)
            raise ValueError(
                f"{store_path.name} is not a time series of sites; open it "
                "through open_path so its pyramid store is built first"
            )
        if trash_path is not None:
            trash_path = Path(trash_path).resolve()
        elif store_path.parent.name.endswith(CACHE_SUFFIX):
            trash_path = store_path.parent
        elif store_path.parent.name == CACHE_DIR_NAME and is_nd2wsi_store(
            store_path
        ):
            # a store inside a pyramids/ folder is a cache this app built
            # (0.5 through 0.7 layouts) and stays trashable; a store the
            # user opened at any other explicit path never is
            trash_path = store_path
        if is_svs(store_path):  # an SVS itself serves with no store
            slide_path = store_path
            try:
                return self.add_direct(slide_path)
            except NotImplementedError:
                from .convert import convert as _convert
                from .convert import default_store_path as _dsp

                built = _dsp(slide_path)
                if not built.exists():
                    _convert(slide_path, built, progress=False)
                store_path = built
                trash_path = built
                registered_source = registered_source or slide_path
        manifest: dict[str, Any] = {}
        container_path = None
        if store_path.parent.name.endswith(CACHE_SUFFIX):
            container_path = store_path.parent
            manifest = read_manifest(container_path) or {}
            registered_source = registered_source or _manifest_source_path(
                container_path, manifest
            )
            if manifest.get("kind") == "overview":
                return self._add_overview(
                    store_path, manifest, source_path=registered_source
                )
        sid = self.sid_for(store_path)
        gen = store_generation(store_path)
        stale = None
        with self._lock:
            st = self.slides.get(sid)
            if (
                st is not None
                and st.generation == gen
                and (registered_source is None or st.source_path == registered_source)
            ):
                return sid
            if st is not None:  # rebuilt underneath: the old root is stale
                stale = self.slides.pop(sid)
            root, attrs = open_store(store_path)
            st = ViewerState(
                root,
                attrs,
                max_render_mpx=self.max_render_mpx,
                annotations_path=annotations_sidecar(store_path, attrs),
                generation=gen,
                trash_path=Path(trash_path) if trash_path is not None else None,
                source_path=registered_source,
                store_path=store_path,
                container_path=container_path,
                manifest=manifest,
            )
            self.slides[sid] = st
        _close_state(stale)
        return sid

    def _add_overview(
        self,
        store_path: Path,
        manifest: dict,
        *,
        source_path: Path | None = None,
    ) -> str:
        """An overview store: the source file plays level 0 when it can.

        When the source is missing, changed, or cannot back a level at
        runtime, the stored overview still shows — at half resolution,
        with a note saying why — rather than failing to open at all.
        """
        from .cache import fingerprints_match, quick_fingerprint
        from .convert import open_store
        from .direct import open_nd2_backed

        sid = self.sid_for(store_path)
        gen = store_generation(store_path)
        stale = None
        with self._lock:
            src_info = manifest.get("source", {})
            source = source_path or _manifest_source_path(store_path.parent, manifest)
            if source is None:
                source = (store_path.parent / src_info.get("relative_path", "")).resolve()
            source_ok = False
            try:
                source_ok = source.is_file() and fingerprints_match(
                    src_info, quick_fingerprint(source)
                )
            except OSError:
                source_ok = False
            st = self.slides.get(sid)
            expected = gen + ("" if source_ok else "-degraded")
            if (
                st is not None
                and st.generation == expected
                and st.source_path == source
            ):
                return sid
            if st is not None:  # rebuilt or mode change: the state is stale
                stale = self.slides.pop(sid)
            root = attrs = None
            reason = f"{src_info.get('name', 'source')} is missing"
            if source.is_file() and not source_ok:
                reason = f"{source.name} has changed since this cache was built"
            if source_ok:
                try:
                    root, attrs = open_nd2_backed(source, store_path)
                except NotImplementedError as e:
                    reason = str(e)
            degraded = root is None
            if degraded:
                root, attrs = open_store(store_path)
                meta = attrs["nd2wsi"]
                meta["kind"] = "overview-degraded"
                for lv in meta["levels"]:
                    lv["downsample"] //= 2
                # the base level is now the half-resolution overview, so a
                # pixel is twice as large — otherwise every scalebar and
                # measurement would read half its true length
                if meta.get("pixel_size_um"):
                    meta["pixel_size_um"] = [v * 2 for v in meta["pixel_size_um"]]
                meta["notes"] = meta.get("notes", []) + [
                    f"{reason}; showing the stored overview at half resolution"
                ]
            st = ViewerState(
                root,
                attrs,
                max_render_mpx=self.max_render_mpx,
                # annotations live in level-0 pixels; a degraded view's base
                # is level 1, so editing here would silently corrupt them
                annotations_path=None
                if degraded
                else annotations_sidecar(store_path, attrs),
                # the two modes map levels differently, so tiles cached as
                # immutable in one must never be revived in the other
                generation=gen + ("-degraded" if degraded else ""),
                trash_path=store_path.parent,
                source_path=source,
                store_path=store_path,
                container_path=store_path.parent,
                manifest=manifest,
            )
            self.slides[sid] = st
        _close_state(stale)
        return sid

    def add_direct(self, slide_path: str | Path) -> str:
        """Serve an SVS straight from the file, writing nothing to disk."""
        from .cache import fingerprints_match, quick_fingerprint
        from .direct import open_direct

        slide_path = Path(slide_path).resolve()
        sid = self.sid_for(slide_path)
        stale = None
        with self._lock:
            fingerprint = quick_fingerprint(slide_path)
            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            st = self.slides.get(sid)
            if st is not None and st.generation == gen:
                return sid

            # Fingerprint before and after opening so the registered auxiliary
            # images can never come from a replacement path while the tiled
            # root still holds the previous inode.
            root = attrs = None
            for _ in range(2):
                root, attrs = open_direct(slide_path)
                try:
                    current = quick_fingerprint(slide_path)
                except Exception:
                    close = getattr(root, "close", None)
                    if close:
                        close()
                    raise
                if fingerprints_match(fingerprint, current):
                    fingerprint = current
                    break
                close = getattr(root, "close", None)
                if close:
                    close()
                root = attrs = None
                fingerprint = current
            if root is None or attrs is None:
                raise RuntimeError(f"{slide_path.name} changed while it was opening")

            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            stale = self.slides.get(sid)
            st = ViewerState(
                root,
                attrs,
                max_render_mpx=self.max_render_mpx,
                annotations_path=annotations_sidecar(slide_path, attrs),
                generation=gen,
                source_path=slide_path,
                manifest={"source": fingerprint},
            )
            self.slides[sid] = st
        _close_state(stale)
        return sid

    def add_plate(self, slide_path: str | Path) -> str:
        """Serve a time series of sites straight from the ND2, writing nothing."""
        from .cache import quick_fingerprint
        from .direct import _Root
        from .plate import PlateSource

        slide_path = Path(slide_path).resolve()
        sid = self.sid_for(slide_path)
        stale = None
        with self._lock:
            fingerprint = quick_fingerprint(slide_path)
            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            st = self.slides.get(sid)
            if st is not None and st.generation == gen and st.plate is not None:
                return sid
            source = PlateSource(slide_path)
            try:
                root = _Root(
                    source.root_for(0, 0, source.z_home), closer=source.close
                )
                annotations = plate_annotations_sidecar(slide_path, source.attrs, 0)
            except BaseException:
                source.close()
                raise
            stale = self.slides.get(sid)
            container = source.store.container if source.store is not None else None
            st = ViewerState(
                root,
                source.attrs,
                max_render_mpx=self.max_render_mpx,
                annotations_path=annotations,
                generation=gen,
                trash_path=container,
                source_path=slide_path,
                container_path=container,
                manifest={"source": fingerprint},
                plate=source,
            )
            self.slides[sid] = st
        _close_state(stale)
        return sid

    def open_path(self, path: str | Path, on_progress=None) -> str:
        """A slide file (converted on first open) or an existing store."""
        from .convert import ensure_cache, existing_cache_store
        from .svs import is_svs

        path = Path(path).expanduser().resolve()
        if path.suffix.lower() in SLIDE_SUFFIXES:
            if is_svs(path) and existing_cache_store(path) is None:
                try:
                    # the file already holds a pyramid, so serve it as it lies
                    return self.add_direct(path)
                except NotImplementedError:
                    pass  # untiled or off-ladder file: build a store instead
            if path.suffix.lower() == ".nd2":
                from .plate import is_plate_file

                if is_plate_file(path):
                    # camera fields over time: no pyramid to build, ever
                    return self.add_plate(path)
            store = ensure_cache(path, on_progress=on_progress)
        elif path.is_dir():  # a user-supplied *.ome.zarr store
            return self.add_store(path)
        else:
            raise ValueError(f"not an ND2/SVS slide or pyramid store: {path.name}")
        # no explicit trash_path: add_store promotes a container store to its
        # container, so trashing removes manifest and pixels together
        return self.add_store(store, source_path=path)

    def remove(self, sid: str) -> bool:
        with self._lock:
            st = self.slides.pop(sid, None)
        _close_state(st)
        return st is not None

    def close_all(self, *, immediate: bool = False) -> None:
        """Close every registered backend after the server stops.

        Normal tab closure defers native-handle teardown briefly so in-flight
        requests can finish. A stopped smoke-test server has no such requests,
        so it may close source-backed roots synchronously.
        """
        with self._lock:
            states = list(self.slides.values())
            self.slides.clear()
        for st in states:
            st.busy.close(timeout=30)
            close = getattr(st.root, "close", None)
            if close is None:
                continue
            if immediate:
                try:
                    close(delay=0)
                    continue
                except TypeError:
                    pass
            close()

    def trash_cache(self, sid: str, on_progress=None) -> int:
        """Close the slide and delete its pyramid store. Returns bytes freed.

        Deletes only the registered store directory, never the source slide,
        and never an annotation sidecar that ended up inside it. A store is
        hundreds of thousands of small files, so the caller gets a fraction
        as the files go.
        """
        import os

        with self._lock:
            st = self.slides.get(sid)
            if st is None:
                raise KeyError(sid)
            if st.trash_path is None:
                raise ValueError("this slide has no cache managed by this app")
            store = Path(st.trash_path)
            from .cache import CACHE_SUFFIX

            if store.name.endswith(".ome.zarr") and store.parent.name.endswith(
                CACHE_SUFFIX
            ):
                store = store.parent  # the container owns manifest and store
            valid_container = store.is_dir() and store.name.endswith(CACHE_SUFFIX)
            valid_store = store.is_dir() and store.name.endswith(".ome.zarr")
            if not (valid_container or valid_store):
                raise ValueError(f"refusing to delete {store}: not a managed cache")
            if st.busy.active:
                raise ValueError(
                    "an export from this slide is still running — try again "
                    "when it finishes"
                )
            self.slides.pop(sid, None)
        # bar any export that raced the check above and wait it out, so the
        # deletion below can never zero-fill a file someone is still writing
        st.busy.close(timeout=30)
        # a plate's builder writes chunks straight into the container, and
        # zarr makes any directory it writes to, so a builder left running
        # would put the store back moments after the delete reported it
        # gone, this time without its manifest
        if st.plate is not None and st.plate.store is not None:
            st.plate.store.close()
        close = getattr(st.root, "close", None)
        if close:  # a source-backed slide holds the ND2 memory map open
            close()

        from .cache import ANNOTATIONS_DIR, CACHES_DIR, MANAGED_DIR, CacheLock
        from .convert import CACHE_DIR_NAME

        if st.annotations_path is not None:
            annotation_home = st.annotations_path.parent
        elif store.name.endswith(CACHE_SUFFIX) and store.parent.name == CACHES_DIR:
            annotation_home = store.parent.parent / ANNOTATIONS_DIR
        elif store.parent.name == CACHE_DIR_NAME:
            annotation_home = store.parent.parent / MANAGED_DIR / ANNOTATIONS_DIR
        else:
            annotation_home = store.parent / MANAGED_DIR / ANNOTATIONS_DIR
        annotation_home.mkdir(parents=True, exist_ok=True)
        rescue_annotations(store, annotation_home)

        # under the build lock, the doomed directory is renamed aside
        # before deletion: a concurrent opener either sees the intact
        # container or none at all, never a half-deleted one
        try:
            lock = CacheLock(store)
            lock.acquire(timeout=10)
        except TimeoutError:
            raise ValueError(
                "this slide's cache is being rebuilt right now — try again "
                "when the build finishes"
            ) from None
        try:
            doomed = store.with_name(f".{store.name}.trashing-{os.getpid()}")
            store.rename(doomed)
        finally:
            lock.release()
        store = doomed
        # two passes, neither holding a path list: a store is hundreds of
        # thousands of files and their names alone would be real memory
        total = 0
        freed = 0
        for walk_root, _, files in os.walk(store):
            total += len(files)
            for name in files:
                try:
                    freed += os.stat(os.path.join(walk_root, name)).st_size
                except OSError:
                    pass
        total = max(1, total)
        step = max(1, total // 100)  # a hundred ticks, whatever the size
        done = 0
        for walk_root, _, files in os.walk(store):
            for name in files:
                try:
                    os.unlink(os.path.join(walk_root, name))
                except OSError:
                    pass
                done += 1
                if on_progress and done % step == 0:
                    on_progress(done / total)
        for root, dirs, _ in os.walk(store, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        try:
            store.rmdir()
        except OSError:
            pass
        if on_progress:
            on_progress(1.0)
        return freed

    def default_sid(self) -> str | None:
        with self._lock:
            return next(iter(self.slides), None)

    def get(self, sid: str | None) -> ViewerState | None:
        with self._lock:
            if sid is None:
                sid = next(iter(self.slides), None)
            return self.slides.get(sid) if sid else None

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.slides.items())
        out = []
        for sid, st in items:
            meta = st.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            out.append(
                {
                    "sid": sid,
                    "name": meta["source"],
                    "width": lv0["width"],
                    "height": lv0["height"],
                    "rgb": meta["rgb"],
                }
            )
        return out


def content_disposition(filename: str) -> str:
    """An attachment header safe for any filename.

    HTTP headers travel as latin-1; a Korean or accented slide name used
    to raise UnicodeEncodeError after the export was already written. The
    ASCII fallback keeps old clients working and the RFC 5987 filename*
    carries the real name.
    """
    import urllib.parse as _up

    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    utf8_name = _up.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


def make_handler(
    registry: SlideRegistry, token: str = ""
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "nd2wsi"

        # ---- helpers -----------------------------------------------------
        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str,
            extra: dict | None = None,
            cache: str = "no-store",
        ):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _error(self, code: int, msg: str):
            self._json({"error": msg}, code)

        def _body_json(self, limit: int = 10_000_000) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= limit:
                raise ValueError("payload missing or too large")
            return json.loads(self.rfile.read(n))

        def log_message(self, fmt, *args):  # quieter default log
            pass

        def _resolve(self, path: str) -> tuple[ViewerState | None, str]:
            """Map a URL path to (slide state, slide-relative path)."""
            m = SLIDE_RE.match(path)
            if m:
                st = registry.get(m.group(1))
                return st, (m.group(2) or "/")
            if path.startswith("/api/"):  # bare API -> first slide (scripts)
                return registry.get(None), path
            return None, path

        # ---- routing -----------------------------------------------------
        def _gate(self) -> str | None:
            """Strip and verify the capability prefix; None means rejected.

            Every route lives under /<token>/. A request without the right
            token gets a bare 404 — tiles, downloads and API alike — so a
            local process or a web page cannot even probe the server. The
            Host header must name loopback, and any Origin present must be
            this server itself.
            """
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            raw_host = self.headers.get("Host") or ""
            if raw_host.startswith("["):  # bracketed IPv6, maybe with a port
                host = raw_host[: raw_host.find("]") + 1]
            else:
                host = raw_host.rsplit(":", 1)[0]
            if host.lower() not in ("127.0.0.1", "localhost", "[::1]", ""):
                self._error(404, "not found")
                return None
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).hostname not in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                self._error(404, "not found")
                return None
            if not token:
                return path
            prefix = f"/{token}"
            if path == prefix:
                # redirect rather than rewrite: relative asset and API URLs
                # only resolve when the document URL ends with the slash
                target = prefix + "/"
                if parsed.query:
                    target += "?" + parsed.query
                self._send(
                    HTTPStatus.PERMANENT_REDIRECT, b"", "text/plain",
                    {"Location": target},
                )
                return None
            if not path.startswith(prefix + "/"):
                self._error(404, "not found")
                return None
            return path[len(prefix) :]

        def do_GET(self):  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = self._gate()
                if path is None:
                    return
                q = urllib.parse.parse_qs(parsed.query)
                if path == "/":
                    return self._static("shell.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/") :])
                if path == "/api/slides":
                    return self._json({"slides": registry.listing()})
                if path == "/api/roi/progress" or path.endswith("/api/roi/progress"):
                    job = (q.get("job") or [""])[0]
                    if not JOB_RE.match(job):
                        return self._error(400, "bad job id")
                    return self._json(_job_get(job))
                if path == "/favicon.ico":
                    return self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")

                st, sub = self._resolve(path)
                if sub == "/" and st is not None:
                    return self._static("index.html")
                if st is None:
                    return self._error(404, f"no slide for {path}")
                if sub == "/api/info":
                    return self._info(st)
                if sub == "/api/inspect":
                    return self._inspect(st)
                if sub == "/api/pixel":
                    return self._pixel(st, q)
                if sub == "/api/histogram":
                    return self._histogram(st, q)
                if sub == "/api/annotations":
                    return self._annotations_get(st, q)
                associated = ASSOCIATED_RE.match(sub)
                if associated:
                    return self._associated(st, associated.group(1))
                m = TILE_RE.match(sub)
                if m:
                    return self._tile(st, m, q)
                m = PLATE_FRAME_RE.match(sub)
                if m:
                    return self._plate_frame(st, m, q)
                if sub == "/api/plate/status":
                    if st.plate is None:
                        return self._error(404, "not a plate slide")
                    return self._json(st.plate.status())
                if sub == "/api/plate/focus":
                    if st.plate is None:
                        return self._error(404, "not a plate slide")
                    return self._json(st.plate.focus_map())
                if sub == "/api/roi":
                    return self._roi(st, q)
                return self._error(404, f"no route for {path}")
            except BrokenPipeError:
                pass
            except Exception as e:  # pragma: no cover - defensive
                try:
                    self._error(500, f"{type(e).__name__}: {e}")
                except Exception:
                    pass

        def do_POST(self):  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = self._gate()
                if path is None:
                    return
                q = urllib.parse.parse_qs(parsed.query)
                ctype = (self.headers.get("Content-Type") or "").split(";")[0]
                if ctype.strip().lower() != "application/json":
                    return self._error(415, "expected application/json")
                if path == "/api/open":
                    return self._open()
                if path == "/api/close":
                    return self._close()
                if path == "/api/trash":
                    return self._trash()
                st, sub = self._resolve(path)
                if st is not None and sub == "/api/annotations":
                    return self._annotations_post(st, q)
                if st is not None and sub == "/api/reveal":
                    return self._reveal(st)
                return self._error(404, f"no POST route for {path}")
            except BrokenPipeError:
                pass
            except Exception as e:  # pragma: no cover - defensive
                try:
                    self._error(500, f"{type(e).__name__}: {e}")
                except Exception:
                    pass

        # ---- global endpoints --------------------------------------------
        def _static(self, rel: str):
            file = (STATIC_DIR / rel).resolve()
            if not file.is_relative_to(STATIC_DIR.resolve()) or not file.is_file():
                return self._error(404, "not found")
            st = file.stat()
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            if self.headers.get("If-None-Match") == etag:
                return self._send(
                    HTTPStatus.NOT_MODIFIED, b"", "text/plain",
                    {"ETag": etag}, cache="private, no-cache",
                )
            body = file.read_bytes()
            self._send(
                200, body, MIME.get(file.suffix, "application/octet-stream"),
                {"ETag": etag}, cache="private, no-cache",
            )

        def _open(self):
            job = None
            try:
                data = self._body_json()
                path = data.get("path") if isinstance(data, dict) else None
                if not path:
                    return self._error(400, 'expected {"path": "..."}')
                job = data.get("job") if isinstance(data, dict) else None
                if job and not JOB_RE.match(str(job)):
                    job = None
                on_progress = None
                if job:
                    _job_update(job, state="converting", pct=0)

                    def on_progress(frac: float) -> None:
                        _job_update(
                            job, state="converting", pct=int(min(1.0, frac) * 100)
                        )

                sid = registry.open_path(path, on_progress=on_progress)
                if job:
                    _job_update(job, state="done", pct=100)
            except (
                ValueError,
                FileNotFoundError,
                OSError,
                NotImplementedError,
                RuntimeError,
            ) as e:
                _job_update(job, state="error", error=str(e))
                return self._error(400, str(e))
            except json.JSONDecodeError as e:
                return self._error(400, f"invalid JSON: {e}")
            return self._json({"sid": sid, "slides": registry.listing()})

        def _trash(self):
            try:
                data = self._body_json(limit=10_000)
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            sid = data.get("sid") if isinstance(data, dict) else None
            job = data.get("job") if isinstance(data, dict) else None
            if job and not JOB_RE.match(str(job)):
                job = None
            on_progress = None
            if job:
                _job_update(job, state="deleting", pct=0)

                def on_progress(frac: float) -> None:
                    _job_update(job, state="deleting", pct=int(min(1.0, frac) * 100))

            try:
                freed = registry.trash_cache(sid or "", on_progress=on_progress)
                if job:
                    _job_update(job, state="done", pct=100)
            except KeyError:
                return self._error(404, f"no open slide {sid}")
            except (ValueError, OSError) as e:
                return self._error(400, str(e))
            return self._json(
                {"ok": True, "freed": freed, "slides": registry.listing()}
            )

        def _close(self):
            try:
                data = self._body_json(limit=10_000)
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            sid = data.get("sid") if isinstance(data, dict) else None
            if not sid or not registry.remove(sid):
                return self._error(404, f"no open slide {sid}")
            return self._json({"ok": True, "slides": registry.listing()})

        # ---- per-slide endpoints -----------------------------------------
        def _info_payload(self, st: ViewerState) -> dict[str, Any]:
            meta = st.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            return {
                "name": meta["source"],
                "width": lv0["width"],
                "height": lv0["height"],
                "tileSize": meta["tile"],
                "dtype": meta["dtype"],
                "rgb": meta["rgb"],
                "pixelSizeUm": meta.get("pixel_size_um"),
                "calibrated": bool(meta.get("pixel_size_um")),
                "levels": meta["levels"],
                "selection": meta.get("selection", {}),
                "notes": meta.get("notes", []),
                "channels": [
                    {
                        "label": ch["label"],
                        "color": ch["color"],
                        "window": ch["window"],
                    }
                    for ch in st.attrs["omero"]["channels"]
                ],
                "maxRenderMpx": st.max_render_mpx,
                "nd2Export": _limnd2_available(),
                "direct": bool(meta.get("direct")),
                "trashable": st.trash_path is not None,
                "generation": st.generation,
                "storage": _storage_mode(meta),
                "kind": "plate" if meta.get("plate") else "slide",
                "plate": self._plate_block(st),
            }

        def _plate_block(self, st: ViewerState) -> dict[str, Any] | None:
            meta = st.attrs["nd2wsi"]
            block = meta.get("plate")
            if not block or st.plate is None:
                return block
            status = st.plate.status()
            return {
                **block,
                "thumbsDone": status["done"],
                "thumbsTotal": status["total"],
                "cachePath": status["path"],
            }

        def _info(self, st: ViewerState):
            self._json(self._info_payload(st))

        def _inspect(self, st: ViewerState):
            meta = st.attrs["nd2wsi"]
            source_usage = _path_usage(st.source_path)
            cache_usage = st.cache_usage()
            storage_details = _inspection_storage_details(st, meta)
            calibration = dict(meta.get("calibration") or {})
            calibration.setdefault("status", "unknown")
            calibration.setdefault("source", "unknown")
            calibration["pixel_size_um"] = meta.get("pixel_size_um")
            payload = {
                **self._info_payload(st),
                "source_path": str(st.source_path) if st.source_path else None,
                "store_path": str(st.store_path) if st.store_path else None,
                "container_path": (
                    str(st.container_path) if st.container_path else None
                ),
                "cache_path": str(st.cache_path) if st.cache_path else None,
                "source_bytes": source_usage["bytes"] if source_usage else None,
                "source_allocated_bytes": (
                    source_usage["allocated_bytes"] if source_usage else None
                ),
                "cache_bytes": cache_usage["bytes"] if cache_usage else None,
                "cache_allocated_bytes": (
                    cache_usage["allocated_bytes"] if cache_usage else None
                ),
                "storage_details": storage_details,
                "calibration": calibration,
                "objective": meta.get("objective_magnification"),
                "objective_magnification": meta.get("objective_magnification"),
                "selection": dict(meta.get("selection") or {}),
                "associated": _associated_names(st),
            }
            self._json(payload)

        def _pixel(self, st: ViewerState, q: dict):
            try:
                x_raw = (q.get("x") or [None])[0]
                y_raw = (q.get("y") or [None])[0]
                if x_raw is None or y_raw is None:
                    raise ValueError("missing parameter x or y")
                x, y = float(x_raw), float(y_raw)
                if not math.isfinite(x) or not math.isfinite(y):
                    raise ValueError("x and y must be finite numbers")
                frame = _frame_args(st, q)
                root = st.plate.root_for(*frame) if frame is not None else None
                payload = _pixel_payload(st, x, y, root=root)
            except (TypeError, ValueError) as e:
                return self._error(400, str(e))
            self._json(payload)

        def _associated(self, st: ViewerState, name: str):
            source = _associated_source_path(st)
            if source is None:
                return self._error(404, "associated image source is unavailable")
            try:
                from .svs import associated_image_jpeg

                body = associated_image_jpeg(source, name)
            except (KeyError, OSError, ValueError):
                return self._error(404, f"associated image {name} is unavailable")
            # The path may have been atomically replaced while it was decoded.
            # In that case discard the body instead of mixing auxiliary pixels
            # from a different file with the already-open primary slide root.
            if _associated_source_path(st) != source:
                return self._error(404, "associated image source changed")
            self._send(200, body, "image/jpeg")

        def _reveal(self, st: ViewerState):
            import subprocess

            try:
                data = self._body_json(limit=1000)
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            which = data.get("which") if isinstance(data, dict) else None
            if which not in ("source", "cache"):
                return self._error(400, 'expected {"which": "source"|"cache"}')
            target = st.source_path if which == "source" else st.cache_path
            if target is None or not target.exists():
                return self._error(404, f"registered {which} path is unavailable")
            try:
                reveal_in_file_manager(target)
            except FileNotFoundError:
                return self._error(404, f"registered {which} path is unavailable")
            except RuntimeError as e:
                return self._error(HTTPStatus.NOT_IMPLEMENTED, str(e))
            except (OSError, subprocess.SubprocessError) as e:
                return self._error(500, f"could not reveal {which}: {e}")
            self._json({"ok": True, "which": which})

        def _plate_site(self, st: ViewerState, q: dict) -> tuple[Path | None, int | None]:
            """(sidecar path, site index) for the annotations routes."""
            if st.plate is None:
                return st.annotations_path, None
            raw = (q.get("p") or ["0"])[0]
            try:
                p = int(raw)
            except (TypeError, ValueError):
                raise ValueError("p must be an integer") from None
            if not 0 <= p < st.plate.P:
                raise ValueError(f"p={p} out of range (0..{st.plate.P - 1})")
            if st.source_path is None:
                return None, p
            return plate_annotations_sidecar(st.source_path, st.attrs, p), p

        def _annotations_get(self, st: ViewerState, q: dict | None = None):
            try:
                p, _site = self._plate_site(st, q or {})
            except ValueError as e:
                return self._error(400, str(e))
            if p is None:
                return self._json({"items": [], "path": None})
            if p.exists():
                try:
                    data = json.loads(p.read_text())
                    items = data.get("items", []) if isinstance(data, dict) else []
                except (json.JSONDecodeError, OSError) as e:
                    return self._error(500, f"could not read {p.name}: {e}")
            else:
                items = []
            self._json({"items": items, "path": str(p)})

        def _annotations_post(self, st: ViewerState, q: dict | None = None):
            import uuid as _uuid

            try:
                p, site = self._plate_site(st, q or {})
            except ValueError as e:
                return self._error(400, str(e))
            if p is None:
                return self._error(400, "no annotation sidecar path for this store")
            try:
                data = self._body_json()
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return self._error(400, "expected {\"items\": [...]}")
            meta = st.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            cal = meta.get("calibration", {})
            ps = meta.get("pixel_size_um")
            payload = {
                "format": "nd2wsi-annotations/2",
                "coordinate_space": "level-0-pixels",
                "source": {
                    "name": meta["source"],
                    "width": lv0["width"],
                    "height": lv0["height"],
                },
                "calibration": {
                    "status": cal.get("status", "unknown"),
                    "source": cal.get("source", "unknown"),
                    **(
                        {"y_um_per_px": ps[0], "x_um_per_px": ps[1]}
                        if ps
                        else {}
                    ),
                },
                "selection": meta.get("selection", {}),
                "items": data["items"],
            }
            if site is not None:
                payload["selection"] = {"p": site}
                sites = (meta.get("plate") or {}).get("sites") or []
                payload["site"] = sites[site]["name"] if site < len(sites) else None
            # unique temp + per-slide lock: two tabs saving at once cannot
            # interleave through one shared temp name
            with st.lock:
                tmp = p.with_name(f".{p.name}.{_uuid.uuid4().hex[:8]}.tmp")
                tmp.write_text(json.dumps(payload, indent=1))
                tmp.replace(p)  # atomic
            self._json({"ok": True, "path": str(p), "count": len(data["items"])})

        def _histogram(self, st: ViewerState, q: dict | None = None):
            if st.plate is not None:
                try:
                    t, p, z = _frame_args(st, q or {})
                except ValueError as e:
                    return self._error(400, str(e))
                return self._json({"channels": st.plate.histogram(t, p, z)})
            with st.lock:
                if st.histograms is None:
                    st.histograms = render.compute_histograms(st.root, st.attrs)
            self._json({"channels": st.histograms})

        def _immutable_when_current(self, st: ViewerState, q: dict) -> str:
            # a URL that names the cache generation can be cached hard: a
            # rebuild mints a new generation, hence new URLs, so a stale
            # render can never be revived from the browser cache
            gen = (q.get("g") or [None])[0]
            return (
                "private, max-age=31536000, immutable"
                if gen and gen == st.generation
                else "no-store"
            )

        def _tile(self, st: ViewerState, m: re.Match, q: dict):
            level, tx, ty = int(m.group(1)), int(m.group(2)), int(m.group(3))
            fmt = m.group(4)
            n = len(st.attrs["omero"]["channels"])
            channels = render.parse_channels((q.get("c") or [None])[0], n)
            win = (q.get("win") or [None])[0]
            try:
                frame = _frame_args(st, q)
            except ValueError as e:
                return self._error(400, str(e))
            root = st.plate.root_for(*frame) if frame is not None else st.root
            try:
                body = render.render_tile(
                    root, st.attrs, level, tx, ty, channels, fmt, win
                )
            except KeyError as e:
                return self._error(404, str(e))
            ctype = "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"
            self._send(200, body, ctype, cache=self._immutable_when_current(st, q))

        def _plate_frame(self, st: ViewerState, m: re.Match, q: dict):
            """A reduced frame of one site for the grid."""
            plate = st.plate
            if plate is None:
                return self._error(404, "not a plate slide")
            t, p, z = int(m.group(1)), int(m.group(2)), int(m.group(3))
            fmt = m.group(4)
            if not (t < plate.T and p < plate.P and z < plate.Z):
                return self._error(404, "frame out of range")
            raw_k = (q.get("k") or ["8"])[0]
            try:
                k = int(raw_k)
            except (TypeError, ValueError):
                k = -1
            if k not in (2, 4, 8, 16):
                return self._error(400, "k must be 2, 4, 8 or 16")
            n = len(st.attrs["omero"]["channels"])
            channels = render.parse_channels((q.get("c") or [None])[0], n)
            win = (q.get("win") or [None])[0]
            try:
                body = plate.render_frame(t, p, z, k, channels, fmt, win)
            except ValueError as e:
                if "closed" in str(e):
                    return self._error(409, "slide was closed")
                return self._error(400, str(e))
            ctype = "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"
            self._send(200, body, ctype, cache=self._immutable_when_current(st, q))
            if k == 8:  # neighbours of the stored reduction only; a sharper pass is on demand
                try:
                    plate.prefetch(t, z, k)
                except Exception:  # pragma: no cover - prefetch never reaches a client
                    pass

        def _roi(self, st: ViewerState, q: dict):
            try:
                with st.busy:
                    return self._roi_impl(st, q)
            except ValueError as e:
                if "closed" in str(e):
                    return self._error(409, "slide was closed")
                raise

        def _roi_impl(self, st: ViewerState, q: dict):
            def qi(name: str, default: int | None = None) -> int:
                v = q.get(name)
                if not v:
                    if default is None:
                        raise ValueError(f"missing parameter {name}")
                    return default
                return int(float(v[0]))

            meta = st.attrs["nd2wsi"]
            level = qi("level", 0)
            try:
                lv = render.level_entry(meta["levels"], level)
            except KeyError:
                return self._error(400, f"level {level} out of range")
            lw, lh = lv["width"], lv["height"]
            try:
                x, y, w, h = qi("x"), qi("y"), qi("w"), qi("h")
            except ValueError as e:
                return self._error(400, str(e))
            x, y = max(0, min(x, lw - 1)), max(0, min(y, lh - 1))
            w, h = max(1, min(w, lw - x)), max(1, min(h, lh - y))
            fmt = (q.get("format") or ["nd2"])[0].lower()
            n = len(st.attrs["omero"]["channels"])
            channels = render.parse_channels((q.get("c") or [None])[0], n)
            win = (q.get("win") or [None])[0]
            job = (q.get("job") or [None])[0]
            if job and not JOB_RE.match(job):
                job = None
            stem = Path(meta["source"]).stem
            try:
                frame = _frame_args(st, q)
            except ValueError as e:
                return self._error(400, str(e))
            root = st.root
            if frame is not None:
                root = st.plate.root_for(*frame)
                stem += "_t{}_p{}_z{}".format(*frame)
            fname = f"{stem}_L{level}_x{x}_y{y}_{w}x{h}"

            if fmt in ("nd2", "tif", "tiff"):
                # write through an on-disk temp file so RAM stays bounded for
                # arbitrarily large regions, then stream it to the client
                is_nd2 = fmt == "nd2"
                ext = ".nd2" if is_nd2 else ".tif"
                ctype = "application/octet-stream" if is_nd2 else "image/tiff"
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                _job_update(job, state="writing", pct=0)

                def on_progress(frac: float) -> None:
                    _job_update(job, state="writing", pct=int(min(1.0, frac) * 100))

                try:
                    tmp.close()
                    if is_nd2:
                        from .export_nd2 import export_roi_nd2

                        try:
                            export_roi_nd2(
                                root, st.attrs, tmp.name,
                                level, x, y, w, h, channels,
                                on_progress=on_progress,
                            )
                        except (RuntimeError, ValueError) as e:
                            _job_update(job, state="error", error=str(e))
                            return self._error(400, str(e))
                    else:
                        render.export_roi_tiff(
                            root, st.attrs, tmp.name,
                            level, x, y, w, h, channels,
                            on_progress=on_progress,
                        )
                    _job_update(job, state="streaming", pct=100)
                    size = Path(tmp.name).stat().st_size
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(size))
                    self.send_header(
                        "Content-Disposition", content_disposition(f"{fname}{ext}")
                    )
                    self.end_headers()
                    with open(tmp.name, "rb") as fh:
                        while True:
                            buf = fh.read(1024 * 1024)
                            if not buf:
                                break
                            self.wfile.write(buf)
                    _job_update(job, state="done", pct=100)
                except BrokenPipeError:
                    _job_update(job, state="error", error="client disconnected")
                    raise
                except Exception as e:
                    _job_update(job, state="error", error=f"{type(e).__name__}: {e}")
                    raise
                finally:
                    Path(tmp.name).unlink(missing_ok=True)
                return

            if fmt in ("png", "jpg", "jpeg"):
                if w * h / 1e6 > st.max_render_mpx:
                    return self._error(
                        400,
                        f"rendered export capped at {st.max_render_mpx:.0f} MPx; "
                        f"requested {w * h / 1e6:.0f} MPx -- use format=tiff "
                        "(streams any size) or a higher level",
                    )
                body = render.export_roi_rendered(
                    root, st.attrs, level, x, y, w, h, channels, fmt, win
                )
                ext = "png" if fmt == "png" else "jpg"
                ctype = "image/png" if fmt == "png" else "image/jpeg"
                return self._send(
                    200,
                    body,
                    ctype,
                    {"Content-Disposition": content_disposition(f"{fname}.{ext}")},
                )
            return self._error(400, f"unknown format {fmt}")

    return Handler


def create_server(
    store_paths: str | Path | list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8000,
    max_render_mpx: float = 400.0,
) -> ThreadingHTTPServer:
    """Build the viewer HTTP server without running it (port 0 = ephemeral).

    The caller runs ``httpd.serve_forever()`` -- directly (CLI) or in a
    thread (the macOS app shell). More slides can be added afterwards via
    ``httpd.registry`` or POST /api/open.
    """
    import ipaddress
    import secrets

    try:
        is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise ValueError(
            f"refusing to bind {host}: this server has no authentication "
            "beyond its capability URL and serves local files -- it binds "
            "loopback only"
        )

    if isinstance(store_paths, (str, Path)):
        store_paths = [store_paths]
    registry = SlideRegistry(max_render_mpx=max_render_mpx)
    for p in store_paths:
        registry.add_store(p)
    token = secrets.token_urlsafe(16)

    class _Server(ThreadingHTTPServer):
        # OpenSeadragon opens many tile connections in one burst; the
        # stdlib default backlog of 5 resets the overflow at the kernel.
        request_queue_size = 64
        daemon_threads = True

        def _close_registry(self) -> None:
            if getattr(self, "_registry_closed", False):
                return
            self._registry_closed = True
            registry.close_all(immediate=True)

        def shutdown(self) -> None:
            super().shutdown()
            self._close_registry()

        def server_close(self) -> None:
            self._close_registry()
            super().server_close()

    httpd = _Server((host, port), make_handler(registry, token))
    httpd.registry = registry  # for embedders
    httpd.token = token
    return httpd


def server_url(httpd: ThreadingHTTPServer, host: str = "127.0.0.1") -> str:
    """The capability URL — the only address that reaches this server."""
    return f"http://{host}:{httpd.server_address[1]}/{httpd.token}/"


def serve(
    store_paths: str | Path | list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8000,
    max_render_mpx: float = 400.0,
) -> None:
    httpd = create_server(
        store_paths, host=host, port=port, max_render_mpx=max_render_mpx
    )
    for s in httpd.registry.listing():
        print(f"serving {s['name']}  {s['width']} x {s['height']} px")
    print(f"open   {server_url(httpd, host)}   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
