"""ROI export back to ND2, via Laboratory Imaging's own ``limnd2`` writer.

The exported file is a plain modern ND2 (one uncompressed ``ImageDataSeq|0!``
frame) carrying the pixel calibration and the channel names/colors of the
source, so it reopens in NIS-Elements -- and in this tool -- like any other
acquisition.

Writes are streamed with ``Nd2Writer.setImageTile``, reading the source one
TILE-row band at a time, so heap stays bounded by ``TILE x ROI-width``
regardless of ROI height.

``limnd2`` is not on PyPI; it lives on Laboratory Imaging's own index:

    pip install --index-url https://pypi.laboratory-imaging.com/simple limnd2
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

LIMND2_HINT = (
    "ND2 export needs the 'limnd2' package (Laboratory Imaging's ND2 SDK), "
    "which is not on PyPI. Install it with:  pip install --index-url "
    "https://pypi.laboratory-imaging.com/simple limnd2"
)

TILE = 512


def require_limnd2() -> Any:
    try:
        import limnd2
    except ImportError as e:
        raise RuntimeError(
            f"{LIMND2_HINT} (import failed with {type(e).__name__}: {e})"
        ) from e
    return limnd2


def _check_dtype(dtype: np.dtype) -> int:
    """Bits per component for dtypes the ND2 writer round-trips faithfully."""
    if dtype not in (np.dtype(np.uint8), np.dtype(np.uint16)):
        raise ValueError(
            f"ND2 export supports uint8/uint16 sources, not {dtype} -- "
            "use format=tiff for other dtypes"
        )
    return dtype.itemsize * 8


def write_nd2(
    out_path: str | Path,
    get_band: Callable[[int, int], np.ndarray],
    *,
    height: int,
    width: int,
    dtype: np.dtype,
    pixel_size_um: float,
    planes: list[dict[str, Any]],
    magnification: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Stream a (Y, X, C) image into a new ND2 file, one row band at a time.

    ``get_band(y, h)`` must return a component-interleaved ``(h, width, C)``
    (or ``(h, width)`` for C=1) band of raw pixel values; it is called with
    ``h <= TILE``, so memory stays bounded by ``TILE x width``.  ``planes`` is
    one dict per component: ``{"name": str, "color": "RRGGBB"}``.
    """
    limnd2 = require_limnd2()
    bits = _check_dtype(np.dtype(dtype))
    n_comp = len(planes)

    out_path = Path(out_path)
    out_path.unlink(missing_ok=True)  # Nd2Writer appends to existing files

    attrs = limnd2.ImageAttributes.create(
        width=width,
        height=height,
        component_count=n_comp,
        bits=bits,
        sequence_count=1,
    )
    mf_kwargs: dict[str, Any] = {"pixel_calibration": float(pixel_size_um)}
    if magnification:
        mf_kwargs["objective_magnification"] = float(magnification)

    with limnd2.Nd2Writer(str(out_path)) as f:
        f.imageAttributes = attrs
        for y0 in range(0, height, TILE):
            th = min(TILE, height - y0)
            band = np.asarray(get_band(y0, th))
            if n_comp == 1 and band.ndim == 3:
                band = band[..., 0]
            for x0 in range(0, width, TILE):
                tw = min(TILE, width - x0)
                f.setImageTile(
                    0, x0, y0, np.ascontiguousarray(band[:, x0 : x0 + tw])
                )
            if on_progress:
                on_progress((y0 + th) / height)
        mf = limnd2.MetadataFactory(**mf_kwargs)
        for pl in planes:
            mf.addPlane(name=pl.get("name") or None, color=pl.get("color") or None)
        f.pictureMetadata = mf.createMetadata()


def _store_planes(attrs: dict[str, Any], channels: list[int]) -> list[dict[str, Any]]:
    chs = attrs["omero"]["channels"]
    return [{"name": chs[ci]["label"], "color": chs[ci]["color"]} for ci in channels]


def export_roi_nd2(
    root: Any,
    attrs: dict[str, Any],
    out_path: str | Path,
    level: int,
    x: int,
    y: int,
    w: int,
    h: int,
    channels: list[int],
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Export a region of one pyramid level of a converted store as ND2."""
    meta = attrs["nd2wsi"]
    levels = meta["levels"]
    arr = root[levels[level]["path"]]
    factor = levels[level]["downsample"]
    py, px = meta["pixel_size_um"]

    def get_band(ty: int, th: int) -> np.ndarray:
        block = np.asarray(arr[channels, y + ty : y + ty + th, x : x + w])
        return np.moveaxis(block, 0, -1)  # (C, h, w) -> (h, w, C)

    write_nd2(
        out_path,
        get_band,
        height=h,
        width=w,
        dtype=np.dtype(meta["dtype"]),
        pixel_size_um=px * factor,
        planes=_store_planes(attrs, channels),
        magnification=meta.get("objective_magnification"),
        on_progress=on_progress,
    )


def crop_nd2_to_nd2(
    nd2_path: str | Path,
    out_path: str | Path,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    selection: Any = None,
    channels: list[int] | None = None,
) -> dict[str, Any]:
    """Native-resolution ND2 -> ND2 crop, straight off the source memory map.

    No pyramid store needed: the source frame is sliced lazily (zero-copy for
    uncompressed files) and streamed into the writer tile by tile.  Returns a
    small summary dict for CLI reporting.
    """
    import nd2 as nd2lib

    from .reader import PlaneSelection, open_plane

    selection = selection or PlaneSelection()
    with nd2lib.ND2File(str(nd2_path)) as f:
        src = open_plane(f, selection, tile=TILE)
        c, H, W = src.shape
        x, y = max(0, min(x, W - 1)), max(0, min(y, H - 1))
        w, h = max(1, min(w, W - x)), max(1, min(h, H - y))
        chans = channels if channels else list(range(c))
        if any(not 0 <= ci < c for ci in chans):
            raise ValueError(f"channel out of range (file has C={c})")

        data = src.data  # (C, Y, X) dask array over the mmap

        def get_band(ty: int, th: int) -> np.ndarray:
            block = data[chans, y + ty : y + ty + th, x : x + w].compute()
            return np.moveaxis(np.asarray(block), 0, -1)

        py_um, px_um = src.pixel_size_um
        planes = [
            {
                "name": src.channels[ci].name,
                "color": "{:02X}{:02X}{:02X}".format(*src.channels[ci].color),
            }
            for ci in chans
        ]
        write_nd2(
            out_path,
            get_band,
            height=h,
            width=w,
            dtype=src.dtype,
            pixel_size_um=px_um,
            planes=planes,
            magnification=src.magnification,
        )
    return {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "channels": chans,
        "um": (w * px_um, h * py_um),
        "pixel_size_um": px_um,
    }
