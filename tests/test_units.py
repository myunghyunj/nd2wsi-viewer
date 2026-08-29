"""Fast unit tests that need no ND2 file."""
import numpy as np
import pytest

from nd2wsi.convert import _even_chunks, build_group_attrs
from nd2wsi.reader import ChannelInfo, PlaneSource, level_shapes
from nd2wsi.render import composite, parse_channels


def test_level_shapes_halve_until_tile():
    shapes = level_shapes(9600, 12800, 512)
    assert shapes[0] == (9600, 12800)
    assert shapes[-1][0] <= 512 and shapes[-1][1] <= 512
    for (h0, w0), (h1, w1) in zip(shapes, shapes[1:]):
        assert h1 == h0 // 2 and w1 == w0 // 2


def test_level_shapes_small_image_single_level():
    assert level_shapes(100, 200, 512) == [(100, 200)]


def test_even_chunks():
    assert _even_chunks(1024, 256) == (256, 256, 256, 256)
    assert _even_chunks(1000, 256) == (256, 256, 256, 232)
    assert sum(_even_chunks(998, 256)) == 998


def test_parse_channels():
    assert parse_channels(None, 3) == [0, 1, 2]
    assert parse_channels("1", 3) == [1]
    assert parse_channels("0,2,9", 3) == [0, 2]
    assert parse_channels(",", 3) == [0, 1, 2]


def test_composite_rgb_passthrough():
    region = np.zeros((3, 4, 4), np.uint8)
    region[0] = 200
    out = composite(region, [0, 1, 2], [(0, 255)] * 3, [(255, 0, 0)] * 3, rgb=True)
    assert out.shape == (4, 4, 3)
    assert out[0, 0, 0] == 200 and out[0, 0, 1] == 0


def test_composite_window_and_color():
    region = np.array([[[100, 200]]], np.uint16)  # (1, 1, 2)
    out = composite(region, [0], [(100, 200)], [(0, 255, 0)], rgb=False)
    assert tuple(out[0, 0]) == (0, 0, 0)
    assert tuple(out[0, 1]) == (0, 255, 0)


def test_group_attrs_shape():
    src = PlaneSource(
        data=None,
        dtype=np.dtype("uint8"),
        shape=(3, 1000, 2000),
        rgb=True,
        channels=[ChannelInfo("R", (255, 0, 0)), ChannelInfo("G", (0, 255, 0)),
                  ChannelInfo("B", (0, 0, 255))],
        pixel_size_um=(0.5, 0.5),
        source_name="x.nd2",
        selection={"t": 0, "p": 0, "z": 0},
    )
    shapes = level_shapes(1000, 2000, 512)
    attrs = build_group_attrs(src, shapes, 512, [dict(start=0, end=255, min=0, max=255)] * 3)
    ms = attrs["multiscales"][0]
    assert [a["name"] for a in ms["axes"]] == ["c", "y", "x"]
    assert len(ms["datasets"]) == len(shapes)
    assert ms["datasets"][1]["coordinateTransformations"][0]["scale"] == [1.0, 1.0, 1.0]
    assert attrs["nd2wsi"]["levels"][0]["width"] == 2000
