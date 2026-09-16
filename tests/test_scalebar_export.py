"""Physical calibration and lossless image preservation in publication exports."""

import base64
import io
import json
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import numpy as np
import pytest
import zarr
from PIL import Image

from nd2wsi import render, scalebar
from nd2wsi.convert import build_group_attrs
from nd2wsi.reader import ChannelInfo, PlaneSource
from nd2wsi.server import create_server, server_url

SVG = {"s": "http://www.w3.org/2000/svg"}


@pytest.fixture
def specimen(tmp_path):
    data = np.random.default_rng(17).integers(0, 4096, (2, 256, 320), dtype=np.uint16)
    src = PlaneSource(data=None, dtype=data.dtype, shape=data.shape, rgb=False,
                      channels=[ChannelInfo("CY5", (255, 0, 0)), ChannelInfo("DAPI", (0, 0, 255))],
                      pixel_size_um=(0.8, 0.5), source_name="synthetic.nd2",
                      selection={"t": 0, "p": 0, "z": 0})
    attrs = build_group_attrs(src, [(256, 320), (128, 160)], 128,
                              [dict(start=0, end=4095, min=0, max=4095)] * 2)
    path = tmp_path / "synthetic.ome.zarr"
    root = zarr.open_group(str(path), mode="w")
    root.create_array("0", data=data, chunks=(1, 128, 128))
    root.create_array("1", data=data[:, ::2, ::2], chunks=(1, 128, 128))
    root.attrs.update(attrs)
    return root, attrs, path


