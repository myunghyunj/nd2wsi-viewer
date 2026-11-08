"""Leica LIF (Leica Image File — LAS AF / LAS X) input: produces the same PlaneSource as ND2, CZI, and SVS.

Supports both 2D fluorescence/brightfield scans (including multi-series containers,
LambdaScan spectral series, and stage tile scans) and multi-channel / Z-stack
confocal & LIGHTNING deconvolution acquisitions.
"""

from __future__ import annotations

import math
import os
import struct
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .platform_io import BINARY, read_at
from .reader import ChannelInfo, PlaneSelection, PlaneSource, _mid

LIF_SUFFIXES = {".lif"}

_LUT_COLORS: dict[str, tuple[int, int, int]] = {
    "green": (0, 255, 0),
    "red": (255, 0, 0),
    "blue": (0, 160, 255),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "yellow": (255, 255, 0),
    "gray": (255, 255, 255),
    "grey": (255, 255, 255),
    "glow": (255, 255, 255),
    "white": (255, 255, 255),
    "orange": (255, 128, 0),
    "violet": (160, 64, 255),
    "purple": (160, 64, 255),
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


def is_lif(path: str | Path) -> bool:
    return Path(path).suffix.lower() in LIF_SUFFIXES


def _finite_positive(v: Any) -> bool:
    try:
        return v is not None and math.isfinite(float(v)) and float(v) > 0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class _LifChannelSpec:
    data_type: int
    channel_tag: int
    resolution: int
    lut_name: str
    bytes_inc: int
    name: str
    color: tuple[int, int, int]
    dtype: np.dtype = np.dtype("uint8")


@dataclass(frozen=True)
class _LifSeriesSpec:
    name: str
    mem_id: str
    data_offset: int
    mem_size: int
    width: int
    height: int
    inc_x: int
    inc_y: int
    n_z: int
    inc_z: int
    n_t: int
    inc_t: int
    n_m: int
    inc_m: int
    is_lambdascan: bool
    dtype: np.dtype
    rgb: bool
    channels: list[_LifChannelSpec]
    pixel_size_um: tuple[float, float] | None
    calibration_source: str
    magnification: float | None
    objective_name: str | None
    tile_positions_px: list[tuple[int, int]] | None
    z_step_um: float | None = None


def _read_lif_header_and_blocks(
    fd: int, file_size: int
) -> tuple[ET.Element, dict[str, tuple[int, int]]]:
    if file_size < 13:
        raise ValueError("LIF file is truncated (smaller than 13-byte container header)")
    hdr = read_at(fd, 13, 0)
    magic, _chunk_len, marker, xml_nc = struct.unpack("<iIBI", hdr)
    if magic != 0x70 or marker != 0x2A:
        raise ValueError("not a valid Leica LIF file (missing 0x70/0x2A container magic)")
    xml_bytes_len = int(xml_nc) * 2
    if xml_bytes_len <= 0 or 13 + xml_bytes_len > file_size:
        raise ValueError("LIF XML header size exceeds file bounds")

    xml_raw = read_at(fd, xml_bytes_len, 13)
    xml_text = xml_raw.decode("utf-16-le", errors="replace").rstrip("\x00 \t\r\n")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"failed to parse Leica LIF XML header: {exc}") from exc

    try:
        ver = int(root.attrib.get("Version", "2") or 2)
    except ValueError:
        ver = 2

    blocks: dict[str, tuple[int, int]] = {}
    pos = 13 + xml_bytes_len
    size_field_len = 8 if ver >= 2 else 4
    prefix_len = 9 + size_field_len + 5

    while pos + prefix_len <= file_size:
        prefix = read_at(fd, prefix_len, pos)
        b_magic, _b_chunk, b_mark = struct.unpack("<iIB", prefix[:9])
        if b_magic != 0x70 or b_mark != 0x2A:
            break
        if ver >= 2:
            mem_size = int(struct.unpack("<Q", prefix[9:17])[0])
            m2, id_nc = struct.unpack("<BI", prefix[17:22])
        else:
            mem_size = int(struct.unpack("<I", prefix[9:13])[0])
            m2, id_nc = struct.unpack("<BI", prefix[13:18])
        if m2 != 0x2A:
            break
        id_bytes_len = int(id_nc) * 2
        id_pos = pos + prefix_len
        if id_pos + id_bytes_len > file_size:
            break
        mem_id = (
            read_at(fd, id_bytes_len, id_pos)
            .decode("utf-16-le", errors="replace")
            .rstrip("\x00")
        )
        data_offset = id_pos + id_bytes_len
        if mem_size > 0 and data_offset + mem_size <= file_size:
            blocks[mem_id] = (data_offset, mem_size)
        pos = data_offset + max(0, mem_size)

    return root, blocks


