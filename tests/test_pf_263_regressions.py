"""Regression tests for v2.6.3 PF-1 through PF-7 fixes."""

from __future__ import annotations

import io
import json
import os
import struct
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import imagecodecs
import numpy as np
import pytest
import tifffile
from PIL import Image

from nd2wsi.convert import (
    _store_matches_source_semantics,
    convert,
    ensure_cache,
    existing_cache_store,
    open_store,
)
from nd2wsi.czi import (
    _decode_subblock_bytes,
    _extract_czi_metadata,
    _read_subblock_window,
    _resolve_czi_entries_spec,
    _SubBlockEntry,
    open_czi,
)
from nd2wsi.direct import open_direct
from nd2wsi.direct_common import _SvsLevel
from nd2wsi.lif import collect_lif_info, open_lif
from nd2wsi.platform_io import BINARY
from nd2wsi.reader import (
    PlaneSelection,
    _channel_infos,
    _nd2_pixel_size,
    _nd2_raw_planes,
    is_source_backable,
    objective_magnification,
    open_plane,
    plane_view,
)
from nd2wsi.render import (
    compute_histograms,
    export_roi_rendered,
    export_roi_tiff,
    parse_windows,
    render_tile,
)
from nd2wsi.svs import collect_svs_info, open_svs


def _strip_zstd_fcs(frame: bytes) -> bytes:
    """Rewrite a single-segment Zstd frame to omit Frame_Content_Size."""
    fhd = frame[4]
    fcs_flag = fhd >> 6
    single = (fhd >> 5) & 1
    did_flag = fhd & 3
    fcs_len = {0: (1 if single else 0), 1: 2, 2: 4, 3: 8}[fcs_flag]
    wd_len = 0 if single else 1
    did_len = {0: 0, 1: 1, 2: 2, 3: 4}[did_flag]
    hdr_end = 5 + wd_len + did_len + fcs_len
    assert fcs_len > 0
    new_fhd = (fhd & 0b00011111) & ~0b00100000
    window = bytes([0x58])
    return (
        frame[:4]
        + bytes([new_fhd])
        + (frame[5 : 5 + wd_len] if wd_len else window)
        + frame[5 + wd_len : 5 + wd_len + did_len]
        + frame[hdr_end:]
    )


def _pack_zstd1(
    raw_bytes: bytes,
    *,
    itemsize: int = 1,
    hilo: bool = True,
    hdr_len: int = 3,
    strip_fcs: bool = False,
) -> bytes:
    """Build a CZI Zstd1 payload using only imagecodecs.zstd_encode."""
    if hilo and itemsize > 1:
        packed_bytes = np.ascontiguousarray(
            np.frombuffer(raw_bytes, dtype=np.uint8).reshape(-1, itemsize).T
        ).tobytes()
    else:
        packed_bytes = raw_bytes
    zstd_frame = imagecodecs.zstd_encode(packed_bytes)
    if strip_fcs:
        zstd_frame = _strip_zstd_fcs(zstd_frame)
    if hdr_len == 1:
        hdr = bytes([1])
    elif hdr_len >= 3:
        hdr = bytes([hdr_len, 1, 1 if hilo else 0]) + b"\x00" * (hdr_len - 3)
    else:
        raise ValueError(f"unsupported hdr_len {hdr_len}")
    return hdr + zstd_frame


def _make_subblock_entry(
    *,
    file_pos: int = 0,
    pixel_type: int = 1,
    compression: int = 0,
    pyramid_type: int = 0,
    ndims: int = 6,
    x_start: int = 0,
    x_size: int = 16,
    x_stored: int = 16,
    y_start: int = 0,
    y_size: int = 12,
    y_stored: int = 12,
    c: int = 0,
    z: int = 0,
    t: int = 0,
    s: int = 0,
    m: int = 0,
) -> _SubBlockEntry:
    return _SubBlockEntry(
        file_pos=file_pos,
        pixel_type=pixel_type,
        compression=compression,
        pyramid_type=pyramid_type,
        ndims=ndims,
        x_start=x_start,
        x_size=x_size,
        x_stored=x_stored,
        y_start=y_start,
        y_size=y_size,
        y_stored=y_stored,
        c=c,
        z=z,
        t=t,
        s=s,
        m=m,
    )


