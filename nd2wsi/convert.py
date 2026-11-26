"""Build full or source-backed OME-Zarr pyramids.

A portable conversion stores level 0 and every reduced level. A compact
viewing cache stores levels 1 and smaller; the source ND2 supplies level 0 at
runtime. Both use the same bounded 2x2 box-mean pipeline.

Pyramid math is independent of physical storage. The current backend writes
OME-NGFF 0.4 metadata on local Zarr v2 for broad reader compatibility. Zarr
v2 chunk naming, padding, fill semantics, and codec I/O live in
:mod:`nd2wsi.storage.zarr_v2`, leaving a narrow seam for a future Zarr v3
backend.

Cache-generation, pyramid build/write/manifest, and chunk/tile conversion
helpers live in :mod:`nd2wsi.convert_cache`; this module remains the flat
public facade and monkeypatch boundary.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import numpy as np

from . import convert_cache as _convert_cache
from .contrast import auto_window
from .convert_cache import (  # noqa: F401
    CACHE_DIR_NAME,
    DOWNSAMPLE_TASK_BUDGET,
    NGFF_VERSION,
    _batch_cols,
    _create_level_array,
    _downsample_into,
    _overview_eligible,
    _store_direct,
    _StoreProgress,
    _write_level0,
    _write_level1_from_source,
    is_nd2wsi_store,
)
from .platform_io import (
    filesystem_path,
    windows_allocation_block_bytes,
    windows_available_memory_bytes,
)
from .reader import PlaneSelection, PlaneSource, level_shapes, nice_bytes, open_plane
from .storage import DEFAULT_STORAGE, StorageBackend

MAX_AUTO_WORKERS = 32  # guards pathological container CPU counts only

_NOTES_NZ_RE = re.compile(
    r"(?:file|series) has (\d+) Z planes|projection over (\d+) planes|LambdaScan: (\d+) spectral"
)


class SourceUnreadable(Exception):
    """Raised when cached semantics check finds an unreadable or corrupt source."""


def _hooks() -> _convert_cache._FacadeHooks:
    """Capture live module attributes so monkeypatches on nd2wsi.convert apply."""
    return _convert_cache._FacadeHooks(
        convert=convert,
        open_store=open_store,
        semantics=_store_matches_source_semantics,
        auto_tile=auto_tile,
        auto_workers=auto_workers,
        downsample=_downsample_into,
        sweep_appledouble=sweep_appledouble,
        block_bytes=_block_bytes,
        percentile_windows=_percentile_windows,
        build_group_attrs=build_group_attrs,
        legacy_store=_legacy_store,
        legacy_matches_source=_legacy_store_matches_source,
        store_matches_selection=_store_matches_selection,
        convert_is_default=(convert is _DEFAULT_CONVERT),
        available_memory_bytes=available_memory_bytes,
    )


# ---------------------------------------------------------------------------
# Hardware & volume sizing policies
# ---------------------------------------------------------------------------


def available_memory_bytes() -> int | None:
    """Memory a burst of workers may reasonably claim, or None if unknown.

    Linux publishes MemAvailable, which counts reclaimable page cache too.
    The bare free-page count would throttle a healthy machine whose RAM is
    rightly full of cache, so it is never used. Windows supplies available
    physical memory through GlobalMemoryStatusEx. macOS has no cheap
    equivalent; the memory ceiling simply does not apply there.
    """
    if os.name == "nt":
        try:
            return windows_available_memory_bytes()
        except OSError:
            return None
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def auto_workers(task_bytes: int | None = None) -> int:
    """Choose useful Dask parallelism without spending the machine's RAM.

    80% of the logical cores, as always. A container or workstation can
    expose far more cores than its memory can feed, so where available
    memory is known, half of it divided by the per-task budget sets a
    second ceiling, and an absolute cap covers the rest.
    """
    task_bytes = task_bytes or DOWNSAMPLE_TASK_BUDGET
    cpu_target = max(2, math.ceil((os.cpu_count() or 4) * 0.8))
    memory = available_memory_bytes()
    if memory is None:
        return min(cpu_target, MAX_AUTO_WORKERS)
    memory_target = max(2, int((memory * 0.5) // max(1, task_bytes)))
    return min(cpu_target, memory_target, MAX_AUTO_WORKERS)


def sweep_appledouble(folder: str | Path) -> tuple[int, int]:
    """Delete the ``._`` twins macOS leaves beside files on exFAT and NTFS.

    Every file written on such a volume gets a ``com.apple.provenance``
    extended attribute, and the attribute is stored in a sibling AppleDouble
    file. A pyramid is tens of thousands of chunks, so the twins double the
    file count, and on a volume with a 1 MB allocation block they double the
    size of the store while holding nothing anyone reads. Returns the number
    of files removed and the blocks they held.
    """
    folder = Path(folder)
    block = _block_bytes(folder)
    n = freed = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if not name.startswith("._"):
                continue
            path = os.path.join(root, name)
            try:
                entry = os.stat(path)
                allocated = getattr(entry, "st_blocks", None)
                size = (
                    (allocated * 512 or block)
                    if allocated is not None
                    else (max(1, math.ceil(entry.st_size / block)) * block)
                )
                os.unlink(path)
            except OSError:
                continue
            n += 1
            freed += size
    return n, freed


def _block_bytes(folder: str | Path) -> int:
    """Smallest number of bytes a file can occupy on this volume."""
    try:
        if os.name == "nt":
            return windows_allocation_block_bytes(folder)
        st = os.statvfs(str(folder))
        return max(512, int(st.f_frsize))
    except (AttributeError, OSError, ValueError):
        return 4096


def auto_tile(
    out_path: str | Path, storage: StorageBackend = DEFAULT_STORAGE
) -> int:
    """Ask the storage backend for a chunk edge suited to this volume.

    The current one-file-per-chunk Zarr v2 backend uses 1024 px chunks on
    large-allocation-block volumes and 512 px chunks elsewhere. A future
    sharded backend may make a different choice without changing callers.
    """
    path = Path(out_path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return storage.recommended_tile(allocation_block=_block_bytes(path))


# ---------------------------------------------------------------------------
# Display windows, OME-NGFF metadata & disk-size estimation
# ---------------------------------------------------------------------------


def _background_mode_start(
    channel: np.ndarray,
    end: float,
    fallback: float,
    *,
    bins: int = 256,
    right_peak_fraction: float = 0.70,
) -> float:
    """A fluorescence background peak, with a bright-background escape hatch.

    The modal bin is a useful black point when background occupies the low
    side of a fluorescence histogram. It is the wrong black point for a
    transmitted-light/BF channel, whose background peak normally sits near
    the white end. In that case keep the percentile fallback instead of
    collapsing the display window around the bright peak.

    Using edges[argmax] is intentional: datasets whose minimum is not zero
    must not lose the histogram origin by treating a bin index as a value.
    """
    data = np.asarray(channel).ravel()
    if np.issubdtype(data.dtype, np.floating):
        data = data[np.isfinite(data)]
    if not data.size or not np.isfinite(end) or not np.isfinite(fallback):
        return float(fallback)
    low = float(np.min(data))
    if end <= low or end <= fallback:
        return float(fallback)
    counts, edges = np.histogram(np.minimum(data, end), bins=bins, range=(low, end))
    mode = float(edges[int(np.argmax(counts))])
    if mode >= fallback + right_peak_fraction * (end - fallback):
        return float(fallback)
    return mode


def _percentile_windows(
    root: Any, level_paths: list[str], src: PlaneSource
) -> list[dict[str, float]]:
    """Per-channel display windows from the smallest pyramid level."""
    small = np.asarray(root[level_paths[-1]][:])
    info = np.iinfo(src.dtype) if np.issubdtype(src.dtype, np.integer) else None
    finite_small = (
        small[np.isfinite(small)]
        if np.issubdtype(small.dtype, np.floating)
        else small.ravel()
    )
    if info:
        lo_all, hi_all = float(info.min), float(info.max)
    elif finite_small.size:
        lo_all, hi_all = float(np.min(finite_small)), float(np.max(finite_small))
    else:
        lo_all, hi_all = 0.0, 1.0
    windows = []
    for ci in range(small.shape[0]):
        if src.rgb:
            if info:
                if (
                    src.dtype != np.dtype(np.uint8)
                    and src.bit_depth is not None
                    and 0 < src.bit_depth < np.dtype(src.dtype).itemsize * 8
                ):
                    start, end = (0.0, float((1 << src.bit_depth) - 1))
                else:
                    start, end = (
                        (0.0, float(info.max))
                        if src.dtype != np.dtype(np.uint8)
                        else (0.0, 255.0)
                    )
            else:
                start, end = (lo_all, hi_all if hi_all > lo_all else lo_all + 1.0)
        else:
            start, end = auto_window(small[ci], dtype=src.dtype)
        windows.append({"start": start, "end": end, "min": lo_all, "max": hi_all})
    return windows


def _hex_color(rgb: tuple[int, int, int]) -> str:
    return "{:02X}{:02X}{:02X}".format(*rgb)


def build_group_attrs(
    src: PlaneSource,
    shapes: list[tuple[int, int]],
    tile: int,
    windows: list[dict[str, float]],
    start_level: int = 0,
    ngff_version: str = NGFF_VERSION,
) -> dict[str, Any]:
    """``start_level`` names the first stored level: an overview store keeps
    levels 1..n and must say so rather than pretend to hold a level 0."""
    calibrated = src.pixel_size_um is not None
    py, px = src.pixel_size_um if calibrated else (1.0, 1.0)
    datasets = []
    for k, (h, w) in enumerate(shapes, start=start_level):
        factor = 2**k
        # uncalibrated files carry relative scales with no physical unit,
        # never an invented micrometer value
        datasets.append(
            {
                "path": str(k),
                "coordinateTransformations": [
                    {"type": "scale", "scale": [1.0, py * factor, px * factor]}
                ],
            }
        )
    multiscales = [
        {
            "version": ngff_version,
            "name": Path(src.source_name).name,
            "axes": [
                {"name": "c", "type": "channel"},
                {"name": "y", "type": "space", **({"unit": "micrometer"} if calibrated else {})},
                {"name": "x", "type": "space", **({"unit": "micrometer"} if calibrated else {})},
            ],
            "datasets": datasets,
            "type": "2x2 local mean",
            "metadata": {"description": "generated by nd2wsi"},
        }
    ]
    omero = {
        "channels": [
            {
                "label": ch.name,
                "color": _hex_color(ch.color),
                "active": True,
                "window": windows[i],
            }
            for i, ch in enumerate(src.channels)
        ],
        "rdefs": {"model": "color"},
    }
    levels = [
        {"path": str(k), "width": w, "height": h, "downsample": 2**k}
        for k, (h, w) in enumerate(shapes, start=start_level)
    ]
    nd2wsi = {
        "rgb": src.rgb,
        "source": Path(src.source_name).name,
        "pixel_size_um": [py, px] if calibrated else None,
        "calibration": {
            "status": "calibrated" if calibrated else "unknown",
            "source": src.calibration_source,
        },
        "dtype": str(src.dtype),
        "tile": tile,
        "levels": levels,
        "selection": src.selection,
        "notes": src.notes,
        "objective_magnification": src.magnification,
    }
    if src.channel_dtypes:
        nd2wsi["channel_dtypes"] = [str(dt) for dt in src.channel_dtypes]
    if src.czi_scene_t is not None:
        nd2wsi["czi_scene_t"] = [int(v) for v in src.czi_scene_t]
    return {"multiscales": multiscales, "omero": omero, "nd2wsi": nd2wsi}


def estimate_store_bytes(
    path: str | Path,
    *,
    tile: int | None = None,
    points: int = 16,
    storage: StorageBackend = DEFAULT_STORAGE,
) -> dict[str, Any]:
    """Predict what a slide's pyramid will take on disk, before building it.

    Decodes a spread of chunk-sized windows, compresses them with the store's
    own codec, and scales the measured ratio by the pixel count of every
    pyramid level. Reading a few dozen tiles costs well under a second, and
    the sample grid covers background as well as tissue, so the ratio is the
    slide's own rather than one hardcoded number.
    """
    from .czi import is_czi, open_czi
    from .lif import is_lif, open_lif
    from .sdpc import is_sdpc
    from .svs import is_svs, sample_planes

    path = Path(path)
    if is_sdpc(path):
        raise ValueError("SDPC is served directly and does not need a viewing cache")
    if tile is None:
        tile = auto_tile(default_store_path(path), storage)
    blocks: list[np.ndarray] = []
    if is_svs(path):
        import tifffile

        with tifffile.TiffFile(str(path)) as tf:
            series = tf.series[0]
            h, w = int(series.shape[0]), int(series.shape[1])
            c = min(3, int(series.shape[-1]))
            dtype = np.dtype(series.dtype)
        blocks = [b[:c] for b in sample_planes(path, tile=tile, points=points)]
    elif is_czi(path) or is_lif(path):
        opener = open_czi if is_czi(path) else open_lif
        with ExitStack() as stack:
            src = opener(stack, path, PlaneSelection(), tile=tile)
            c, h, w = src.shape
            dtype = src.dtype
            blocks = _convert_cache._sample_plane_blocks(src, tile, points)
    else:
        import nd2 as nd2lib

        with nd2lib.ND2File(str(path)) as f:
            src = open_plane(f, PlaneSelection(), tile=tile)
            c, h, w = src.shape
            dtype = src.dtype
            blocks = _convert_cache._sample_plane_blocks(src, tile, points)

    shapes = level_shapes(h, w, tile)
    codec = storage.compressor()
    sampled = sum(block.nbytes for block in blocks)
    packed = sum(
        len(codec.encode(np.ascontiguousarray(plane)))
        for block in blocks
        for plane in block
    )
    ratio = (packed / sampled) if sampled else 1.0
    zero_fraction = (
        sum(1 for block in blocks if not block.any()) / len(blocks) if blocks else 0.0
    )
    allocation_block = _block_bytes(path.parent)
    layout = storage.estimate_layout(
        shapes=shapes,
        channels=c,
        itemsize=dtype.itemsize,
        tile=tile,
        compression_ratio=ratio,
        allocation_block=allocation_block,
        zero_fraction=zero_fraction,
    )
    expected = max(layout["data_bytes"], layout["disk_bytes"])
    return {
        "bytes": expected,
        "human": nice_bytes(expected),
        **layout,
        "block_bytes": allocation_block,
        "ratio": ratio,
        "zero_fraction": zero_fraction,
        "width": w,
        "height": h,
        "channels": c,
        "levels": len(shapes),
        "source_bytes": path.stat().st_size,
        "free_bytes": shutil.disk_usage(path.parent).free,
        "storage": storage.descriptor(ngff_version=NGFF_VERSION),
    }


# ---------------------------------------------------------------------------
# Conversion & cache-generation facade entry points
# ---------------------------------------------------------------------------


def convert(
    nd2_path: str | Path,
    out_path: str | Path,
    *,
    tile: int | None = None,
    selection: PlaneSelection | None = None,
    overwrite: bool = False,
    progress: bool = True,
    workers: int | None = None,
    on_progress: Callable[[float], None] | None = None,
    overview: bool = False,
    storage: StorageBackend | None = None,
    ngff_version: str = NGFF_VERSION,
) -> Path:
    """Write a portable pyramid or a source-linked overview store.

    ``overview=False`` writes levels 0..n and produces a self-contained
    OME-Zarr store. ``overview=True`` writes levels 1..n only; an eligible
    uncompressed ND2 supplies level 0 at runtime.
    """
    return _convert_cache.run_convert(
        nd2_path,
        out_path,
        tile=tile,
        selection=selection,
        overwrite=overwrite,
        progress=progress,
        workers=workers,
        on_progress=on_progress,
        overview=overview,
        storage=storage,
        ngff_version=ngff_version,
        hooks=_hooks(),
    )


_DEFAULT_CONVERT = convert


def ensure_cache(
    slide: str | Path,
    selection: PlaneSelection | None = None,
    on_progress: Callable[[float], None] | None = None,
    tile: int | None = None,
    kind: str = "auto",
) -> Path:
    """The store to open for this slide and selection, building if needed.

    Every automatic open goes through here. Legacy full stores from before
    the container era are honored where they lie for the default selection.
    Otherwise the container's manifest decides: a match serves, a mismatch
    or damage is quarantined and rebuilt, and building itself is staged,
    locked against concurrent builders, fingerprint-checked against a
    source that might still be copying, and renamed into place complete.

    ``kind="auto"`` builds a compact overview store when the source can
    back its own level 0 and a full store otherwise; an existing cache of
    either kind is honored. ``kind="full"`` insists on a full store.
    """
    return _convert_cache.ensure_cache(
        slide, selection, on_progress=on_progress, tile=tile, kind=kind, hooks=_hooks()
    )


def existing_cache_store(
    slide: str | Path,
    selection: PlaneSelection | None = None,
    *,
    read_only: bool = False,
    tile: int | None = None,
) -> Path | None:
    """A valid, already-built store, or None. Never builds.

    Selection/Agent callers must use ``read_only=True``: historical broken
    directory stores are skipped without quarantine, repair or migration.
    """
    return _convert_cache.existing_cache_store(
        slide, selection, read_only=read_only, tile=tile, hooks=_hooks()
    )


def _legacy_store_matches_source(
    store: Path, slide: Path, attrs: dict[str, Any] | None = None
) -> bool:
    """Whether a stem-only store can be assigned to this source safely."""
    return _convert_cache._legacy_store_matches_source(store, slide, attrs, hooks=_hooks())


def _legacy_store(
    slide: Path,
    selection: PlaneSelection | None = None,
    *,
    read_only: bool = False,
    tile: int | None = None,
) -> Path | None:
    """A complete portable store found beside this source.

    The current suffix-aware name is checked first, followed by historical
    stem-only names. A directory that exists but does not open as a store is
    quarantined so it cannot wedge automatic opening. Stores the user opens
    directly never pass through here and are never touched.
    """
    return _convert_cache._legacy_store(
        slide, selection, read_only=read_only, tile=tile, hooks=_hooks()
    )


def default_store_path(
    nd2_path: str | Path, cache_dir: str | Path | None = None
) -> Path:
    """Default path for a complete, portable pyramid.

    New stores retain the source suffix (``slide.nd2.ome.zarr``), so an ND2
    and SVS with the same stem cannot collide. Existing stem-only stores remain
    valid and are reused where they lie.
    """
    return _convert_cache.default_store_path(nd2_path, cache_dir, hooks=_hooks())


def tidy_caches(folder: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Move scattered pyramid stores in ``folder`` into ``pyramids/``.

    Only directories this tool wrote are touched, and only when the target
    name is free. AppleDouble twins inside the collected stores go too.
    Returns ``{"moved": [(from, to)], "swept": count, "freed": bytes}``.
    """
    return _convert_cache.tidy_caches(folder, dry_run=dry_run, hooks=_hooks())


