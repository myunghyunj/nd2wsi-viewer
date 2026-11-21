"""Plate geometry, well-label parsing, stage coordinate inference, and layout builders.

Separates pure plate detection, site grid clustering, well identity matching,
and acquisition metadata/layout construction from cache persistence and runtime
frame serving.
"""

from __future__ import annotations

import math
import os
import re
import sys
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .platform_io import BINARY

PLATE_MAX_FRAME_PX = 4_500_000  # a camera field, not a stitched scan
CZI_LIF_PLATE_MAX_DIM = 8192  # keeps level_shapes within PLATE_K=(2, 4, 8, 16) at tile=512
CZI_LIF_PLATE_MAX_FRAME_PX = 40_000_000  # confocal / lightsheet Z-stacks and time series
THUMB_K = 8  # the reduction the store keeps, the one the site grid shows

_ORIG_PLATE_MAX_FRAME_PX = PLATE_MAX_FRAME_PX
_ORIG_CZI_LIF_PLATE_MAX_DIM = CZI_LIF_PLATE_MAX_DIM
_ORIG_CZI_LIF_PLATE_MAX_FRAME_PX = CZI_LIF_PLATE_MAX_FRAME_PX
_ORIG_THUMB_K = THUMB_K


def _layout_limit(name: str, local_value: int, original_value: int) -> int:
    """Resolve a layout limit honoring monkeypatches on either ``nd2wsi.plate`` or this module."""
    if local_value != original_value:
        return local_value
    plate_mod = sys.modules.get("nd2wsi.plate")
    if plate_mod is not None:
        override = getattr(plate_mod, name, original_value)
        if override != original_value:
            return override
    return local_value


def _czi_plate_geometry(s0_entries: Sequence[Any]) -> tuple[int, int, int, int]:
    """Return ``(x0, y0, width, height)`` bounding box of level-0 CZI subblocks."""
    x0 = min(e.x_start for e in s0_entries)
    y0 = min(e.y_start for e in s0_entries)
    width = max(e.x_start + e.x_size for e in s0_entries) - x0
    height = max(e.y_start + e.y_size for e in s0_entries) - y0
    return x0, y0, width, height


def _lif_series_bounds(series: Any) -> tuple[int, int]:
    """Return ``(width, height)`` of a LIF series including mosaic tile extents."""
    if series.tile_positions_px and series.n_m > 1:
        width = max(tx + series.width for tx, _ty in series.tile_positions_px)
        height = max(ty + series.height for _tx, ty in series.tile_positions_px)
        return width, height
    return series.width, series.height


def _lif_plate_series(all_series: list[Any]) -> list[Any]:
    """Only combine series that share the plate's geometry and metadata.

    Do not filter a subset: site indices must still identify the original LIF
    series (including in existing annotation sidecars). Heterogeneous 2D
    containers use the ordinary, explicitly selected-series conversion path.
    """
    first = all_series[0]
    if first.n_z > 1 or first.n_t > 1:
        return [first]

    def signature(s):
        return (
            s.width, s.height, s.n_z, s.n_t, s.n_m, s.dtype, s.rgb,
            tuple(s.tile_positions_px or [(0, 0)]),
            s.pixel_size_um, s.calibration_source, s.magnification, s.objective_name,
            tuple((ch.name, ch.color, ch.data_type, ch.resolution) for ch in s.channels),
        )

    expected = signature(first)
    return all_series if all(signature(s) == expected for s in all_series) else []


