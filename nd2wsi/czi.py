"""Zeiss CZI (ZISRAW) input: produces the same PlaneSource as ND2 and SVS.

Supports both 2D whole-slide/plane scans (including multi-tile M mosaics and
RGB brightfield scans) and multi-channel / Z-stack confocal acquisitions.
"""

from __future__ import annotations

import os
import struct
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import numpy as np

from .czi_codecs import (
    _COMPRESSION_NAMES,
    _MAX_SUBBLOCK_BYTES,
    _PIXEL_TYPE_NAMES,
    _PIXEL_TYPES,
    _decode_subblock_bytes,
    _resolve_czi_entries_spec,
    _SubBlockEntry,
)
from .czi_codecs import _ZSTD_FRAME_MAGIC as _ZSTD_FRAME_MAGIC
from .czi_codecs import _zstd1_payload_bytes as _zstd1_payload_bytes
from .czi_metadata import _CHANNEL_ID_RE as _CHANNEL_ID_RE
from .czi_metadata import _DEFAULT_PALETTE as _DEFAULT_PALETTE
from .czi_metadata import _DTD_BYTES_RE as _DTD_BYTES_RE
from .czi_metadata import (
    _MAX_XML_BYTES,
    _extract_czi_metadata,
    _parse_czi_xml_bytes,
)
from .czi_metadata import _czi_channel_index as _czi_channel_index
from .czi_metadata import _extract_czi_z_step_um as _extract_czi_z_step_um
from .czi_metadata import _finite_positive as _finite_positive
from .czi_metadata import _first_nonempty as _first_nonempty
from .czi_metadata import _map_czi_channels as _map_czi_channels
from .czi_metadata import _parse_hex_color as _parse_hex_color
from .platform_io import BINARY, read_at
from .reader import PlaneSelection, PlaneSource, _mid

CZI_SUFFIXES = {".czi"}

_MAX_DIR_BYTES = 128 * 1024 * 1024
_MAX_DIR_ENTRIES = 5_000_000
_MAX_MOSAIC_SIDE = 1_000_000


def is_czi(path: str | Path) -> bool:
    return Path(path).suffix.lower() in CZI_SUFFIXES


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
    if entry_count < 0 or entry_count > _MAX_DIR_ENTRIES:
        raise ValueError(f"invalid CZI SubBlock count {entry_count}")

    # Read the directory payload in one positional read for fast parsing
    avail_dir = file_size - (dir_pos + 160)
    if used_size > _MAX_DIR_BYTES:
        raise ValueError(f"CZI SubBlock directory size {used_size} exceeds safety limit")
    max_dir_bytes = min(avail_dir, _MAX_DIR_BYTES, max(used_size, entry_count * 256))
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
            if 0 < xml_size <= _MAX_XML_BYTES and meta_pos + 288 + xml_size <= file_size:
                xml_bytes = read_at(fd, xml_size, meta_pos + 288)
                xml_root = _parse_czi_xml_bytes(xml_bytes)

    return entries, xml_root


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

    file_size = os.fstat(fd).st_size
    if sb.file_pos < 0 or sb.file_pos > file_size - 48:
        raise ValueError(f"invalid CZI SubBlock header: file offset {sb.file_pos} exceeds file bounds")
    sb_hdr = read_at(fd, min(80, file_size - sb.file_pos), sb.file_pos)
    if len(sb_hdr) < 48 or not sb_hdr[:14].startswith(b"ZISRAWSUBBLOCK"):
        raise ValueError(f"invalid CZI SubBlock header at offset {sb.file_pos}")
    meta_size, _att_size, data_size = struct.unpack("<IIQ", sb_hdr[32:48])
    hdr_ndims = sb.ndims
    if len(sb_hdr) >= 80 and sb_hdr[48:50] == b"DV":
        hdr_ptype = struct.unpack_from("<i", sb_hdr, 50)[0]
        if hdr_ptype != sb.pixel_type:
            raise ValueError(
                f"CZI SubBlock header PixelType ({hdr_ptype}) disagrees with directory ({sb.pixel_type})"
            )
        hdr_comp = struct.unpack_from("<i", sb_hdr, 66)[0]
        if hdr_comp != sb.compression:
            raise ValueError(
                f"CZI SubBlock header Compression ({hdr_comp}) disagrees with directory ({sb.compression})"
            )
        hdr_ndims = struct.unpack_from("<i", sb_hdr, 76)[0]
        if not (0 <= hdr_ndims <= 64):
            raise ValueError(f"invalid CZI SubBlock dimension count {hdr_ndims}")
    header_len = max(256, 16 + 32 + 20 * hdr_ndims)
    data_offset = sb.file_pos + 32 + header_len + meta_size
    file_size = os.fstat(fd).st_size
    if data_offset < 0 or data_offset > file_size:
        raise ValueError(f"CZI SubBlock data offset {data_offset} exceeds file bounds")

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
        if span_bytes > _MAX_SUBBLOCK_BYTES or span_offset + span_bytes > file_size:
            raise ValueError(f"CZI SubBlock span ({span_bytes} B) exceeds bounds")
        raw = read_at(fd, span_bytes, span_offset)
        rows = np.frombuffer(raw, dtype=sb_dtype).reshape(ry1 - ry0, sb.x_stored, sb_samples)
        if sb_samples == 1:
            return np.ascontiguousarray(rows[:, rx0:rx1, 0], dtype=dtype)
        return np.ascontiguousarray(rows[:, rx0:rx1, :3][:, :, ::-1], dtype=dtype)

    if int(data_size) > _MAX_SUBBLOCK_BYTES or data_offset + int(data_size) > file_size:
        raise ValueError(f"CZI SubBlock payload size ({data_size} B) exceeds safety bounds")
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
    n_p = len(s_values)
    if not 0 <= selection.p < n_p:
        raise ValueError(f"--position {selection.p} out of range (P={n_p})")

    s_target = s_values[selection.p]
    s_entries = [e for e in lvl0 if e.s == s_target]
    t_values = sorted({e.t for e in s_entries})
    n_t = len(t_values)
    if not 0 <= selection.t < n_t:
        raise ValueError(f"--t {selection.t} out of range (T={n_t})")

    t_target = t_values[selection.t]
    st_entries = [e for e in s_entries if e.t == t_target]
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
    if not (0 < width <= _MAX_MOSAIC_SIDE and 0 < height <= _MAX_MOSAIC_SIDE):
        raise ValueError(f"CZI mosaic canvas {width}x{height} exceeds safety limit")

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
        czi_scene_t=[int(s_target), int(t_target)],
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
