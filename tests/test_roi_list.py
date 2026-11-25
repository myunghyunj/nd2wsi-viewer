"""Native reading leaves coordinate semantics to the shared AstroMapper importer."""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from nd2wsi import roi_list
from nd2wsi.roi_list import (
    _MAX_REFERENCE_HEADER_PIXELS,
    MAX_BYTES,
    ROI_IMAGE_SUFFIXES,
    _read_image_size_stream,
    load_roi_file,
    roi_pair_paths,
)

ROOT = Path(__file__).resolve().parents[1]
ASTROMAP_IMPORT_JS = ROOT / "nd2wsi" / "static" / "astromap-import-v1.js"
TEXT = "INDEX\tX1\tY1\tX2\tY2\tNOTE\n7\t10\t20\t13\t25\tfirst\n"


def _png_header(width: int, height: int, *, total_size: int = 33, chunk_len: int = 13, chunk_type: bytes = b"IHDR") -> bytes:
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", chunk_len) + chunk_type + struct.pack(">II", width, height)
    if total_size > len(header):
        header += b"\x00" * (total_size - len(header))
    return header[:total_size]


def _jpeg_sof_segment(marker: int, width: int, height: int, *, components: int = 3, declared_len: int | None = None) -> bytes:
    body = struct.pack(">BHHB", 8, height, width, components) + b"\x00" * (3 * components)
    length = len(body) + 2 if declared_len is None else declared_len
    return bytes([0xFF, marker]) + struct.pack(">H", length) + body


def _jpeg_app_segment(marker: int, payload_len: int) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", payload_len + 2) + (b"\x42" * payload_len)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_pair_is_read_only_and_returns_stored_axes(tmp_path, encoding):
    path = tmp_path / "image.txt"
    content = TEXT.encode(encoding)
    path.write_bytes(content)
    jpg = path.with_suffix(".jpg")
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (80, 60)).save(jpg, exif=exif)
    image_bytes = jpg.read_bytes()
    result = load_roi_file(path, width=800, height=600)
    assert result == {"ok": True, "name": "image.txt", "text": TEXT,
                      "coordinateSize": {"width": 80, "height": 60},
                      "reference": {"name": "image.jpg", "width": 80, "height": 60}}
    assert path.read_bytes() == content and jpg.read_bytes() == image_bytes


def test_missing_or_unmatched_pair_is_rejected(tmp_path):
    path = tmp_path / "image.txt"
    path.write_text(TEXT)
    with pytest.raises(ValueError, match="together"):
        load_roi_file(path, width=800, height=600)
    other = tmp_path / "other.jpg"
    Image.new("RGB", (80, 60)).save(other)
    with pytest.raises(ValueError, match="same filename"):
        load_roi_file(path, width=800, height=600, image_path=other)


def test_bounded_file_and_extension(tmp_path):
    path = tmp_path / "bad.txt"
    Image.new("RGB", (80, 60)).save(path.with_suffix(".jpg"))
    path.write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match="32 MiB"):
        load_roi_file(path, width=80, height=60)
    with pytest.raises(ValueError, match=".txt"):
        load_roi_file(path.with_suffix(".json"), width=80, height=60)


def test_explicit_uppercase_named_pair_and_png(tmp_path):
    path, image = tmp_path / "image.txt", tmp_path / "IMAGE.PNG"
    path.write_text(TEXT)
    Image.new("RGB", (80, 60)).save(image)
    result = load_roi_file(path, width=800, height=600, image_path=image)
    assert result["reference"]["name"] == "IMAGE.PNG"


def test_load_roi_file_never_mutates_pil_max_image_pixels(tmp_path, monkeypatch):
    sentinel = 12_345
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", sentinel)
    path = tmp_path / "slide.txt"
    path.write_text(TEXT)
    jpg = tmp_path / "slide.jpg"
    Image.new("RGB", (80, 60)).save(jpg)
    result = load_roi_file(path, width=800, height=600)
    assert result["coordinateSize"] == {"width": 80, "height": 60}
    assert Image.MAX_IMAGE_PIXELS == sentinel
    assert "PIL" not in Path(roi_list.__file__).read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO non-blocking guard")
def test_fifo_reference_image_is_rejected_without_blocking(tmp_path):
    path = tmp_path / "slide.txt"
    path.write_text(TEXT)
    fifo = tmp_path / "slide.jpg"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular reference image"):
        load_roi_file(path, width=800, height=600, image_path=fifo)


def test_directory_reference_image_is_rejected(tmp_path):
    path = tmp_path / "slide.txt"
    path.write_text(TEXT)
    folder = tmp_path / "slide.jpg"
    folder.mkdir()
    with pytest.raises((ValueError, OSError)):
        load_roi_file(path, width=800, height=600, image_path=folder)


