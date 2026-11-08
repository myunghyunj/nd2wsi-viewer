"""Zeiss CZI (ZISRAW) input: produces the same PlaneSource as ND2 and SVS.

Supports both 2D whole-slide/plane scans (including multi-tile M mosaics and
RGB brightfield scans) and multi-channel / Z-stack confocal acquisitions.
"""

from __future__ import annotations

import math
import os
import re
import struct
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .platform_io import BINARY, read_at
from .reader import ChannelInfo, PlaneSelection, PlaneSource, _mid

CZI_SUFFIXES = {".czi"}

# Zeiss ZISRAW PixelType codes -> (numpy dtype, samples per pixel, is_rgb)
_PIXEL_TYPES: dict[int, tuple[np.dtype, int, bool]] = {
    0: (np.dtype("uint8"), 1, False),    # Gray8
    1: (np.dtype("uint16"), 1, False),   # Gray16
    2: (np.dtype("float32"), 1, False),  # Gray32Float
    3: (np.dtype("uint8"), 3, True),     # Bgr24
    4: (np.dtype("uint16"), 3, True),    # Bgr48
    8: (np.dtype("float32"), 3, True),   # Bgr96Float
    9: (np.dtype("uint8"), 4, True),     # Bgra32
}

_PIXEL_TYPE_NAMES: dict[int, str] = {
    0: "Gray8",
    1: "Gray16",
    2: "Gray32Float",
    3: "Bgr24",
    4: "Bgr48",
    8: "Bgr96Float",
    9: "Bgra32",
}

_COMPRESSION_NAMES: dict[int, str] = {
    0: "Uncompressed",
    1: "JPEG",
    2: "LZW",
    4: "JPEG-XR",
    5: "Zstd0",
    6: "Zstd1",
    121: "ApoTomeRaw",
}

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


def is_czi(path: str | Path) -> bool:
    return Path(path).suffix.lower() in CZI_SUFFIXES


def _finite_positive(v: Any) -> bool:
    try:
        return v is not None and math.isfinite(float(v)) and float(v) > 0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class _SubBlockEntry:
    pixel_type: int
    file_pos: int
    compression: int
    pyramid_type: int
    ndims: int
    x_start: int
    x_size: int
    x_stored: int
    y_start: int
    y_size: int
    y_stored: int
    c: int
    z: int
    t: int
    s: int
    m: int


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