# ---------------------------------------------------------------------------
# Source-semantics validation & store opening
# ---------------------------------------------------------------------------


def _source_n_z(slide: Path, attrs: dict[str, Any], p: int = 0) -> int:
    notes = "\n".join(str(n) for n in (attrs.get("nd2wsi", {}).get("notes") or []))
    m = _NOTES_NZ_RE.search(notes)
    if m:
        for grp in m.groups():
            if grp is not None:
                return max(1, int(grp))
    if not slide.is_file():
        return 1
    suffix = slide.suffix.lower()
    try:
        if suffix == ".czi":
            from .czi import _level0_entries, _read_czi_header_and_index
            from .platform_io import BINARY

            fd = os.open(str(slide), os.O_RDONLY | BINARY)
            try:
                entries, _ = _read_czi_header_and_index(fd, slide.stat().st_size)
            finally:
                os.close(fd)
            lvl0 = _level0_entries(entries)
            s_vals = sorted({e.s for e in lvl0})
            if not s_vals:
                return 1
            s_pick = s_vals[p] if 0 <= p < len(s_vals) else s_vals[0]
            return max(1, len({e.z for e in lvl0 if e.s == s_pick}))
        if suffix == ".lif":
            from .lif import _list_lif_series, _read_lif_header_and_blocks
            from .platform_io import BINARY

            fd = os.open(str(slide), os.O_RDONLY | BINARY)
            try:
                root, blocks = _read_lif_header_and_blocks(fd, slide.stat().st_size)
            finally:
                os.close(fd)
            series = _list_lif_series(root, blocks)
            if 0 <= p < len(series):
                return max(1, int(series[p].n_z))
            return 1
        if suffix == ".nd2":
            import nd2

            with nd2.ND2File(str(slide)) as f:
                return max(1, int(f.sizes.get("Z", 1)))
    except Exception:
        return 1
    return 1