def _single_channel_dtype(data_type: int, resolution: int) -> np.dtype:
    if data_type == 1:
        if resolution == 32:
            return np.dtype("<f4")
        raise ValueError(f"unsupported LIF float channel resolution: {resolution}")
    if data_type == 0:
        if 0 < resolution <= 8:
            return np.dtype("uint8")
        if 8 < resolution <= 16:
            return np.dtype("<u2")
        raise ValueError(
            f"unsupported LIF integer channel resolution {resolution} "
            "(>16-bit integer channels are not supported)"
        )
    raise ValueError(f"unsupported LIF channel DataType: {data_type}")


def _series_dtype(ch_dtypes: list[np.dtype]) -> np.dtype:
    if any(dt == np.dtype("<f4") for dt in ch_dtypes):
        return np.dtype("<f4")
    if any(dt == np.dtype("<u2") for dt in ch_dtypes):
        return np.dtype("<u2")
    return np.dtype("uint8")


def _validate_lif_channel_layout(
    channels: list[_LifChannelSpec],
    *,
    width: int,
    height: int,
    inc_x: int,
    inc_y: int,
    n_z: int,
    inc_z: int,
    n_t: int,
    inc_t: int,
    n_m: int,
    inc_m: int,
    declared_size: int,
    block_size: int,
) -> None:
    if inc_y < width * inc_x:
        raise ValueError(
            f"invalid LIF dimension strides: row stride ({inc_y}) < width * pixel stride ({width * inc_x})"
        )
    if (n_z > 1 and inc_z <= 0) or (n_t > 1 and inc_t <= 0) or (n_m > 1 and inc_m <= 0):
        raise ValueError("invalid LIF dimension strides: non-positive stride for multi-plane axis")
    max_plane_offset = (
        (n_z - 1) * max(0, inc_z)
        + (n_t - 1) * max(0, inc_t)
        + (n_m - 1) * max(0, inc_m)
    )
    pixel_intervals: list[tuple[int, int]] = []
    for ch in channels:
        itemsize = ch.dtype.itemsize
        if ch.bytes_inc < 0:
            raise ValueError(f"invalid LIF channel BytesInc: {ch.bytes_inc}")
        if inc_x < itemsize:
            raise ValueError(
                f"invalid LIF channel layout: pixel stride ({inc_x}) < channel itemsize ({itemsize})"
            )
        if ch.bytes_inc < inc_x:
            if ch.bytes_inc + itemsize > inc_x:
                raise ValueError(
                    f"invalid LIF pixel-interleaved channel: offset {ch.bytes_inc} + {itemsize} exceeds pixel stride {inc_x}"
                )
            for lo, hi in pixel_intervals:
                if not (ch.bytes_inc + itemsize <= lo or ch.bytes_inc >= hi):
                    raise ValueError("invalid LIF pixel-interleaved layout: overlapping channel offsets")
            pixel_intervals.append((ch.bytes_inc, ch.bytes_inc + itemsize))
        max_end = (
            max_plane_offset
            + ch.bytes_inc
            + (height - 1) * inc_y
            + (width - 1) * inc_x
            + itemsize
        )
        if max_end > block_size or max_end > declared_size:
            raise ValueError("invalid LIF channel layout: channel data exceeds memory block bounds")


