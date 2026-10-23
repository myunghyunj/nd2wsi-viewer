"""Release checks executed inside the shipped application, without pytest."""

from __future__ import annotations

import hashlib
import io
import json
import math
import sys
import tempfile
import threading
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _served(path):
    from .server import create_server, server_url

    httpd = create_server(path, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, server_url(httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def _fetch(url):
    with urllib.request.urlopen(url, timeout=90) as response:
        return response.read()


def _verify_sdpc_runtime_license(av) -> None:
    """Read the delivered libraries' own metadata, not a pre-signing hash claim."""
    # Keep these restrictions aligned with packaging/verify_sdpc_codec.py.
    required = {
        "--disable-autodetect", "--disable-everything", "--disable-gpl", "--disable-nonfree",
        "--disable-version3", "--disable-static", "--enable-shared", "--disable-network",
        "--enable-decoder=hevc", "--enable-parser=hevc", "--enable-demuxer=hevc",
    }
    forbidden = ("--enable-gpl", "--enable-nonfree", "--enable-version3", "--enable-lib")
    metadata = av._core.library_meta
    assert av.__version__ == "16.0.1", "unexpected packaged PyAV version"
    assert len(metadata) == 7, "expected seven packaged FFmpeg shared libraries"
    for name, item in metadata.items():
        assert item["license"] == "LGPL version 2.1 or later", f"unapproved {name} runtime license"
        configuration = item["configuration"]
        assert all(flag in configuration for flag in required), f"missing {name} runtime restrictions"
        assert not any(flag in configuration for flag in forbidden), f"unapproved {name} runtime configuration"
    assert "libx264" not in av.codecs_available and "libx265" not in av.codecs_available, (
        "external encoders appeared in the packaged decoder-only runtime"
    )


def _smoke_sdpc(work: Path) -> None:
    """Mandatory 2.3 release check: use frozen synthetic bytes, never encode HEVC."""
    import av
    import numpy as np
    import tifffile
    from PIL import Image

    from . import _sdpc_smoke_fixture as fixture
    from .app import open_or_convert
    from .sdpc import sdpc_info

    decoder = av.Codec("hevc", "r")
    assert decoder.is_decoder and decoder.type == "video", "bundled HEVC decoder missing"
    if getattr(sys, "frozen", False):
        _verify_sdpc_runtime_license(av)
        print("smoke ok: SDPC LGPL runtime, seven restricted shared libraries; no external encoders")
    encoded = fixture.add_associated_images(fixture.load_sdpc())
    slide = work / "synthetic-sdpc-smoke.sdpc"
    slide.write_bytes(encoded)
    assert open_or_convert(slide) == slide, "SDPC smoke unexpectedly requested conversion"
    x, y, width, height = fixture.ROI
    with _served(slide) as (httpd, url):
        state = httpd.registry.get(None)
        assert state.store_path is None, "SDPC must remain source-backed"
        assert state.attrs["nd2wsi"]["source_format"] == "sdpc"
        actual = np.moveaxis(np.asarray(state.root["0"][:]), 0, -1)
        assert actual.shape == (fixture.HEIGHT, fixture.WIDTH, 3)
        assert hashlib.sha256(actual.tobytes()).hexdigest() == fixture.RGB_SHA256, (
            "SDPC native RGB differs from the independent decoded reference"
        )
        info = json.loads(_fetch(url + "api/inspect"))
        assert info["storage"] == "direct" and info["rgb"] is True
        assert info["calibration"]["pixel_size_um"] == [fixture.MPP, fixture.MPP]
        assert info["objective_magnification"] == 40
        assert info["associated"] == ["thumbnail", "associated-1", "associated-2"]
        for name, size in (("thumbnail", (48, 32)), ("associated-1", (96, 48)),
                           ("associated-2", (80, 120))):
            with Image.open(io.BytesIO(_fetch(url + f"api/associated/{name}.jpg?detail=1"))) as image:
                image.load()
                assert image.mode == "RGB" and image.size == size, (
                    "SDPC Info did not serve the original embedded image"
                )
        with Image.open(io.BytesIO(_fetch(url + "api/tile/0/0/0.jpg"))) as image:
            image.load()
            assert image.mode == "RGB" and image.size == (fixture.WIDTH, fixture.HEIGHT)
        pixel = json.loads(_fetch(
            url + f"api/pixel?x={fixture.TILE_WIDTH}&y={fixture.TILE_HEIGHT}"
        ))
        assert [channel["value"] for channel in pixel["values"]] == actual[
            fixture.TILE_HEIGHT, fixture.TILE_WIDTH
        ].tolist()
        roi = _fetch(url + f"api/roi?level=0&x={x}&y={y}&w={width}&h={height}&format=tiff")
        with tifffile.TiffFile(io.BytesIO(roi)) as exported:
            pixels = exported.asarray()
            assert np.array_equal(pixels, actual[y:y+height, x:x+width]), (
                "SDPC seam-crossing TIFF export changed decoded RGB"
            )
            assert hashlib.sha256(pixels.tobytes()).hexdigest() == fixture.ROI_RGB_SHA256
            tags = exported.pages[0].tags
            assert tags["ResolutionUnit"].value == 3, "SDPC TIFF must use centimeters"
            for axis in ("XResolution", "YResolution"):
                numerator, denominator = tags[axis].value
                assert math.isclose(10000 * denominator / numerator, fixture.MPP,
                                    rel_tol=1e-7), "SDPC TIFF calibration changed"
    corrupt = work / "synthetic-sdpc-truncated.sdpc"
    corrupt.write_bytes(encoded[:-1])
    try:
        sdpc_info(corrupt)
    except ValueError:
        pass
    else:
        raise AssertionError("SDPC truncated source was accepted")
    print(
        f"smoke ok: SDPC HEVC (PyAV {av.__version__}), native RGB reference exact; "
        "tile/pixel and Info image APIs, seam-crossing TIFF RGB and calibration passed; "
        "truncation rejected"
    )


def _smoke_astromap(work: Path) -> None:
    """Require the delivered raster decoder, PDF renderer and stream writer."""
    from PIL import Image
    from pypdf import PdfReader

    from .astromap_export import HEADER, prepare_astromap
    from .server import SlideRegistry

    image_path = work / "astromap-source.jpg"
    with Image.new("RGB", (317, 151), (40, 100, 170)) as image:
        image.save(image_path, "JPEG")
    source_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
    registry = SlideRegistry()
    try:
        sid = registry.open_path(image_path)
        state = registry.get(sid)
        assert state.attrs["nd2wsi"]["raster"] is True
        assert state.attrs["nd2wsi"]["pixel_size_um"] is None
        assert state.cache_path is None, "JPEG input unexpectedly created a pyramid cache"
        payload = {"generation": state.generation, "channels": [0, 1, 2],
                   "rois": [{"x": 11, "y": 13, "w": 21, "h": 17, "note": "검증 ROI"}]}
        ordinary = {}
        for include_pdf in (False, True):
            output = work / ("astromap-pdf" if include_pdf else "astromap-ordinary")
            output.mkdir()
            plan = prepare_astromap(state, {**payload, "include_pdf": include_pdf},
                                    resolve_frame=None)
            with zipfile.ZipFile(plan.write(output)) as bundle:
                names = bundle.namelist()
                assert len(names) == (3 if include_pdf else 2), "Astromap ZIP members differ"
                jpg = next(name for name in names if name.endswith(".jpg"))
                txt = next(name for name in names if name.endswith(".txt"))
                assert bundle.read(txt).decode("utf-8") == HEADER + "1\t11\t13\t31\t29\t검증 ROI\n"
                with Image.open(io.BytesIO(bundle.read(jpg))) as image:
                    image.load()
                    assert image.size == (317, 151) and image.mode == "RGB"
                if include_pdf:
                    assert all(bundle.read(name) == data for name, data in ordinary.items())
                    pdf = PdfReader(io.BytesIO(bundle.read(next(name for name in names if name.endswith(".pdf")))))
                    assert len(pdf.pages) == 1
                    assert float(pdf.pages[0].mediabox.width) > float(pdf.pages[0].mediabox.height)
                    assert "ROI" in pdf.pages[0].extract_text(), "PDF labels could not be read"
                else:
                    ordinary = {jpg: bundle.read(jpg), txt: bundle.read(txt)}
        assert hashlib.sha256(image_path.read_bytes()).hexdigest() == source_sha
    finally:
        registry.close_all(immediate=True)
    print("smoke ok: Astromap JPEG, TXT and PDF; pixel coordinates and source preserved")


def run(nd2_path: Path) -> int:
    import nd2
    import numpy as np
    import tifffile
    from PIL import Image

    from .app import open_or_convert
    from .reader import PlaneSelection, open_plane

    try:
        with tempfile.TemporaryDirectory(prefix="nd2wsi-smoke-") as temp:
            work = Path(temp)
            _smoke_sdpc(work)
            _smoke_astromap(work)
            # These compiled SciPy modules and the frozen settings must be in
            # the delivered bundle, not merely present in a developer venv.
            from . import h_adaptive, h_model

            config = json.loads(Path(h_model.__file__).with_name("h_config.json").read_text())
            assert config["schema"] == "H_auto_refit/1"
            criteria = h_adaptive.validated_acceptance_criteria(config["h_adaptive"])
            assert h_adaptive.ADAPTIVE_SCHEMA == h_model.Model.ADAPTIVE_SCHEMA
            assert criteria["gain_and_engineering_invariants"] == {
                "minimum_valid_gain": 0.25, "maximum_valid_gain": 1.75,
            }
            phase = np.array([[[0.25, 0.75]], [[0.5, 0.5]]])
            sampled = h_model.sample_template(np.ones((4, 4)), phase)
            assert np.array_equal(sampled, np.ones((1, 2)))
            envelope = h_model.smooth_node_envelope_at(np.ones((3, 3)), (0, 0), phase)
            assert np.array_equal(envelope, np.ones((1, 2)))
            optimum = h_model.minimize(lambda x: float(np.sum((x - 2) ** 2)),
                                       [0.0, 0.0], method="Powell")
            assert optimum.success and np.allclose(optimum.x, [2, 2])
            print("smoke ok: legacy/adaptive Shading Correction settings, interpolation and optimizer")
            store = open_or_convert(nd2_path, on_status=print)
            with _served(store) as (_httpd, url):
                info = json.loads(_fetch(url + "api/info"))
                tile = _fetch(url + "api/tile/0/0/0.jpg")
                with Image.open(io.BytesIO(tile)) as image:
                    image.load()
                    assert min(image.size) > 0
                if len(info["levels"]) > 1:
                    reduced = info["levels"][1]
                    tiff = _fetch(url + "api/roi?level=" + str(reduced["path"]) +
                                  f"&x=0&y=0&w={reduced['width']}&h={reduced['height']}&format=tiff")
                    with tifffile.TiffFile(io.BytesIO(tiff)) as exported:
                        page = exported.pages[0]
                        assert (page.imagewidth, page.imagelength) == (reduced["width"], reduced["height"])
                    print("smoke ok: whole-slide reduced TIFF export")
                roi = work / "roi.nd2"
                roi.write_bytes(_fetch(
                    url + "api/roi?level=0&x=0&y=0&w=64&h=64&format=nd2"
                ))
                with nd2.ND2File(str(nd2_path)) as source, nd2.ND2File(str(roi)) as exported:
                    original = open_plane(source, PlaneSelection())
                    restored = open_plane(exported, PlaneSelection())
                    expected = original.data[:, :64, :64].compute()
                    actual = restored.data.compute()
                    assert np.array_equal(actual, expected), "ND2 export changed raw pixels"
                    assert restored.pixel_size_um == original.pixel_size_um, "ND2 calibration changed"
                    assert [c.name for c in restored.channels] == [c.name for c in original.channels]
                print(
                    f"smoke ok: {info['name']} {info['width']}x{info['height']}, "
                    f"tile {len(tile)} bytes; ND2 export pixel-exact, calibrated, channels intact"
                )

            # Exercise real compressed TIFF tiles in the frozen bundle. A
            # lossless JPEG 2000 fixture also proves raw tile placement exactly.
            rng = np.random.default_rng(15)
            pixels = rng.integers(0, 256, (577, 641, 3), dtype=np.uint8)
            for codec in ("jpeg", "jpeg2000"):
                slide = work / f"{codec}.svs"
                options = {"compressionargs": {"reversible": True}} if codec == "jpeg2000" else {}
                with tifffile.TiffWriter(slide) as writer:
                    writer.write(
                        pixels, subifds=1, tile=(128, 128), photometric="rgb",
                        description="Aperio Image|MPP = 0.25|AppMag = 20",
                        compression=codec, **options,
                    )
                    writer.write(
                        pixels[::2, ::2].copy(), subfiletype=1,
                        tile=(128, 128), photometric="rgb", compression=codec, **options,
                    )
                decoded = tifffile.imread(slide)
                if codec == "jpeg2000":
                    assert np.array_equal(decoded, pixels)
                with _served(slide) as (httpd, url):
                    state = httpd.registry.get(None)
                    actual = np.moveaxis(np.asarray(state.root["0"][:, :577, :641]), 0, -1)
                    assert np.array_equal(actual, decoded), f"{codec} window pixels differ"
                    info = json.loads(_fetch(url + "api/info"))
                    assert len(info["levels"]) >= 2, "SVS pyramid missing"
                    with Image.open(io.BytesIO(_fetch(url + "api/tile/0/0/0.jpg"))) as image:
                        image.load()
                    roi = _fetch(url + "api/roi?level=0&x=11&y=13&w=64&h=64&format=tiff")
                    raw = tifffile.imread(io.BytesIO(roi))
                    if raw.shape == (3, 64, 64):
                        raw = np.moveaxis(raw, 0, -1)
                    assert np.array_equal(raw, decoded[13:77, 11:75]), f"{codec} TIFF ROI differs"
                print(f"smoke ok: SVS {codec} pyramid, tile and pixel-exact TIFF ROI")
        return 0
    except Exception as exc:
        print(f"smoke FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 3