def _read_czi_header_and_index(
    fd: int, file_size: int
) -> tuple[list[_SubBlockEntry], ET.Element | None]:
    if file_size < 112:
        raise ValueError("CZI file is truncated (smaller than 112-byte ZISRAWFILE header)")
    hdr = read_at(fd, 112, 0)
    if not hdr[:10].startswith(b"ZISRAWFILE"):
        raise ValueError("not a valid Zeiss CZI file (missing ZISRAWFILE magic)")
    dir_pos, meta_pos = struct.unpack("<QQ", hdr[84:100])
    if dir_pos <= 0 or dir_pos + 160 > file_size:
        raise ValueError(f"CZI SubBlock directory position {dir_pos} is invalid")

    dir_hdr = read_at(fd, 160, dir_pos)
    if not dir_hdr[:15].startswith(b"ZISRAWDIRECTORY"):
        raise ValueError("CZI SubBlock directory segment is missing ZISRAWDIRECTORY header")
    _, used_size = struct.unpack("<QQ", dir_hdr[16:32])
    entry_count = struct.unpack("<i", dir_hdr[32:36])[0]
    if entry_count < 0:
        raise ValueError(f"invalid CZI SubBlock count {entry_count}")

    # Read the directory payload in one positional read for fast parsing
    max_dir_bytes = min(file_size - (dir_pos + 160), max(used_size, entry_count * 256))
    dir_buf = read_at(fd, max_dir_bytes, dir_pos + 160)
    pos = 0
    entries: list[_SubBlockEntry] = []
    for _ in range(entry_count):
        if pos + 32 > len(dir_buf):
            raise ValueError("CZI SubBlock directory is truncated")
        schema = dir_buf[pos : pos + 2]
        if schema != b"DV":
            raise ValueError(f"unexpected CZI directory entry schema {schema!r}")
        ptype, fpos, _fpart, comp, pyr_type, _r1, _r2, ndims = struct.unpack(
            "<iqiiBB4si", dir_buf[pos + 2 : pos + 32]
        )
        pos += 32
        if ndims <= 0 or pos + ndims * 20 > len(dir_buf):
            raise ValueError("CZI dimension entries are truncated")
        dims: dict[str, tuple[int, int, int]] = {}
        for _ in range(ndims):
            dname_raw, start, size, _start_coord, stored_size = struct.unpack(
                "<4siifi", dir_buf[pos : pos + 20]
            )
            pos += 20
            dname = dname_raw.rstrip(b"\x00").decode("ascii", errors="replace")
            dims[dname] = (int(start), int(size), int(stored_size))
        if "X" not in dims or "Y" not in dims:
            continue
        x_start, x_size, x_stored = dims["X"]
        y_start, y_size, y_stored = dims["Y"]
        if x_size <= 0 or y_size <= 0 or x_stored <= 0 or y_stored <= 0:
            continue
        entries.append(
            _SubBlockEntry(
                pixel_type=int(ptype),
                file_pos=int(fpos),
                compression=int(comp),
                pyramid_type=int(pyr_type),
                ndims=int(ndims),
                x_start=x_start,
                x_size=x_size,
                x_stored=x_stored,
                y_start=y_start,
                y_size=y_size,
                y_stored=y_stored,
                c=dims.get("C", (0, 1, 1))[0],
                z=dims.get("Z", (0, 1, 1))[0],
                t=dims.get("T", (0, 1, 1))[0],
                s=dims.get("S", (0, 1, 1))[0],
                m=dims.get("M", (0, 1, 1))[0],
            )
        )

    xml_root: ET.Element | None = None
    if 0 < meta_pos and meta_pos + 288 <= file_size:
        meta_hdr = read_at(fd, 288, meta_pos)
        if meta_hdr[:14].startswith(b"ZISRAWMETADATA"):
            xml_size = struct.unpack("<I", meta_hdr[32:36])[0]
            if 0 < xml_size and meta_pos + 288 + xml_size <= file_size:
                xml_bytes = read_at(fd, xml_size, meta_pos + 288).rstrip(b"\x00 \t\r\n")
                try:
                    xml_root = ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
                except ET.ParseError:
                    xml_root = None

    return entries, xml_root


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


_CHANNEL_ID_RE = re.compile(r"(?:^|:)(\d+)$")


def _czi_channel_index(ch: ET.Element) -> int | None:
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
    scale_um: dict[str, float] = {}
    for dist in xml_root.findall(".//Scaling/Items/Distance"):
        axis = dist.get("Id")
        val_text = dist.findtext("Value")
        if axis in ("X", "Y") and _finite_positive(val_text):
            um = float(val_text) * 1e6
            if _finite_positive(um):
                scale_um[axis] = um
    if "X" in scale_um and "Y" in scale_um:
        pixel_size_um: tuple[float, float] | None = (scale_um["Y"], scale_um["X"])
        cal_source = "czi-scaling"
    else:
        pixel_size_um = None
        cal_source = "unknown"

    # 2. Objective magnification
    magnification: float | None = None
    for obj in xml_root.findall(".//Instrument/Objectives/Objective"):
        mag_text = obj.findtext("NominalMagnification")
        if _finite_positive(mag_text):
            magnification = float(mag_text)
            break

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
            color: tuple[int, int, int] | None = None
            ch = mapped_chosen[slot_idx]
            if ch is not None:
                cand_name = (
                    ch.findtext("ShortName")
                    or ch.get("Name")
                    or ch.findtext("Name")
                    or ch.findtext("Fluor")
                    or ch.get("Id")
                )
                if cand_name and cand_name.strip():
                    name = cand_name.strip()
                color = _parse_hex_color(ch.findtext("Color") or ch.get("Color"))
            ch_dim = mapped_dim[slot_idx]
            if color is None and ch_dim is not None:
                if name == default_name:
                    cand_name = (
                        ch_dim.get("Name")
                        or ch_dim.findtext("Fluor")
                        or ch_dim.get("Id")
                    )
                    if cand_name and cand_name.strip():
                        name = cand_name.strip()
                color = _parse_hex_color(ch_dim.findtext("Color") or ch_dim.get("Color"))
            if color is None:
                color = (
                    (255, 255, 255)
                    if n_channels == 1
                    else _DEFAULT_PALETTE[slot_idx % len(_DEFAULT_PALETTE)]
                )
            channels.append(ChannelInfo(name, color))

    return pixel_size_um, cal_source, magnification, channels