def _resolve_z_selection(z_val: Any, n_z: int) -> str | int | None:
    from .reader import _mid

    if z_val == "max":
        return "max" if n_z > 1 else 0
    if z_val in ("mid", None, ""):
        return _mid(n_z)
    if type(z_val) is int:
        return z_val
    if isinstance(z_val, str):
        try:
            return int(z_val)
        except ValueError:
            return None
    return None


def _px_matches(
    stored_px: Any, expected_px_tuple: tuple[float, float] | None
) -> bool:
    if expected_px_tuple is None:
        return stored_px is None
    expected_px = list(expected_px_tuple)
    return (
        isinstance(stored_px, list)
        and len(stored_px) == 2
        and bool(np.allclose(stored_px, expected_px, rtol=1e-6, atol=1e-9))
    )


def _requires_channel_dtypes(expected_dtypes: list[str]) -> bool:
    return len(set(expected_dtypes)) > 1 or any(
        "+" in dt or ":" in dt or dt in ("Bgr96Float", "Bgra32")
        for dt in expected_dtypes
    )


def _lif_semantics_match(
    slide: Path,
    selection: PlaneSelection,
    meta: dict[str, Any],
    omero_channels: list[Any],
    stored_wh: tuple[int, int] | None,
) -> bool:
    from .lif import _list_lif_series, _read_lif_header_and_blocks
    from .platform_io import BINARY

    fd = os.open(str(slide), os.O_RDONLY | BINARY)
    try:
        root_xml, blocks = _read_lif_header_and_blocks(fd, slide.stat().st_size)
    finally:
        os.close(fd)
    all_series = _list_lif_series(root_xml, blocks)
    p_idx = int(selection.p)
    if not (0 <= p_idx < len(all_series)):
        return False
    series = all_series[p_idx]
    if not (0 <= int(selection.t) < int(series.n_t)):
        return False
    if series.tile_positions_px and series.n_m > 1:
        exp_w = max(tx + series.width for tx, _ty in series.tile_positions_px)
        exp_h = max(ty + series.height for _tx, ty in series.tile_positions_px)
    else:
        exp_w, exp_h = int(series.width), int(series.height)
    if stored_wh is not None and stored_wh != (exp_w, exp_h):
        return False
    if (
        str(meta.get("dtype") or "") != str(series.dtype)
        or bool(meta.get("rgb")) != bool(series.rgb)
    ):
        return False
    expected_dtypes = [str(ch.dtype) for ch in series.channels]
    if _requires_channel_dtypes(expected_dtypes) and meta.get("channel_dtypes") != expected_dtypes:
        return False
    expected_labels = [ch.name for ch in series.channels]
    stored_labels = [str((c or {}).get("label") or "") for c in omero_channels]
    if stored_labels != expected_labels:
        return False
    if not _px_matches(meta.get("pixel_size_um"), series.pixel_size_um):
        return False
    exp_mag = getattr(
        series, "magnification", getattr(series, "objective_magnification", None)
    )
    stored_mag = meta.get("objective_magnification")
    if exp_mag is not None:
        if not isinstance(stored_mag, (int, float)) or not np.isclose(
            float(stored_mag), float(exp_mag), rtol=1e-6, atol=1e-9
        ):
            return False
    elif stored_mag is not None:
        return False
    return True


