"""Local viewer server (stdlib only -- no web framework dependency).

Multiple slides are served at once, browser-tab style:

GET  /                          tab shell (one tab per open slide)
GET  /static/<path>             js / css / vendored OpenSeadragon (global)
GET  /api/slides                open slides: [{sid, name, width, height}]
POST /api/open                  {"path": "/…/slide.nd2|.svs|.sdpc|store.ome.zarr"}
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
GET  /s/<sid>/api/roi?level=&x=&y=&w=&h=&format=nd2|tiff|png|jpg|svg&c=&win=&scalebar=1
GET/POST /s/<sid>/api/annotations   sidecar annotations
"""

from __future__ import annotations

import concurrent.futures
import errno
import functools
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import shlex
import socketserver
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

from . import render, session_log
from .annotation_sidecars import (
    SLIDE_SUFFIXES,
    _manifest_source_path,
    annotations_sidecar,
    confocal_annotations_sidecar,
    plate_annotations_sidecar,
)
from .astromap_export import (
    AstromapConflict,
    AstromapDownloads,
    AstromapError,
    prepare_astromap,
)
from .cache_removal import (
    _delete_verified_cache_file,
    _delete_verified_cache_tree,
    _same_file_identity,
    _validate_file_cache_for_trash,
    rescue_annotations,
)
from .direct_common import LeaseClosed, _Lifecycle
from .region_export import JOB_RE, ExportRequestError, prepare_export

STATIC_DIR = Path(__file__).parent / "static"
TILE_RE = re.compile(r"^/api/tile/(\d+)/(\d+)/(\d+)\.(jpg|jpeg|png)$")
PLATE_FRAME_RE = re.compile(r"^/api/(?:plate/)?frame/(\d+)/(\d+)/(\d+)\.(jpg|jpeg|png)$")
ASSOCIATED_RE = re.compile(
    r"^/api/associated/(thumbnail|label|macro|associated-(?:[1-9]|1[0-6]))\.jpg$"
)
SLIDE_RE = re.compile(r"^/s/([0-9a-f]{8})(/.*)?$")
# A rendered ROI of this many pixels or more counts as an export for close
# and update. The viewer's scale-strip previews and size samples are at most
# 512 px a side; streamed ND2/TIFF exports always count.
EXPORT_COUNT_MIN_PIXELS = 4_000_000

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


_HEX6_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


@functools.lru_cache(maxsize=64)
def _accepts_kwarg(fn: Any, kwarg: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == kwarg:
            return True
    return False


def _attrs_with_colors(attrs: dict[str, Any], col: str | None) -> dict[str, Any]:
    """Return a detached attrs dict with valid 6-hex custom colors applied."""
    if not col or (attrs.get("nd2wsi") or {}).get("rgb"):
        return attrs
    omero = attrs.get("omero") or {}
    orig_channels = list(omero.get("channels") or [])
    if not orig_channels:
        return attrs
    tokens = col.split(",")
    changed = False
    new_channels = []
    for idx, ch in enumerate(orig_channels):
        tok = tokens[idx].strip().lstrip("#") if idx < len(tokens) else ""
        if _HEX6_RE.fullmatch(tok):
            upper = tok.upper()
            if upper != ch.get("color"):
                changed = True
                new_channels.append({**ch, "color": upper})
                continue
        new_channels.append(ch)
    if not changed:
        return attrs
    return {**attrs, "omero": {**omero, "channels": new_channels}}


def _render_tile_compat(
    root: Any,
    attrs: dict[str, Any],
    level: int,
    tx: int,
    ty: int,
    channels: list[int],
    fmt: str,
    win: str | None,
    col: str | None,
) -> bytes:
    if col:
        if _accepts_kwarg(render.render_tile, "col"):
            try:
                return render.render_tile(
                    root, attrs, level, tx, ty, channels, fmt, win, col=col
                )
            except TypeError:
                pass
        attrs = _attrs_with_colors(attrs, col)
    return render.render_tile(root, attrs, level, tx, ty, channels, fmt, win)


def _render_plate_frame_compat(
    plate: Any,
    t: int,
    p: int,
    z: int,
    k: int,
    channels: list[int],
    fmt: str,
    win: str | None,
    col: str | None,
) -> bytes:
    if col:
        if _accepts_kwarg(plate.render_frame, "col"):
            try:
                return plate.render_frame(t, p, z, k, channels, fmt, win, col=col)
            except TypeError:
                pass
        if not getattr(plate, "rgb", False):
            region = plate.reduced(t, p, z, k)
            patched = _attrs_with_colors(plate.attrs, col)
            windows, colors = render.display_params(patched)
            windows, gammas = render.parse_windows(win, windows)
            img = render.composite(region, channels, windows, colors, plate.rgb, gammas)
            return render.encode_image(img, fmt)
    return plate.render_frame(t, p, z, k, channels, fmt, win)


# ---- UI settings & Recent files delegation + standalone fallback ---------

_UI_FALLBACK_LOCK = threading.RLock()
_PYTEST_FALLBACK_DIR: Path | None = None


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


def _fallback_ui_settings_file() -> Path:
    global _PYTEST_FALLBACK_DIR
    for env_key in ("ND2WSI_UI_SETTINGS_PATH",):
        raw = os.environ.get(env_key)
        if raw:
            return Path(raw).expanduser().resolve()
    for env_key in ("ND2WSI_UI_SETTINGS_ROOT", "ND2WSI_APP_SUPPORT_DIR"):
        raw = os.environ.get(env_key)
        if raw:
            return Path(raw).expanduser().resolve() / "ui-settings.json"
    lut_override = os.environ.get("ND2WSI_LUT_SETTINGS_ROOT")
    if lut_override:
        return Path(lut_override).expanduser().resolve().parent / "ui-settings.json"
    if "PYTEST_CURRENT_TEST" in os.environ and not _is_isolated_settings_env():
        if _PYTEST_FALLBACK_DIR is None:
            _PYTEST_FALLBACK_DIR = Path(tempfile.mkdtemp(prefix="nd2wsi-ui-test-"))
        return _PYTEST_FALLBACK_DIR / "ui-settings.json"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "nd2wsi-viewer" / "ui-settings.json"


def _fallback_read_doc() -> dict[str, Any]:
    path = _fallback_ui_settings_file()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return {}


def _fallback_write_doc(doc: dict[str, Any]) -> None:
    from .window_sessions import _atomic_json

    path = _fallback_ui_settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, doc)


def _normalize_ui_settings_dict(raw: dict[str, Any]) -> dict[str, Any]:
    theme = raw.get("theme", "auto")
    if theme not in ("auto", "light", "dark"):
        theme = "auto"
    panels_raw = raw.get("panels")
    panels = dict(panels_raw) if isinstance(panels_raw, dict) else {}
    expanded = raw.get("shellToolsExpanded")
    if expanded is not None and not isinstance(expanded, bool):
        expanded = None
    return {
        "theme": theme,
        "panels": panels,
        "shellToolsExpanded": expanded,
    }


def _load_ui_settings_safe() -> dict[str, Any]:
    try:
        from . import ui_settings as _ui_mod

        if "PYTEST_CURRENT_TEST" not in os.environ or _is_isolated_settings_env():
            return dict(_ui_mod.load_ui_settings())
    except ImportError:
        pass
    with _UI_FALLBACK_LOCK:
        return _normalize_ui_settings_dict(_fallback_read_doc())


def _save_ui_settings_safe(patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ValueError("UI settings payload must be a JSON object")
    clean_patch = {k: v for k, v in patch.items() if k != "ok"}
    if "theme" in clean_patch and clean_patch["theme"] not in ("auto", "light", "dark"):
        raise ValueError("theme must be 'auto', 'light', or 'dark'")
    if "panels" in clean_patch and not isinstance(clean_patch["panels"], dict):
        raise ValueError("panels must be an object")
    if "shellToolsExpanded" in clean_patch and clean_patch["shellToolsExpanded"] is not None:
        if not isinstance(clean_patch["shellToolsExpanded"], bool):
            raise ValueError("shellToolsExpanded must be a boolean or null")
    try:
        from . import ui_settings as _ui_mod

        if "PYTEST_CURRENT_TEST" not in os.environ or _is_isolated_settings_env():
            return dict(_ui_mod.save_ui_settings(clean_patch))
    except ImportError:
        pass
    with _UI_FALLBACK_LOCK:
        doc = _fallback_read_doc()
        current = _normalize_ui_settings_dict(doc)
        if "theme" in clean_patch:
            current["theme"] = clean_patch["theme"]
        if "panels" in clean_patch:
            merged_panels = dict(current["panels"])
            merged_panels.update(clean_patch["panels"])
            current["panels"] = merged_panels
        if "shellToolsExpanded" in clean_patch:
            current["shellToolsExpanded"] = clean_patch["shellToolsExpanded"]
        doc.update(current)
        _fallback_write_doc(doc)
        return current


def _is_unmounted_macos_volume(p: Path) -> bool:
    parts = p.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        vol_root = Path("/") / "Volumes" / parts[2]
        return not vol_root.exists()
    return False


def _list_recent_files_safe(limit: int = 12) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 50))
    try:
        from . import ui_settings as _ui_mod

        if "PYTEST_CURRENT_TEST" not in os.environ or _is_isolated_settings_env():
            return list(_ui_mod.list_recent_files(limit=limit))
    except ImportError:
        pass
    with _UI_FALLBACK_LOCK:
        doc = _fallback_read_doc()
        raw_items = doc.get("recentFiles")
        if not isinstance(raw_items, list):
            return []
        kept: list[dict[str, Any]] = []
        pruned = False
        for entry in raw_items:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                pruned = True
                continue
            p = Path(entry["path"])
            exists = p.exists()
            if not exists and _is_unmounted_macos_volume(p):
                kept.append(
                    {
                        "path": entry["path"],
                        "name": str(entry.get("name") or p.name),
                        "openedAt": float(entry.get("openedAt") or 0.0),
                        "available": False,
                    }
                )
            elif exists:
                kept.append(
                    {
                        "path": entry["path"],
                        "name": str(entry.get("name") or p.name),
                        "openedAt": float(entry.get("openedAt") or 0.0),
                        "available": True,
                    }
                )
            else:
                pruned = True
        if pruned:
            doc["recentFiles"] = kept[:20]
            try:
                _fallback_write_doc(doc)
            except OSError:
                pass
        return kept[:limit]