def _decode_subblock_bytes(
    raw: bytes,
    sb: _SubBlockEntry,
    dtype: np.dtype,
    samples: int,
) -> np.ndarray:
    """Decode one SubBlock payload into (y_stored, x_stored) or (y_stored, x_stored, 3)."""
    h, w = sb.y_stored, sb.x_stored
    comp = sb.compression
    if comp == 0:
        arr = np.frombuffer(raw, dtype=dtype).reshape(h, w, samples)
        if samples == 1:
            return arr[:, :, 0]
        # Uncompressed Bgr24 / Bgr48 / Bgra32 -> RGB
        return np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])

    import imagecodecs

    if comp in (5, 6):  # Zstd0 (5) or Zstd1 (6)
        hi_lo = False
        payload = raw
        if comp == 6:
            if not raw:
                raise ValueError("empty Zstd1 SubBlock payload")
            hdr_len = raw[0]
            if hdr_len == 3 and len(raw) >= 3 and raw[1] == 1:
                hi_lo = bool(raw[2] & 1)
                payload = raw[3:]
            elif hdr_len == 1:
                payload = raw[1:]
            else:
                payload = raw[min(hdr_len, len(raw)) :]
        decomp = imagecodecs.zstd_decode(payload)
        if hi_lo and dtype.itemsize == 2:
            u8 = np.frombuffer(decomp, dtype=np.uint8)
            half = len(u8) // 2
            u16 = u8[:half].astype(np.uint16) | (u8[half : half * 2].astype(np.uint16) << 8)
            arr = u16.view(dtype).reshape(h, w, samples)
        else:
            arr = np.frombuffer(decomp, dtype=dtype).reshape(h, w, samples)
        if samples == 1:
            return arr[:, :, 0]
        return np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])

    if comp == 4:  # JPEG-XR (jxrlib already outputs RGB channel order)
        arr = np.asarray(imagecodecs.jpegxr_decode(raw))
        if arr.ndim == 2:
            return arr[:h, :w]
        return np.ascontiguousarray(arr[:h, :w, :3])

    if comp == 1:  # JPEG
        arr = np.asarray(imagecodecs.jpeg_decode(raw))
        if arr.ndim == 2:
            return arr[:h, :w]
        return np.ascontiguousarray(arr[:h, :w, :3])

    if comp == 2:  # LZW
        decomp = imagecodecs.lzw_decode(raw)
        arr = np.frombuffer(decomp, dtype=dtype).reshape(h, w, samples)
        if samples == 1:
            return arr[:, :, 0]
        return np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])

    plane_bytes = h * w * samples * dtype.itemsize
    if comp >= 100 and plane_bytes > 0 and len(raw) % plane_bytes == 0:
        n_phases = len(raw) // plane_bytes
        phases = np.frombuffer(raw, dtype=dtype).reshape(n_phases, h, w, samples)
        if n_phases == 1:
            arr = phases[0]
        elif n_phases == 3 and samples == 1:
            # Zeiss ApoTome 3-phase (0, 120, 240 deg) optical sectioning demodulation
            f0 = phases[0, :, :, 0].astype(np.float32)
            f1 = phases[1, :, :, 0].astype(np.float32)
            f2 = phases[2, :, :, 0].astype(np.float32)
            sec = (np.float32(0.47140452) * np.sqrt((f0 - f1) ** 2 + (f1 - f2) ** 2 + (f2 - f0) ** 2))
            if np.issubdtype(dtype, np.integer):
                info_i = np.iinfo(dtype)
                return np.clip(np.rint(sec), info_i.min, info_i.max).astype(dtype)
            return sec.astype(dtype)
        else:
            mean = phases.astype(np.float32).mean(axis=0)
            arr = mean.astype(dtype)
        if samples == 1:
            return arr[:, :, 0]
        return np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])

    raise NotImplementedError(
        f"unsupported CZI compression code {comp} ({_COMPRESSION_NAMES.get(comp, 'unknown')})"
    )