def _extract_dye_names(elem: ET.Element, n_channels: int) -> list[str | None]:
    multibands: dict[str, str] = {}
    for mb in elem.findall(".//MultiBand"):
        ch_id = mb.attrib.get("Channel")
        dye = (mb.attrib.get("DyeName") or "").strip()
        if dye.lower().startswith("leica/"):
            dye = dye[6:].strip()
        if ch_id and dye:
            multibands[ch_id] = dye

    active_ch_ids: list[str] = []
    for det in elem.findall(".//Detector"):
        if det.attrib.get("IsActive") == "1":
            ch_id = det.attrib.get("Channel")
            if ch_id and ch_id not in active_ch_ids:
                active_ch_ids.append(ch_id)

    if len(active_ch_ids) == n_channels:
        return [multibands.get(cid) for cid in active_ch_ids]
    if len(multibands) == n_channels:
        return list(multibands.values())
    return [None] * n_channels


def _parse_series_element(
    elem: ET.Element,
    blocks: dict[str, tuple[int, int]],
) -> _LifSeriesSpec | None:
    img = elem.find("./Data/Image")
    mem = elem.find("./Memory")
    if img is None or mem is None:
        return None
    try:
        declared_size = int(mem.attrib.get("Size", "0") or 0)
    except ValueError:
        declared_size = 0
    mem_id = mem.attrib.get("MemoryBlockID", "")
    if declared_size <= 0 or mem_id not in blocks:
        return None
    data_offset, block_size = blocks[mem_id]

    dim_elems = img.findall("./ImageDescription/Dimensions/DimensionDescription")
    ch_elems = img.findall("./ImageDescription/Channels/ChannelDescription")
    if not dim_elems or not ch_elems:
        return None

    dims: dict[int, dict[str, Any]] = {}
    for d in dim_elems:
        try:
            dim_id = int(d.attrib.get("DimID", "0") or 0)
            n_el = max(1, int(d.attrib.get("NumberOfElements", "1") or 1))
            b_inc = int(d.attrib.get("BytesInc", "0") or 0)
        except ValueError:
            continue
        dims[dim_id] = {
            "n": n_el,
            "inc": b_inc,
            "length": d.attrib.get("Length"),
            "unit": (d.attrib.get("Unit") or "").strip(),
        }

    if 1 not in dims or 2 not in dims:
        return None

    width = dims[1]["n"]
    height = dims[2]["n"]
    inc_x = dims[1]["inc"]
    inc_y = dims[2]["inc"]
    if width <= 0 or height <= 0 or inc_x <= 0 or inc_y <= 0:
        return None

    is_lambdascan = False
    if 3 in dims and dims[3]["n"] > 1:
        n_z = dims[3]["n"]
        inc_z = dims[3]["inc"]
    elif 5 in dims and dims[5]["n"] > 1:
        n_z = dims[5]["n"]
        inc_z = dims[5]["inc"]
        is_lambdascan = True
    else:
        n_z = 1
        inc_z = 0

    n_t = dims[4]["n"] if 4 in dims else 1
    inc_t = dims[4]["inc"] if 4 in dims else 0
    n_m = dims[10]["n"] if 10 in dims else 1
    inc_m = dims[10]["inc"] if 10 in dims else 0

    raw_chans: list[tuple[int, int, str, int, int, np.dtype]] = []
    for c in ch_elems:
        try:
            dt = int(c.attrib.get("DataType", "0") or 0)
            tag = int(c.attrib.get("ChannelTag", "0") or 0)
            res = int(c.attrib.get("Resolution", "8") or 8)
            b_inc = int(c.attrib.get("BytesInc", "0") or 0)
        except ValueError:
            continue
        ch_dtype = _single_channel_dtype(dt, res)
        lut = (c.attrib.get("LUTName") or "").strip()
        raw_chans.append((dt, tag, lut, b_inc, res, ch_dtype))

    if not raw_chans:
        return None

    dtype = _series_dtype([rc[5] for rc in raw_chans])

    tags = {rc[1] for rc in raw_chans}
    rgb = len(raw_chans) == 3 and tags == {1, 2, 3}

    if rgb:
        # ChannelTag 1=Red, 2=Green, 3=Blue; order channels as R, G, B
        by_tag = {rc[1]: rc for rc in raw_chans}
        rgb_defs = [
            (1, "Red", (255, 0, 0)),
            (2, "Green", (0, 255, 0)),
            (3, "Blue", (0, 0, 255)),
        ]
        channels = [
            _LifChannelSpec(
                data_type=by_tag[tag][0],
                channel_tag=tag,
                resolution=by_tag[tag][4],
                lut_name=cname,
                bytes_inc=by_tag[tag][3],
                name=cname,
                color=ccolor,
                dtype=by_tag[tag][5],
            )
            for tag, cname, ccolor in rgb_defs
        ]
    else:
        dyes = _extract_dye_names(elem, len(raw_chans))
        channels = []
        used_names: set[str] = set()
        for idx, (dt, tag, lut, b_inc, res, ch_dtype) in enumerate(raw_chans):
            color = _LUT_COLORS.get(lut.lower())
            if color is None:
                color = (
                    (255, 255, 255)
                    if len(raw_chans) == 1
                    else _DEFAULT_PALETTE[idx % len(_DEFAULT_PALETTE)]
                )
            base_name = dyes[idx] or lut or f"Channel {idx}"
            cname = base_name
            if cname in used_names:
                cname = f"{base_name} ({idx + 1})"
            used_names.add(cname)
            channels.append(
                _LifChannelSpec(
                    data_type=dt,
                    channel_tag=tag,
                    resolution=res,
                    lut_name=lut or cname,
                    bytes_inc=b_inc,
                    name=cname,
                    color=color,
                    dtype=ch_dtype,
                )
            )

    _validate_lif_channel_layout(
        channels,
        width=width,
        height=height,
        inc_x=inc_x,
        inc_y=inc_y,
        n_z=n_z,
        inc_z=inc_z,
        n_t=n_t,
        inc_t=inc_t,
        n_m=n_m,
        inc_m=inc_m,
        declared_size=declared_size,
        block_size=block_size,
    )

    # Physical pixel calibration in micrometers (LIF stores Length in meters when Unit == "m")
    px_um = _axis_pixel_size_um(dims[1])
    py_um = _axis_pixel_size_um(dims[2])
    z_step_um = _axis_pixel_size_um(dims[3]) if (3 in dims and dims[3]["n"] > 1) else None
    if px_um is not None and py_um is not None:
        pixel_size_um: tuple[float, float] | None = (py_um, px_um)
        cal_source = "lif-dimension"
    else:
        pixel_size_um = None
        cal_source = "unknown"

    # Objective magnification & lens name
    magnification: float | None = None
    objective_name: str | None = None
    atl = elem.find(".//ATLConfocalSettingDefinition")
    if atl is not None:
        mag_text = atl.attrib.get("Magnification")
        if _finite_positive(mag_text):
            magnification = float(mag_text)
        obj_raw = (atl.attrib.get("ObjectiveName") or "").strip()
        if obj_raw:
            objective_name = " ".join(obj_raw.split())

    # Optional TileScanInfo positions for stage mosaics (DimID=10)
    tile_positions_px: list[tuple[int, int]] | None = None
    if n_m > 1:
        tiles = elem.findall(".//Attachment[@Name='TileScanInfo']/Tile")
        if len(tiles) == n_m:
            try:
                if px_um and py_um and all("PosX" in t.attrib and "PosY" in t.attrib for t in tiles):
                    xs_m = [float(t.attrib["PosX"]) for t in tiles]
                    ys_m = [float(t.attrib["PosY"]) for t in tiles]
                    min_x, min_y = min(xs_m), min(ys_m)
                    tile_positions_px = [
                        (
                            int(round((xm - min_x) * 1e6 / px_um)),
                            int(round((ym - min_y) * 1e6 / py_um)),
                        )
                        for xm, ym in zip(xs_m, ys_m)
                    ]
                elif all("FieldX" in t.attrib and "FieldY" in t.attrib for t in tiles):
                    fxs = [int(t.attrib["FieldX"]) for t in tiles]
                    fys = [int(t.attrib["FieldY"]) for t in tiles]
                    min_fx, min_fy = min(fxs), min(fys)
                    tile_positions_px = [
                        ((fx - min_fx) * width, (fy - min_fy) * height)
                        for fx, fy in zip(fxs, fys)
                    ]
            except (KeyError, ValueError):
                tile_positions_px = None

    return _LifSeriesSpec(
        name=elem.attrib.get("Name") or "Series",
        mem_id=mem_id,
        data_offset=data_offset,
        mem_size=min(declared_size, block_size),
        width=width,
        height=height,
        inc_x=inc_x,
        inc_y=inc_y,
        n_z=n_z,
        inc_z=inc_z,
        n_t=n_t,
        inc_t=inc_t,
        n_m=n_m,
        inc_m=inc_m,
        is_lambdascan=is_lambdascan,
        dtype=dtype,
        rgb=rgb,
        channels=channels,
        pixel_size_um=pixel_size_um,
        calibration_source=cal_source,
        magnification=magnification,
        objective_name=objective_name,
        tile_positions_px=tile_positions_px,
        z_step_um=z_step_um,
    )


