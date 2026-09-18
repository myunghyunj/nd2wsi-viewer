"""True-colour components are independent LUTs, not fluorescence channels."""
import base64
import io
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from nd2wsi import render
from nd2wsi.convert import _percentile_windows, build_group_attrs
from nd2wsi.reader import ChannelInfo, PlaneSource, _frame_to_cyx

RGB = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
WINDOWS = [(0, 255)] * 3


def test_rgb_nd2_sample_axis_remains_rgb_not_three_fluorescence_channels():
    frame = np.arange(2 * 4 * 3, dtype=np.uint8).reshape(2, 4, 3)
    cyx, rgb = _frame_to_cyx(frame, {"Y": 2, "X": 4, "S": 3})
    assert rgb and cyx.shape == (3, 2, 4)
    np.testing.assert_array_equal(cyx, np.moveaxis(frame, -1, 0))


def test_default_rgb_display_preserves_every_8bit_value_without_mutating_input():
    data = np.stack([np.arange(256, dtype=np.uint8),
                     np.arange(255, -1, -1, dtype=np.uint8),
                     np.full(256, 113, dtype=np.uint8)])[:, None, :]
    before = data.copy()
    image = render.composite(data, [0, 1, 2], WINDOWS, RGB, True)
    np.testing.assert_array_equal(image, np.moveaxis(data, 0, -1))
    np.testing.assert_array_equal(data, before)


@pytest.mark.parametrize("component", [0, 1, 2])
@pytest.mark.parametrize("gamma", [1, 2])
def test_each_rgb_component_has_an_independent_window_and_gamma(component, gamma):
    data = np.array([[[20, 70, 220]]] * 3, dtype=np.uint8)
    windows = WINDOWS.copy()
    windows[component] = (20, 220)
    gammas = [1, 1, 1]
    gammas[component] = gamma
    result = render.composite(data, [0, 1, 2], windows, RGB, True, gammas)
    for ci in range(3):
        expected = [0, 63 if gamma == 1 else 127, 255] if ci == component else [20, 70, 220]
        assert result[0, :, ci].tolist() == expected


@pytest.mark.parametrize("channels", [[0], [1], [2], [0, 2], [2, 1, 0], []])
def test_rgb_visibility_keeps_fixed_component_slots(channels):
    data = np.array([[[10]], [[20]], [[30]]], dtype=np.uint8)
    # Even a stale colour label must not turn RGB into additive pseudocolour.
    image = render.composite(data, channels, WINDOWS, [(255, 255, 255)] * 3, True)
    assert image.tolist() == [[[v if i in channels else 0 for i, v in enumerate((10, 20, 30))]]]


def test_rgb_defaults_and_histograms_preserve_full_8bit_range():
    data = np.array([[[10, 20]], [[30, 40]], [[50, 60]]], dtype=np.uint8)
    root = {"0": data}
    windows = _percentile_windows(root, ["0"], SimpleNamespace(dtype=data.dtype, rgb=True))
    assert windows == [{"start": 0.0, "end": 255.0, "min": 0.0, "max": 255.0}] * 3
    attrs = {"nd2wsi": {"levels": [{"path": "0", "width": 2, "height": 1}]},
             "omero": {"channels": [{"window": window} for window in windows]}}
    histograms = render.compute_histograms(root, attrs, min_pixels=1)
    assert len(histograms) == 3
    assert all((h["vmin"], h["vmax"]) == (0, 255) for h in histograms)
    assert [h["detail"]["values"] for h in histograms] == [[10, 20], [30, 40], [50, 60]]


def test_rgb_tile_png_and_svg_exports_share_component_luts():
    data = np.random.default_rng(42).integers(0, 256, (3, 128, 128), dtype=np.uint8)
    before = data.copy()
    source = PlaneSource(data=None, dtype=data.dtype, shape=data.shape, rgb=True,
                         channels=[ChannelInfo(n, c) for n, c in zip(("Red", "Green", "Blue"), RGB)],
                         pixel_size_um=(0.17, 0.17), source_name="brightfield.nd2")
    defaults = [{"start": 0, "end": 255, "min": 0, "max": 255}] * 3
    attrs = build_group_attrs(source, [(128, 128)], 128, defaults)
    root = {"0": data}
    win = "0:255,20:220:2,30:200:0.5"
    tile = render.render_tile(root, attrs, 0, 0, 0, [0, 1, 2], "png", win)
    png = render.export_roi_rendered(root, attrs, 0, 0, 0, 128, 128, [0, 1, 2], "png", win)
    svg = render.export_roi_rendered(root, attrs, 0, 0, 0, 128, 128, [0, 1, 2], "svg", win)
    xml = ET.fromstring(svg)
    embedded = xml.find("{http://www.w3.org/2000/svg}image").get("{http://www.w3.org/1999/xlink}href")
    assert base64.b64decode(embedded.split(",", 1)[1]) == png
    image = np.asarray(Image.open(io.BytesIO(png)))
    np.testing.assert_array_equal(Image.open(io.BytesIO(tile)), image)
    np.testing.assert_array_equal(image[..., 0], data[0])
    assert not np.array_equal(image[..., 1], data[1])
    assert not np.array_equal(image[..., 2], data[2])
    np.testing.assert_array_equal(data, before)
