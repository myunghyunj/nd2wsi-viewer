"""Tests for Leica LIF (.lif) fluorescence, confocal Z-stack, and LambdaScan support."""

from __future__ import annotations

import io
import json
import struct
import threading
import urllib.request
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image

from nd2wsi import app
from nd2wsi.annotation_sidecars import SLIDE_SUFFIXES
from nd2wsi.cli import main as cli_main
from nd2wsi.convert import convert, ensure_cache, estimate_store_bytes, open_store
from nd2wsi.lif import collect_lif_info, format_lif_info, is_lif
from nd2wsi.reader import PlaneSelection
from nd2wsi.server import create_server, server_url


def build_synthetic_lif(
    path: Path,
    series_list: list[dict],
) -> Path:
    """Write a synthetic Leica LIF (Version 2) container from a list of series dicts."""
    elem_xmls: list[str] = []
    mem_Payloads: list[tuple[str, bytes]] = []

    for idx, spec in enumerate(series_list):
        name = spec.get("name", f"Series{idx:03d}")
        mem_id = f"MemBlock_{idx + 1}"
        payload: bytes = spec["payload"]
        dims_xml = "".join(spec["dims_xml"])
        chans_xml = "".join(spec["chans_xml"])
        extra_xml = spec.get("extra_xml", "")
        elem_xmls.append(
            f'<Element Name="{name}">'
            f"<Data><Image>{extra_xml}"
            f"<ImageDescription>"
            f"<Channels>{chans_xml}</Channels>"
            f"<Dimensions>{dims_xml}</Dimensions>"
            f"</ImageDescription>"
            f"</Image></Data>"
            f'<Memory Size="{len(payload)}" MemoryBlockID="{mem_id}"/>'
            f"</Element>"
        )
        mem_Payloads.append((mem_id, payload))

    xml_str = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<LMSDataContainerHeader Version="2">'
        f"{''.join(elem_xmls)}"
        "</LMSDataContainerHeader>"
    )
    xml_utf16 = xml_str.encode("utf-16-le")
    xml_nc = len(xml_utf16) // 2

    buf = bytearray()
    buf.extend(struct.pack("<iIBI", 0x70, 5 + len(xml_utf16), 0x2A, xml_nc))
    buf.extend(xml_utf16)

    for mem_id, payload in mem_Payloads:
        id_utf16 = mem_id.encode("utf-16-le")
        id_nc = len(id_utf16) // 2
        chunk_len = 1 + 8 + 1 + 4 + len(id_utf16)
        buf.extend(struct.pack("<iIBQBI", 0x70, chunk_len, 0x2A, len(payload), 0x2A, id_nc))
        buf.extend(id_utf16)
        buf.extend(payload)

    path.write_bytes(bytes(buf))
    return path


def test_lif_suffixes_and_open_types():
    assert ".lif" in SLIDE_SUFFIXES
    assert ".lif" in app.OPEN_SUFFIXES
    assert "*.lif" in app.OPEN_FILE_TYPES[0]
    assert is_lif("harvard_astrocyte.lif")
    assert is_lif("CONFOCAL.LIF")


