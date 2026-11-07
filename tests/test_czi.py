"""Tests for Zeiss CZI (.czi) plane scan and confocal Z-stack support."""

from __future__ import annotations

import io
import json
import struct
import threading
import urllib.request
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
import tifffile
from PIL import Image

from nd2wsi import app
from nd2wsi.annotation_sidecars import SLIDE_SUFFIXES
from nd2wsi.convert import convert, ensure_cache, estimate_store_bytes, open_store
from nd2wsi.czi import collect_czi_info, format_czi_info, is_czi
from nd2wsi.reader import PlaneSelection
from nd2wsi.server import create_server, server_url


def build_synthetic_czi(
    path: Path,
    planes: dict[tuple[int, int, int], np.ndarray],
    *,
    pixel_type: int = 1,
    compression: int = 0,
    x_offset: int = 0,
    y_offset: int = 0,
    tile_offsets: dict[int, tuple[int, int]] | None = None,
    pixel_size_um: tuple[float, float] = (0.25, 0.25),
    magnification: float = 40.0,
    channels: list[tuple[str, str]] | None = None,
) -> Path:
    """Write a synthetic Zeiss CZI (ZISRAW) file from {(z, c, m): array}."""
    # Each item: (fpos, x_start, x_size, y_start, y_size, z, c, m)
    dir_records: list[tuple[int, int, int, int, int, int, int, int]] = []

    buf = bytearray(512)  # Reserve 512 bytes for ZISRAWFILE header segment
    for (z, c, m), arr in planes.items():
        if arr.ndim == 2:
            h, w = arr.shape
            raw_arr = arr
        else:
            h, w, _ = arr.shape
            # Store as BGR order for uncompressed/zstd, RGB for jpegxr
            raw_arr = arr[:, :, ::-1] if compression != 4 else arr
        raw_bytes = np.ascontiguousarray(raw_arr).tobytes()
        if compression == 0:
            payload = raw_bytes
        elif compression == 5:
            payload = imagecodecs.zstd_encode(raw_bytes)
        elif compression == 6:
            u8 = np.frombuffer(raw_bytes, dtype=np.uint8)
            lo = u8[0::2]
            hi = u8[1::2]
            packed = np.concatenate([lo, hi]).tobytes()
            payload = bytes([3, 1, 1]) + imagecodecs.zstd_encode(packed)
        elif compression == 4:
            payload = imagecodecs.jpegxr_encode(np.ascontiguousarray(arr))
        elif compression == 121:
            # 3-phase ApoTomeRaw payload
            p0 = raw_arr
            p1 = (raw_arr + 300).astype(raw_arr.dtype)
            p2 = (raw_arr + 600).astype(raw_arr.dtype)
            payload = np.stack([p0, p1, p2], axis=0).tobytes()
        else:
            raise ValueError(f"Unsupported test compression {compression}")

        mx, my = (tile_offsets or {}).get(m, (0, 0))
        xs = x_offset + mx
        ys = y_offset + my

        fpos = len(buf)
        ndims = 5  # X, Y, C, Z, M
        hdr_len = max(256, 16 + 32 + 20 * ndims)
        used_size = hdr_len + len(payload)
        alloc_size = (used_size + 31) // 32 * 32

        seg_hdr = struct.pack("<16sQQ", b"ZISRAWSUBBLOCK\x00\x00", alloc_size, used_size)
        sb_prefix = bytearray(hdr_len)
        struct.pack_into("<IIQ", sb_prefix, 0, 0, 0, len(payload))
        struct.pack_into(
            "<2siqiiBB4si",
            sb_prefix,
            16,
            b"DV",
            pixel_type,
            fpos,
            0,
            compression,
            0,
            0,
            b"\x00" * 4,
            ndims,
        )
        dims = [
            (b"X\x00\x00\x00", xs, w, float(xs), w),
            (b"Y\x00\x00\x00", ys, h, float(ys), h),
            (b"C\x00\x00\x00", c, 1, 0.0, 1),
            (b"Z\x00\x00\x00", z, 1, 0.0, 1),
            (b"M\x00\x00\x00", m, 1, 0.0, 1),
        ]
        for idx, (dname, st_i, sz_i, st_f, st_sz) in enumerate(dims):
            struct.pack_into("<4siifi", sb_prefix, 48 + idx * 20, dname, st_i, sz_i, st_f, st_sz)

        buf.extend(seg_hdr)
        buf.extend(sb_prefix)
        buf.extend(payload)
        if alloc_size > used_size:
            buf.extend(b"\x00" * (alloc_size - used_size))
        dir_records.append((fpos, xs, w, ys, h, z, c, m))

    # Write ZISRAWDIRECTORY segment
    dir_pos = len(buf)
    dir_payload = bytearray(128)
    struct.pack_into("<i", dir_payload, 0, len(dir_records))
    for fpos, xs, w, ys, h, z, c, m in dir_records:
        ndims = 5
        dir_payload.extend(
            struct.pack(
                "<2siqiiBB4si",
                b"DV",
                pixel_type,
                fpos,
                0,
                compression,
                0,
                0,
                b"\x00" * 4,
                ndims,
            )
        )
        for dname, st_i, sz_i in (
            (b"X\x00\x00\x00", xs, w),
            (b"Y\x00\x00\x00", ys, h),
            (b"C\x00\x00\x00", c, 1),
            (b"Z\x00\x00\x00", z, 1),
            (b"M\x00\x00\x00", m, 1),
        ):
            dir_payload.extend(struct.pack("<4siifi", dname, st_i, sz_i, float(st_i), sz_i))
    dir_used = len(dir_payload)
    dir_alloc = (dir_used + 31) // 32 * 32
    buf.extend(struct.pack("<16sQQ", b"ZISRAWDIRECTORY\x00", dir_alloc, dir_used))
    buf.extend(dir_payload)
    if dir_alloc > dir_used:
        buf.extend(b"\x00" * (dir_alloc - dir_used))

    # Write ZISRAWMETADATA segment
    meta_pos = len(buf)
    ch_nodes = ""
    for idx, (cname, ccolor) in enumerate(channels or [("DAPI", "#FF00A0FF"), ("EGFP", "#FF00FF33")]):
        ch_nodes += (
            f'<Channel Id="Channel:{idx}" Name="{cname}">'
            f"<ShortName>{cname}</ShortName><Color>{ccolor}</Color></Channel>"
        )
    py_m = pixel_size_um[0] * 1e-6
    px_m = pixel_size_um[1] * 1e-6
    xml_str = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<ImageDocument><Metadata>"
        "<Information><Instrument><Objectives>"
        f'<Objective Id="Obj:1"><NominalMagnification>{magnification}</NominalMagnification></Objective>'
        "</Objectives></Instrument></Information>"
        "<Scaling><Items>"
        f'<Distance Id="X"><Value>{px_m}</Value></Distance>'
        f'<Distance Id="Y"><Value>{py_m}</Value></Distance>'
        "</Items></Scaling>"
        f"<DisplaySetting><Channels>{ch_nodes}</Channels></DisplaySetting>"
        "</Metadata></ImageDocument>"
    )
    xml_bytes = xml_str.encode("utf-8")
    meta_hdr_payload = bytearray(256)
    struct.pack_into("<II", meta_hdr_payload, 0, len(xml_bytes), 0)
    meta_used = 256 + len(xml_bytes)
    meta_alloc = (meta_used + 31) // 32 * 32
    buf.extend(struct.pack("<16sQQ", b"ZISRAWMETADATA\x00\x00", meta_alloc, meta_used))
    buf.extend(meta_hdr_payload)
    buf.extend(xml_bytes)
    if meta_alloc > meta_used:
        buf.extend(b"\x00" * (meta_alloc - meta_used))

    # Fill ZISRAWFILE header at offset 0
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