def test_roi_pair_paths_supports_png_and_case_insensitive_stems(tmp_path):
    assert ROI_IMAGE_SUFFIXES == (".jpg", ".jpeg", ".png")
    txt = tmp_path / "Slide.txt"
    png = tmp_path / "SLIDE.PNG"
    txt.touch()
    png.touch()

    assert roi_pair_paths([png, txt]) == [txt, png]
    assert roi_pair_paths([txt]) == [txt, png]
    assert roi_pair_paths([png]) == [txt, png]

    # Ambiguous siblings (.png + .jpg) are refused when auto-resolving a single file.
    jpg = tmp_path / "slide.jpg"
    jpg.touch()
    with pytest.raises(ValueError, match="More than one"):
        roi_pair_paths([txt], require_pair=True)
    assert roi_pair_paths([txt], require_pair=False) == [txt]

    # Unsupported suffixes and mismatched stems are refused.
    with pytest.raises(ValueError):
        roi_pair_paths([txt, tmp_path / "Slide.bmp"])
    with pytest.raises(ValueError):
        roi_pair_paths([txt, tmp_path / "Other.png"])


def test_roi_companion_discovery_routes_through_filesystem_path_and_returns_plain_paths(
    tmp_path, monkeypatch
):
    seen: list[str] = []
    real_fs_path = roi_list.filesystem_path

    def spy_fs_path(p, **kw):
        res = real_fs_path(p, **kw)
        seen.append(res)
        return res

    monkeypatch.setattr(roi_list, "filesystem_path", spy_fs_path)

    txt = tmp_path / "sample.txt"
    jpg = tmp_path / "sample.jpg"
    txt.write_text(TEXT, encoding="utf-8")
    Image.new("RGB", (80, 60)).save(jpg)

    # 1. roi_pair_paths routes scandir and is_file through filesystem_path and returns plain Paths.
    pair = roi_pair_paths([txt])
    assert pair == [txt, jpg]
    assert all(not str(p).startswith("\\\\?\\") for p in pair)
    assert real_fs_path(tmp_path) in seen
    assert real_fs_path(jpg) in seen

    # 2. load_roi_file companion auto-discovery routes is_file + os.open through filesystem_path.
    seen.clear()
    result = load_roi_file(txt, width=800, height=600)
    assert result["ok"] is True
    assert result["coordinateSize"] == {"width": 80, "height": 60}
    assert result["reference"] == {"name": "sample.jpg", "width": 80, "height": 60}
    assert real_fs_path(txt) in seen
    assert seen.count(real_fs_path(jpg)) >= 2


def test_header_reader_enforces_2b_pixel_cap_and_invalid_dimensions():
    assert _MAX_REFERENCE_HEADER_PIXELS == 2_000_000_000
    # Exactly at the 2B pixel ceiling succeeds for both JPEG and PNG.
    at_limit_jpg = b"\xff\xd8" + _jpeg_sof_segment(0xC0, 40_000, 50_000) + b"\xff\xd9"
    assert _read_image_size_stream(io.BytesIO(at_limit_jpg), len(at_limit_jpg)) == {"width": 40_000, "height": 50_000}
    at_limit_png = _png_header(40_000, 50_000)
    assert _read_image_size_stream(io.BytesIO(at_limit_png), len(at_limit_png)) == {"width": 40_000, "height": 50_000}

    # Above 2B pixels is refused with the safety-limits error.
    over_limit_jpg = b"\xff\xd8" + _jpeg_sof_segment(0xC0, 50_000, 50_000) + b"\xff\xd9"
    with pytest.raises(ValueError, match="exceed safety limits"):
        _read_image_size_stream(io.BytesIO(over_limit_jpg), len(over_limit_jpg))
    over_limit_png = _png_header(50_000, 50_000)
    with pytest.raises(ValueError, match="exceed safety limits"):
        _read_image_size_stream(io.BytesIO(over_limit_png), len(over_limit_png))

    # Zero dimensions or PNG > 2^31 - 1 report invalid dimensions.
    for data in (
        b"\xff\xd8" + _jpeg_sof_segment(0xC0, 0, 100) + b"\xff\xd9",
        b"\xff\xd8" + _jpeg_sof_segment(0xC0, 100, 0) + b"\xff\xd9",
        _png_header(0, 100),
        _png_header(2_147_483_648, 1),
    ):
        with pytest.raises(ValueError, match="invalid dimensions"):
            _read_image_size_stream(io.BytesIO(data), len(data))

    # Boundary 4096 0xFF padding bytes succeeds; 4097 is rejected.
    pad_ok = b"\xff\xd8\xff" + (b"\xff" * 4096) + _jpeg_sof_segment(0xC0, 12, 34)[1:] + b"\xff\xd9"
    assert _read_image_size_stream(io.BytesIO(pad_ok), len(pad_ok)) == {"width": 12, "height": 34}
    pad_bad = b"\xff\xd8\xff" + (b"\xff" * 4097) + _jpeg_sof_segment(0xC0, 12, 34)[1:] + b"\xff\xd9"
    with pytest.raises(ValueError, match="JPG or PNG"):
        _read_image_size_stream(io.BytesIO(pad_bad), len(pad_bad))

    # Boundary 4096 segments: 4095 metadata segments + SOF succeeds; 4096 metadata segments + SOF fails.
    seg = b"\xff\xe0\x00\x02"
    seg_ok = b"\xff\xd8" + (seg * 4095) + _jpeg_sof_segment(0xC0, 21, 32) + b"\xff\xd9"
    assert _read_image_size_stream(io.BytesIO(seg_ok), len(seg_ok)) == {"width": 21, "height": 32}
    seg_bad = b"\xff\xd8" + (seg * 4096) + _jpeg_sof_segment(0xC0, 21, 32) + b"\xff\xd9"
    with pytest.raises(ValueError, match="JPG or PNG"):
        _read_image_size_stream(io.BytesIO(seg_bad), len(seg_bad))


