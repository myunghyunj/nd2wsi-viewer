"""ND2 export round-trips (need limnd2, from Laboratory Imaging's index)."""
import numpy as np
import pytest

limnd2 = pytest.importorskip("limnd2")
nd2 = pytest.importorskip("nd2")


@pytest.fixture()
def fluor_nd2(tmp_path):
    """A small 2-channel uint16 ND2 written with limnd2 (the fixture writer)."""
    rng = np.random.default_rng(11)
    img = rng.integers(0, 4000, size=(700, 900, 2), dtype=np.uint16)  # (Y, X, C)
    path = tmp_path / "fluor.nd2"
    attrs = limnd2.ImageAttributes.create(
        width=900, height=700, component_count=2, bits=16, sequence_count=1
    )
    with limnd2.Nd2Writer(str(path)) as f:
        f.imageAttributes = attrs
        f.setImage(0, img)
        mf = limnd2.MetadataFactory(
            objective_magnification=20.0, pixel_calibration=0.66
        )
        mf.addPlane(name="CY5", color="#FF0000")
        mf.addPlane(name="DAPI", color="#0000FF")
        f.pictureMetadata = mf.createMetadata()
    return path, img


def test_crop_nd2_to_nd2_roundtrip(fluor_nd2, tmp_path):
    from nd2wsi.export_nd2 import crop_nd2_to_nd2

    src_path, img = fluor_nd2
    out = tmp_path / "roi.nd2"
    # unaligned, odd-sized region spanning several 512-tiles
    res = crop_nd2_to_nd2(src_path, out, x=123, y=77, w=601, h=555)
    assert (res["w"], res["h"]) == (601, 555)

    with nd2.ND2File(str(out)) as f:
        back = np.moveaxis(np.array(f.read_frame(0)), 0, -1)  # (Y, X, C)
        assert np.array_equal(back, img[77 : 77 + 555, 123 : 123 + 601])
        assert f.voxel_size().x == pytest.approx(0.66)
        chs = f.metadata.channels
        assert [c.channel.name for c in chs] == ["CY5", "DAPI"]
        assert (chs[0].channel.color.r, chs[0].channel.color.g) == (255, 0)
        assert (f.attributes.compressionType or "none") == "none"


def test_crop_channel_subset(fluor_nd2, tmp_path):
    from nd2wsi.export_nd2 import crop_nd2_to_nd2

    src_path, img = fluor_nd2
    out = tmp_path / "roi_c1.nd2"
    crop_nd2_to_nd2(src_path, out, x=10, y=20, w=100, h=80, channels=[1])
    with nd2.ND2File(str(out)) as f:
        back = np.array(f.read_frame(0))
        assert back.shape == (80, 100)  # single channel -> plain 2D frame
        assert np.array_equal(back, img[20:100, 10:110, 1])
        assert [c.channel.name for c in f.metadata.channels] == ["DAPI"]


def test_store_roi_export_matches_source(fluor_nd2, tmp_path):
    """convert -> export_roi_nd2 -> identical pixels + metadata carried over."""
    from nd2wsi.convert import convert, open_store
    from nd2wsi.export_nd2 import export_roi_nd2

    src_path, img = fluor_nd2
    store = tmp_path / "fluor.ome.zarr"
    convert(src_path, store, progress=False)
    root, attrs = open_store(store)
    assert attrs["nd2wsi"]["objective_magnification"] == pytest.approx(20.0)

    out = tmp_path / "store_roi.nd2"
    export_roi_nd2(root, attrs, out, level=0, x=333, y=41, w=512, h=300,
                   channels=[0, 1])
    with nd2.ND2File(str(out)) as f:
        back = np.moveaxis(np.array(f.read_frame(0)), 0, -1)
        assert np.array_equal(back, img[41 : 41 + 300, 333 : 333 + 512])
        assert f.voxel_size().x == pytest.approx(0.66)