def is_plate_file(path: str | Path) -> bool:
    """True for an ND2, CZI, or LIF of camera fields with a T, P or Z loop."""
    try:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in (".nd2", ".czi", ".lif") or not path.is_file():
            return False
        thumb_k = _layout_limit("THUMB_K", THUMB_K, _ORIG_THUMB_K)
        max_dim = _layout_limit(
            "CZI_LIF_PLATE_MAX_DIM",
            CZI_LIF_PLATE_MAX_DIM,
            _ORIG_CZI_LIF_PLATE_MAX_DIM,
        )
        czi_lif_max_px = _layout_limit(
            "CZI_LIF_PLATE_MAX_FRAME_PX",
            CZI_LIF_PLATE_MAX_FRAME_PX,
            _ORIG_CZI_LIF_PLATE_MAX_FRAME_PX,
        )
        max_frame_px = _layout_limit(
            "PLATE_MAX_FRAME_PX",
            PLATE_MAX_FRAME_PX,
            _ORIG_PLATE_MAX_FRAME_PX,
        )
        if suffix == ".czi":
            from .czi import _level0_entries, _read_czi_header_and_index

            file_size = path.stat().st_size
            fd = os.open(str(path), os.O_RDONLY | BINARY)
            try:
                entries, _ = _read_czi_header_and_index(fd, file_size)
            finally:
                os.close(fd)
            lvl0 = _level0_entries(entries)
            s_values = sorted({e.s for e in lvl0})
            s0_entries = [e for e in lvl0 if e.s == s_values[0]] if s_values else lvl0
            _x0, _y0, width, height = _czi_plate_geometry(s0_entries)
            if not (thumb_k <= width <= max_dim and thumb_k <= height <= max_dim):
                return False
            if width * height > czi_lif_max_px:
                return False
            n_z = len({e.z for e in s0_entries})
            n_t = len({e.t for e in s0_entries})
            return n_z > 1 or n_t > 1
        if suffix == ".lif":
            from .lif import _list_lif_series, _read_lif_header_and_blocks

            file_size = path.stat().st_size
            fd = os.open(str(path), os.O_RDONLY | BINARY)
            try:
                root, blocks = _read_lif_header_and_blocks(fd, file_size)
            finally:
                os.close(fd)
            all_series = _list_lif_series(root, blocks)
            s0 = all_series[0]
            width, height = _lif_series_bounds(s0)
            if not (thumb_k <= width <= max_dim and thumb_k <= height <= max_dim):
                return False
            if width * height > czi_lif_max_px:
                return False
            if s0.n_z > 1 or s0.n_t > 1:
                return True
            return len(_lif_plate_series(all_series)) > 1
        import nd2

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with nd2.ND2File(str(path)) as f:
                if getattr(f, "is_legacy", False):
                    return False
                if (getattr(f.attributes, "compressionType", None) or "none") != "none":
                    return False
                sizes = dict(f.sizes)
                h = int(sizes.get("Y", 0))
                w = int(sizes.get("X", 0))
                if not (thumb_k <= h <= max_dim and thumb_k <= w <= max_dim):
                    return False
                area = h * w
                if area <= 0 or area > max_frame_px:
                    return False
                loops = sizes.get("T", 1) * sizes.get("P", 1) * sizes.get("Z", 1)
                return loops > 1
    except Exception:
        return False


def _cluster_1d(values: list[float], *, gap_limit: float | None = None) -> list[int]:
    """Cluster index per value, ascending, split where the step between
    neighbours stands out.

    Half the widest step allows a column to spread over a few millimetres.
    Missing tracks can make that estimate too large; site_layout supplies
    a smaller limit when the resulting cells contain separate stage sites.
    The floor keeps revisits within a millimetre in the same track.
    """
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    steps = [d for d in (values[b] - values[a] for a, b in zip(order, order[1:])) if d > 0]
    widest = max(steps) if steps else 0.0
    gap = 0.5 * widest
    if gap_limit is not None:
        gap = min(gap, gap_limit)
    gap = max(1000.0, gap)
    labels = [0] * n
    c = 0
    for prev, cur in zip(order, order[1:]):
        if values[cur] - values[prev] > gap:
            c += 1
        labels[cur] = c
    return labels