def test_czi_suffixes_and_open_types():
    assert ".czi" in SLIDE_SUFFIXES
    assert ".czi" in app.OPEN_SUFFIXES
    assert "*.czi" in app.OPEN_FILE_TYPES[0]
    assert is_czi("brain_cortex.czi")
    assert is_czi("BRAIN.CZI")


def test_czi_multichannel_confocal_zstack_and_mosaic_pyramid(tmp_path: Path):
    rng = np.random.default_rng(42)
    # 2 Z-planes, 2 channels, 2 mosaic tiles (each 96x128, stitched into 192x128)
    planes: dict[tuple[int, int, int], np.ndarray] = {}
    for z in range(2):
        for c in range(2):
            for m in range(2):
                base = (z + 1) * 1000 + (c + 1) * 300 + m * 50
                planes[(z, c, m)] = rng.integers(
                    base, base + 200, size=(96, 128), dtype=np.uint16
                )

    czi_path = build_synthetic_czi(
        tmp_path / "confocal_mosaic.czi",
        planes,
        pixel_type=1,
        compression=0,
        x_offset=50000,
        y_offset=-200,
        tile_offsets={0: (0, 0), 1: (0, 96)},
        pixel_size_um=(0.15, 0.15),
        magnification=63.0,
        channels=[("ATTO 590", "#FFFF6600"), ("Alexa Fluor 488", "#FF00FF33")],
    )

    info = collect_czi_info(czi_path)
    assert info["width"] == 128
    assert info["height"] == 192
    assert info["channels"] == 2
    assert info["z_planes"] == 2
    assert info["mosaic_tiles"] == 2
    assert info["magnification"] == 63.0
    assert np.allclose(info["pixel_size_um"], [0.15, 0.15])
    assert "ATTO 590" in format_czi_info(info)

    est = estimate_store_bytes(czi_path, tile=64)
    assert est["width"] == 128 and est["height"] == 192 and est["channels"] == 2

    # Build default Z=mid (z=1) pyramid cache
    store_mid = ensure_cache(czi_path, tile=64)
    root, attrs = open_store(store_mid)
    try:
        expected_c0 = np.vstack([planes[(1, 0, 0)], planes[(1, 0, 1)]])
        expected_c1 = np.vstack([planes[(1, 1, 0)], planes[(1, 1, 1)]])
        assert np.array_equal(np.asarray(root["0"][0]), expected_c0)
        assert np.array_equal(np.asarray(root["0"][1]), expected_c1)
        assert attrs["nd2wsi"]["objective_magnification"] == 63.0
        assert attrs["nd2wsi"]["calibration"]["source"] == "czi-scaling"
    finally:
        if hasattr(root, "close"):
            root.close()

    # Build Z=max MIP pyramid cache
    store_max = ensure_cache(czi_path, selection=PlaneSelection(z="max"), tile=64)
    root_max, attrs_max = open_store(store_max)
    try:
        expected_max_c0 = np.maximum(
            np.vstack([planes[(0, 0, 0)], planes[(0, 0, 1)]]),
            np.vstack([planes[(1, 0, 0)], planes[(1, 0, 1)]]),
        )
        assert np.array_equal(np.asarray(root_max["0"][0]), expected_max_c0)
        assert attrs_max["nd2wsi"]["selection"]["z"] == "max"
    finally:
        root_max.close()


