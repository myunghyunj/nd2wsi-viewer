"""ROI/region export rendering, OME-TIFF streaming, scalebar dispatch, and image encoding.

Extracted from :mod:`nd2wsi.render` to keep core compositing and tile caching
separate from export serialization. Public and private helpers remain
re-exported on :mod:`nd2wsi.render`, and internal cross-calls resolve through
the facade so existing monkeypatch seams on ``nd2wsi.render`` stay intact.
"""

from __future__ import annotations

import io
import math
import re
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr as _xml_quoteattr

import numpy as np

_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

_OME_DTYPE_NAMES = {
    "int8": "int8",
    "int16": "int16",
    "int32": "int32",
    "uint8": "uint8",
    "uint16": "uint16",
    "uint32": "uint32",
    "float32": "float",
    "float64": "double",
    "bool": "bit",
}
_OME_SCHEMA = "http://www.openmicroscopy.org/Schemas/OME/2016-06"

RENDER_STRIP_ROWS = 1024


def _render_module() -> Any:
    """Resolve the sibling ``nd2wsi.render`` facade lazily.

    Keeps module import acyclic and preserves monkeypatch seams installed on
    ``nd2wsi.render``.
    """
    from . import render

    return render


# --------------------------------------------------------------------------
# Image encoding
# --------------------------------------------------------------------------


def encode_image(img: np.ndarray, fmt: str, quality: int = 87) -> bytes:
    if (
        fmt in ("jpg", "jpeg")
        and isinstance(img, np.ndarray)
        and img.dtype == np.uint8
        and img.size > 0
    ):
        try:
            import imagecodecs

            jpeg8 = getattr(imagecodecs, "jpeg8_encode", None)
            if jpeg8 is not None and getattr(
                getattr(imagecodecs, "JPEG8", None), "available", True
            ):
                return bytes(jpeg8(np.ascontiguousarray(img), level=int(quality)))
        except Exception:
            pass

    from PIL import Image

    buf = io.BytesIO()
    pil = Image.fromarray(img)
    if fmt in ("jpg", "jpeg"):
        pil.save(buf, format="JPEG", quality=quality)
    elif fmt == "png":
        pil.save(buf, format="PNG")
    else:
        raise ValueError(f"unsupported image format {fmt}")
    return buf.getvalue()


# --------------------------------------------------------------------------
# ROI export
# --------------------------------------------------------------------------


def _read_channel_region(
    root: Any, level_path: str, ci: int, x: int, y: int, w: int, h: int
) -> np.ndarray:
    """(h, w) region of channel ``ci`` from one pyramid level, clipped to bounds.

    Equal to ``_read_region(...)[ci]`` but asks the level for that channel
    alone, so a per-plane caller fetches (and, for reduced or corrected
    levels, decodes or recomputes) each channel once instead of all of them.
    """
    arr = root[level_path]
    _, H, W = arr.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 0), dtype=arr.dtype)
    return np.asarray(arr[ci, y0:y1, x0:x1])


def _roi_tile_iter(
    root: Any,
    level_path: str,
    x: int,
    y: int,
    w: int,
    h: int,
    channels: list[int],
    tile: int,
) -> Iterator[np.ndarray]:
    """Yield (tile, tile[, ...]) blocks of the ROI in tifffile's expected order.

    For multi-channel output (separate planes) tifffile expects all tiles of
    plane 0 row-major, then plane 1, ...; for a single plane just row-major.
    """
    read_channel = _render_module()._read_channel_region
    for ci in channels:
        for ty0 in range(0, h, tile):
            th = min(tile, h - ty0)
            for tx0 in range(0, w, tile):
                tw = min(tile, w - tx0)
                block = read_channel(root, level_path, ci, x + tx0, y + ty0, tw, th)
                if th < tile or tw < tile:
                    pad = np.zeros((tile, tile), dtype=block.dtype)
                    pad[:th, :tw] = block
                    block = pad
                yield block


