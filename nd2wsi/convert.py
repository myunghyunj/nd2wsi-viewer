"""Build full or source-backed OME-Zarr pyramids.

A portable conversion stores level 0 and every reduced level. A compact
viewing cache stores levels 1 and smaller; the source ND2 supplies level 0 at
runtime. Both use the same bounded 2x2 box-mean pipeline.

Pyramid math is independent of physical storage. The current backend writes
OME-NGFF 0.4 metadata on local Zarr v2 for broad reader compatibility. Zarr
v2 chunk naming, padding, fill semantics, and codec I/O live in
:mod:`nd2wsi.storage.zarr_v2`, leaving a narrow seam for a future Zarr v3
backend.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from .reader import PlaneSelection, PlaneSource, level_shapes, nice_bytes, open_plane
from .storage import DEFAULT_STORAGE, StorageBackend

NGFF_VERSION = "0.4"


class _StoreProgress:
    """Dask callback that reports one store() call's completion fraction."""

    def __new__(cls, cb):
        from dask.callbacks import Callback

        class _CB(Callback):
            def _start_state(self, dsk, state):
                self._done = 0
                self._total = max(1, len(state["ready"]) + len(state["waiting"]))

            def _posttask(self, key, result, dsk, state, worker_id):
                self._done += 1
                cb(min(1.0, self._done / self._total))

        return _CB()


MAX_AUTO_WORKERS = 32  # guards pathological container CPU counts only


def available_memory_bytes() -> int | None:
    """Memory a burst of workers may reasonably claim, or None if unknown.

    Linux publishes MemAvailable, which counts reclaimable page cache too.
    The bare free-page count would throttle a healthy machine whose RAM is
    rightly full of cache, so it is never used. macOS has no cheap
    equivalent; the memory ceiling simply does not apply there.
    """
    try:
        with open("/proc/meminfo") as fh:
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


def store_compressor() -> Any:
    """Codec used by the default persistent storage backend."""
    return DEFAULT_STORAGE.compressor()


def _create_level_array(
    root: Any,
    path: str,
    shape: tuple[int, ...],
    dtype: np.dtype,
    tile: int,
    storage: StorageBackend = DEFAULT_STORAGE,
) -> Any:
    return storage.create_array(root, path, shape, dtype, tile)


def _store_direct(
    data: Any,
    array: Any,
    storage: StorageBackend = DEFAULT_STORAGE,
    *,
    allow_rechunk: bool = True,
) -> None:
    storage.store_dask(data, array, allow_rechunk=allow_rechunk)


def _write_level0(
    src: PlaneSource, arr: Any, storage: StorageBackend = DEFAULT_STORAGE
) -> None:
    _store_direct(src.data, arr, storage)


