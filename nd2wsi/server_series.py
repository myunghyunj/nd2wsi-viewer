"""Series/multi-scene/plate discovery, metadata normalization, and per-series view/tile helpers.

Extracted from ``nd2wsi.server`` so slide/series inspection and rendering
helpers live independently of HTTP routing and slide-registry lifecycle.
"""

from __future__ import annotations

import functools
import inspect
import math
import os
import re
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import render


def _resolve_server_hook(name: str, local_value: Any, original_value: Any) -> Any:
    """Honor monkeypatches applied either directly here or on ``nd2wsi.server``."""
    if local_value is not original_value:
        return local_value
    server_mod = sys.modules.get("nd2wsi.server")
    if server_mod is not None:
        return getattr(server_mod, name, local_value)
    return local_value


# ---- Tile & Frame Color/Rendering Compatibility Helpers ------------------

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


# ---- Multi-Series / Multi-Scene Container Metadata Inspection ------------

_SERIES_META_CACHE: OrderedDict[
    tuple[str, int, int, int], tuple[int, list[dict[str, Any]]]
] = OrderedDict()
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


def _inspect_slide_series(st: Any) -> tuple[int, int, list[dict[str, Any]]]:
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
    inspect_fn = _resolve_server_hook(
        "_inspect_path_series", _inspect_path_series, _ORIG_INSPECT_PATH_SERIES
    )
    count, series_list = inspect_fn(st.source_path)
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


# ---- Storage Inspection, Associated Images, Plate & Pixel View Helpers ---

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
                allocated = _add_allocation(
                    allocated, _allocated_bytes(entry_path.lstat(), entry_path)
                )
            except OSError:
                continue
        for name in files:
            try:
                st = (Path(walk_root) / name).lstat()
            except OSError:
                continue
            logical += int(st.st_size)
            allocated = _add_allocation(
                allocated, _allocated_bytes(st, Path(walk_root) / name)
            )
    return {"bytes": logical, "allocated_bytes": allocated}


def _storage_mode(meta: dict[str, Any]) -> str:
    if meta.get("direct"):
        return "direct"
    return {
        "source-backed": "compact",
        "overview-degraded": "overview-degraded",
    }.get(meta.get("kind"), "full")


def _inspection_storage_details(
    st: Any, meta: dict[str, Any]
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


def _associated_source_path(st: Any) -> Path | None:
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


def _associated_names(st: Any) -> list[str]:
    source_fn = _resolve_server_hook(
        "_associated_source_path", _associated_source_path, _ORIG_ASSOCIATED_SOURCE_PATH
    )
    backend_fn = _resolve_server_hook(
        "_associated_backend", _associated_backend, _ORIG_ASSOCIATED_BACKEND
    )
    source = source_fn(st)
    if source is None:
        return []
    try:
        names = backend_fn(source).associated_image_names(source)
        return names if source_fn(st) == source else []
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
    st: Any, q: dict, *, require_p: bool = False
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
    st: Any, x: float, y: float, root: Any = None
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


_ORIG_INSPECT_PATH_SERIES = _inspect_path_series
_ORIG_ASSOCIATED_SOURCE_PATH = _associated_source_path
_ORIG_ASSOCIATED_BACKEND = _associated_backend
