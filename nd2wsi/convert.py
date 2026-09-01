"""ND2 -> OME-Zarr pyramid conversion.

Writes an OME-NGFF 0.4 multiscales group (zarr v2 layout for maximum viewer
compatibility: napari, vizarr, ome-zarr-py, QuPath all read it today):

    out.zarr/
      .zattrs          multiscales + omero + nd2wsi metadata
      .zgroup
      0/               level 0, shape (C, Y, X), chunks (1, tile, tile)
      1/               level 1 = level 0 downsampled 2x (2x2 mean)
      ...

Every step is memory-bounded:

* level 0 streams straight out of the ND2 memory map in (1, tile, tile)
  chunks (see :mod:`nd2wsi.reader`);
* level k+1 is computed from the *zarr* level k with a chunk-aligned
  2x2 mean (dask ``coarsen``), so nothing larger than a few tiles is ever
  resident.
"""

from __future__ import annotations

import json
import math
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from .reader import PlaneSelection, PlaneSource, level_shapes, nice_bytes, open_plane

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


def auto_workers() -> int:
    """Dask worker threads matched to this machine's CPU.

    Uses 80% of the logical cores (rounded up, floor 2), leaving headroom
    for the OS and the UI. Tile decode and zstd compression both release
    the GIL, so throughput scales with cores on Apple silicon.
    """
    import os

    n = os.cpu_count() or 4
    return max(2, math.ceil(n * 0.8))


def store_compressor() -> Any:
    """The codec every pyramid level is written with."""
    from numcodecs import Blosc

    return Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE)


def _create_level_array(
    root: Any, path: str, shape: tuple[int, ...], dtype: np.dtype, tile: int
) -> Any:
    return root.create_array(
        name=path,
        shape=shape,
        chunks=(1, tile, tile),
        dtype=dtype,
        compressors=store_compressor(),
        dimension_names=None,
        overwrite=True,
    )


def _chunk_io(arr: Any) -> tuple[Path, Any, tuple[int, ...], str]:
    """(dir, codec, chunk shape, key separator) for a local zarr v2 array.

    zarr-python 3's synchronous API funnels every chunk read and write from
    dask's worker threads through one async bridge, capping bulk pixel I/O
    near 0.7 GB/s where direct file + blosc access reaches ~4.7 GB/s on the
    same cores -- so the conversion hot loops do their own chunk file I/O
    against the v2 layout and keep zarr for metadata and casual reads.
    """
    import numcodecs

    path = Path(arr.store.root) / arr.path
    meta = json.loads((path / ".zarray").read_text())
    if meta.get("order", "C") != "C":
        raise ValueError("expected C-order chunks")
    codec = numcodecs.get_codec(meta["compressor"])
    sep = meta.get("dimension_separator", ".")
    return path, codec, tuple(meta["chunks"]), sep


def _is_aligned(chunks: tuple[tuple[int, ...], ...], cshape: tuple[int, ...]) -> bool:
    """True when every dask block boundary sits on the store's chunk grid."""
    for dim, step in zip(chunks, cshape):
        edge = 0
        for size in dim[:-1]:
            edge += size
            if edge % step:
                return False
    return True