def _resolve_czi_entries_spec(
    entries: list[_SubBlockEntry],
) -> tuple[np.dtype, bool, list[str]]:
    """Validate SubBlock pixel types and return (promoted_dtype, rgb, channel_dtypes)."""
    if not entries:
        raise ValueError("CZI selection contains no SubBlocks")
    rgb_flags: set[bool] = set()
    by_c: dict[int, set[np.dtype]] = {}
    all_dtypes: list[np.dtype] = []
    for e in entries:
        if e.pixel_type not in _PIXEL_TYPES:
            raise NotImplementedError(f"unsupported CZI PixelType code {e.pixel_type}")
        dt, _samples, is_rgb = _PIXEL_TYPES[e.pixel_type]
        rgb_flags.add(is_rgb)
        by_c.setdefault(e.c, set()).add(dt)
        all_dtypes.append(dt)
    if len(rgb_flags) > 1:
        raise ValueError("unsupported CZI layout: mixing RGB and grayscale SubBlocks")
    rgb = next(iter(rgb_flags))
    promoted = all_dtypes[0]
    for dt in all_dtypes[1:]:
        promoted = np.promote_types(promoted, dt)
    if rgb:
        raw_ptypes = sorted({_PIXEL_TYPE_NAMES[e.pixel_type] for e in entries})
        if len(raw_ptypes) > 1 or any(e.pixel_type in (8, 9) for e in entries):
            sig = "+".join(raw_ptypes)
            channel_dtypes = [sig] * 3
        else:
            channel_dtypes = [str(promoted)] * 3
    else:
        channel_dtypes = []
        for c_val in sorted(by_c):
            c_dts = sorted(by_c[c_val], key=lambda d: str(np.dtype(d)))
            if len(c_dts) == 1:
                channel_dtypes.append(str(np.dtype(c_dts[0])))
            else:
                channel_dtypes.append("+".join(str(np.dtype(d)) for d in c_dts))
    return np.dtype(promoted), rgb, channel_dtypes


def _read_subblock_window(
    fd: int,
    sb: _SubBlockEntry,
    dtype: np.dtype,
    samples: int,
    ry0: int,
    ry1: int,
    rx0: int,
    rx1: int,
) -> np.ndarray:
    """Read [ry0:ry1, rx0:rx1] within a single Level-0 SubBlock."""
    if sb.pixel_type not in _PIXEL_TYPES:
        raise NotImplementedError(f"unsupported CZI PixelType code {sb.pixel_type}")
    sb_dtype, sb_samples, sb_rgb = _PIXEL_TYPES[sb.pixel_type]
    if sb_rgb != (samples > 1):
        raise ValueError("CZI SubBlock RGB/grayscale mode does not match plane expectation")

    sb_hdr = read_at(fd, 64, sb.file_pos)
    if not sb_hdr[:14].startswith(b"ZISRAWSUBBLOCK"):
        raise ValueError(f"invalid CZI SubBlock header at offset {sb.file_pos}")
    meta_size, _att_size, data_size = struct.unpack("<IIQ", sb_hdr[32:48])
    if len(sb_hdr) >= 54 and sb_hdr[48:50] == b"DV":
        hdr_ptype = struct.unpack_from("<i", sb_hdr, 50)[0]
        if hdr_ptype != sb.pixel_type:
            raise ValueError(
                f"CZI SubBlock header PixelType ({hdr_ptype}) disagrees with directory ({sb.pixel_type})"
            )
    header_len = max(256, 16 + 32 + 20 * sb.ndims)
    data_offset = sb.file_pos + 32 + header_len + meta_size

    # Fast path for uncompressed SubBlocks: read only the intersecting row span
    if sb.compression == 0 and sb.x_stored == sb.x_size and sb.y_stored == sb.y_size:
        row_bytes = sb.x_stored * sb_samples * sb_dtype.itemsize
        expected_total = sb.y_stored * row_bytes
        if int(data_size) < expected_total:
            raise ValueError(
                f"CZI SubBlock payload ({data_size} B) smaller than expected ({expected_total} B)"
            )
        span_offset = data_offset + ry0 * row_bytes
        span_bytes = (ry1 - ry0) * row_bytes
        raw = read_at(fd, span_bytes, span_offset)
        rows = np.frombuffer(raw, dtype=sb_dtype).reshape(ry1 - ry0, sb.x_stored, sb_samples)
        if sb_samples == 1:
            return np.ascontiguousarray(rows[:, rx0:rx1, 0], dtype=dtype)
        return np.ascontiguousarray(rows[:, rx0:rx1, :3][:, :, ::-1], dtype=dtype)

    raw = read_at(fd, int(data_size), data_offset)
    decoded = _decode_subblock_bytes(raw, sb, sb_dtype, sb_samples)
    if decoded.ndim == 2:
        return np.ascontiguousarray(decoded[ry0:ry1, rx0:rx1], dtype=dtype)
    return np.ascontiguousarray(decoded[ry0:ry1, rx0:rx1, :3], dtype=dtype)


