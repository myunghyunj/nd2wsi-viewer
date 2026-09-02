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
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np

from .reader import ChannelInfo, PlaneSource

SVS_SUFFIXES = {".svs"}
ASSOCIATED_IMAGE_NAMES = ("thumbnail", "label", "macro")
ASSOCIATED_MAX_PIXELS = 50_000_000
ASSOCIATED_MAX_DECODE_BYTES = 64 * 1024 * 1024
IMAGECODECS_HINT = (
    "reading SVS needs the 'imagecodecs' package for its JPEG tiles: "
    "pip install \"nd2wsi-viewer[svs]\"  (or: pip install imagecodecs)"
)


def is_svs(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SVS_SUFFIXES


def _associated_series_name(series: Any) -> str | None:
    """Map a tifffile auxiliary series to the public whitelist.

    Aperio normally supplies ``series.name``. Looking at the description
    first also handles mixed-case metadata and files where tifffile groups a
    macro after a label under a repeated generic series name.
    """
    page = series.pages[0]
    description = (page.description or "").strip().lower()
    for name in ASSOCIATED_IMAGE_NAMES:
        if re.match(rf"^{name}\b", description):
            return name
    series_name = str(getattr(series, "name", "") or "").strip().lower()
    return series_name if series_name in ASSOCIATED_IMAGE_NAMES else None


def associated_image_names(path: str | Path) -> list[str]:
    """Available whitelisted Aperio auxiliary images, in stable UI order."""
    import tifffile

    found = set()
    with tifffile.TiffFile(str(path)) as tf:
        for series in tf.series[1:]:
            name = _associated_series_name(series)
            if name is not None:
                found.add(name)
    return [name for name in ASSOCIATED_IMAGE_NAMES if name in found]


def _uint8_associated(array: np.ndarray) -> np.ndarray:
    if array.dtype == np.uint8:
        return array
    if array.dtype == np.bool_:
        return array.astype(np.uint8) * 255
    if np.issubdtype(array.dtype, np.integer):
        info = np.iinfo(array.dtype)
        scale = 255.0 / max(1, info.max - info.min)
        return np.clip((array.astype(np.float32) - info.min) * scale, 0, 255).astype(
            np.uint8
        )
    data = np.asarray(array, dtype=np.float32)
    finite = data[np.isfinite(data)]
    if not finite.size:
        return np.zeros(data.shape, dtype=np.uint8)
    lo, hi = float(finite.min()), float(finite.max())
    if 0 <= lo and hi <= 1:
        lo, hi = 0.0, 1.0
    data = np.nan_to_num(data, nan=lo, posinf=hi, neginf=lo)
    return np.clip((data - lo) * (255.0 / max(hi - lo, 1e-12)), 0, 255).astype(
        np.uint8
    )


def _validate_associated_shape(series: Any, name: str) -> None:
    """Reject unsupported or memory-heavy auxiliary series before decoding."""
    shape = tuple(int(value) for value in series.shape)
    if not shape or any(value <= 0 for value in shape):
        raise ValueError(f"unsupported associated image layout {shape}")
    axes = str(getattr(series, "axes", "") or "")
    channel_axis = None
    if len(axes) == len(shape):
        channel_axis = next(
            (
                index
                for index, axis_name in enumerate(axes)
                if axis_name.upper() in ("S", "C") and shape[index] in (1, 3, 4)
            ),
            None,
        )
    if channel_axis is None and len(shape) >= 3 and shape[-1] in (1, 3, 4):
        channel_axis = len(shape) - 1
    spatial = [
        value
        for index, value in enumerate(shape)
        if index != channel_axis and value != 1
    ]
    if len(spatial) != 2:
        raise ValueError(f"unsupported associated image layout {shape}")

    pixels = spatial[0] * spatial[1]
    samples = 1
    for value in shape:
        samples *= value
    decoded_bytes = samples * np.dtype(series.dtype).itemsize
    if (
        pixels > ASSOCIATED_MAX_PIXELS
        or decoded_bytes > ASSOCIATED_MAX_DECODE_BYTES
    ):
        raise ValueError(f"associated {name} image is unexpectedly large")


def associated_image_jpeg(
    path: str | Path, name: str, *, max_side: int = 512
) -> bytes:
    """Decode one whitelisted SVS auxiliary series as a bounded JPEG."""
    import tifffile
    from PIL import Image

    name = name.lower()
    if name not in ASSOCIATED_IMAGE_NAMES:
        raise KeyError(name)
    with tifffile.TiffFile(str(path)) as tf:
        series = next(
            (s for s in tf.series[1:] if _associated_series_name(s) == name),
            None,
        )
        if series is None:
            raise KeyError(name)
        _validate_associated_shape(series, name)
        array = np.asarray(series.asarray())
        axes = str(getattr(series, "axes", "") or "")
        orientation_tag = series.pages[0].tags.get("Orientation")
        orientation = int(orientation_tag.value) if orientation_tag is not None else 1

    if axes and len(axes) == array.ndim:
        for axis in range(array.ndim - 1, -1, -1):
            if array.shape[axis] == 1:
                array = np.squeeze(array, axis=axis)
                axes = axes[:axis] + axes[axis + 1 :]
    else:
        array = np.squeeze(array)
        axes = ""

    if array.ndim == 3:
        channel_axis = next(
            (
                i
                for i, axis_name in enumerate(axes)
                if axis_name.upper() in ("S", "C") and array.shape[i] in (1, 3, 4)
            ),
            None,
        )
        if channel_axis is None and array.shape[-1] in (1, 3, 4):
            channel_axis = array.ndim - 1
        if channel_axis is None:
            raise ValueError(f"unsupported associated image layout {array.shape}")
        array = np.moveaxis(array, channel_axis, -1)
        if array.shape[-1] == 1:
            array = array[..., 0]
    if array.ndim not in (2, 3) or (array.ndim == 3 and array.shape[-1] not in (3, 4)):
        raise ValueError(f"unsupported associated image layout {array.shape}")

    array = _uint8_associated(np.ascontiguousarray(array))
    image = Image.fromarray(array)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    transforms = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    if orientation in transforms:
        image = image.transpose(transforms[orientation])
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    image = image.convert("RGB")
    out = BytesIO()
    image.save(out, format="JPEG", quality=88)
    return out.getvalue()


def _require_imagecodecs() -> None:
    try:
        import imagecodecs  # noqa: F401
    except ImportError as e:
        raise RuntimeError(IMAGECODECS_HINT) from e


def _aperio_meta(description: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, pattern in (("mpp", r"\bMPP\s*=\s*([0-9.]+)"),
                         ("magnification", r"\bAppMag\s*=\s*([0-9.]+)")):
        m = re.search(pattern, description)
        if m:
            try:
                out[key] = float(m.group(1))
            except ValueError:  # malformed metadata such as "1.2.3"
                pass
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
    mpp = meta.get("mpp")
    calibrated = mpp is not None and mpp > 0
    h, w = int(series.shape[0]), int(series.shape[1])
    notes = [f"Aperio SVS baseline level ({w} x {h})"]
    if not calibrated:
        notes.append("no MPP in the Aperio metadata; measurements are in pixels")
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
        pixel_size_um=(float(mpp), float(mpp)) if calibrated else None,
        source_name=str(path),
        selection={},
        notes=notes,
        magnification=meta.get("magnification"),
        calibration_source="aperio-mpp" if calibrated else "unknown",
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