def _axis_pixel_size_um(dim_info: dict[str, Any]) -> float | None:
    unit = (dim_info.get("unit") or "").lower()
    length_raw = dim_info.get("length")
    n_el = int(dim_info.get("n", 1) or 0)
    if n_el <= 1 or length_raw is None:
        return None
    try:
        span = abs(float(length_raw))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(span) or span <= 0:
        return None
    denom = n_el - 1
    if unit == "m":
        um = (span * 1e6) / denom
    elif unit in ("um", "µm", "μm"):
        um = span / denom
    elif unit == "mm":
        um = (span * 1e3) / denom
    else:
        return None
    return um if _finite_positive(um) else None


def _list_lif_series(
    root: ET.Element, blocks: dict[str, tuple[int, int]]
) -> list[_LifSeriesSpec]:
    series: list[_LifSeriesSpec] = []
    for elem in root.iter("Element"):
        spec = _parse_series_element(elem, blocks)
        if spec is not None:
            series.append(spec)
    if not series:
        raise ValueError("Leica LIF file contains no readable image series")
    return series


def _read_lif_channel_window(
    fd: int,
    series: _LifSeriesSpec,
    ch: _LifChannelSpec,
    z_idx: int,
    t_idx: int,
    m_idx: int,
    ry0: int,
    ry1: int,
    rx0: int,
    rx1: int,
) -> np.ndarray:
    """Read [ry0:ry1, rx0:rx1] for one channel in a single series/tile plane."""
    h_win = ry1 - ry0
    w_win = rx1 - rx0
    ch_dtype = ch.dtype
    ch_itemsize = ch_dtype.itemsize
    plane_base = (
        series.data_offset
        + z_idx * series.inc_z
        + t_idx * series.inc_t
        + m_idx * series.inc_m
    )

    # Fast path 1: planar channel with contiguous X pixels of ch_dtype
    if series.inc_x == ch_itemsize and series.inc_y == series.width * ch_itemsize:
        span_offset = plane_base + ch.bytes_inc + ry0 * series.inc_y
        span_bytes = h_win * series.inc_y
        raw = read_at(fd, span_bytes, span_offset)
        rows = np.frombuffer(raw, dtype=ch_dtype).reshape(h_win, series.width)
        return np.ascontiguousarray(rows[:, rx0:rx1], dtype=series.dtype)

    # Fast path 2: pixel-interleaved channels (homogeneous or mixed-depth)
    if (
        series.inc_y == series.width * series.inc_x
        and 0 <= ch.bytes_inc
        and ch.bytes_inc + ch_itemsize <= series.inc_x
    ):
        span_offset = plane_base + ry0 * series.inc_y
        span_bytes = h_win * series.inc_y
        raw = read_at(fd, span_bytes, span_offset)
        strided = np.ndarray(
            shape=(h_win, w_win),
            dtype=ch_dtype,
            buffer=raw,
            offset=ch.bytes_inc + rx0 * series.inc_x,
            strides=(series.inc_y, series.inc_x),
        )
        return np.array(strided, dtype=series.dtype, copy=True)

    # General stride path: read each row span with ch_dtype and step by inc_x
    out = np.empty((h_win, w_win), dtype=series.dtype)
    row_span_bytes = (w_win - 1) * series.inc_x + ch_itemsize
    for r_i, y in enumerate(range(ry0, ry1)):
        row_offset = plane_base + ch.bytes_inc + y * series.inc_y + rx0 * series.inc_x
        raw_row = read_at(fd, row_span_bytes, row_offset)
        if series.inc_x == ch_itemsize:
            out[r_i] = np.frombuffer(raw_row, dtype=ch_dtype, count=w_win)
        else:
            strided = np.ndarray(
                shape=(w_win,),
                dtype=ch_dtype,
                buffer=raw_row,
                strides=(series.inc_x,),
            )
            out[r_i] = strided
    return out