def _level0_entries(entries: list[_SubBlockEntry]) -> list[_SubBlockEntry]:
    lvl0 = [
        e for e in entries
        if e.pyramid_type == 0 and e.x_size == e.x_stored and e.y_size == e.y_stored
    ]
    if not lvl0:
        lvl0 = [e for e in entries if e.x_size == e.x_stored and e.y_size == e.y_stored]
    if not lvl0:
        raise ValueError("CZI file contains no full-resolution Level-0 SubBlocks")
    return lvl0


def _grid_chunks(size: int, step: int) -> tuple[int, ...]:
    n_full, rem = divmod(size, step)
    return tuple([step] * n_full + ([rem] if rem else []))


def open_czi(
    stack: ExitStack,
    path: str | Path,
    selection: PlaneSelection | None = None,
    tile: int = 512,
) -> PlaneSource:
    """Open one plane (or Z maximum-intensity projection) of a Zeiss CZI file."""
    import dask.array as da

    path = Path(path)
    selection = selection or PlaneSelection()
    file_size = path.stat().st_size
    fd = os.open(str(path), os.O_RDONLY | BINARY)
    stack.callback(os.close, fd)

    entries, xml_root = _read_czi_header_and_index(fd, file_size)
    lvl0 = _level0_entries(entries)

    s_values = sorted({e.s for e in lvl0})
    t_values = sorted({e.t for e in lvl0})
    n_p = len(s_values)
    n_t = len(t_values)

    if not 0 <= selection.t < n_t:
        raise ValueError(f"--t {selection.t} out of range (T={n_t})")
    if not 0 <= selection.p < n_p:
        raise ValueError(f"--position {selection.p} out of range (P={n_p})")

    s_target = s_values[selection.p]
    t_target = t_values[selection.t]
    st_entries = [e for e in lvl0 if e.s == s_target and e.t == t_target]
    if not st_entries:
        raise ValueError(
            f"no CZI SubBlocks found for scene={selection.p} (S={s_target}), t={selection.t} (T={t_target})"
        )

    z_values = sorted({e.z for e in st_entries})
    c_values = sorted({e.c for e in st_entries})
    m_values = sorted({e.m for e in st_entries})
    n_z = len(z_values)

    dtype, rgb, channel_dtypes = _resolve_czi_entries_spec(st_entries)
    samples = 3 if rgb else 1

    x0_origin = min(e.x_start for e in st_entries)
    y0_origin = min(e.y_start for e in st_entries)
    width = max(e.x_start + e.x_size for e in st_entries) - x0_origin
    height = max(e.y_start + e.y_size for e in st_entries) - y0_origin

    notes: list[str] = [f"Zeiss CZI ({width} x {height})"]
    if n_p > 1:
        notes.append(f"file has {n_p} scenes; converting position {selection.p} only")
    if n_t > 1:
        notes.append(f"file has {n_t} timepoints; converting t={selection.t}")
    if len(m_values) > 1:
        notes.append(f"stitched {len(m_values)} mosaic tiles")

    # Group SubBlocks by (z_val, c_val) for O(1) plane lookup
    by_zc: dict[tuple[int, int], list[_SubBlockEntry]] = {}
    for e in st_entries:
        by_zc.setdefault((e.z, e.c), []).append(e)

    def make_z_dask(z_val: int) -> Any:
        if rgb:
            z_sbs = by_zc.get((z_val, c_values[0]), [])
            bg_fill = (
                (255 if dtype == np.uint8 else (65535 if dtype == np.uint16 else 0))
                if len(m_values) > 1
                else 0
            )

            def fetch_rgb(block_info=None):
                (_, _), (by0, by1), (bx0, bx1) = block_info[None]["array-location"]
                out = np.full((3, by1 - by0, bx1 - bx0), bg_fill, dtype=dtype)
                for sb in z_sbs:
                    sx0 = sb.x_start - x0_origin
                    sy0 = sb.y_start - y0_origin
                    sx1 = sx0 + sb.x_size
                    sy1 = sy0 + sb.y_size
                    oy0, oy1 = max(by0, sy0), min(by1, sy1)
                    ox0, ox1 = max(bx0, sx0), min(bx1, sx1)
                    if oy1 <= oy0 or ox1 <= ox0:
                        continue
                    patch = _read_subblock_window(
                        fd, sb, dtype, samples,
                        oy0 - sy0, oy1 - sy0, ox0 - sx0, ox1 - sx0,
                    )
                    out[:, oy0 - by0 : oy1 - by0, ox0 - bx0 : ox1 - bx0] = np.moveaxis(
                        patch, -1, 0
                    )
                return out

            return da.map_blocks(
                fetch_rgb,
                chunks=((3,), _grid_chunks(height, tile), _grid_chunks(width, tile)),
                dtype=dtype,
            )

        n_c = len(c_values)

        def fetch_gray(block_info=None):
            (ci0, ci1), (by0, by1), (bx0, bx1) = block_info[None]["array-location"]
            out = np.zeros((ci1 - ci0, by1 - by0, bx1 - bx0), dtype=dtype)
            for slot, ci in enumerate(range(ci0, ci1)):
                c_val = c_values[ci]
                for sb in by_zc.get((z_val, c_val), ()):
                    sx0 = sb.x_start - x0_origin
                    sy0 = sb.y_start - y0_origin
                    sx1 = sx0 + sb.x_size
                    sy1 = sy0 + sb.y_size
                    oy0, oy1 = max(by0, sy0), min(by1, sy1)
                    ox0, ox1 = max(bx0, sx0), min(bx1, sx1)
                    if oy1 <= oy0 or ox1 <= ox0:
                        continue
                    patch = _read_subblock_window(
                        fd, sb, dtype, 1,
                        oy0 - sy0, oy1 - sy0, ox0 - sx0, ox1 - sx0,
                    )
                    out[slot, oy0 - by0 : oy1 - by0, ox0 - bx0 : ox1 - bx0] = patch
            return out

        return da.map_blocks(
            fetch_gray,
            chunks=((1,) * n_c, _grid_chunks(height, tile), _grid_chunks(width, tile)),
            dtype=dtype,
        )

    if selection.z == "max" and n_z > 1:
        planes = [make_z_dask(zv) for zv in z_values]
        data = da.stack(planes, axis=0).max(axis=0)
        z_used: str | int = "max"
        notes.append(f"Z: maximum-intensity projection over {n_z} planes")
    else:
        if selection.z == "mid":
            z_index = _mid(n_z)
        elif selection.z == "max":
            z_index = 0
        else:
            z_index = int(selection.z)
        if not 0 <= z_index < n_z:
            raise ValueError(f"--z {z_index} out of range (Z={n_z})")
        data = make_z_dask(z_values[z_index])
        z_used = z_index
        if n_z > 1:
            notes.append(f"file has {n_z} Z planes; converting z={z_index}")

    n_channels = 3 if rgb else len(c_values)
    pixel_size_um, cal_source, magnification, channels = _extract_czi_metadata(
        xml_root, n_channels, rgb, c_values=c_values
    )
    if pixel_size_um is None:
        notes.append("no pixel calibration in the file; measurements are in pixels")

    return PlaneSource(
        data=data,
        dtype=dtype,
        shape=(n_channels, height, width),
        rgb=rgb,
        channels=channels,
        pixel_size_um=pixel_size_um,
        source_name=str(path),
        selection={"t": selection.t, "p": selection.p, "z": z_used},
        notes=notes,
        magnification=magnification,
        calibration_source=cal_source,
        channel_dtypes=channel_dtypes,
    )