def _roi_rgb_tile_iter(
    root: Any, level_path: str, x: int, y: int, w: int, h: int, tile: int
) -> Iterator[np.ndarray]:
    read_region = _render_module()._read_region
    for ty0 in range(0, h, tile):
        th = min(tile, h - ty0)
        for tx0 in range(0, w, tile):
            tw = min(tile, w - tx0)
            block = read_region(root, level_path, x + tx0, y + ty0, tw, th)
            block = np.moveaxis(block, 0, -1)  # (h, w, 3)
            if th < tile or tw < tile:
                pad = np.zeros((tile, tile, block.shape[-1]), dtype=block.dtype)
                pad[:th, :tw] = block
                block = pad
            yield block


def _clean_xml_text(value: Any) -> str:
    return _XML_ILLEGAL_RE.sub("", str(value))


def _rgb_to_ome_color_int(rgb_tuple: tuple[int, int, int]) -> int:
    r, g, b = (max(0, min(255, int(v))) for v in rgb_tuple)
    rgba = (r << 24) | (g << 16) | (b << 8) | 0xFF
    return rgba - (1 << 32) if rgba >= (1 << 31) else rgba


def _build_roi_ome_xml(
    *,
    source_name: str,
    summary_description: str,
    dtype: np.dtype,
    w: int,
    h: int,
    channels: list[int],
    attrs: dict[str, Any],
    px_um: float | None,
    py_um: float | None,
    rgb: bool,
    col: str | Sequence[str | None] | None = None,
) -> str:
    r = _render_module()
    ome_type = _OME_DTYPE_NAMES.get(np.dtype(dtype).name)
    if ome_type is None:
        return summary_description.encode("ascii", "backslashreplace").decode("ascii")

    clean_source = r._clean_xml_text(Path(source_name).name or source_name)
    clean_desc = r._clean_xml_text(summary_description)
    omero_channels = (attrs.get("omero") or {}).get("channels") or []
    default_colors: list[tuple[int, int, int]] = []
    labels: list[str] = []
    for idx in range(max(len(omero_channels), max(channels, default=-1) + 1)):
        if idx < len(omero_channels) and isinstance(omero_channels[idx], dict):
            ch_obj = omero_channels[idx]
            labels.append(r._clean_xml_text(ch_obj.get("label") or f"C{idx}"))
            default_colors.append(
                r._rgb_tuple_from_default(str(ch_obj.get("color") or "FFFFFF"))
            )
        else:
            labels.append(f"C{idx}")
            default_colors.append((255, 255, 255))
    resolved_colors = r.parse_colors(col, default_colors) if col is not None else default_colors

    if rgb:
        size_c = 3
        interleaved = "true"
        ch_meta_seed: list[tuple[str, int]] = [("RGB", -1)]
        ch_xml = '<Channel ID="Channel:0:0" SamplesPerPixel="3" Name="RGB"/>'
    else:
        size_c = len(channels)
        interleaved = "false"
        ch_meta_seed = []
        ch_parts = []
        for out_idx, ci in enumerate(channels):
            ch_label = labels[ci] if 0 <= ci < len(labels) else f"C{ci}"
            ch_rgb = resolved_colors[ci] if 0 <= ci < len(resolved_colors) else (255, 255, 255)
            color_int = r._rgb_to_ome_color_int(ch_rgb)
            ch_meta_seed.append((ch_label, color_int))
            ch_parts.append(
                f'<Channel ID="Channel:0:{out_idx}" SamplesPerPixel="1" '
                f'Name={_xml_quoteattr(ch_label)} Color="{color_int}"/>'
            )
        ch_xml = "".join(ch_parts)

    phys_xml = ""
    if (
        px_um is not None
        and py_um is not None
        and math.isfinite(px_um)
        and math.isfinite(py_um)
        and px_um > 0
        and py_um > 0
    ):
        phys_xml = (
            f' PhysicalSizeX="{px_um!r}" PhysicalSizeXUnit="µm"'
            f' PhysicalSizeY="{py_um!r}" PhysicalSizeYUnit="µm"'
        )

    if not rgb and len(channels) > 1:
        tiffdata_xml = "".join(
            f'<TiffData FirstC="{out_idx}" FirstZ="0" FirstT="0" IFD="{out_idx}" PlaneCount="1"/>'
            for out_idx in range(len(channels))
        )
    else:
        tiffdata_xml = '<TiffData IFD="0" PlaneCount="1"/>'

    uid = uuid.uuid5(
        uuid.NAMESPACE_OID,
        f"nd2wsi-ome:{clean_source}:{clean_desc}:{w}x{h}:{ome_type}:{rgb}:{ch_meta_seed}:{px_um}:{py_um}",
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<OME xmlns="{_OME_SCHEMA}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="{_OME_SCHEMA} {_OME_SCHEMA}/ome.xsd" UUID="urn:uuid:{uid}">'
        f'<Image ID="Image:0" Name={_xml_quoteattr(clean_source)}>'
        f"<Description>{_xml_escape(clean_desc)}</Description>"
        f'<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="{ome_type}" '
        f'BigEndian="false" '
        f'SizeX="{int(w)}" SizeY="{int(h)}" SizeC="{size_c}" SizeZ="1" SizeT="1" '
        f'Interleaved="{interleaved}"{phys_xml}>'
        f"{ch_xml}"
        f"{tiffdata_xml}"
        "</Pixels></Image></OME>"
    )
    return xml.encode("ascii", "xmlcharrefreplace").decode("ascii")