def _czi_semantics_match(
    slide: Path,
    selection: PlaneSelection,
    meta: dict[str, Any],
    omero_channels: list[Any],
    stored_wh: tuple[int, int] | None,
) -> bool:
    from .czi import (
        _extract_czi_metadata,
        _level0_entries,
        _read_czi_header_and_index,
        _resolve_czi_entries_spec,
    )
    from .platform_io import BINARY

    fd = os.open(str(slide), os.O_RDONLY | BINARY)
    try:
        entries, xml_root = _read_czi_header_and_index(fd, slide.stat().st_size)
    finally:
        os.close(fd)
    lvl0 = _level0_entries(entries)
    if not lvl0:
        return True
    s_vals = sorted({e.s for e in lvl0})
    p_idx = int(selection.p)
    t_idx = int(selection.t)
    if not (0 <= p_idx < len(s_vals)):
        return False
    s_pick = s_vals[p_idx]
    s_entries = [e for e in lvl0 if e.s == s_pick]
    t_vals = sorted({e.t for e in s_entries})
    if not (0 <= t_idx < len(t_vals)):
        return False
    t_pick = t_vals[t_idx]
    if "czi_scene_t" in meta:
        if meta.get("czi_scene_t") != [int(s_pick), int(t_pick)]:
            return False
    else:
        legacy_t = sorted({e.t for e in lvl0})
        if not (0 <= t_idx < len(legacy_t) and legacy_t[t_idx] == t_pick):
            return False
    st_entries = [e for e in s_entries if e.t == t_pick]
    if not st_entries:
        return False
    exp_w = max(e.x_start + e.x_size for e in st_entries) - min(
        e.x_start for e in st_entries
    )
    exp_h = max(e.y_start + e.y_size for e in st_entries) - min(
        e.y_start for e in st_entries
    )
    if stored_wh is not None and stored_wh != (exp_w, exp_h):
        return False
    expected_dtype, expected_rgb, expected_ch_dtypes = _resolve_czi_entries_spec(
        st_entries
    )
    if (
        str(meta.get("dtype") or "") != str(expected_dtype)
        or bool(meta.get("rgb")) != expected_rgb
    ):
        return False
    if (
        _requires_channel_dtypes(expected_ch_dtypes)
        and meta.get("channel_dtypes") != expected_ch_dtypes
    ):
        return False
    c_vals = sorted({e.c for e in st_entries})
    n_c = 3 if expected_rgb else max(1, len(c_vals))
    expected_px_tuple, _, _, expected_channels = _extract_czi_metadata(
        xml_root, n_c, expected_rgb, c_values=c_vals
    )
    if not _px_matches(meta.get("pixel_size_um"), expected_px_tuple):
        return False
    expected_pairs = [(ch.name, _hex_color(ch.color)) for ch in expected_channels]
    stored_pairs = [
        (
            str((c or {}).get("label") or ""),
            str((c or {}).get("color") or "").upper(),
        )
        for c in omero_channels
    ]
    return stored_pairs == expected_pairs


