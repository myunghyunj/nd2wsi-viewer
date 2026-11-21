"""SlideRegistry, ViewerState, open-slide lifecycle, handle caching, and cleanup helpers.

Extracted from ``nd2wsi.server`` so slide registration, handle lifecycles,
pyramid replacement/refresh, and safe cache teardown are decoupled from HTTP
request routing.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import render, ui_settings, windows_fs
from .annotation_sidecars import (
    SLIDE_SUFFIXES,
    _manifest_source_path,
    annotations_sidecar,
    confocal_annotations_sidecar,
    plate_annotations_sidecar,
)
from .astromap_export import AstromapDownloads
from .cache_removal import (
    _delete_verified_cache_file,
    _delete_verified_cache_tree,
    _same_file_identity,
    _validate_file_cache_for_trash,
    rescue_annotations,
)
from .direct_common import _Lifecycle
from .server_series import (
    _inspect_path_series,
    _path_usage,
    _series_item_is_stack,
)
from .windows_fs import is_windows_name_surrogate_reparse


def _resolve_server_hook(name: str, local_value: Any, original_value: Any) -> Any:
    """Honor monkeypatches applied either directly here or on ``nd2wsi.server``."""
    if local_value is not original_value:
        return local_value
    server_mod = sys.modules.get("nd2wsi.server")
    if server_mod is not None:
        return getattr(server_mod, name, local_value)
    return local_value


def _resolve_reparse_check() -> Any:
    """Honor monkeypatches on ``server_registry``, ``windows_fs``, or ``server``."""
    if is_windows_name_surrogate_reparse is not _ORIG_IS_REPARSE:
        return is_windows_name_surrogate_reparse
    if windows_fs.is_windows_name_surrogate_reparse is not _ORIG_IS_REPARSE:
        return windows_fs.is_windows_name_surrogate_reparse
    server_mod = sys.modules.get("nd2wsi.server")
    if server_mod is not None:
        return getattr(
            server_mod,
            "is_windows_name_surrogate_reparse",
            windows_fs.is_windows_name_surrogate_reparse,
        )
    return windows_fs.is_windows_name_surrogate_reparse


def _is_isolated_settings_env() -> bool:
    if any(
        os.environ.get(key)
        for key in (
            "ND2WSI_APP_SUPPORT_DIR",
            "ND2WSI_UI_SETTINGS_ROOT",
            "ND2WSI_UI_SETTINGS_PATH",
            "ND2WSI_LUT_SETTINGS_ROOT",
        )
    ):
        return True
    try:
        home = Path.home().resolve()
        tmp_root = Path(tempfile.gettempdir()).resolve()
        if home.is_relative_to(tmp_root) or "/pytest-" in str(home) or "\\pytest-" in str(home):
            return True
    except OSError:
        pass
    return False


def _auto_record_allowed() -> bool:
    """Skip implicit recent-file writes to real user Application Support during bare pytest runs."""
    if "PYTEST_CURRENT_TEST" in os.environ and not _is_isolated_settings_env():
        return False
    return True


def _sidecar_items(document: Any, *, require_items: bool = False) -> list:
    """The items of an annotation sidecar, which must be a JSON object.

    A window snapshot without items is empty (a missing one reads as {}, and
    the window writer keeps other keys). A file the browser server rewrites
    whole on save must name its items (require_items), or a document of some
    other kind would be replaced by the pane's empty list. Anything else
    raises ValueError.
    """
    if not isinstance(document, dict):
        raise ValueError("annotation document must be a JSON object")
    if require_items and "items" not in document:
        raise ValueError("annotation document has no items list")
    items = document.get("items", [])
    if not isinstance(items, list):
        raise ValueError("annotation items must be a list")
    return items


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
        managed_cache: bool = True,
    ):
        self.root = root
        self.attrs = attrs
        self.plate = plate  # a PlateSource when the slide is a time series of sites
        self.managed_cache = managed_cache
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
        # Every slide read (tiles, pixels, histograms, exports) holds busy;
        # close, replacement and trash drain it before any handle goes away.
        self.busy = _Lifecycle()
        # Only real exports (streamed ROI files, large renders, AstroMap)
        # enter this, inside busy, except the AstroMap zip download: it reads
        # the window's own file, holds only this, and AstromapDownloads keeps
        # that file until the reader is done. Close and update wait for
        # these, never for tile reads or export previews.
        self.exports = _Lifecycle()
        # zarr reads are thread-safe; this lock is kept for attrs, the LUT
        # store's one-time setup and browser-mode annotation writes
        self.lock = threading.Lock()
        self._hist_lock = threading.Lock()  # one histogram computation at a time
        self._inspect_lock = threading.Lock()
        self._cache_usage_ready = False
        self._cache_usage: dict[str, int] | None = None
        self._lut_settings_store = None
        self._shading = None
        self._shading_closed = False
        self._shading_lock = threading.Lock()
        self._replacement_lock = threading.RLock()
        self._closed = False
        # Pairs _closed with busy's door and is never held while waiting:
        # a replacement or refresh that drained reads readmits them only
        # while the slide is open.
        self._admission = threading.Lock()
        self.correction_write_allowed = True
        self._cache_identity = self._pyramid_identity()

    @property
    def shading(self):
        """Persistent, source-bound correction of the registered display pyramid."""
        with self._shading_lock:
            if self._shading is None:
                if self._shading_closed:
                    # A request that resolved this slide before it closed
                    # must not start a correction on the retired state.
                    raise ValueError("This slide's Shading Correction controller is closed")
                from .h_correction import ShadingCorrectionController

                self._shading = ShadingCorrectionController(self)
            return self._shading

    def close_shading(self, *, blocking: bool = True) -> bool:
        """Close Shading Correction, mark the slide closed and bar its reads.

        None of that waits. A pyramid replacement or refresh already past
        its closed check holds the replacement lock while it drains reads,
        for up to 30 s; it still finishes, but can no longer readmit reads.
        With ``blocking`` this then waits for it to let go; with
        ``blocking`` False it returns False while one holds the lock.
        """
        with self._shading_lock:
            self._shading_closed = True
            if self._shading is not None:
                self._shading.close()
        self._mark_closed()
        if not self._replacement_lock.acquire(blocking=blocking):
            return False
        self._replacement_lock.release()
        return True

    def _mark_closed(self) -> None:
        with self._admission:
            self._closed = True
            self.busy.close(timeout=0)

    def _readmit(self) -> None:
        """Admit reads after a replacement drained them, unless the slide closed."""
        with self._admission:
            if not self._closed:
                self.busy.reopen()

    def _pyramid_identity(self):
        path = self.cache_path
        try:
            if path is not None and path.suffix.lower() == ".nd2svs":
                st = path.stat()
                return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns
        except OSError:
            pass
        return None

    @staticmethod
    def _close_root(root):
        store = getattr(root, "store", None)
        store_path = getattr(store, "path", None)
        if store_path is not None:
            render._RAW_CHUNK_CACHE.purge(store_path)
        close = getattr(root, "close", None)
        if close is not None:
            try:
                close(delay=0)
            except TypeError:
                close()

    def _open_pyramid(self, path, manifest):
        if manifest.get("kind") == "overview":
            from .direct import open_nd2_backed

            return open_nd2_backed(self.source_path, path)
        from .convert import open_store

        return open_store(path)

    def _adopt_pyramid(self, root, attrs, manifest):
        with self.lock, self._inspect_lock:
            self.root, self.attrs, self.manifest = root, attrs, dict(manifest)
            self.generation = str(manifest["generation"])
            self.histograms = None
            self._cache_usage_ready = False
            self._cache_usage = None
            self._cache_identity = self._pyramid_identity()
            if hasattr(self._lut_settings_store, "refresh_attrs"):
                self._lut_settings_store.refresh_attrs(attrs)

    def replace_pyramid(self, replacement):
        """Validate, drain readers, atomically publish, then reopen the new pixels."""
        with self._replacement_lock:
            if self._closed:
                raise ValueError("The slide was closed before pyramid publication")
            probe, _ = self._open_pyramid(replacement.staging, replacement.manifest)
            self._close_root(probe)
            if not self.busy.close(timeout=30):
                self._readmit()
                raise ValueError("A slide read/export is still running; retry the replacement")
            admitted = False
            try:
                self._close_root(self.root)
                try:
                    replacement.publish()
                except BaseException:
                    # A denied rename (including another Windows reader) keeps
                    # the original file intact. Reopen it before admitting reads.
                    root, attrs = self._open_pyramid(replacement.final, replacement.prior_manifest)
                    self._adopt_pyramid(root, attrs, replacement.prior_manifest)
                    admitted = True
                    raise
                try:
                    root, attrs = self._open_pyramid(replacement.final, replacement.manifest)
                except Exception as exc:
                    self.manifest = dict(replacement.manifest)
                    self.generation = str(replacement.manifest["generation"])
                    self._cache_identity = None  # Status retries activation of the saved cache.
                    raise RuntimeError(
                        "The new pyramid was saved but could not be opened; reopen the slide"
                    ) from exc
                self._adopt_pyramid(root, attrs, replacement.manifest)
                admitted = True
            finally:
                # If the OS denies reopening after a successful publish, the
                # validated cache remains on disk. Keep reads barred until reopen.
                if admitted:
                    self._readmit()

    def refresh_pyramid(self):
        """Observe another window's atomic replacement without resetting sidecars."""
        # PlateSource owns native frames and refreshes its thumbs.zarr cache.
        # Thumbnail/focus writes legitimately change this container's identity;
        # they are not replacement stitched pyramids under store.ome.zarr.
        if self.plate is not None or self.attrs.get("nd2wsi", {}).get("plate"):
            return
        identity = self._pyramid_identity()
        if identity is None or identity == self._cache_identity or self._closed:
            return
        from .cache import read_manifest

        with self._replacement_lock:
            identity = self._pyramid_identity()
            if identity is None or identity == self._cache_identity or self._closed:
                return
            manifest = read_manifest(self.cache_path, recover=False)
            if not manifest or not manifest.get("generation"):
                raise ValueError("The replaced pyramid has no complete manifest")
            root, attrs = self._open_pyramid(self.cache_path, manifest)
            if not self.busy.close(timeout=30):
                self._close_root(root)
                self._readmit()
                raise ValueError("Waiting for slide reads before opening the replaced pyramid")
            try:
                self._close_root(self.root)
                self._adopt_pyramid(root, attrs, manifest)
            finally:
                self._readmit()

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


