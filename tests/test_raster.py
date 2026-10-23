"""Raster opening keeps the TXT pixel grid and annotation ownership."""
import json

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
