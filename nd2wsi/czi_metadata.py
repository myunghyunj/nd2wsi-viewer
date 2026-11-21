"""Zeiss CZI XML metadata parsing and channel/calibration extraction."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Any

from .reader import ChannelInfo

_DEFAULT_PALETTE: tuple[tuple[int, int, int], ...] = (
    (0, 255, 0),
    (255, 0, 0),
    (0, 160, 255),
    (255, 255, 0),
    (255, 0, 255),
    (0, 255, 255),
    (255, 128, 0),
    (255, 255, 255),
)

_MAX_XML_BYTES = 32 * 1024 * 1024
_DTD_BYTES_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_CHANNEL_ID_RE = re.compile(r"(?:^|:)(\d+)$")


def _finite_positive(v: Any) -> bool:
    try:
        return v is not None and math.isfinite(float(v)) and float(v) > 0
    except (TypeError, ValueError):
        return False


def _parse_hex_color(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    text = raw.strip().lstrip("#")
    if len(text) == 8:
        text = text[2:]
    if len(text) != 6:
        return None
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
    except ValueError:
        return None
    if (r, g, b) == (0, 0, 0):
        return None
    return (r, g, b)


def _parse_czi_xml_bytes(xml_bytes: bytes) -> ET.Element | None:
    """Parse a CZI ZISRAWMETADATA XML payload, rejecting DTD/ENTITY declarations."""
    trimmed = xml_bytes.rstrip(b"\x00 \t\r\n")
    if not trimmed or _DTD_BYTES_RE.search(trimmed):
        return None
    try:
        return ET.fromstring(trimmed.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return None


def _extract_czi_z_step_um(xml_root: ET.Element | None) -> float | None:
    if xml_root is None:
        return None
    for dist in xml_root.findall(".//Scaling/Items/Distance"):
        if dist.get("Id") == "Z":
            val_text = dist.findtext("Value")
            if _finite_positive(val_text):
                um = float(val_text) * 1e6
                if _finite_positive(um):
                    return um
    return None


def _czi_channel_index(ch: ET.Element) -> int | None:
    start_c = (ch.get("StartC") or "").strip()
    if start_c.isdigit():
        return int(start_c)
    raw = (ch.get("Id") or "").strip()
    m = _CHANNEL_ID_RE.search(raw)
    return int(m.group(1)) if m else None


def _map_czi_channels(
    ch_list: list[ET.Element], actual_c_values: list[int]
) -> list[ET.Element | None]:
    n_channels = len(actual_c_values)
    if not ch_list or n_channels == 0:
        return [None] * n_channels
    indexed = [(_czi_channel_index(ch), ch) for ch in ch_list]
    if any(idx is not None for idx, _ in indexed):
        by_id: dict[int, ET.Element] = {}
        for idx, ch in indexed:
            if idx is not None and idx not in by_id:
                by_id[idx] = ch
        used: set[int] = set()
        mapped: list[ET.Element | None] = []
        for c_idx in actual_c_values:
            ch = by_id.get(c_idx) if c_idx not in used else None
            if ch is not None:
                used.add(c_idx)
            mapped.append(ch)
        if (
            any(m is None for m in mapped)
            and len(ch_list) == n_channels
            and len(set(actual_c_values)) == n_channels
            and actual_c_values == sorted(actual_c_values)
        ):
            xml_ids = [idx for idx, _ in indexed]
            if (
                all(idx is not None for idx in xml_ids)
                and len(set(xml_ids)) == n_channels
                and xml_ids == sorted(xml_ids)  # type: ignore[type-var]
            ):
                return list(ch_list)
        return mapped
    if (
        len(set(actual_c_values)) == n_channels
        and len(ch_list) > n_channels
        and all(0 <= c_idx < len(ch_list) for c_idx in actual_c_values)
    ):
        return [ch_list[c_idx] for c_idx in actual_c_values]
    if len(ch_list) == n_channels:
        return list(ch_list)
    return [None] * n_channels


def _first_nonempty(*values: str | None) -> str:
    for v in values:
        if v and v.strip():
            return v.strip()
    return ""


def _extract_czi_pixel_size_um(xml_root: ET.Element) -> tuple[tuple[float, float] | None, str]:
    # Pixel calibration in micrometers (CZI stores Distance Value in meters)
    scale_um: dict[str, float] = {}
    for dist in xml_root.findall(".//Scaling/Items/Distance"):
        axis = dist.get("Id")
        val_text = dist.findtext("Value")
        if axis in ("X", "Y") and _finite_positive(val_text):
            um = float(val_text) * 1e6
            if _finite_positive(um):
                scale_um[axis] = um
    if "X" in scale_um and "Y" in scale_um:
        return (scale_um["Y"], scale_um["X"]), "czi-scaling"
    return None, "unknown"


def _extract_czi_magnification(xml_root: ET.Element) -> float | None:
    for obj in xml_root.findall(".//Instrument/Objectives/Objective"):
        mag_text = obj.findtext("NominalMagnification")
        if _finite_positive(mag_text):
            return float(mag_text)
    return None


def _extract_czi_metadata(
    xml_root: ET.Element | None,
    n_channels: int,
    rgb: bool,
    c_values: list[int] | None = None,
) -> tuple[tuple[float, float] | None, str, float | None, list[ChannelInfo]]:
    actual_c_values = (
        list(c_values)
        if c_values is not None and not rgb and len(c_values) == n_channels
        else list(range(n_channels))
    )
    if rgb:
        channels = [
            ChannelInfo("Red", (255, 0, 0)),
            ChannelInfo("Green", (0, 255, 0)),
            ChannelInfo("Blue", (0, 0, 255)),
        ][:n_channels]
    else:
        channels = []

    if xml_root is None:
        if not channels:
            for slot_idx, c_idx in enumerate(actual_c_values):
                color = (
                    (255, 255, 255)
                    if n_channels == 1
                    else _DEFAULT_PALETTE[slot_idx % len(_DEFAULT_PALETTE)]
                )
                channels.append(ChannelInfo(f"Channel {c_idx}", color))
        return None, "unknown", None, channels

    # 1. Pixel calibration in micrometers (CZI stores Distance Value in meters)
    pixel_size_um, cal_source = _extract_czi_pixel_size_um(xml_root)

    # 2. Objective magnification
    magnification = _extract_czi_magnification(xml_root)

    # 3. Channels
    if not rgb:
        disp_channels = xml_root.findall(".//DisplaySetting/Channels/Channel")
        dim_channels = xml_root.findall(".//Information/Image/Dimensions/Channels/Channel")
        selected_disp = [
            ch for ch in disp_channels
            if (ch.get("IsSelected") or "true").strip().lower() != "false"
        ]
        if c_values is None and len(selected_disp) == n_channels and len(disp_channels) != n_channels:
            chosen = selected_disp
        else:
            chosen = disp_channels or dim_channels

        mapped_chosen = _map_czi_channels(chosen, actual_c_values)
        mapped_dim = _map_czi_channels(dim_channels, actual_c_values)

        for slot_idx, c_idx in enumerate(actual_c_values):
            default_name = f"Channel {c_idx}"
            name = default_name
            named = False
            color: tuple[int, int, int] | None = None
            ch = mapped_chosen[slot_idx]
            if ch is not None:
                cand_name = _first_nonempty(
                    ch.findtext("ShortName"),
                    ch.get("Name"),
                    ch.findtext("Name"),
                    ch.findtext("Fluor"),
                )
                if cand_name:
                    name, named = cand_name, True
                color = _parse_hex_color(ch.findtext("Color") or ch.get("Color"))
            ch_dim = mapped_dim[slot_idx]
            if not named and ch_dim is not None:
                cand_dim_name = _first_nonempty(
                    ch_dim.findtext("ShortName"),
                    ch_dim.get("Name"),
                    ch_dim.findtext("Name"),
                    ch_dim.findtext("Fluor"),
                )
                if cand_dim_name:
                    name, named = cand_dim_name, True
            if not named:
                cand_id = _first_nonempty(
                    ch.get("Id") if ch is not None else None,
                    ch_dim.get("Id") if ch_dim is not None else None,
                )
                if cand_id:
                    name = cand_id
            if color is None and ch_dim is not None:
                color = _parse_hex_color(ch_dim.findtext("Color") or ch_dim.get("Color"))
            if color is None:
                color = (
                    (255, 255, 255)
                    if n_channels == 1
                    else _DEFAULT_PALETTE[slot_idx % len(_DEFAULT_PALETTE)]
                )
            channels.append(ChannelInfo(name, color))

    return pixel_size_um, cal_source, magnification, channels
