"""Release checks executed inside the shipped application, without pytest."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import urllib.request
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
            store = open_or_convert(nd2_path, on_status=print)
            with _served(store) as (_httpd, url):
                info = json.loads(_fetch(url + "api/info"))
                tile = _fetch(url + "api/tile/0/0/0.jpg")
                with Image.open(io.BytesIO(tile)) as image:
                    image.load()
                    assert min(image.size) > 0
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
