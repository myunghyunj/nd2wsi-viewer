"""SVS reading, pyramid geometry and the size question asked before building."""
import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")

from nd2wsi.convert import convert, estimate_store_bytes  # noqa: E402
from nd2wsi.svs import open_svs, sample_planes  # noqa: E402


def write_svs(path, img, tile=240, compression="jpeg2000"):
    kw = {"tile": (tile, tile), "photometric": "rgb"}
    if compression == "jpeg2000":
        kw["compressionargs"] = {"reversible": True}  # lossless, so we can compare
    tifffile.imwrite(path, img, compression=compression, **kw)
    return path


@pytest.fixture()
def slide(tmp_path):
    """A tiled RGB slide whose size is a multiple of neither tile nor chunk."""
    rng = np.random.default_rng(3)
    img = rng.integers(0, 255, (1300, 1717, 3), dtype=np.uint8)
    return write_svs(tmp_path / "slide.svs", img), img


def test_open_svs_reads_every_pixel(slide):
    from contextlib import ExitStack

    path, img = slide
    with ExitStack() as stack:
        src = open_svs(stack, path, tile=512)
        got = np.moveaxis(np.asarray(src.data.compute()), 0, -1)
    assert got.shape == img.shape
    assert (got == img).all()


def test_convert_matches_a_plain_numpy_pyramid(slide, tmp_path):
    import zarr

    path, img = slide
    out = tmp_path / "slide.ome.zarr"
    convert(path, out, progress=False)
    root = zarr.open_group(str(out), mode="r")

    assert (np.moveaxis(np.asarray(root["0"][:]), 0, -1) == img).all()
    ref = np.moveaxis(img, -1, 0).astype(np.float64)
    eh, ew = (img.shape[0] // 2) * 2, (img.shape[1] // 2) * 2
    mean = ref[:, :eh, :ew].reshape(3, eh // 2, 2, ew // 2, 2).mean(axis=(2, 4))
    assert (np.asarray(root["1"][:]) == np.rint(mean).astype(np.uint8)).all()


def test_striped_svs_still_converts(tmp_path):
    """Untiled TIFFs fall back to the tifffile zarr path."""
    import zarr

    rng = np.random.default_rng(5)
    img = rng.integers(0, 255, (600, 700, 3), dtype=np.uint8)
    path = tmp_path / "striped.svs"
    tifffile.imwrite(path, img, photometric="rgb", compression="lzw")
    out = tmp_path / "striped.ome.zarr"
    convert(path, out, progress=False)
    root = zarr.open_group(str(out), mode="r")
    assert (np.moveaxis(np.asarray(root["0"][:]), 0, -1) == img).all()


def test_sample_planes_covers_the_slide(slide):
    path, _ = slide
    blocks = sample_planes(path, tile=512, points=9)
    assert len(blocks) == 9
    assert all(b.shape[0] == 3 for b in blocks)
    assert len({b[0, 0, 0] for b in blocks}) > 1  # not all the same window


def test_estimate_is_close_to_what_convert_writes(slide, tmp_path):
    path, _ = slide
    est = estimate_store_bytes(path, points=9)
    out = tmp_path / "slide.ome.zarr"
    convert(path, out, progress=False)
    actual = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())

    assert est["width"] == 1717 and est["height"] == 1300
    assert est["channels"] == 3
    assert 0.5 < est["bytes"] / actual < 2.0
    assert est["source_bytes"] == path.stat().st_size


def test_window_reader_hands_the_decoder_its_jpeg_tables():
    """Aperio JPEG slides keep one shared table set in TIFF tag 347."""
    from nd2wsi.svs import _window_reader

    seen = {}

    class FakePage:
        imagelength = imagewidth = 8
        tilelength = tilewidth = 8
        samplesperpixel = 1
        dtype = np.uint8
        dataoffsets = (0,)
        databytecounts = (4,)
        jpegtables = b"TABLES"
        jpegheader = b"HEADER"

        def decode(self, data, index, **kw):
            seen.update(kw)
            return (np.zeros((1, 8, 8, 1), np.uint8),)

    import os
    import tempfile

    fd, name = tempfile.mkstemp()
    os.write(fd, b"\0" * 8)
    read = _window_reader(FakePage(), fd)
    read(0, 8, 0, 8)
    os.close(fd)
    os.unlink(name)
    assert seen["jpegtables"] == b"TABLES"
    assert seen["jpegheader"] == b"HEADER"


def test_pyramid_of_a_thin_strip_keeps_its_values(tmp_path):
    """A collapsed axis has no second row to average with, only itself."""
    import zarr

    img = np.full((90, 5000, 3), 200, np.uint8)
    path = write_svs(tmp_path / "strip.svs", img, tile=240)
    out = tmp_path / "strip.ome.zarr"
    convert(path, out, tile=64, progress=False)

    root = zarr.open_group(str(out), mode="r")
    for level in root.array_keys():
        arr = np.asarray(root[level][:])
        assert arr.min() == 200 and arr.max() == 200, f"level {level} decayed"


def test_declining_the_size_question_builds_nothing(slide, tmp_path, monkeypatch):
    from nd2wsi import convert as convert_mod
    from nd2wsi.server import ConversionDeclined, SlideRegistry

    path, _ = slide
    store = convert_mod.default_store_path(path)
    asked = {}

    registry = SlideRegistry()
    registry.confirm_convert = lambda p, est: asked.setdefault("est", est) and False

    with pytest.raises(ConversionDeclined):
        registry.open_path(path)
    assert not store.exists()
    assert asked["est"]["bytes"] > 0

    registry.confirm_convert = lambda p, est: True
    sid = registry.open_path(path)
    assert sid and store.exists()


def test_trash_reports_progress_and_saves_annotations(slide, tmp_path):
    from nd2wsi.convert import convert, default_store_path
    from nd2wsi.server import SlideRegistry

    path, _ = slide
    store = default_store_path(path)
    convert(path, store, tile=128, progress=False)  # many small chunks
    (store / "annotations_slide.json").write_text('{"items": [1]}')

    registry = SlideRegistry()
    sid = registry.add_store(store)
    seen = []
    freed = registry.trash_cache(sid, on_progress=seen.append)

    assert freed > 0
    assert not store.exists()
    assert seen == sorted(seen) and seen[-1] == 1.0
    assert len(seen) > 10  # the bar actually moves
    assert (path.parent / "annotations_slide.json").read_text() == '{"items": [1]}'