def _nd2_semantics_match(
    slide: Path,
    selection: PlaneSelection,
    meta: dict[str, Any],
    omero_channels: list[Any],
    stored_wh: tuple[int, int] | None,
) -> bool:
    import nd2

    if not nd2.is_supported_file(str(slide)):
        return True
    with nd2.ND2File(str(slide)) as f:
        sizes = dict(f.sizes)
        n_t = int(sizes.get("T", 1))
        n_p = int(sizes.get("P", 1))
        n_z = int(sizes.get("Z", 1))
        if not (0 <= int(selection.t) < n_t and 0 <= int(selection.p) < n_p):
            return False
        if selection.z not in ("mid", "max"):
            try:
                z_req = int(selection.z)
            except (TypeError, ValueError):
                return False
            if not (0 <= z_req < n_z):
                return False
        n_c = int(sizes.get("C", 1))
        n_s = int(sizes.get("S", 1))
        if n_c > 1 and n_s > 1:
            return False
        exp_rgb = n_s > 1
        exp_channels = n_s if exp_rgb else n_c
        exp_w = int(sizes.get("X", 0))
        exp_h = int(sizes.get("Y", 0))
        if stored_wh is not None and stored_wh != (exp_w, exp_h):
            return False
        if len(omero_channels) != exp_channels:
            return False
        if (
            str(meta.get("dtype") or "") != str(np.dtype(f.dtype))
            or bool(meta.get("rgb")) != exp_rgb
        ):
            return False
        from .reader import _channel_infos, _nd2_pixel_size

        exp_px, _ = _nd2_pixel_size(f)
        if not _px_matches(meta.get("pixel_size_um"), exp_px):
            return False
        exp_labels = [ch.name for ch in _channel_infos(f, exp_channels, exp_rgb)]
        stored_labels = [str((ch or {}).get("label") or "") for ch in omero_channels]
        return stored_labels == exp_labels