def _write_custom_czi(
    path: Path,
    records: list[dict[str, Any]],
    *,
    xml_str: str | None = None,
) -> Path:
    """Write a custom synthetic CZI with per-SubBlock control over headers and dimensions."""
    buf = bytearray(512)
    dir_entries: list[tuple[int, int, int, list[tuple[bytes, int, int]]]] = []

    for rec in records:
        arr: np.ndarray = rec["arr"]
        ptype: int = int(rec["pixel_type"])
        comp: int = int(rec.get("compression", 0))
        s_val = int(rec.get("s", 0))
        t_val = int(rec.get("t", 0))
        z_val = int(rec.get("z", 0))
        c_val = int(rec.get("c", 0))
        m_val = int(rec.get("m", 0))
        xs = int(rec.get("x_start", 0))
        ys = int(rec.get("y_start", 0))
        local_extra_dims: int = int(rec.get("local_extra_dims", 0))

        if arr.ndim == 2:
            h, w = arr.shape
            raw_arr = arr
        else:
            h, w, s_cnt = arr.shape
            raw_arr = arr[:, :, [2, 1, 0, 3]] if s_cnt == 4 else arr[:, :, ::-1]
        raw_bytes = np.ascontiguousarray(raw_arr).tobytes()

        if "payload" in rec:
            payload = bytes(rec["payload"])
        elif comp == 0:
            payload = raw_bytes
        elif comp == 5:
            payload = imagecodecs.zstd_encode(raw_bytes)
        elif comp == 6:
            payload = _pack_zstd1(
                raw_bytes,
                itemsize=arr.dtype.itemsize,
                hilo=bool(rec.get("hilo", True)),
            )
        else:
            raise ValueError(f"Unsupported test compression {comp}")

        base_dims = [
            (b"X\x00\x00\x00", xs, w),
            (b"Y\x00\x00\x00", ys, h),
            (b"C\x00\x00\x00", c_val, 1),
            (b"Z\x00\x00\x00", z_val, 1),
            (b"M\x00\x00\x00", m_val, 1),
            (b"T\x00\x00\x00", t_val, 1),
            (b"S\x00\x00\x00", s_val, 1),
        ]
        local_dims = list(base_dims) + [
            (f"D{i}".encode("ascii").ljust(4, b"\x00"), 0, 1)
            for i in range(local_extra_dims)
        ]
        local_ndims = len(local_dims)
        hdr_len = max(256, 16 + 32 + 20 * local_ndims)
        sb_prefix = bytearray(b"\xAA" * hdr_len)
        struct.pack_into("<IIQ", sb_prefix, 0, 0, 0, len(payload))
        fpos = len(buf)
        struct.pack_into(
            "<2siqiiBB4si",
            sb_prefix,
            16,
            b"DV",
            ptype,
            fpos,
            0,
            comp,
            0,
            0,
            b"\x00" * 4,
            local_ndims,
        )
        for idx, (dname, st_i, sz_i) in enumerate(local_dims):
            struct.pack_into(
                "<4siifi", sb_prefix, 48 + idx * 20, dname, st_i, sz_i, float(st_i), sz_i
            )

        used_size = hdr_len + len(payload)
        alloc_size = (used_size + 31) // 32 * 32
        buf.extend(struct.pack("<16sQQ", b"ZISRAWSUBBLOCK\x00\x00", alloc_size, used_size))
        buf.extend(sb_prefix)
        buf.extend(payload)
        if alloc_size > used_size:
            buf.extend(b"\x00" * (alloc_size - used_size))

        dir_dims = base_dims[: int(rec.get("dir_ndims", len(base_dims)))]
        dir_entries.append((fpos, ptype, comp, dir_dims))

    dir_pos = len(buf)
    dir_payload = bytearray(128)
    struct.pack_into("<i", dir_payload, 0, len(dir_entries))
    for fpos, ptype, comp, dir_dims in dir_entries:
        dir_payload.extend(
            struct.pack(
                "<2siqiiBB4si",
                b"DV",
                ptype,
                fpos,
                0,
                comp,
                0,
                0,
                b"\x00" * 4,
                len(dir_dims),
            )
        )
        for dname, st_i, sz_i in dir_dims:
            dir_payload.extend(struct.pack("<4siifi", dname, st_i, sz_i, float(st_i), sz_i))
    dir_used = len(dir_payload)
    dir_alloc = (dir_used + 31) // 32 * 32
    buf.extend(struct.pack("<16sQQ", b"ZISRAWDIRECTORY\x00", dir_alloc, dir_used))
    buf.extend(dir_payload)
    if dir_alloc > dir_used:
        buf.extend(b"\x00" * (dir_alloc - dir_used))

    meta_pos = 0
    if xml_str is not None:
        meta_pos = len(buf)
        xml_bytes = xml_str.encode("utf-8")
        meta_hdr = bytearray(256)
        struct.pack_into("<II", meta_hdr, 0, len(xml_bytes), 0)
        meta_used = 256 + len(xml_bytes)
        meta_alloc = (meta_used + 31) // 32 * 32
        buf.extend(struct.pack("<16sQQ", b"ZISRAWMETADATA\x00\x00", meta_alloc, meta_used))
        buf.extend(meta_hdr)
        buf.extend(xml_bytes)
        if meta_alloc > meta_used:
            buf.extend(b"\x00" * (meta_alloc - meta_used))

    struct.pack_into("<16sQQ", buf, 0, b"ZISRAWFILE\x00\x00\x00\x00\x00\x00", 480, 80)
    struct.pack_into(
        "<iiii16s16siQQiQ",
        buf,
        32,
        1,
        0,
        0,
        0,
        b"\x01" * 16,
        b"\x02" * 16,
        0,
        dir_pos,
        meta_pos,
        0,
        0,
    )
    path.write_bytes(bytes(buf))
    return path