def _grid_chunks(size: int, step: int) -> tuple[int, ...]:
    n_full, rem = divmod(size, step)
    return tuple([step] * n_full + ([rem] if rem else []))


def open_lif(
    stack: ExitStack,
    path: str | Path,
    selection: PlaneSelection | None = None,
    tile: int = 512,
) -> PlaneSource:
    """Open one plane (or Z/Lambda maximum-intensity projection) of a Leica LIF file."""
    import dask.array as da

    path = Path(path)
    selection = selection or PlaneSelection()
    file_size = path.stat().st_size
    fd = os.open(str(path), os.O_RDONLY | BINARY)
    stack.callback(os.close, fd)

    root, blocks = _read_lif_header_and_blocks(fd, file_size)
    all_series = _list_lif_series(root, blocks)
    n_p = len(all_series)
    if not 0 <= selection.p < n_p:
        raise ValueError(f"--position {selection.p} out of range (P={n_p})")

    series = all_series[selection.p]
    if not 0 <= selection.t < series.n_t:
        raise ValueError(f"--t {selection.t} out of range (T={series.n_t})")

    t_idx = selection.t
    n_z = series.n_z
    n_c = len(series.channels)

    if series.tile_positions_px and series.n_m > 1:
        tile_coords = series.tile_positions_px
        width = max(tx + series.width for tx, _ty in tile_coords)
        height = max(ty + series.height for _tx, ty in tile_coords)
    else:
        tile_coords = [(0, 0)]
        width = series.width
        height = series.height

    notes: list[str] = [f"Leica LIF series {selection.p} '{series.name}' ({width} x {height})"]
    if n_p > 1:
        notes.append(f"file has {n_p} series; converting position {selection.p} only")
    if series.n_t > 1:
        notes.append(f"series has {series.n_t} timepoints; converting t={t_idx}")
    if series.is_lambdascan:
        notes.append(f"LambdaScan: {n_z} spectral emission bands mapped to Z axis")
    if len(tile_coords) > 1:
        notes.append(f"stitched {len(tile_coords)} stage tiles")

    def make_z_dask(z_idx: int) -> Any:
        def fetch_block(block_info=None):
            (ci0, ci1), (by0, by1), (bx0, bx1) = block_info[None]["array-location"]
            out = np.zeros((ci1 - ci0, by1 - by0, bx1 - bx0), dtype=series.dtype)
            for slot, ci in enumerate(range(ci0, ci1)):
                ch_spec = series.channels[ci]
                for m_idx, (tx0, ty0) in enumerate(tile_coords):
                    tx1 = tx0 + series.width
                    ty1 = ty0 + series.height
                    oy0, oy1 = max(by0, ty0), min(by1, ty1)
                    ox0, ox1 = max(bx0, tx0), min(bx1, tx1)
                    if oy1 <= oy0 or ox1 <= ox0:
                        continue
                    patch = _read_lif_channel_window(
                        fd,
                        series,
                        ch_spec,
                        z_idx,
                        t_idx,
                        m_idx,
                        oy0 - ty0,
                        oy1 - ty0,
                        ox0 - tx0,
                        ox1 - tx0,
                    )
                    out[slot, oy0 - by0 : oy1 - by0, ox0 - bx0 : ox1 - bx0] = patch
            return out

        c_chunks = (3,) if series.rgb else (1,) * n_c
        return da.map_blocks(
            fetch_block,
            chunks=(c_chunks, _grid_chunks(height, tile), _grid_chunks(width, tile)),
            dtype=series.dtype,
        )

    if selection.z == "max" and n_z > 1:
        planes = [make_z_dask(zi) for zi in range(n_z)]
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
        data = make_z_dask(z_index)
        z_used = z_index
        if n_z > 1:
            notes.append(f"series has {n_z} Z planes; converting z={z_index}")

    if series.pixel_size_um is None:
        notes.append("no pixel calibration in the series; measurements are in pixels")

    channels_info = [ChannelInfo(ch.name, ch.color) for ch in series.channels]

    return PlaneSource(
        data=data,
        dtype=series.dtype,
        shape=(n_c, height, width),
        rgb=series.rgb,
        channels=channels_info,
        pixel_size_um=series.pixel_size_um,
        source_name=str(path),
        selection={"t": t_idx, "p": selection.p, "z": z_used},
        notes=notes,
        magnification=series.magnification,
        calibration_source=series.calibration_source,
        channel_dtypes=[str(ch.dtype) for ch in series.channels],
    )