def test_python_and_node_header_readers_agree_on_shared_binary_vectors(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    vectors = [
        ("baseline-sof0", b"\xff\xd8" + _jpeg_sof_segment(0xC0, 8000, 5676) + b"\xff\xd9", {"width": 8000, "height": 5676}),
        (
            "large-app1-app2-gt-64k",
            b"\xff\xd8" + _jpeg_app_segment(0xE1, 40_000) + _jpeg_app_segment(0xE2, 60_000)
            + _jpeg_sof_segment(0xC0, 31578, 22409) + b"\xff\xd9",
            {"width": 31578, "height": 22409},
        ),
        (
            "progressive-sof2",
            b"\xff\xd8" + _jpeg_app_segment(0xE0, 16) + _jpeg_sof_segment(0xC2, 1920, 1080) + b"\xff\xd9",
            {"width": 1920, "height": 1080},
        ),
        (
            "ff-padding-and-rst",
            b"\xff\xd8\xff\x01\xff\xd0" + (b"\xff" * 32) + _jpeg_sof_segment(0xC1, 640, 480)[1:] + b"\xff\xd9",
            {"width": 640, "height": 480},
        ),
        ("valid-png", _png_header(4096, 3072), {"width": 4096, "height": 3072}),
        ("at-limit-2b-jpeg", b"\xff\xd8" + _jpeg_sof_segment(0xC0, 40_000, 50_000) + b"\xff\xd9", {"width": 40_000, "height": 50_000}),
        ("at-limit-2b-png", _png_header(40_000, 50_000), {"width": 40_000, "height": 50_000}),
        ("over-limit-2b-jpeg", b"\xff\xd8" + _jpeg_sof_segment(0xC0, 50_000, 50_000) + b"\xff\xd9", None),
        ("over-limit-2b-png", _png_header(50_000, 50_000), None),
        ("truncated-jpeg-segment", b"\xff\xd8\xff\xe1\x00\x40" + b"\x00" * 10, None),
        ("jpeg-sos-without-sof", b"\xff\xd8\xff\xda\x00\x02", None),
        ("jpeg-truncated-sof", b"\xff\xd8" + _jpeg_sof_segment(0xC0, 100, 100, components=3, declared_len=10) + b"\xff\xd9", None),
        ("truncated-png", _png_header(100, 100, total_size=24), None),
        ("png-bad-ihdr", _png_header(100, 100, chunk_type=b"XXXX"), None),
        ("not-an-image", b"GIF89a\x01\x00\x01\x00\x00\x00\x00;", None),
    ]

    manifest = []
    for name, payload, expected in vectors:
        file_path = tmp_path / f"{name}.bin"
        file_path.write_bytes(payload)
        manifest.append({"name": name, "path": str(file_path)})
        if expected is None:
            with pytest.raises(ValueError):
                _read_image_size_stream(io.BytesIO(payload), len(payload))
        else:
            assert _read_image_size_stream(io.BytesIO(payload), len(payload)) == expected

    script = (
        "const fs = require('fs');"
        f"const mod = require({json.dumps(str(ASTROMAP_IMPORT_JS))});"
        f"const items = {json.dumps(manifest)};"
        "(async () => {"
        "  const out = { maxPixels: mod.MAX_REFERENCE_HEADER_PIXELS, items: {} };"
        "  for (const item of items) {"
        "    const buf = fs.readFileSync(item.path);"
        "    const file = {"
        "      size: buf.length,"
        "      slice(start, end) {"
        "        const sub = buf.subarray(start, end);"
        "        return { arrayBuffer: async () => sub.buffer.slice(sub.byteOffset, sub.byteOffset + sub.byteLength) };"
        "      },"
        "    };"
        "    try { out.items[item.name] = { ok: true, size: await mod.readImageSize(file) }; }"
        "    catch (e) { out.items[item.name] = { ok: false, error: e.message }; }"
        "  }"
        "  process.stdout.write(JSON.stringify(out));"
        "})().catch(e => { console.error(e); process.exit(1); });"
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    node_payload = json.loads(proc.stdout)
    assert node_payload["maxPixels"] == _MAX_REFERENCE_HEADER_PIXELS == 2_000_000_000
    node_results = node_payload["items"]
    for name, _payload, expected in vectors:
        if expected is None:
            assert node_results[name]["ok"] is False, f"Node unexpectedly accepted {name}"
            if name.startswith("over-limit-2b-"):
                assert "exceed safety limits" in node_results[name]["error"]
        else:
            assert node_results[name] == {"ok": True, "size": expected}, f"Mismatch on {name}"