def _svs_semantics_match(
    slide: Path,
    meta: dict[str, Any],
    omero_channels: list[Any],
    stored_wh: tuple[int, int] | None,
    *,
    raise_unreadable: bool,
) -> bool:
    with open(slide, "rb") as fh:
        magic = fh.read(4)
    if magic not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return True
    import tifffile

    from .svs import _aperio_meta

    try:
        with tifffile.TiffFile(str(slide)) as tf:
            if not tf.series:
                return False
            series = tf.series[0]
            if series.ndim != 3 or series.shape[-1] not in (3, 4):
                return False
            exp_h, exp_w = int(series.shape[0]), int(series.shape[1])
            if stored_wh is not None and stored_wh != (exp_w, exp_h):
                return False
            if len(omero_channels) != 3:
                return False
            if (
                str(meta.get("dtype") or "") != str(np.dtype(series.dtype))
                or not bool(meta.get("rgb"))
            ):
                return False
            svs_meta = _aperio_meta(tf.pages[0].description or "")
            mpp = svs_meta.get("mpp")
            exp_px = (
                (float(mpp), float(mpp)) if (mpp is not None and mpp > 0) else None
            )
            return _px_matches(meta.get("pixel_size_um"), exp_px)
    except tifffile.TiffFileError as exc:
        if raise_unreadable:
            raise SourceUnreadable(str(exc)) from exc
        return False