def test_lif_multichannel_confocal_zstack_xycz_and_xyzc_pyramid(tmp_path: Path):
    rng = np.random.default_rng(123)
    h, w, n_z, n_c = 96, 128, 3, 2

    # Series 0: 8-bit XYCZ memory order (like raw confocal Z-stack)
    s0_arr = rng.integers(10, 240, size=(n_z, n_c, h, w), dtype=np.uint8)
    s0_dims = [
        f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="1" Length="{w * 0.18e-6:.9e}" Unit="m"/>',
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w}" Length="{h * 0.18e-6:.9e}" Unit="m"/>',
        f'<DimensionDescription DimID="3" NumberOfElements="{n_z}" BytesInc="{h * w * n_c}" Length="1.0e-5" Unit="m"/>',
    ]
    s0_chans = [
        '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
        f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Red" BytesInc="{h * w}"/>',
    ]
    s0_extra = (
        '<Attachment Name="HardwareSetting">'
        '<ATLConfocalSettingDefinition Magnification="63" ObjectiveName="HC PL APO CS2 63x/1.30 GLYC">'
        '<MultiBand Channel="1" DyeName="Leica/EGFP"/>'
        '<MultiBand Channel="2" DyeName="Leica/TRITC"/>'
        '<Detector Channel="1" IsActive="1"/>'
        '<Detector Channel="2" IsActive="1"/>'
        "</ATLConfocalSettingDefinition></Attachment>"
    )

    # Series 1: 16-bit XYZC memory order (like Leica LIGHTNING deconvolved Z-stack)
    s1_arr = rng.integers(200, 12000, size=(n_c, n_z, h, w), dtype=np.uint16)
    s1_dims = [
        f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="{w * 0.05e-6:.9e}" Unit="m"/>',
        f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="{h * 0.05e-6:.9e}" Unit="m"/>',
        f'<DimensionDescription DimID="3" NumberOfElements="{n_z}" BytesInc="{h * w * 2}" Length="5.0e-6" Unit="m"/>',
    ]
    s1_chans = [
        '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Cyan" BytesInc="0"/>',
        f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Magenta" BytesInc="{n_z * h * w * 2}"/>',
    ]

    lif_path = build_synthetic_lif(
        tmp_path / "astrocyte_confocal.lif",
        [
            {
                "name": "ONH_Astrocyte_63x",
                "payload": s0_arr.tobytes(),
                "dims_xml": s0_dims,
                "chans_xml": s0_chans,
                "extra_xml": s0_extra,
            },
            {
                "name": "ONH_Astrocyte_63x_Lng",
                "payload": s1_arr.tobytes(),
                "dims_xml": s1_dims,
                "chans_xml": s1_chans,
            },
        ],
    )

    info = collect_lif_info(lif_path)
    assert info["width"] == w
    assert info["height"] == h
    assert info["channels"] == 2
    assert info["z_planes"] == 3
    assert info["series_count"] == 2
    assert info["magnification"] == 63.0
    assert np.allclose(info["pixel_size_um"], [0.18, 0.18], atol=1e-5)
    assert [c["name"] for c in info["channel_info"]] == ["EGFP", "TRITC"]
    assert "EGFP" in format_lif_info(info)

    est = estimate_store_bytes(lif_path, tile=64)
    assert est["width"] == w and est["height"] == h and est["channels"] == 2

    # Cache default Series 0 (z=mid -> z=1)
    store_mid = ensure_cache(lif_path, tile=64)
    root_mid, attrs_mid = open_store(store_mid)
    try:
        lvl0_mid = np.asarray(root_mid["0"][:])
        assert np.array_equal(lvl0_mid, s0_arr[1])
        assert attrs_mid["nd2wsi"]["objective_magnification"] == 63.0
        assert attrs_mid["nd2wsi"]["calibration"]["source"] == "lif-dimension"
    finally:
        if hasattr(root_mid, "close"):
            root_mid.close()

    # Cache Series 0 with Z maximum-intensity projection
    store_max = ensure_cache(lif_path, tile=64, selection=PlaneSelection(z="max"))
    root_max, attrs_max = open_store(store_max)
    try:
        lvl0_max = np.asarray(root_max["0"][:])
        assert np.array_equal(lvl0_max, s0_arr.max(axis=0))
        assert attrs_max["nd2wsi"]["selection"]["z"] == "max"
    finally:
        if hasattr(root_max, "close"):
            root_max.close()

    # Convert Series 1 (16-bit XYZC LIGHTNING deconvolved)
    out_s1 = tmp_path / "series1_lng.nd2svs"
    convert(lif_path, out_s1, tile=64, selection=PlaneSelection(p=1, z=2), progress=False)
    root_s1, _attrs_s1 = open_store(out_s1)
    try:
        arr_s1 = np.asarray(root_s1["0"][:])
        assert arr_s1.dtype == np.dtype("uint16")
        assert np.array_equal(arr_s1, s1_arr[:, 2])
    finally:
        if hasattr(root_s1, "close"):
            root_s1.close()


def test_lif_lambdascan_and_rgb_snapshot(tmp_path: Path):
    rng = np.random.default_rng(77)
    h, w, n_lambda = 64, 80, 5
    lambda_arr = rng.integers(50, 4000, size=(n_lambda, h, w), dtype=np.uint16)

    # RGB snapshot stored as BGR interleaved (ChannelTag 3=B at inc=0, 2=G at inc=1, 1=R at inc=2)
    rgb_arr = rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    bgr_bytes = np.ascontiguousarray(rgb_arr[:, :, ::-1]).tobytes()

    lif_path = build_synthetic_lif(
        tmp_path / "lambdascan_and_rgb.lif",
        [
            {
                "name": "Convallaria_Lambda",
                "payload": lambda_arr.tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="{w * 1.5e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="{h * 1.5e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="5" NumberOfElements="{n_lambda}" BytesInc="{h * w * 2}" Length="2.0e-7" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Gray" BytesInc="0"/>',
                ],
            },
            {
                "name": "Snapshot1",
                "payload": bgr_bytes,
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="3" Length="{w}Pixel" Unit="Pixel"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 3}" Length="{h}Pixel" Unit="Pixel"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="3" Resolution="8" LUTName="" BytesInc="0"/>',
                    '<ChannelDescription DataType="0" ChannelTag="2" Resolution="8" LUTName="" BytesInc="1"/>',
                    '<ChannelDescription DataType="0" ChannelTag="1" Resolution="8" LUTName="" BytesInc="2"/>',
                ],
            },
        ],
    )

    info = collect_lif_info(lif_path)
    assert info["is_lambdascan"] is True
    assert info["z_planes"] == n_lambda

    store_lambda_max = ensure_cache(lif_path, tile=64, selection=PlaneSelection(z="max"))
    root_lambda, _attrs_lambda = open_store(store_lambda_max)
    try:
        assert np.array_equal(np.asarray(root_lambda["0"][0]), lambda_arr.max(axis=0))
    finally:
        if hasattr(root_lambda, "close"):
            root_lambda.close()

    store_rgb = ensure_cache(lif_path, tile=64, selection=PlaneSelection(p=1))
    root_rgb, attrs_rgb = open_store(store_rgb)
    try:
        assert attrs_rgb["nd2wsi"]["rgb"] is True
        assert np.array_equal(np.asarray(root_rgb["0"][:]), np.moveaxis(rgb_arr, -1, 0))
    finally:
        if hasattr(root_rgb, "close"):
            root_rgb.close()