def _write_raw_lif(path: Path, xml_body: str, mem_blocks: list[tuple[str, bytes]]) -> Path:
    xml_str = (
        '<?xml version="1.0" encoding="utf-16"?>'
        f'<LMSDataContainerHeader Version="2">{xml_body}</LMSDataContainerHeader>'
    )
    xml_utf16 = xml_str.encode("utf-16-le")
    buf = bytearray()
    buf.extend(struct.pack("<iIBI", 0x70, 5 + len(xml_utf16), 0x2A, len(xml_utf16) // 2))
    buf.extend(xml_utf16)
    for mem_id, payload in mem_blocks:
        id_utf16 = mem_id.encode("utf-16-le")
        chunk_len = 1 + 8 + 1 + 4 + len(id_utf16)
        buf.extend(
            struct.pack("<iIBQBI", 0x70, chunk_len, 0x2A, len(payload), 0x2A, len(id_utf16) // 2)
        )
        buf.extend(id_utf16)
        buf.extend(payload)
    path.write_bytes(bytes(buf))
    return path


# ---------------------------------------------------------------------------
# PF-1: CZI Zstd1 4-byte HiLo float32/Bgr96Float, extended headers, plain Zstd,
#       and :zstd1 channel_dtypes cache invalidation
# ---------------------------------------------------------------------------


def test_pf1_czi_zstd1_float32_and_bgr96float_hilo_roundtrip(tmp_path: Path):
    rng = np.random.default_rng(2026)
    f32_plane = (rng.standard_normal((48, 64)) * 125.0 + 37.5).astype(np.float32)
    czi_gray = _write_custom_czi(
        tmp_path / "gray32_zstd1.czi",
        [{"arr": f32_plane, "pixel_type": 2, "compression": 6, "hilo": True}],
    )
    with ExitStack() as stack:
        src = open_czi(stack, czi_gray, tile=32)
        assert src.dtype == np.dtype(np.float32)
        assert src.channel_dtypes == ["float32"]
        got = np.asarray(src.data.compute())
        assert np.array_equal(got[0], f32_plane)

    rgb_f32 = (rng.uniform(-50.0, 500.0, size=(40, 56, 3))).astype(np.float32)
    czi_rgb = _write_custom_czi(
        tmp_path / "bgr96_zstd1.czi",
        [{"arr": rgb_f32, "pixel_type": 8, "compression": 6, "hilo": True}],
    )
    with ExitStack() as stack:
        src_rgb = open_czi(stack, czi_rgb, tile=32)
        assert src_rgb.rgb is True
        assert src_rgb.dtype == np.dtype(np.float32)
        assert src_rgb.channel_dtypes == ["Bgr96Float"] * 3
        got_rgb = np.moveaxis(np.asarray(src_rgb.data.compute()), 0, -1)
        assert np.array_equal(got_rgb, rgb_f32)


def test_pf1_czi_zstd1_extended_header_plain_zstd_zstd0_and_errors(monkeypatch):
    rng = np.random.default_rng(7)
    h, w = 12, 16

    # Extended Zstd1 header (hdr_len = 5 > 3) with 4-byte float32 HiLo packing
    f32 = rng.uniform(-10.0, 20.0, size=(h, w)).astype(np.float32)
    ext_zstd1 = _pack_zstd1(f32.tobytes(), itemsize=4, hilo=True, hdr_len=5)
    sb_f32 = _make_subblock_entry(pixel_type=2, compression=6, y_size=h, y_stored=h, x_size=w, x_stored=w)
    assert np.array_equal(_decode_subblock_bytes(ext_zstd1, sb_f32, np.dtype(np.float32), 1), f32)

    # Zstd frame without Frame_Content_Size (hdr_len=1 and hdr_len=3+hilo)
    u16 = rng.integers(0, 65535, size=(h, w), dtype=np.uint16)
    sb_u16 = _make_subblock_entry(pixel_type=1, compression=6, y_size=h, y_stored=h, x_size=w, x_stored=w)
    fcs_less_plain = _pack_zstd1(u16.tobytes(), itemsize=2, hilo=False, hdr_len=1, strip_fcs=True)
    assert np.array_equal(_decode_subblock_bytes(fcs_less_plain, sb_u16, np.dtype(np.uint16), 1), u16)
    fcs_less_hilo = _pack_zstd1(f32.tobytes(), itemsize=4, hilo=True, hdr_len=3, strip_fcs=True)
    assert np.array_equal(_decode_subblock_bytes(fcs_less_hilo, sb_f32, np.dtype(np.float32), 1), f32)

    # Plain Zstd frame (0x28B52FFD magic, no Zstd1 header) on comp == 6 Bgr48 (pixel_type=4)
    rgb16 = rng.integers(0, 65535, size=(h, w, 3), dtype=np.uint16)
    bgr16_bytes = np.ascontiguousarray(rgb16[:, :, ::-1]).tobytes()
    plain_zstd = imagecodecs.zstd_encode(bgr16_bytes)
    sb_bgr48 = _make_subblock_entry(pixel_type=4, compression=6, y_size=h, y_stored=h, x_size=w, x_stored=w)
    assert np.array_equal(_decode_subblock_bytes(plain_zstd, sb_bgr48, np.dtype(np.uint16), 3), rgb16)

    # Even if imagecodecs.zstd1_decode is unavailable or raises (Python 3.11 / older imagecodecs),
    # compression 6 still decodes cleanly via imagecodecs.zstd_decode.
    monkeypatch.setattr(
        imagecodecs,
        "zstd1_decode",
        lambda *_a, **_k: (_ for _ in ()).throw(AttributeError("zstd1_decode unavailable")),
        raising=False,
    )
    assert np.array_equal(_decode_subblock_bytes(ext_zstd1, sb_f32, np.dtype(np.float32), 1), f32)
    assert np.array_equal(_decode_subblock_bytes(fcs_less_plain, sb_u16, np.dtype(np.uint16), 1), u16)

    # comp == 5 (Zstd0) continues using zstd_decode
    sb_zstd0 = _make_subblock_entry(pixel_type=2, compression=5, y_size=h, y_stored=h, x_size=w, x_stored=w)
    assert np.array_equal(
        _decode_subblock_bytes(imagecodecs.zstd_encode(f32.tobytes()), sb_zstd0, np.dtype(np.float32), 1),
        f32,
    )

    # Empty Zstd1 payload raises ValueError
    with pytest.raises(ValueError, match="empty CZI Zstd1 SubBlock payload"):
        _decode_subblock_bytes(b"", sb_f32, np.dtype(np.float32), 1)

    # Truncated decoded payload raises ValueError
    short_raw = _pack_zstd1(f32[:4, :4].tobytes(), itemsize=4, hilo=True)
    with pytest.raises(ValueError, match="smaller than expected"):
        _decode_subblock_bytes(short_raw, sb_f32, np.dtype(np.float32), 1)

    # Surplus decoded bytes must raise ValueError (never silently crop)
    surplus_raw = _pack_zstd1(np.zeros((h + 2, w), dtype=np.float32).tobytes(), itemsize=4, hilo=True)
    with pytest.raises(ValueError, match="larger than expected"):
        _decode_subblock_bytes(surplus_raw, sb_f32, np.dtype(np.float32), 1)

    # Corrupt Frame_Content_Size claiming huge allocation must raise ValueError (never MemoryError)
    bogus_fcs_frame = b"\x28\xb5\x2f\xfd\xe0" + struct.pack("<Q", 1 << 40) + b"\x00\x00\x00"
    with pytest.raises(ValueError, match="invalid CZI Zstd1 SubBlock payload"):
        _decode_subblock_bytes(bytes([1]) + bogus_fcs_frame, sb_f32, np.dtype(np.float32), 1)


def test_pf1_czi_zstd1_reuses_262_cache_without_zstd1_tag(tmp_path: Path):
    # Unit verification across grayscale, mixed compression multi-channel, and RGB
    dt, rgb, ch_dtypes = _resolve_czi_entries_spec(
        [
            _make_subblock_entry(pixel_type=1, compression=6, c=0),
            _make_subblock_entry(pixel_type=1, compression=5, c=1),
        ]
    )
    assert dt == np.dtype(np.uint16)
    assert rgb is False
    assert ch_dtypes == ["uint16", "uint16"]

    _, rgb2, ch_dtypes2 = _resolve_czi_entries_spec(
        [_make_subblock_entry(pixel_type=4, compression=6, c=0)]
    )
    assert rgb2 is True
    assert ch_dtypes2 == ["uint16"] * 3

    _, _, ch_dtypes_no_zstd1 = _resolve_czi_entries_spec(
        [_make_subblock_entry(pixel_type=2, compression=5, c=0)]
    )
    assert ch_dtypes_no_zstd1 == ["float32"]

    # End-to-end reuse: a 2.6.2 store recorded ["uint16", "uint16"] (without ":zstd1")
    # and must be accepted and reused as-is without quarantine or rebuild.
    rng = np.random.default_rng(19)
    ch0 = rng.integers(100, 50000, size=(64, 64), dtype=np.uint16)
    ch1 = rng.integers(100, 50000, size=(64, 64), dtype=np.uint16)
    czi_path = _write_custom_czi(
        tmp_path / "mixed_zstd1.czi",
        [
            {"arr": ch0, "pixel_type": 1, "compression": 6, "c": 0, "hilo": True},
            {"arr": ch1, "pixel_type": 1, "compression": 5, "c": 1},
        ],
    )

    legacy_dir = czi_path.with_suffix(".ome.zarr")
    convert(czi_path, legacy_dir, tile=64, progress=False)
    zattrs_path = legacy_dir / ".zattrs"
    raw_zattrs = json.loads(zattrs_path.read_text(encoding="utf-8"))
    assert raw_zattrs["nd2wsi"]["channel_dtypes"] == ["uint16", "uint16"]
    assert _store_matches_source_semantics(czi_path, PlaneSelection(), raw_zattrs) is True
    assert existing_cache_store(czi_path) == legacy_dir
    assert ensure_cache(czi_path, tile=64) == legacy_dir
    assert not list(tmp_path.glob("*.corrupt-*"))
    root, attrs = open_store(legacy_dir)
    try:
        assert attrs["nd2wsi"]["channel_dtypes"] == ["uint16", "uint16"]
        assert np.array_equal(np.asarray(root["0"][0]), ch0)
        assert np.array_equal(np.asarray(root["0"][1]), ch1)
    finally:
        if hasattr(root, "close"):
            root.close()


# ---------------------------------------------------------------------------
# PF-2: LIF series XML scoping to ./Data/Image (no bleed into ./Children)
#       and non-finite / overflowing TileScanInfo guards
# ---------------------------------------------------------------------------


def test_pf2_lif_series_does_not_bleed_metadata_from_children_elements(tmp_path: Path):
    h, w, n_m = 32, 40, 2
    parent_arr = np.arange(n_m * 2 * h * w, dtype=np.uint8).reshape(n_m, 2, h, w)
    child_arr = np.zeros((n_m, 2, h, w), dtype=np.uint8)

    parent_dims = (
        f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="1" Length="{(w - 1) * 0.5e-6:.9e}" Unit="m"/>'
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w}" Length="{(h - 1) * 0.5e-6:.9e}" Unit="m"/>'
        f'<DimensionDescription DimID="10" NumberOfElements="{n_m}" BytesInc="{2 * h * w}" Length="1" Unit="m"/>'
    )
    parent_chans = (
        '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green" BytesInc="0"/>'
        f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Red" BytesInc="{h * w}"/>'
    )
    child_extra = (
        '<Attachment Name="HardwareSetting">'
        '<ATLConfocalSettingDefinition Magnification="100" ObjectiveName="Child 100x Oil">'
        '<MultiBand Channel="1" DyeName="Leica/ChildDyeA"/>'
        '<MultiBand Channel="2" DyeName="Leica/ChildDyeB"/>'
        '<Detector Channel="1" IsActive="1"/>'
        '<Detector Channel="2" IsActive="1"/>'
        "</ATLConfocalSettingDefinition></Attachment>"
        '<Attachment Name="TileScanInfo">'
        '<Tile FieldX="0" FieldY="0" PosX="0.0" PosY="0.0"/>'
        '<Tile FieldX="0" FieldY="5" PosX="0.0" PosY="5.0e-4"/>'
        "</Attachment>"
    )

    xml_body = (
        '<Element Name="ParentSeries">'
        "<Data><Image>"
        f"<ImageDescription><Channels>{parent_chans}</Channels><Dimensions>{parent_dims}</Dimensions></ImageDescription>"
        "</Image></Data>"
        f'<Memory Size="{len(parent_arr.tobytes())}" MemoryBlockID="Mem_Parent"/>'
        "<Children>"
        '<Element Name="ChildDeconv">'
        f"<Data><Image>{child_extra}"
        f"<ImageDescription><Channels>{parent_chans}</Channels><Dimensions>{parent_dims}</Dimensions></ImageDescription>"
        "</Image></Data>"
        f'<Memory Size="{len(child_arr.tobytes())}" MemoryBlockID="Mem_Child"/>'
        "</Element>"
        "</Children>"
        "</Element>"
    )
    lif_path = _write_raw_lif(
        tmp_path / "nested_children.lif",
        xml_body,
        [("Mem_Parent", parent_arr.tobytes()), ("Mem_Child", child_arr.tobytes())],
    )

    info = collect_lif_info(lif_path)
    assert info["series_count"] == 2
    assert info["series_name"] == "ParentSeries"
    # Parent has no ATL, no MultiBand dyes, and no TileScanInfo in its own ./Data/Image:
    # single-tile fallback gives width = w, height = h (NOT child's vertical PosY mosaic 1000 + h!)
    assert info["magnification"] is None
    assert info["objective_name"] is None
    assert [c["name"] for c in info["channel_info"]] == ["Green", "Red"]
    assert info["width"] == w
    assert info["height"] == h

    # Child series (p=1) retains its own metadata
    with ExitStack() as stack:
        src_child = open_lif(stack, lif_path, PlaneSelection(p=1), tile=32)
        assert src_child.magnification == 100.0
        assert [c.name for c in src_child.channels] == ["ChildDyeA", "ChildDyeB"]
        assert src_child.shape == (2, 1000 + h, w)


@pytest.mark.parametrize(
    ("pos_x0", "pos_x1"),
    [
        ("NaN", "1.0e-4"),
        ("0.0", "inf"),
        ("-inf", "0.0"),
        ("-1.0e308", "1.0e308"),
    ],
)
def test_pf2_lif_tilescan_nonfinite_and_overflow_positions_handled_safely(
    tmp_path: Path, pos_x0: str, pos_x1: str
):
    h, w, n_m = 24, 32, 2
    arr = np.ones((n_m, h, w), dtype=np.uint16) * 42
    dims = (
        f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="{(w - 1) * 0.25e-6:.9e}" Unit="m"/>'
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="{(h - 1) * 0.25e-6:.9e}" Unit="m"/>'
        f'<DimensionDescription DimID="10" NumberOfElements="{n_m}" BytesInc="{h * w * 2}" Length="1" Unit="m"/>'
    )
    chans = '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Gray" BytesInc="0"/>'
    tiles_xml = (
        '<Attachment Name="TileScanInfo">'
        f'<Tile PosX="{pos_x0}" PosY="0.0"/>'
        f'<Tile PosX="{pos_x1}" PosY="0.0"/>'
        "</Attachment>"
    )
    xml_body = (
        '<Element Name="BadTiles">'
        f"<Data><Image>{tiles_xml}"
        f"<ImageDescription><Channels>{chans}</Channels><Dimensions>{dims}</Dimensions></ImageDescription>"
        "</Image></Data>"
        f'<Memory Size="{len(arr.tobytes())}" MemoryBlockID="Mem_1"/>'
        "</Element>"
    )
    lif_path = _write_raw_lif(
        tmp_path / f"bad_tiles_{pos_x0}_{pos_x1}.lif",
        xml_body,
        [("Mem_1", arr.tobytes())],
    )
    with ExitStack() as stack:
        src = open_lif(stack, lif_path, tile=32)
        # Falls back cleanly to tile_positions_px=None -> (1, h, w) without raising ValueError/OverflowError
        assert src.shape == (1, h, w)
        assert np.array_equal(np.asarray(src.data.compute())[0], arr[0])


# ---------------------------------------------------------------------------
# PF-3: CZI SubBlock local DV header validation/hdr_ndims & scene-scoped T
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("comp", [0, 5])
def test_pf3_czi_subblock_local_dv_ndims_exceeding_256_header(tmp_path: Path, comp: int):
    rng = np.random.default_rng(303)
    plane = rng.integers(500, 40000, size=(32, 48), dtype=np.uint16)
    # Directory entry has only 6 dims (256-byte header if trusted), while local DV header has
    # 7 + 6 = 13 dims -> header_len = 16 + 32 + 20 * 13 = 308 > 256, filled with 0xAA sentinel bytes.
    czi_path = _write_custom_czi(
        tmp_path / f"ext_dims_comp{comp}.czi",
        [
            {
                "arr": plane,
                "pixel_type": 1,
                "compression": comp,
                "dir_ndims": 6,
                "local_extra_dims": 6,
            }
        ],
    )
    with ExitStack() as stack:
        src = open_czi(stack, czi_path, tile=32)
        got = np.asarray(src.data.compute())[0]
        assert np.array_equal(got, plane)


def test_pf3_czi_subblock_header_validation_errors(tmp_path: Path):
    sb = _make_subblock_entry(file_pos=0, pixel_type=1, compression=0, y_size=4, y_stored=4, x_size=4, x_stored=4)

    def _run_hdr(raw_bytes: bytes) -> np.ndarray:
        p = tmp_path / "hdr_case.bin"
        p.write_bytes(raw_bytes)
        fd = os.open(str(p), os.O_RDONLY | BINARY)
        try:
            return _read_subblock_window(fd, sb, np.dtype(np.uint16), 1, 0, 4, 0, 4)
        finally:
            os.close(fd)

    # 1. Truncated header (< 48 bytes)
    with pytest.raises(ValueError, match="invalid CZI SubBlock header"):
        _run_hdr(b"ZISRAWSUBBLOCK\x00\x00" + b"\x00" * 16)

    # 2. Invalid segment magic
    with pytest.raises(ValueError, match="invalid CZI SubBlock header"):
        _run_hdr(b"NOT_A_SUBBLOCK\x00\x00" + b"\x00" * 80)

    def _make_dv_hdr(*, ptype: int = 1, comp: int = 0, ndims: int = 6) -> bytes:
        hdr = bytearray(288)
        struct.pack_into("<16sQQ", hdr, 0, b"ZISRAWSUBBLOCK\x00\x00", 512, 320)
        struct.pack_into("<IIQ", hdr, 32, 0, 0, 32)
        struct.pack_into("<2siqiiBB4si", hdr, 48, b"DV", ptype, 0, 0, comp, 0, 0, b"\x00" * 4, ndims)
        return bytes(hdr) + b"\x00" * 64

    # 3. PixelType disagrees with directory
    with pytest.raises(ValueError, match="PixelType"):
        _run_hdr(_make_dv_hdr(ptype=2))

    # 4. Compression disagrees with directory
    with pytest.raises(ValueError, match="Compression"):
        _run_hdr(_make_dv_hdr(comp=5))

    # 5. Out-of-bounds hdr_ndims (< 0 or > 64)
    with pytest.raises(ValueError, match="dimension count"):
        _run_hdr(_make_dv_hdr(ndims=-1))
    with pytest.raises(ValueError, match="dimension count"):
        _run_hdr(_make_dv_hdr(ndims=65))


def test_pf3_czi_open_czi_scene_scoped_t_values(tmp_path: Path):
    s0_t0 = np.full((24, 24), 11, dtype=np.uint16)
    s0_t1 = np.full((24, 24), 22, dtype=np.uint16)
    s1_t5 = np.full((24, 24), 555, dtype=np.uint16)

    czi_path = _write_custom_czi(
        tmp_path / "heterogeneous_scenes_t.czi",
        [
            {"arr": s0_t0, "pixel_type": 1, "s": 0, "t": 0},
            {"arr": s0_t1, "pixel_type": 1, "s": 0, "t": 1},
            {"arr": s1_t5, "pixel_type": 1, "s": 1, "t": 5},
        ],
    )

    with ExitStack() as stack:
        # Scene 1 has only one timepoint (T=5), so selection (p=1, t=0) must map to T=5
        src_s1 = open_czi(stack, czi_path, PlaneSelection(p=1, t=0), tile=16)
        assert np.array_equal(np.asarray(src_s1.data.compute())[0], s1_t5)

    with ExitStack() as stack:
        # Scene 1 does NOT have a second timepoint, even though Scene 0 has 2 and global T has 3
        with pytest.raises(ValueError, match=r"--t 1 out of range \(T=1\)"):
            open_czi(stack, czi_path, PlaneSelection(p=1, t=1), tile=16)


# ---------------------------------------------------------------------------
# PF-4: render.parse_windows rejects non-finite lo/hi/gamma without touching composite
# ---------------------------------------------------------------------------


def test_pf4_parse_windows_ignores_nonfinite_tokens():
    defaults = [(0.0, 1000.0), (10.0, 2000.0), (5.0, 500.0), (0.0, 255.0), (0.0, 4095.0)]
    win_param = "nan:500:1.2,20:inf:1.5,-inf:300:1.0,15:200:nan,25:250:2.0"
    windows, gammas = parse_windows(win_param, defaults)
    assert windows[:4] == defaults[:4]
    assert gammas[:4] == [1.0, 1.0, 1.0, 1.0]
    assert windows[4] == (25.0, 250.0)
    assert gammas[4] == 2.0

    windows2, gammas2 = parse_windows("0:100:inf,0:100:-inf", defaults[:2])
    assert windows2 == defaults[:2]
    assert gammas2 == [1.0, 1.0]

    # MINOR2-F1: finite tokens exceeding float32 precision must also fall back
    windows3, gammas3 = parse_windows("0:1e39:1.0,-1e39:100:1.0", defaults[:2])
    assert windows3 == defaults[:2]
    assert gammas3 == [1.0, 1.0]


# ---------------------------------------------------------------------------
# PF-5: CZI _extract_czi_metadata prefers Dimensions/Channels names before Id
# ---------------------------------------------------------------------------


def test_pf5_extract_czi_metadata_prefers_dim_channel_names_before_id():
    xml = ET.fromstring(
        """<?xml version="1.0" encoding="utf-8"?>
        <ImageDocument><Metadata>
          <Information><Image><Dimensions><Channels>
            <Channel Id="Channel:0" Name="DAPI"/>
            <Channel Id="Channel:1"><Fluor>Alexa Fluor 488</Fluor></Channel>
            <Channel Id="Channel:2"><ShortName>Cy5</ShortName><Color>#FFFF0080</Color></Channel>
            <Channel Id="Channel:3"/>
            <Channel Id="Channel:4" Name="Channel 0"/>
          </Channels></Dimensions></Image></Information>
          <DisplaySetting><Channels>
            <Channel Id="Channel:0"><ShortName>   </ShortName><Color>#FF00A0FF</Color></Channel>
            <Channel Id="Channel:1"><Color>#FF00FF33</Color></Channel>
            <Channel Id="Channel:2"/>
            <Channel Id="Channel:3"><Color>#FFFFFF00</Color></Channel>
            <Channel Id="Channel:4"/>
          </Channels></DisplaySetting>
        </Metadata></ImageDocument>
        """
    )
    _, _, _, channels = _extract_czi_metadata(xml, n_channels=5, rgb=False)
    assert [(ch.name, ch.color) for ch in channels] == [
        ("DAPI", (0, 160, 255)),
        ("Alexa Fluor 488", (0, 255, 51)),
        ("Cy5", (255, 0, 128)),
        ("Channel:3", (255, 255, 0)),
        ("Channel 0", (255, 0, 255)),
    ]


def test_pf6_uint16_svs_bits_per_sample_below_16_scales_default_window(tmp_path: Path):
    """C13: 12-bit RGB SVS stored in uint16 uses 2**12 - 1 (4095) as default window end."""
    rng = np.random.default_rng(612)
    img12 = rng.integers(0, 4096, size=(512, 512, 3), dtype=np.uint16)

    # 1. Direct pyramidal tiled SVS (bitspersample=12)
    pyr12 = tmp_path / "pyr12.svs"
    opts = dict(tile=(256, 256), photometric="rgb", compression="jpeg2000", bitspersample=12)
    with tifffile.TiffWriter(pyr12) as tw:
        tw.write(img12, subifds=1, description="Aperio Image|MPP = 0.25|AppMag = 20", **opts)
        tw.write(img12[::4, ::4], subfiletype=1, **opts)

    root, attrs = open_direct(pyr12)
    try:
        for ch_meta in attrs["omero"]["channels"]:
            assert ch_meta["window"] == {
                "start": 0.0,
                "end": 4095.0,
                "min": 0.0,
                "max": 65535.0,
            }
    finally:
        root.close(delay=0)

    # 2. Converted non-pyramidal SVS (open_svs -> bit_depth=12 -> window end 4095.0)
    flat12 = tmp_path / "flat12.svs"
    with tifffile.TiffWriter(flat12) as tw:
        tw.write(
            img12[:256, :256],
            photometric="rgb",
            compression="jpeg2000",
            bitspersample=12,
            description="Aperio Image|MPP = 0.25|AppMag = 20",
        )
    with ExitStack() as stack:
        src = open_svs(stack, flat12)
        assert src.bit_depth == 12
    store12 = ensure_cache(flat12)
    croot, cattrs = open_store(store12)
    try:
        for ch_meta in cattrs["omero"]["channels"]:
            assert ch_meta["window"]["start"] == 0.0
            assert ch_meta["window"]["end"] == 4095.0
    finally:
        close = getattr(croot, "close", None)
        if close is not None:
            close()


# ---------------------------------------------------------------------------
# PF-6: uint16 direct SVS (_SvsLevel, open_direct, histograms, render, export)
#       and axes-aware collect_svs_info
# ---------------------------------------------------------------------------


def test_pf6_direct_uint16_svs_histograms_render_and_roi_export(tmp_path: Path):
    # 1. _SvsLevel preserves explicit uint16 dtype
    lv = _SvsLevel(
        lambda y0, y1, x0, x1: np.full((y1 - y0, x1 - x0, 3), 12345, dtype=np.uint16),
        64,
        80,
        extra=1,
        dtype=np.dtype(np.uint16),
    )
    assert lv.dtype == np.dtype(np.uint16)
    assert lv[:, :8, :8].dtype == np.dtype(np.uint16)

    # 2. Full uint16 pyramidal tiled RGB SVS through open_direct + render + histogram + ROI TIFF
    rng = np.random.default_rng(616)
    img16 = rng.integers(0, 65535, size=(1024, 1280, 3), dtype=np.uint16)
    svs16 = tmp_path / "uint16_slide.svs"
    opts = dict(tile=(256, 256), photometric="rgb", compression="zlib")
    with tifffile.TiffWriter(svs16) as tw:
        tw.write(
            img16,
            subifds=1,
            description="Aperio Image|MPP = 0.25|AppMag = 40",
            **opts,
        )
        small16 = np.rint(
            img16.reshape(256, 4, 320, 4, 3).mean(axis=(1, 3), dtype=np.float32)
        ).astype(np.uint16)
        tw.write(small16, subfiletype=1, **opts)

    root, attrs = open_direct(svs16)
    try:
        meta = attrs["nd2wsi"]
        assert meta["dtype"] == "uint16"
        assert root["0"].dtype == np.dtype(np.uint16)
        assert root["1"].dtype == np.dtype(np.uint16)
        for ch_meta in attrs["omero"]["channels"]:
            assert ch_meta["window"] == {
                "start": 0.0,
                "end": 65535.0,
                "min": 0.0,
                "max": 65535.0,
            }

        got0 = np.moveaxis(root["0"][:, :128, :128], 0, -1)
        assert got0.dtype == np.dtype(np.uint16)
        assert np.array_equal(got0, img16[:128, :128])

        # Histograms cover 0..65535 cleanly
        hists = compute_histograms(root, attrs, bins=64, min_pixels=10_000)
        assert len(hists) == 3
        assert all(h["vmin"] == 0.0 and h["vmax"] == 65535.0 for h in hists)
        assert all(sum(h["bins"]) > 0 for h in hists)

        # Tile & ROI rendered PNGs decode cleanly and are non-trivial
        tile_png = render_tile(root, attrs, 0, 0, 0, [0, 1, 2], fmt="png")
        tile_arr = np.asarray(Image.open(io.BytesIO(tile_png)))
        assert tile_arr.shape == (512, 512, 3)
        assert tile_arr.std() > 10.0

        roi_png = export_roi_rendered(root, attrs, 0, 32, 48, 120, 96, [0, 1, 2], "png")
        roi_arr = np.asarray(Image.open(io.BytesIO(roi_png)))
        assert roi_arr.shape == (96, 120, 3)

        # Native-resolution ROI TIFF export preserves exact uint16 values (RGB & single-channel)
        out_rgb_tif = tmp_path / "roi_rgb16.tif"
        export_roi_tiff(root, attrs, out_rgb_tif, 0, 40, 60, 300, 200, [0, 1, 2])
        exported_rgb = tifffile.imread(out_rgb_tif)
        assert exported_rgb.dtype == np.dtype(np.uint16)
        assert np.array_equal(exported_rgb, img16[60:260, 40:340])

        out_ch1_tif = tmp_path / "roi_ch1_16.tif"
        export_roi_tiff(root, attrs, out_ch1_tif, 0, 40, 60, 300, 200, [1])
        exported_ch1 = tifffile.imread(out_ch1_tif)
        assert exported_ch1.dtype == np.dtype(np.uint16)
        assert np.array_equal(exported_ch1, img16[60:260, 40:340, 1])
    finally:
        root.close(delay=0)

    # 3. Unsupported float32 pyramidal SVS raises NotImplementedError
    f32_svs = tmp_path / "float32_slide.svs"
    img_f32 = rng.uniform(0.0, 1.0, size=(1024, 1024, 3)).astype(np.float32)
    with tifffile.TiffWriter(f32_svs) as tw:
        tw.write(img_f32, subifds=1, tile=(256, 256), photometric="rgb", compression="zlib")
        tw.write(
            img_f32[::4, ::4],
            subfiletype=1,
            tile=(256, 256),
            photometric="rgb",
            compression="zlib",
        )
    with pytest.raises(NotImplementedError, match="unsupported direct SVS dtype"):
        open_direct(f32_svs)


def test_pf6_collect_svs_info_axes_aware_and_missing_axes_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # 2D grayscale TIFF (axes == "YX")
    gray_tif = tmp_path / "gray2d.svs"
    tifffile.imwrite(gray_tif, np.zeros((180, 240), dtype=np.uint16), tile=(64, 64))
    info_gray = collect_svs_info(gray_tif)
    assert info_gray["sizes"] == {"Y": 180, "X": 240}
    assert info_gray["levels"] == [[180, 240]]

    # Planar 3-channel TIFF (axes == "SYX", shape == (3, 180, 240))
    planar_tif = tmp_path / "planar3d.svs"
    tifffile.imwrite(
        planar_tif,
        np.zeros((3, 180, 240), dtype=np.uint8),
        photometric="rgb",
        planarconfig="separate",
        tile=(64, 64),
    )
    info_planar = collect_svs_info(planar_tif)
    assert info_planar["sizes"] == {"S": 3, "Y": 180, "X": 240}
    assert info_planar["levels"] == [[180, 240]]

    # Fallback when series.axes is missing/empty for 2D and 3D series
    for shape, expected_sizes in (
        ((120, 160), {"Y": 120, "X": 160, "S": 1}),
        ((120, 160, 3), {"Y": 120, "X": 160, "S": 3}),
    ):
        fake_page = SimpleNamespace(
            description="",
            is_tiled=True,
            tilelength=64,
            tilewidth=64,
            compression=SimpleNamespace(name="ZLIB"),
        )
        fake_level = SimpleNamespace(shape=shape, axes="")
        fake_series = SimpleNamespace(
            shape=shape,
            axes="",
            dtype=np.dtype(np.uint8),
            levels=[fake_level],
        )

        class _FakeTiffFile:
            def __init__(self, _p: str):
                self.series = [fake_series]
                self.pages = [fake_page]

            def __enter__(self):
                return self

            def __exit__(self, *args: Any):
                return False

        monkeypatch.setattr(tifffile, "TiffFile", _FakeTiffFile)
        assert collect_svs_info(gray_tif)["sizes"] == expected_sizes


# ---------------------------------------------------------------------------
# PF-7: reader.py calibration fallbacks, open_plane/plane_view bounds,
#       objective_magnification finite>0, and _nd2_raw_planes numeric sorting
# ---------------------------------------------------------------------------


def test_pf7a_nd2_pixel_size_global_volume_flags_and_empty_channels():
    def _mock_nd2(
        *,
        channels: Any,
        global_vol: dict[str, Any] | None = None,
        voxel_xy: tuple[float, float] = (1.0, 1.0),
        text_info: dict[str, str] | None = None,
    ) -> Any:
        rdr = SimpleNamespace(
            _cached_global_metadata=lambda: {"volume": global_vol} if global_vol is not None else {}
        )
        return SimpleNamespace(
            metadata=SimpleNamespace(channels=channels),
            _rdr=rdr,
            voxel_size=lambda: SimpleNamespace(x=voxel_xy[0], y=voxel_xy[1], z=1.0),
            text_info=text_info or {},
        )

    # 1. Channel lacks axesCalibrated, global volume says uncalibrated -> (None, "unknown")
    ch_no_flags = [SimpleNamespace(volume=SimpleNamespace(axesCalibrated=None))]
    f1 = _mock_nd2(
        channels=ch_no_flags,
        global_vol={"axesCalibrated": [False, False, False], "axesCalibration": [1.0, 1.0, 1.0]},
        voxel_xy=(1.0, 1.0),
    )
    assert _nd2_pixel_size(f1) == (None, "unknown")

    # 2. Channel lacks axesCalibrated, global volume says calibrated -> uses voxel_size
    f2 = _mock_nd2(
        channels=ch_no_flags,
        global_vol={"axesCalibrated": [True, True, False]},
        voxel_xy=(0.325, 0.325),
    )
    assert _nd2_pixel_size(f2) == ((0.325, 0.325), "nd2-voxel-size")

    # 3. Empty channels=[] and flags is None -> must NOT trust voxel_size() 1.0 default
    f3 = _mock_nd2(channels=[], global_vol=None, voxel_xy=(1.0, 1.0))
    assert _nd2_pixel_size(f3) == (None, "unknown")

    # 4. Empty channels=[] and flags is None with text_info calibration -> still (None, "unknown") (A6a)
    f4 = _mock_nd2(
        channels=[],
        global_vol=None,
        voxel_xy=(1.0, 1.0),
        text_info={"optics": "Plan Apo 20x\nCalibration (µm/px): 0.345"},
    )
    assert _nd2_pixel_size(f4) == (None, "unknown")

    # 5. Empty channels=[], global volume carries calibrated axesCalibration -> uses volume calibration
    f5 = _mock_nd2(
        channels=[],
        global_vol={"axesCalibrated": [True, True, False], "axesCalibration": [0.22, 0.25, 1.0]},
        voxel_xy=(1.0, 1.0),
    )
    assert _nd2_pixel_size(f5) == ((0.25, 0.22), "nd2-volume-calibration")


def test_pf7b_nd2_open_plane_plane_view_and_backable_bounds(tmp_path: Path):
    # Mock single-plane ND2 (no T or P in sizes -> n_t=1, n_p=1) and multi-T/P ND2
    frame = np.zeros((16, 20), dtype=np.uint16)

    def _mock_open_nd2(sizes: dict[str, int]) -> Any:
        return SimpleNamespace(
            sizes=sizes,
            dtype=np.dtype(np.uint16),
            attributes=SimpleNamespace(compressionType="none"),
            metadata=SimpleNamespace(channels=[]),
            _rdr=None,
            text_info={},
            read_frame=lambda _seq: frame,
            is_legacy=False,
        )

    f_single = _mock_open_nd2({"Y": 16, "X": 20})
    for bad_sel in (
        PlaneSelection(t=-1),
        PlaneSelection(p=-1),
        PlaneSelection(t=1),
        PlaneSelection(p=1),
    ):
        with pytest.raises(ValueError, match="out of range"):
            open_plane(f_single, bad_sel)
        with pytest.raises(ValueError, match="out of range"):
            plane_view(f_single, bad_sel)
        ok, reason = is_source_backable(f_single, bad_sel)
        assert ok is False
        assert "out of range" in reason

    f_multi = _mock_open_nd2({"T": 2, "P": 3, "Y": 16, "X": 20})
    assert plane_view(f_multi, PlaneSelection(t=1, p=2))[0].shape == (1, 16, 20)
    for bad_multi in (
        PlaneSelection(t=-1, p=0),
        PlaneSelection(t=2, p=0),
        PlaneSelection(t=0, p=-1),
        PlaneSelection(t=0, p=3),
    ):
        with pytest.raises(ValueError, match="out of range"):
            open_plane(f_multi, bad_multi)
        with pytest.raises(ValueError, match="out of range"):
            plane_view(f_multi, bad_multi)


def test_pf7c_objective_magnification_rejects_nonpositive_and_nonfinite():
    def _ch(mag: Any) -> Any:
        return SimpleNamespace(microscope=SimpleNamespace(objectiveMagnification=mag))

    f_bad = SimpleNamespace(
        metadata=SimpleNamespace(
            channels=[_ch(-1.0), _ch(0.0), _ch(float("nan")), _ch(float("inf")), _ch(float("-inf"))]
        )
    )
    assert objective_magnification(f_bad) is None

    f_mixed = SimpleNamespace(
        metadata=SimpleNamespace(channels=[_ch(-1.0), _ch(float("nan")), _ch(40.0)])
    )
    assert objective_magnification(f_mixed) == 40.0


def test_pf7d_nd2_raw_planes_numeric_key_order(tmp_path: Path):
    # Insert a0..a11 (nd2 0.11+ style) in scrambled/lexicographic-trap order plus a non-numbered key
    order = ["a0", "a10", "a11", "a1", "a2", "aux", "a9", "a3", "a4", "a5", "a6", "a7", "a8"]
    plane_table = {
        k: {
            "sDescription": f"Ch_{k}",
            "uiColor": 0x0000FF00,
        }
        for k in order
    }
    raw_meta = {"sPicturePlanes": {"sPlaneNew": plane_table}}

    class _BrokenMeta:
        @property
        def channels(self):
            raise RuntimeError("structured metadata broken")

    f = SimpleNamespace(
        metadata=_BrokenMeta(),
        _rdr=SimpleNamespace(_cached_raw_metadata=lambda: raw_meta),
    )

    planes = _nd2_raw_planes(f)
    assert [name for name, _ in planes] == [f"Ch_a{i}" for i in range(12)] + ["Ch_aux"]

    ch_infos = _channel_infos(f, n_channels=12, rgb=False)
    assert [c.name for c in ch_infos] == [f"Ch_a{i}" for i in range(12)]

    # End-to-end 12-plane limnd2-written ND2 whose structured metadata fails:
    # verify fallback channel names still align with each channel's pixel values.
    limnd2 = pytest.importorskip("limnd2")
    nd2 = pytest.importorskip("nd2")
    n_planes, h_nd2, w_nd2 = 12, 32, 48
    img = np.stack(
        [np.full((h_nd2, w_nd2), 100 * (i + 1), dtype=np.uint16) for i in range(n_planes)],
        axis=-1,
    )
    real_nd2 = tmp_path / "twelve_channels.nd2"
    with limnd2.Nd2Writer(str(real_nd2)) as w:
        w.imageAttributes = limnd2.ImageAttributes.create(
            width=w_nd2, height=h_nd2, component_count=n_planes, bits=16, sequence_count=1
        )
        w.setImage(0, img)
        mf = limnd2.MetadataFactory(pixel_calibration=0.5)
        for i in range(n_planes):
            mf.addPlane(name=f"ch{i:02d}", color="#00FF00")
        w.pictureMetadata = mf.createMetadata()

    with nd2.ND2File(str(real_nd2)) as f_real:
        raw = f_real._rdr._cached_raw_metadata()
        ss = raw["sPicturePlanes"].setdefault("sSampleSetting", {})
        for i in range(n_planes):
            ss.setdefault(f"a{i}", {})["matCameraToStage"] = {
                "Columns": 2,
                "Rows": 2,
                "Data": "not-bytes",
            }
        with pytest.raises(TypeError):
            _ = f_real.metadata
        src_real = open_plane(f_real, PlaneSelection())
        assert [c.name for c in src_real.channels] == [f"ch{i:02d}" for i in range(n_planes)]
        for i in range(n_planes):
            assert int(np.asarray(src_real.data[i, 0, 0].compute())) == 100 * (i + 1)