def _store_matches_source_semantics(
    slide: Path,
    selection: PlaneSelection | None,
    attrs: dict[str, Any],
    *,
    raise_unreadable: bool = False,
) -> bool:
    if not slide.is_file():
        return True
    selection = selection or PlaneSelection()
    meta = attrs.get("nd2wsi") or {}
    omero_channels = list((attrs.get("omero") or {}).get("channels") or [])
    if meta.get("rgb"):
        try:
            dt = np.dtype(str(meta.get("dtype") or "uint8"))
        except Exception:
            dt = np.dtype(np.uint8)
        if np.issubdtype(dt, np.integer) and dt != np.dtype(np.uint8):
            hi = float(np.iinfo(dt).max)
            for ch in omero_channels:
                win = (ch or {}).get("window") or {}
                end = win.get("end")
                if isinstance(end, (int, float)) and float(end) <= 255.0 < hi:
                    return False

    ov = meta.get("overview_of")
    if isinstance(ov, dict):
        stored_wh: tuple[int, int] | None = (
            int(ov.get("width") or 0),
            int(ov.get("height") or 0),
        )
    elif meta.get("levels"):
        lv0 = meta["levels"][0]
        stored_wh = (int(lv0.get("width") or 0), int(lv0.get("height") or 0))
    else:
        stored_wh = None

    suffix = slide.suffix.lower()
    try:
        if suffix == ".lif":
            return _lif_semantics_match(slide, selection, meta, omero_channels, stored_wh)
        if suffix == ".czi":
            return _czi_semantics_match(slide, selection, meta, omero_channels, stored_wh)
        if suffix == ".nd2":
            return _nd2_semantics_match(slide, selection, meta, omero_channels, stored_wh)
        if suffix == ".svs":
            return _svs_semantics_match(
                slide, meta, omero_channels, stored_wh, raise_unreadable=raise_unreadable
            )
    except OSError:
        return True
    except (
        ValueError,
        NotImplementedError,
        ET.ParseError,
        struct.error,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
        ZeroDivisionError,
        OverflowError,
        EOFError,
        zlib.error,
    ) as exc:
        if raise_unreadable:
            raise SourceUnreadable(f"{type(exc).__name__}: {exc}") from exc
        return False
    return True