def collect_czi_info(path: str | Path) -> dict[str, Any]:
    """`nd2wsi info` payload for a Zeiss CZI file."""
    path = Path(path)
    file_size = path.stat().st_size
    fd = os.open(str(path), os.O_RDONLY | BINARY)
    try:
        entries, xml_root = _read_czi_header_and_index(fd, file_size)
    finally:
        os.close(fd)
    lvl0 = _level0_entries(entries)
    s_values = sorted({e.s for e in lvl0})
    s0_entries = [e for e in lvl0 if e.s == s_values[0]] if s_values else lvl0
    x0 = min(e.x_start for e in s0_entries)
    y0 = min(e.y_start for e in s0_entries)
    width = max(e.x_start + e.x_size for e in s0_entries) - x0
    height = max(e.y_start + e.y_size for e in s0_entries) - y0
    ptype = s0_entries[0].pixel_type
    try:
        dtype, rgb, _channel_dtypes = _resolve_czi_entries_spec(s0_entries)
    except Exception:
        dtype, _samples, rgb = _PIXEL_TYPES.get(ptype, (np.dtype("uint16"), 1, False))
    ptypes = sorted({_PIXEL_TYPE_NAMES.get(e.pixel_type, str(e.pixel_type)) for e in s0_entries})
    c_values = sorted({e.c for e in s0_entries})
    z_values = sorted({e.z for e in s0_entries})
    t_values = sorted({e.t for e in s0_entries})
    m_values = sorted({e.m for e in s0_entries})
    n_channels = 3 if rgb else len(c_values)
    pixel_size_um, cal_source, magnification, channels = _extract_czi_metadata(
        xml_root, n_channels, rgb, c_values=c_values
    )
    compressions = sorted({_COMPRESSION_NAMES.get(e.compression, str(e.compression)) for e in lvl0})
    return {
        "file": str(path),
        "size_bytes": file_size,
        "format": "Zeiss CZI (ZISRAW)",
        "width": width,
        "height": height,
        "channels": n_channels,
        "z_planes": len(z_values),
        "timepoints": len(t_values),
        "scenes": len(s_values),
        "mosaic_tiles": len(m_values),
        "dtype": str(dtype),
        "pixel_type": ", ".join(ptypes) if ptypes else _PIXEL_TYPE_NAMES.get(ptype, str(ptype)),
        "rgb": rgb,
        "compression": ", ".join(compressions),
        "pixel_size_um": list(pixel_size_um) if pixel_size_um else None,
        "calibration_source": cal_source,
        "magnification": magnification,
        "channel_info": [
            {"name": ch.name, "color": "#{:02X}{:02X}{:02X}".format(*ch.color)}
            for ch in channels
        ],
    }


def format_czi_info(info: dict[str, Any]) -> str:
    from .reader import nice_bytes

    lines = [
        f"File          : {info['file']} ({nice_bytes(info['size_bytes'])})",
        f"Format        : {info['format']}",
        f"Dimensions    : {info['width']} x {info['height']} px, "
        f"C={info['channels']}, Z={info['z_planes']}, T={info['timepoints']}, "
        f"S={info['scenes']}, M={info['mosaic_tiles']}",
        f"Pixel type    : {info['pixel_type']} ({info['dtype']}), compression: {info['compression']}",
    ]
    if info.get("pixel_size_um"):
        py, px = info["pixel_size_um"]
        lines.append(f"Pixel size    : {px:.4f} x {py:.4f} µm/px ({info['calibration_source']})")
    else:
        lines.append("Pixel size    : uncalibrated")
    if info.get("magnification"):
        lines.append(f"Objective     : {info['magnification']:g}x")
    lines.append("Channels      :")
    for ch in info.get("channel_info", []):
        lines.append(f"  - {ch['name']} ({ch['color']})")
    return "\n".join(lines)