def _store_direct(data: Any, zarr_arr: Any) -> None:
    """da.store equivalent writing encoded chunk files straight to disk.

    Each dask block may span many store chunks (block boundaries must sit on
    the chunk grid); the whole block is sliced, encoded and written inside
    one task, so nothing is reshuffled through the dask graph.
    """
    import dask.array as da

    path, codec, cshape, sep = _chunk_io(zarr_arr)
    if not _is_aligned(data.chunks, cshape):
        data = data.rechunk(cshape)

    def put(block: np.ndarray, block_info: Any = None) -> np.ndarray:
        starts = [a for a, _ in block_info[0]["array-location"]]
        ranges = [range(0, s, c) for s, c in zip(block.shape, cshape)]
        for c0 in ranges[0]:
            for y0 in ranges[1]:
                for x0 in ranges[2]:
                    sub = block[
                        c0 : c0 + cshape[0],
                        y0 : y0 + cshape[1],
                        x0 : x0 + cshape[2],
                    ]
                    if sub.shape != cshape:  # v2 pads edge chunks to full size
                        full = np.zeros(cshape, sub.dtype)
                        full[tuple(slice(0, s) for s in sub.shape)] = sub
                        sub = full
                    key = sep.join(
                        str((a + o) // s)
                        for a, o, s in zip(starts, (c0, y0, x0), cshape)
                    )
                    (path / key).write_bytes(
                        codec.encode(np.ascontiguousarray(sub))
                    )
        return np.zeros((1,) * block.ndim, np.uint8)

    da.map_blocks(
        put, data, chunks=tuple((1,) * len(c) for c in data.chunks), dtype=np.uint8
    ).compute()


def _write_level0(src: PlaneSource, arr: Any) -> None:
    _store_direct(src.data, arr)


def _downsample_into(prev_arr: Any, next_arr: Any, tile: int, dtype: np.dtype) -> None:
    """2x2-mean one pyramid level into the next, one task per target chunk.

    Each task decodes the four source chunk files under its footprint,
    means them (float32 is exact for 2x2 means of uint8/uint16), and writes
    the encoded target chunk -- direct chunk I/O end to end, nothing moves
    through the dask graph.
    """
    import dask

    ppath, pcodec, pcs, psep = _chunk_io(prev_arr)
    npath, ncodec, ncs, nsep = _chunk_io(next_arr)
    pdtype = np.dtype(prev_arr.dtype)
    c, ph, pw = prev_arr.shape
    _, nh, nw = next_arr.shape
    ew = nw * 2  # even source extent consumed per row
    _, pt, _ = pcs
    _, nt, _ = ncs
    integer = np.issubdtype(dtype, np.integer)

    def one_row(ci: int, ty: int) -> None:
        y0, y1 = ty * nt, min((ty + 1) * nt, nh)
        sy0, sy1 = 2 * y0, 2 * y1
        src = np.zeros((sy1 - sy0, ew), pdtype)
        # An axis that has already collapsed to one pixel has no second row
        # or column to average with. Read only what the source holds, then
        # repeat its edge, so the level carries the real value forward
        # instead of averaging it with the padding zarr keeps in edge chunks.
        ry, rx = min(sy1, ph), min(ew, pw)
        for cy in range(sy0 // pt, -(-ry // pt)):
            for cx in range(-(-rx // pt)):
                key = psep.join((str(ci), str(cy), str(cx)))
                full = np.frombuffer(
                    pcodec.decode((ppath / key).read_bytes()), pdtype
                ).reshape(pcs)[0]
                oy0, oy1 = max(sy0, cy * pt), min(ry, cy * pt + pt)
                ox0, ox1 = cx * pt, min(cx * pt + pt, rx)
                src[oy0 - sy0 : oy1 - sy0, ox0:ox1] = full[
                    oy0 - cy * pt : oy1 - cy * pt, : ox1 - ox0
                ]
        if ry < sy1:
            src[ry - sy0 :] = src[ry - sy0 - 1 : ry - sy0]
        if rx < ew:
            src[:, rx:] = src[:, rx - 1 : rx]
        h2 = y1 - y0
        mean = src.reshape(h2, 2, nw, 2).mean(axis=(1, 3), dtype=np.float32)
        row = np.rint(mean).astype(dtype) if integer else mean.astype(dtype)
        for tx in range(-(-nw // nt)):
            x0, x1 = tx * nt, min((tx + 1) * nt, nw)
            out = np.zeros(ncs, dtype)  # v2 pads edge chunks to full size
            out[0, :h2, : x1 - x0] = row[:, x0:x1]
            (npath / nsep.join((str(ci), str(ty), str(tx)))).write_bytes(
                ncodec.encode(out)
            )

    tasks = [
        dask.delayed(one_row)(ci, ty)
        for ci in range(c)
        for ty in range(-(-nh // nt))
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
) -> dict[str, Any]:
    calibrated = src.pixel_size_um is not None
    py, px = src.pixel_size_um if calibrated else (1.0, 1.0)
    datasets = []
    for k, (h, w) in enumerate(shapes):
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
            "version": NGFF_VERSION,
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
        for k, (h, w) in enumerate(shapes)
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
) -> Path:
    """Convert one plane of an ND2 file to an OME-Zarr pyramid on disk.

    ``workers=None`` sizes the thread pool to this machine's CPU.
    """
    import os

    import dask
    import nd2
    import zarr

    nd2_path = Path(nd2_path)
    out_path = Path(out_path)
    if tile is None:
        tile = auto_tile(out_path)
        if progress and tile != 512:
            print(f"tile {tile} for this volume's allocation blocks")
    selection = selection or PlaneSelection()
    if workers is None:
        workers = auto_workers()
        if progress:
            print(f"hardware: {os.cpu_count()} CPU cores -> {workers} worker threads")

    if out_path.exists():
        if not overwrite:
            raise FileExistsError(f"{out_path} exists (use --overwrite)")
        shutil.rmtree(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    from contextlib import ExitStack

    from .svs import is_svs, open_svs

    with dask.config.set(scheduler="threads", num_workers=workers):
        with ExitStack() as stack:
            if is_svs(nd2_path):
                src = open_svs(stack, nd2_path, tile=tile)
            else:
                f = stack.enter_context(nd2.ND2File(str(nd2_path)))
                src = open_plane(f, selection, tile=tile)
            c, h, w = src.shape
            shapes = level_shapes(h, w, tile)
            if progress:
                raw = c * h * w * src.dtype.itemsize
                print(
                    f"{nd2_path.name}: {w} x {h} px, {c} channel(s), "
                    f"{src.dtype}, {'RGB' if src.rgb else 'grayscale'}, "
                    f"~{nice_bytes(raw)} of level-0 pixels"
                )
                for note in src.notes:
                    print(f"  note: {note}")
                print(f"  pyramid: {len(shapes)} levels, tile {tile}")

            root = zarr.open_group(str(out_path), mode="w", zarr_format=2)
            arrays = []
            for k, (lh, lw) in enumerate(shapes):
                arrays.append(
                    _create_level_array(root, str(k), (c, lh, lw), src.dtype, tile)
                )

            weights = [lh * lw for (lh, lw) in shapes]
            total_px = float(sum(weights))
            done_px = 0.0

            def level_ctx(k: int):
                if on_progress is None:
                    import contextlib

                    return contextlib.nullcontext()
                base = done_px

                return _StoreProgress(
                    lambda f: on_progress((base + f * weights[k]) / total_px)
                )

            if progress:
                print(f"  level 0  {w} x {h}  writing from ND2 ...", flush=True)
            with level_ctx(0):
                _write_level0(src, arrays[0])
            done_px += weights[0]

            for k in range(1, len(shapes)):
                lh, lw = shapes[k]
                if progress:
                    print(f"  level {k}  {lw} x {lh}  downsampling ...", flush=True)
                with level_ctx(k):
                    _downsample_into(arrays[k - 1], arrays[k], tile, src.dtype)
                done_px += weights[k]

            windows = _percentile_windows(root, [str(k) for k in range(len(shapes))], src)
            root.attrs.update(build_group_attrs(src, shapes, tile, windows))
            if on_progress is not None:
                on_progress(1.0)

    swept, freed = sweep_appledouble(out_path)
    if progress:
        size = sum(p.stat().st_size for p in out_path.rglob("*") if p.is_file())
        print(
            f"  done in {time.time() - t0:.1f}s -> {out_path} ({nice_bytes(size)} on disk)"
        )
        if swept:
            print(f"  swept {swept} AppleDouble files, {nice_bytes(freed)} recovered")
    return out_path


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


def auto_tile(out_path: str | Path) -> int:
    """Chunk edge matched to where the pyramid will live.

    A chunk file never occupies less than one allocation block, and big
    exFAT volumes use blocks of 128 KB to 1 MB, so 512 px chunks there cost
    four times their content. Measured on such an SSD, 1024 px chunks cut
    the store to 1.6 GB where 512 took 3.8, built five times faster, and
    filled a screen a shade quicker, with a pan step at 17 ms. Volumes with
    ordinary 4 KB blocks keep 512, which pans fastest.
    """
    path = Path(out_path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return 1024 if _block_bytes(path) >= 65536 else 512


def estimate_store_bytes(
    path: str | Path, *, tile: int | None = None, points: int = 16
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
        tile = auto_tile(default_store_path(path))
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
    raw = sum(c * lh * lw for lh, lw in shapes) * dtype.itemsize

    codec = store_compressor()
    sampled = sum(b.nbytes for b in blocks)
    packed = sum(
        len(codec.encode(np.ascontiguousarray(plane)))
        for b in blocks
        for plane in b
    )
    ratio = (packed / sampled) if sampled else 1.0
    total = int(raw * ratio)

    # A pyramid is tens of thousands of small files, and a file never takes
    # less than one allocation block. On a big exFAT volume that block is
    # 1 MB, which turns a 130 KB chunk into a megabyte and the whole store
    # into several times its own size, so report what the volume will
    # actually spend rather than the size of the data.
    block = _block_bytes(path.parent)
    chunk_bytes = tile * tile * dtype.itemsize * ratio
    per_chunk = max(block, -(-int(chunk_bytes) // block) * block)
    files = sum(c * -(-lh // tile) * -(-lw // tile) for lh, lw in shapes)
    on_disk = files * per_chunk + block * (len(shapes) + 2)  # plus metadata
    return {
        "bytes": max(total, on_disk),
        "human": nice_bytes(max(total, on_disk)),
        "data_bytes": total,
        "disk_bytes": on_disk,
        "block_bytes": block,
        "files": files,
        "ratio": ratio,
        "width": w,
        "height": h,
        "channels": c,
        "levels": len(shapes),
        "raw_bytes": raw,
        "source_bytes": path.stat().st_size,
        "free_bytes": shutil.disk_usage(path.parent).free,
    }


CACHE_DIR_NAME = "pyramids"


def default_store_path(nd2_path: str | Path, cache_dir: str | Path | None = None) -> Path:
    """``pyramids/<slide>.ome.zarr`` beside the slide.

    One folder per directory of slides keeps the caches together instead of
    scattering a store between every file. Stores made by older versions
    (``pyramid_<slide>.ome.zarr`` or ``<slide>.ome.zarr`` next to the slide)
    are still used where they lie, so nothing has to be rebuilt.
    """
    nd2_path = Path(nd2_path)
    base = Path(cache_dir) if cache_dir else nd2_path.parent
    for legacy in (
        base / f"pyramid_{nd2_path.stem}.ome.zarr",
        base / f"{nd2_path.stem}.ome.zarr",
    ):
        if legacy.exists():
            return legacy
    return base / CACHE_DIR_NAME / f"{nd2_path.stem}.ome.zarr"


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


def mpx(w: int, h: int) -> float:
    return w * h / 1e6


def ceil_log2(n: int) -> int:
    return int(math.ceil(math.log2(max(1, n))))