def store_generation(store_path: str | Path) -> str:
    """A token that changes whenever this store's pixels could have.

    A managed cache carries its manifest's generation uuid; anything else
    falls back to the metadata file's mtime. Tile URLs carry it, which is
    what lets the browser cache tiles at all: a rebuilt cache mints new
    URLs instead of reviving stale renders.
    """
    from .cache import manifest_container, read_manifest

    store_path = Path(store_path)
    container = manifest_container(store_path)
    if container is not None:
        m = read_manifest(container)
        if m and m.get("generation"):
            return str(m["generation"])
    probe = store_path / ".zattrs" if store_path.is_dir() else store_path
    try:
        return format(probe.stat().st_mtime_ns, "x")
    except OSError:
        return ""


class SlideRegistry:
    """The set of slides this server has open, keyed by a stable short id."""

    def __init__(self, max_render_mpx: float = 400.0, *, window_session=None):
        self.slides: dict[str, ViewerState] = {}  # insertion-ordered
        # States that left the registry with an export still running; it
        # counts for close and update until it finishes.
        self._retired: list[ViewerState] = []
        # States that left the registry and whose roots have not closed yet,
        # mostly while a read finishes; shutdown and relaunch wait for them.
        self._closing: list[ViewerState] = []
        # close_all_for_relaunch's release: (done, errors), once it started.
        self._relaunch_release: tuple[threading.Event, list[Exception]] | None = None
        self.max_render_mpx = max_render_mpx
        self._lock = threading.Lock()
        self._closing_done = threading.Condition(self._lock)
        self.window_session = window_session
        self.astromap_downloads = AstromapDownloads(
            getattr(window_session, "exports_root", None)
        )
        self.annotation_workspace = None
        if window_session is not None:
            from .annotation_workspace import AnnotationWorkspace

            self.annotation_workspace = AnnotationWorkspace(window_session)
        self._annotation_claim_lock = threading.Lock()
        self._agent_ui_lock = threading.Lock()
        self.on_remove = None

    def _apply_window_policy(self, st: ViewerState) -> None:
        if self.agent_window:
            private = getattr(self.window_session, "exports_root", None)
            st.correction_write_allowed = bool(
                private and st.cache_path and st.cache_path.is_relative_to(private.resolve())
            )
            if st.correction_write_allowed and st.manifest.get("origin") != "export":
                st.managed_cache = True

    def _notify_removed(self, sid: str) -> None:
        if self.on_remove is not None:
            try:
                self.on_remove(sid)
            except Exception:
                pass

    @property
    def agent_window(self) -> bool:
        return self.window_session is not None and self.window_session.role == "agent"

    def annotation_path(
        self,
        path: Path,
        attrs: dict,
        *,
        site: int | None = None,
        frame: tuple[int, int, int] | None = None,
    ) -> Path | None:
        readonly_errnos = (errno.EACCES, errno.EPERM, errno.EROFS)
        sidecar_fn = _resolve_server_hook(
            "annotations_sidecar", annotations_sidecar, _ORIG_ANNOTATIONS_SIDECAR
        )
        plate_sidecar_fn = _resolve_server_hook(
            "plate_annotations_sidecar",
            plate_annotations_sidecar,
            _ORIG_PLATE_ANNOTATIONS_SIDECAR,
        )
        confocal_sidecar_fn = _resolve_server_hook(
            "confocal_annotations_sidecar",
            confocal_annotations_sidecar,
            _ORIG_CONFOCAL_ANNOTATIONS_SIDECAR,
        )
        if frame is not None:
            from .annotation_workspace import _atomic_write, _read, annotation_lock

            canonical, legacy = confocal_sidecar_fn(path, attrs, frame)
            usable_legacy: Path | None = None
            raw: bytes | None = None
            if legacy is not None:
                try:
                    raw = _read(legacy)
                    if raw is not None:
                        _sidecar_items(json.loads(raw.decode("utf-8")), require_items=True)
                        usable_legacy = legacy
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    usable_legacy = None
                    raw = None
            if self.agent_window:
                return self.annotation_workspace.snapshot(canonical, source=usable_legacy)
            if canonical.exists():
                return canonical
            if usable_legacy is None:
                probe = canonical.parent
                while not probe.exists() and probe != probe.parent:
                    probe = probe.parent
                return (
                    canonical
                    if (probe.is_dir() and os.access(probe, os.W_OK | os.X_OK))
                    else None
                )
            assert raw is not None
            try:
                with annotation_lock(canonical):
                    # Recheck under the writer lock: another window may already
                    # have saved this plane. Never replace it with shared marks.
                    if not canonical.exists():
                        _atomic_write(canonical, raw)
            except OSError as exc:
                if exc.errno in readonly_errnos:
                    return None
                raise
            return canonical
        from .ometiff import is_ometiff
        from .svs import is_svs

        if site is None and not is_svs(path) and is_ometiff(path):
            legacy_ome: list[Path] = []
            canonical = sidecar_fn(path, attrs, migrate=False, legacy_paths=legacy_ome)
            if self.agent_window:
                return self.annotation_workspace.snapshot(
                    canonical, source=legacy_ome[0] if legacy_ome else None
                )
            if canonical.exists() or not legacy_ome:
                probe = canonical.parent
                while not probe.exists() and probe != probe.parent:
                    probe = probe.parent
                return (
                    canonical
                    if (
                        canonical.is_file()
                        or (probe.is_dir() and os.access(probe, os.W_OK | os.X_OK))
                    )
                    else None
                )
        if self.annotation_workspace is None:
            try:
                with self._annotation_claim_lock:
                    return (
                        sidecar_fn(path, attrs)
                        if site is None
                        else plate_sidecar_fn(path, attrs, site)
                    )
            except OSError as exc:
                if exc.errno in readonly_errnos:
                    fallback = (
                        sidecar_fn(path, attrs, migrate=False)
                        if site is None
                        else plate_sidecar_fn(path, attrs, site, create_directory=False)
                    )
                    return fallback if fallback.is_file() else None
                raise
        from .annotation_workspace import annotation_lock

        legacy_list: list[Path] = []
        canonical = (
            sidecar_fn(path, attrs, migrate=False, legacy_paths=legacy_list)
            if site is None
            else plate_sidecar_fn(path, attrs, site, create_directory=False)
        )
        if self.agent_window:
            return self.annotation_workspace.snapshot(
                canonical, source=legacy_list[0] if legacy_list else None
            )
        # Legacy migration is a write too; new user-window servers serialize it.
        try:
            with self._annotation_claim_lock, annotation_lock(canonical):
                return (
                    sidecar_fn(path, attrs)
                    if site is None
                    else plate_sidecar_fn(path, attrs, site)
                )
        except OSError as exc:
            if exc.errno in readonly_errnos:
                return canonical if canonical.is_file() else None
            raise

    @staticmethod
    def sid_for(store_path: Path, *, series: int | None = None) -> str:
        raw = str(store_path.resolve())
        if series is not None and series > 0:
            raw += f"#series={int(series)}"
        return hashlib.sha1(raw.encode()).hexdigest()[:8]

    def lut_settings_for(self, st: ViewerState):
        from .lut_settings import LutSettingsStore

        with st.lock:
            if st._lut_settings_store is None:
                st._lut_settings_store = LutSettingsStore(
                    st.source_path, st.attrs, self.window_session
                )
            return st._lut_settings_store

    def add_store(
        self,
        store_path: str | Path,
        *,
        trash_path: str | Path | None = None,
        source_path: str | Path | None = None,
    ) -> str:
        from .cache import (
            CACHE_SUFFIX,
            SINGLE_FILE_SUFFIX,
            CacheLock,
            is_managed_cache_path,
            manifest_container,
            quarantine,
            read_manifest,
        )
        from .convert import CACHE_DIR_NAME, is_nd2wsi_store, open_store
        from .raster import is_raster
        from .sdpc import is_sdpc
        from .svs import is_svs

        gen_fn = _resolve_server_hook(
            "store_generation", store_generation, _ORIG_STORE_GENERATION
        )
        viewer_state_cls = _resolve_server_hook(
            "ViewerState", ViewerState, _ORIG_VIEWER_STATE
        )

        requested_path = Path(store_path).expanduser()
        if requested_path.suffix.lower() == SINGLE_FILE_SUFFIX and requested_path.is_symlink():
            raise ValueError("open the actual .nd2svs cache file, not a symbolic link")
        store_path = requested_path.resolve()
        registered_source = (
            Path(source_path).expanduser().resolve() if source_path is not None else None
        )
        if is_raster(store_path):
            return self.add_direct(store_path)
        if store_path.suffix.lower() in (".nd2", ".czi", ".lif") and store_path.is_file():
            from .plate import is_plate_file

            if is_plate_file(store_path):
                return self.add_plate(store_path)
            raise ValueError(
                f"{store_path.name} is not a time series of sites; open it "
                "through open_path so its pyramid store is built first"
            )
        explicit_trash = trash_path is not None
        if trash_path is not None:
            trash_path = Path(trash_path).resolve()
        elif (
            store_path.suffix.lower() == SINGLE_FILE_SUFFIX
            and store_path.is_file()
            and is_managed_cache_path(store_path)
        ):
            trash_path = store_path
        elif store_path.parent.name.endswith(CACHE_SUFFIX):
            trash_path = store_path.parent
        elif store_path.parent.name == CACHE_DIR_NAME and is_nd2wsi_store(store_path):
            # a store inside a pyramids/ folder is a cache this app built
            # (0.5 through 0.7 layouts) and stays trashable; a store the
            # user opened at any other explicit path never is
            trash_path = store_path
        if is_sdpc(store_path):
            # SDPC is direct-only. A decoder/layout error must not enter the
            # ND2 conversion path or create/quarantine a research cache.
            return self.add_direct(store_path)
        if is_svs(store_path):  # an SVS itself serves with no store
            slide_path = store_path
            try:
                return self.add_direct(slide_path)
            except NotImplementedError:
                if self.agent_window:
                    raise PermissionError(
                        "Agent windows cannot build a shared cache; open an existing cache instead"
                    )
                from .convert import ensure_cache as _ensure_cache

                # the same verified route open_path takes: a valid legacy
                # pyramids/ store is reused, a stale one is never served
                store_path = Path(_ensure_cache(slide_path)).resolve()
                trash_path = store_path
                registered_source = registered_source or slide_path
        from .ometiff import is_ometiff, is_ometiff_stack

        if is_ometiff(store_path) and store_path.is_file():
            if is_ometiff_stack(store_path, series=0):
                return self.add_stack_scene(store_path, series=0)
            return self.add_direct(store_path, series=0)
        manifest: dict[str, Any] = {}
        container_path = manifest_container(store_path)
        if container_path is not None:
            manifest = read_manifest(container_path, recover=not self.agent_window) or {}
            if not explicit_trash and manifest.get("origin") == "export":
                trash_path = None
            if store_path.suffix.lower() == SINGLE_FILE_SUFFIX and not manifest:
                raise ValueError("single-file cache has no complete manifest")
            if store_path.suffix.lower() == SINGLE_FILE_SUFFIX:
                expected_formats = (
                    {"nd2wsi-plate/2"}
                    if manifest.get("kind") == "plate"
                    else {"nd2wsi-cache/4", "nd2wsi-cache/5"}
                )
                if manifest.get("format") not in expected_formats or manifest.get("kind") not in (
                    "full",
                    "overview",
                    "plate",
                ):
                    raise ValueError("unsupported single-file cache format; update the viewer")
            registered_source = registered_source or _manifest_source_path(
                container_path, manifest
            )
            if manifest.get("kind") == "plate":
                return self._add_plate_cache(store_path, manifest)
            if manifest.get("kind") == "overview":
                return self._add_overview(
                    store_path, manifest, source_path=registered_source
                )
        sid = self.sid_for(store_path)
        gen = gen_fn(store_path)
        with self._lock:
            initial_st = self.slides.get(sid)
            if (
                initial_st is not None
                and initial_st.generation == gen
                and (registered_source is None or initial_st.source_path == registered_source)
            ):
                return sid

        root, attrs = open_store(store_path)
        stale = None
        try:
            if (
                registered_source is not None
                and registered_source.is_file()
                and manifest.get("kind") in (None, "full")
            ):
                from .cache import cache_container, fingerprints_match, quick_fingerprint
                from .convert import (
                    SourceUnreadable,
                    _store_matches_source_semantics,
                    ensure_cache,
                )
                from .reader import PlaneSelection

                raw_sel = (manifest.get("selection") if manifest else None) or (
                    (attrs.get("nd2wsi") or {}).get("selection") or {}
                )
                try:
                    sel = PlaneSelection(
                        t=int(raw_sel.get("t", 0) or 0),
                        p=int(raw_sel.get("p", 0) or 0),
                        z=raw_sel.get("z", "mid"),
                    )
                except (TypeError, ValueError):
                    sel = PlaneSelection()
                unreadable_same_fp = False
                try:
                    matches = _store_matches_source_semantics(
                        registered_source, sel, attrs, raise_unreadable=True
                    )
                except SourceUnreadable as exc:
                    recorded = manifest.get("source") if manifest else None
                    changed = not (
                        isinstance(recorded, dict)
                        and fingerprints_match(recorded, quick_fingerprint(registered_source))
                    )
                    if changed and manifest:
                        # the file beside this self-contained cache is not the one it was
                        # built from (still copying, damaged, replaced): serve the cache,
                        # never set it aside on the strength of an unreadable source
                        meta = attrs.setdefault("nd2wsi", {})
                        meta["notes"] = list(meta.get("notes") or []) + [
                            f"{registered_source.name} could not be read ({exc}); "
                            "showing the cache as it was built"
                        ]
                        matches = True
                    else:
                        matches = False
                        unreadable_same_fp = bool(manifest and not changed)
                if not matches:
                    ViewerState._close_root(root)
                    root = None
                    if (
                        not self.agent_window
                        and container_path is not None
                        and container_path == cache_container(registered_source, sel)
                    ):
                        tile_opt = int((attrs.get("nd2wsi") or {}).get("tile") or 0) or None
                        try:
                            store_path = ensure_cache(
                                registered_source, selection=sel, tile=tile_opt, kind="full"
                            )
                        except (ValueError, NotImplementedError, RuntimeError) as rebuild_exc:
                            if unreadable_same_fp:
                                with CacheLock(container_path):
                                    if container_path.exists():
                                        set_aside = quarantine(container_path)
                                        raise ValueError(
                                            f"{rebuild_exc} (corrupt cache set aside as {set_aside.name})"
                                        ) from rebuild_exc
                            raise
                        manifest = read_manifest(container_path, recover=True) or {}
                        gen = gen_fn(store_path)
                        sid = self.sid_for(store_path)
                        root, attrs = open_store(store_path)
                    elif sel != PlaneSelection():
                        raise ValueError(
                            f"cache {store_path.name} is outdated; rebuild with: "
                            f"nd2wsi convert --overwrite --position {sel.p} --t {sel.t} --z {sel.z} "
                            f"{shlex.quote(registered_source.name)} {shlex.quote(store_path.name)}"
                        )
                    else:
                        raise ValueError(
                            f"cache {store_path.name} is outdated; open {registered_source.name} to rebuild"
                        )
            annotations = self.annotation_path(store_path, attrs)
            with self._lock:
                current = self.slides.get(sid)
                if current is not None and (
                    current is not initial_st
                    or (
                        current.generation == gen
                        and (
                            registered_source is None
                            or current.source_path == registered_source
                        )
                    )
                ):
                    ViewerState._close_root(root)
                    root = None
                    return sid
                stale = current
                st = viewer_state_cls(
                    root,
                    attrs,
                    max_render_mpx=self.max_render_mpx,
                    annotations_path=annotations,
                    generation=gen,
                    trash_path=Path(trash_path) if trash_path is not None else None,
                    source_path=registered_source,
                    store_path=store_path,
                    container_path=container_path,
                    manifest=manifest,
                    managed_cache=bool(
                        container_path is not None
                        and store_path.suffix.lower() == SINGLE_FILE_SUFFIX
                        and is_managed_cache_path(container_path, manifest)
                    ),
                )
                root = None
                self._apply_window_policy(st)
                self.slides[sid] = st
                self._retire(stale, closing=True)
        except BaseException:
            if root is not None:
                ViewerState._close_root(root)
            raise
        self._close_state(stale)
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
        from .cache import (
            CACHE_SUFFIX,
            fingerprints_match,
            is_managed_cache_path,
            manifest_container,
            quick_fingerprint,
        )
        from .convert import open_store
        from .direct import open_nd2_backed

        gen_fn = _resolve_server_hook(
            "store_generation", store_generation, _ORIG_STORE_GENERATION
        )
        viewer_state_cls = _resolve_server_hook(
            "ViewerState", ViewerState, _ORIG_VIEWER_STATE
        )

        sid = self.sid_for(store_path)
        gen = gen_fn(store_path)
        container = manifest_container(store_path) or store_path.parent
        stale = None
        with self._lock:
            src_info = manifest.get("source", {})
            # None when the manifest's source is missing or outside the
            # slide's folder; then no path from inside the cache is used.
            source = source_path or _manifest_source_path(container, manifest)
            source_ok = False
            try:
                source_ok = (
                    source is not None
                    and source.is_file()
                    and fingerprints_match(src_info, quick_fingerprint(source))
                )
            except OSError:
                source_ok = False
            st = self.slides.get(sid)
            expected = gen + ("" if source_ok else "-degraded")
            if st is not None and st.generation == expected and st.source_path == source:
                return sid
            if st is not None:  # keep the old state usable if opening fails
                stale = st
            root = attrs = None
            reason = f"{src_info.get('name', 'source')} is missing"
            if source is not None and source.is_file() and not source_ok:
                reason = f"{source.name} has changed since this cache was built"
            if source_ok:
                try:
                    root, attrs = open_nd2_backed(source, store_path)
                except NotImplementedError as e:
                    reason = str(e)
            degraded = root is None
            try:
                if degraded:
                    root, attrs = open_store(store_path)
                    meta = attrs["nd2wsi"]
                    meta["kind"] = "overview-degraded"
                    for lv in meta["levels"]:
                        ds = int(lv["downsample"])
                        if ds < 2:
                            raise ValueError(f"invalid overview downsample {ds!r}")
                        lv["downsample"] = ds // 2
                    # the base level is now the half-resolution overview, so a
                    # pixel is twice as large — otherwise every scalebar and
                    # measurement would read half its true length
                    if meta.get("pixel_size_um"):
                        meta["pixel_size_um"] = [v * 2 for v in meta["pixel_size_um"]]
                    meta["notes"] = meta.get("notes", []) + [
                        f"{reason}; showing the stored overview at half resolution"
                    ]
                annotations = None if degraded else self.annotation_path(store_path, attrs)
                is_managed_ov = is_managed_cache_path(
                    container, manifest
                ) or container.name.endswith(CACHE_SUFFIX)
                st = viewer_state_cls(
                    root,
                    attrs,
                    max_render_mpx=self.max_render_mpx,
                    # annotations live in level-0 pixels; a degraded view's base
                    # is level 1, so editing here would silently corrupt them
                    annotations_path=annotations,
                    # the two modes map levels differently, so tiles cached as
                    # immutable in one must never be revived in the other
                    generation=gen + ("-degraded" if degraded else ""),
                    trash_path=container if is_managed_ov else None,
                    source_path=source,
                    store_path=store_path,
                    container_path=container,
                    manifest=manifest,
                    managed_cache=is_managed_ov,
                )
            except BaseException:
                ViewerState._close_root(root)
                raise
            self._apply_window_policy(st)
            self.slides[sid] = st
            self._retire(stale, closing=True)
        self._close_state(stale)
        return sid

    def add_direct(self, slide_path: str | Path, *, series: int = 0) -> str:
        """Serve a slide directly without a pixel cache; isolate Agent annotations."""
        from .cache import fingerprints_match, quick_fingerprint
        from .ometiff import is_ometiff
        from .raster import is_raster
        from .sdpc import is_sdpc
        from .svs import is_svs

        viewer_state_cls = _resolve_server_hook(
            "ViewerState", ViewerState, _ORIG_VIEWER_STATE
        )

        slide_path = Path(slide_path).resolve()
        series = int(series)
        use_ometiff = not is_svs(slide_path) and is_ometiff(slide_path)
        if is_raster(slide_path):
            from .raster import open_direct as _open_fn
        elif is_sdpc(slide_path):
            from .sdpc import open_direct as _open_fn
        elif use_ometiff:
            from .ometiff import open_direct as _ome_open_direct

            def _open_fn(p: Path):
                return _ome_open_direct(p, series=series)
        else:
            from .direct import open_direct as _open_fn
        sid = self.sid_for(slide_path, series=series if series > 0 else None)
        stale = None
        with self._lock:
            fingerprint = quick_fingerprint(slide_path)
            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            if series > 0:
                gen += f"-s{series}"
            st = self.slides.get(sid)
            if st is not None and st.generation == gen:
                return sid

            # Fingerprint before and after opening so the registered auxiliary
            # images can never come from a replacement path while the tiled
            # root still holds the previous inode.
            root = attrs = None
            for _ in range(2):
                root, attrs = _open_fn(slide_path)
                # Never registered, so nothing reads it: close now rather than
                # after _Root's delay, which kept a Windows sharing lock open
                # while the retry opened the file again.
                try:
                    current = quick_fingerprint(slide_path)
                except Exception:
                    ViewerState._close_root(root)
                    raise
                if fingerprints_match(fingerprint, current):
                    fingerprint = current
                    break
                ViewerState._close_root(root)
                root = attrs = None
                fingerprint = current
            if root is None or attrs is None:
                raise RuntimeError(f"{slide_path.name} changed while it was opening")

            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            if series > 0:
                gen += f"-s{series}"
            stale = self.slides.get(sid)
            try:
                annotations = self.annotation_path(slide_path, attrs)
                st = viewer_state_cls(
                    root,
                    attrs,
                    max_render_mpx=self.max_render_mpx,
                    annotations_path=annotations,
                    generation=gen,
                    source_path=slide_path,
                    manifest={"source": fingerprint},
                )
            except BaseException:
                ViewerState._close_root(root)
                raise
            self._apply_window_policy(st)
            self.slides[sid] = st
            self._retire(stale, closing=True)
        self._close_state(stale)
        return sid

    def add_stack_scene(self, slide_path: str | Path, *, series: int = 0) -> str:
        """Serve an interactive OME-TIFF/ImageJ stack or targeted LIF/CZI stack scene."""
        from .cache import fingerprints_match, quick_fingerprint
        from .direct_common import _Root
        from .ometiff import ContainerSceneStackSource, OmeTiffStackSource, is_ometiff

        viewer_state_cls = _resolve_server_hook(
            "ViewerState", ViewerState, _ORIG_VIEWER_STATE
        )

        slide_path = Path(slide_path).resolve()
        series = int(series)
        sid = self.sid_for(slide_path, series=series if series > 0 else None)
        stale = None
        with self._lock:
            fingerprint = quick_fingerprint(slide_path)
            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            if series > 0:
                gen += f"-s{series}"
            st = self.slides.get(sid)
            if st is not None and st.generation == gen and st.plate is not None:
                return sid

            source = None
            for _ in range(2):
                candidate = (
                    OmeTiffStackSource(slide_path, series=series)
                    if is_ometiff(slide_path)
                    else ContainerSceneStackSource(slide_path, series=series)
                )
                try:
                    current = quick_fingerprint(slide_path)
                except BaseException:
                    candidate.close()
                    raise
                if fingerprints_match(fingerprint, current):
                    source = candidate
                    fingerprint = current
                    break
                candidate.close()
                fingerprint = current
            if source is None:
                raise RuntimeError(f"{slide_path.name} changed while it was opening")
            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            if series > 0:
                gen += f"-s{series}"
            try:
                root = _Root(source.root_for(0, 0, source.z_home), closer=source.close)
                annotations = self.annotation_path(
                    slide_path,
                    source.attrs,
                    site=0,
                    frame=(0, 0, source.z_home)
                    if source.attrs["nd2wsi"]["plate"].get("annotationScope") == "plane"
                    else None,
                )
                stale = self.slides.get(sid)
                st = viewer_state_cls(
                    root,
                    source.attrs,
                    max_render_mpx=self.max_render_mpx,
                    annotations_path=annotations,
                    generation=gen,
                    source_path=slide_path,
                    manifest={"source": fingerprint},
                    plate=source,
                )
            except BaseException:
                source.close()
                raise
            self._apply_window_policy(st)
            self.slides[sid] = st
            self._retire(stale, closing=True)
        self._close_state(stale)
        return sid

    def _add_plate_cache(self, cache_path: Path, manifest: dict) -> str:
        from .cache import fingerprints_match, quick_fingerprint

        source = _manifest_source_path(cache_path, manifest)
        fmt = (
            source.suffix[1:].upper()
            if source is not None and source.suffix.lower() in (".czi", ".lif")
            else "ND2"
        )
        if (
            source is None
            or source.suffix.lower() not in (".nd2", ".czi", ".lif")
            or not source.is_file()
        ):
            raise ValueError(
                f"plate cache needs its original {fmt} source; restore the source first"
            )
        if not fingerprints_match(manifest.get("source", {}), quick_fingerprint(source)):
            raise ValueError(
                f"plate cache source has changed; open the original {fmt} to rebuild"
            )
        return self.add_plate(source, cache_path=cache_path)

    def add_plate(self, slide_path: str | Path, *, cache_path: Path | None = None) -> str:
        """Serve a time series of sites straight from the ND2, writing nothing."""
        from .cache import fingerprints_match, quick_fingerprint
        from .direct_common import _Root
        from .plate import PlateSource

        gen_fn = _resolve_server_hook(
            "store_generation", store_generation, _ORIG_STORE_GENERATION
        )
        viewer_state_cls = _resolve_server_hook(
            "ViewerState", ViewerState, _ORIG_VIEWER_STATE
        )

        slide_path = Path(slide_path).resolve()
        sid = self.sid_for(cache_path if cache_path is not None else slide_path)
        stale = None
        with self._lock:
            fingerprint = quick_fingerprint(slide_path)
            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            if cache_path is not None:
                gen += "-" + gen_fn(cache_path)
            st = self.slides.get(sid)
            if st is not None and st.generation == gen and st.plate is not None:
                return sid
            # As with direct SVS, bind the registered generation to the exact
            # source PlateSource opened. PlateSource itself checks its ND2 and
            # raw handles; this outer retry closes the remaining gap between the
            # registry fingerprint and that internally consistent open.
            source = None
            for _ in range(2):
                candidate = (
                    PlateSource(slide_path, store=False)
                    if self.agent_window
                    else PlateSource(slide_path, cache_path=cache_path)
                    if cache_path is not None
                    else PlateSource(slide_path)
                )
                try:
                    current = quick_fingerprint(slide_path)
                except BaseException:
                    candidate.close()
                    raise
                if fingerprints_match(fingerprint, current):
                    source = candidate
                    fingerprint = current
                    break
                candidate.close()
                fingerprint = current
            if source is None:
                raise RuntimeError(f"{slide_path.name} changed while it was opening")
            gen = (
                f"{int(fingerprint['mtime_ns']):x}-"
                f"{str(fingerprint['quick_sha256'])[:16]}"
            )
            if cache_path is not None and source.store is not None:
                gen += "-" + str(source.store.manifest.get("generation", ""))
            try:
                root = _Root(source.root_for(0, 0, source.z_home), closer=source.close)
                annotations = self.annotation_path(
                    slide_path,
                    source.attrs,
                    site=0,
                    frame=(0, 0, source.z_home)
                    if source.attrs["nd2wsi"]["plate"].get("annotationScope") == "plane"
                    else None,
                )
                stale = self.slides.get(sid)
                container = source.store.container if source.store is not None else None
                st = viewer_state_cls(
                    root,
                    source.attrs,
                    max_render_mpx=self.max_render_mpx,
                    annotations_path=annotations,
                    generation=gen,
                    trash_path=container,
                    source_path=slide_path,
                    container_path=container,
                    manifest=(
                        dict(source.store.manifest)
                        if source.store is not None
                        else {"source": fingerprint}
                    ),
                    plate=source,
                )
            except BaseException:
                source.close()
                raise
            self._apply_window_policy(st)
            self.slides[sid] = st
            self._retire(stale, closing=True)
        self._close_state(stale)
        return sid

    def open_path(
        self,
        path: str | Path,
        on_progress=None,
        *,
        series: int | None = None,
    ) -> str:
        """A slide file (converted on first open) or an existing store."""
        from .cache import SINGLE_FILE_SUFFIX
        from .convert import ensure_cache, existing_cache_store
        from .ometiff import inspect_ometiff_series, is_ometiff, is_ometiff_stack
        from .raster import is_raster
        from .reader import PlaneSelection
        from .sdpc import is_sdpc
        from .svs import is_svs

        inspect_series_fn = _resolve_server_hook(
            "_inspect_path_series", _inspect_path_series, _ORIG_INSPECT_PATH_SERIES
        )
        is_stack_fn = _resolve_server_hook(
            "_series_item_is_stack", _series_item_is_stack, _ORIG_SERIES_ITEM_IS_STACK
        )

        if series is not None:
            if isinstance(series, bool) or not isinstance(series, int):
                raise ValueError("series must be an integer")
            if series < 0:
                raise ValueError(f"series {series} must be >= 0")

        path = Path(path).expanduser()
        if path.suffix.lower() == SINGLE_FILE_SUFFIX:
            sid = self.add_store(path)
            self._record_opened_recent(sid, path)
            return sid
        path = path.resolve()
        if is_sdpc(path) or is_raster(path):
            sid = self.add_direct(path)
            self._record_opened_recent(sid, path)
            return sid
        if is_svs(path) and existing_cache_store(path, read_only=self.agent_window) is None:
            try:
                # the file already holds a pyramid, so serve it as it lies
                sid = self.add_direct(path)
                self._record_opened_recent(sid, path)
                return sid
            except NotImplementedError:
                pass  # untiled or off-ladder file: build a store instead
        if not is_svs(path) and is_ometiff(path):
            if not path.is_file():
                raise FileNotFoundError(f"could not find {path.name}")
            target_s = 0 if series is None else int(series)
            if series is not None:
                ome_items = inspect_ometiff_series(path)
                max_s = max(1, len(ome_items))
                if not (0 <= target_s < max_s):
                    raise ValueError(f"series {target_s} out of range (0..{max_s - 1})")
            if is_ometiff_stack(path, series=target_s):
                sid = self.add_stack_scene(path, series=target_s)
            else:
                sid = self.add_direct(path, series=target_s)
            self._record_opened_recent(sid, path)
            return sid
        if path.suffix.lower() in SLIDE_SUFFIXES:
            suffix = path.suffix.lower()
            if suffix in (".nd2", ".czi", ".lif"):
                from .plate import is_plate_file

                if series is not None and suffix in (".czi", ".lif"):
                    if not path.is_file():
                        raise FileNotFoundError(f"could not find {path.name}")
                    s_count, s_list = inspect_series_fn(path)
                    if not (0 <= series < s_count):
                        raise ValueError(f"series {series} out of range (0..{s_count - 1})")
                    if series == 0 and is_plate_file(path):
                        sid = self.add_plate(path)
                        self._record_opened_recent(sid, path)
                        return sid
                    if series < len(s_list) and is_stack_fn(s_list[series]):
                        sid = self.add_stack_scene(path, series=series)
                        self._record_opened_recent(sid, path)
                        return sid
                    sel = PlaneSelection(t=0, p=series, z="mid")
                    if self.agent_window:
                        store = existing_cache_store(path, selection=sel, read_only=True)
                        if store is None:
                            raise PermissionError(
                                "Agent windows cannot build or repair a shared cache; open an existing cache instead"
                            )
                    else:
                        store = ensure_cache(path, selection=sel, on_progress=on_progress)
                    sid = self.add_store(store, source_path=path)
                    self._record_opened_recent(sid, path)
                    return sid

                if series is not None and suffix == ".nd2":
                    if not path.is_file():
                        raise FileNotFoundError(f"could not find {path.name}")
                    s_count, _ = inspect_series_fn(path)
                    if not (0 <= series < s_count):
                        raise ValueError(f"series {series} out of range (0..{s_count - 1})")
                    if series == 0 and is_plate_file(path):
                        sid = self.add_plate(path)
                        self._record_opened_recent(sid, path)
                        return sid

                if series is None and is_plate_file(path):
                    # camera fields over time: no pyramid to build, ever
                    sid = self.add_plate(path)
                    self._record_opened_recent(sid, path)
                    return sid

            sel_opt = PlaneSelection(t=0, p=series, z="mid") if series is not None else None
            if self.agent_window:
                store = existing_cache_store(path, selection=sel_opt, read_only=True)
                if store is None:
                    raise PermissionError(
                        "Agent windows cannot build or repair a shared cache; open an existing cache instead"
                    )
            else:
                store = (
                    ensure_cache(path, selection=sel_opt, on_progress=on_progress)
                    if sel_opt is not None
                    else ensure_cache(path, on_progress=on_progress)
                )
        elif path.is_dir():  # a user-supplied *.ome.zarr store
            sid = self.add_store(path)
            self._record_opened_recent(sid, path)
            return sid
        else:
            raise ValueError(
                "not a supported slide (.nd2, .czi, .lif, .tif/.ome.tif, .svs, .sdpc), "
                f".nd2svs cache, .jpg/.png image, or pyramid store: {path.name}"
            )
        # no explicit trash_path: add_store promotes a container store to its
        # container, so trashing removes manifest and pixels together
        sid = self.add_store(store, source_path=path)
        self._record_opened_recent(sid, path)
        return sid

    def _record_opened_recent(self, sid: str, fallback_path: Path) -> None:
        auto_allowed_fn = _resolve_server_hook(
            "_auto_record_allowed", _auto_record_allowed, _ORIG_AUTO_RECORD_ALLOWED
        )
        if self.agent_window or not auto_allowed_fn():
            return
        st = self.slides.get(sid)
        target = (
            st.source_path
            if (st is not None and st.source_path is not None and st.source_path.exists())
            else fallback_path
        )
        server_mod = sys.modules.get("nd2wsi.server")
        record_fn = (
            getattr(server_mod, "_record_recent_file_safe", ui_settings.record_recent_file)
            if server_mod is not None
            else ui_settings.record_recent_file
        )
        try:
            record_fn(target, window_role="user")
        except Exception:
            pass

    def remove(self, sid: str) -> bool:
        with self._lock:
            st = self.slides.get(sid)
            # Only a real export refuses a window's close, and it refuses
            # before anything is cancelled or barred. The check also bars
            # exports in one step, so an export request that resolved this
            # slide already cannot start after it and go uncounted. Tile,
            # pixel and histogram reads never refuse: _close_state lets them
            # finish before the root closes.
            if (
                st is not None
                and self.window_session is not None
                and not st.exports.close_if_idle()
            ):
                raise ValueError(
                    "An export from this slide is still running; close it after the export finishes"
                )
            # Shading Correction is closed by _close_state, without waiting:
            # the controller's cancel() reports status, which may refresh the
            # pyramid and wait for its reads while this lock is held.
            st = self.slides.pop(sid, None)
            self._retire(st, closing=True)
        self._close_state(st)
        if st is not None:
            self._notify_removed(sid)
        return st is not None

    def close_all(self, *, immediate: bool = False) -> None:
        """Close every registered backend after the server stops.

        Without ``immediate``, source-backed roots keep _Root's short delay
        only when reads are still draining. Once ``busy`` has drained, stores,
        plate prefetchers, and OME-TIFF handles close deterministically.
        """
        with self._lock:
            states = list(self.slides.values())
            self.slides.clear()
        for st in states:
            st.close_shading()
            drained = st.busy.close(timeout=30)
            for p_attr in ("store_path", "source_path"):
                p_val = getattr(st, p_attr, None)
                if p_val is not None:
                    render._RAW_CHUNK_CACHE.purge(p_val)
            if drained:
                plate_close = getattr(getattr(st, "plate", None), "close", None)
                if callable(plate_close):
                    try:
                        plate_close(timeout=30)
                    except TypeError:
                        plate_close()
                else:
                    ViewerState._close_root(st.root)
                continue
            # Readers are still active after the drain budget; never close an
            # mmap-backed handle underneath an active reader (SIGSEGV). Hand off
            # to a background closer or delayed close instead.
            if immediate:
                with self._lock:
                    self._closing.append(st)
                self._close_state(st)
                continue
            close = getattr(st.root, "close", None)
            if close is not None:
                close()
        if immediate:
            self._wait_for_closing(timeout=30)

    def _retire(self, st: ViewerState | None, *, closing: bool = False) -> None:
        """Bar exports on a state leaving the registry; keep counting running ones.

        Called under ``self._lock`` in the same step that removes or
        replaces the state, so an export request that resolved it earlier
        cannot start afterwards, and one already streaming still counts.
        With ``closing`` the caller hands it to ``_close_state`` next, and
        it is tracked until its root is closed, so a shutdown or relaunch
        that comes first still waits for it.
        """
        if st is None:
            return
        self._retired = [old for old in self._retired if old.exports.active]
        if not st.exports.close(timeout=0):
            self._retired.append(st)
        if closing:
            self._closing.append(st)

    def _close_state(self, st: ViewerState | None) -> None:
        """Close a state that has left the registry and been tracked as closing.

        Requests that resolved it before it left may still be reading. New
        reads and exports are barred at once, and the root closes as soon as
        the last read is done, without _Root's delay: never under a reader,
        and without holding the calling request thread.
        """
        if st is None:
            return
        handed_off = False
        try:
            st.exports.close(timeout=0)
            # The slide is marked closed and its reads barred at once, so a
            # pyramid replacement or refresh still running can no longer
            # readmit them. While one holds the replacement lock it may still
            # swap the root, so the root closes once it has let go. A plate's
            # frame reads and cache builder hold no busy, and its close waits
            # for them, so a plate always closes on the closer thread.
            quiet = st.close_shading(blocking=False)
            if quiet and st.plate is None and st.busy.close(timeout=0):
                ViewerState._close_root(st.root)
                return

            def close_after_reads() -> None:
                try:
                    self._close_after_reads(st, quiet)
                finally:
                    self._untrack_closing(st)

            try:
                threading.Thread(
                    target=close_after_reads, name="nd2wsi-slide-close", daemon=True
                ).start()
                handed_off = True
            except Exception:
                # No thread to wait on: wait here rather than leave it open.
                self._close_after_reads(st, quiet)
        finally:
            if not handed_off:
                self._untrack_closing(st)

    @staticmethod
    def _close_after_reads(st: ViewerState, quiet: bool) -> None:
        if not quiet:
            st.close_shading()  # waits for the replacement or refresh
        st.busy.close(timeout=None)
        for p_attr in ("store_path", "source_path"):
            p_val = getattr(st, p_attr, None)
            if p_val is not None:
                render._RAW_CHUNK_CACHE.purge(p_val)
        if st.plate is not None:
            st.plate.close(timeout=None)
        else:
            ViewerState._close_root(st.root)

    def _untrack_closing(self, st: ViewerState) -> None:
        with self._lock:
            self._closing = [old for old in self._closing if old is not st]
            self._closing_done.notify_all()

    def _wait_for_closing(self, *, timeout: float | None) -> bool:
        """Wait for slides closed earlier to finish closing their roots."""
        with self._lock:
            return self._closing_done.wait_for(lambda: not self._closing, timeout)

    def active_export_count(self) -> int:
        """Return the number of real exports which are still running.

        Tile, pixel and histogram reads, export previews and size samples
        hold only ``busy``; they never hold back a tab close or an update.
        An export on a slide that was closed, reopened from a changed file
        or trashed while it ran counts until it finishes.
        """
        with self._lock:
            self._retired = [st for st in self._retired if st.exports.active]
            states = [*self.slides.values(), *self._retired]
        return sum(st.exports.active for st in states)

    def close_all_for_relaunch(self, timeout: float | None = None) -> None:
        """Synchronously release every source and writer before an update.

        Unlike ordinary tab closure, this path may not leave delayed native
        teardown behind: Sparkle is about to replace the application bundle
        and relaunch the process.  Removing the states first bars new requests;
        the lifecycle guards then drain work which already obtained a state.

        ``timeout`` bounds the whole wait. A read wedged in native code (a
        share that went away) never returns, and an update waiting for it
        would never install, so after ``timeout`` seconds this raises
        TimeoutError and the update backs out. Nothing closes under a reader:
        the release goes on, in the same order, on its own thread until the
        reads let go, and a later call waits for that same release instead
        of starting another.
        """
        with self._lock:
            release = self._relaunch_release
            started = release is None
            if started:
                states = list(self.slides.values())
                self.slides.clear()
                release = self._relaunch_release = (threading.Event(), [])
        done, errors = release
        if started:

            def release_all() -> None:
                try:
                    self._release_for_relaunch(states)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    done.set()

            try:
                threading.Thread(
                    target=release_all, name="nd2wsi-relaunch-release", daemon=True
                ).start()
            except Exception:
                # No thread to wait on: release here, without the bound,
                # rather than leave a source or writer open.
                release_all()
        if not done.wait(timeout):
            raise TimeoutError(f"a slide read did not finish within {timeout:g} seconds")
        if errors:
            raise errors[0]

    def _release_for_relaunch(self, states: list[ViewerState]) -> None:
        for st in states:
            st.close_shading()
            st.busy.close(timeout=None)
            for p_attr in ("store_path", "source_path"):
                p_val = getattr(st, p_attr, None)
                if p_val is not None:
                    render._RAW_CHUNK_CACHE.purge(p_val)
            if st.plate is not None:
                st.plate.close(timeout=None)
                continue
            ViewerState._close_root(st.root)
        # Slides closed just before, whose last reads are still finishing,
        # close their roots the same way once those reads end.
        self._wait_for_closing(timeout=None)

    def trash_cache(self, sid: str, on_progress=None) -> int:
        """Close the slide and delete its pyramid store. Returns bytes freed.

        Deletes only the registered store directory, never the source slide,
        and never an annotation sidecar that ended up inside it. A store is
        hundreds of thousands of small files, so the caller gets a fraction
        as the files go.
        """
        if self.window_session is not None:
            raise PermissionError(
                "Shared cache removal is unavailable in multiwindow mode; another window may still be reading it"
            )

        from .cache import (
            ANNOTATIONS_DIR,
            CACHE_SUFFIX,
            CACHES_DIR,
            MANAGED_DIR,
            SINGLE_FILE_SUFFIX,
            CacheLock,
        )
        from .convert import CACHE_DIR_NAME

        is_reparse = _resolve_reparse_check()
        same_identity = _resolve_server_hook(
            "_same_file_identity", _same_file_identity, _ORIG_SAME_FILE_IDENTITY
        )
        validate_file_cache = _resolve_server_hook(
            "_validate_file_cache_for_trash",
            _validate_file_cache_for_trash,
            _ORIG_VALIDATE_FILE_CACHE_FOR_TRASH,
        )
        rescue_fn = _resolve_server_hook(
            "rescue_annotations", rescue_annotations, _ORIG_RESCUE_ANNOTATIONS
        )
        delete_file_fn = _resolve_server_hook(
            "_delete_verified_cache_file",
            _delete_verified_cache_file,
            _ORIG_DELETE_VERIFIED_CACHE_FILE,
        )
        delete_tree_fn = _resolve_server_hook(
            "_delete_verified_cache_tree",
            _delete_verified_cache_tree,
            _ORIG_DELETE_VERIFIED_CACHE_TREE,
        )

        store_guard_fd = None
        with self._lock:
            st = self.slides.get(sid)
            if st is None:
                raise KeyError(sid)
            if st.trash_path is None:
                raise ValueError("this slide has no cache managed by this app")
            store = Path(st.trash_path)
            if store.name.endswith(".ome.zarr") and store.parent.name.endswith(
                CACHE_SUFFIX
            ):
                store = store.parent  # the container owns manifest and store
            try:
                store_stat = store.stat(follow_symlinks=False)
            except OSError:
                store_stat = None
            real_directory = bool(
                store_stat is not None
                and stat.S_ISDIR(store_stat.st_mode)
                and not is_reparse(store_stat, fd=None)
            )
            valid_container = real_directory and store.name.endswith(CACHE_SUFFIX)
            valid_store = real_directory and store.name.endswith(".ome.zarr")
            single_file = bool(
                store_stat is not None
                and stat.S_ISREG(store_stat.st_mode)
                and store_stat.st_nlink == 1
                and not is_reparse(store_stat, fd=None)
                and store.suffix.lower() == SINGLE_FILE_SUFFIX
            )
            if not (valid_container or valid_store or single_file):
                raise ValueError(f"refusing to delete {store}: not a managed cache")
            if st.busy.active:
                raise ValueError(
                    "an export from this slide is still running — try again "
                    "when it finishes"
                )
            plate_store = (
                st.plate.store
                if st.plate is not None and st.plate.store is not None
                else None
            )
            plate_source = st.plate if plate_store is not None else None
            local_plate_writer = bool(plate_store is not None and plate_store.writable)
            guard_flags = os.O_RDONLY
            if not single_file:
                guard_flags |= getattr(os, "O_DIRECTORY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                guard_flags |= os.O_NOFOLLOW
            if os.name == "nt":
                from .windows_fs import open_guard

                store_guard_fd = open_guard(store, allow_rename=True)
            else:
                store_guard_fd = os.open(store, guard_flags)
            guarded_stat = os.fstat(store_guard_fd)
            if not same_identity(guarded_stat, store_stat) or is_reparse(
                guarded_stat, fd=store_guard_fd
            ):
                os.close(store_guard_fd)
                store_guard_fd = None
                raise ValueError(
                    f"refusing to delete {store}: cache root changed during validation"
                )
            store_stat = guarded_stat

        # Plate mutation uses the same order as open/create: the session writer
        # first, then the short build lock. A locally writable store already
        # owns the first lock; a read-only store must prove that its other
        # process has gone away before deletion can begin.
        writer = None
        lock = None
        ready_to_delete = False
        try:
            if plate_store is not None and not local_plate_writer:
                from .plate import PlateWriterLock

                writer = PlateWriterLock(store)
                try:
                    writer.acquire(timeout=0.0)
                except (TimeoutError, OSError):
                    raise ValueError(
                        "another viewer is writing this plate cache — close it "
                        "before deleting"
                    ) from None

            lock = CacheLock(store)
            try:
                lock.acquire(timeout=10)
            except TimeoutError:
                raise ValueError(
                    "this slide's cache is being rebuilt right now — try again "
                    "when the build finishes"
                ) from None

            # Rescue user work before closing anything. Any read, durable-copy,
            # or verification failure aborts deletion, leaving the live state
            # registered and usable.
            if st.annotations_path is not None:
                annotation_home = st.annotations_path.parent
            elif store.name.endswith(CACHE_SUFFIX) and store.parent.name == CACHES_DIR:
                annotation_home = store.parent.parent / ANNOTATIONS_DIR
            elif store.parent.name == CACHE_DIR_NAME:
                annotation_home = store.parent.parent / MANAGED_DIR / ANNOTATIONS_DIR
            else:
                annotation_home = store.parent / MANAGED_DIR / ANNOTATIONS_DIR
            if single_file:
                validate_file_cache(store, st.manifest)
            else:
                annotation_home.mkdir(parents=True, exist_ok=True)
                rescue_fn(store, annotation_home)

            # Lock acquisition can wait. Revalidate while holding the registry
            # lock, bar late requests, and drain before the state is removed.
            # Keeping the entry until close succeeds avoids stranding an open
            # plate (and its writer lease) outside the registry on a timeout.
            with self._lock:
                if self.slides.get(sid) is not st:
                    raise KeyError(sid)
                if st.busy.active:
                    raise ValueError(
                        "an export from this slide is still running — try again "
                        "when it finishes"
                    )
                if local_plate_writer and not plate_store.writable:
                    raise ValueError(
                        "this viewer lost the plate cache writer — reopen the "
                        "slide before deleting"
                    )
                # A refresh after another window's rebuild must not swap the
                # root or readmit reads between the drain and the slide being
                # marked closed, so both happen under its lock. The correction
                # controller closes after it: its cancel() refreshes while
                # holding the controller's own lock.
                with st._replacement_lock:
                    if not st.busy.close(timeout=30):
                        # No resource was closed or renamed. Restore admission so a
                        # timed-out delete cannot leave a listed but unusable slide.
                        st.busy.reopen()
                        raise ValueError(
                            "an export from this slide did not finish in time — "
                            "try again when it finishes"
                        )
                    if plate_source is None:
                        st._mark_closed()

                try:
                    if plate_source is not None:
                        # Drain plate readers before marking closed so a timeout
                        # rollback leaves the slide completely healthy.
                        detached_writer = plate_source.close_for_trash()
                        st._mark_closed()
                        st.close_shading()
                        if local_plate_writer:
                            writer = detached_writer
                            if writer is None or not writer.acquired:
                                raise RuntimeError(
                                    "could not retain the plate cache writer"
                                )
                        elif detached_writer is not None:
                            detached_writer.release()
                            raise RuntimeError(
                                "read-only plate unexpectedly held a writer"
                            )
                    else:
                        st.close_shading()
                        ViewerState._close_root(st.root)
                except BaseException:
                    # PlateSource restores itself when a pre-teardown drain times
                    # out. Once any backend close became irreversible, unregister
                    # the state so a later open constructs fresh handles instead
                    # of returning a listed-but-dead same-generation source.
                    backend_closed = plate_source is None or bool(
                        getattr(plate_source, "_closed", False)
                    )
                    if backend_closed:
                        self.slides.pop(sid, None)
                        self._retire(st)
                    else:
                        st.busy.reopen()
                    raise

                # Closing is irreversible, so unregister and atomically remove
                # the live pathname before releasing the registry lock. A
                # concurrent current-build opener can then see either the old
                # registered state or an absent cache, never install a new state
                # in the pop-to-rename gap and have it deleted underneath it.
                self.slides.pop(sid, None)
                self._retire(st)
                # Rename while the registry plus both filesystem locks are
                # held. Current opens cannot bind this soon-to-be-deleted inode.
                doomed = store.with_name(
                    f".{store.name}.trashing-{os.getpid()}-{time.time_ns():x}"
                )
                store.rename(doomed)
                renamed_stat = doomed.stat(follow_symlinks=False)
                if (
                    not (
                        stat.S_ISREG(renamed_stat.st_mode)
                        if single_file
                        else stat.S_ISDIR(renamed_stat.st_mode)
                    )
                    or is_reparse(renamed_stat, fd=None)
                    or not same_identity(renamed_stat, store_stat)
                    or not same_identity(os.fstat(store_guard_fd), renamed_stat)
                ):
                    raise OSError(
                        "cache root changed before deletion; retained safely at "
                        f"{doomed}"
                    )
            # Close/rename is the commit boundary. Scan the renamed container a
            # second time before releasing the writer/build locks so an old
            # path-based producer that wrote between the first rescue and the
            # rename cannot lose its last annotation copy.
            if single_file:
                validate_file_cache(doomed, st.manifest)
            else:
                rescue_fn(doomed, annotation_home)
            ready_to_delete = True
        finally:
            if lock is not None:
                lock.release()
            if writer is not None:
                writer.release()
            if not ready_to_delete and store_guard_fd is not None:
                os.close(store_guard_fd)
                store_guard_fd = None
        guarded_fd, store_guard_fd = store_guard_fd, None
        if single_file:
            return delete_file_fn(doomed, store_stat, guarded_fd, on_progress)
        return delete_tree_fn(
            doomed,
            store_stat,
            on_progress,
            root_fd=guarded_fd,
        )

    def default_sid(self) -> str | None:
        with self._lock:
            return next(iter(self.slides), None)

    def get(self, sid: str | None) -> ViewerState | None:
        if sid is not None:
            # Writers still serialise under _lock and one dict lookup is
            # atomic. A caller may get a state that is about to be replaced
            # or removed, as it always could once the lock was released. The
            # busy lease guards slide reads only, not info, LUT or annotation
            # requests. Skipping the lock keeps open slides serving while
            # another slide opens.
            return self.slides.get(sid) if sid else None
        with self._lock:  # iterating can race a resize, so keep the lock
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


_ORIG_IS_REPARSE = is_windows_name_surrogate_reparse
_ORIG_VIEWER_STATE = ViewerState
_ORIG_STORE_GENERATION = store_generation
_ORIG_ANNOTATIONS_SIDECAR = annotations_sidecar
_ORIG_PLATE_ANNOTATIONS_SIDECAR = plate_annotations_sidecar
_ORIG_CONFOCAL_ANNOTATIONS_SIDECAR = confocal_annotations_sidecar
_ORIG_SAME_FILE_IDENTITY = _same_file_identity
_ORIG_VALIDATE_FILE_CACHE_FOR_TRASH = _validate_file_cache_for_trash
_ORIG_RESCUE_ANNOTATIONS = rescue_annotations
_ORIG_DELETE_VERIFIED_CACHE_FILE = _delete_verified_cache_file
_ORIG_DELETE_VERIFIED_CACHE_TREE = _delete_verified_cache_tree
_ORIG_INSPECT_PATH_SERIES = _inspect_path_series
_ORIG_SERIES_ITEM_IS_STACK = _series_item_is_stack
_ORIG_AUTO_RECORD_ALLOWED = _auto_record_allowed