@pytest.mark.parametrize("downsample", [1, 2, 4, 8])
def test_scale_uses_horizontal_calibration_and_export_level(downsample):
    bar = scalebar.layout(800 // downsample, 776 // downsample, [1.2, 0.5], downsample)
    assert bar.length_um == 50
    assert bar.width * 0.5 * downsample == pytest.approx(50)
    assert bar.width == pytest.approx(100 / downsample)


def test_approved_example_has_exact_100_um_vector_length():
    bar = scalebar.layout(796, 775, [0.6599744317231592] * 2, 1)
    assert bar.label == "100 µm"
    assert bar.width == pytest.approx(151.52102141124644)
    assert bar.thickness == 6 and bar.font_size == 24


@pytest.mark.parametrize("calibration", [None, [], [1], [0, 1], [1, -1],
                                        [float("nan"), 1], [1, float("inf")], ["unknown", 1]])
def test_unknown_or_invalid_calibration_never_fabricates_a_bar(calibration):
    with pytest.raises(ValueError, match="calibration"):
        scalebar.layout(796, 775, calibration, 1)


@pytest.mark.parametrize("downsample", [0, -1, float("nan"), float("inf")])
def test_invalid_level_scale_is_rejected(downsample):
    with pytest.raises(ValueError, match="calibration"):
        scalebar.layout(796, 775, [0.5, 0.5], downsample)


def test_readable_annotation_requires_a_large_enough_export():
    for width, height in [(95, 775), (796, 63)]:
        with pytest.raises(ValueError, match="larger export"):
            scalebar.layout(width, height, [0.5, 0.5], 1)


@pytest.mark.parametrize("channels,win", [([0, 1], None), ([1], "30:3600:1.6,100:2800:2"),
                                        ([], None)])
def test_svg_embeds_exact_rendered_png_and_editable_calibrated_geometry(specimen, channels, win):
    root, attrs, _ = specimen
    args = (root, attrs, 0, 15, 20, 200, 180, channels)
    png = render.export_roi_rendered(*args, "png", win)
    body = render.export_roi_rendered(*args, "svg", win, scale_bar=True)
    xml = ET.fromstring(body)
    embedded = xml.find("s:image", SVG).get("{http://www.w3.org/1999/xlink}href")
    assert base64.b64decode(embedded.split(",", 1)[1]) == png
    rect = xml.find("s:g/s:rect", SVG)
    label = xml.find("s:g/s:text", SVG)
    assert float(rect.get("width")) * 0.5 == pytest.approx(20)
    assert label.text == "20 µm"
    assert xml.get("viewBox") == "0 0 200 180"
    assert json.loads(xml.find("s:metadata", SVG).text)["roi"]["x"] == 15


def test_second_level_uses_actual_downsample_even_when_level_zero_is_omitted(specimen):
    root, attrs, _ = specimen
    attrs["nd2wsi"]["levels"] = attrs["nd2wsi"]["levels"][1:]
    body = render.export_roi_rendered(root, attrs, 1, 0, 0, 160, 128, [0, 1], "svg")
    xml = ET.fromstring(body)
    assert float(xml.find("s:g/s:rect", SVG).get("width")) == pytest.approx(20)
    assert xml.find("s:g/s:text", SVG).text == "20 µm"


def test_annotation_compositing_leaves_other_pixels_and_source_unchanged():
    original = np.random.default_rng(19).integers(0, 200, (775, 796, 3), dtype=np.uint8)
    image = Image.fromarray(original.copy())
    bar = scalebar.layout(796, 775, [0.6599744317231592] * 2, 1)
    annotated = np.asarray(scalebar.draw(image, bar))
    assert np.array_equal(annotated[:680], original[:680])
    assert np.array_equal(annotated[:, :450], original[:, :450])
    middle = int(bar.y + bar.thickness / 2)
    white = (annotated[middle] >= 250).all(axis=1)
    assert abs(int(white.sum()) - bar.width) < 2


def test_jpeg_is_full_size_high_quality_rgb_and_has_bar(specimen):
    root, attrs, _ = specimen
    body = render.export_roi_rendered(root, attrs, 0, 0, 0, 320, 256, [], "jpg", scale_bar=True)
    image = Image.open(io.BytesIO(body))
    assert image.format == "JPEG" and image.size == (320, 256)
    assert image.mode == "RGB"
    bar = scalebar.layout(320, 256, [0.8, 0.5], 1)
    assert min(image.getpixel((round(bar.x + bar.width / 2), round(bar.y)))) > 230
    assert max(image.getpixel((10, 10))) < 3


@pytest.fixture
def endpoint(specimen):
    _, _, path = specimen
    server = create_server(path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server_url(server).rstrip("/"), server
    finally:
        server.shutdown()
        server.server_close()
        server.registry.close_all(immediate=True)
        thread.join(timeout=3)


@pytest.mark.parametrize("fmt,mime", [("svg", "image/svg+xml"), ("jpg", "image/jpeg")])
def test_http_download_has_correct_type_and_scalebar_filename(endpoint, fmt, mime):
    base, _ = endpoint
    with urllib.request.urlopen(base + f"/api/roi?x=3&y=5&w=200&h=160&format={fmt}&scalebar=1", timeout=10) as response:
        assert response.headers["Content-Type"].startswith(mime)
        assert f"_L0_x3_y5_200x160_scalebar.{fmt}" in response.headers["Content-Disposition"]
        assert response.read()


def test_http_refuses_unknown_calibration_but_ordinary_export_still_works(endpoint):
    base, server = endpoint
    for state in server.registry.slides.values():
        state.attrs["nd2wsi"]["pixel_size_um"] = None
    url = base + "/api/roi?x=0&y=0&w=200&h=160&format="
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(url + "svg", timeout=10)
    assert caught.value.code == 400
    assert b"calibration" in caught.value.read()
    with urllib.request.urlopen(url + "png", timeout=10) as response:
        assert Image.open(io.BytesIO(response.read())).size == (200, 160)


def test_raw_export_cannot_accidentally_burn_in_a_bar(endpoint):
    base, _ = endpoint
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(base + "/api/roi?x=0&y=0&w=200&h=160&format=tiff&scalebar=1", timeout=10)
    assert caught.value.code == 400