def _named_site_layout(
    xs: list[float], ys: list[float], names: list[str] | None,
) -> list[tuple[int, int]] | None:
    """Use unambiguous well identities, with orientation taken from the stage."""
    if names is None or len(names) != len(xs):
        return None
    wells = []
    for name in names:
        match = re.fullmatch(r"([A-Za-z]+)0*([1-9]\d*)", str(name).strip())
        if match is None:
            return None
        row = 0
        for letter in match[1].upper():
            row = row * 26 + ord(letter) - ord("A") + 1
        try:
            wells.append((row, int(match[2])))
        except ValueError:
            return None
    # Repeated well names may represent several fields of one well. Their
    # geometry must remain authoritative instead of merging by identity.
    if len(set(wells)) != len(wells):
        return None

    def axis(values: list[float], part: int) -> tuple[list[int], float]:
        groups: dict[int, list[float]] = {}
        for well, value in zip(wells, values):
            groups.setdefault(well[part], []).append(value)
        centers = {key: sum(v) / len(v) for key, v in groups.items()}
        order = sorted(groups, key=lambda key: (centers[key], key))
        ranks = {key: rank for rank, key in enumerate(order)}
        error = sum(
            (value - centers[well[part]]) ** 2 for well, value in zip(wells, values)
        )
        return [ranks[well[part]] for well in wells], error

    candidates = []
    for row_part, col_part in ((0, 1), (1, 0)):
        rows, row_error = axis(ys, row_part)
        cols, col_error = axis(xs, col_part)
        candidates.append((row_error + col_error, list(zip(rows, cols))))
    # Compare normal and transposed grouping; sorting each track by its
    # stage center also preserves reversed axes and serpentine acquisitions.
    return min(candidates, key=lambda candidate: candidate[0])[1]


def site_layout(
    points: list[tuple[float, float] | None], *, names: list[str] | None = None,
) -> list[tuple[int, int]]:
    """(row, col) per stage point. Rows follow ascending Y, columns ascending X.

    Unique well names disambiguate sparse grids without assuming a plate size.
    Without usable coordinates every site lands in row 0 at its own index.
    """
    n = len(points)
    if n == 0:
        return []
    xs: list[float] = []
    ys: list[float] = []
    for pt in points:
        try:
            x, y = float(pt[0]), float(pt[1])  # type: ignore[index]
        except (TypeError, ValueError, IndexError):
            return [(0, i) for i in range(n)]
        if not (math.isfinite(x) and math.isfinite(y)):
            return [(0, i) for i in range(n)]
        xs.append(x)
        ys.append(y)
    named = _named_site_layout(xs, ys, names)
    if named is not None:
        return named
    rows = _cluster_1d(ys)
    cols = _cluster_1d(xs)
    # Refine only occupied cells that combine widely separated positions.
    # Independent smallest-gap clustering would split a real column whose
    # sites drift by 3.2 mm between rows. Resolve the strongest separation
    # first, then reconsider collisions with the other axis still intact.
    limits = [math.inf, math.inf]
    while True:
        cells: dict[tuple[int, int], list[int]] = {}
        for i, cell in enumerate(zip(rows, cols)):
            cells.setdefault(cell, []).append(i)
        candidates = []
        for axis, values in enumerate((ys, xs)):
            steps = []
            for members in cells.values():
                positions = sorted(values[i] for i in members)
                steps.extend(
                    b - a for a, b in zip(positions, positions[1:]) if b - a > 1000.0
                )
            if steps:
                limit = min(limits[axis], 0.5 * min(steps))
                labels = _cluster_1d(values, gap_limit=limit)
                if labels != (rows if axis == 0 else cols):
                    candidates.append((max(steps), axis, limit, labels))
        if not candidates:
            break
        _, axis, limit, labels = max(candidates, key=lambda candidate: candidate[0])
        limits[axis] = limit
        if axis == 0:
            rows = labels
        else:
            cols = labels
    return list(zip(rows, cols))


def _exposure_ms(text_info: dict) -> float | None:
    for key in ("capturing", "description"):
        text = text_info.get(key)
        if not isinstance(text, str):
            continue
        m = re.search(r"Exposure:\s*([0-9]+(?:\.[0-9]+)?)\s*ms", text)
        if m:
            return float(m.group(1))
    return None