def _record_recent_file_safe(
    path: str | Path, *, name: str | None = None, window_role: str = "user"
) -> list[dict[str, Any]]:
    if window_role == "agent":
        return []
    try:
        from . import ui_settings as _ui_mod

        if "PYTEST_CURRENT_TEST" not in os.environ or _is_isolated_settings_env():
            return list(
                _ui_mod.record_recent_file(path, name=name, window_role=window_role)
            )
    except ImportError:
        pass
    resolved = str(Path(path).expanduser().resolve())
    disp_name = str(name).strip() if name else Path(resolved).name
    now = time.time()
    with _UI_FALLBACK_LOCK:
        doc = _fallback_read_doc()
        raw_items = doc.get("recentFiles")
        existing = [
            e
            for e in (raw_items if isinstance(raw_items, list) else [])
            if isinstance(e, dict) and e.get("path") != resolved
        ]
        new_entry = {
            "path": resolved,
            "name": disp_name,
            "openedAt": now,
            "available": Path(resolved).exists(),
        }
        updated = [new_entry, *existing][:20]
        doc["recentFiles"] = updated
        _fallback_write_doc(doc)
    return _list_recent_files_safe()


# ---- Multi-Series / Multi-Scene Container Metadata Inspection ------------

_SERIES_META_CACHE: OrderedDict[tuple[str, int, int], tuple[int, list[dict[str, Any]]]] = OrderedDict()
_SERIES_META_LOCK = threading.Lock()
_SERIES_META_MAX = 64


def _inspect_path_series(source_path: Path) -> tuple[int, list[dict[str, Any]]]:
    """Return (seriesCount, seriesList) for .lif, .czi, multipoint .nd2, or multi-series .ome.tif.

    ``seriesList`` is populated when ``seriesCount > 1`` on non-grid heterogeneous
    ``.lif``, multi-scene ``.czi``, non-plate multipoint ``.nd2``, or multi-series
    ``.ome.tif``, and ``[]`` otherwise.
    """
    from .ometiff import inspect_ometiff_series, is_ometiff
    from .plate import PLATE_MAX_FRAME_PX
    from .platform_io import BINARY

    try:
        resolved = source_path.resolve()
        st_info = resolved.stat()
    except OSError:
        return 1, []

    cache_key = (
        str(resolved),
        int(st_info.st_mtime_ns),
        int(st_info.st_size),
        int(PLATE_MAX_FRAME_PX),
    )
    with _SERIES_META_LOCK:
        cached = _SERIES_META_CACHE.get(cache_key)
        if cached is not None:
            _SERIES_META_CACHE.move_to_end(cache_key)
            return cached[0], [dict(item) for item in cached[1]]

    suffix = resolved.suffix.lower()
    result: tuple[int, list[dict[str, Any]]] = (1, [])
    try:
        if suffix == ".lif":
            from .lif import _list_lif_series, _read_lif_header_and_blocks
            from .plate import _lif_plate_series

            fd = os.open(str(resolved), os.O_RDONLY | BINARY)
            try:
                root_xml, blocks = _read_lif_header_and_blocks(fd, st_info.st_size)
            finally:
                os.close(fd)
            all_series = _list_lif_series(root_xml, blocks)
            if len(all_series) > 1 and len(_lif_plate_series(all_series)) <= 1:
                items: list[dict[str, Any]] = []
                for idx, ser in enumerate(all_series):
                    if ser.tile_positions_px and ser.n_m > 1:
                        w = max(tx + ser.width for tx, _ty in ser.tile_positions_px)
                        h = max(ty + ser.height for _tx, ty in ser.tile_positions_px)
                    else:
                        w, h = ser.width, ser.height
                    items.append(
                        {
                            "index": idx,
                            "name": str(ser.name or f"Series {idx + 1}"),
                            "width": int(w),
                            "height": int(h),
                            "channels": int(len(ser.channels)),
                            "zCount": int(ser.n_z),
                            "tCount": int(ser.n_t),
                        }
                    )
                result = (len(items), items)
        elif suffix == ".czi":
            from .czi import (
                _level0_entries,
                _read_czi_header_and_index,
                _resolve_czi_entries_spec,
            )

            fd = os.open(str(resolved), os.O_RDONLY | BINARY)
            try:
                entries, xml_root = _read_czi_header_and_index(fd, st_info.st_size)
            finally:
                os.close(fd)
            lvl0 = _level0_entries(entries)
            s_values = sorted({e.s for e in lvl0})
            if len(s_values) > 1:
                scene_names: dict[int, str] = {}
                if xml_root is not None:
                    for sc_i, sc in enumerate(xml_root.findall(".//Scenes/Scene")):
                        sc_name = (sc.get("Name") or sc.findtext("Name") or "").strip()
                        if not sc_name:
                            continue
                        raw_idx = sc.get("Index")
                        try:
                            key_idx = int(raw_idx) if raw_idx is not None else sc_i
                        except ValueError:
                            key_idx = sc_i
                        scene_names[key_idx] = sc_name
                items = []
                for idx, s_val in enumerate(s_values):
                    s_entries = [e for e in lvl0 if e.s == s_val]
                    t_vals = sorted({e.t for e in s_entries})
                    ref = [e for e in s_entries if e.t == t_vals[0]] if t_vals else s_entries
                    w = max(e.x_start + e.x_size for e in ref) - min(e.x_start for e in ref)
                    h = max(e.y_start + e.y_size for e in ref) - min(e.y_start for e in ref)
                    _dt, is_rgb, _ = _resolve_czi_entries_spec(ref)
                    n_c = 3 if is_rgb else len({e.c for e in ref})
                    n_z = max(1, len({e.z for e in ref}))
                    n_t = max(1, len(t_vals))
                    sname = scene_names.get(s_val) or scene_names.get(idx) or f"Scene {idx + 1}"
                    items.append(
                        {
                            "index": idx,
                            "name": sname,
                            "width": int(w),
                            "height": int(h),
                            "channels": int(n_c),
                            "zCount": int(n_z),
                            "tCount": int(n_t),
                        }
                    )
                result = (len(items), items)
        elif suffix == ".nd2":
            import warnings

            import nd2

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with nd2.ND2File(str(resolved)) as f:
                    sizes = dict(f.sizes)
                    n_p = int(sizes.get("P", 1))
                    w = int(sizes.get("X", 0))
                    h = int(sizes.get("Y", 0))
                    area = w * h
                    is_plate = (
                        not getattr(f, "is_legacy", False)
                        and (getattr(f.attributes, "compressionType", None) or "none") == "none"
                        and 0 < area <= PLATE_MAX_FRAME_PX
                        and (int(sizes.get("T", 1)) * n_p * int(sizes.get("Z", 1))) > 1
                    )
                    if n_p > 1 and not is_plate and w > 0 and h > 0:
                        pos_names: list[str] = []
                        try:
                            for loop in getattr(f, "experiment", None) or []:
                                if getattr(loop, "type", "") == "XYPosLoop":
                                    pts = (
                                        getattr(getattr(loop, "parameters", None), "points", None)
                                        or []
                                    )
                                    pos_names = [
                                        str(getattr(pt, "name", "") or "").strip() for pt in pts
                                    ]
                                    break
                        except Exception:
                            pos_names = []
                        n_s = int(sizes.get("S", 1))
                        n_c_raw = int(sizes.get("C", 1))
                        n_c = n_s if (n_s > 1 and n_c_raw == 1) else n_c_raw
                        n_z = int(sizes.get("Z", 1))
                        n_t = int(sizes.get("T", 1))
                        items = [
                            {
                                "index": idx,
                                "name": (
                                    pos_names[idx]
                                    if idx < len(pos_names) and pos_names[idx]
                                    else f"Position {idx + 1}"
                                ),
                                "width": w,
                                "height": h,
                                "channels": n_c,
                                "zCount": n_z,
                                "tCount": n_t,
                            }
                            for idx in range(n_p)
                        ]
                        result = (len(items), items)
        elif is_ometiff(resolved):
            items = inspect_ometiff_series(resolved)
            if len(items) > 1:
                result = (len(items), items)
    except Exception:
        result = (1, [])

    with _SERIES_META_LOCK:
        _SERIES_META_CACHE[cache_key] = result
        while len(_SERIES_META_CACHE) > _SERIES_META_MAX:
            _SERIES_META_CACHE.popitem(last=False)
    return result[0], [dict(item) for item in result[1]]


