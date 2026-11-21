"""OME-TIFF, QPTIFF, ImageJ, and TIFF metadata, calibration, channel, and axis parsing."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .reader import ChannelInfo, _finite_positive

OMETIFF_SUFFIXES = {".tif", ".tiff", ".qptiff"}

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

_MAX_XML_BYTES = 32 * 1024 * 1024
_DTD_TEXT_RE = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


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


def _parse_ome_color(raw: Any, *, rgb24: bool = False) -> tuple[int, int, int] | None:
    """Decode OME-XML signed 32-bit RGBA integer (or 24-bit 0x00RRGGBB) or hex string into (R, G, B)."""
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
    if rgb24 and 0 <= c_int <= 0xFFFFFF:
        rgb_24 = c_int & 0xFFFFFF
    else:
        rgb_24 = (c_int >> 8) & 0xFFFFFF
    r = (rgb_24 >> 16) & 0xFF
    g = (rgb_24 >> 8) & 0xFF
    b = rgb_24 & 0xFF
    if (r, g, b) == (0, 0, 0):
        return None
    return (r, g, b)


def _pixels_uses_rgb24_colors(raw_colors: list[str | None]) -> bool:
    """Detect unsigned 24-bit 0x00RRGGBB channel colors (e.g. Nikon NIS-Elements OME-XML).

    Standard OME-XML packs 32-bit RGBA ``(R << 24) | (G << 16) | (B << 8) | A`` with
    ``A == 0xFF`` (negative signed int32 whenever ``R >= 128``, ``> 0xFFFFFF`` whenever
    ``1 <= R <= 127``, and low byte ``0xFF`` whenever ``R == 0``). NIS-Elements writes
    unsigned 24-bit ``0x00RRGGBB`` integers in ``0..0xFFFFFF`` where non-blue channels
    have low byte ``!= 0xFF`` (e.g. ``FITC = 0x24FF00``, ``TxRed = 0xFF0000``).
    """
    ints: list[int] = []
    for raw in raw_colors:
        if raw is None:
            continue
        t = str(raw).strip()
        if not t or t.startswith("#"):
            continue
        try:
            ints.append(int(t))
        except ValueError:
            return False
    if not ints:
        return False
    if any(c < 0 or c > 0xFFFFFF for c in ints):
        return False
    nonzero = [c for c in ints if c > 0]
    return bool(nonzero) and any((c & 0xFF) != 0xFF for c in nonzero)


def _objective_mag(elem: ET.Element) -> float | None:
    mag_raw = elem.attrib.get("NominalMagnification") or elem.attrib.get("CalibratedMagnification")
    return float(mag_raw) if _finite_positive(mag_raw) else None


def _parse_ome_xml(ome_xml: str | None) -> list[dict[str, Any]]:
    """Parse OME-XML into per-Image metadata records."""
    if not ome_xml or len(ome_xml) > _MAX_XML_BYTES:
        return []
    cleaned = ome_xml.strip().rstrip("\x00")
    if "<" not in cleaned or _DTD_TEXT_RE.search(cleaned):
        return []
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return []

    objectives_by_id: dict[str, float] = {}
    instruments_by_id: dict[str, float] = {}
    default_magnification: float | None = None
    for elem in root.iter():
        tag = _local_tag(elem.tag)
        if tag == "Instrument":
            inst_id = (elem.attrib.get("ID") or "").strip()
            for sub in elem.iter():
                if _local_tag(sub.tag) == "Objective":
                    mag = _objective_mag(sub)
                    if mag is not None:
                        obj_id = (sub.attrib.get("ID") or "").strip()
                        if obj_id and obj_id not in objectives_by_id:
                            objectives_by_id[obj_id] = mag
                        if inst_id and inst_id not in instruments_by_id:
                            instruments_by_id[inst_id] = mag
                        if default_magnification is None:
                            default_magnification = mag
        elif tag == "Objective":
            mag = _objective_mag(elem)
            if mag is not None:
                obj_id = (elem.attrib.get("ID") or "").strip()
                if obj_id and obj_id not in objectives_by_id:
                    objectives_by_id[obj_id] = mag
                if default_magnification is None:
                    default_magnification = mag

    images: list[dict[str, Any]] = []
    for elem in root.iter():
        if _local_tag(elem.tag) != "Image":
            continue
        pixels = None
        img_obj_id: str | None = None
        img_inst_id: str | None = None
        for child in elem:
            ctag = _local_tag(child.tag)
            if ctag == "Pixels" and pixels is None:
                pixels = child
            elif ctag == "ObjectiveSettings" and img_obj_id is None:
                img_obj_id = (child.attrib.get("ID") or "").strip() or None
            elif ctag == "InstrumentRef" and img_inst_id is None:
                img_inst_id = (child.attrib.get("ID") or "").strip() or None
        if pixels is None:
            continue

        magnification: float | None = None
        if img_obj_id and img_obj_id in objectives_by_id:
            magnification = objectives_by_id[img_obj_id]
        elif img_inst_id and img_inst_id in instruments_by_id:
            magnification = instruments_by_id[img_inst_id]
        else:
            magnification = default_magnification

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

        ch_elems = [pchild for pchild in pixels if _local_tag(pchild.tag) == "Channel"]
        rgb24_colors = _pixels_uses_rgb24_colors([c.attrib.get("Color") for c in ch_elems])
        ch_records: list[dict[str, Any]] = []
        for pchild in ch_elems:
            cname = (
                (pchild.attrib.get("Name") or "").strip()
                or (pchild.attrib.get("Fluor") or "").strip()
            )
            ccolor = _parse_ome_color(pchild.attrib.get("Color"), rgb24=rgb24_colors)
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
                "name": (elem.attrib.get("Name") or "").strip() or f"Series {len(images) + 1}",
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
    if not desc or len(desc) > _MAX_XML_BYTES or "PerkinElmer-QPI" not in desc:
        return None
    if _DTD_TEXT_RE.search(desc):
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
        try:
            spacing_val = abs(float(spacing))
        except (TypeError, ValueError):
            spacing_val = None
        if spacing_val is not None and _finite_positive(spacing_val):
            z_step_um = _unit_to_um(spacing_val, unit_str)

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
        if dt.itemsize <= 1 and np.issubdtype(dt, np.unsignedinteger):
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

    use_s = has_s and (not has_c or s_dim > 1)
    use_c = has_c and not use_s
    samples_raw = s_dim if use_s else (c_dim if use_c else 1)
    if use_s:
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
    elif use_c:
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
