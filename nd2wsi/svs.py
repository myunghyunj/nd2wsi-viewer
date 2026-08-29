"""Aperio SVS input: the same PlaneSource the ND2 path produces.

SVS is a pyramidal TIFF with JPEG tiles; ``tifffile`` exposes the baseline
image as a zarr array, which slots straight into the memory-bounded dask
conversion. Decoding the JPEG tiles needs ``imagecodecs``
(``pip install "nd2wsi-viewer[svs]"``).

Calibration comes from the Aperio metadata string (``MPP`` in µm/px,
``AppMag`` for the objective).
"""

from __future__ import annotations

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