def _inspect_slide_series(st: ViewerState) -> tuple[int, int, list[dict[str, Any]]]:
    """Return (seriesCount, seriesIndex, seriesList) for an open ViewerState."""
    meta = st.attrs.get("nd2wsi") or {}
    sel = meta.get("selection") or {}
    try:
        cur_p = max(0, int(sel.get("p", 0) or 0))
    except (TypeError, ValueError):
        cur_p = 0
    if st.plate is not None and getattr(st.plate, "P", 1) > 1:
        return 1, 0, []
    if st.source_path is None or not st.source_path.is_file():
        return 1, 0, []
    count, series_list = _inspect_path_series(st.source_path)
    if count <= 1:
        return 1, 0, []
    clamped_p = cur_p if 0 <= cur_p < count else 0
    return count, clamped_p, series_list


def _series_item_is_stack(item: dict[str, Any]) -> bool:
    from .ometiff import STACK_MAX_DIM, STACK_MAX_FRAME_PX, STACK_MIN_DIM

    w = int(item.get("width", 0))
    h = int(item.get("height", 0))
    n_z = int(item.get("zCount", 1))
    n_t = int(item.get("tCount", 1))
    if n_z <= 1 and n_t <= 1:
        return False
    return (
        STACK_MIN_DIM <= w <= STACK_MAX_DIM
        and STACK_MIN_DIM <= h <= STACK_MAX_DIM
        and w * h <= STACK_MAX_FRAME_PX
    )


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
                    raise RuntimeError("The new pyramid was saved but could not be opened; reopen the slide") from exc
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


def _allocated_bytes(st: os.stat_result, path: Path | None = None) -> int | None:
    blocks = getattr(st, "st_blocks", None)
    if blocks is not None:
        return int(blocks) * 512
    if os.name == "nt" and path is not None:
        from .windows_fs import allocated_bytes

        try:
            return allocated_bytes(path)
        except OSError:
            pass
    return None


def _add_allocation(left: int | None, right: int | None) -> int | None:
    return left + right if left is not None and right is not None else None


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
            "allocated_bytes": _allocated_bytes(root_stat, path),
        }

    logical = 0
    allocated = _allocated_bytes(root_stat, path)
    for walk_root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            try:
                entry_path = Path(walk_root) / name
                allocated = _add_allocation(allocated, _allocated_bytes(entry_path.lstat(), entry_path))
            except OSError:
                continue
        for name in files:
            try:
                st = (Path(walk_root) / name).lstat()
            except OSError:
                continue
            logical += int(st.st_size)
            allocated = _add_allocation(allocated, _allocated_bytes(st, Path(walk_root) / name))
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


def _associated_source_path(st: ViewerState) -> Path | None:
    """A verified registered source, never a request-supplied path."""
    source = st.source_path
    if source is None or source.suffix.lower() not in {".svs", ".sdpc"} or not source.is_file():
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
        names = _associated_backend(source).associated_image_names(source)
        return names if _associated_source_path(st) == source else []
    except Exception:
        # An auxiliary image must never make the primary slide inspector fail.
        return []


def _associated_backend(source: Path):
    """Resolve the auxiliary-image reader for an already verified source."""
    if source.suffix.lower() == ".sdpc":
        from . import sdpc

        return sdpc
    from . import svs

    return svs


