"""Native OME-TIFF, TIFF, ImageJ hyperstack, and QPTIFF input.

Serves 2D tiled pyramidal TIFFs, exported ROI TIFFs, and untiled 2D TIFFs
directly with zero disk cache, and opens bounded multi-plane (Z/T) stacks
interactively with Z-slider, autofocus, and timeline support.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import render
from .contrast import auto_window
from .direct_common import (
    _TILE_CACHE,
    MAX_EXTRA,
    LeaseClosed,
    _Lifecycle,
    _parse_cyx_index,
    _Root,
    _TileCache,
    box_mean,
)
from .platform_io import BINARY, read_at
from .reader import ChannelInfo, PlaneSource, _finite_positive, level_shapes

OMETIFF_SUFFIXES = {".tif", ".tiff", ".qptiff"}

STACK_MIN_DIM = 8
STACK_MAX_DIM = 8192
STACK_MAX_FRAME_PX = 40_000_000
UNTILED_MAX_PIXELS = 256_000_000

PLATE_K = (2, 4, 8, 16)
THUMB_K = 8
HISTOGRAM_LRU = 64
PREFETCH_PENDING_MAX = 128
_MEMO_BAND_ROWS = 64

_FLUOROPHORE_PALETTE: tuple[tuple[int, int, int], ...] = (
    (0, 255, 255),    # Cyan
    (255, 0, 255),    # Magenta
    (255, 255, 0),    # Yellow
    (0, 255, 0),      # Green
    (0, 160, 255),    # Blue
    (255, 64, 64),    # Red
    (255, 128, 0),    # Orange
    (255, 255, 255),  # White
)

_ASSOCIATED_NAMES = frozenset({"thumbnail", "label", "macro", "overview", "mask"})


def is_ometiff(path: str | Path) -> bool:
    """True for .tif, .tiff (including .ome.tif/.ome.tiff), and .qptiff files."""
    return Path(path).suffix.lower() in OMETIFF_SUFFIXES


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _unit_to_um(value: Any, unit_raw: str | None) -> float | None:
    """Convert a positive physical size and unit string into micrometers."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0:
        return None
    if unit_raw is None:
        return None
    u = unit_raw.strip()
    if u in ("\\u00B5m", "\\u03BCm"):
        u = "um"
    u_low = u.lower()
    factors: dict[str, float] = {
        "nm": 1e-3,
        "nanometer": 1e-3,
        "nanometre": 1e-3,
        "nanometers": 1e-3,
        "um": 1.0,
        "µm": 1.0,
        "μm": 1.0,
        "micron": 1.0,
        "microns": 1.0,
        "micrometer": 1.0,
        "micrometre": 1.0,
        "micrometers": 1e0,
        "mm": 1e3,
        "millimeter": 1e3,
        "millimetre": 1e3,
        "millimeters": 1e3,
        "cm": 1e4,
        "centimeter": 1e4,
        "centimetre": 1e4,
        "centimeters": 1e4,
        "m": 1e6,
        "meter": 1e6,
        "metre": 1e6,
        "in": 25400.0,
        "inch": 25400.0,
        "inches": 25400.0,
    }
    factor = factors.get(u_low)
    if factor is None:
        return None
    um = val * factor
    return um if _finite_positive(um) else None


