"""Raster opening keeps the TXT pixel grid and annotation ownership."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PIL import Image

from nd2wsi import raster
from nd2wsi.annotation_sidecars import annotations_sidecar
from nd2wsi.server import SlideRegistry, make_handler
from nd2wsi.window_sessions import create_window_session


def save_image(tmp_path, *, name="sample.png", size=(67, 43), mode="RGB", **save_args):
    path = tmp_path / name
    pixels = np.zeros((size[1], size[0], 3), np.uint8)
    pixels[..., 0] = np.arange(size[0], dtype=np.uint8)
    pixels[..., 1] = np.arange(size[1], dtype=np.uint8)[:, None]
    pixels[..., 2] = 170
    Image.fromarray(pixels).convert(mode).save(path, **save_args)
    return path


@pytest.mark.parametrize("name", ["sample.png", "sample.jpg", "sample.JPEG"])
def test_raster_grid_channels_and_dpi_are_not_physical_calibration(tmp_path, name):
    path = save_image(tmp_path, name=name, dpi=(300, 300))
    root, attrs = raster.open_direct(path)
    try:
        meta = attrs["nd2wsi"]
        assert meta["raster"] and meta["direct"] and meta["rgb"]
        assert meta["pixel_size_um"] is None
        assert meta["calibration"] == {"status": "unknown", "source": "unknown"}
        assert root["0"].shape == (3, 43, 67)
        with Image.open(path) as expected:
            expected = np.moveaxis(np.asarray(expected.convert("RGB")), -1, 0)
        np.testing.assert_array_equal(root["0"][[2, 0], 7:17, 9:29], expected[[2, 0], 7:17, 9:29])
        assert all("unit" not in axis for axis in attrs["multiscales"][0]["axes"])
    finally:
        root.close(delay=0)
    with pytest.raises(ValueError, match="closed"):
        root["0"][:, 0:1, 0:1]


def test_exif_rotation_does_not_change_astromapper_pixel_axes(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    path = save_image(tmp_path, name="rotated.jpg", exif=exif)
    root, attrs = raster.open_direct(path)
    try:
        assert root["0"].shape == (3, 43, 67)
        assert any("orientation 6 is ignored" in note for note in attrs["nd2wsi"]["notes"])
        with Image.open(path) as source:
            np.testing.assert_array_equal(root["0"][:, :, :], np.moveaxis(np.asarray(source), -1, 0))
    finally:
        root.close(delay=0)


def test_odd_pyramid_dimensions_and_reduced_windows_stay_inside_source(tmp_path):
    path = save_image(tmp_path, size=(1027, 771))
    root, attrs = raster.open_direct(path)
    try:
        assert [level.shape for level in root.values()] == [(3, 771, 1027), (3, 385, 513), (3, 192, 256)]
        with Image.open(path) as image:
            expected = np.moveaxis(np.asarray(image.resize((13, 11), Image.Resampling.BOX,
                                                box=(8 * 4, 7 * 4, 21 * 4, 18 * 4))), -1, 0)
        np.testing.assert_array_equal(root["2"][:, 7:18, 8:21], expected)
        assert root["2"][:, -1:, -1:].shape == (3, 1, 1)
        assert root["1"][:, 0:0, 0:1].shape == (3, 0, 1)
    finally:
        root.close(delay=0)


def test_narrow_image_pyramid_reads_do_not_extend_past_collapsed_axis(tmp_path):
    path = save_image(tmp_path, size=(1, 1025))
    root, _ = raster.open_direct(path)
    try:
        assert root["1"].shape == (3, 512, 1)
        assert root["1"][:, 0:512, :].shape == (3, 512, 1)
    finally:
        root.close(delay=0)


def save_noise(tmp_path, size, *, name="noise.png"):
    """Uncorrelated pixels, so any BOX rounding difference shows."""
    path = tmp_path / name
    pixels = np.random.default_rng(7).integers(0, 256, (size[1], size[0], 3), dtype=np.uint8)
    Image.fromarray(pixels).save(path, compress_level=1)
    return path


def box_window(image, d, y0, y1, x0, x1):
    """What a per-tile read has always returned: one BOX resample of the window."""
    window = image.resize((x1 - x0, y1 - y0), Image.Resampling.BOX, box=(x0 * d, y0 * d, x1 * d, y1 * d))
    return np.moveaxis(np.asarray(window), -1, 0)


def test_overview_levels_are_memoized_with_per_tile_pixels(tmp_path, monkeypatch):
    path = save_noise(tmp_path, (8300, 260))
    # Several bands per level, the last one partial.
    monkeypatch.setattr(raster, "_MEMO_BAND_ROWS", 5)
    root, _ = raster.open_direct(path)
    try:
        assert [root[k].shape for k in ("3", "4", "5")] == [(3, 32, 1037), (3, 16, 518), (3, 8, 259)]
        with Image.open(path) as image:
            image = image.convert("RGB")
            expected = {k: box_window(image, 2 ** int(k), 0, root[k].shape[1], 0, root[k].shape[2])
                        for k in ("3", "4", "5")}
            for k in ("3", "4", "5"):
                d, (_, h, w) = 2 ** int(k), root[k].shape
                windows = [(0, h, x, min(w, x + 512)) for x in range(0, w, 512)]
                windows += [(3, h - 1, 100, min(w, 357)), (h - 1, h, w - 1, w)]
                for y0, y1, x0, x1 in windows:
                    tile = box_window(image, d, y0, y1, x0, x1)
                    np.testing.assert_array_equal(root[k][:, y0:y1, x0:x1], tile)
                    np.testing.assert_array_equal(expected[k][:, y0:y1, x0:x1], tile)
        monkeypatch.setattr(Image.Image, "resize", lambda *a, **k: pytest.fail("resampled a memoized level"))
        for k in ("3", "4", "5"):
            np.testing.assert_array_equal(root[k][:, :, :], expected[k])
            np.testing.assert_array_equal(root[k][[2, 0], 1:4, 5:9], expected[k][[2, 0], 1:4, 5:9])
            np.testing.assert_array_equal(root[k][1, 1:4, 5:9], expected[k][1, 1:4, 5:9])
        # Reads are private copies: editing one never reaches the memo.
        tile = root["3"][:, 0:4, 0:4]
        assert tile.flags.c_contiguous and tile.flags.writeable
        tile[...] = 0
        np.testing.assert_array_equal(root["3"][:, 0:4, 0:4], expected["3"][:, 0:4, 0:4])
        assert not np.shares_memory(root["3"][:, :, :], root["3"]._memo)
    finally:
        root.close(delay=0)
    assert all(level._memo is None for level in root.values())


def test_overview_level_is_built_once_under_concurrent_first_reads(tmp_path, monkeypatch):
    path = save_noise(tmp_path, (8300, 260))
    monkeypatch.setattr(raster, "_MEMO_BAND_ROWS", 5)
    sizes, real_resize = [], Image.Image.resize

    def resize(self, size, *args, **kwargs):
        sizes.append(size)
        time.sleep(0.01)  # keep the build open while the other readers arrive
        return real_resize(self, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", resize)
    root, _ = raster.open_direct(path)
    try:
        with ThreadPoolExecutor(8) as pool:
            tiles = list(pool.map(lambda x0: root["3"][:, :, x0:x0 + 100], range(0, 800, 100)))
        assert sizes == [(1037, 5)] * 6 + [(1037, 2)]
        np.testing.assert_array_equal(np.concatenate(tiles, axis=2), root["3"][:, :, 0:800])
    finally:
        root.close(delay=0)


def test_close_waits_for_an_in_flight_memo_build_then_frees_it(tmp_path, monkeypatch):
    path = save_noise(tmp_path, (2100, 600))
    with Image.open(path) as image:
        expected = box_window(image.convert("RGB"), 8, 0, 10, 0, 10)
    entered, release, real_resize = threading.Event(), threading.Event(), Image.Image.resize

    def resize(self, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real_resize(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", resize)
    root, _ = raster.open_direct(path)
    level = root["3"]
    assert level.shape == (3, 75, 262)
    with ThreadPoolExecutor(2) as pool:
        read = pool.submit(lambda: level[:, 0:10, 0:10])
        assert entered.wait(10)
        closing = pool.submit(root.close, 0)
        deadline = time.monotonic() + 10
        while not level._life.closed and time.monotonic() < deadline:
            time.sleep(0.001)
        assert level._life.closed and not closing.done()
        release.set()
        # The build finished on a live base image, and close waited for it.
        np.testing.assert_array_equal(read.result(10), expected)
        closing.result(10)
    assert all(level._memo is None for level in root.values())
    with pytest.raises(ValueError, match="closed"):
        level[:, 0:1, 0:1]


def test_reads_overlap_up_to_the_slot_limit(tmp_path, monkeypatch):
    path = save_image(tmp_path, size=(1027, 771))
    slots = raster.MAX_CONCURRENT_READS
    # Every slot must be busy at once, or the barrier times out.
    gate, counter = threading.Barrier(slots, timeout=10), threading.Lock()
    active = peak = 0
    real_crop = Image.Image.crop

    def crop(self, box=None):
        nonlocal active, peak
        with counter:
            active += 1
            peak = max(peak, active)
        try:
            gate.wait()
            return real_crop(self, box)
        finally:
            with counter:
                active -= 1

    monkeypatch.setattr(Image.Image, "crop", crop)
    root, _ = raster.open_direct(path)
    try:
        with ThreadPoolExecutor(2 * slots) as pool:
            tiles = list(pool.map(lambda y: root["0"][:, y:y + 8, 0:8], range(0, 16 * slots, 8)))
        assert peak == slots
        with Image.open(path) as image:
            expected = np.moveaxis(np.asarray(image), -1, 0)
        np.testing.assert_array_equal(np.concatenate(tiles, axis=1), expected[:, 0:16 * slots, 0:8])
    finally:
        root.close(delay=0)


def test_concurrent_reads_on_every_level_match_serial_reads(tmp_path):
    path = save_noise(tmp_path, (2100, 600))
    rng = np.random.default_rng(3)

    def read(root, r):
        k, channels, y0, y1, x0, x1 = r
        return root[k][channels, y0:y1, x0:x1]

    root, _ = raster.open_direct(path)
    try:
        assert [root[k].shape[1:] for k in sorted(root)] == [(600, 2100), (300, 1050), (150, 525), (75, 262)]
        reads = []
        for i in range(160):
            k = str(rng.integers(0, len(root)))
            _, h, w = root[k].shape
            y0, x0 = int(rng.integers(0, h)), int(rng.integers(0, w))
            y1, x1 = int(rng.integers(y0 + 1, min(h, y0 + 300) + 1)), int(rng.integers(x0 + 1, min(w, x0 + 300) + 1))
            reads.append((k, [slice(None), 1, [2, 0]][i % 3], y0, y1, x0, x1))
        expected = [read(root, r) for r in reads]
    finally:
        root.close(delay=0)
    # A fresh root, so the memo levels are built while other reads race them.
    fresh, _ = raster.open_direct(path)
    try:
        with ThreadPoolExecutor(8) as pool:
            got = list(pool.map(lambda r: read(fresh, r), reads * 3))
    finally:
        fresh.close(delay=0)
    for tile, want in zip(got, expected * 3, strict=True):
        np.testing.assert_array_equal(tile, want)


def test_alpha_is_composited_on_black_and_grayscale_becomes_rgb(tmp_path):
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (4, 3), (200, 100, 50, 128)).save(path)
    root, _ = raster.open_direct(path)
    try:
        assert root["0"][:, 0:1, 0:1].ravel().tolist() == [100, 50, 25]
    finally:
        root.close(delay=0)
    Image.new("L", (4, 3), 84).save(path)
    root, _ = raster.open_direct(path)
    try:
        assert root["0"][:, 0:1, 0:1].ravel().tolist() == [84, 84, 84]
    finally:
        root.close(delay=0)


def test_16_bit_grayscale_png_keeps_its_values_through_probe_and_tiff_export(tmp_path):
    # A 12-bit camera image saved as 16-bit PNG used to be clipped to 255.
    import io
    import sys
    import urllib.request
    from pathlib import Path

    tifffile = pytest.importorskip("tifffile")
    sys.path.insert(0, str(Path(__file__).parent))
    from http_fixture import running_server

    from nd2wsi.server import server_url

    pixels = (np.arange(700 * 600).reshape(700, 600) * 7 % 4096).astype(np.uint16)
    path = tmp_path / "camera.png"
    Image.fromarray(pixels).save(path)
    root, attrs = raster.open_direct(path)
    try:
        assert root["0"].shape == (1, 700, 600) and root["0"].dtype == np.uint16
        assert np.array_equal(root["0"][:, :, :][0], pixels)
        assert root["1"][:, 0:1, 0:1].item() == round(pixels[:2, :2].mean())
    finally:
        root.close(delay=0)

    with running_server(path, host="127.0.0.1") as httpd:
        base = f"{server_url(httpd).rstrip('/')}/s/{httpd.registry.default_sid()}"

        def get(route):
            return urllib.request.urlopen(base + route, timeout=30).read()

        info = json.loads(get("/api/info"))
        assert (info["rgb"], info["dtype"], len(info["channels"])) == (False, "uint16", 1)
        assert info["channels"][0]["window"]["max"] == float(pixels.max())
        probe = json.loads(get("/api/pixel?x=41&y=33"))
        assert probe["values"] == [{"name": "Gray", "value": int(pixels[33, 41])}]
        assert Image.open(io.BytesIO(get("/api/tile/0/0/0.jpg?c=0"))).size == (512, 512)
        exported = tifffile.imread(io.BytesIO(get("/api/roi?level=0&x=0&y=0&w=600&h=700&format=tiff")))
        assert exported.dtype == np.uint16
        assert np.array_equal(exported.reshape(pixels.shape), pixels)


def test_pixel_limit_and_memory_preflight_happen_before_full_decode(tmp_path, monkeypatch):
    path = save_image(tmp_path)
    monkeypatch.setattr(Image.Image, "load", lambda _: pytest.fail("decoded before preflight"))
    monkeypatch.setattr(raster, "MAX_PIXELS", 10)
    with pytest.raises(ValueError, match="PNG input exceeds"):
        raster.open_direct(path)
    monkeypatch.setattr(raster, "MAX_PIXELS", 64_000_000)
    monkeypatch.setattr("nd2wsi.convert.available_memory_bytes", lambda: None)
    monkeypatch.setattr("nd2wsi.render._available_memory", lambda: 1)
    with pytest.raises(ValueError, match="Not enough memory"):
        raster.open_direct(path)


def test_jpeg_uses_local_pixel_limit_without_changing_pillow_global(tmp_path, monkeypatch):
    path = save_image(tmp_path, name="sample.jpg")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 500)
    with pytest.raises(Image.DecompressionBombError):
        Image.open(path)
    root, _ = raster.open_direct(path)
    try:
        assert root["0"].shape == (3, 43, 67)
        assert root["0"][:, 7:17, 9:29].shape == (3, 10, 20)
        assert Image.MAX_IMAGE_PIXELS == 500
        # Other decoders retain Pillow's ordinary global protection.
        png = save_image(tmp_path)
        with pytest.raises(Image.DecompressionBombError):
            raster.open_direct(png)
    finally:
        root.close(delay=0)


def jpeg_with_large_header(tmp_path, width, height):
    """Exercise preflight limits without allocating a large test image."""
    import struct

    path = save_image(tmp_path, name="large.jpg")
    content = bytearray(path.read_bytes())
    frame = content.index(b"\xff\xc0")
    content[frame + 5:frame + 9] = struct.pack(">HH", height, width)
    path.write_bytes(content)
    return path


def test_large_jpeg_pixel_limit_is_checked_before_decode(tmp_path, monkeypatch):
    from PIL import JpegImagePlugin

    path = jpeg_with_large_header(tmp_path, 25000, 22000)
    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", lambda _: pytest.fail("decoded before preflight"))
    with pytest.raises(ValueError, match="JPEG input exceeds 512 megapixels"):
        raster.open_direct(path)


@pytest.mark.parametrize("available", [16 << 30, 1 << 30])
def test_large_jpeg_uses_current_available_memory_before_decode(tmp_path, monkeypatch, available):
    from PIL import JpegImagePlugin

    path = jpeg_with_large_header(tmp_path, 24000, 16000)
    monkeypatch.setattr("nd2wsi.convert.available_memory_bytes", lambda: available)
    # The old macOS-only probe's Windows fallback must not cap a capable PC.
    monkeypatch.setattr("nd2wsi.render._available_memory", lambda: pytest.fail("used fallback instead of available RAM"))

    class DecoderReached(Exception):
        pass

    def decode(_):
        raise DecoderReached

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", decode)
    if available == 16 << 30:
        with pytest.raises(DecoderReached):
            raster.open_direct(path)
    else:
        with pytest.raises(ValueError, match="Not enough memory"):
            raster.open_direct(path)


def test_agent_raster_opens_without_shared_cache_or_sidecar_writes(tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.mkdir()
    path = save_image(source)
    # Unscoped legacy coordinates might refer to a full-resolution ND2.
    legacy = source / "sample.annotations.json"
    legacy.write_text(json.dumps({"items": [{"x": 99999, "y": 88888}]}))
    session = create_window_session("agent", tmp_path / "sessions")
    registry = SlideRegistry(window_session=session)
    monkeypatch.setattr("nd2wsi.convert.ensure_cache", lambda *a, **k: pytest.fail("built shared cache"))
    try:
        sid = registry.open_path(path)
        state = registry.get(sid)
        assert state.source_path == path and state.cache_path is None
        assert state.annotations_path.is_relative_to(session.annotation_root)
        assert json.loads(state.annotations_path.read_text()) == {"items": []}
        assert sorted(item.name for item in source.iterdir()) == ["sample.annotations.json", "sample.png"]
        handler_type = make_handler(registry)
        handler = object.__new__(handler_type)
        info = handler._info_payload(state)
        assert info["raster"] and not info["calibrated"] and not info["nd2Export"]
        assert (info["width"], info["height"]) == (67, 43)
        assert registry.add_store(path) == sid
    finally:
        for state in registry.slides.values():
            state.root.close(delay=0)
        session.mark_closed()


def test_raster_sidecar_is_distinct_from_same_stem_nd2_and_never_claims_legacy(tmp_path):
    path = save_image(tmp_path)
    (tmp_path / "sample.nd2").write_bytes(b"original source placeholder")
    legacy = tmp_path / "sample.annotations.json"
    legacy.write_text('{"items": [{"x": 50000}]}')
    root, attrs = raster.open_direct(path)
    try:
        jpg_sidecar = annotations_sidecar(path, attrs, migrate=False)
        nd2_attrs = {"nd2wsi": {"source": "sample.nd2"}}
        nd2_sidecar = annotations_sidecar(tmp_path / "sample.nd2", nd2_attrs, migrate=False)
        assert jpg_sidecar != nd2_sidecar
        assert not jpg_sidecar.exists() and not nd2_sidecar.exists()
        assert legacy.exists()
        assert not (tmp_path / ".nd2wsi").exists()
    finally:
        root.close(delay=0)


def test_app_accepts_raster_without_converting(tmp_path, monkeypatch):
    from nd2wsi import app

    path = save_image(tmp_path)
    monkeypatch.setattr("nd2wsi.convert.ensure_cache", lambda *a, **k: pytest.fail("built raster cache"))
    assert app.open_or_convert(path) == path
    api = app.Api(None)
    monkeypatch.setattr(api, "_launch_many", lambda paths: paths)
    assert api.open_paths([str(path)]) == [path]
    assert all(extension in app.OPEN_FILE_TYPES[0] for extension in ("*.jpg", "*.jpeg", "*.png"))


@pytest.mark.parametrize("name", ["sample.jpg", "sample.JPEG", "sample.png"])
def test_cli_views_raster_directly_without_nd2_inspection_or_conversion(tmp_path, monkeypatch, name):
    from nd2wsi import cli

    path = save_image(tmp_path, name=name)
    served = []
    monkeypatch.setattr("nd2wsi.convert.ensure_cache", lambda *a, **k: pytest.fail("built raster cache"))
    monkeypatch.setattr("nd2wsi.plate.is_plate_file", lambda *a, **k: pytest.fail("inspected raster as ND2"))
    monkeypatch.setattr("nd2wsi.server.serve", lambda paths, **kwargs: served.append(paths))
    assert cli.main(["view", str(path)]) == 0
    assert served == [[path]]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("arguments", [
    ["--store", "out.zarr"], ["--overwrite"], ["--t", "0"], ["--z=mid"],
    ["--position", "0"], ["--tile", "512"], ["--workers", "1"], ["--yes"],
])
def test_cli_rejects_raster_conversion_options_before_opening_any_slide(monkeypatch, capsys, arguments):
    from nd2wsi import cli

    monkeypatch.setattr("nd2wsi.convert.ensure_cache", lambda *a, **k: pytest.fail("opened earlier ND2"))
    monkeypatch.setattr("nd2wsi.server.serve", lambda *a, **k: pytest.fail("served invalid request"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["view", "earlier.nd2", "sample.jpg", *arguments])
    assert caught.value.code == 2
    assert "JPEG/PNG supports direct viewing only" in capsys.readouterr().err