def _frame_args(
    st: ViewerState, q: dict, *, require_p: bool = False
) -> tuple[int, int, int] | None:
    """The (t, p, z) a plate request names, or None for an ordinary slide.

    Missing values fall back to t 0, site 0 and the home z plane. User-facing
    operations on a multi-position acquisition can set ``require_p`` so an
    absent site can never silently become P0. Raises ``ValueError`` when a
    value is not an integer or lies out of range.
    """
    plate = st.plate
    if plate is None:
        return None

    raw_p = (q.get("p") or [None])[0]
    if require_p and plate.P > 1 and (raw_p is None or raw_p == ""):
        raise ValueError("p is required; choose a plate site first")

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
    if str(level["path"]) == "0" and hasattr(root, "raw_level0"):
        root = {"0": root.raw_level0}
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
        try:
            py, px = float(pixel_size[0]), float(pixel_size[1])
            if math.isfinite(px) and math.isfinite(py) and px > 0 and py > 0:
                um = [xi * px, yi * py]
        except (TypeError, ValueError, IndexError):
            um = None
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
    """Reveal one registered path in Finder or Explorer without a shell."""
    import subprocess
    import sys

    platform = platform or sys.platform
    if platform not in ("darwin", "win32"):
        raise RuntimeError("Reveal is available on macOS and Windows")
    if not path.exists():
        raise FileNotFoundError(path)
    run = runner or subprocess.run
    if platform == "win32":
        # Explorer may return a nonzero status when handing off to an existing
        # window. Use the full SystemRoot path to prevent CWD binary hijacking.
        import ntpath

        explorer = ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows", "explorer.exe")
        run([explorer, "/select,", str(path.resolve())], check=False, timeout=10)
    else:
        run(["/usr/bin/open", "-R", "--", str(path.resolve())], check=True, timeout=10)


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
        self, path: Path, attrs: dict, *, site: int | None = None,
        frame: tuple[int, int, int] | None = None,
    ) -> Path | None:
        readonly_errnos = (errno.EACCES, errno.EPERM, errno.EROFS)
        if frame is not None:
            from .annotation_workspace import _atomic_write, _read, annotation_lock

            canonical, legacy = confocal_annotations_sidecar(path, attrs, frame)
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
                return canonical if (probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)) else None
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
            canonical = annotations_sidecar(path, attrs, migrate=False, legacy_paths=legacy_ome)
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
                    if (canonical.is_file() or (probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)))
                    else None
                )
        if self.annotation_workspace is None:
            try:
                with self._annotation_claim_lock:
                    return (annotations_sidecar(path, attrs) if site is None
                            else plate_annotations_sidecar(path, attrs, site))
            except OSError as exc:
                if exc.errno in readonly_errnos:
                    fallback = (
                        annotations_sidecar(path, attrs, migrate=False)
                        if site is None
                        else plate_annotations_sidecar(path, attrs, site, create_directory=False)
                    )
                    return fallback if fallback.is_file() else None
                raise
        from .annotation_workspace import annotation_lock
        legacy_list: list[Path] = []
        canonical = (
            annotations_sidecar(path, attrs, migrate=False, legacy_paths=legacy_list)
            if site is None
            else plate_annotations_sidecar(path, attrs, site, create_directory=False)
        )
        if self.agent_window:
            return self.annotation_workspace.snapshot(canonical, source=legacy_list[0] if legacy_list else None)
        # Legacy migration is a write too; new user-window servers serialize it.
        try:
            with self._annotation_claim_lock, annotation_lock(canonical):
                return (annotations_sidecar(path, attrs) if site is None
                        else plate_annotations_sidecar(path, attrs, site))
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
                st._lut_settings_store = LutSettingsStore(st.source_path, st.attrs, self.window_session)
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
        elif store_path.parent.name == CACHE_DIR_NAME and is_nd2wsi_store(
            store_path
        ):
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
                    raise PermissionError("Agent windows cannot build a shared cache; open an existing cache instead")
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
                    {"nd2wsi-plate/2"} if manifest.get("kind") == "plate"
                    else {"nd2wsi-cache/4", "nd2wsi-cache/5"}
                )
                if (manifest.get("format") not in expected_formats
                        or manifest.get("kind") not in ("full", "overview", "plate")):
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
        gen = store_generation(store_path)
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
                from .convert import SourceUnreadable, _store_matches_source_semantics, ensure_cache
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
                        gen = store_generation(store_path)
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
                        and (registered_source is None or current.source_path == registered_source)
                    )
                ):
                    ViewerState._close_root(root)
                    root = None
                    return sid
                stale = current
                st = ViewerState(
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

        sid = self.sid_for(store_path)
        gen = store_generation(store_path)
        container = manifest_container(store_path) or store_path.parent
        stale = None
        with self._lock:
            src_info = manifest.get("source", {})
            # None when the manifest's source is missing or outside the
            # slide's folder; then no path from inside the cache is used.
            source = source_path or _manifest_source_path(container, manifest)
            source_ok = False
            try:
                source_ok = source is not None and source.is_file() and fingerprints_match(
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
                is_managed_ov = (
                    is_managed_cache_path(container, manifest)
                    or container.name.endswith(CACHE_SUFFIX)
                )
                st = ViewerState(
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
                st = ViewerState(
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
                root = _Root(
                    source.root_for(0, 0, source.z_home), closer=source.close
                )
                annotations = self.annotation_path(
                    slide_path,
                    source.attrs,
                    site=0,
                    frame=(0, 0, source.z_home)
                    if source.attrs["nd2wsi"]["plate"].get("annotationScope") == "plane"
                    else None,
                )
                stale = self.slides.get(sid)
                st = ViewerState(
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
        if source is None or source.suffix.lower() not in (".nd2", ".czi", ".lif") or not source.is_file():
            raise ValueError(f"plate cache needs its original {fmt} source; restore the source first")
        if not fingerprints_match(manifest.get("source", {}), quick_fingerprint(source)):
            raise ValueError(f"plate cache source has changed; open the original {fmt} to rebuild")
        return self.add_plate(source, cache_path=cache_path)

    def add_plate(self, slide_path: str | Path, *, cache_path: Path | None = None) -> str:
        """Serve a time series of sites straight from the ND2, writing nothing."""
        from .cache import fingerprints_match, quick_fingerprint
        from .direct_common import _Root
        from .plate import PlateSource

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
                gen += "-" + store_generation(cache_path)
            st = self.slides.get(sid)
            if st is not None and st.generation == gen and st.plate is not None:
                return sid
            # As with direct SVS, bind the registered generation to the exact
            # source PlateSource opened. PlateSource itself checks its ND2 and
            # raw handles; this outer retry closes the remaining gap between the
            # registry fingerprint and that internally consistent open.
            source = None
            for _ in range(2):
                candidate = (PlateSource(slide_path, store=False) if self.agent_window else
                    PlateSource(slide_path, cache_path=cache_path)
                    if cache_path is not None else PlateSource(slide_path))
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
                root = _Root(
                    source.root_for(0, 0, source.z_home), closer=source.close
                )
                annotations = self.annotation_path(
                    slide_path, source.attrs, site=0,
                    frame=(0, 0, source.z_home)
                    if source.attrs["nd2wsi"]["plate"].get("annotationScope") == "plane" else None,
                )
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
                    manifest=(dict(source.store.manifest) if source.store is not None
                              else {"source": fingerprint}),
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
                    s_count, s_list = _inspect_path_series(path)
                    if not (0 <= series < s_count):
                        raise ValueError(f"series {series} out of range (0..{s_count - 1})")
                    if series == 0 and is_plate_file(path):
                        sid = self.add_plate(path)
                        self._record_opened_recent(sid, path)
                        return sid
                    if series < len(s_list) and _series_item_is_stack(s_list[series]):
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
                    s_count, _ = _inspect_path_series(path)
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
                    raise PermissionError("Agent windows cannot build or repair a shared cache; open an existing cache instead")
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
        if self.agent_window or not _auto_record_allowed():
            return
        st = self.slides.get(sid)
        target = (
            st.source_path
            if (st is not None and st.source_path is not None and st.source_path.exists())
            else fallback_path
        )
        try:
            _record_recent_file_safe(target, window_role="user")
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
            if (st is not None and self.window_session is not None
                    and not st.exports.close_if_idle()):
                raise ValueError("An export from this slide is still running; close it after the export finishes")
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
            raise PermissionError("Shared cache removal is unavailable in multiwindow mode; another window may still be reading it")
        import os

        from .cache import (
            ANNOTATIONS_DIR,
            CACHE_SUFFIX,
            CACHES_DIR,
            MANAGED_DIR,
            SINGLE_FILE_SUFFIX,
            CacheLock,
        )
        from .convert import CACHE_DIR_NAME

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
                store_stat is not None and stat.S_ISDIR(store_stat.st_mode)
                and not (getattr(store_stat, "st_file_attributes", 0) & 0x400)
            )
            valid_container = real_directory and store.name.endswith(CACHE_SUFFIX)
            valid_store = real_directory and store.name.endswith(".ome.zarr")
            single_file = bool(
                store_stat is not None and stat.S_ISREG(store_stat.st_mode)
                and store_stat.st_nlink == 1
                and not (getattr(store_stat, "st_file_attributes", 0) & 0x400)
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
            if not _same_file_identity(guarded_stat, store_stat):
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
                _validate_file_cache_for_trash(store, st.manifest)
            else:
                annotation_home.mkdir(parents=True, exist_ok=True)
                rescue_annotations(store, annotation_home)

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
                    not (stat.S_ISREG(renamed_stat.st_mode) if single_file
                         else stat.S_ISDIR(renamed_stat.st_mode))
                    or not _same_file_identity(renamed_stat, store_stat)
                    or not _same_file_identity(os.fstat(store_guard_fd), renamed_stat)
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
                _validate_file_cache_for_trash(doomed, st.manifest)
            else:
                rescue_annotations(doomed, annotation_home)
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
            return _delete_verified_cache_file(doomed, store_stat, guarded_fd, on_progress)
        return _delete_verified_cache_tree(
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


def content_disposition(filename: str) -> str:
    """An attachment header safe for any filename.

    HTTP headers travel as latin-1; a Korean or accented slide name used
    to raise UnicodeEncodeError after the export was already written. The
    ASCII fallback keeps old clients working and the RFC 5987 filename*
    carries the real name. Control characters (including CR/LF) are stripped
    to prevent response header injection.
    """
    import urllib.parse as _up

    clean = re.sub(r"[\x00-\x1f\x7f]+", "_", str(filename))
    ascii_name = (
        clean.encode("ascii", "replace")
        .decode("ascii")
        .replace('"', "_")
        .replace("\\", "_")
    )
    utf8_name = _up.quote(clean, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


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


_DISCONNECT_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "EPIPE",
        "ECONNRESET",
        "ECONNABORTED",
        "ENOTCONN",
        "EBADF",
        "ESHUTDOWN",
    )
    if hasattr(errno, name)
)
HTTP_MAX_WORKERS = 16


def _resolve_http_max_workers() -> int:
    raw = os.environ.get("ND2WSI_HTTP_MAX_WORKERS")
    if raw is not None and raw.strip() != "":
        try:
            val = int(raw)
        except ValueError:
            return HTTP_MAX_WORKERS
        if val >= 1:
            return val
    return HTTP_MAX_WORKERS


# pywebview 6's Window.evaluate_js() runs `eval(...)` inside macOS WKWebView's
# main world, so `script-src` must permit `'unsafe-eval'` while still forbidding
# `'unsafe-inline'`, remote/data/blob scripts, plugins, and cross-origin framing.
_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'; "
    "form-action 'self'"
)


def make_handler(
    registry: SlideRegistry, token: str = ""
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "nd2wsi"
        # Bound idle keep-alive sockets and stalled unauthenticated connections
        # so they cannot permanently occupy all ThreadPoolExecutor workers.
        timeout = 15.0
        # Set once a download has sent its status line; reset per request.
        _response_started = False

        def version_string(self) -> str:
            return self.server_version

        def send_error(
            self, code: int, message: str | None = None, explain: str | None = None
        ) -> None:
            # Never answer unauthenticated probes (OPTIONS/HEAD/PUT/DELETE or
            # malformed requests) with stdlib HTML or runtime version banners.
            self.close_connection = True
            if self._response_started:
                return
            if getattr(self, "command", None) == "HEAD":
                self._send(404, b"", "application/json")
            else:
                self._error(404, "not found")

        def handle_expect_100(self) -> bool:
            if getattr(self, "command", None) != "POST":
                self.close_connection = True
                self._error(404, "not found")
                return False
            if self._gate() is None:
                return False
            return super().handle_expect_100()

        def handle_one_request(self) -> None:
            if getattr(self.server, "_shutting_down", False):
                self.close_connection = True
                return
            try:
                super().handle_one_request()
            except (ConnectionError, TimeoutError):
                self.close_connection = True
            except OSError as exc:
                if exc.errno in _DISCONNECT_ERRNOS or getattr(
                    self.server, "_shutting_down", False
                ):
                    self.close_connection = True
                    return
                raise
            if getattr(self.server, "_shutting_down", False):
                self.close_connection = True

        def finish(self) -> None:
            try:
                super().finish()
            except (ConnectionError, TimeoutError):
                pass
            except OSError as exc:
                if exc.errno in _DISCONNECT_ERRNOS or getattr(
                    self.server, "_shutting_down", False
                ):
                    return
                raise

        # ---- helpers -----------------------------------------------------
        def _discard_rejected_body(self) -> None:
            """Discard at most 64 KiB / 250 ms after sending a rejection.

            Closing over unread bytes can reset a Windows TCP connection and
            discard the error response. The write side is already shut down,
            and this connection will not be reused: even bytes after an invalid
            or ambiguous body are discarded, never parsed as another request.
            """
            remaining = 65_536
            previous_timeout = self.connection.gettimeout()
            deadline = time.monotonic() + 0.25
            try:
                while remaining:
                    wait = deadline - time.monotonic()
                    if wait <= 0:
                        break
                    self.connection.settimeout(wait)
                    body = self.rfile.read1(min(remaining, 8192))
                    if not body:
                        break
                    remaining -= len(body)
            except OSError:
                pass  # a partial/slow body is dropped when this socket closes
            finally:
                self.connection.settimeout(previous_timeout)

        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str,
            extra: dict | None = None,
            cache: str = "no-store",
        ):
            rejecting_post = self.command == "POST" and code >= 300
            if rejecting_post or code >= 400:
                self.close_connection = True
            else:
                server = getattr(self, "server", None)
                active = getattr(server, "_active_sockets", None)
                max_workers = getattr(server, "max_workers", None)
                if (
                    active is not None
                    and isinstance(max_workers, int)
                    and max_workers > 2
                    and len(active) >= max_workers - 2
                ):
                    # Shed idle keep-alive sockets before the pool saturates.
                    self.close_connection = True
            # As in _stream_file: once the status line may be out, a failed
            # write cannot become an error reply inside this one's declared
            # length. _error drops the connection instead.
            self._response_started = True
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self._policy_headers(cache)
                if self.close_connection:
                    self.send_header("Connection", "close")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                if rejecting_post:
                    import socket

                    self.wfile.flush()
                    try:
                        self.connection.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    self._discard_rejected_body()
            except BaseException:
                self.close_connection = True
                raise

        def _policy_headers(self, cache: str) -> None:
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP_HEADER)

        def _stream_file(
            self,
            write_body,
            *,
            size: int,
            content_type: str,
            filename: str,
        ) -> None:
            """Send a 200 download whose body ``write_body(wfile)`` copies out.

            Once the status line is out, a failure can no longer become an
            error reply: it would land inside the declared Content-Length on
            a keep-alive connection. The connection is closed instead and the
            exception propagates, so the export's own context still sees it.
            """
            disposition = content_disposition(filename)
            self._response_started = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", disposition)
                self._policy_headers("no-store")
                self.end_headers()
                write_body(self.wfile)
            except BaseException:
                self.close_connection = True
                raise

        def _json(self, obj: Any, code: int = 200):
            try:
                body = json.dumps(obj, allow_nan=False).encode()
            except ValueError:
                body = json.dumps(
                    {"error": "internal server error: non-finite value in response"}
                ).encode()
                code = 500
            self._send(code, body, "application/json")

        def _error(self, code: int, msg: str):
            if self._response_started:
                # A download already sent its status and length; drop the
                # connection rather than write JSON into its body.
                self.close_connection = True
                return
            self._json({"error": msg}, code)

        def _body_json(self, limit: int = 10_000_000) -> Any:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError("expected one Content-Length and no Transfer-Encoding")
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= limit:
                raise ValueError("payload missing or too large")
            return json.loads(
                self.rfile.read(n),
                parse_constant=lambda c: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant {c} is not allowed")
                ),
            )

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
            Host header must name loopback (and this server's port if given),
            and any Origin present must match this server's exact origin.
            """
            if getattr(self, "request_version", "") == "HTTP/0.9":
                self.close_connection = True
                self._error(404, "not found")
                return None
            parsed = urllib.parse.urlparse(self.path)
            if parsed.scheme or parsed.netloc:
                self.close_connection = True
                self._error(404, "not found")
                return None
            path = parsed.path
            server_port = getattr(getattr(self, "server", None), "server_port", None)
            if not isinstance(server_port, int) or server_port <= 0:
                server_port = None
            get_all = getattr(self.headers, "get_all", None)
            host_headers = get_all("Host", []) if get_all is not None else []
            raw_host = (host_headers[0] if len(host_headers) == 1 else "").strip()
            host: str | None = None
            host_port: int | None = None
            if raw_host.startswith("["):  # bracketed IPv6, maybe with a port
                if "]" in raw_host:
                    bracket, rest = raw_host.split("]", 1)
                    if rest == "":
                        host = (bracket + "]").lower()
                    elif rest.startswith(":") and rest[1:].isascii() and rest[1:].isdigit():
                        host = (bracket + "]").lower()
                        host_port = int(rest[1:])
            elif ":" in raw_host:
                h, port = raw_host.rsplit(":", 1)
                if h != "" and port.isascii() and port.isdigit():
                    host = h.lower()
                    host_port = int(port)
            elif raw_host:
                host = raw_host.lower()
            if host not in ("127.0.0.1", "localhost", "[::1]") or (
                host_port is not None and server_port is not None and host_port != server_port
            ):
                self.close_connection = True
                self._error(404, "not found")
                return None
            origin_headers = get_all("Origin", []) if get_all is not None else (
                [self.headers.get("Origin")] if self.headers.get("Origin") else []
            )
            if len(origin_headers) > 1:
                self.close_connection = True
                self._error(404, "not found")
                return None
            if origin_headers:
                try:
                    parsed_origin = urllib.parse.urlparse(origin_headers[0])
                    origin_port = parsed_origin.port
                except ValueError:
                    self.close_connection = True
                    self._error(404, "not found")
                    return None
                effective_origin_port = 80 if origin_port is None else origin_port
                if (
                    parsed_origin.scheme != "http"
                    or parsed_origin.hostname not in ("127.0.0.1", "localhost", "::1")
                    or (server_port is not None and effective_origin_port != server_port)
                ):
                    self.close_connection = True
                    self._error(404, "not found")
                    return None
            if not token:
                return path
            if not path.startswith("/"):
                self.close_connection = True
                self._error(404, "not found")
                return None
            seg, sep, tail = path[1:].partition("/")
            if not hmac.compare_digest(seg, token):
                self.close_connection = True
                self._error(404, "not found")
                return None
            if not sep:
                # redirect rather than rewrite: relative asset and API URLs
                # only resolve when the document URL ends with the slash
                target = f"/{token}/"
                if parsed.query:
                    target += "?" + parsed.query
                self._send(
                    HTTPStatus.PERMANENT_REDIRECT, b"", "text/plain",
                    {"Location": target},
                )
                return None
            return "/" + tail

        def do_GET(self):  # noqa: N802 (http.server API)
            self._response_started = False
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
                if path == "/api/ui-settings" or path.endswith("/api/ui-settings"):
                    return self._ui_settings_get()
                if path == "/api/recent-files" or path.endswith("/api/recent-files"):
                    return self._recent_files_get(q)
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
                if sub == "/api/lut-settings":
                    return self._json(registry.lut_settings_for(st).read())
                if sub == "/api/inspect":
                    return self._inspect(st)
                if sub in ("/api/pixel", "/api/histogram"):
                    try:
                        with st.busy:
                            return self._pixel(st, q) if sub.endswith("pixel") else self._histogram(st, q)
                    except ValueError as e:
                        return self._error(409, str(e))
                if sub == "/api/shading/status":
                    try:
                        controller = st.shading
                    except ValueError:
                        if not st._shading_closed:
                            raise
                        # Resolved before the close, which came before any
                        # controller: answer as a closed controller does.
                        from .h_correction import closed_status

                        return self._json(closed_status(st))
                    return self._json(controller.status())
                if sub == "/api/annotations":
                    return self._annotations_get(st, q)
                associated = ASSOCIATED_RE.match(sub)
                if associated:
                    return self._associated(st, associated.group(1), q)
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
                if sub in ("/api/roi", "/api/region"):
                    return self._roi(st, q)
                if sub == "/api/astromap/download":
                    return self._astromap_download(st, q)
                return self._error(404, f"no route for {path}")
            except ConnectionError:
                # BrokenPipe on macOS, ConnectionReset/Aborted on Windows: the
                # client left, so nothing more can be sent on this socket.
                self.close_connection = True
            except Exception as e:  # pragma: no cover - defensive
                try:
                    self._error(500, f"{type(e).__name__}: {e}")
                except Exception:
                    pass

        def do_POST(self):  # noqa: N802 (http.server API)
            self._response_started = False
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
                if path == "/api/ui-settings" or path.endswith("/api/ui-settings"):
                    return self._ui_settings_post()
                if path == "/api/recent-files" or path.endswith("/api/recent-files"):
                    return self._recent_files_post()
                st, sub = self._resolve(path)
                if st is not None and sub == "/api/select-scene":
                    return self._select_scene(st)
                if st is not None and sub in ("/api/shading/start", "/api/shading/revoke", "/api/shading/cancel"):
                    try:
                        data = self._body_json(limit=1000)
                        allowed = ({"generation", "method", "experimental_confirmed"}
                                   if sub == "/api/shading/start" else {"generation"})
                        if (not isinstance(data, dict) or "generation" not in data
                                or set(data) - allowed or not isinstance(data["generation"], str)):
                            raise ValueError("expected the current slide generation")
                        if sub == "/api/shading/start":
                            if data.get("method", "bffree") not in ("h", "bffree"):
                                raise ValueError("Unknown Shading Correction method")
                            if ("experimental_confirmed" in data
                                    and not isinstance(data["experimental_confirmed"], bool)):
                                raise ValueError("Experimental confirmation must be a boolean")
                    except (ValueError, json.JSONDecodeError) as e:
                        return self._error(400, str(e))
                    try:
                        controller = st.shading
                        current = controller.status()
                        if data["generation"] != current["generation"]:
                            return self._error(409, "The pyramid changed; refresh before continuing")
                        if not st.correction_write_allowed:
                            return self._error(403, "Agent windows can replace pyramids only in their private exports folder")
                        if sub == "/api/shading/start":
                            if "method" not in data:
                                return self._json(controller.start())
                            return self._json(controller.start(data["method"],
                                experimental_confirmed=data.get("experimental_confirmed", False)))
                        action = {"/api/shading/revoke": controller.revoke,
                                  "/api/shading/cancel": controller.cancel}[sub]
                        return self._json(action())
                    except ValueError as e:
                        return self._error(409, str(e))
                if st is not None and sub == "/api/lut-settings":
                    return self._lut_settings_post(st)
                if st is not None and sub == "/api/annotations":
                    return self._annotations_post(st, q)
                if st is not None and sub == "/api/astromap":
                    return self._astromap(st)
                if st is not None and sub == "/api/reveal":
                    return self._reveal(st)
                return self._error(404, f"no POST route for {path}")
            except ConnectionError:
                self.close_connection = True
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
            path = None
            began = time.monotonic()
            try:
                data = self._body_json()
                path = data.get("path") if isinstance(data, dict) else None
                if not isinstance(path, str) or not path.strip():
                    return self._error(400, 'expected {"path": "..."}')
                has_series = isinstance(data, dict) and data.get("series") is not None
                series_arg: int | None = None
                if has_series:
                    raw_s = data["series"]
                    if isinstance(raw_s, bool) or not isinstance(raw_s, int):
                        return self._error(400, "series must be an integer")
                    series_arg = int(raw_s)
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

                if has_series:
                    sid = registry.open_path(path, on_progress=on_progress, series=series_arg)
                else:
                    sid = registry.open_path(path, on_progress=on_progress)
                if job:
                    _job_update(job, state="done", pct=100)
            except ConnectionError:
                raise
            except (
                ValueError,
                FileNotFoundError,
                OSError,
                NotImplementedError,
                RuntimeError,
            ) as e:
                _job_update(job, state="error", error=str(e))
                if path:
                    self._record_open(path, began, ok=False, error=str(e))
                return self._error(400, str(e))
            except json.JSONDecodeError as e:
                return self._error(400, f"invalid JSON: {e}")
            self._record_open(path, began, ok=True, sid=sid)
            return self._json({"sid": sid, "slides": registry.listing()})

        @staticmethod
        def _record_open(path, began, **fields):
            # The desktop app's session log; a no-op for the CLI viewer.
            session_log.event("file_open", via="shell", path=str(path),
                              format=Path(str(path)).suffix.lower().lstrip("."),
                              seconds=round(time.monotonic() - began, 2), **fields)

        def _ui_settings_get(self):
            if registry.agent_window:
                with registry._agent_ui_lock:
                    agent_ui = getattr(registry, "_agent_ui_settings", None)
                    if agent_ui is None:
                        agent_ui = dict(_load_ui_settings_safe())
                        registry._agent_ui_settings = agent_ui
                    snapshot = dict(agent_ui)
                return self._json({"ok": True, **snapshot})
            settings = _load_ui_settings_safe()
            return self._json({"ok": True, **settings})

        def _ui_settings_post(self):
            try:
                data = self._body_json(limit=65_536)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                if registry.agent_window:
                    from . import ui_settings as _ui_mod

                    clean = {k: v for k, v in data.items() if k != "ok"}
                    with registry._agent_ui_lock:
                        current = getattr(registry, "_agent_ui_settings", None)
                        if current is None:
                            current = dict(_load_ui_settings_safe())
                        doc = {
                            "theme": current.get("theme", "auto"),
                            "panels": dict(current.get("panels") or {}),
                            "shellToolsExpanded": current.get("shellToolsExpanded"),
                            "recentFiles": [],
                        }
                        updated = _ui_mod._public_ui_settings(_ui_mod._apply_ui_patch(doc, clean))
                        registry._agent_ui_settings = dict(updated)
                    return self._json({"ok": True, **updated})
                updated = _save_ui_settings_safe(data)
                return self._json({"ok": True, **updated})
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            except OSError as e:
                return self._error(500, f"Could not save UI settings: {e}")

        def _recent_files_get(self, q: dict):
            if registry.agent_window:
                return self._json({"ok": True, "items": []})
            raw_limit = (q.get("limit") or ["12"])[0]
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 12
            items = _list_recent_files_safe(limit=limit)
            return self._json({"ok": True, "items": items})

        def _recent_files_post(self):
            try:
                data = self._body_json(limit=16_384)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                raw_path = data.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    raise ValueError('expected {"path": "..."}')
                if registry.agent_window:
                    return self._json({"ok": True, "items": []})
                candidate = Path(raw_path).expanduser().resolve()
                with registry._lock:
                    slide_states = list(registry.slides.values())
                registered = {
                    p.resolve()
                    for st in slide_states
                    for p in (st.source_path, st.store_path, st.container_path)
                    if p is not None
                }
                allowed_suffixes = {
                    *SLIDE_SUFFIXES,
                    ".tif", ".tiff", ".qptiff", ".svs", ".sdpc", ".nd2svs",
                    ".jpg", ".jpeg", ".png", ".zarr",
                }
                if candidate not in registered and candidate.suffix.lower() not in allowed_suffixes:
                    raise ValueError("path is not a supported slide or registered store")
                raw_name = data.get("name")
                name = str(raw_name) if isinstance(raw_name, str) and raw_name.strip() else None
                items = _record_recent_file_safe(candidate, name=name, window_role="user")
                return self._json({"ok": True, "items": items})
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            except OSError as e:
                return self._error(500, f"Could not record recent file: {e}")

        def _select_scene(self, st: ViewerState):
            try:
                data = self._body_json(limit=10_000)
                if not isinstance(data, dict) or "series" not in data:
                    raise ValueError('expected {"series": <int>}')
                raw_series = data["series"]
                if isinstance(raw_series, bool) or not isinstance(raw_series, int):
                    raise ValueError("series must be an integer")
                target_series = int(raw_series)
                if st.source_path is None or not st.source_path.is_file():
                    raise ValueError("original slide source is unavailable for scene selection")
                series_count, _cur_idx, series_list = _inspect_slide_series(st)
                if not (0 <= target_series < series_count):
                    raise ValueError(
                        f"series {target_series} out of range (0..{series_count - 1})"
                    )
                new_sid = registry.open_path(st.source_path, series=target_series)
                new_st = registry.get(new_sid)
                base_name = (
                    new_st.attrs["nd2wsi"]["source"]
                    if new_st is not None
                    else st.source_path.name
                )
                scene_name = (
                    series_list[target_series]["name"]
                    if target_series < len(series_list)
                    else ""
                )
                display_name = (
                    f"{base_name} · {scene_name}"
                    if (series_count > 1 and scene_name)
                    else base_name
                )
                return self._json(
                    {
                        "ok": True,
                        "sid": new_sid,
                        "name": display_name,
                        "url": f"/s/{new_sid}/",
                    }
                )
            except ConnectionError:
                raise
            except (
                ValueError,
                FileNotFoundError,
                OSError,
                NotImplementedError,
                RuntimeError,
                json.JSONDecodeError,
            ) as e:
                return self._error(400, str(e))

        def _trash(self):
            if registry.window_session is not None:
                return self._error(403, "Shared cache removal is unavailable in multiwindow mode; another window may still be reading it")
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
            try:
                if not sid or not registry.remove(sid):
                    return self._error(404, f"no open slide {sid}")
            except ValueError as e:
                return self._error(409, str(e))
            return self._json({"ok": True, "slides": registry.listing()})

        # ---- per-slide endpoints -----------------------------------------
        def _lut_settings_post(self, st: ViewerState):
            from .lut_settings import MAX_BYTES, LutSettingsUnavailable

            try:
                payload = self._body_json(limit=MAX_BYTES)
                return self._json(registry.lut_settings_for(st).write(payload))
            except LutSettingsUnavailable as exc:
                return self._error(409, str(exc))
            except (ValueError, TypeError, RecursionError) as exc:
                return self._error(400, str(exc))
            except OSError as exc:
                return self._error(500, f"Could not save LUT settings: {exc}")

        def _info_payload(self, st: ViewerState) -> dict[str, Any]:
            meta = st.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            is_raster = bool(meta.get("raster", False))
            try:
                dtype_name = np.dtype(meta["dtype"]).name
            except Exception:
                dtype_name = str(meta.get("dtype") or "")
            limnd2_ok = _limnd2_available()
            nd2_export = bool(
                limnd2_ok and not is_raster and dtype_name in ("uint8", "uint16")
            )
            if nd2_export:
                nd2_export_reason = ""
            elif is_raster:
                nd2_export_reason = (
                    "ND2 export is unavailable for JPEG and PNG images; use TIFF export instead."
                )
            elif dtype_name not in ("uint8", "uint16"):
                nd2_export_reason = (
                    f"ND2 export supports 8-bit and 16-bit integer images (this slide is {dtype_name}); "
                    "use TIFF export to preserve native values."
                )
            else:
                nd2_export_reason = (
                    "ND2 export requires the optional limnd2 package; use TIFF export instead."
                )
            series_count, series_index, series_list = _inspect_slide_series(st)
            return {
                "name": meta["source"],
                **({"window": registry.window_session.as_dict()} if registry.window_session is not None else {}),
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
                "nd2Export": nd2_export,
                "nd2ExportReason": nd2_export_reason,
                "raster": is_raster,
                "direct": bool(meta.get("direct")),
                "trashable": st.trash_path is not None and registry.window_session is None,
                "generation": st.generation,
                "storage": _storage_mode(meta),
                "kind": "plate" if meta.get("plate") else "slide",
                "plate": self._plate_block(st),
                "seriesCount": series_count,
                "seriesIndex": series_index,
                "seriesList": series_list,
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
            from .reader import _finite_positive

            mag = meta.get("objective_magnification")
            clean_mag = mag if _finite_positive(mag) else None
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
                "objective": clean_mag,
                "objective_magnification": clean_mag,
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
                frame = _frame_args(st, q, require_p=True)
                root = st.plate.root_for(*frame) if frame is not None else None
                payload = _pixel_payload(st, x, y, root=root)
                payload["generation"] = st.generation
                payload["frame"] = (
                    {"t": frame[0], "p": frame[1], "z": frame[2]}
                    if frame is not None
                    else None
                )
            except (TypeError, ValueError) as e:
                return self._error(400, str(e))
            self._json(payload)

        def _associated(self, st: ViewerState, name: str, q: dict):
            source = _associated_source_path(st)
            if source is None:
                return self._error(404, "associated image source is unavailable")
            try:
                backend = _associated_backend(source)
                # Two bounded display sizes; clients cannot request arbitrary
                # decode sizes or address a source outside the open slide.
                if q.get("detail") == ["1"]:
                    body = backend.associated_image_jpeg(source, name, max_side=2048)
                else:
                    body = backend.associated_image_jpeg(source, name)
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
            if st.attrs["nd2wsi"]["plate"].get("annotationScope") == "plane":
                frame = _frame_args(st, q, require_p=True)
                if st.source_path is None:
                    return None, frame[1]
                return registry.annotation_path(st.source_path, st.attrs, frame=frame), frame[1]
            raw = (q.get("p") or [None])[0]
            if st.plate.P > 1 and (raw is None or raw == ""):
                raise ValueError("p is required; choose a plate site first")
            if raw is None or raw == "":
                raw = "0"
            try:
                p = int(raw)
            except (TypeError, ValueError):
                raise ValueError("p must be an integer") from None
            if not 0 <= p < st.plate.P:
                raise ValueError(f"p={p} out of range (0..{st.plate.P - 1})")
            if st.source_path is None:
                return None, p
            return registry.annotation_path(st.source_path, st.attrs, site=p), p

        def _annotations_get(self, st: ViewerState, q: dict | None = None):
            try:
                p, _site = self._plate_site(st, q or {})
            except ValueError as e:
                return self._error(400, str(e))
            if p is None:
                return self._json({"items": [], "path": None})
            lv0 = st.attrs["nd2wsi"]["levels"][0]

            def _same_geometry(document: Any) -> None:
                src = document.get("source") if isinstance(document, dict) else None
                if not isinstance(src, dict):
                    return
                w, h = src.get("width"), src.get("height")
                if (
                    isinstance(w, int)
                    and not isinstance(w, bool)
                    and isinstance(h, int)
                    and not isinstance(h, bool)
                    and (w, h) != (int(lv0["width"]), int(lv0["height"]))
                ):
                    raise ValueError(
                        f"these annotations were drawn on a {w} x {h} image; this image is "
                        f"{lv0['width']} x {lv0['height']}"
                    )

            # A sidecar that is not an object with an items list is refused,
            # never read as empty: the pane's next save would replace it.
            if registry.annotation_workspace is not None:
                try:
                    document, rev = registry.annotation_workspace.read(p)
                    items = _sidecar_items(document)
                    _same_geometry(document)
                    return self._json({"items": items, "path": str(p),
                                       **({"slacs": document["slacs"]} if document and "slacs" in document else {}),
                                       "revision": rev, "window": registry.window_session.as_dict()})
                except (ValueError, UnicodeError, OSError, TimeoutError) as e:
                    return self._error(500, f"could not read {p.name}: {e}")
            document = {}
            if p.exists() or p.is_symlink():
                try:
                    from .annotation_workspace import _read

                    raw_bytes = _read(p)
                    if raw_bytes is None:
                        items = []
                    else:
                        document = json.loads(raw_bytes.decode("utf-8"))
                        items = _sidecar_items(document, require_items=True)
                        _same_geometry(document)
                except (ValueError, UnicodeError, OSError) as e:
                    return self._error(500, f"could not read {p.name}: {e}")
            else:
                items = []
            self._json({"items": items, "path": str(p),
                        **({"slacs": document["slacs"]} if "slacs" in document else {})})

        def _annotations_post(self, st: ViewerState, q: dict | None = None):
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
            from .slacs_laser import metadata

            try:
                laser_metadata = metadata(data.get("slacs"))
            except ValueError as e:
                return self._error(400, str(e))
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
                **laser_metadata,
            }
            if site is not None:
                if meta["plate"].get("annotationScope") == "plane":
                    t, site, z = _frame_args(st, q or {}, require_p=True)
                    payload["selection"] = {"t": t, "p": site, "z": z}
                else:
                    payload["selection"] = {"p": site}
                sites = (meta.get("plate") or {}).get("sites") or []
                payload["site"] = sites[site]["name"] if site < len(sites) else None
            try:
                raw = json.dumps(payload, indent=1, allow_nan=False).encode("utf-8")
            except ValueError as e:
                return self._error(400, str(e))
            if registry.annotation_workspace is not None:
                from .annotation_workspace import AnnotationConflict
                try:
                    rev = registry.annotation_workspace.write(p, payload, data.get("expected_revision"), source_path=st.source_path)
                except AnnotationConflict as e:
                    return self._json({"ok": False, "error": str(e), "conflict": True,
                                       "current_revision": e.current_revision,
                                       "draft_path": str(e.draft_path) if e.draft_path else None,
                                       "draft_error": e.draft_error}, code=409)
                except (ValueError, UnicodeError, OSError, TimeoutError) as e:
                    return self._error(500, f"could not save {p.name}: {e}")
                return self._json({"ok": True, "path": str(p), "count": len(data["items"]), "revision": rev})
            from contextlib import ExitStack

            from .annotation_workspace import _atomic_write, annotation_lock

            # unique, fsynced temp + per-slide and window lock: two tabs or
            # windows saving at once cannot interleave or clobber each other.
            # The window lock is advisory: when it cannot be used here (another
            # account's file, no flock on this filesystem, ...) the atomic
            # write proceeds as in 2.6.2; only real contention is refused.
            lock_path = p.with_name("." + p.name + ".window-lock")
            failure: tuple[int, str] | None = None
            with st.lock, ExitStack() as held:
                if lock_path.exists():
                    try:
                        held.enter_context(annotation_lock(p))
                    except TimeoutError as e:
                        if e.errno is None:  # SessionFileLock contention, not ETIMEDOUT
                            failure = (409, "annotations are open in another window")
                    except OSError:
                        pass
                if failure is None:
                    try:
                        _atomic_write(p, raw)
                    except OSError as e:
                        failure = (500, f"could not save {p.name}: {e}")
            if failure is not None:
                code, message = failure
                if code == 409:
                    return self._json({"error": message}, code=409)
                return self._error(code, message)
            self._json({"ok": True, "path": str(p), "count": len(data["items"])})

        def _histogram(self, st: ViewerState, q: dict | None = None):
            if st.plate is not None:
                try:
                    t, p, z = _frame_args(st, q or {}, require_p=True)
                except ValueError as e:
                    return self._error(400, str(e))
                return self._json(
                    {
                        "channels": st.plate.histogram(t, p, z),
                        "generation": st.generation,
                        "frame": {"t": t, "p": p, "z": z},
                    }
                )
            # Its own lock: LUT settings and annotation saves on st.lock never
            # wait for a histogram. Requests hold busy, and a replacement
            # drains busy before it resets histograms.
            with st._hist_lock:
                if st.histograms is None:
                    st.histograms = render.compute_histograms(st.root, st.attrs)
            self._json(
                {"channels": st.histograms, "generation": st.generation, "frame": None}
            )

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
            try:
                with st.busy:
                    generation = (q.get("g") or [None])[0]
                    if generation and generation != st.generation:
                        return self._error(409, "The pyramid changed; refresh the slide")
                    if q.get("correction"):
                        return self._error(409, "Shading Correction now replaces the pyramid; refresh the slide")
                    return self._tile_impl(st, m, q)
            except ValueError as e:
                return self._error(409, str(e))

        def _tile_impl(self, st: ViewerState, m: re.Match, q: dict):
            level, tx, ty = int(m.group(1)), int(m.group(2)), int(m.group(3))
            fmt = m.group(4)
            n = len(st.attrs["omero"]["channels"])
            try:
                channels = render.parse_channels((q.get("c") or [None])[0], n)
            except ValueError as e:
                return self._error(400, str(e))
            win = (q.get("win") or [None])[0]
            col = (q.get("col") or [None])[0]
            try:
                frame = _frame_args(st, q)
            except ValueError as e:
                return self._error(400, str(e))
            root = st.plate.root_for(*frame) if frame is not None else st.root
            try:
                body = _render_tile_compat(
                    root, st.attrs, level, tx, ty, channels, fmt, win, col
                )
            except KeyError as e:
                return self._error(404, str(e))
            except ValueError as e:
                return self._error(409, str(e))
            ctype = "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"
            self._send(200, body, ctype, cache=self._immutable_when_current(st, q))
            if frame is not None and st.plate is not None and hasattr(st.plate, "prefetch_neighbors"):
                try:
                    st.plate.prefetch_neighbors(*frame)
                except Exception:  # pragma: no cover - prefetch never reaches a client
                    pass

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
            try:
                channels = render.parse_channels((q.get("c") or [None])[0], n)
            except ValueError as e:
                return self._error(400, str(e))
            win = (q.get("win") or [None])[0]
            col = (q.get("col") or [None])[0]
            try:
                body = _render_plate_frame_compat(plate, t, p, z, k, channels, fmt, win, col)
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

        def _astromap(self, st: ViewerState):
            try:
                payload = self._body_json(limit=1_000_000)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._error(400, str(exc))
            prefix = (f"agent-{registry.window_session.id[:12]}_" if registry.agent_window else "")
            try:
                # A pyramid replacement drains this lease before changing root,
                # attrs or generation. Do not acquire _replacement_lock here:
                # replacement holds it while waiting for readers to finish.
                with st.busy, st.exports:
                    plan = prepare_astromap(st, payload, resolve_frame=_frame_args,
                                           filename_prefix=prefix)
                    result = registry.astromap_downloads.create(plan, st)
            except AstromapConflict as exc:
                return self._error(409, str(exc))
            except AstromapError as exc:
                return self._error(400, str(exc))
            except ValueError as exc:
                return self._error(409, str(exc))
            return self._json(result)

        def _astromap_download(self, st: ViewerState, q: dict):
            key = (q.get("token") or [""])[0]
            try:
                # The zip is this window's own file, not slide pixels, so it
                # takes no busy lease; it still counts for close and update.
                with st.exports, registry.astromap_downloads.download(key, st) as (item, source):

                    def write_body(output) -> None:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)

                    self._stream_file(
                        write_body,
                        size=item.path.stat().st_size,
                        content_type="application/zip",
                        filename=item.filename,
                    )
            except AstromapError as exc:
                return self._error(404, str(exc))
            except LeaseClosed as exc:
                if exc.lease is not st.exports:
                    raise
                # The slide closed before the download began.
                return self._error(409, "slide was closed")

        def _roi(self, st: ViewerState, q: dict):
            try:
                with st.busy:
                    return self._roi_impl(st, q)
            except ValueError as e:
                if "closed" in str(e):
                    return self._error(409, "slide was closed")
                raise

        def _roi_impl(self, st: ViewerState, q: dict):
            from contextlib import nullcontext
            from dataclasses import replace as _dc_replace

            prefix = (
                f"agent-{registry.window_session.id[:12]}_"
                if registry.agent_window else ""
            )
            col = (q.get("col") or [None])[0]
            try:
                export = prepare_export(st, q, resolve_frame=_frame_args, filename_prefix=prefix)
                # Scale-strip previews and size samples are api/roi reads too.
                # They stay plain slide reads so they never block a close.
                counted = (export.streams_file
                           or export.w * export.h >= EXPORT_COUNT_MIN_PIXELS)
                with st.exports if counted else nullcontext():
                    if export.streams_file:
                        with export.file(_job_update) as artifact:
                            self._stream_file(
                                artifact.copy_to,
                                size=artifact.size,
                                content_type=artifact.content_type,
                                filename=artifact.filename,
                            )
                        return
                    if col:
                        if _accepts_kwarg(render.export_roi_rendered, "col"):
                            if export.fmt not in ("png", "jpg", "jpeg", "svg"):
                                raise ExportRequestError(f"unknown format {export.fmt}")
                            if export.w * export.h / 1e6 > export.max_render_mpx:
                                export.rendered()
                            try:
                                body = render.export_roi_rendered(
                                    export.root,
                                    export.attrs,
                                    export.level,
                                    export.x,
                                    export.y,
                                    export.w,
                                    export.h,
                                    export.channels,
                                    export.fmt,
                                    export.window,
                                    scale_bar=export.scale_bar,
                                    col=col,
                                )
                            except ValueError as exc:
                                raise ExportRequestError(str(exc)) from exc
                            ext = "jpg" if export.fmt == "jpeg" else export.fmt
                            ctype = {
                                "png": "image/png",
                                "jpg": "image/jpeg",
                                "svg": "image/svg+xml",
                            }[ext]
                            suffix = "_scalebar" if export.scale_bar else ""
                            return self._send(
                                200,
                                body,
                                ctype,
                                {
                                    "Content-Disposition": content_disposition(
                                        f"{export.filename}{suffix}.{ext}"
                                    )
                                },
                            )
                        export = _dc_replace(
                            export, attrs=_attrs_with_colors(export.attrs, col)
                        )
                    artifact = export.rendered()
                    return self._send(
                        200, artifact.body, artifact.content_type,
                        {"Content-Disposition": content_disposition(artifact.filename)},
                    )
            except ExportRequestError as e:
                return self._error(400, str(e))

    return Handler


def create_server(
    store_paths: str | Path | list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8000,
    max_render_mpx: float = 400.0,
    window_session=None,
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
    registry = SlideRegistry(max_render_mpx=max_render_mpx, window_session=window_session)
    token = secrets.token_urlsafe(16)

    class _Server(ThreadingHTTPServer):
        # OpenSeadragon opens many tile connections in one burst; the
        # stdlib default backlog of 5 resets the overflow at the kernel.
        request_queue_size = 64
        daemon_threads = True
        max_workers = _resolve_http_max_workers()

        def __init__(self, *args, **kwargs):
            self._pool_lock = threading.Lock()
            self._shutting_down = False
            self._pool_closed = False
            self._pending_sockets: dict[Any, Any] = {}
            self._active_sockets: set[Any] = set()
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="nd2wsi-http",
            )
            try:
                super().__init__(*args, **kwargs)
            except BaseException:
                self._pool_closed = True
                self._executor.shutdown(wait=False, cancel_futures=True)
                raise

        def server_bind(self) -> None:
            # Bypass HTTPServer.server_bind()'s socket.getfqdn(host) reverse DNS /
            # mDNS lookup, which blocks 5s+ per server on offline Macs and macOS CI VMs.
            socketserver.TCPServer.server_bind(self)
            host, port = self.server_address[:2]
            self.server_name = str(host) if host else "127.0.0.1"
            self.server_port = int(port)

        def process_request(self, request, client_address) -> None:
            with self._pool_lock:
                if self._shutting_down or self._pool_closed:
                    abort = True
                else:
                    abort = False
                    key = object()
                    self._pending_sockets[key] = request
                    try:
                        self._executor.submit(
                            self._run_pooled_request, key, request, client_address
                        )
                    except RuntimeError:
                        self._pending_sockets.pop(key, None)
                        abort = True
            if abort:
                self.shutdown_request(request)

        def _run_pooled_request(self, key: Any, request, client_address) -> None:
            with self._pool_lock:
                if self._pending_sockets.pop(key, None) is None:
                    return
                if self._shutting_down or self._pool_closed:
                    abort = True
                else:
                    abort = False
                    self._active_sockets.add(request)
            if abort:
                try:
                    self.shutdown_request(request)
                except Exception:
                    pass
                return
            try:
                self.process_request_thread(request, client_address)
            finally:
                with self._pool_lock:
                    self._active_sockets.discard(request)

        def handle_error(self, request, client_address) -> None:
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionError, TimeoutError)):
                return
            if isinstance(exc, OSError) and (
                self._shutting_down or exc.errno in _DISCONNECT_ERRNOS
            ):
                return
            super().handle_error(request, client_address)

        def _drop_queued_requests(self) -> None:
            with self._pool_lock:
                self._shutting_down = True
                queued = list(self._pending_sockets.values())
                self._pending_sockets.clear()
            for req in queued:
                try:
                    self.shutdown_request(req)
                except Exception:
                    pass

        def _shutdown_pool(self) -> None:
            import socket as _socket

            with self._pool_lock:
                self._shutting_down = True
                already_closed = self._pool_closed
                self._pool_closed = True
                queued = list(self._pending_sockets.values())
                self._pending_sockets.clear()
                active = list(self._active_sockets)
            if not already_closed:
                self._executor.shutdown(wait=False, cancel_futures=True)
            for req in queued:
                try:
                    self.shutdown_request(req)
                except Exception:
                    pass
            for req in active:
                try:
                    req.shutdown(_socket.SHUT_RDWR)
                except Exception:
                    pass
                if os.name == "nt":
                    # On Windows Winsock, shutdown(SD_BOTH) does not wake a thread
                    # blocked in recv() on an idle keep-alive socket, and socket.close()
                    # defers _real_close() while BaseHTTPRequestHandler.rfile holds _io_refs > 0.
                    try:
                        req._real_close()
                    except Exception:
                        pass
            if not already_closed:
                cur = threading.current_thread()
                join_deadline = time.monotonic() + 5.0
                for worker in list(getattr(self._executor, "_threads", ())):
                    if worker is not cur and worker.is_alive():
                        remaining = join_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        worker.join(timeout=remaining)

        def _close_registry(self) -> None:
            if getattr(self, "_registry_closed", False):
                return
            self._registry_closed = True
            registry.astromap_downloads.close()
            registry.close_all(immediate=True)

        def shutdown(self) -> None:
            self._drop_queued_requests()
            super().shutdown()
            self._close_registry()

        def shutdown_for_relaunch(self, timeout: float | None = None) -> None:
            """Stop accepting work and synchronously release update locks.

            With ``timeout``, raise TimeoutError while reads still hold
            sources after it. The release goes on, and calling this again
            waits for it again.
            """
            self._drop_queued_requests()
            super().shutdown()
            if not getattr(self, "_registry_closed", False):
                self._registry_closed = self._released_for_relaunch = True
                registry.astromap_downloads.close()
            if getattr(self, "_released_for_relaunch", False):
                registry.close_all_for_relaunch(timeout=timeout)

        def server_close(self) -> None:
            self._shutdown_pool()
            self._close_registry()
            super().server_close()

    try:
        for p in store_paths:
            registry.add_store(p)
        httpd = _Server((host, port), make_handler(registry, token))
    except BaseException:
        registry.astromap_downloads.close()
        registry.close_all(immediate=True)
        raise
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
