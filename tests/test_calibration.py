"""A file without a pixel size stays uncalibrated everywhere.

The old behavior silently invented 1.0 um/px and let it flow into the
scale bar, measurements, TIFF resolution tags and exported ND2
calibration — plausible numbers with no basis. These tests pin the new
contract: unknown in, unknown out, on every path.
"""

import io
import json

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")

from nd2wsi.convert import convert, default_store_path, open_store  # noqa: E402
from nd2wsi.server import SlideRegistry, server_url  # noqa: E402


@pytest.fixture()
def uncalibrated_svs(tmp_path):
    """A tiled SVS whose description carries no MPP at all."""
    rng = np.random.default_rng(6)
    img = rng.integers(0, 255, (1300, 1717, 3), dtype=np.uint8)
    p = tmp_path / "nompp.svs"
    with tifffile.TiffWriter(p) as tw:
        opts = dict(tile=(240, 240), photometric="rgb", compression="jpeg2000",
                    compressionargs={"reversible": True})
        tw.write(img, subifds=1, description="Aperio Image|AppMag = 20", **opts)
        small = (img[:1300, :1716].reshape(325, 4, 429, 4, 3)
                 .mean(axis=(1, 3)).astype(np.uint8))
        tw.write(small, subfiletype=1, **opts)
    return p, img


def test_store_attrs_do_not_invent_a_micrometer(uncalibrated_svs, tmp_path):
    path, _ = uncalibrated_svs
    out = tmp_path / "s.ome.zarr"
    convert(path, out, progress=False)
    _, attrs = open_store(out)

    meta = attrs["nd2wsi"]
    assert meta["pixel_size_um"] is None
    assert meta["calibration"] == {"status": "unknown", "source": "unknown"}
    axes = attrs["multiscales"][0]["axes"]
    assert all("unit" not in a for a in axes if a["name"] in ("y", "x"))
    assert any("measurements are in pixels" in n for n in meta["notes"])


def test_direct_svs_reports_uncalibrated(uncalibrated_svs):
    path, _ = uncalibrated_svs
    registry = SlideRegistry()
    sid = registry.open_path(path)
    meta = registry.slides[sid].attrs["nd2wsi"]
    assert meta["pixel_size_um"] is None
    assert meta["calibration"]["status"] == "unknown"
    registry.remove(sid)


def test_tiff_export_carries_no_resolution_tags(uncalibrated_svs, tmp_path):
    from nd2wsi.render import export_roi_tiff

    path, img = uncalibrated_svs
    store = default_store_path(path)
    convert(path, store, progress=False)
    root, attrs = open_store(store)

    buf = io.BytesIO()
    export_roi_tiff(root, attrs, buf, level=0, x=100, y=100, w=400, h=300,
                    channels=[0, 1, 2])
    buf.seek(0)
    with tifffile.TiffFile(buf) as tf:
        tags = tf.pages[0].tags
        # tifffile always materializes the tags; honesty means unit NONE
        # and the 1/1 placeholder rather than a rate built from a made-up
        # micrometer value
        assert tags["ResolutionUnit"].value == 1  # RESUNIT.NONE
        assert tuple(tags["XResolution"].value) == (1, 1)
        got = tf.pages[0].asarray()
    assert (got == img[100:400, 100:500]).all()


def test_calibrated_svs_still_carries_units(tmp_path):
    """The guard must not strip real calibration."""
    rng = np.random.default_rng(7)
    img = rng.integers(0, 255, (700, 900, 3), dtype=np.uint8)
    p = tmp_path / "cal.svs"
    tifffile.imwrite(p, img, tile=(240, 240), photometric="rgb",
                     compression="jpeg2000", compressionargs={"reversible": True},
                     description="Aperio Image|MPP = 0.4990|AppMag = 20")
    out = tmp_path / "cal.ome.zarr"
    convert(p, out, progress=False)
    _, attrs = open_store(out)
    meta = attrs["nd2wsi"]
    assert meta["pixel_size_um"] == [0.499, 0.499]
    assert meta["calibration"] == {"status": "calibrated", "source": "aperio-mpp"}
    assert attrs["multiscales"][0]["axes"][1]["unit"] == "micrometer"


def test_nd2_export_omits_fabricated_calibration(tmp_path):
    pytest.importorskip("limnd2")
    nd2 = pytest.importorskip("nd2")

    from nd2wsi.export_nd2 import write_nd2

    img = np.random.default_rng(8).integers(0, 4000, (300, 400, 2), np.uint16)
    out = tmp_path / "uncal.nd2"
    write_nd2(
        out,
        lambda x, y, w, h: img[y : y + h, x : x + w],
        height=300,
        width=400,
        dtype=np.dtype(np.uint16),
        pixel_size_um=None,
        planes=[{"name": "A", "color": "FF0000"}, {"name": "B", "color": "0000FF"}],
    )
    with nd2.ND2File(str(out)) as f:
        # the reader substitutes 1.0 for missing calibration; the per-axis
        # flags carry the truth and must say uncalibrated
        flags = f.metadata.channels[0].volume.axesCalibrated
        assert not flags[0] and not flags[1]
        frame = np.asarray(f.read_frame(0))
        if frame.shape[-1] == 2:  # (Y, X, C) variant
            frame = np.moveaxis(frame, -1, 0)
        assert (frame[0] == img[..., 0]).all()


def test_info_payload_flags_calibration(uncalibrated_svs):
    import threading
    import urllib.request

    from nd2wsi.server import create_server

    path, _ = uncalibrated_svs
    httpd = create_server(path, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = server_url(httpd).rstrip("/")
        info = json.loads(urllib.request.urlopen(base + "/api/info", timeout=30).read())
        assert info["calibrated"] is False
        assert info["pixelSizeUm"] is None
    finally:
        httpd.shutdown()


def test_anisotropic_pixels_export_uncalibrated():
    from nd2wsi.export_nd2 import _scalar_calibration

    assert _scalar_calibration(None) is None
    assert _scalar_calibration((0.66, 0.66)) == 0.66
    with pytest.warns(UserWarning, match="anisotropic"):
        assert _scalar_calibration((0.5, 0.7)) is None