def test_lif_server_viewer_luts_annotations_and_export(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ND2WSI_LUT_SETTINGS_ROOT", str(tmp_path / "lut-settings"))
    h, w = 128, 128
    yy, xx = np.mgrid[0:h, 0:w]
    ch0 = ((yy * 40 + xx * 20) % 4096).astype(np.uint16)
    ch1 = ((yy * 15 + (w - xx) * 35) % 4096).astype(np.uint16)
    payload = np.stack([ch0, ch1], axis=0).tobytes()

    lif_path = build_synthetic_lif(
        tmp_path / "viewer_slide.lif",
        [
            {
                "name": "Cortex_40x",
                "payload": payload,
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="{w * 0.25e-6:.9e}" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="{h * 0.25e-6:.9e}" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Green" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Red" BytesInc="{h * w * 2}"/>',
                ],
            }
        ],
    )

    httpd = create_server([])
    base = server_url(httpd)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            base + "api/open",
            data=json.dumps({"path": str(lif_path)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            opened = json.loads(resp.read())
        sid = opened["sid"]

        with urllib.request.urlopen(f"{base}s/{sid}/api/info") as resp:
            info = json.loads(resp.read().decode("utf-8"))
        assert info["name"] == "viewer_slide.lif"
        assert info["width"] == w and info["height"] == h
        assert [c["label"] for c in info["channels"]] == ["Green", "Red"]

        # Render composite tile with custom LUT adjustments
        with urllib.request.urlopen(
            f"{base}s/{sid}/api/tile/0/0/0.jpg?c=0,1&win=0:2000:1.2,100:3500:0.9"
        ) as resp:
            tile_img = Image.open(io.BytesIO(resp.read()))
            assert tile_img.size == (128, 128)

        # Save and read LUT settings
        lut_req = urllib.request.Request(
            f"{base}s/{sid}/api/lut-settings",
            data=json.dumps(
                {
                    "format": "nd2wsi-lut-settings/1",
                    "luts": [
                        {"lo": 15.0, "hi": 1900.0, "gamma": 1.15},
                        {"lo": 25.0, "hi": 2400.0, "gamma": 0.95},
                    ],
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(lut_req) as resp:
            assert json.loads(resp.read())["ok"] is True

        # Save and reload ROI annotation sidecar
        ann_req = urllib.request.Request(
            f"{base}s/{sid}/api/annotations",
            data=json.dumps(
                {
                    "items": [
                        {
                            "id": "roi-lif-1",
                            "type": "rect",
                            "x": 8,
                            "y": 12,
                            "w": 32,
                            "h": 24,
                            "label": "Astrocyte Soma",
                        }
                    ]
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(ann_req) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True

        with urllib.request.urlopen(f"{base}s/{sid}/api/annotations") as resp:
            loaded = json.loads(resp.read().decode("utf-8"))
            assert len(loaded["items"]) == 1
            assert loaded["items"][0]["label"] == "Astrocyte Soma"

        # Export native-value 16-bit TIFF ROI
        with urllib.request.urlopen(
            f"{base}s/{sid}/api/roi?level=0&x=8&y=12&w=32&h=24&format=tiff"
        ) as resp:
            tif_arr = tifffile.imread(io.BytesIO(resp.read()))
        assert tif_arr.shape == (2, 24, 32)
        assert np.array_equal(tif_arr[0], ch0[12:36, 8:40])
        assert np.array_equal(tif_arr[1], ch1[12:36, 8:40])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_lif_cli_info_and_crop_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    arr = np.arange(64 * 64, dtype=np.uint8).reshape(64, 64)
    lif_path = build_synthetic_lif(
        tmp_path / "cli_check.lif",
        [
            {
                "name": "S0",
                "payload": arr.tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="64" BytesInc="1" Length="6.4e-5" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="64" BytesInc="64" Length="6.4e-5" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
                ],
            }
        ],
    )
    rc = cli_main(["info", str(lif_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["format"] == "Leica LIF (LAS AF / LAS X)"
    assert out["width"] == 64 and out["height"] == 64

    with pytest.raises(SystemExit):
        cli_main(
            [
                "crop",
                str(lif_path),
                str(tmp_path / "crop.tif"),
                "--x",
                "0",
                "--y",
                "0",
                "--w",
                "16",
                "--h",
                "16",
            ]
        )
    err = capsys.readouterr().err
    assert "open the LIF with 'view' and use ROI export" in err