def _store_matches_selection(
    slide: Path,
    attrs: dict[str, Any],
    requested: PlaneSelection | None = None,
    *,
    tile: int | None = None,
    require_provenance: bool = False,
) -> bool:
    from .cache import fingerprints_match, names_match, quick_fingerprint

    requested = requested or PlaneSelection()
    meta = attrs.get("nd2wsi") or {}
    recorded_source = Path(str(meta.get("source") or "")).name
    if not names_match(recorded_source, slide.name):
        return False
    recorded_fp = meta.get("source_fingerprint")
    if isinstance(recorded_fp, dict) and slide.is_file():
        if not fingerprints_match(recorded_fp, quick_fingerprint(slide)):
            return False
    elif require_provenance:
        return False
    if tile is not None and int(meta.get("tile") or 0) != int(tile):
        return False
    sel = meta.get("selection") or {}
    try:
        sel_t = int(sel.get("t", 0) or 0)
        sel_p = int(sel.get("p", 0) or 0)
    except (TypeError, ValueError):
        return False
    if sel_t != int(requested.t) or sel_p != int(requested.p):
        return False
    n_z = _source_n_z(slide, attrs, p=int(requested.p))
    resolved_store_z = _resolve_z_selection(sel.get("z", 0), n_z)
    resolved_req_z = _resolve_z_selection(requested.z, n_z)
    if resolved_store_z is None or resolved_store_z != resolved_req_z:
        return False
    return _store_matches_source_semantics(slide, requested, attrs)


def open_store(path: str | Path) -> tuple[Any, dict[str, Any]]:
    """Open a converted store, returning (zarr group, attrs dict)."""
    import zarr

    from .storage.zarr_v2 import validate_zarr_v2_directory

    if Path(path).is_file():
        from .storage.single_file import open_zarr_group

        root = _ClosableGroup(open_zarr_group(path, prefix="store.ome.zarr", mode="r"))
    else:
        store_dir = Path(filesystem_path(path))
        validate_zarr_v2_directory(store_dir)
        root = zarr.open_group(store_dir, mode="r")
    try:
        attrs = json.loads(json.dumps(dict(root.attrs)))
        if "nd2wsi" not in attrs or "multiscales" not in attrs:
            raise ValueError(
                f"{path} does not look like an nd2wsi store "
                "(missing multiscales/nd2wsi metadata)"
            )
    except BaseException:
        if hasattr(root, "close"):
            root.close()
        raise
    return root, attrs


class _ClosableGroup:
    """A Zarr group with an explicit lifetime for its single-file connection.

    Members are looked up once per open root. A Zarr lookup re-reads the
    member's metadata from SQLite on every call, and every tile read starts
    with one. The group is read-only and a replaced pyramid always arrives
    as a new root, so the memo cannot go stale; ``close()`` drops it
    together with the connection.
    """

    def __init__(self, group: Any):
        self._group = group
        self._members: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._group, name)

    def __getitem__(self, key: str) -> Any:
        try:
            return self._members[key]
        except KeyError:
            pass
        # concurrent first lookups may both build a member; keep one of them
        return self._members.setdefault(key, self._group[key])

    def close(self, **_: Any) -> None:
        self._members.clear()
        self._group.store.close()