def collect_lif_info(path: str | Path) -> dict[str, Any]:
    """`nd2wsi info` payload for a Leica LIF file."""
    path = Path(path)
    file_size = path.stat().st_size
    fd = os.open(str(path), os.O_RDONLY | BINARY)
    try:
        root, blocks = _read_lif_header_and_blocks(fd, file_size)
    finally:
        os.close(fd)
    all_series = _list_lif_series(root, blocks)
    s0 = all_series[0]
    if s0.tile_positions_px and s0.n_m > 1:
        width = max(tx + s0.width for tx, _ty in s0.tile_positions_px)
        height = max(ty + s0.height for _tx, ty in s0.tile_positions_px)
    else:
        width = s0.width
        height = s0.height

    return {
        "file": str(path),
        "size_bytes": file_size,
        "format": "Leica LIF (LAS AF / LAS X)",
        "series_name": s0.name,
        "width": width,
        "height": height,
        "channels": len(s0.channels),
        "z_planes": s0.n_z,
        "timepoints": s0.n_t,
        "series_count": len(all_series),
        "mosaic_tiles": s0.n_m,
        "is_lambdascan": s0.is_lambdascan,
        "dtype": str(s0.dtype),
        "rgb": s0.rgb,
        "pixel_size_um": list(s0.pixel_size_um) if s0.pixel_size_um else None,
        "calibration_source": s0.calibration_source,
        "magnification": s0.magnification,
        "objective_name": s0.objective_name,
        "channel_info": [
            {"name": ch.name, "color": "#{:02X}{:02X}{:02X}".format(*ch.color)}
            for ch in s0.channels
        ],
    }


def format_lif_info(info: dict[str, Any]) -> str:
    from .reader import nice_bytes

    lines = [
        f"File          : {info['file']} ({nice_bytes(info['size_bytes'])})",
        f"Format        : {info['format']}",
        f"Series 0      : {info['series_name']} (total series: {info['series_count']})",
        f"Dimensions    : {info['width']} x {info['height']} px, "
        f"C={info['channels']}, Z={info['z_planes']}, T={info['timepoints']}, "
        f"S={info['series_count']}, M={info['mosaic_tiles']}",
        f"Data type     : {info['dtype']} (RGB={info['rgb']})",
    ]
    if info.get("pixel_size_um"):
        py, px = info["pixel_size_um"]
        lines.append(f"Pixel size    : {px:.4f} x {py:.4f} µm/px ({info['calibration_source']})")
    else:
        lines.append("Pixel size    : uncalibrated")
    if info.get("magnification"):
        obj_suffix = f" ({info['objective_name']})" if info.get("objective_name") else ""
        lines.append(f"Objective     : {info['magnification']:g}x{obj_suffix}")
    lines.append("Channels      :")
    for ch in info.get("channel_info", []):
        lines.append(f"  - {ch['name']} ({ch['color']})")
    return "\n".join(lines)