def _write_level1_from_source(
    src: PlaneSource, arr: Any, storage: StorageBackend = DEFAULT_STORAGE
) -> None:
    """Level 1 straight from the source, skipping a stored level 0.

    Byte-identical to downsampling a stored level 0 (``box-mean-floor-v1``):
    a 2x2 mean of four uint8/uint16 values is exact in float32, and the
    same rint lands on the same integer. Floor semantics drop an odd last
    row or column, exactly as :func:`level_shapes` floors.
    """
    import dask.array as da

    _, h, w = src.shape
    _, out_tile, _ = storage.chunk_array(arr).chunk_shape
    data = src.data[:, : h // 2 * 2, : w // 2 * 2]
    # Merge four aligned source tiles before reducing them. The 2x2 mean then
    # emits blocks on the target grid, so the writer never has to reshuffle
    # half-tile output blocks into full store chunks.
    data = data.rechunk((1, out_tile * 2, out_tile * 2))
    mean = da.coarsen(np.mean, data.astype(np.float32), {0: 1, 1: 2, 2: 2})
    if np.issubdtype(src.dtype, np.integer):
        mean = da.rint(mean)
    _store_direct(
        mean.astype(src.dtype),
        arr,
        storage,
        allow_rechunk=False,
    )


DOWNSAMPLE_TASK_BUDGET = 96 << 20  # peak bytes one downsample task may hold


def _batch_cols(nt: int, itemsize: int) -> int:
    """Output columns one task may span, from the task memory budget.

    Per output column a task holds two source rows of chunks, a float32
    mean and the output row: ``2*nt*2*itemsize + nt*4 + nt*itemsize``
    bytes. The batch is a whole number of chunks and at least one.
    """
    per_col = 2 * nt * 2 * itemsize + nt * 4 + nt * itemsize
    cols = max(nt, int(DOWNSAMPLE_TASK_BUDGET / per_col) // nt * nt)
    return cols


def _downsample_into(
    prev_arr: Any,
    next_arr: Any,
    tile: int,
    dtype: np.dtype,
    storage: StorageBackend = DEFAULT_STORAGE,
) -> None:
    """2x2-mean one pyramid level into the next, in bounded batches.

    Each task decodes the source chunk files under its footprint, means
    them (float32 is exact for 2x2 means of uint8/uint16), and writes the
    encoded target chunks -- direct chunk I/O end to end. A task spans one
    target chunk row by a column batch sized from a fixed memory budget,
    so peak bytes per task depend on the tile size, never the slide width.
    """
    import dask

    previous = storage.chunk_array(prev_arr)
    target = storage.chunk_array(next_arr)
    pcs = previous.chunk_shape
    ncs = target.chunk_shape
    pdtype = np.dtype(prev_arr.dtype)
    c, ph, pw = prev_arr.shape
    _, nh, nw = next_arr.shape
    _, pt, _ = pcs
    _, nt, _ = ncs
    integer = np.issubdtype(dtype, np.integer)
    bw = _batch_cols(nt, pdtype.itemsize)

    def one_batch(ci: int, ty: int, x0: int, x1: int) -> None:
        y0, y1 = ty * nt, min((ty + 1) * nt, nh)
        sy0, sy1 = 2 * y0, 2 * y1
        sx0, sx1 = 2 * x0, 2 * x1
        src = np.zeros((sy1 - sy0, sx1 - sx0), pdtype)
        # An axis that has already collapsed to one pixel has no second row
        # or column to average with. Read only what the source holds, then
        # repeat its edge, so the level carries the real value forward
        # instead of averaging it with the padding zarr keeps in edge chunks.
        ry, rx = min(sy1, ph), min(sx1, pw)
        for cy in range(sy0 // pt, -(-ry // pt)):
            for cx in range(sx0 // pt, -(-rx // pt)):
                chunk = previous.read_or_none((ci, cy, cx))
                if chunk is None:
                    continue  # an omitted chunk is all zeros, as src already is
                full = chunk[0]
                oy0, oy1 = max(sy0, cy * pt), min(ry, cy * pt + pt)
                ox0, ox1 = max(sx0, cx * pt), min(rx, cx * pt + pt)
                src[oy0 - sy0 : oy1 - sy0, ox0 - sx0 : ox1 - sx0] = full[
                    oy0 - cy * pt : oy1 - cy * pt, ox0 - cx * pt : ox1 - cx * pt
                ]
        if ry < sy1:
            src[ry - sy0 :] = src[ry - sy0 - 1 : ry - sy0]
        if rx < sx1:
            src[:, rx - sx0 :] = src[:, rx - sx0 - 1 : rx - sx0]
        h2, w2 = y1 - y0, x1 - x0
        mean = src.reshape(h2, 2, w2, 2).mean(axis=(1, 3), dtype=np.float32)
        row = np.rint(mean).astype(dtype) if integer else mean.astype(dtype)
        for tx in range(x0 // nt, -(-x1 // nt)):
            cx0, cx1 = tx * nt - x0, min((tx + 1) * nt, nw) - x0
            if not row[:, cx0:cx1].any():  # all-zero chunk stays unwritten
                continue
            out = np.zeros(ncs, dtype)  # v2 pads edge chunks to full size
            out[0, :h2, : cx1 - cx0] = row[:, cx0:cx1]
            target.write((ci, ty, tx), out)

    tasks = [
        dask.delayed(one_batch)(ci, ty, x0, min(x0 + bw, nw))
        for ci in range(c)
        for ty in range(-(-nh // nt))
        for x0 in range(0, nw, bw)
    ]
    dask.compute(*tasks)


def _percentile_windows(
    root: Any, level_paths: list[str], src: PlaneSource
) -> list[dict[str, float]]:
    """Per-channel display windows from the smallest pyramid level."""
    small = np.asarray(root[level_paths[-1]][:])
    info = np.iinfo(src.dtype) if np.issubdtype(src.dtype, np.integer) else None
    lo_all = float(info.min) if info else float(np.nanmin(small))
    hi_all = float(info.max) if info else float(np.nanmax(small))
    windows = []
    for ci in range(small.shape[0]):
        ch = small[ci]
        if src.rgb:
            start, end = 0.0, 255.0
        else:
            start = float(np.percentile(ch, 1.0))
            end = float(np.percentile(ch, 99.8))
            if end <= start:
                end = start + 1.0
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
    return {"multiscales": multiscales, "omero": omero, "nd2wsi": nd2wsi}


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
    import uuid
    from contextlib import ExitStack

    import dask
    import nd2

    from .cache import commit_container
    from .svs import is_svs, open_svs

    source_path = Path(nd2_path)
    final_path = Path(out_path)
    storage = storage or DEFAULT_STORAGE
    if tile is None:
        tile = auto_tile(final_path, storage)
        if progress and tile != 512:
            print(f"tile {tile} for this volume's allocation blocks")
    selection = selection or PlaneSelection()
    if workers is None:
        workers = auto_workers()
        if progress:
            print(f"hardware: {os.cpu_count()} CPU cores -> {workers} worker threads")

    if final_path.exists() and not overwrite:
        raise FileExistsError(f"{final_path} exists (use --overwrite)")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = final_path.parent / (
        f".{final_path.name}.building-{uuid.uuid4().hex[:12]}"
    )
    shutil.rmtree(staging_path, ignore_errors=True)

    started = time.time()
    swept = freed = 0
    try:
        with dask.config.set(scheduler="threads", num_workers=workers):
            with ExitStack() as stack:
                if is_svs(source_path):
                    if overview:
                        raise ValueError("overview stores are for ND2 sources only")
                    src = open_svs(stack, source_path, tile=tile)
                else:
                    handle = stack.enter_context(nd2.ND2File(str(source_path)))
                    # Coarsening a 2*tile source block yields one tile-sized
                    # level-1 block. This alignment prevents the hidden Dask
                    # rechunk that made early compact-cache prototypes slower
                    # than full conversion.
                    source_tile = tile * 2 if overview else tile
                    src = open_plane(handle, selection, tile=source_tile)

                channels, height, width = src.shape
                shapes = level_shapes(height, width, tile)
                first_level = 1 if overview else 0
                if overview and len(shapes) < 2:
                    raise ValueError(
                        f"{source_path.name} fits one tile; an overview would be empty"
                    )

                if progress:
                    raw = channels * height * width * src.dtype.itemsize
                    print(
                        f"{source_path.name}: {width} x {height} px, "
                        f"{channels} channel(s), {src.dtype}, "
                        f"{'RGB' if src.rgb else 'grayscale'}, "
                        f"~{nice_bytes(raw)} of level-0 pixels"
                    )
                    for note in src.notes:
                        print(f"  note: {note}")
                    label = "overview levels 1.." if overview else "levels 0.."
                    print(f"  pyramid: {label}{len(shapes) - 1}, tile {tile}")

                root = storage.create_group(staging_path)
                arrays: dict[int, Any] = {}
                for level in range(first_level, len(shapes)):
                    level_height, level_width = shapes[level]
                    arrays[level] = _create_level_array(
                        root,
                        str(level),
                        (channels, level_height, level_width),
                        src.dtype,
                        tile,
                        storage,
                    )

                weights = {
                    level: shapes[level][0] * shapes[level][1]
                    for level in arrays
                }
                total_pixels = float(sum(weights.values()))
                completed_pixels = 0.0

                def level_context(level: int):
                    if on_progress is None:
                        import contextlib

                        return contextlib.nullcontext()
                    base = completed_pixels
                    return _StoreProgress(
                        lambda fraction: on_progress(
                            (base + fraction * weights[level]) / total_pixels
                        )
                    )

                level_height, level_width = shapes[first_level]
                if progress:
                    action = (
                        "downsampling from the source"
                        if overview
                        else "writing from the source"
                    )
                    print(
                        f"  level {first_level}  {level_width} x {level_height}  "
                        f"{action} ...",
                        flush=True,
                    )
                with level_context(first_level):
                    if overview:
                        _write_level1_from_source(src, arrays[1], storage)
                    else:
                        _write_level0(src, arrays[0], storage)
                completed_pixels += weights[first_level]

                for level in range(first_level + 1, len(shapes)):
                    level_height, level_width = shapes[level]
                    if progress:
                        print(
                            f"  level {level}  {level_width} x {level_height}  "
                            "downsampling ...",
                            flush=True,
                        )
                    with level_context(level):
                        _downsample_into(
                            arrays[level - 1],
                            arrays[level],
                            tile,
                            src.dtype,
                            storage,
                        )
                    completed_pixels += weights[level]

                level_paths = [
                    str(level) for level in range(first_level, len(shapes))
                ]
                windows = _percentile_windows(root, level_paths, src)
                attrs = build_group_attrs(
                    src,
                    shapes[first_level:],
                    tile,
                    windows,
                    start_level=first_level,
                    ngff_version=ngff_version,
                )
                attrs["nd2wsi"]["storage"] = storage.descriptor(
                    ngff_version=ngff_version
                )
                if overview:
                    attrs["nd2wsi"]["kind"] = "overview"
                    attrs["nd2wsi"]["overview_of"] = {
                        "channels": channels,
                        "height": height,
                        "width": width,
                    }
                    attrs["nd2wsi"]["notes"] = attrs["nd2wsi"].get(
                        "notes", []
                    ) + [
                        "overview store: full resolution is read from the source file"
                    ]
                root.attrs.update(attrs)
                if on_progress is not None:
                    on_progress(1.0)

        swept, freed = sweep_appledouble(staging_path)
        if final_path.exists() and not overwrite:
            raise FileExistsError(f"{final_path} exists (use --overwrite)")
        commit_container(staging_path, final_path)
    except BaseException:
        shutil.rmtree(staging_path, ignore_errors=True)
        raise

    if progress:
        size = sum(path.stat().st_size for path in final_path.rglob("*") if path.is_file())
        print(
            f"  done in {time.time() - started:.1f}s -> {final_path} "
            f"({nice_bytes(size)} on disk)"
        )
        if swept:
            print(f"  swept {swept} AppleDouble files, {nice_bytes(freed)} recovered")
    return final_path


def sweep_appledouble(folder: str | Path) -> tuple[int, int]:
    """Delete the ``._`` twins macOS leaves beside files on exFAT and NTFS.

    Every file written on such a volume gets a ``com.apple.provenance``
    extended attribute, and the attribute is stored in a sibling AppleDouble
    file. A pyramid is tens of thousands of chunks, so the twins double the
    file count, and on a volume with a 1 MB allocation block they double the
    size of the store while holding nothing anyone reads. Returns the number
    of files removed and the blocks they held.
    """
    import os

    folder = Path(folder)
    block = _block_bytes(folder)
    n = freed = 0
    for root, dirs, files in os.walk(folder):
        for name in files:
            if not name.startswith("._"):
                continue
            path = os.path.join(root, name)
            try:
                size = os.stat(path).st_blocks * 512 or block
                os.unlink(path)
            except OSError:
                continue
            n += 1
            freed += size
    return n, freed


def _block_bytes(folder: str | Path) -> int:
    """Smallest number of bytes a file can occupy on this volume."""
    import os

    try:
        st = os.statvfs(str(folder))
        return max(512, int(st.f_frsize))
    except (OSError, ValueError):
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
    from .reader import PlaneSelection, level_shapes, nice_bytes, open_plane
    from .svs import is_svs, sample_planes

    path = Path(path)
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
    else:
        import nd2 as nd2lib

        with nd2lib.ND2File(str(path)) as f:
            src = open_plane(f, PlaneSelection(), tile=tile)
            c, h, w = src.shape
            dtype = src.dtype
            side = max(1, int(round(points**0.5)))
            for i in range(side):
                for j in range(side):
                    y = int((i + 0.5) / side * max(1, h - tile))
                    x = int((j + 0.5) / side * max(1, w - tile))
                    block = src.data[:, y : y + tile, x : x + tile]
                    blocks.append(np.asarray(block.compute()))

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
        sum(1 for block in blocks if not block.any()) / len(blocks)
        if blocks
        else 0.0
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


CACHE_DIR_NAME = "pyramids"


def _overview_eligible(slide: Path, selection: PlaneSelection, tile: int) -> bool:
    """Should this slide's cache be an overview backed by the source?

    Only when the file can truly serve as its own level 0 — checked on the
    open file, not assumed — and the image is big enough to have stored
    levels at all. Anything else gets the full store it always did.
    """
    if slide.suffix.lower() != ".nd2":
        return False
    try:
        import nd2

        from .reader import is_source_backable

        with nd2.ND2File(str(slide)) as f:
            ok, _ = is_source_backable(f, selection)
            if not ok:
                return False
            h = int(f.sizes.get("Y", 0))
            w = int(f.sizes.get("X", 0))
        return max(h, w) > tile
    except Exception:
        return False  # anything odd falls back to the proven full path


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
    from .cache import (
        CacheLock,
        cache_container,
        cache_matches,
        commit_container,
        container_store,
        quarantine,
        quick_fingerprint,
        sweep_stale_builds,
        write_manifest,
    )

    slide = Path(slide).resolve()
    selection = selection or PlaneSelection()
    want = None if kind == "auto" else kind

    container = cache_container(slide, selection)
    store = container_store(container)
    # the common case takes no lock: a valid container serves immediately,
    # and it outranks legacy stores so an old pyramids dir cannot shadow a
    # correct managed cache
    if cache_matches(container, slide, selection, kind=want) and store.is_dir():
        try:
            open_store(store)
            return store
        except ValueError:
            pass  # damaged despite its manifest: handled under the lock

    # Versions 0.8 and 0.9 used the stem alone. Reuse a valid old container,
    # but never let it become the identity of a new cache: ``slide.nd2`` and
    # ``slide.svs`` may live together.
    from .cache import legacy_cache_container

    old_container = legacy_cache_container(slide, selection)
    old_store = container_store(old_container)
    if old_container != container and cache_matches(
        old_container, slide, selection, kind=want
    ) and old_store.is_dir():
        try:
            open_store(old_store)
            return old_store
        except ValueError:
            pass  # leave it for explicit cleanup; build the new identity

    if selection == PlaneSelection():
        legacy = _legacy_store(slide)
        if legacy is not None:
            return legacy

    with CacheLock(container):
        # someone else may have finished it while this process waited, and
        # quarantining under the lock means two openers cannot both grab
        # the same stale container
        if cache_matches(container, slide, selection, kind=want) and store.is_dir():
            try:
                open_store(store)
                return store
            except ValueError:
                pass
        if container.exists():
            quarantine(container)
        sweep_stale_builds(container.parent)
        overview = kind == "auto" and _overview_eligible(
            slide, selection, tile or auto_tile(container)
        )
        before = quick_fingerprint(slide)
        staging = container.with_name(f"{container.name}.building-{os.getpid()}")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            convert(
                slide,
                staging / store.name,
                tile=tile,
                selection=selection,
                progress=False,
                on_progress=on_progress,
                overview=overview,
            )
            after = quick_fingerprint(slide)
            if before != after:
                raise RuntimeError(
                    f"{slide.name} changed while its cache was building "
                    "(still copying, or being written by the scanner?) — "
                    "try again once the file is settled"
                )
            _, attrs = open_store(staging / store.name)
            meta = attrs["nd2wsi"]
            ov = meta.get("overview_of")
            if ov:
                shape = (ov["channels"], ov["height"], ov["width"])
            else:
                lv0 = meta["levels"][0]
                shape = (len(attrs["omero"]["channels"]), lv0["height"], lv0["width"])
            write_manifest(
                staging,
                slide,
                after,
                selection.describe(),
                meta.get("selection", {}).get("z"),
                meta["tile"],
                shape,
                meta["dtype"],
                kind="overview" if ov else "full",
                storage=meta.get("storage"),
            )
            commit_container(staging, container)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return store


def existing_cache_store(
    slide: str | Path, selection: PlaneSelection | None = None
) -> Path | None:
    """A valid, already-built store for this slide, or None. Never builds."""
    from .cache import cache_container, cache_matches, container_store

    slide = Path(slide).resolve()
    selection = selection or PlaneSelection()
    if selection == PlaneSelection():
        legacy = _legacy_store(slide)
        if legacy is not None:
            return legacy
    container = cache_container(slide, selection)
    store = container_store(container)
    if cache_matches(container, slide, selection) and store.is_dir():
        return store
    from .cache import legacy_cache_container

    old_container = legacy_cache_container(slide, selection)
    old_store = container_store(old_container)
    if old_container != container and cache_matches(
        old_container, slide, selection
    ) and old_store.is_dir():
        return old_store
    return None


def _legacy_store_matches_source(
    store: Path, slide: Path, attrs: dict[str, Any] | None = None
) -> bool:
    """Whether a stem-only store can be assigned to this source safely."""
    try:
        if attrs is None:
            _, attrs = open_store(store)
    except (ValueError, FileNotFoundError, OSError):
        # an unopenable legacy dir still claims its name: fall through to
        # the sibling check so a unique source reuses (and can repair) it
        # in place instead of stranding it beside a fresh rebuild
        attrs = None
    recorded = ""
    if attrs is not None:
        recorded = Path(str(attrs.get("nd2wsi", {}).get("source") or "")).name
    if recorded:
        return recorded == slide.name
    try:
        siblings = [
            item
            for item in slide.parent.iterdir()
            if item.is_file()
            and item.stem == slide.stem
            and item.suffix.lower() in (".nd2", ".svs")
        ]
    except OSError:
        return False
    return len(siblings) == 1


def _legacy_store(slide: Path) -> Path | None:
    """A complete portable store found beside this source.

    The current suffix-aware name is checked first, followed by historical
    stem-only names. A directory that exists but does not open as a store is
    quarantined so it cannot wedge automatic opening. Stores the user opens
    directly never pass through here and are never touched.
    """
    from .cache import quarantine

    base = slide.parent
    for cand in (
        base / CACHE_DIR_NAME / f"{slide.name}.ome.zarr",
        base / CACHE_DIR_NAME / f"{slide.stem}.ome.zarr",
        base / f"pyramid_{slide.stem}.ome.zarr",
        base / f"{slide.stem}.ome.zarr",
    ):
        if not cand.exists():
            continue
        try:
            _, attrs = open_store(cand)
        except (ValueError, FileNotFoundError):
            # 0.8 converts atomically, so a store that exists but does not
            # open is a wedge from an older version, never work in progress
            try:
                quarantine(cand)
            except FileNotFoundError:
                pass  # another opener quarantined it first
            continue
        if not _legacy_store_matches_source(cand, slide, attrs):
            continue
        sel = attrs["nd2wsi"].get("selection") or {}
        if sel.get("t", 0) or sel.get("p", 0):
            # built for another timepoint or position: not the default view
            continue
        return cand
    return None


def default_store_path(nd2_path: str | Path, cache_dir: str | Path | None = None) -> Path:
    """Default path for a complete, portable pyramid.

    New stores retain the source suffix (``slide.nd2.ome.zarr``), so an ND2
    and SVS with the same stem cannot collide. Existing stem-only stores remain
    valid and are reused where they lie.
    """
    nd2_path = Path(nd2_path)
    base = Path(cache_dir) if cache_dir else nd2_path.parent
    for legacy in (
        base / CACHE_DIR_NAME / f"{nd2_path.stem}.ome.zarr",
        base / f"pyramid_{nd2_path.stem}.ome.zarr",
        base / f"{nd2_path.stem}.ome.zarr",
    ):
        if legacy.exists() and _legacy_store_matches_source(legacy, nd2_path):
            return legacy
    return base / CACHE_DIR_NAME / f"{nd2_path.name}.ome.zarr"


def is_nd2wsi_store(path: str | Path) -> bool:
    """True for a directory this tool wrote, cheaply and without opening it."""
    attrs = Path(path) / ".zattrs"
    try:
        return '"nd2wsi"' in attrs.read_text()
    except OSError:
        return False


def tidy_caches(folder: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Move scattered pyramid stores in ``folder`` into ``pyramids/``.

    Only directories this tool wrote are touched, and only when the target
    name is free. AppleDouble twins inside the collected stores go too.
    Returns ``{"moved": [(from, to)], "swept": count, "freed": bytes}``.
    """
    folder = Path(folder)
    dest = folder / CACHE_DIR_NAME
    moved = []
    swept = freed = 0
    if not dry_run and dest.is_dir():
        n, b = sweep_appledouble(dest)
        swept += n
        freed += b
    for item in sorted(folder.iterdir()):
        if not item.is_dir() or not item.name.endswith(".ome.zarr"):
            continue
        if item.name.startswith("._") or not is_nd2wsi_store(item):
            continue
        stem = item.name[: -len(".ome.zarr")]
        if stem.startswith("pyramid_"):
            stem = stem[len("pyramid_") :]
        target = dest / f"{stem}.ome.zarr"
        if target.exists():
            continue
        moved.append((item, target))
        if not dry_run:
            dest.mkdir(exist_ok=True)
            item.rename(target)
            n, b = sweep_appledouble(target)
            swept += n
            freed += b
            # exFAT and NTFS carry Finder metadata in a sibling AppleDouble
            # file, which is dead weight once its item has moved away.
            sidecar = item.parent / f"._{item.name}"
            if sidecar.exists():
                sidecar.unlink()
                swept += 1
                freed += _block_bytes(folder)
    return {"moved": moved, "swept": swept, "freed": freed}


def open_store(path: str | Path) -> tuple[Any, dict[str, Any]]:
    """Open a converted store, returning (zarr group, attrs dict)."""
    import zarr

    root = zarr.open_group(str(path), mode="r")
    attrs = json.loads(json.dumps(dict(root.attrs)))
    if "nd2wsi" not in attrs or "multiscales" not in attrs:
        raise ValueError(
            f"{path} does not look like an nd2wsi store "
            "(missing multiscales/nd2wsi metadata)"
        )
    return root, attrs
