"""ROI export back to ND2, via Laboratory Imaging's own ``limnd2`` writer.

The exported file is a plain modern ND2 (one uncompressed ``ImageDataSeq|0!``
frame) carrying the pixel calibration and the channel names/colors of the
source, so it reopens in NIS-Elements -- and in this tool -- like any other
acquisition.

Writes are streamed with ``Nd2Writer.setImageTile``, reading the source one
TILE x TILE tile at a time, so heap stays bounded by one tile regardless
of the ROI's width or height.

``limnd2`` is not on PyPI; it lives on Laboratory Imaging's own index:

    pip install --index-url https://pypi.laboratory-imaging.com/simple limnd2
"""

from __future__ import annotations

import os
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
    get_tile: Callable[[int, int, int, int], np.ndarray],
    *,
    height: int,
    width: int,
    dtype: np.dtype,
    pixel_size_um: float | None,
    planes: list[dict[str, Any]],
    magnification: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Stream a (Y, X, C) image into a new ND2 file, one tile at a time.

    ``get_tile(x, y, w, h)`` must return a component-interleaved
    ``(h, w, C)`` (or ``(h, w)`` for C=1) block of raw pixel values; it is
    called with ``w, h <= TILE``, so memory stays bounded by one tile
    whatever the ROI's dimensions. ``planes`` is one dict per component:
    ``{"name": str, "color": "RRGGBB"}``.
    """
    import uuid

    limnd2 = require_limnd2()
    bits = _check_dtype(np.dtype(dtype))
    n_comp = len(planes)

    # write to a sibling and rename in only when complete, so a failure
    # mid-write can never have destroyed an existing file first
    out_path = Path(out_path)
    partial = out_path.with_name(f"{out_path.name}.partial-{uuid.uuid4().hex[:8]}")

    attrs = limnd2.ImageAttributes.create(
        width=width,
        height=height,
        component_count=n_comp,
        bits=bits,
        sequence_count=1,
    )
    # an uncalibrated export leaves the writer at its own uncalibrated
    # default rather than stamping a fabricated micrometer value
    mf_kwargs: dict[str, Any] = {}
    if pixel_size_um is not None:
        mf_kwargs["pixel_calibration"] = float(pixel_size_um)
    if magnification:
        mf_kwargs["objective_magnification"] = float(magnification)

    try:
        _write_partial(limnd2, partial, get_tile, height, width, n_comp,
                       attrs, mf_kwargs, planes, on_progress)
        _validate_nd2(partial, width, height, n_comp)
        os.replace(partial, out_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _write_partial(
    limnd2: Any,
    partial: Path,
    get_tile: Callable[[int, int, int, int], np.ndarray],
    height: int,
    width: int,
    n_comp: int,
    attrs: Any,
    mf_kwargs: dict[str, Any],
    planes: list[dict[str, Any]],
    on_progress: Callable[[float], None] | None = None,
) -> None:
    with limnd2.Nd2Writer(str(partial)) as f:
        f.imageAttributes = attrs
        for y0 in range(0, height, TILE):
            th = min(TILE, height - y0)
            for x0 in range(0, width, TILE):
                tw = min(TILE, width - x0)
                block = np.asarray(get_tile(x0, y0, tw, th))
                if n_comp == 1 and block.ndim == 3:
                    block = block[..., 0]
                f.setImageTile(0, x0, y0, np.ascontiguousarray(block))
            if on_progress:
                on_progress((y0 + th) / height)
        mf = limnd2.MetadataFactory(**mf_kwargs)
        for pl in planes:
            mf.addPlane(name=pl.get("name") or None, color=pl.get("color") or None)
        f.pictureMetadata = mf.createMetadata()


def _scalar_calibration(pair: Any) -> float | None:
    """One scalar for the ND2 writer, or None when that would lie.

    The writer takes a single calibration value. Anisotropic pixels cannot
    be represented by one number, so past a 0.1% imbalance the export goes
    out uncalibrated with a warning instead of silently isotropizing.
    """
    if not pair:
        return None
    py, px = float(pair[0]), float(pair[1])
    if abs(px - py) > 0.001 * max(abs(px), abs(py), 1e-12):
        import warnings

        warnings.warn(
            f"anisotropic pixels ({px:.4f} x {py:.4f} um): the ND2 format "
            "takes one calibration scalar, so this export is uncalibrated",
            stacklevel=3,
        )
        return None
    return px


def _validate_nd2(path: Path, width: int, height: int, n_comp: int) -> None:
    """The finished file must reopen with the dimensions it was asked for."""
    import nd2 as nd2lib

    with nd2lib.ND2File(str(path)) as f:
        sizes = dict(f.sizes)
        got = (sizes.get("X"), sizes.get("Y"))
        if got != (width, height):
            raise RuntimeError(
                f"written ND2 reads back as {got}, expected {(width, height)}"
            )


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
    from .render import level_entry

    lv = level_entry(meta["levels"], level)
    arr = root[lv["path"]]
    factor = lv["downsample"]
    px = _scalar_calibration(meta.get("pixel_size_um"))

    def get_tile(tx: int, ty: int, tw: int, th: int) -> np.ndarray:
        block = np.asarray(
            arr[channels, y + ty : y + ty + th, x + tx : x + tx + tw]
        )
        return np.moveaxis(block, 0, -1)  # (C, h, w) -> (h, w, C)

    write_nd2(
        out_path,
        get_tile,
        height=h,
        width=w,
        dtype=np.dtype(meta["dtype"]),
        pixel_size_um=None if px is None else px * factor,
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

        def get_tile(tx: int, ty: int, tw: int, th: int) -> np.ndarray:
            block = data[
                chans, y + ty : y + ty + th, x + tx : x + tx + tw
            ].compute()
            return np.moveaxis(np.asarray(block), 0, -1)

        px_um = _scalar_calibration(src.pixel_size_um)
        py_um = px_um
        planes = [
            {
                "name": src.channels[ci].name,
                "color": "{:02X}{:02X}{:02X}".format(*src.channels[ci].color),
            }
            for ci in chans
        ]
        write_nd2(
            out_path,
            get_tile,
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
        "um": None if px_um is None else (w * px_um, h * py_um),
        "pixel_size_um": px_um,
    }
