"""Cache-generation, pyramid build/write/manifest, and chunk/tile conversion helpers.

This sibling module backs the flat :mod:`nd2wsi.convert` facade so that
callers and tests can keep importing and monkeypatching ``nd2wsi.convert``
without circular imports.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .reader import PlaneSelection, PlaneSource, level_shapes, nice_bytes, open_plane
from .storage import DEFAULT_STORAGE, StorageBackend

NGFF_VERSION = "0.4"
DOWNSAMPLE_TASK_BUDGET = 96 << 20  # peak bytes one downsample task may hold
CACHE_DIR_NAME = "pyramids"


@dataclass(frozen=True)
class _FacadeHooks:
    """Live callables resolved from :mod:`nd2wsi.convert` at invocation time."""

    convert: Callable[..., Path]
    open_store: Callable[[str | Path], tuple[Any, dict[str, Any]]]
    semantics: Callable[..., bool]
    auto_tile: Callable[..., int]
    auto_workers: Callable[[], int]
    downsample: Callable[..., None]
    sweep_appledouble: Callable[[str | Path], tuple[int, int]]
    block_bytes: Callable[[str | Path], int]
    percentile_windows: Callable[..., list[dict[str, float]]]
    build_group_attrs: Callable[..., dict[str, Any]]
    legacy_store: Callable[..., Path | None]
    legacy_matches_source: Callable[..., bool]
    store_matches_selection: Callable[..., bool]


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


# ---------------------------------------------------------------------------
# Chunk/tile conversion & 2x2 pyramid downsampling
# ---------------------------------------------------------------------------


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
    from .metal import reduce2x_or_cpu

    if src.dtype in (np.dtype("uint8"), np.dtype("uint16")):
        # The GPU kernel when enabled, else the exact integer CPU box mean;
        # every block is a whole number of 2x2 boxes, so none is dropped.
        mean = da.map_blocks(
            reduce2x_or_cpu,
            data,
            dtype=src.dtype,
            chunks=(
                data.chunks[0],
                tuple(n // 2 for n in data.chunks[1]),
                tuple(n // 2 for n in data.chunks[2]),
            ),
            meta=np.empty((0, 0, 0), dtype=src.dtype),
        )
        _store_direct(mean, arr, storage, allow_rechunk=False)
        return
    acc_dtype = (
        np.float64
        if src.dtype == np.dtype(np.float64)
        or (np.issubdtype(src.dtype, np.integer) and src.dtype.itemsize >= 4)
        else np.float32
    )
    mean = da.coarsen(np.mean, data.astype(acc_dtype), {0: 1, 1: 2, 2: 2})
    if np.issubdtype(src.dtype, np.integer):
        info = np.iinfo(src.dtype)
        mean = da.clip(da.rint(mean), info.min, info.max)
    _store_direct(mean.astype(src.dtype), arr, storage, allow_rechunk=False)


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
        h2 = y1 - y0
        from .metal import reduce2x_or_cpu

        # GPU kernel first when enabled, else the exact CPU box mean; both
        # round to the target dtype only when it is an integer type.
        row = reduce2x_or_cpu(src, dtype)
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


def _sample_plane_blocks(src: PlaneSource, tile: int, points: int) -> list[np.ndarray]:
    """Sample a grid of tile-sized blocks from a plane source for ratio estimation."""
    _, h, w = src.shape
    side = max(1, int(round(points**0.5)))
    blocks: list[np.ndarray] = []
    for i in range(side):
        for j in range(side):
            y = int((i + 0.5) / side * max(1, h - tile))
            x = int((j + 0.5) / side * max(1, w - tile))
            block = src.data[:, y : y + tile, x : x + tile]
            blocks.append(np.asarray(block.compute()))
    return blocks


# ---------------------------------------------------------------------------
# Pyramid build, write, manifest & export publication helpers
# ---------------------------------------------------------------------------


def _is_zarr_directory(path: Path) -> bool:
    return (path / ".zattrs").is_file() or (path / ".zgroup").is_file()


def _open_conversion_source(
    stack: ExitStack,
    source_path: Path,
    selection: PlaneSelection,
    tile: int,
    overview: bool,
) -> PlaneSource:
    import nd2

    from .czi import is_czi, open_czi
    from .lif import is_lif, open_lif
    from .svs import is_svs, open_svs

    if is_svs(source_path):
        if overview:
            raise ValueError("overview stores are for ND2 sources only")
        return open_svs(stack, source_path, tile=tile)
    if is_czi(source_path):
        if overview:
            raise ValueError("overview stores are for ND2 sources only")
        return open_czi(stack, source_path, selection=selection, tile=tile)
    if is_lif(source_path):
        if overview:
            raise ValueError("overview stores are for ND2 sources only")
        return open_lif(stack, source_path, selection=selection, tile=tile)
    handle = stack.enter_context(nd2.ND2File(str(source_path)))
    # Coarsening a 2*tile source block yields one tile-sized
    # level-1 block. This alignment prevents the hidden Dask
    # rechunk that made early compact-cache prototypes slower
    # than full conversion.
    source_tile = tile * 2 if overview else tile
    return open_plane(handle, selection, tile=source_tile)


def _write_pyramid_levels(
    root: Any,
    src: PlaneSource,
    shapes: list[tuple[int, int]],
    first_level: int,
    tile: int,
    storage: StorageBackend,
    *,
    overview: bool,
    progress: bool,
    on_progress: Callable[[float], None] | None,
    hooks: _FacadeHooks,
) -> None:
    channels = src.shape[0]
    arrays: dict[int, Any] = {}
    for level in range(first_level, len(shapes)):
        level_height, level_width = shapes[level]
        arrays[level] = _create_level_array(
            root, str(level), (channels, level_height, level_width), src.dtype, tile, storage
        )

    weights = {level: shapes[level][0] * shapes[level][1] for level in arrays}
    total_pixels = float(sum(weights.values()))
    completed_pixels = 0.0

    def level_context(level: int):
        if on_progress is None:
            return contextlib.nullcontext()
        base = completed_pixels
        return _StoreProgress(
            lambda fraction: on_progress((base + fraction * weights[level]) / total_pixels)
        )

    level_height, level_width = shapes[first_level]
    if progress:
        action = "downsampling from the source" if overview else "writing from the source"
        print(f"  level {first_level}  {level_width} x {level_height}  {action} ...", flush=True)
    with level_context(first_level):
        if overview:
            _write_level1_from_source(src, arrays[1], storage)
        else:
            _write_level0(src, arrays[0], storage)
    completed_pixels += weights[first_level]

    for level in range(first_level + 1, len(shapes)):
        level_height, level_width = shapes[level]
        if progress:
            print(f"  level {level}  {level_width} x {level_height}  downsampling ...", flush=True)
        with level_context(level):
            hooks.downsample(arrays[level - 1], arrays[level], tile, src.dtype, storage)
        completed_pixels += weights[level]


def _build_store_attrs(
    root: Any,
    src: PlaneSource,
    source_path: Path,
    shapes: list[tuple[int, int]],
    first_level: int,
    tile: int,
    storage: StorageBackend,
    *,
    overview: bool,
    ngff_version: str,
    hooks: _FacadeHooks,
) -> dict[str, Any]:
    channels, height, width = src.shape
    level_paths = [str(level) for level in range(first_level, len(shapes))]
    windows = hooks.percentile_windows(root, level_paths, src)
    attrs = hooks.build_group_attrs(
        src,
        shapes[first_level:],
        tile,
        windows,
        start_level=first_level,
        ngff_version=ngff_version,
    )
    attrs["nd2wsi"]["storage"] = storage.descriptor(ngff_version=ngff_version)
    if source_path.is_file():
        from .cache import quick_fingerprint

        attrs["nd2wsi"]["source_fingerprint"] = quick_fingerprint(source_path)
    if overview:
        attrs["nd2wsi"]["kind"] = "overview"
        attrs["nd2wsi"]["overview_of"] = {
            "channels": channels,
            "height": height,
            "width": width,
        }
        attrs["nd2wsi"]["notes"] = attrs["nd2wsi"].get("notes", []) + [
            "overview store: full resolution is read from the source file"
        ]
    return attrs


def _write_store_manifest(
    staging_path: Path,
    slide: Path,
    fingerprint: dict[str, Any],
    selection: PlaneSelection,
    attrs: dict[str, Any],
    *,
    origin: str | None = None,
) -> None:
    from .cache import write_manifest

    meta = attrs["nd2wsi"]
    ov = meta.get("overview_of")
    if ov:
        shape = (ov["channels"], ov["height"], ov["width"])
    else:
        lv0 = meta["levels"][0]
        shape = (len(attrs["omero"]["channels"]), lv0["height"], lv0["width"])
    write_manifest(
        staging_path,
        slide,
        fingerprint,
        selection.describe(),
        meta.get("selection", {}).get("z"),
        meta["tile"],
        shape,
        meta["dtype"],
        kind="overview" if ov else "full",
        storage=meta.get("storage"),
        origin=origin,
    )


def _pack_and_commit_export(
    staging_path: Path,
    packed_file: Path,
    final_path: Path,
    src_resolved: Path,
    before_fp: dict[str, Any] | None,
    selection: PlaneSelection,
    attrs: dict[str, Any],
    *,
    overwrite: bool,
    hooks: _FacadeHooks,
) -> None:
    from .cache import commit_container, quick_fingerprint
    from .migration import pack_directory

    after_fp = quick_fingerprint(src_resolved)
    if after_fp != before_fp:
        raise RuntimeError(
            f"{src_resolved.name} changed while exporting "
            "(still copying, or being written by the scanner?) — "
            "try again once the file is settled"
        )
    _write_store_manifest(staging_path, src_resolved, after_fp, selection, attrs, origin="export")
    pack_directory(staging_path, packed_file, source=src_resolved)
    r_val, _ = hooks.open_store(packed_file)
    close_val = getattr(r_val, "close", None)
    if close_val is not None:
        close_val()
    if final_path.exists() and not overwrite:
        raise FileExistsError(f"{final_path} exists (use --overwrite)")
    if final_path.is_symlink():
        raise ValueError("cache publication refuses symbolic links")
    if overwrite and final_path.is_dir() and not _is_zarr_directory(final_path):
        raise IsADirectoryError(f"refusing to overwrite non-Zarr directory {final_path}")
    commit_container(packed_file, final_path)
    shutil.rmtree(staging_path, ignore_errors=True)


def run_convert(
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
    hooks: _FacadeHooks,
) -> Path:
    """Write a portable pyramid or a source-linked overview store."""
    import dask

    from .cache import (
        SINGLE_FILE_SUFFIX,
        _refuse_database_companions,
        commit_container,
        quick_fingerprint,
    )
    from .sdpc import is_sdpc

    source_path = Path(nd2_path)
    if is_sdpc(source_path):
        raise ValueError("SDPC supports direct viewing and ROI export, not pyramid conversion")
    final_path = Path(out_path)
    single_file_out = final_path.suffix.lower() == SINGLE_FILE_SUFFIX
    storage = storage or DEFAULT_STORAGE
    if tile is None:
        tile = hooks.auto_tile(final_path, storage)
        if progress and tile != 512:
            print(f"tile {tile} for this volume's allocation blocks")
    selection = selection or PlaneSelection()
    if workers is None:
        workers = hooks.auto_workers()
        if progress:
            print(f"hardware: {os.cpu_count()} CPU cores -> {workers} worker threads")

    _refuse_database_companions(final_path)
    if final_path.is_symlink():
        raise ValueError("cache publication refuses symbolic links")
    if final_path.exists() and not overwrite:
        raise FileExistsError(f"{final_path} exists (use --overwrite)")
    if single_file_out and overwrite and final_path.is_dir() and not _is_zarr_directory(final_path):
        raise IsADirectoryError(f"refusing to overwrite non-Zarr directory {final_path}")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    before_fp: dict[str, Any] | None = None
    src_resolved = source_path.resolve()
    if single_file_out:
        final_path = final_path.parent.resolve() / final_path.name
        before_fp = quick_fingerprint(src_resolved)
        staging_path = final_path.with_name(f".{final_path.name}.building-{uuid.uuid4().hex}")
        packed_file: Path | None = final_path.with_name(
            f".{final_path.name}.packed-{uuid.uuid4().hex}"
        )
        shutil.rmtree(staging_path, ignore_errors=True)
        packed_file.unlink(missing_ok=True)
        staging_path.mkdir(parents=True, exist_ok=False)
        store_build_dir = staging_path / "store.ome.zarr"
    else:
        staging_path = final_path.parent / f".{final_path.name}.building-{uuid.uuid4().hex[:12]}"
        packed_file = None
        shutil.rmtree(staging_path, ignore_errors=True)
        store_build_dir = staging_path

    started = time.time()
    swept = freed = 0
    try:
        with dask.config.set(scheduler="threads", num_workers=workers):
            with ExitStack() as stack:
                src = _open_conversion_source(stack, source_path, selection, tile, overview)
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

                root = storage.create_group(store_build_dir)
                _write_pyramid_levels(
                    root,
                    src,
                    shapes,
                    first_level,
                    tile,
                    storage,
                    overview=overview,
                    progress=progress,
                    on_progress=on_progress,
                    hooks=hooks,
                )
                attrs = _build_store_attrs(
                    root,
                    src,
                    source_path,
                    shapes,
                    first_level,
                    tile,
                    storage,
                    overview=overview,
                    ngff_version=ngff_version,
                    hooks=hooks,
                )
                root.attrs.update(attrs)
                if on_progress is not None:
                    on_progress(1.0)

        swept, freed = hooks.sweep_appledouble(staging_path)
        if single_file_out:
            assert packed_file is not None
            _pack_and_commit_export(
                staging_path,
                packed_file,
                final_path,
                src_resolved,
                before_fp,
                selection,
                attrs,
                overwrite=overwrite,
                hooks=hooks,
            )
        else:
            if final_path.exists() and not overwrite:
                raise FileExistsError(f"{final_path} exists (use --overwrite)")
            commit_container(staging_path, final_path)
    except BaseException:
        shutil.rmtree(staging_path, ignore_errors=True)
        if packed_file is not None:
            packed_file.unlink(missing_ok=True)
        raise

    if progress:
        size = (
            final_path.stat().st_size
            if final_path.is_file()
            else sum(path.stat().st_size for path in final_path.rglob("*") if path.is_file())
        )
        print(
            f"  done in {time.time() - started:.1f}s -> {final_path} "
            f"({nice_bytes(size)} on disk)"
        )
        if swept:
            print(f"  swept {swept} AppleDouble files, {nice_bytes(freed)} recovered")
    return final_path


# ---------------------------------------------------------------------------
# Cache generation, validation, discovery & legacy store management
# ---------------------------------------------------------------------------


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


def _validate_store(
    candidate: Path,
    slide: Path,
    selection: PlaneSelection,
    *,
    tile: int | None,
    hooks: _FacadeHooks,
) -> None:
    root, attrs = hooks.open_store(candidate)
    close = getattr(root, "close", None)
    if close:
        close()
    if tile is not None and int((attrs.get("nd2wsi") or {}).get("tile") or 0) != int(tile):
        raise ValueError("cached tile size does not match requested tile")
    if not hooks.semantics(slide, selection, attrs):
        raise ValueError("cached decoder or channel metadata semantics are outdated")


def _import_old_container(
    old_valid: Path,
    container: Path,
    slide: Path,
    selection: PlaneSelection,
    *,
    want: str | None,
    tile: int | None,
    on_progress: Callable[[float], None] | None,
    validate_fn: Callable[[Path], None],
    refuse_if_newer_fn: Callable[[], None],
) -> None:
    from .cache import CacheLock, cache_matches, commit_container, quarantine
    from .migration import pack_directory

    packed = container.with_name(f"{container.name}.building-{uuid.uuid4().hex}")
    with CacheLock(old_valid):
        if not cache_matches(old_valid, slide, selection, kind=want, tile=tile):
            raise RuntimeError("legacy cache changed before import; reopen the slide")
        try:
            pack_directory(old_valid, packed, source=slide, on_progress=on_progress)
            validate_fn(packed)
            if container.exists():
                refuse_if_newer_fn()
                quarantine(container)
            commit_container(packed, container)
        except FileExistsError:
            raise  # do not remove a competing or pre-existing artifact
        except BaseException:
            packed.unlink(missing_ok=True)
            raise


def _build_and_commit_cache(
    container: Path,
    slide: Path,
    selection: PlaneSelection,
    *,
    tile: int | None,
    kind: str,
    on_progress: Callable[[float], None] | None,
    hooks: _FacadeHooks,
    validate_fn: Callable[[Path], None],
    refuse_if_newer_fn: Callable[[], None],
) -> None:
    from .cache import commit_container, quarantine, quick_fingerprint, sweep_stale_builds
    from .migration import pack_directory

    sweep_stale_builds(container.parent)
    overview = kind == "auto" and _overview_eligible(
        slide, selection, tile or hooks.auto_tile(container)
    )
    before = quick_fingerprint(slide)
    staging = container.with_name(f"{container.name}.building-{uuid.uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        hooks.convert(
            slide,
            staging / "store.ome.zarr",
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
        root_staged, attrs = hooks.open_store(staging / "store.ome.zarr")
        close_staged = getattr(root_staged, "close", None)
        if close_staged:
            close_staged()
        _write_store_manifest(staging, slide, after, selection, attrs)

        packed = container.with_name(f"{container.name}.building-packed-{uuid.uuid4().hex}")
        try:
            pack_directory(staging, packed, source=slide)
            validate_fn(packed)
            if container.exists():
                refuse_if_newer_fn()
                quarantine(container)
            commit_container(packed, container)
        except FileExistsError:
            raise
        except BaseException:
            packed.unlink(missing_ok=True)
            raise
        shutil.rmtree(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def ensure_cache(
    slide: str | Path,
    selection: PlaneSelection | None = None,
    on_progress: Callable[[float], None] | None = None,
    tile: int | None = None,
    kind: str = "auto",
    *,
    hooks: _FacadeHooks,
) -> Path:
    """The store to open for this slide and selection, building if needed."""
    from .cache import (
        SINGLE_FILE_FORMAT,
        CacheFromNewerApp,
        CacheLock,
        cache_container,
        cache_matches,
        container_store,
        directory_cache_container,
        legacy_cache_container,
        newer_cache_format,
    )
    from .sdpc import is_sdpc

    slide = Path(slide).resolve()
    if is_sdpc(slide):
        raise ValueError("SDPC is served directly; open the slide instead of building a viewing cache")
    selection = selection or PlaneSelection()
    want = None if kind == "auto" else kind

    container = cache_container(slide, selection)
    store = container  # a v2 destination is a file, even if a damaged directory occupies it

    def refuse_if_newer() -> None:
        newer = newer_cache_format(container)
        if newer:
            raise CacheFromNewerApp(
                f"The cache for {slide.name} was built by a newer nd2wsi-viewer "
                f"(cache format {newer}; this app reads up to {SINGLE_FILE_FORMAT}). "
                f"Update the app, or remove {container.name} to rebuild it here."
            )

    def validate(candidate: Path) -> None:
        _validate_store(candidate, slide, selection, tile=tile, hooks=hooks)

    refuse_if_newer()
    # the common case takes no lock: a valid container serves immediately,
    # and it outranks legacy stores so an old pyramids dir cannot shadow a
    # correct managed cache
    if cache_matches(container, slide, selection, kind=want, tile=tile) and store.exists():
        try:
            validate(store)
            return store
        except ValueError:
            pass  # damaged despite its manifest: handled under the lock

    # Versions 0.8 and 0.9 used the stem alone. Reuse a valid old container,
    # but never let it become the identity of a new cache: ``slide.nd2`` and
    # ``slide.svs`` may live together.
    old_valid = None
    for old_container in (
        directory_cache_container(slide, selection),
        legacy_cache_container(slide, selection),
    ):
        old_store = container_store(old_container)
        if (
            cache_matches(old_container, slide, selection, kind=want, tile=tile)
            and old_store.is_dir()
        ):
            try:
                validate(old_store)
                old_valid = old_container
                break
            except ValueError:
                pass  # old cache remains preserved

    if old_valid is None:
        legacy = hooks.legacy_store(slide, selection, tile=tile)
        if legacy is not None:
            return legacy

    with CacheLock(container):
        # someone else may have finished it while this process waited, and
        # quarantining under the lock means two openers cannot both grab
        # the same stale container
        if cache_matches(container, slide, selection, kind=want, tile=tile) and store.exists():
            try:
                validate(store)
                return store
            except ValueError:
                pass
        if container.exists():
            refuse_if_newer()  # it may have appeared while we waited
        if old_valid is not None:
            _import_old_container(
                old_valid,
                container,
                slide,
                selection,
                want=want,
                tile=tile,
                on_progress=on_progress,
                validate_fn=validate,
                refuse_if_newer_fn=refuse_if_newer,
            )
            return store  # explicit cleanup, never an automatic open, removes the old tree
        _build_and_commit_cache(
            container,
            slide,
            selection,
            tile=tile,
            kind=kind,
            on_progress=on_progress,
            hooks=hooks,
            validate_fn=validate,
            refuse_if_newer_fn=refuse_if_newer,
        )
    return store


def existing_cache_store(
    slide: str | Path,
    selection: PlaneSelection | None = None,
    *,
    read_only: bool = False,
    tile: int | None = None,
    hooks: _FacadeHooks,
) -> Path | None:
    """A valid, already-built store, or None. Never builds."""
    from .cache import (
        cache_container,
        cache_matches,
        container_store,
        directory_cache_container,
        legacy_cache_container,
    )

    slide = Path(slide).resolve()
    selection = selection or PlaneSelection()
    for container in (
        cache_container(slide, selection),
        directory_cache_container(slide, selection),
        legacy_cache_container(slide, selection),
    ):
        store = container_store(container)
        if cache_matches(container, slide, selection, tile=tile) and store.exists():
            try:
                _validate_store(store, slide, selection, tile=tile, hooks=hooks)
            except Exception:
                continue
            return store
    legacy = hooks.legacy_store(slide, selection, read_only=read_only, tile=tile)
    return legacy if legacy is not None else None


def _legacy_store_matches_source(
    store: Path,
    slide: Path,
    attrs: dict[str, Any] | None = None,
    *,
    hooks: _FacadeHooks,
) -> bool:
    """Whether a stem-only store can be assigned to this source safely."""
    try:
        if attrs is None:
            root, attrs = hooks.open_store(store)
            close = getattr(root, "close", None)
            if close is not None:
                close()
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
            and item.suffix.lower() in (".nd2", ".czi", ".lif", ".svs", ".sdpc")
        ]
    except OSError:
        return False
    return len(siblings) == 1


def _legacy_store(
    slide: Path,
    selection: PlaneSelection | None = None,
    *,
    read_only: bool = False,
    tile: int | None = None,
    hooks: _FacadeHooks,
) -> Path | None:
    """A complete portable store found beside this source."""
    from .cache import quarantine

    selection = selection or PlaneSelection()
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
            root, attrs = hooks.open_store(cand)
            close = getattr(root, "close", None)
            if close is not None:
                close()
        except (ValueError, FileNotFoundError):
            if read_only:
                continue
            # 0.8 converts atomically, so a store that exists but does not
            # open is a wedge from an older version, never work in progress
            try:
                quarantine(cand)
            except FileNotFoundError:
                pass  # another opener quarantined it first
            continue
        if not hooks.legacy_matches_source(cand, slide, attrs):
            continue
        if (
            not isinstance((attrs.get("nd2wsi") or {}).get("source_fingerprint"), dict)
            and slide.is_file()
        ):
            try:
                if (cand / ".zattrs").stat().st_mtime_ns < slide.stat().st_mtime_ns:
                    continue
            except OSError:
                continue
        if not hooks.store_matches_selection(slide, attrs, selection, tile=tile):
            # built for another timepoint, position, or Z plane/projection
            continue
        return cand
    return None


def default_store_path(
    nd2_path: str | Path,
    cache_dir: str | Path | None = None,
    *,
    hooks: _FacadeHooks,
) -> Path:
    """Default path for a complete, portable pyramid."""
    nd2_path = Path(nd2_path)
    base = Path(cache_dir) if cache_dir else nd2_path.parent
    for legacy in (
        base / CACHE_DIR_NAME / f"{nd2_path.stem}.ome.zarr",
        base / f"pyramid_{nd2_path.stem}.ome.zarr",
        base / f"{nd2_path.stem}.ome.zarr",
    ):
        if legacy.exists() and hooks.legacy_matches_source(legacy, nd2_path):
            return legacy
    return base / CACHE_DIR_NAME / f"{nd2_path.name}.ome.zarr"


def is_nd2wsi_store(path: str | Path) -> bool:
    """True for a directory this tool wrote, cheaply and without opening it."""
    attrs = Path(path) / ".zattrs"
    try:
        return '"nd2wsi"' in attrs.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


def tidy_caches(
    folder: str | Path, *, dry_run: bool = False, hooks: _FacadeHooks
) -> dict[str, Any]:
    """Move scattered pyramid stores in ``folder`` into ``pyramids/``."""
    folder = Path(folder)
    dest = folder / CACHE_DIR_NAME
    moved = []
    swept = freed = 0
    if not dry_run and dest.is_dir():
        n, b = hooks.sweep_appledouble(dest)
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
            n, b = hooks.sweep_appledouble(target)
            swept += n
            freed += b
            # exFAT and NTFS carry Finder metadata in a sibling AppleDouble
            # file, which is dead weight once its item has moved away.
            sidecar = item.parent / f"._{item.name}"
            if sidecar.exists():
                sidecar.unlink()
                swept += 1
                freed += hooks.block_bytes(folder)
    return {"moved": moved, "swept": swept, "freed": freed}
