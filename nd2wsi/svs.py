"""Aperio SVS input: the same PlaneSource the ND2 path produces.

SVS is a pyramidal TIFF with JPEG or JPEG 2000 tiles. The baseline level is
exposed as a lazy dask array whose tasks read each compressed tile straight
off the file with ``os.pread`` and decode it with ``TiffPage.decode`` -- no
shared file lock, so decoding scales across all worker threads, and blocks
are aligned to the file's own tile grid so every tile is decoded exactly
once. Decoding needs ``imagecodecs`` (``pip install "nd2wsi-viewer[svs]"``).

Calibration comes from the Aperio metadata string (``MPP`` in µm/px,
``AppMag`` for the objective).
"""

from __future__ import annotations

import os
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import numpy as np

from .reader import ChannelInfo, PlaneSource

SVS_SUFFIXES = {".svs"}
IMAGECODECS_HINT = (
    "reading SVS needs the 'imagecodecs' package for its JPEG tiles: "
    "pip install \"nd2wsi-viewer[svs]\"  (or: pip install imagecodecs)"
)


def is_svs(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SVS_SUFFIXES


def _require_imagecodecs() -> None:
    try:
        import imagecodecs  # noqa: F401
    except ImportError as e:
        raise RuntimeError(IMAGECODECS_HINT) from e


def _aperio_meta(description: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = re.search(r"\bMPP\s*=\s*([0-9.]+)", description)
    if m:
        out["mpp"] = float(m.group(1))
    m = re.search(r"\bAppMag\s*=\s*([0-9.]+)", description)
    if m:
        out["magnification"] = float(m.group(1))
    return out


def _grid_chunks(size: int, step: int) -> tuple[int, ...]:
    """Chunk sizes covering ``size`` in ``step`` strides (last may be short)."""
    n_full, rem = divmod(size, step)
    return tuple([step] * n_full + ([rem] if rem else []))


def _window_reader(page: Any, fd: int):
    """Return ``read(y0, y1, x0, x1) -> (h, w, S)`` for one tiled TIFF page.

    Each call fetches the native tiles covering the window with ``os.pread``
    on ``fd`` and decodes them with ``page.decode``. imagecodecs releases the
    GIL, so many threads may share one reader and one descriptor.
    """
    h, w = page.imagelength, page.imagewidth
    th, tw = page.tilelength, page.tilewidth
    samples = page.samplesperpixel
    dtype = np.dtype(page.dtype)
    across = -(-w // tw)
    offsets = page.dataoffsets
    counts = page.databytecounts
    # Aperio's JPEG flavour keeps one shared quantization/Huffman table in
    # TIFF tag 347 and writes abbreviated streams per tile, so the decoder
    # needs the tables handed to it. Every tifffile decoder takes these two
    # keywords, and both are None for JPEG 2000 and LZW.
    tables = {
        "jpegtables": getattr(page, "jpegtables", None),
        "jpegheader": getattr(page, "jpegheader", None),
    }

    def read(y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        out = np.zeros((y1 - y0, x1 - x0, samples), dtype)
        for ty in range(y0 // th, -(-y1 // th)):
            for tx in range(x0 // tw, -(-x1 // tw)):
                seg = ty * across + tx
                if counts[seg] == 0:  # sparse tile, leave it at zero
                    continue
                data = os.pread(fd, counts[seg], offsets[seg])
                decoded = np.asarray(page.decode(data, seg, **tables)[0]).reshape(
                    -1, tw, samples
                )[:th]
                oy0, oy1 = max(y0, ty * th), min(y1, ty * th + th, h)
                ox0, ox1 = max(x0, tx * tw), min(x1, tx * tw + tw, w)
                out[oy0 - y0 : oy1 - y0, ox0 - x0 : ox1 - x0] = decoded[
                    oy0 - ty * th : oy1 - ty * th, ox0 - tx * tw : ox1 - tx * tw
                ]
        return out

    return read


def _tiled_baseline(page: Any, path: Path, stack: ExitStack, tile: int) -> Any:
    """Lazy (S, Y, X) dask array over a tiled TIFF page, decode-per-task.

    Each dask block is an ``8 x tile`` square aligned to the pyramid store's
    chunk grid, so the conversion writes it out with no dask-level shuffle.
    Native tiles straddling a block edge are decoded by both neighbors, a
    small price next to the shuffle it avoids.
    """
    import dask.array as da

    h, w = page.imagelength, page.imagewidth
    samples = page.samplesperpixel
    fd = os.open(str(path), os.O_RDONLY)
    stack.callback(os.close, fd)
    read = _window_reader(page, fd)
    block = tile * 8

    def fetch(block_info=None):
        (y0, y1), (x0, x1) = block_info[None]["array-location"][1:]
        return np.ascontiguousarray(np.moveaxis(read(y0, y1, x0, x1), -1, 0))

    return da.map_blocks(
        fetch,
        chunks=((samples,), _grid_chunks(h, block), _grid_chunks(w, block)),
        dtype=np.dtype(page.dtype),
    )


def sample_planes(path: str | Path, tile: int = 512, points: int = 24) -> list:
    """Decode a spread of chunk-sized windows from an SVS baseline level.

    Returns ``(C, tile, tile)`` blocks taken on an even grid over the slide,
    background included, so a compressor measured on them reflects the whole
    image. Used to predict the pyramid's size before building it.
    """
    import tifffile

    _require_imagecodecs()
    path = Path(path)
    out = []
    with tifffile.TiffFile(str(path)) as tf:
        page = tf.series[0].pages[0]
        if not getattr(page, "is_tiled", False):
            return out
        h, w = page.imagelength, page.imagewidth
        fd = os.open(str(path), os.O_RDONLY)
        try:
            read = _window_reader(page, fd)
            side = max(1, int(round(points**0.5)))
            for i in range(side):
                for j in range(side):
                    y = int((i + 0.5) / side * max(1, h - tile))
                    x = int((j + 0.5) / side * max(1, w - tile))
                    win = read(y, min(y + tile, h), x, min(x + tile, w))
                    out.append(np.ascontiguousarray(np.moveaxis(win, -1, 0)))
        finally:
            os.close(fd)
    return out


def open_svs(stack: ExitStack, path: str | Path, tile: int = 512) -> PlaneSource:
    """Open the baseline level of an SVS as a lazy (3, Y, X) RGB source.

    ``stack`` keeps the TIFF handle alive for the duration of the conversion.
    """
    import dask.array as da
    import tifffile

    _require_imagecodecs()
    path = Path(path)
    tf = stack.enter_context(tifffile.TiffFile(str(path)))
    series = tf.series[0]
    if series.ndim != 3 or series.shape[-1] not in (3, 4):
        raise NotImplementedError(f"unexpected SVS layout {series.shape}")

    page = series.pages[0]
    if getattr(page, "is_tiled", False):
        data = _tiled_baseline(page, path, stack, tile)[:3]
    else:
        import zarr

        z = zarr.open(series.aszarr(level=0), mode="r")
        arr = da.from_array(z, chunks=(tile, tile, series.shape[-1]))[..., :3]
        data = da.moveaxis(arr, -1, 0)  # (Y, X, S) -> (S, Y, X)

    meta = _aperio_meta(tf.pages[0].description or "")
    mpp = float(meta.get("mpp", 1.0))
    h, w = int(series.shape[0]), int(series.shape[1])
    return PlaneSource(
        data=data,
        dtype=np.dtype(series.dtype),
        shape=(3, h, w),
        rgb=True,
        channels=[
            ChannelInfo("Red", (255, 0, 0)),
            ChannelInfo("Green", (0, 255, 0)),
            ChannelInfo("Blue", (0, 0, 255)),
        ],
        pixel_size_um=(mpp, mpp),
        source_name=str(path),
        selection={},
        notes=[f"Aperio SVS baseline level ({w} x {h})"],
        magnification=meta.get("magnification"),
    )


def collect_svs_info(path: str | Path) -> dict[str, Any]:
    """`nd2wsi info` payload for an SVS file."""
    import tifffile

    path = Path(path)
    out: dict[str, Any] = {"file": str(path), "size_bytes": path.stat().st_size}
    with tifffile.TiffFile(str(path)) as tf:
        series = tf.series[0]
        meta = _aperio_meta(tf.pages[0].description or "")
        page = tf.pages[0]
        out.update(
            {
                "format": "Aperio SVS (pyramidal TIFF)",
                "sizes": {"Y": series.shape[0], "X": series.shape[1], "S": series.shape[-1]},
                "dtype": str(series.dtype),
                "levels": [list(lv.shape[:2]) for lv in series.levels],
                "tile": [page.tilelength, page.tilewidth] if page.is_tiled else None,
                "compression": str(page.compression.name),
                "mpp_um": meta.get("mpp"),
                "magnification": meta.get("magnification"),
            }
        )
    return out


def format_svs_info(info: dict[str, Any]) -> str:
    from .reader import nice_bytes

    lines = [
        f"file            {info['file']}  ({nice_bytes(info['size_bytes'])})",
        f"format          {info['format']}",
        f"dimensions      {info['sizes']}",
        f"dtype           {info['dtype']}   compression={info['compression']}"
        + (f"   tile={info['tile']}" if info.get("tile") else ""),
        f"pixel size      {info['mpp_um']} um (MPP)"
        + (f"   objective {info['magnification']}x" if info.get("magnification") else ""),
        "",
        f"embedded pyramid: {len(info['levels'])} level(s) "
        + " ".join(f"{h}x{w}" for h, w in info["levels"]),
        "  (conversion reads the baseline level and rebuilds a clean halving pyramid)",
    ]
    return "\n".join(lines)