def export_roi_tiff(
    root: Any,
    attrs: dict[str, Any],
    out_file: Any,
    level: int,
    x: int,
    y: int,
    w: int,
    h: int,
    channels: list[int],
    on_progress=None,
    col: str | Sequence[str | None] | None = None,
) -> None:
    """Stream a raw-dtype tiled OME-TIFF of the ROI to a file object or path."""
    import tifffile

    r = _render_module()
    meta = attrs["nd2wsi"]
    # TIFF ImageDescription is ASCII, unlike the file path and HTTP download
    # name. Keep non-ASCII source names recoverable without rejecting exports.
    raw_source = str(meta["source"])
    source_description = raw_source.encode("ascii", "backslashreplace").decode("ascii")
    lv = r.level_entry(meta["levels"], level)
    level_path = lv["path"]
    tile = min(int(meta["tile"]), 512)
    dtype = np.dtype(meta["dtype"])
    factor = lv["downsample"]
    # pixels per centimeter at this level; an uncalibrated store gets no
    # resolution tags at all rather than tags built from an invented value
    px_um: float | None = None
    py_um: float | None = None
    res_kw: dict[str, Any] = {}
    if meta.get("pixel_size_um") and factor and factor > 0:
        try:
            py, px = meta["pixel_size_um"]
            cand_px = float(px) * factor
            cand_py = float(py) * factor
            if math.isfinite(cand_px) and math.isfinite(cand_py) and cand_px > 0 and cand_py > 0:
                px_um = cand_px
                py_um = cand_py
                res_kw = {
                    "resolution": (1e4 / px_um, 1e4 / py_um),
                    "resolutionunit": "CENTIMETER",
                }
        except (TypeError, ValueError):
            pass

    ny = -(-h // tile)
    nx = -(-w // tile)
    rgb = bool(meta["rgb"]) and channels == [0, 1, 2]
    total_tiles = ny * nx * (1 if rgb else len(channels))
    completed_tiles = 0

    def counted(it: Iterator[np.ndarray]) -> Iterator[np.ndarray]:
        nonlocal completed_tiles
        for block in it:
            completed_tiles += 1
            if on_progress and completed_tiles % 8 == 0 and completed_tiles < total_tiles:
                on_progress(completed_tiles / total_tiles)
            yield block

    summary = (
        f"nd2wsi ROI from {source_description} level {level} "
        f"(downsample {factor}) x={x} y={y} w={w} h={h}"
        + ("" if rgb else f" channels={channels}")
    )
    ome_description = r._build_roi_ome_xml(
        source_name=raw_source,
        summary_description=summary,
        dtype=dtype,
        w=w,
        h=h,
        channels=channels,
        attrs=attrs,
        px_um=px_um,
        py_um=py_um,
        rgb=rgb,
        col=col,
    )
    with tifffile.TiffWriter(out_file, bigtiff=True, byteorder="<") as tw:
        if rgb:
            tw.write(
                counted(r._roi_rgb_tile_iter(root, level_path, x, y, w, h, tile)),
                shape=(h, w, 3),
                dtype=dtype,
                tile=(tile, tile),
                photometric="rgb",
                **res_kw,
                compression="zlib",
                description=ome_description,
                metadata=None,
            )
        else:
            for out_idx, ci in enumerate(channels):
                page_kw: dict[str, Any] = {
                    "shape": (h, w),
                    "dtype": dtype,
                    "tile": (tile, tile),
                    "photometric": "minisblack",
                    "contiguous": False,
                    **res_kw,
                    "compression": "zlib",
                    "metadata": None,
                }
                if out_idx == 0:
                    page_kw["description"] = ome_description
                tw.write(
                    counted(r._roi_tile_iter(root, level_path, x, y, w, h, [ci], tile)),
                    **page_kw,
                )
    if on_progress and total_tiles > 0:
        on_progress(1.0)


def export_roi_rendered(
    root: Any,
    attrs: dict[str, Any],
    level: int,
    x: int,
    y: int,
    w: int,
    h: int,
    channels: list[int],
    fmt: str,
    win: str | None = None,
    col: str | Sequence[str | None] | None = None,
    *,
    scale_bar: bool = False,
) -> bytes:
    """Render a region to PNG/JPEG or a PNG-backed SVG, strip by strip.

    Compositing goes through float32, which at hundreds of megapixels used
    to mean several times the output size in transients. Each strip is
    composited alone into one preallocated uint8 canvas, so the peak is
    the canvas plus one strip — the output buffers are checked against a
    quarter of installed memory before anything is read. Encoding still
    needs the whole image; this is bounded, not streaming.
    """
    r = _render_module()
    meta = attrs["nd2wsi"]
    lv = r.level_entry(meta["levels"], level)
    windows, colors = r.display_params(attrs, col=col)
    windows, gammas = r.parse_windows(win, windows)

    bar = None
    if scale_bar or fmt == "svg":
        from .scalebar import layout

        bar = layout(w, h, meta.get("pixel_size_um"), lv["downsample"])

    canvas_bytes = h * w * 3
    budget = r._available_memory() // 2
    # SVG additionally holds the lossless PNG and its base64/XML representation.
    required_bytes = canvas_bytes * (4 if fmt == "svg" else 1)
    if required_bytes > budget:
        raise ValueError(
            f"a {w} x {h} render needs {required_bytes / 1e9:.1f} GB for its "
            f"output buffers but only {budget / 1e9:.1f} GB is safely available — "
            "export a coarser level, or use TIFF, which streams"
        )

    canvas = np.empty((h, w, 3), np.uint8)
    strip_rows = r.RENDER_STRIP_ROWS
    read_region = r._read_region
    composite_fn = r.composite
    for y0 in range(0, h, strip_rows):
        th = min(strip_rows, h - y0)
        region = read_region(root, lv["path"], x, y + y0, w, th)
        canvas[y0 : y0 + th] = composite_fn(
            region, channels, windows, colors, meta["rgb"], gammas
        )
    if bar is not None:
        from PIL import Image

        from . import scalebar

        if fmt == "svg":
            return scalebar.svg(
                r.encode_image(canvas, "png"),
                w,
                h,
                bar,
                roi={"level": level, "x": x, "y": y, "width": w, "height": h},
            )
        if fmt in ("jpg", "jpeg"):
            return scalebar.jpeg(Image.fromarray(canvas), bar)
        raise ValueError("Scale bar export supports SVG and JPEG")
    return r.encode_image(canvas, fmt, quality=92)


def _available_memory() -> int:
    """Bytes this process can reasonably claim: half of installed memory.

    The same meaning on every OS. The probe is cached and starts no process,
    so an export pays nothing for it.
    """
    return _render_module().total_physical_memory_bytes() // 2
