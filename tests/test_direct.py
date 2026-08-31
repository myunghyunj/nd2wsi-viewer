"""An SVS served straight from the file, with no pyramid store."""
import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")

from nd2wsi.direct import open_direct  # noqa: E402
from nd2wsi.server import SlideRegistry  # noqa: E402


@pytest.fixture()
def svs(tmp_path):
    """Baseline plus an embedded 4x level, tiled lossless JPEG 2000."""
    rng = np.random.default_rng(9)
    img = rng.integers(0, 255, (2200, 2900, 3), dtype=np.uint8)
    p = tmp_path / "d.svs"
    opts = dict(tile=(240, 240), photometric="rgb", compression="jpeg2000",
                compressionargs={"reversible": True})
    with tifffile.TiffWriter(p) as tw:
        tw.write(img, subifds=1,
                 description="Aperio Image|MPP = 0.25|AppMag = 20", **opts)
        small = (img[:2200, :2900].reshape(550, 4, 725, 4, 3)
                 .mean(axis=(1, 3)).astype(np.uint8))
        tw.write(small, subfiletype=1, **opts)
    return p, img, small


def test_direct_ladder_and_baseline_pixels(svs):
    p, img, small = svs
    root, attrs = open_direct(p)
    try:
        meta = attrs["nd2wsi"]
        assert meta["direct"] is True
        assert [lv["path"] for lv in meta["levels"]] == ["0", "1", "2", "3"]

        got = np.moveaxis(root["0"][:, :, :], 0, -1)
        assert (got == img).all()  # embedded baseline, decode-exact

        lv2 = np.moveaxis(root["2"][:, :, :], 0, -1)
        assert (lv2 == small).all()  # the file's own 4x level, untouched

        # a gap level is the 2x2 mean of the next finer embedded level
        ref = (img[:1100 * 2, :1450 * 2].reshape(1100, 2, 1450, 2, 3)
               .mean(axis=(1, 3)))
        lv1 = np.moveaxis(root["1"][:, :, :], 0, -1)
        assert np.abs(lv1.astype(float) - ref).max() <= 1.0
    finally:
        root.close()


def test_direct_supports_the_export_access_pattern(svs):
    p, img, _ = svs
    root, attrs = open_direct(p)
    try:
        block = root["0"][[0, 2], 300:900, 400:1100]
        ref = np.moveaxis(img, -1, 0)[[0, 2], 300:900, 400:1100]
        assert (block == ref).all()
        assert root["0"].shape == (3, 2200, 2900)
        assert root["0"].dtype == np.uint8
    finally:
        root.close()


def test_registry_opens_an_svs_without_writing_anything(svs, tmp_path):
    p, img, _ = svs
    registry = SlideRegistry()
    sid = registry.open_path(p)
    st = registry.slides[sid]
    assert st.attrs["nd2wsi"]["direct"] is True
    assert st.store_path is None
    assert not (tmp_path / "pyramids").exists()  # truly nothing on disk

    with pytest.raises(ValueError, match="no cache"):
        registry.trash_cache(sid)
    assert registry.remove(sid)  # closes the file handles


def test_an_existing_store_still_wins(svs):
    from nd2wsi.convert import convert, default_store_path

    p, img, _ = svs
    store = default_store_path(p)
    convert(p, store, tile=512, progress=False)
    registry = SlideRegistry()
    sid = registry.open_path(p)
    st = registry.slides[sid]
    assert not st.attrs["nd2wsi"].get("direct")
    assert st.store_path == store


def test_rgba_svs_serves_every_level(tmp_path):
    """Alpha is dropped before the gap-level mean, not fed into it."""
    rng = np.random.default_rng(3)
    rgba = rng.integers(0, 255, (1100, 1450, 4), dtype=np.uint8)
    p = tmp_path / "a.svs"
    opts = dict(tile=(240, 240), photometric="rgb", extrasamples=["unassalpha"],
                compression="jpeg2000", compressionargs={"reversible": True})
    with tifffile.TiffWriter(p) as tw:
        tw.write(rgba, subifds=1, **opts)
        small = (rgba[:1100, :1448].reshape(275, 4, 362, 4, 4)
                 .mean(axis=(1, 3)).astype(np.uint8))
        tw.write(small, subfiletype=1, **opts)
    root, attrs = open_direct(p)
    try:
        for lv in attrs["nd2wsi"]["levels"]:
            block = root[lv["path"]][:, :, :]
            assert block.shape[0] == 3  # alpha gone at every level
        got = np.moveaxis(root["0"][:, :, :], 0, -1)
        assert (got == rgba[..., :3]).all()
    finally:
        root.close()


def test_off_ladder_embedded_levels_are_ignored(tmp_path):
    """A 3x sublevel is not on the halving ladder, so with nothing else
    usable the file is refused and callers convert instead."""
    rng = np.random.default_rng(4)
    img = rng.integers(0, 255, (1200, 1500, 3), dtype=np.uint8)
    p = tmp_path / "b.svs"
    opts = dict(tile=(240, 240), photometric="rgb",
                compression="jpeg2000", compressionargs={"reversible": True})
    with tifffile.TiffWriter(p) as tw:
        tw.write(img, subifds=1, **opts)
        odd = img[::3, ::3]  # a 3x level, off the power-of-two ladder
        tw.write(odd, subfiletype=1, **opts)
    with pytest.raises(NotImplementedError):
        open_direct(p)


def test_level_dims_never_exceed_the_source():
    """A level advertises exactly what its source page can serve, so no
    request can cross the source edge, and out-of-range indexing raises
    instead of returning zeros."""
    from nd2wsi.direct import _SvsLevel

    calls = []

    def read(y0, y1, x0, x1):
        calls.append((y0, y1, x0, x1))
        assert 0 <= y0 <= y1 <= 494 and 0 <= x0 <= x1 <= 598
        block = np.full((y1 - y0, x1 - x0, 3), 7, np.uint8)
        return block

    # a 494 x 598 source serving a gap level at 2x: dims floor to 247 x 299
    lv = _SvsLevel(read, 494 // 2, 598 // 2, extra=2)
    assert lv.shape == (3, 247, 299)

    block = lv[:, 240:247, 290:299]
    assert block.shape == (3, 7, 9)
    assert (block == 7).all()  # nothing zero-filled at the edge
    assert calls[-1] == (480, 494, 580, 598)

    assert (lv[:, -1, -1] == 7).all()  # negative indices resolve
    assert lv[:, 0:500, 0:10].shape == (3, 247, 10)  # slices clamp, like zarr
    with pytest.raises(IndexError):
        lv[:, 999, 0]  # an integer index past the edge raises
    with pytest.raises(IndexError):
        lv[:, ::2, :]  # strided