def _parse_ome_color(raw: Any) -> tuple[int, int, int] | None:
    """Decode OME-XML signed 32-bit RGBA integer or hex string into (R, G, B)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.startswith("#"):
        hex_part = text[1:]
        if len(hex_part) == 8:
            hex_part = hex_part[:6]
        if len(hex_part) == 6:
            try:
                rgb = (
                    int(hex_part[0:2], 16),
                    int(hex_part[2:4], 16),
                    int(hex_part[4:6], 16),
                )
                return None if rgb == (0, 0, 0) else rgb
            except ValueError:
                return None
    try:
        c_int = int(text)
    except ValueError:
        return None
    rgb_24 = (c_int >> 8) & 0xFFFFFF
    r = (rgb_24 >> 16) & 0xFF
    g = (rgb_24 >> 8) & 0xFF
    b = rgb_24 & 0xFF
    if (r, g, b) == (0, 0, 0):
        return None
    return (r, g, b)


def _parse_ome_xml(ome_xml: str | None) -> list[dict[str, Any]]:
    """Parse OME-XML into per-Image metadata records."""
    if not ome_xml:
        return []
    cleaned = ome_xml.strip().rstrip("\x00")
    if "<" not in cleaned:
        return []
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return []

    magnification: float | None = None
    for elem in root.iter():
        if _local_tag(elem.tag) == "Objective":
            mag_raw = elem.attrib.get("NominalMagnification") or elem.attrib.get("CalibratedMagnification")
            if _finite_positive(mag_raw):
                magnification = float(mag_raw)
                break

    images: list[dict[str, Any]] = []
    for img_idx, elem in enumerate(root.iter()):
        if _local_tag(elem.tag) != "Image":
            continue
        pixels = None
        for child in elem:
            if _local_tag(child.tag) == "Pixels":
                pixels = child
                break
        if pixels is None:
            continue

        px_unit = pixels.attrib.get("PhysicalSizeXUnit", "µm")
        py_unit = pixels.attrib.get("PhysicalSizeYUnit", px_unit)
        pz_unit = pixels.attrib.get("PhysicalSizeZUnit", "µm")

        has_px = "PhysicalSizeX" in pixels.attrib
        has_py = "PhysicalSizeY" in pixels.attrib
        px_um = _unit_to_um(pixels.attrib.get("PhysicalSizeX"), px_unit) if has_px else None
        py_um = _unit_to_um(pixels.attrib.get("PhysicalSizeY"), py_unit) if has_py else None
        if px_um is not None and py_um is None and not has_py:
            py_um = px_um
        elif py_um is not None and px_um is None and not has_px:
            px_um = py_um
        pixel_size_um = (py_um, px_um) if (px_um is not None and py_um is not None) else None

        z_step_um = (
            _unit_to_um(pixels.attrib.get("PhysicalSizeZ"), pz_unit)
            if "PhysicalSizeZ" in pixels.attrib
            else None
        )

        ch_records: list[dict[str, Any]] = []
        for pchild in pixels:
            if _local_tag(pchild.tag) == "Channel":
                cname = (
                    (pchild.attrib.get("Name") or "").strip()
                    or (pchild.attrib.get("Fluor") or "").strip()
                )
                ccolor = _parse_ome_color(pchild.attrib.get("Color"))
                spp_raw = pchild.attrib.get("SamplesPerPixel")
                try:
                    spp = int(spp_raw) if spp_raw is not None else 1
                except ValueError:
                    spp = 1
                ch_records.append({"name": cname, "color": ccolor, "samples": spp})

        def _int_attr(attr_name: str) -> int | None:
            raw = pixels.attrib.get(attr_name)
            try:
                return int(raw) if raw is not None else None
            except ValueError:
                return None

        images.append(
            {
                "name": (elem.attrib.get("Name") or "").strip() or f"Series {img_idx + 1}",
                "pixel_size_um": pixel_size_um,
                "z_step_um": z_step_um,
                "magnification": magnification,
                "channels": ch_records,
                "size_x": _int_attr("SizeX"),
                "size_y": _int_attr("SizeY"),
                "size_c": _int_attr("SizeC"),
                "size_z": _int_attr("SizeZ"),
                "size_t": _int_attr("SizeT"),
            }
        )
    return images


def _parse_qpi_page_meta(desc: str | None) -> dict[str, Any] | None:
    """Extract PerkinElmer/Akoya QPTIFF channel metadata from page XML."""
    if not desc or "PerkinElmer-QPI" not in desc:
        return None
    try:
        root = ET.fromstring(desc.strip().rstrip("\x00"))
    except ET.ParseError:
        return None
    out: dict[str, Any] = {"is_qpi": True}
    for elem in root.iter():
        tag = _local_tag(elem.tag)
        text = (elem.text or "").strip()
        if not text:
            continue
        if tag == "Name" and "name" not in out:
            out["name"] = text
        elif tag == "Biomarker" and "biomarker" not in out:
            out["biomarker"] = text
        elif tag == "Color" and "color" not in out:
            parts = [p.strip() for p in text.split(",")]
            if len(parts) == 3:
                try:
                    rgb = (int(parts[0]), int(parts[1]), int(parts[2]))
                    if all(0 <= v <= 255 for v in rgb) and rgb != (0, 0, 0):
                        out["color"] = rgb
                except ValueError:
                    pass
        elif tag == "Objective" and "magnification" not in out:
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
            if m and _finite_positive(m.group(1)):
                out["magnification"] = float(m.group(1))
    return out


def _tag_rational_float(page: Any, tag_name: str) -> float | None:
    tag = page.tags.get(tag_name)
    if tag is None:
        return None
    val = tag.value
    if isinstance(val, tuple) and len(val) == 2:
        num, den = val
        if den == 0:
            return None
        f = float(num) / float(den)
    else:
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
    return f if _finite_positive(f) else None


def _extract_tiff_tag_calibration(page: Any) -> tuple[tuple[float, float] | None, str]:
    """Read standard TIFF XResolution/YResolution + ResolutionUnit (CENTIMETER/INCH).

    Never invents 1.0 µm/px when ResolutionUnit is NONE (1) or missing.
    """
    res_unit_tag = page.tags.get("ResolutionUnit")
    if res_unit_tag is None:
        return None, "unknown"
    raw_unit = res_unit_tag.value
    unit_code = int(raw_unit) if isinstance(raw_unit, int) else getattr(raw_unit, "value", None)
    unit_name = str(getattr(raw_unit, "name", raw_unit)).upper()

    if unit_code == 3 or "CENTIMETER" in unit_name:
        um_per_unit = 10_000.0
        source = "tiff-resolution-cm"
    elif unit_code == 2 or "INCH" in unit_name:
        um_per_unit = 25_400.0
        source = "tiff-resolution-inch"
    else:
        return None, "unknown"

    x_res = _tag_rational_float(page, "XResolution")
    y_res = _tag_rational_float(page, "YResolution")
    if x_res is None or y_res is None:
        return None, "unknown"
    px_um = um_per_unit / x_res
    py_um = um_per_unit / y_res
    if not (_finite_positive(px_um) and _finite_positive(py_um)):
        return None, "unknown"
    return (py_um, px_um), source


def _extract_imagej_calibration(
    ij_meta: dict[str, Any] | None, page: Any
) -> tuple[tuple[float, float] | None, float | None, str]:
    """Extract (pixel_size_um, z_step_um, source) from ImageJ metadata + TIFF resolution."""
    if not isinstance(ij_meta, dict) or not ij_meta:
        return None, None, "unknown"

    unit_str = ij_meta.get("unit")
    info_text = str(ij_meta.get("Info") or ij_meta.get("info") or "")
    if not unit_str and info_text:
        m_unit = re.search(r"(?im)^\s*unit\s*[:=]\s*([^\r\n]+)", info_text)
        if m_unit:
            unit_str = m_unit.group(1).strip()

    pixel_size_um: tuple[float, float] | None = None
    cal_source = "unknown"
    if isinstance(unit_str, str) and unit_str.strip():
        x_res = _tag_rational_float(page, "XResolution")
        y_res = _tag_rational_float(page, "YResolution")
        if x_res is not None and y_res is not None:
            px_um = _unit_to_um(1.0 / x_res, unit_str)
            py_um = _unit_to_um(1.0 / y_res, unit_str)
            if px_um is not None and py_um is not None:
                pixel_size_um = (py_um, px_um)
                cal_source = "imagej"

    if pixel_size_um is None and info_text:
        mx = re.search(r"PhysicalSizeX\s*=\s*([0-9.eE+-]+)", info_text)
        my = re.search(r"PhysicalSizeY\s*=\s*([0-9.eE+-]+)", info_text)
        if mx and my:
            px_um = _unit_to_um(mx.group(1), unit_str or "µm")
            py_um = _unit_to_um(my.group(1), unit_str or "µm")
            if px_um is not None and py_um is not None:
                pixel_size_um = (py_um, px_um)
                cal_source = "imagej-info"

    z_step_um: float | None = None
    spacing = ij_meta.get("spacing")
    if spacing is not None and isinstance(unit_str, str):
        z_step_um = _unit_to_um(abs(float(spacing)) if _finite_positive(abs(float(spacing))) else None, unit_str)

    return pixel_size_um, z_step_um, cal_source


def _clean_imagej_label(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    m = re.match(r"^c:\d+/\d+\s*-\s*(.+)$", first_line, re.I)
    if m:
        return m.group(1).strip()
    return first_line


def _extract_imagej_channels(
    ij_meta: dict[str, Any] | None, n_channels: int
) -> tuple[list[str | None], list[tuple[int, int, int] | None]]:
    names: list[str | None] = [None] * n_channels
    colors: list[tuple[int, int, int] | None] = [None] * n_channels
    if not isinstance(ij_meta, dict) or n_channels <= 0:
        return names, colors

    labels = ij_meta.get("Labels")
    if isinstance(labels, (list, tuple)) and len(labels) >= n_channels:
        for i in range(n_channels):
            cleaned = _clean_imagej_label(labels[i])
            if cleaned:
                names[i] = cleaned

    luts = ij_meta.get("LUTs")
    if isinstance(luts, (list, tuple)) and len(luts) >= n_channels:
        for i in range(n_channels):
            lut = np.asarray(luts[i])
            if lut.shape == (3, 256):
                rgb = (int(lut[0, -1]), int(lut[1, -1]), int(lut[2, -1]))
                if rgb == (0, 0, 0):
                    peak = lut.max(axis=1)
                    rgb = (int(peak[0]), int(peak[1]), int(peak[2]))
                if rgb != (0, 0, 0):
                    colors[i] = rgb
    return names, colors


def _build_channels(
    n_channels: int,
    rgb: bool,
    ome_rec: dict[str, Any] | None,
    ij_meta: dict[str, Any] | None,
    qpi_pages: list[dict[str, Any] | None] | None = None,
) -> list[ChannelInfo]:
    if rgb:
        return [
            ChannelInfo("Red", (255, 0, 0)),
            ChannelInfo("Green", (0, 255, 0)),
            ChannelInfo("Blue", (0, 0, 255)),
        ][:n_channels]

    ome_chans = (ome_rec or {}).get("channels") or []
    ij_names, ij_colors = _extract_imagej_channels(ij_meta, n_channels)

    out: list[ChannelInfo] = []
    used_names: set[str] = set()
    for i in range(n_channels):
        name: str | None = None
        color: tuple[int, int, int] | None = None

        if i < len(ome_chans):
            if ome_chans[i].get("name"):
                name = str(ome_chans[i]["name"])
            color = ome_chans[i].get("color")

        if qpi_pages and i < len(qpi_pages) and qpi_pages[i]:
            qp = qpi_pages[i] or {}
            if not name:
                name = qp.get("biomarker") or qp.get("name")
            if color is None:
                color = qp.get("color")

        if not name and ij_names[i]:
            name = ij_names[i]
        if color is None and ij_colors[i] is not None:
            color = ij_colors[i]

        base_name = name or f"Ch{i + 1}"
        final_name = base_name
        if final_name in used_names:
            final_name = f"{base_name} ({i + 1})"
        used_names.add(final_name)

        if color is None:
            if n_channels == 1:
                color = (255, 255, 255)
            else:
                color = _FLUOROPHORE_PALETTE[i % len(_FLUOROPHORE_PALETTE)]

        out.append(ChannelInfo(final_name, color))
    return out


def _normalize_dtype(raw_dtype: Any) -> np.dtype:
    dt = np.dtype(raw_dtype).newbyteorder("=")
    if dt == np.dtype(np.bool_):
        return np.dtype(np.uint8)
    if dt in (np.dtype(np.uint8), np.dtype(np.uint16), np.dtype(np.float32), np.dtype(np.float64)):
        return dt
    if np.issubdtype(dt, np.integer):
        if dt.itemsize <= 1:
            return np.dtype(np.uint8)
        if dt.itemsize <= 2 and np.issubdtype(dt, np.unsignedinteger):
            return np.dtype(np.uint16)
        return np.dtype(np.float32)
    if np.issubdtype(dt, np.floating):
        return np.dtype(np.float32)
    raise NotImplementedError(f"unsupported TIFF pixel dtype {raw_dtype}")


@dataclass(frozen=True)
class _ParsedSeriesLayout:
    height: int
    width: int
    n_c: int
    n_z: int
    n_t: int
    rgb: bool
    samples_raw: int
    axes: str
    shape: tuple[int, ...]
    dtype: np.dtype
    raw_dtype: np.dtype


def _parse_series_axes(
    series: Any,
    ome_rec: dict[str, Any] | None = None,
    *,
    is_qptiff_container: bool = False,
) -> _ParsedSeriesLayout:
    shape = tuple(int(v) for v in series.shape)
    axes = str(getattr(series, "axes", "") or "")
    if len(axes) != len(shape):
        if len(shape) == 2:
            axes = "YX"
        elif len(shape) == 3 and shape[-1] in (3, 4):
            axes = "YXS"
        elif len(shape) == 3:
            axes = "CYX"
        else:
            raise NotImplementedError(f"unsupported TIFF series shape {shape} (axes={axes!r})")

    if "Y" not in axes or "X" not in axes:
        raise NotImplementedError(f"TIFF series lacks spatial Y/X axes (shape={shape}, axes={axes!r})")

    page0 = series.pages[0]
    photometric = int(getattr(page0, "photometric", 1))
    is_photo_rgb = photometric in (2, 6)  # RGB or YCbCr

    ome_chans = (ome_rec or {}).get("channels") or []
    ome_size_c = (ome_rec or {}).get("size_c")
    ome_size_z = (ome_rec or {}).get("size_z")

    # Resolve ambiguous 'Q' or 'I' axis produced by tifffile for plain multi-page TIFFs
    resolved_axes = list(axes)
    for idx, ax in enumerate(resolved_axes):
        if ax in ("Q", "I"):
            dim = shape[idx]
            if is_qptiff_container or _parse_qpi_page_meta(getattr(page0, "description", None)):
                resolved_axes[idx] = "C"
            elif ome_size_c is not None and ome_size_c == dim and (ome_size_z in (None, 1)):
                resolved_axes[idx] = "C"
            elif ome_size_z is not None and ome_size_z == dim:
                resolved_axes[idx] = "Z"
            elif "Z" not in resolved_axes:
                resolved_axes[idx] = "Z"
            elif "C" not in resolved_axes:
                resolved_axes[idx] = "C"
    axes_norm = "".join(resolved_axes)

    y_idx = axes_norm.index("Y")
    x_idx = axes_norm.index("X")
    height = shape[y_idx]
    width = shape[x_idx]

    n_z = shape[axes_norm.index("Z")] if "Z" in axes_norm else 1
    n_t = shape[axes_norm.index("T")] if "T" in axes_norm else 1

    has_c = "C" in axes_norm
    has_s = "S" in axes_norm
    c_dim = shape[axes_norm.index("C")] if has_c else 1
    s_dim = shape[axes_norm.index("S")] if has_s else 1

    if has_c and has_s and c_dim > 1 and s_dim > 1:
        raise NotImplementedError(
            f"TIFF series combining C={c_dim} and S={s_dim} is not supported"
        )

    samples_raw = s_dim if has_s else c_dim
    if has_s and not has_c:
        if is_photo_rgb and s_dim in (3, 4):
            rgb = True
            n_c = 3
        elif (
            s_dim in (3, 4)
            and axes_norm.endswith("S")
            and len(ome_chans) <= 1
            and "nd2wsi ROI from " not in (getattr(page0, "description", "") or "")
        ):
            rgb = True
            n_c = 3
        else:
            rgb = False
            n_c = s_dim
    elif has_c and not has_s:
        if c_dim in (3, 4) and is_photo_rgb and len(ome_chans) <= 1:
            rgb = True
            n_c = 3
        else:
            rgb = False
            n_c = c_dim
    else:
        rgb = False
        n_c = 1

    supported_axes = {"T", "Z", "C", "Y", "X", "S"}
    for idx, ax in enumerate(axes_norm):
        if ax not in supported_axes and shape[idx] > 1:
            raise NotImplementedError(f"unsupported multi-element TIFF axis {ax!r} in {axes!r}")

    raw_dt = np.dtype(series.dtype)
    norm_dt = _normalize_dtype(raw_dt)
    return _ParsedSeriesLayout(
        height=height,
        width=width,
        n_c=n_c,
        n_z=n_z,
        n_t=n_t,
        rgb=rgb,
        samples_raw=samples_raw,
        axes=axes_norm,
        shape=shape,
        dtype=norm_dt,
        raw_dtype=raw_dt,
    )


def _is_associated_series(series: Any) -> bool:
    name = str(getattr(series, "name", "") or "").strip().lower()
    if name in _ASSOCIATED_NAMES:
        return True
    page0 = series.pages[0] if getattr(series, "pages", None) else None
    if page0 is None:
        return True
    desc = (getattr(page0, "description", "") or "").strip().lower()
    for assoc in _ASSOCIATED_NAMES:
        if re.match(rf"^{assoc}\b", desc):
            return True
    subfile = int(getattr(page0, "subfiletype", 0) or 0)
    # FILETYPE.REDUCEDIMAGE = 1, MACRO/MASK bits > 1 when small
    if subfile not in (0, 1) and ("macro" in desc or "label" in desc):
        return True
    return False


def _group_logical_series(
    tf: Any, ome_images: list[dict[str, Any]], *, is_qptiff: bool = False
) -> list[tuple[Any, list[Any], dict[str, Any] | None]]:
    """Return [(base_series, level_objects, ome_record), ...] for real image scenes.

    Handles both SubIFD pyramids (``series.levels``) and multi-page pyramids where
    decreasing-resolution pages were written as separate top-level series without
    ``subfiletype=1``.
    """
    raw_series = [s for s in tf.series if getattr(s, "pages", None) and not _is_associated_series(s)]
    if not raw_series:
        raw_series = [s for s in tf.series if getattr(s, "pages", None)]
    if not raw_series:
        raise ValueError("TIFF file contains no readable image series")

    # Multi-image OME-TIFF: each series corresponds to an OME <Image> scene
    if len(ome_images) > 1 and len(raw_series) >= len(ome_images):
        grouped = []
        for idx in range(len(ome_images)):
            s = raw_series[idx]
            grouped.append((s, list(s.levels), ome_images[idx]))
        return grouped

    s0 = raw_series[0]
    ome0 = ome_images[0] if ome_images else None
    if len(s0.levels) > 1 or len(raw_series) == 1 or len(ome_images) > 1:
        if len(ome_images) <= 1 and len(s0.levels) > 1:
            return [(s0, list(s0.levels), ome0)]
        return [
            (s, list(s.levels), ome_images[i] if i < len(ome_images) else None)
            for i, s in enumerate(raw_series)
        ]

    # Single-level s0 with multiple top-level series and <=1 OME <Image>:
    # Check whether subsequent series form a decreasing-resolution pyramid of s0.
    try:
        base_layout = _parse_series_axes(s0, ome0, is_qptiff_container=is_qptiff)
    except NotImplementedError:
        return [(s, list(s.levels), None) for s in raw_series]

    if base_layout.n_z == 1 and base_layout.n_t == 1 and getattr(s0.pages[0], "is_tiled", False):
        pyr_levels = [s0]
        prev_h, prev_w = base_layout.height, base_layout.width
        base_aspect = base_layout.width / max(1, base_layout.height)
        non_pyramid: list[Any] = []
        for cand in raw_series[1:]:
            try:
                cl = _parse_series_axes(cand, None, is_qptiff_container=is_qptiff)
            except NotImplementedError:
                continue
            cand_aspect = cl.width / max(1, cl.height)
            aspect_ok = abs(cand_aspect - base_aspect) / max(base_aspect, 1e-6) <= 0.06
            if (
                cl.n_z == 1
                and cl.n_t == 1
                and cl.n_c == base_layout.n_c
                and cl.rgb == base_layout.rgb
                and cl.dtype == base_layout.dtype
                and cl.height < prev_h
                and cl.width < prev_w
                and aspect_ok
                and getattr(cand.pages[0], "is_tiled", False)
            ):
                pyr_levels.append(cand)
                prev_h, prev_w = cl.height, cl.width
            else:
                non_pyramid.append(cand)
        if len(pyr_levels) > 1 and not non_pyramid:
            return [(s0, pyr_levels, ome0)]

    return [
        (s, list(s.levels), ome_images[i] if i < len(ome_images) else None)
        for i, s in enumerate(raw_series)
    ]


def _resolve_metadata_for_series(
    tf: Any,
    series: Any,
    layout: _ParsedSeriesLayout,
    ome_rec: dict[str, Any] | None,
    *,
    series_index: int = 0,
) -> tuple[
    tuple[float, float] | None,
    float | None,
    str,
    float | None,
    list[ChannelInfo],
]:
    """Return (pixel_size_um, z_step_um, calibration_source, magnification, channels)."""
    page0 = series.pages[0]
    ij_meta = getattr(tf, "imagej_metadata", None) if series_index == 0 else None

    qpi_pages: list[dict[str, Any] | None] = []
    if not layout.rgb and len(series.pages) >= layout.n_c and layout.n_z == 1 and layout.n_t == 1:
        for ci in range(layout.n_c):
            qpi_pages.append(_parse_qpi_page_meta(getattr(series.pages[ci], "description", None)))
    elif not layout.rgb:
        qpi0 = _parse_qpi_page_meta(getattr(page0, "description", None))
        if qpi0:
            qpi_pages = [qpi0]

    pixel_size_um: tuple[float, float] | None = None
    z_step_um: float | None = None
    cal_source = "unknown"
    magnification: float | None = None

    # (a) OME-XML Pixels PhysicalSizeX / PhysicalSizeY
    if ome_rec is not None:
        if ome_rec.get("pixel_size_um") is not None:
            pixel_size_um = ome_rec["pixel_size_um"]
            cal_source = "ome-xml"
        if ome_rec.get("z_step_um") is not None:
            z_step_um = ome_rec["z_step_um"]
        if ome_rec.get("magnification") is not None:
            magnification = ome_rec["magnification"]

    # (b) ImageJ metadata + XResolution/YResolution
    if pixel_size_um is None and ij_meta:
        ij_px, ij_z, ij_src = _extract_imagej_calibration(ij_meta, page0)
        if ij_px is not None:
            pixel_size_um = ij_px
            cal_source = ij_src
        if z_step_um is None and ij_z is not None:
            z_step_um = ij_z
    elif z_step_um is None and ij_meta:
        _, ij_z, _ = _extract_imagej_calibration(ij_meta, page0)
        if ij_z is not None:
            z_step_um = ij_z

    # (c) Standard TIFF XResolution/YResolution + ResolutionUnit (CENTIMETER / INCH)
    if pixel_size_um is None:
        tag_px, tag_src = _extract_tiff_tag_calibration(page0)
        if tag_px is not None:
            pixel_size_um = tag_px
            cal_source = tag_src

    if magnification is None:
        for qp in qpi_pages:
            if qp and qp.get("magnification") is not None:
                magnification = qp["magnification"]
                break

    channels = _build_channels(
        layout.n_c,
        layout.rgb,
        ome_rec,
        ij_meta,
        qpi_pages=qpi_pages if any(qpi_pages) else None,
    )
    return pixel_size_um, z_step_um, cal_source, magnification, channels


def inspect_ometiff_series(path: str | Path) -> list[dict[str, Any]]:
    """Inspect series/scenes in a TIFF/OME-TIFF container without decoding pixels."""
    import tifffile

    path = Path(path)
    is_qp = path.suffix.lower() == ".qptiff"
    with tifffile.TiffFile(str(path)) as tf:
        ome_images = _parse_ome_xml(getattr(tf, "ome_metadata", None))
        grouped = _group_logical_series(tf, ome_images, is_qptiff=is_qp)
        out: list[dict[str, Any]] = []
        for i, (s, _lvs, ome_rec) in enumerate(grouped):
            try:
                layout = _parse_series_axes(s, ome_rec, is_qptiff_container=is_qp)
            except NotImplementedError:
                continue
            name = (
                (ome_rec or {}).get("name")
                or str(getattr(s, "name", "") or "").strip()
                or f"Series {i + 1}"
            )
            out.append(
                {
                    "index": i,
                    "name": name,
                    "width": int(layout.width),
                    "height": int(layout.height),
                    "channels": int(layout.n_c),
                    "zCount": int(layout.n_z),
                    "tCount": int(layout.n_t),
                }
            )
        return out


def is_ometiff_stack(path: str | Path, *, series: int = 0) -> bool:
    """True when the selected TIFF/OME-TIFF series is an interactive Z/T stack."""
    import tifffile

    try:
        path = Path(path)
        if not is_ometiff(path) or not path.is_file():
            return False
        is_qp = path.suffix.lower() == ".qptiff"
        with tifffile.TiffFile(str(path)) as tf:
            ome_images = _parse_ome_xml(getattr(tf, "ome_metadata", None))
            grouped = _group_logical_series(tf, ome_images, is_qptiff=is_qp)
            if not (0 <= series < len(grouped)):
                return False
            s, _lvs, ome_rec = grouped[series]
            layout = _parse_series_axes(s, ome_rec, is_qptiff_container=is_qp)
            if layout.n_z <= 1 and layout.n_t <= 1:
                return False
            if not (
                STACK_MIN_DIM <= layout.width <= STACK_MAX_DIM
                and STACK_MIN_DIM <= layout.height <= STACK_MAX_DIM
            ):
                return False
            return layout.width * layout.height <= STACK_MAX_FRAME_PX
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fast Positional Tile Readers for Direct 2D Pyramidal / Tiled TIFFs
# ---------------------------------------------------------------------------


def _decode_page_segment(page: Any, raw_bytes: bytes, seg: int, tables: dict[str, Any], out_dtype: np.dtype) -> np.ndarray:
    try:
        decoded = np.asarray(page.decode(raw_bytes, seg, **tables)[0])
    except ValueError as exc:
        if "imagecodecs" in str(exc).lower():
            raise RuntimeError(
                "reading compressed TIFF tiles requires 'imagecodecs': pip install imagecodecs"
            ) from exc
        raise
    if decoded.dtype == np.bool_:
        return decoded.astype(np.uint8) * 255
    return np.asarray(decoded, dtype=out_dtype)


def _contig_tile_reader(page: Any, fd: int, cache: _TileCache, tag: Any, out_dtype: np.dtype, keep_samples: int):
    """Cached window reader ``(y0, y1, x0, x1) -> (keep_samples, h, w)`` for a CONTIG tiled page."""
    h, w = int(page.imagelength), int(page.imagewidth)
    th, tw = int(page.tilelength), int(page.tilewidth)
    samples = int(page.samplesperpixel)
    across = -(-w // tw)
    offsets = page.dataoffsets
    counts = page.databytecounts
    tables = {
        "jpegtables": getattr(page, "jpegtables", None),
        "jpegheader": getattr(page, "jpegheader", None),
    }

    def _tile_cyx(ty: int, tx: int) -> np.ndarray:
        key = (tag, ty, tx)
        hit = cache.get(key)
        if hit is not None:
            return hit
        seg = ty * across + tx
        if seg >= len(counts) or counts[seg] == 0:
            block = np.zeros((keep_samples, th, tw), dtype=out_dtype)
        else:
            raw = read_at(fd, counts[seg], offsets[seg])
            dec = _decode_page_segment(page, raw, seg, tables, out_dtype).reshape(-1, tw, samples)[:th]
            block = np.ascontiguousarray(np.moveaxis(dec[..., :keep_samples], -1, 0))
        cache.put(key, block)
        return block

    def read(cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        out: np.ndarray | None = None
        for ty in range(y0 // th, -(-y1 // th)):
            for tx in range(x0 // tw, -(-x1 // tw)):
                t_blk = _tile_cyx(ty, tx)[cs]
                if out is None:
                    lead = t_blk.shape[:-2]
                    out = np.zeros((*lead, y1 - y0, x1 - x0), dtype=out_dtype)
                oy0, oy1 = max(y0, ty * th), min(y1, ty * th + th, h)
                ox0, ox1 = max(x0, tx * tw), min(x1, tx * tw + tw, w)
                out[..., oy0 - y0 : oy1 - y0, ox0 - x0 : ox1 - x0] = t_blk[
                    ..., oy0 - ty * th : oy1 - ty * th, ox0 - tx * tw : ox1 - tx * tw
                ]
        if out is not None:
            return out
        dummy = np.empty((keep_samples, 0, 0), dtype=out_dtype)[cs]
        return np.zeros((*dummy.shape[:-2], y1 - y0, x1 - x0), dtype=out_dtype)

    return read


def _planar_tile_reader(
    pages: list[Any],
    fd: int,
    cache: _TileCache,
    tag: Any,
    out_dtype: np.dtype,
    n_c: int,
):
    """Cached per-channel window reader for multi-page ``CYX`` or single-page ``SEPARATE`` TIFFs."""
    single_separate = len(pages) == 1 and n_c > 1
    page0 = pages[0]
    h, w = int(page0.imagelength), int(page0.imagewidth)
    th, tw = int(page0.tilelength), int(page0.tilewidth)
    ny = -(-h // th)
    nx = -(-w // tw)
    tiles_per_plane = ny * nx

    page_tables = [
        {
            "jpegtables": getattr(p, "jpegtables", None),
            "jpegheader": getattr(p, "jpegheader", None),
        }
        for p in pages
    ]

    def _channel_tile(ci: int, ty: int, tx: int) -> np.ndarray:
        key = (tag, ci, ty, tx)
        hit = cache.get(key)
        if hit is not None:
            return hit
        if single_separate:
            page = page0
            tables = page_tables[0]
            seg = ci * tiles_per_plane + ty * nx + tx
        else:
            page = pages[ci]
            tables = page_tables[ci]
            seg = ty * nx + tx
        offsets = page.dataoffsets
        counts = page.databytecounts
        if seg >= len(counts) or counts[seg] == 0:
            block = np.zeros((th, tw), dtype=out_dtype)
        else:
            raw = read_at(fd, counts[seg], offsets[seg])
            dec = _decode_page_segment(page, raw, seg, tables, out_dtype).reshape(-1, tw)[:th]
            block = np.ascontiguousarray(dec)
        cache.put(key, block)
        return block

    def read(cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        selected = np.arange(n_c)[cs]
        scalar = np.ndim(selected) == 0
        ch_list = [int(c) for c in np.atleast_1d(selected)]
        out = np.zeros((len(ch_list), y1 - y0, x1 - x0), dtype=out_dtype)
        if y1 <= y0 or x1 <= x0 or not ch_list:
            return out[0] if scalar else out
        for slot, ci in enumerate(ch_list):
            for ty in range(y0 // th, -(-y1 // th)):
                for tx in range(x0 // tw, -(-x1 // tw)):
                    t_yx = _channel_tile(ci, ty, tx)
                    oy0, oy1 = max(y0, ty * th), min(y1, ty * th + th, h)
                    ox0, ox1 = max(x0, tx * tw), min(x1, tx * tw + tw, w)
                    out[slot, oy0 - y0 : oy1 - y0, ox0 - x0 : ox1 - x0] = t_yx[
                        oy0 - ty * th : oy1 - ty * th, ox0 - tx * tw : ox1 - tx * tw
                    ]
        return out[0] if scalar else out

    return read


class _OmeTiledLevel:
    """One halving-ladder level backed by a tiled TIFF page/series."""

    def __init__(
        self,
        reader_fn: Any,
        n_c: int,
        h: int,
        w: int,
        extra: int,
        life: _Lifecycle,
        dtype: np.dtype,
    ):
        self._read = reader_fn
        self.shape = (n_c, h, w)
        self.dtype = np.dtype(dtype)
        self._extra = int(extra)
        self._life = life

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        e = self._extra
        with self._life:
            block = self._read(cs, y0 * e, y1 * e, x0 * e, x1 * e)
        if e > 1 and y1 > y0 and x1 > x0:
            block = box_mean(block, e, self.dtype)
        return block


class _CoarseMemoLevel:
    """Lazily memoized coarse overview level when a TIFF lacks deep pyramid SubIFDs."""

    def __init__(self, finer: Any, h: int, w: int, factor: int, life: _Lifecycle):
        self._finer = finer
        self._factor = int(factor)
        self._life = life
        self.shape = (finer.shape[0], h, w)
        self.dtype = np.dtype(finer.dtype)
        self._memo: np.ndarray | None = None
        self._lock = threading.Lock()

    def _whole(self) -> np.ndarray:
        with self._lock:
            if self._memo is None:
                c, h, w = self.shape
                f = self._factor
                fh, fw = self._finer.shape[1], self._finer.shape[2]
                memo = np.zeros((c, h, w), dtype=self.dtype)
                for r0 in range(0, h, _MEMO_BAND_ROWS):
                    r1 = min(h, r0 + _MEMO_BAND_ROWS)
                    fy0, fy1 = r0 * f, min(fh, r1 * f)
                    fx1 = min(fw, w * f)
                    band = np.asarray(self._finer[:, fy0:fy1, 0:fx1])
                    if fy1 - fy0 >= f and fx1 >= f:
                        reduced = box_mean(band, f, self.dtype)
                        rh, rw = reduced.shape[-2], reduced.shape[-1]
                        memo[:, r0 : r0 + rh, :rw] = reduced
                self._memo = memo
            return self._memo

    def release(self) -> None:
        self._memo = None

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        with self._life:
            if x0 == x1 or y0 == y1:
                return np.empty((self.shape[0], y1 - y0, x1 - x0), dtype=self.dtype)[cs]
            return self._whole()[:, y0:y1, x0:x1][cs].copy()


class _MemoryPyramidLevel:
    """Direct in-memory 2D pyramid level for untiled 2D TIFFs (instant ROI reopen)."""

    def __init__(self, base_cyx: np.ndarray, h: int, w: int, downsample: int, life: _Lifecycle):
        self._base = base_cyx
        self._downsample = int(downsample)
        self._life = life
        self.shape = (base_cyx.shape[0], h, w)
        self.dtype = base_cyx.dtype
        self._memo: np.ndarray | None = None
        self._lock = threading.Lock()

    def _reduce_window(self, cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        d = self._downsample
        if d == 1:
            return self._base[cs, y0:y1, x0:x1].copy()
        _, bh, bw = self._base.shape
        ky = min(d, bh)
        kx = min(d, bw)
        raw = self._base[cs, y0 * ky : min(bh, y1 * ky), x0 * kx : min(bw, x1 * kx)]
        if ky == d and kx == d and raw.shape[-2] >= d and raw.shape[-1] >= d:
            return np.ascontiguousarray(box_mean(raw, d, self.dtype))
        # Narrow edge axis already collapsed to 1 px
        mean = raw.astype(np.float32).mean(axis=(-2, -1), keepdims=True)
        mean = np.broadcast_to(mean, (*raw.shape[:-2], y1 - y0, x1 - x0))
        if np.issubdtype(self.dtype, np.integer):
            return np.rint(mean).astype(self.dtype)
        return mean.astype(self.dtype)

    def _whole(self) -> np.ndarray:
        with self._lock:
            if self._memo is None:
                c, h, w = self.shape
                memo = np.empty((c, h, w), dtype=self.dtype)
                for r0 in range(0, h, _MEMO_BAND_ROWS):
                    r1 = min(h, r0 + _MEMO_BAND_ROWS)
                    memo[:, r0:r1, :] = self._reduce_window(slice(None), r0, r1, 0, w)
                self._memo = memo
            return self._memo

    def release(self) -> None:
        self._memo = None

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        with self._life:
            if x0 == x1 or y0 == y1:
                return np.empty((self.shape[0], y1 - y0, x1 - x0), dtype=self.dtype)[cs]
            if self._downsample >= 8:
                return self._whole()[:, y0:y1, x0:x1][cs].copy()
            return self._reduce_window(cs, y0, y1, x0, x1)


def _series_2d_to_cyx(series: Any, layout: _ParsedSeriesLayout, *, z_idx: int = 0, t_idx: int = 0) -> np.ndarray:
    """Decode one 2D (C, H, W) plane from a TiffPageSeries in native byte order."""
    axes = layout.axes
    shape = layout.shape
    page0 = series.pages[0]
    page_ndim = len(page0.shape)
    lead_axes = axes[: len(axes) - page_ndim]
    lead_shape = shape[: len(shape) - page_ndim]
    n_lead = math.prod(lead_shape) if lead_shape else 1

    if lead_axes and len(series.pages) == n_lead and shape[len(lead_shape) :] == tuple(page0.shape):
        if "C" in lead_axes:
            planes = []
            for ci in range(layout.n_c):
                coords = []
                for ax in lead_axes:
                    if ax == "T":
                        coords.append(t_idx)
                    elif ax == "Z":
                        coords.append(z_idx)
                    elif ax == "C":
                        coords.append(ci)
                    else:
                        coords.append(0)
                p_flat = int(np.ravel_multi_index(coords, lead_shape))
                arr_p = np.asarray(series.pages[p_flat].asarray())
                if arr_p.dtype == np.bool_:
                    arr_p = arr_p.astype(np.uint8) * 255
                planes.append(np.asarray(arr_p, dtype=layout.dtype))
            return np.ascontiguousarray(np.stack(planes, axis=0))

        coords = []
        for ax in lead_axes:
            if ax == "T":
                coords.append(t_idx)
            elif ax == "Z":
                coords.append(z_idx)
            else:
                coords.append(0)
        p_flat = int(np.ravel_multi_index(coords, lead_shape))
        raw = np.asarray(series.pages[p_flat].asarray())
        return _page_array_to_cyx(raw, axes[len(lead_axes) :], layout)

    # Single-IFD contiguous hyperstack (e.g., ImageJ raw dataoffset) or single-page 2D
    dataoffset = getattr(series, "dataoffset", None)
    if dataoffset is not None and lead_axes and int(getattr(page0, "compression", 1)) == 1:
        frame_axes = axes[len(lead_axes) :]
        frame_shape = shape[len(lead_shape) :]
        elem_bytes = int(math.prod(frame_shape)) * layout.raw_dtype.itemsize
        fd = series.parent.filehandle._fh.fileno()
        if "C" in lead_axes:
            planes = []
            for ci in range(layout.n_c):
                coords = [
                    t_idx if ax == "T" else z_idx if ax == "Z" else ci if ax == "C" else 0
                    for ax in lead_axes
                ]
                idx_flat = int(np.ravel_multi_index(coords, lead_shape))
                buf = read_at(fd, elem_bytes, int(dataoffset) + idx_flat * elem_bytes)
                arr_p = np.frombuffer(buf, dtype=layout.raw_dtype).reshape(frame_shape)
                planes.append(np.asarray(arr_p, dtype=layout.dtype))
            return np.ascontiguousarray(np.stack(planes, axis=0))
        coords = [t_idx if ax == "T" else z_idx if ax == "Z" else 0 for ax in lead_axes]
        idx_flat = int(np.ravel_multi_index(coords, lead_shape))
        buf = read_at(fd, elem_bytes, int(dataoffset) + idx_flat * elem_bytes)
        raw = np.frombuffer(buf, dtype=layout.raw_dtype).reshape(frame_shape)
        return _page_array_to_cyx(raw, frame_axes, layout)

    # Fallback: decode full series and index (t, z)
    full = np.asarray(series.asarray())
    slc: list[Any] = []
    rem_axes = ""
    for ax in axes:
        if ax == "T":
            slc.append(t_idx)
        elif ax == "Z":
            slc.append(z_idx)
        else:
            slc.append(slice(None))
            rem_axes += ax
    sub = full[tuple(slc)]
    return _page_array_to_cyx(sub, rem_axes, layout)


def _page_array_to_cyx(arr: np.ndarray, axes: str, layout: _ParsedSeriesLayout) -> np.ndarray:
    if arr.dtype == np.bool_:
        arr = arr.astype(np.uint8) * 255
    # Squeeze any unit axes outside C/S/Y/X
    for i in range(len(axes) - 1, -1, -1):
        if axes[i] not in ("C", "S", "Y", "X") and arr.shape[i] == 1:
            arr = np.squeeze(arr, axis=i)
            axes = axes[:i] + axes[i + 1 :]
    if axes == "YX":
        cyx = arr[None, :, :]
    elif axes in ("CYX", "SYX"):
        cyx = arr[: layout.n_c]
    elif axes in ("YXS", "YXC"):
        cyx = np.moveaxis(arr[..., : layout.n_c], -1, 0)
    else:
        y_pos = axes.index("Y")
        x_pos = axes.index("X")
        c_pos = next((i for i, a in enumerate(axes) if a in ("C", "S")), None)
        if c_pos is None:
            cyx = np.moveaxis(arr, (y_pos, x_pos), (0, 1))[None, :, :]
        else:
            cyx = np.moveaxis(arr, (c_pos, y_pos, x_pos), (0, 1, 2))[: layout.n_c]
    return np.ascontiguousarray(cyx, dtype=layout.dtype)


def _compute_default_windows(
    sample_cyx: np.ndarray,
    dtype: np.dtype,
    rgb: bool,
    *,
    raw_bps: int | None = None,
) -> list[dict[str, float]]:
    if rgb and np.issubdtype(dtype, np.integer):
        hi = float(np.iinfo(dtype).max)
        end_val = (
            float((1 << raw_bps) - 1)
            if dtype == np.dtype(np.uint16) and isinstance(raw_bps, int) and 0 < raw_bps < 16
            else hi
        )
        return [{"start": 0.0, "end": end_val, "min": 0.0, "max": hi} for _ in range(sample_cyx.shape[0])]

    if np.issubdtype(dtype, np.integer):
        lo_bound, hi_bound = float(np.iinfo(dtype).min), float(np.iinfo(dtype).max)
    else:
        finite = sample_cyx[np.isfinite(sample_cyx)]
        if finite.size:
            lo_bound, hi_bound = float(finite.min()), float(finite.max())
        else:
            lo_bound, hi_bound = 0.0, 1.0
        if hi_bound <= lo_bound:
            hi_bound = lo_bound + 1.0

    if rgb:
        start, end = auto_window(sample_cyx, dtype=dtype, rgb=True)
        return [
            {
                "start": start,
                "end": end,
                "min": min(lo_bound, start),
                "max": max(hi_bound, end),
            }
            for _ in range(sample_cyx.shape[0])
        ]

    windows: list[dict[str, float]] = []
    for ci in range(sample_cyx.shape[0]):
        start, end = auto_window(sample_cyx[ci], dtype=dtype)
        windows.append(
            {
                "start": start,
                "end": end,
                "min": min(lo_bound, start),
                "max": max(hi_bound, end),
            }
        )
    return windows


def _tiled_level_reader_for(
    lv_obj: Any,
    fd: int,
    cache: _TileCache,
    tag: Any,
    layout: _ParsedSeriesLayout,
) -> tuple[Any, int, int] | None:
    """Build a positional tile reader for one pyramid level object if tiled."""
    pages = list(getattr(lv_obj, "pages", None) or [])
    if not pages or not getattr(pages[0], "is_tiled", False):
        return None
    p0 = pages[0]
    lh, lw = int(p0.imagelength), int(p0.imagewidth)
    if np.dtype(p0.dtype).newbyteorder("=") != layout.raw_dtype.newbyteorder("="):
        return None
    planar = int(getattr(p0, "planarconfig", 1))
    samples = int(getattr(p0, "samplesperpixel", 1))

    if len(pages) == 1 and planar == 1 and samples >= layout.n_c:
        reader = _contig_tile_reader(p0, fd, cache, tag, layout.dtype, layout.n_c)
        return reader, lh, lw
    if len(pages) == 1 and planar == 2 and samples >= layout.n_c:
        reader = _planar_tile_reader([p0], fd, cache, tag, layout.dtype, layout.n_c)
        return reader, lh, lw
    if len(pages) >= layout.n_c and all(
        getattr(p, "is_tiled", False)
        and int(p.imagelength) == lh
        and int(p.imagewidth) == lw
        and int(getattr(p, "samplesperpixel", 1)) == 1
        for p in pages[: layout.n_c]
    ):
        reader = _planar_tile_reader(pages[: layout.n_c], fd, cache, tag, layout.dtype, layout.n_c)
        return reader, lh, lw
    return None


def open_direct(
    path: str | Path,
    *,
    series: int = 0,
    tile: int = 512,
) -> tuple[_Root, dict[str, Any]]:
    """Open a 2D TIFF / OME-TIFF / QPTIFF directly with zero on-disk cache."""
    import tifffile

    from .convert import available_memory_bytes, build_group_attrs

    path = Path(path)
    is_qp = path.suffix.lower() == ".qptiff"
    tf = tifffile.TiffFile(str(path))
    fd = -1
    try:
        ome_images = _parse_ome_xml(getattr(tf, "ome_metadata", None))
        grouped = _group_logical_series(tf, ome_images, is_qptiff=is_qp)
        if not (0 <= series < len(grouped)):
            raise ValueError(f"series {series} out of range (0..{len(grouped) - 1})")
        base_series, level_objs, ome_rec = grouped[series]
        layout = _parse_series_axes(base_series, ome_rec, is_qptiff_container=is_qp)

        z_sel = layout.n_z // 2 if layout.n_z > 1 else 0
        t_sel = 0
        pixel_size_um, _z_step_um, cal_source, magnification, channels = _resolve_metadata_for_series(
            tf, base_series, layout, ome_rec, series_index=series
        )

        H, W = layout.height, layout.width
        shapes = level_shapes(H, W, tile)
        life = _Lifecycle()

        can_tile_direct = (
            layout.n_z == 1
            and layout.n_t == 1
            and getattr(base_series.pages[0], "is_tiled", False)
        )
        embedded: dict[int, tuple[Any, int, int]] = {}
        owner = f"ometiff:{path.resolve()}:s{series}"
        if can_tile_direct:
            fd = os.open(str(path), os.O_RDONLY | BINARY)
            _TILE_CACHE.purge(owner)
            ident = path.stat().st_mtime_ns
            for lv_obj in level_objs:
                p0 = lv_obj.pages[0]
                lh, lw = int(p0.imagelength), int(p0.imagewidth)
                d = max(1, round(H / max(1, lh)))
                k = d.bit_length() - 1
                on_ladder = (
                    2**k == d
                    and k < len(shapes)
                    and abs(shapes[k][0] - lh) <= 2
                    and abs(shapes[k][1] - lw) <= 2
                )
                if not on_ladder or k in embedded:
                    continue
                built = _tiled_level_reader_for(
                    lv_obj, fd, _TILE_CACHE, (owner, ident, k), layout
                )
                if built is not None:
                    embedded[k] = built

        memo_levels: list[Any] = []
        if 0 in embedded:
            levels: dict[str, Any] = {}
            served: list[tuple[int, int]] = []
            for k in range(len(shapes)):
                src_k = max(j for j in embedded if j <= k)
                e = 2 ** (k - src_k)
                reader_fn, sh, sw = embedded[src_k]
                if e <= MAX_EXTRA:
                    h_k, w_k = max(1, sh // e), max(1, sw // e)
                    levels[str(k)] = _OmeTiledLevel(
                        reader_fn, layout.n_c, h_k, w_k, e, life, layout.dtype
                    )
                    served.append((h_k, w_k))
                else:
                    # Chain from the previous level via bounded band memoization
                    prev_lv = levels[str(k - 1)]
                    h_k, w_k = max(1, prev_lv.shape[1] // 2), max(1, prev_lv.shape[2] // 2)
                    coarse = _CoarseMemoLevel(prev_lv, h_k, w_k, 2, life)
                    levels[str(k)] = coarse
                    memo_levels.append(coarse)
                    served.append((h_k, w_k))
            shapes = served
            raw_bps = getattr(base_series.pages[0], "bitspersample", None)
            if isinstance(raw_bps, (tuple, list)):
                raw_bps = raw_bps[0] if raw_bps else None
            coarsest = np.asarray(levels[str(len(shapes) - 1)][:, :, :])
            windows = _compute_default_windows(
                coarsest, layout.dtype, layout.rgb, raw_bps=raw_bps if isinstance(raw_bps, int) else None
            )
            notes = [
                f"served directly from tiled TIFF ({len(embedded)} embedded level(s), {W} x {H})"
            ]
            if not pixel_size_um:
                notes.append("no physical pixel calibration in TIFF metadata; measurements are in pixels")

            selection_meta: dict[str, Any] = {"p": series} if series > 0 or len(grouped) > 1 else {}
            src_info = PlaneSource(
                data=None,
                dtype=layout.dtype,
                shape=(layout.n_c, H, W),
                rgb=layout.rgb,
                channels=channels,
                pixel_size_um=pixel_size_um,
                source_name=str(path),
                selection=selection_meta,
                notes=notes,
                magnification=magnification,
                calibration_source=cal_source if pixel_size_um else "unknown",
            )
            attrs = build_group_attrs(src_info, shapes, tile, windows)
            attrs["nd2wsi"]["direct"] = True

            def _teardown_tiled() -> None:
                if not life.close():
                    return
                for ml in memo_levels:
                    ml.release()
                tf.close()
                if fd >= 0:
                    os.close(fd)
                _TILE_CACHE.purge(owner)

            return _Root(levels, _teardown_tiled), attrs

        # Untiled 2D TIFF (or mid-plane fallback of an oversized >8192 stack):
        if H * W > UNTILED_MAX_PIXELS:
            raise ValueError(
                f"untiled TIFF ({W} x {H}) exceeds {UNTILED_MAX_PIXELS // 1_000_000} MPx direct memory limit; "
                "save as a tiled TIFF or OME-TIFF pyramid"
            )
        plane_bytes = layout.n_c * H * W * layout.dtype.itemsize
        avail = available_memory_bytes()
        budget = (avail if avail is not None else render._available_memory()) // 2
        if plane_bytes * 2 > budget:
            raise ValueError(
                "Not enough memory to open this untiled TIFF at full resolution; close other slides and retry"
            )

        base_cyx = _series_2d_to_cyx(base_series, layout, z_idx=z_sel, t_idx=t_sel)
        raw_bps = getattr(base_series.pages[0], "bitspersample", None)
        if isinstance(raw_bps, (tuple, list)):
            raw_bps = raw_bps[0] if raw_bps else None
        tf.close()
        tf_closed = True

        mem_levels: dict[str, _MemoryPyramidLevel] = {
            str(k): _MemoryPyramidLevel(base_cyx, sh, sw, 2**k, life)
            for k, (sh, sw) in enumerate(shapes)
        }
        coarsest = np.asarray(mem_levels[str(len(shapes) - 1)][:, :, :])
        windows = _compute_default_windows(
            coarsest, layout.dtype, layout.rgb, raw_bps=raw_bps if isinstance(raw_bps, int) else None
        )
        notes = [f"served directly in memory from TIFF ({W} x {H})"]
        if layout.n_z > 1:
            notes.append(f"series has {layout.n_z} Z planes; showing plane z={z_sel}")
        if not pixel_size_um:
            notes.append("no physical pixel calibration in TIFF metadata; measurements are in pixels")

        selection_meta = {}
        if series > 0 or len(grouped) > 1:
            selection_meta["p"] = series
        if layout.n_z > 1:
            selection_meta["z"] = z_sel
        if layout.n_t > 1:
            selection_meta["t"] = t_sel

        src_info = PlaneSource(
            data=None,
            dtype=layout.dtype,
            shape=(layout.n_c, H, W),
            rgb=layout.rgb,
            channels=channels,
            pixel_size_um=pixel_size_um,
            source_name=str(path),
            selection=selection_meta,
            notes=notes,
            magnification=magnification,
            calibration_source=cal_source if pixel_size_um else "unknown",
        )
        attrs = build_group_attrs(src_info, shapes, tile, windows)
        attrs["nd2wsi"]["direct"] = True

        def _teardown_mem() -> None:
            if life.close():
                for ml in mem_levels.values():
                    ml.release()

        return _Root(mem_levels, _teardown_mem), attrs
    except BaseException:
        if not locals().get("tf_closed", False):
            tf.close()
        if fd >= 0:
            os.close(fd)
        raise


# ---------------------------------------------------------------------------
# Interactive Multi-Plane Stack Source (Z > 1 or T > 1, PlateSource-compatible)
# ---------------------------------------------------------------------------


def _sharpness_score(a: np.ndarray) -> float:
    x = np.asarray(a, dtype=np.float32)
    if x.ndim == 3:
        x = x.mean(axis=0)
    if x.ndim != 2 or x.shape[0] < 2 or x.shape[1] < 2:
        return 0.0
    gy = np.diff(x, axis=0)
    gx = np.diff(x, axis=1)
    energy = float(np.mean(gy * gy)) + float(np.mean(gx * gx))
    level = float(x.mean())
    if not math.isfinite(energy) or not math.isfinite(level):
        return 0.0
    return energy / (level * level + 1e-6)


class _StackArrayLevel:
    def __init__(self, array: np.ndarray):
        self._a = array
        self.shape = tuple(array.shape)
        self.dtype = array.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        return self._a[cs, y0:y1, x0:x1].copy()


class _StackReducedLevel:
    def __init__(self, source: Any, t: int, p: int, z: int, k: int):
        self._source = source
        self._tpz = (t, p, z)
        self._k = int(k)
        c, h, w = source.frame_shape
        self.shape = (c, max(1, h // self._k), max(1, w // self._k))
        self.dtype = source.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        t, p, z = self._tpz
        return self._source.reduced(t, p, z, self._k)[cs, y0:y1, x0:x1].copy()


_STACK_FRAME_CACHE = _TileCache(320 * 1024 * 1024)
_STACK_REDUCED_CACHE = _TileCache(256 * 1024 * 1024)


class _BaseInteractiveStackSource:
    """Shared interactive multi-plane Z/T stack source (PlateSource-compatible)."""

    def _init_common(self, path: str | Path, series: int, tile: int) -> None:
        import uuid

        self.path = Path(path).resolve()
        self.series_index = int(series)
        self.tile = int(tile)
        self.store = None
        self._open_generation = uuid.uuid4().hex
        self._owner = (str(self.path), self.series_index, self._open_generation)
        self._life = _Lifecycle()
        self._io_lock = threading.Lock()
        self._closed = False
        self._fg = 0
        self._hist_lock = threading.Lock()
        self._hist: OrderedDict[tuple[int, int, int], list[dict[str, Any]]] = OrderedDict()
        self._focus_lock = threading.Lock()
        self._focus_cache: dict[int, list[float]] = {}
        self._pf_lock = threading.Lock()
        self._pf_cv = threading.Condition(self._pf_lock)
        self._pf_queue: deque[tuple[int, int, int, int | None]] = deque()
        self._pf_set: set[tuple[int, int, int, int | None]] = set()
        self._pf_thread: threading.Thread | None = None

        st = os.stat(self.path)
        self._ident = (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))

    def _finalize_stack_attrs(
        self,
        *,
        source_label: str,
        include_selection_p: bool,
        channel_dtypes: list[str] | None = None,
    ) -> None:
        from .convert import build_group_attrs

        _, H, W = self.frame_shape
        shapes = level_shapes(H, W, self.tile)
        self.levels = [
            {"path": str(k), "width": w, "height": h, "downsample": 2**k}
            for k, (h, w) in enumerate(shapes)
        ]

        home_small = self.reduced(0, 0, self.z_home, 4)
        windows = _compute_default_windows(home_small, self.dtype, self.rgb)

        notes: list[str] = []
        if self.T > 1:
            notes.append(f"time series: {self.T} frames")
        if self.z_step_um:
            notes.append(f"{self.Z} z planes, step {self.z_step_um:g} µm")
        elif self.Z > 1:
            notes.append(f"{self.Z} z planes")
        if self.pixel_size_um is None:
            notes.append(f"no physical pixel calibration in {source_label} metadata; measurements are in pixels")
        notes.append(f"interactive stack served directly from {source_label}")

        selection_meta: dict[str, Any] = {"p": self.series_index} if include_selection_p else {}
        src_info = PlaneSource(
            data=None,
            dtype=self.dtype,
            shape=self.frame_shape,
            rgb=self.rgb,
            channels=self.channels,
            pixel_size_um=self.pixel_size_um,
            source_name=str(self.path),
            selection=selection_meta,
            notes=notes,
            magnification=self.magnification,
            calibration_source=self.calibration_source if self.pixel_size_um else "unknown",
            channel_dtypes=channel_dtypes,
        )
        attrs = build_group_attrs(src_info, shapes, self.tile, windows)
        meta = attrs["nd2wsi"]
        meta["direct"] = True
        meta["kind"] = "plate"
        meta["plate"] = {
            "annotationScope": "plane",
            "T": self.T,
            "P": self.P,
            "Z": self.Z,
            "sites": [dict(s) for s in self.sites],
            "rows": self.rows,
            "cols": self.cols,
            "zHome": self.z_home,
            "zStepUm": self.z_step_um,
            "bottomToTop": self.bottom_to_top,
            "timesMs": list(self.times_ms),
            "periodMs": self.period_ms,
            "exposureMs": self.exposure_ms,
            "frameW": W,
            "frameH": H,
        }
        self.attrs = attrs

    def _read_plane_cyx(self, t: int, p: int, z: int) -> np.ndarray:
        raise NotImplementedError

    def _close_backend_handles(self) -> None:
        pass

    def _check_source(self) -> None:
        if self._closed:
            raise ValueError("slide is closed")
        try:
            st = os.stat(self.path)
        except OSError as exc:
            raise ValueError(
                f"the source file is unreachable ({exc.strerror}); close and reopen the slide"
            ) from exc
        if (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns)) != self._ident:
            raise ValueError("the source file changed while it was open; close and reopen the slide")

    def frame(self, t: int, p: int, z: int) -> np.ndarray:
        if self._closed:
            raise ValueError("slide is closed")
        t, p, z = int(t), int(p), int(z)
        if not (0 <= t < self.T and 0 <= p < self.P and 0 <= z < self.Z):
            raise ValueError(f"frame ({t}, {p}, {z}) out of range")
        with self._life:
            self._check_source()
            key = (self._owner, "frame", t, p, z)
            hit = _STACK_FRAME_CACHE.get(key)
            if hit is not None:
                return hit
            self._fg += 1
            try:
                cyx = self._read_plane_cyx(t, p, z)
            finally:
                self._fg -= 1
            cyx = np.ascontiguousarray(cyx)
            if not self._closed:
                _STACK_FRAME_CACHE.put(key, cyx)
            return cyx

    def frame_view(self, t: int, p: int, z: int) -> np.ndarray:
        return self.frame(t, p, z)

    def reduced(self, t: int, p: int, z: int, k: int, *, persist: bool = True) -> np.ndarray:
        if self._closed:
            raise ValueError("slide is closed")
        k = int(k)
        if k not in PLATE_K:
            raise ValueError(f"k must be one of {PLATE_K}")
        t, p, z = int(t), int(p), int(z)
        with self._life:
            self._check_source()
            key = (self._owner, t, p, z, k)
            hit = _STACK_REDUCED_CACHE.get(key)
            if hit is not None:
                return hit
            full = self.frame(t, p, z)
            _, h, w = full.shape
            if h >= k and w >= k:
                out = np.ascontiguousarray(box_mean(full, k, self.dtype))
            else:
                out = np.ascontiguousarray(full[:, :: min(k, h), :: min(k, w)])
            if not self._closed:
                _STACK_REDUCED_CACHE.put(key, out)
            return out

    def root_for(self, t: int, p: int, z: int) -> _Root:
        levels: dict[str, Any] = {"0": _StackArrayLevel(self.frame(t, p, z))}
        for lv in self.levels[1:]:
            levels[lv["path"]] = _StackReducedLevel(self, t, p, z, int(lv["downsample"]))
        return _Root(levels, closer=None)

    def status(self) -> dict[str, Any]:
        total = self.T * self.P * self.Z
        return {
            "done": total,
            "total": total,
            "perT": [self.P * self.Z] * self.T,
            "path": None,
            "format": None,
            "building": False,
            "writer": False,
            "integrityErrors": 0,
            "repairPending": 0,
        }

    def focus_map(self) -> dict[str, Any]:
        with self._life:
            self._check_source()
            best: list[list[int]] = []
            measured_z: list[list[int]] = []
            complete: list[list[bool]] = []
            for t in range(self.T):
                with self._focus_lock:
                    scores = self._focus_cache.get(t)
                if scores is None:
                    scores = [
                        _sharpness_score(self.reduced(t, 0, z, THUMB_K))
                        for z in range(self.Z)
                    ]
                    with self._focus_lock:
                        self._focus_cache[t] = scores
                best_z = int(np.argmax(scores)) if scores else int(self.z_home)
                best.append([best_z])
                measured_z.append([self.Z])
                complete.append([True])
            return {
                "best": best,
                "measured": self.T * self.P,
                "total": self.T * self.P,
                "measuredZ": measured_z,
                "totalZ": self.Z,
                "complete": complete,
                "completeCount": self.T * self.P,
                "zHome": int(self.z_home),
                "scoring": {
                    "channelMode": "all-channels-mean-image",
                    "metric": "mean-squared-gradient-normalized-by-mean",
                },
            }

    def histogram(self, t: int, p: int, z: int) -> list[dict[str, Any]]:
        key = (int(t), int(p), int(z))
        with self._life:
            self._check_source()
            with self._hist_lock:
                hit = self._hist.get(key)
                if hit is not None:
                    self._hist.move_to_end(key)
                    return hit
        small = self.reduced(t, p, z, THUMB_K)
        attrs = dict(self.attrs)
        attrs["nd2wsi"] = {
            **self.attrs["nd2wsi"],
            "levels": [
                {
                    "path": "0",
                    "width": small.shape[2],
                    "height": small.shape[1],
                    "downsample": THUMB_K,
                }
            ],
        }
        hist = render.compute_histograms({"0": _StackArrayLevel(small)}, attrs, min_pixels=1)
        for h_item in hist:
            h_item["level"] = str(THUMB_K)
        with self._hist_lock:
            self._hist[key] = hist
            while len(self._hist) > HISTOGRAM_LRU:
                self._hist.popitem(last=False)
        return hist

    def render_frame(
        self,
        t: int,
        p: int,
        z: int,
        k: int,
        channels: list[int],
        fmt: str = "jpg",
        win: str | None = None,
        col: str | None = None,
    ) -> bytes:
        self._fg += 1
        try:
            region = self.reduced(t, p, z, k)
        finally:
            self._fg -= 1
        windows, colors = render.display_params(self.attrs)
        if col and not self.rgb:
            colors = _apply_col_override(colors, col)
        windows, gammas = render.parse_windows(win, windows)
        img = render.composite(region, channels, windows, colors, self.rgb, gammas)
        return render.encode_image(img, fmt)

    def prefetch(self, t: int, z: int, k: int) -> None:
        if k not in PLATE_K or self._closed:
            return
        wanted: list[tuple[int, int, int, int | None]] = []
        if t + 1 < self.T:
            wanted.append((t + 1, 0, z, int(k)))
        for z2 in (z + 1, z - 1):
            if 0 <= z2 < self.Z:
                wanted.append((t, 0, z2, int(k)))
        self._enqueue_prefetch(wanted)

    def prefetch_neighbors(self, t: int, p: int, z: int) -> None:
        if self.Z <= 1 or self._closed:
            return
        wanted: list[tuple[int, int, int, int | None]] = []
        for z2 in (int(z) - 1, int(z) + 1):
            if 0 <= z2 < self.Z:
                if _STACK_FRAME_CACHE.get((self._owner, "frame", int(t), int(p), z2)) is None:
                    wanted.append((int(t), int(p), z2, None))
        if wanted:
            self._enqueue_prefetch(wanted)

    def _enqueue_prefetch(self, items: list[tuple[int, int, int, int | None]]) -> None:
        try:
            with self._pf_cv:
                if self._closed:
                    return
                for item in items:
                    if item in self._pf_set:
                        continue
                    while len(self._pf_queue) >= PREFETCH_PENDING_MAX:
                        old = self._pf_queue.popleft()
                        self._pf_set.discard(old)
                    self._pf_queue.append(item)
                    self._pf_set.add(item)
                if self._pf_thread is None or not self._pf_thread.is_alive():
                    self._pf_thread = threading.Thread(
                        target=self._prefetch_worker, name="stack-prefetch", daemon=True
                    )
                    self._pf_thread.start()
                self._pf_cv.notify()
        except Exception:
            pass

    def _prefetch_worker(self) -> None:
        while True:
            with self._pf_cv:
                while not self._pf_queue and not self._closed:
                    self._pf_cv.wait()
                if self._closed:
                    self._pf_queue.clear()
                    self._pf_set.clear()
                    return
                t, p, z, k = self._pf_queue.popleft()
                self._pf_set.discard((t, p, z, k))
            while self._fg > 0 and not self._closed:
                time.sleep(0.015)
            if self._closed:
                return
            try:
                if k is None:
                    if _STACK_FRAME_CACHE.get((self._owner, "frame", t, p, z)) is None:
                        self.frame(t, p, z)
                else:
                    if _STACK_REDUCED_CACHE.get((self._owner, t, p, z, k)) is None:
                        self.reduced(t, p, z, k)
            except (LeaseClosed, ValueError, OSError):
                if self._closed:
                    return

    def close(self, timeout: float | None = 300.0, **_kw: Any) -> None:
        if self._closed:
            return
        self._closed = True
        with self._pf_cv:
            self._pf_queue.clear()
            self._pf_set.clear()
            self._pf_cv.notify_all()
        if self._life.close(timeout=timeout):
            self._close_backend_handles()
            _STACK_FRAME_CACHE.purge(self._owner)
            _STACK_REDUCED_CACHE.purge(self._owner)


class OmeTiffStackSource(_BaseInteractiveStackSource):
    """Interactive multi-plane Z/T stack source for OME-TIFF and ImageJ hyperstacks."""

    def __init__(self, path: str | Path, *, series: int = 0, tile: int = 512):
        import tifffile

        self._init_common(path, series, tile)
        is_qp = self.path.suffix.lower() == ".qptiff"
        self._tf = tifffile.TiffFile(str(self.path))
        try:
            ome_images = _parse_ome_xml(getattr(self._tf, "ome_metadata", None))
            grouped = _group_logical_series(self._tf, ome_images, is_qptiff=is_qp)
            if not (0 <= self.series_index < len(grouped)):
                raise ValueError(f"series {self.series_index} out of range (0..{len(grouped) - 1})")
            self._series, _lvs, ome_rec = grouped[self.series_index]
            self._layout = _parse_series_axes(self._series, ome_rec, is_qptiff_container=is_qp)

            self.T = max(1, int(self._layout.n_t))
            self.P = 1
            self.Z = max(1, int(self._layout.n_z))
            self.frame_shape = (self._layout.n_c, self._layout.height, self._layout.width)
            self.dtype = self._layout.dtype
            self.rgb = self._layout.rgb
            (
                self.pixel_size_um,
                self.z_step_um,
                self.calibration_source,
                self.magnification,
                self.channels,
            ) = _resolve_metadata_for_series(
                self._tf, self._series, self._layout, ome_rec, series_index=self.series_index
            )
            site_name = (
                (ome_rec or {}).get("name")
                or str(getattr(self._series, "name", "") or "").strip()
                or f"Series {self.series_index + 1}"
            )
            self.sites = [{"i": 0, "name": site_name, "row": 0, "col": 0, "stageUm": None}]
            self.rows = 1
            self.cols = 1
            self.z_home = self.Z // 2
            self.bottom_to_top = True if self.z_step_um is not None else None
            self.period_ms: float | None = None
            self.times_ms: list[float] = []
            self.exposure_ms: float | None = None

            self._finalize_stack_attrs(
                source_label="TIFF",
                include_selection_p=self.series_index > 0 or len(grouped) > 1,
            )
        except BaseException:
            self._tf.close()
            raise

    def _read_plane_cyx(self, t: int, _p: int, z: int) -> np.ndarray:
        with self._io_lock:
            return _series_2d_to_cyx(self._series, self._layout, z_idx=z, t_idx=t)

    def _close_backend_handles(self) -> None:
        with self._io_lock:
            self._tf.close()


class ContainerSceneStackSource(_BaseInteractiveStackSource):
    """Targeted single-series interactive Z/T stack source for multi-series LIF and CZI."""

    def __init__(self, path: str | Path, *, series: int, tile: int = 512):
        self._init_common(path, series, tile)
        suffix = self.path.suffix.lower()
        if suffix not in (".lif", ".czi"):
            raise ValueError(f"Unsupported container stack format {suffix}")
        self._backend = suffix[1:]
        self._fd = os.open(str(self.path), os.O_RDONLY | BINARY)
        try:
            file_size = os.fstat(self._fd).st_size
            if self._backend == "lif":
                ch_dtypes = self._init_lif_series(file_size)
                label = "LIF"
            else:
                ch_dtypes = self._init_czi_scene(file_size)
                label = "CZI"
            self._finalize_stack_attrs(
                source_label=label,
                include_selection_p=True,
                channel_dtypes=ch_dtypes,
            )
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            raise

    def _init_lif_series(self, file_size: int) -> list[str]:
        from .lif import _list_lif_series, _read_lif_header_and_blocks

        root, blocks = _read_lif_header_and_blocks(self._fd, file_size)
        all_series = _list_lif_series(root, blocks)
        if not (0 <= self.series_index < len(all_series)):
            raise ValueError(f"series {self.series_index} out of range (0..{len(all_series) - 1})")
        ser = all_series[self.series_index]
        self._lif_spec = ser
        if ser.tile_positions_px and ser.n_m > 1:
            width = max(tx + ser.width for tx, _ty in ser.tile_positions_px)
            height = max(ty + ser.height for _tx, ty in ser.tile_positions_px)
        else:
            width, height = ser.width, ser.height
        n_c = len(ser.channels)
        self.T = max(1, int(ser.n_t))
        self.P = 1
        self.Z = max(1, int(ser.n_z))
        self.frame_shape = (n_c, height, width)
        self.dtype = np.dtype(ser.dtype)
        self.rgb = bool(ser.rgb)
        self.channels = [ChannelInfo(ch.name, ch.color) for ch in ser.channels]
        self.pixel_size_um = ser.pixel_size_um
        self.calibration_source = ser.calibration_source
        self.magnification = ser.magnification
        self.sites = [
            {"i": 0, "name": ser.name or f"Series {self.series_index + 1}", "row": 0, "col": 0, "stageUm": None}
        ]
        self.rows = 1
        self.cols = 1
        self.z_home = self.Z // 2
        self.z_step_um = ser.z_step_um
        self.bottom_to_top = True if self.z_step_um is not None else None
        self.period_ms = None
        self.times_ms = []
        self.exposure_ms = None
        return [str(ch.dtype) for ch in ser.channels]

    def _init_czi_scene(self, file_size: int) -> list[str]:
        from .czi import (
            _extract_czi_metadata,
            _extract_czi_z_step_um,
            _level0_entries,
            _read_czi_header_and_index,
            _resolve_czi_entries_spec,
        )

        entries, xml_root = _read_czi_header_and_index(self._fd, file_size)
        lvl0 = _level0_entries(entries)
        s_values = sorted({e.s for e in lvl0})
        if not (0 <= self.series_index < len(s_values)):
            raise ValueError(f"series {self.series_index} out of range (0..{len(s_values) - 1})")
        s_target = s_values[self.series_index]
        s_entries = [e for e in lvl0 if e.s == s_target]

        x0 = min(e.x_start for e in s_entries)
        y0 = min(e.y_start for e in s_entries)
        width = max(e.x_start + e.x_size for e in s_entries) - x0
        height = max(e.y_start + e.y_size for e in s_entries) - y0

        dtype, rgb, channel_dtypes = _resolve_czi_entries_spec(s_entries)
        c_values = sorted({e.c for e in s_entries})
        z_values = sorted({e.z for e in s_entries})
        t_values = sorted({e.t for e in s_entries})
        n_c = 3 if rgb else len(c_values)

        self.T = max(1, len(t_values))
        self.P = 1
        self.Z = max(1, len(z_values))
        self.frame_shape = (n_c, height, width)
        self.dtype = np.dtype(dtype)
        self.rgb = bool(rgb)

        self._czi_origin = (x0, y0)
        self._czi_samples = 3 if rgb else 1
        self._czi_c_values = c_values
        self._czi_z_values = z_values
        self._czi_t_values = t_values
        by_tz: dict[tuple[int, int], list[Any]] = {}
        by_tzc: dict[tuple[int, int, int], list[Any]] = {}
        for sb in s_entries:
            by_tz.setdefault((sb.t, sb.z), []).append(sb)
            by_tzc.setdefault((sb.t, sb.z, sb.c), []).append(sb)
        self._czi_by_tz = by_tz
        self._czi_by_tzc = by_tzc

        pixel_size_um, cal_source, magnification, channels = _extract_czi_metadata(
            xml_root, n_c, self.rgb, c_values=c_values
        )
        self.channels = channels
        self.pixel_size_um = pixel_size_um
        self.calibration_source = cal_source
        self.magnification = magnification
        self.sites = [{"i": 0, "name": f"Scene {self.series_index + 1}", "row": 0, "col": 0, "stageUm": None}]
        self.rows = 1
        self.cols = 1
        self.z_home = self.Z // 2
        self.z_step_um = _extract_czi_z_step_um(xml_root)
        self.bottom_to_top = True if self.z_step_um is not None else None
        self.period_ms = None
        self.times_ms = []
        self.exposure_ms = None
        return channel_dtypes

    def _read_plane_cyx(self, t: int, _p: int, z: int) -> np.ndarray:
        if self._backend == "lif":
            from .lif import _read_lif_channel_window

            ser = self._lif_spec
            tile_coords = (
                ser.tile_positions_px if (ser.tile_positions_px and ser.n_m > 1) else [(0, 0)]
            )
            out = np.zeros(self.frame_shape, dtype=self.dtype)
            for ci, ch_spec in enumerate(ser.channels):
                for m_idx, (tx0, ty0) in enumerate(tile_coords):
                    patch = _read_lif_channel_window(
                        self._fd, ser, ch_spec, z, t, m_idx, 0, ser.height, 0, ser.width
                    )
                    out[ci, ty0 : ty0 + ser.height, tx0 : tx0 + ser.width] = patch
            return out

        from .czi import _read_subblock_window

        t_val = self._czi_t_values[t]
        z_val = self._czi_z_values[z]
        x0, y0 = self._czi_origin
        n_c, height, width = self.frame_shape
        if self.rgb:
            z_sbs = self._czi_by_tz.get((t_val, z_val), ())
            bg_fill = (
                (65535 if self.dtype == np.uint16 else (255 if self.dtype == np.uint8 else 0))
                if len(z_sbs) > 1
                else 0
            )
            out = np.full((3, height, width), bg_fill, dtype=self.dtype)
            for sb in z_sbs:
                sx0, sy0 = sb.x_start - x0, sb.y_start - y0
                patch = _read_subblock_window(
                    self._fd, sb, self.dtype, self._czi_samples, 0, sb.y_size, 0, sb.x_size
                )
                out[:, sy0 : sy0 + sb.y_size, sx0 : sx0 + sb.x_size] = np.moveaxis(patch, -1, 0)
            return out
        out = np.zeros((n_c, height, width), dtype=self.dtype)
        for ci, c_val in enumerate(self._czi_c_values):
            for sb in self._czi_by_tzc.get((t_val, z_val, c_val), ()):
                sx0, sy0 = sb.x_start - x0, sb.y_start - y0
                patch = _read_subblock_window(
                    self._fd, sb, self.dtype, 1, 0, sb.y_size, 0, sb.x_size
                )
                out[ci, sy0 : sy0 + sb.y_size, sx0 : sx0 + sb.x_size] = patch
        return out

    def _close_backend_handles(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def _apply_col_override(
    defaults: list[tuple[int, int, int]], col_param: str | None
) -> list[tuple[int, int, int]]:
    if not col_param:
        return defaults
    out = list(defaults)
    for i, tok in enumerate(col_param.split(",")):
        if i >= len(out):
            break
        clean = tok.strip().lstrip("#")
        if len(clean) == 6:
            try:
                out[i] = (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))
            except ValueError:
                continue
    return out


def collect_ometiff_info(path: str | Path) -> dict[str, Any]:
    """`nd2wsi info` summary payload for a TIFF / OME-TIFF / QPTIFF file."""
    import tifffile

    path = Path(path)
    is_qp = path.suffix.lower() == ".qptiff"
    with tifffile.TiffFile(str(path)) as tf:
        ome_images = _parse_ome_xml(getattr(tf, "ome_metadata", None))
        grouped = _group_logical_series(tf, ome_images, is_qptiff=is_qp)
        s0, level_objs, ome0 = grouped[0]
        layout = _parse_series_axes(s0, ome0, is_qptiff_container=is_qp)
        px_um, z_step_um, cal_src, mag, channels = _resolve_metadata_for_series(
            tf, s0, layout, ome0, series_index=0
        )
        p0 = s0.pages[0]
        fmt_label = (
            "OME-TIFF"
            if ome_images or getattr(tf, "is_ome", False)
            else "PerkinElmer QPTIFF"
            if is_qp
            else "ImageJ TIFF"
            if getattr(tf, "is_imagej", False)
            else "TIFF"
        )
        return {
            "file": str(path),
            "size_bytes": path.stat().st_size,
            "format": fmt_label,
            "series_count": len(grouped),
            "width": layout.width,
            "height": layout.height,
            "channels": layout.n_c,
            "z_planes": layout.n_z,
            "timepoints": layout.n_t,
            "dtype": str(layout.dtype),
            "rgb": layout.rgb,
            "tiled": bool(getattr(p0, "is_tiled", False)),
            "pyramid_levels": len(level_objs),
            "pixel_size_um": list(px_um) if px_um else None,
            "z_step_um": z_step_um,
            "calibration_source": cal_src,
            "magnification": mag,
            "channel_info": [
                {"name": ch.name, "color": "#{:02X}{:02X}{:02X}".format(*ch.color)}
                for ch in channels
            ],
        }