def _finite_positive(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _loop(f: Any, kind: str) -> Any:
    try:
        for loop in f.experiment or []:
            if getattr(loop, "type", None) == kind:
                return loop
    except Exception:
        return None
    return None


def _nd2_site_layout(
    f: Any,
    n_sites: int,
    *,
    layout_fn: Callable[..., list[tuple[int, int]]] = site_layout,
) -> tuple[list[dict[str, Any]], int, int]:
    """Extract site records and ``(rows, cols)`` from an ND2 ``XYPosLoop``."""
    sites: list[dict[str, Any]] = []
    points: list[tuple[float, float] | None] = []
    xy = _loop(f, "XYPosLoop")
    raw_points = []
    try:
        raw_points = list(xy.parameters.points) if xy is not None else []
    except Exception:
        raw_points = []
    for p in range(n_sites):
        name = f"Site {p + 1}"
        stage = None
        if p < len(raw_points):
            pt = raw_points[p]
            pname = getattr(pt, "name", None)
            if pname:
                name = str(pname)
            pos = getattr(pt, "stagePositionUm", None)
            try:
                stage = [float(pos.x), float(pos.y), float(pos.z)]
                if not all(math.isfinite(v) for v in stage):
                    stage = None
            except (AttributeError, TypeError, ValueError):
                stage = None
        sites.append({"i": p, "name": name, "row": 0, "col": p, "stageUm": stage})
        points.append((stage[0], stage[1]) if stage else None)
    layout = layout_fn(points, names=[site["name"] for site in sites])
    for site, (row, col) in zip(sites, layout):
        site["row"], site["col"] = int(row), int(col)
    rows = 1 + max((s["row"] for s in sites), default=0)
    cols = 1 + max((s["col"] for s in sites), default=0)
    return sites, rows, cols


def _nd2_z_metadata(f: Any, n_z: int) -> tuple[int, float | None, bool | None]:
    """Extract ``(z_home, z_step_um, bottom_to_top)`` from an ND2 ``ZStackLoop``."""
    zl = _loop(f, "ZStackLoop")
    z_home = n_z // 2
    z_step_um: float | None = None
    bottom_to_top: bool | None = None
    if zl is not None:
        params = getattr(zl, "parameters", None)
        home = getattr(params, "homeIndex", None)
        if isinstance(home, int) and 0 <= home < n_z:
            z_home = home
        z_step_um = _finite_positive(getattr(params, "stepUm", None))
        direction = getattr(params, "bottomToTop", None)
        bottom_to_top = direction if isinstance(direction, bool) else None
    if z_step_um is None:
        try:
            z_step_um = _finite_positive(f.voxel_size().z)
        except Exception:
            z_step_um = None
    return z_home, z_step_um, bottom_to_top


def _nd2_time_metadata(
    f: Any,
    n_t: int,
    seq_fn: Callable[[int, int, int], int],
) -> tuple[float | None, list[float], float | None]:
    """Extract ``(period_ms, times_ms, exposure_ms)`` from an ND2 ``TimeLoop``."""
    tl = _loop(f, "TimeLoop")
    period_ms: float | None = None
    if tl is not None:
        period_ms = _finite_positive(
            getattr(getattr(tl, "parameters", None), "periodMs", None)
        )
    times_ms: list[float] = []
    for t in range(n_t):
        value = None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fm = f.frame_metadata(seq_fn(t, 0, 0))
            value = float(fm.channels[0].time.relativeTimeMs)
            if not math.isfinite(value):
                value = None
        except Exception:
            value = None
        if value is None:
            value = t * period_ms if period_ms else float(t)
        times_ms.append(value)

    try:
        exposure_ms = _exposure_ms(dict(f.text_info or {}))
    except Exception:
        exposure_ms = None
    return period_ms, times_ms, exposure_ms


def _index_czi_subblocks(
    s0_entries: Sequence[Any],
    x0: int,
    y0: int,
    width: int,
    height: int,
    t_values: Sequence[int],
    z_values: Sequence[int],
    *,
    rgb: bool,
) -> tuple[dict[tuple[int, int], list[Any]], dict[tuple[int, int, int], list[Any]], bool]:
    """Index CZI subblocks by ``(t, z)`` and ``(t, z, c)`` and detect sparse RGB planes."""
    by_tz: dict[tuple[int, int], list[Any]] = {}
    by_tzc: dict[tuple[int, int, int], list[Any]] = {}
    for sb in s0_entries:
        by_tz.setdefault((sb.t, sb.z), []).append(sb)
        by_tzc.setdefault((sb.t, sb.z, sb.c), []).append(sb)
    requires_manifest_channel_dtypes = False
    if rgb and (len(z_values) > 1 or len(t_values) > 1):
        needs_zero_bg_tag = any(
            len(z_sbs := by_tz.get((t_val, z_val), ())) == 0
            or (
                len(z_sbs) == 1
                and (
                    z_sbs[0].x_start != x0
                    or z_sbs[0].y_start != y0
                    or z_sbs[0].x_size != width
                    or z_sbs[0].y_size != height
                )
            )
            for t_val in t_values
            for z_val in z_values
        )
        if needs_zero_bg_tag:
            requires_manifest_channel_dtypes = True
    return by_tz, by_tzc, requires_manifest_channel_dtypes


def _lif_plate_sites(selected: Sequence[Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Build site records and ``(rows, cols)`` for homogeneous LIF plate series."""
    sites = [
        {"i": p, "name": ser.name or f"Series {p + 1}", "row": 0, "col": p, "stageUm": None}
        for p, ser in enumerate(selected)
    ]
    return sites, 1, len(selected)


def _build_pyramid_levels(shapes: Sequence[tuple[int, int]]) -> list[dict[str, Any]]:
    """Build level metadata dicts for one plate frame's pyramid shapes."""
    return [
        {"path": str(k), "width": w, "height": h, "downsample": 2**k}
        for k, (h, w) in enumerate(shapes)
    ]


def _build_plate_notes(
    *,
    n_t: int,
    n_p: int,
    n_z: int,
    period_ms: float | None,
    z_step_um: float | None,
    pixel_size_um: float | None,
) -> list[str]:
    """Build descriptive notes for a plate slide's metadata block."""
    notes = []
    if n_t > 1 and period_ms:
        notes.append(f"time series: {n_t} frames every {period_ms / 60000:g} min")
    elif n_t > 1:
        notes.append(f"time series: {n_t} frames")
    notes.append(f"{n_p} sites")
    if z_step_um:
        notes.append(f"{n_z} z planes, step {z_step_um:g} µm")
    else:
        notes.append(f"{n_z} z planes")
    if pixel_size_um is None:
        notes.append("no pixel calibration in the file; measurements are in pixels")
    notes.append(
        "full-resolution pixels are served from the ND2; reduced grid frames "
        "use a verified managed cache"
    )
    return notes


def _build_plate_block(
    *,
    backend: str,
    n_t: int,
    n_p: int,
    n_z: int,
    sites: Sequence[dict[str, Any]],
    rows: int,
    cols: int,
    z_home: int,
    z_step_um: float | None,
    bottom_to_top: bool | None,
    times_ms: Sequence[float],
    period_ms: float | None,
    exposure_ms: float | None,
    frame_shape: tuple[int, int, int],
) -> dict[str, Any]:
    """Build the ``nd2wsi.plate`` metadata block for the viewer."""
    return {
        "annotationScope": "plane" if backend in ("czi", "lif") else "site",
        "T": n_t,
        "P": n_p,
        "Z": n_z,
        "sites": [dict(s) for s in sites],
        "rows": rows,
        "cols": cols,
        "zHome": z_home,
        "zStepUm": z_step_um,
        "bottomToTop": bottom_to_top,
        "timesMs": list(times_ms),
        "periodMs": period_ms,
        "exposureMs": exposure_ms,
        "frameW": frame_shape[2],
        "frameH": frame_shape[1],
    }