@pytest.mark.parametrize("compression", [5, 6, 4])
def test_czi_compressed_rgb_codecs(tmp_path: Path, compression: int):
    rng = np.random.default_rng(7)
    rgb = rng.integers(1000, 50000, size=(64, 80, 3), dtype=np.uint16)
    czi_path = build_synthetic_czi(
        tmp_path / f"rgb_comp_{compression}.czi",
        {(0, 0, 0): rgb},
        pixel_type=4,  # Bgr48
        compression=compression,
    )
    out = convert(czi_path, tmp_path / f"out_{compression}.ome.zarr", tile=64, progress=False)
    root, attrs = open_store(out)
    try:
        assert attrs["nd2wsi"]["rgb"] is True
        actual = np.moveaxis(np.asarray(root["0"][:]), 0, -1)
        if compression in (5, 6):
            assert np.array_equal(actual, rgb)
        else:
            assert actual.shape == rgb.shape
    finally:
        if hasattr(root, "close"):
            root.close()


def test_czi_server_view_lut_and_annotations_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ND2WSI_LUT_SETTINGS_ROOT", str(tmp_path / "lut-settings"))
    rng = np.random.default_rng(99)
    p0 = rng.integers(0, 4000, size=(128, 128), dtype=np.uint16)
    p1 = rng.integers(0, 4000, size=(128, 128), dtype=np.uint16)
    czi_path = build_synthetic_czi(
        tmp_path / "plane_scan.czi",
        {(0, 0, 0): p0, (0, 1, 0): p1},
        pixel_type=1,
        compression=0,
        channels=[("DAPI", "#FF00A0FF"), ("GFP", "#FF00FF00")],
    )

    httpd = create_server([])
    base = server_url(httpd)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            base + "api/open",
            data=json.dumps({"path": str(czi_path)}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            opened = json.loads(resp.read())
        sid = opened["sid"]

        with urllib.request.urlopen(f"{base}s/{sid}/api/info") as resp:
            info = json.loads(resp.read())
        assert info["name"] == "plane_scan.czi"
        assert [c["label"] for c in info["channels"]] == ["DAPI", "GFP"]

        with urllib.request.urlopen(f"{base}s/{sid}/api/tile/0/0/0.jpg?c=0,1&win=0:2000:1.2,0:2000:1.0") as resp:
            tile = resp.read()
        with Image.open(io.BytesIO(tile)) as im:
            assert im.size == (128, 128)

        # Save and read LUT settings
        lut_req = urllib.request.Request(
            f"{base}s/{sid}/api/lut-settings",
            data=json.dumps({
                "format": "nd2wsi-lut-settings/1",
                "luts": [
                    {"lo": 10.0, "hi": 1800.0, "gamma": 1.1},
                    {"lo": 20.0, "hi": 2200.0, "gamma": 0.9},
                ],
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(lut_req) as resp:
            assert json.loads(resp.read())["ok"] is True

        # Save and read annotations
        ann_req = urllib.request.Request(
            f"{base}s/{sid}/api/annotations",
            data=json.dumps({
                "items": [{"id": "a1", "type": "rect", "x": 10, "y": 12, "w": 40, "h": 30, "label": "Dentate"}]
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(ann_req) as resp:
            assert json.loads(resp.read())["ok"] is True

        with urllib.request.urlopen(f"{base}s/{sid}/api/annotations") as resp:
            anns = json.loads(resp.read())
        assert anns["items"][0]["label"] == "Dentate"

        # Export raw TIFF ROI and verify pixel exactness
        with urllib.request.urlopen(f"{base}s/{sid}/api/roi?level=0&x=10&y=12&w=32&h=24&format=tiff") as resp:
            tiff_raw = tifffile.imread(io.BytesIO(resp.read()))
        assert np.array_equal(tiff_raw[0], p0[12:36, 10:42])
        assert np.array_equal(tiff_raw[1], p1[12:36, 10:42])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_czi_apotome_raw_demodulation(tmp_path: Path):
    base_plane = np.full((64, 64), 1000, dtype=np.uint16)
    czi_path = build_synthetic_czi(
        tmp_path / "apotome.czi",
        {(0, 0, 0): base_plane},
        pixel_type=1,
        compression=121,
    )
    info = collect_czi_info(czi_path)
    assert info["compression"] == "ApoTomeRaw"
    store = ensure_cache(czi_path, tile=64)
    root, _attrs = open_store(store)
    try:
        arr = np.asarray(root["0"][0])
        # For I0=1000, I1=1300, I2=1600: sqrt(2)/3 * sqrt(300^2 + 300^2 + 600^2) = 346
        assert np.all(arr == 346)
    finally:
        if hasattr(root, "close"):
            root.close()


def test_czi_zstack_plate_mode_viewer_and_roi_export(tmp_path: Path):
    from nd2wsi.plate import is_plate_file

    rng = np.random.default_rng(77)
    z0 = rng.integers(100, 1000, size=(128, 128), dtype=np.uint16)
    z1 = rng.integers(1100, 2000, size=(128, 128), dtype=np.uint16)
    z2 = rng.integers(2100, 3000, size=(128, 128), dtype=np.uint16)
    czi_path = build_synthetic_czi(
        tmp_path / "confocal_zstack.czi",
        {(0, 0, 0): z0, (1, 0, 0): z1, (2, 0, 0): z2},
        pixel_type=1,
        compression=5,
        channels=[("GFP", "#FF00FF00")],
    )
    assert is_plate_file(czi_path) is True

    httpd = create_server([])
    base = server_url(httpd)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            base + "api/open",
            data=json.dumps({"path": str(czi_path)}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            opened = json.loads(resp.read())
        sid = opened["sid"]

        with urllib.request.urlopen(f"{base}s/{sid}/api/info") as resp:
            info = json.loads(resp.read())
        assert info["kind"] == "plate"
        assert info["plate"]["Z"] == 3
        assert info["plate"]["P"] == 1
        assert info["plate"]["zHome"] == 1

        # Fetch reduced frame for Z-slider preview
        with urllib.request.urlopen(f"{base}s/{sid}/api/plate/frame/0/0/2.jpg?k=8") as resp:
            thumb = Image.open(io.BytesIO(resp.read()))
            assert thumb.size == (16, 16)

        # Export ROI from z=2 plane and verify exact pixel match
        with urllib.request.urlopen(
            f"{base}s/{sid}/api/roi?level=0&x=4&y=8&w=32&h=24&format=tiff&t=0&p=0&z=2"
        ) as resp:
            tif_z2 = tifffile.imread(io.BytesIO(resp.read()))
        assert np.array_equal(tif_z2.squeeze(), z2[8:32, 4:36])
    finally:
        httpd.shutdown()
        httpd.server_close()
