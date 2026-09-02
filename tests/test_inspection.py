"""Pixel and slide inspection APIs, including Aperio auxiliary images."""

import io
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")

from nd2wsi.server import (  # noqa: E402
    ViewerState,
    _associated_source_path,
    _inspection_storage_details,
    _pixel_payload,
    create_server,
    reveal_in_file_manager,
    server_url,
)
from nd2wsi.svs import _validate_associated_shape  # noqa: E402


def _write_pyramidal_svs(path, *, associated=True, seed=42):
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 255, (1040, 1200, 3), dtype=np.uint8)
    opts = {
        "tile": (240, 240),
        "photometric": "rgb",
        "compression": "jpeg2000",
        "compressionargs": {"reversible": True},
    }
    with tifffile.TiffWriter(path) as writer:
        writer.write(
            image,
            subifds=1,
            description="Aperio Image|MPP = 0.25|AppMag = 20",
            **opts,
        )
        writer.write(image[::4, ::4].copy(), subfiletype=1, **opts)
        if associated:
            thumbnail = rng.integers(0, 255, (700, 900, 3), dtype=np.uint8)
            writer.write(
                thumbnail,
                description="ThUmBnAiL 900x700",
                metadata=None,
                photometric="rgb",
                extratags=[(274, "H", 1, 6, False)],
            )
            label = np.linspace(0, 65535, 90 * 120, dtype=np.uint16).reshape(90, 120)
            writer.write(label, description="LaBeL 120x90", metadata=None)
            macro = rng.integers(0, 255, (80, 200, 4), dtype=np.uint8)
            writer.write(
                macro,
                description="MACRO 200x80",
                metadata=None,
                photometric="rgb",
                extrasamples=["unassalpha"],
            )
    return image


@pytest.fixture()
def associated_svs(tmp_path):
    path = tmp_path / "with-associated.svs"
    return path, _write_pyramidal_svs(path)


@contextmanager
def _served(path):
    httpd = create_server(path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, server_url(httpd).rstrip("/")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get_json(url):
    return json.loads(urllib.request.urlopen(url, timeout=30).read())


def _http_error(url, code, *, data=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=30)
    assert caught.value.code == code
    return json.loads(caught.value.read())


def test_direct_svs_pixel_inspect_and_associated_endpoints(associated_svs):
    from PIL import Image

    path, pixels = associated_svs
    with _served(path) as (httpd, base):
        state = httpd.registry.get(None)
        slide_base = f"{base}/s/{httpd.registry.default_sid()}"
        assert state.source_path == path.resolve()
        assert state.store_path is None and state.container_path is None
        with pytest.raises(AttributeError):
            state.source_path = path.with_name("other.svs")

        pixel = _get_json(slide_base + "/api/pixel?x=12.8&y=9.9")
        assert (pixel["x"], pixel["y"]) == (12, 9)
        assert pixel["probed_level"] == 0
        assert pixel["sample_kind"] == "native"
        assert pixel["um"] == pytest.approx([3.0, 2.25])
        assert [item["name"] for item in pixel["values"]] == ["Red", "Green", "Blue"]
        assert [item["value"] for item in pixel["values"]] == pixels[9, 12].tolist()
        assert pixel["calibration"] == {
            "status": "calibrated",
            "source": "aperio-mpp",
            "pixel_size_um": [0.25, 0.25],
        }

        inspect = _get_json(slide_base + "/api/inspect")
        assert inspect["source_path"] == str(path.resolve())
        assert inspect["store_path"] is None
        assert inspect["container_path"] is None
        assert inspect["cache_path"] is None
        assert inspect["source_bytes"] == path.stat().st_size
        assert inspect["source_allocated_bytes"] is not None
        assert inspect["cache_bytes"] is None
        assert inspect["storage"] == "direct"
        assert inspect["storage_details"] == {"mode": "direct"}
        assert inspect["objective"] == pytest.approx(20.0)
        assert inspect["associated"] == ["thumbnail", "label", "macro"]

        for name in inspect["associated"]:
            response = urllib.request.urlopen(
                f"{slide_base}/api/associated/{name}.jpg?g={state.generation}",
                timeout=30,
            )
            assert response.headers.get_content_type() == "image/jpeg"
            image = Image.open(io.BytesIO(response.read()))
            assert image.mode == "RGB"
            assert max(image.size) <= 512
        # Orientation 6 rotates the landscape thumbnail to portrait.
        thumb = Image.open(
            io.BytesIO(
                urllib.request.urlopen(
                    slide_base + "/api/associated/thumbnail.jpg", timeout=30
                ).read()
            )
        )
        assert thumb.height > thumb.width

        _http_error(slide_base + "/api/pixel?x=1", 400)
        _http_error(slide_base + "/api/associated/baseline.jpg", 404)


def test_inspect_and_associated_are_clean_when_series_are_missing(tmp_path):
    path = tmp_path / "baseline-only.svs"
    _write_pyramidal_svs(path, associated=False)
    with _served(path) as (_, base):
        assert _get_json(base + "/api/inspect")["associated"] == []
        _http_error(base + "/api/associated/label.jpg", 404)


def test_direct_svs_hides_auxiliary_images_after_atomic_source_replacement(
    associated_svs, tmp_path, monkeypatch
):
    import nd2wsi.svs as svs_module

    path, _ = associated_svs
    replacement = tmp_path / "replacement.svs"
    _write_pyramidal_svs(replacement, associated=True, seed=43)
    with _served(path) as (httpd, base):
        state = httpd.registry.get(None)
        assert state.manifest["source"]["quick_sha256"]
        assert _get_json(base + "/api/inspect")["associated"]

        decode = svs_module.associated_image_jpeg

        def replace_then_decode(source, name, **kwargs):
            replacement.replace(path)
            return decode(source, name, **kwargs)

        monkeypatch.setattr(svs_module, "associated_image_jpeg", replace_then_decode)
        _http_error(base + "/api/associated/label.jpg", 404)
        assert _get_json(base + "/api/inspect")["associated"] == []


def test_associated_shape_guard_uses_checked_pixel_and_byte_limits():
    _validate_associated_shape(
        SimpleNamespace(shape=(512, 512, 3), axes="YXS", dtype=np.uint8),
        "thumbnail",
    )
    with pytest.raises(ValueError, match="unexpectedly large"):
        _validate_associated_shape(
            SimpleNamespace(
                shape=(2**32 - 1, 2**32 - 1, 4),
                axes="YXS",
                dtype=np.uint8,
            ),
            "macro",
        )
    with pytest.raises(ValueError, match="unexpectedly large"):
        _validate_associated_shape(
            SimpleNamespace(shape=(4_000, 8_000, 4), axes="YXS", dtype=np.uint16),
            "macro",
        )


def test_inspection_storage_details_fall_back_to_embedded_store_descriptor():
    descriptor = {
        "format": "zarr",
        "zarr_version": 2,
        "ngff_version": "0.4",
        "backend": "zarr-v2-direct",
    }
    attrs = {"nd2wsi": {"kind": "full", "storage": descriptor}}
    state = ViewerState({}, attrs)

    assert _inspection_storage_details(state, attrs["nd2wsi"]) == {
        **descriptor,
        "mode": "full",
    }


def test_legacy_store_without_source_fingerprint_hides_auxiliary_images(tmp_path):
    source = tmp_path / "legacy.svs"
    source.write_bytes(b"current source is not tied to the cached pixels")
    state = ViewerState({}, {"nd2wsi": {}}, source_path=source, manifest={})

    assert _associated_source_path(state) is None


def test_cached_svs_keeps_provenance_but_hides_associated_when_source_is_missing(
    associated_svs,
):
    from nd2wsi.convert import ensure_cache

    path, _ = associated_svs
    store = ensure_cache(path, kind="full", tile=512)
    path.unlink()
    with _served(store) as (httpd, base):
        state = httpd.registry.get(None)
        inspect = _get_json(base + "/api/inspect")
        assert state.source_path == path.resolve()
        assert state.store_path == store.resolve()
        assert state.container_path == store.parent.resolve()
        assert inspect["source_path"] == str(path.resolve())
        assert inspect["source_bytes"] is None
        assert inspect["cache_path"] == str(store.parent.resolve())
        assert inspect["cache_bytes"] > 0
        assert inspect["cache_allocated_bytes"] is not None
        assert inspect["storage_details"]["mode"] == "full"
        assert inspect["storage_details"]["backend"] == "zarr-v2-direct"
        assert inspect["associated"] == []
        _http_error(base + "/api/associated/macro.jpg", 404)
        _http_error(base + "/api/reveal", 404, data={"which": "source"})


def test_degraded_pixel_reads_first_stored_level_without_dividing_coordinates():
    data = np.array(
        [
            [[0, 1, 2, 3], [10, 11, 12, 13], [20, 21, 22, 23]],
            [[100, 101, 102, 103], [110, 111, 112, 113], [120, 121, 122, 123]],
        ],
        dtype=np.uint16,
    )
    attrs = {
        "nd2wsi": {
            "source": "missing.nd2",
            "kind": "overview-degraded",
            "dtype": "uint16",
            "rgb": False,
            "tile": 512,
            "pixel_size_um": [1.0, 1.5],
            "calibration": {"status": "calibrated", "source": "nd2-voxel-size"},
            "levels": [{"path": "1", "width": 4, "height": 3, "downsample": 1}],
            "selection": {"t": 0, "p": 0, "z": 2},
            "notes": [],
        },
        "omero": {
            "channels": [
                {"label": "C0", "color": "FFFFFF", "window": {}},
                {"label": "C1", "color": "FFFFFF", "window": {}},
            ]
        },
    }
    payload = _pixel_payload(ViewerState({"1": data}, attrs), 3, 2)
    assert (payload["x"], payload["y"]) == (3, 2)
    assert payload["probed_level"] == 1
    assert payload["sample_kind"] == "overview-mean"
    assert [item["value"] for item in payload["values"]] == [23, 123]
    assert payload["um"] == pytest.approx([4.5, 2.0])


def test_reveal_accepts_only_the_registered_selector(associated_svs, monkeypatch):
    import nd2wsi.server as server_module

    path, _ = associated_svs
    revealed = []
    monkeypatch.setattr(server_module, "reveal_in_file_manager", revealed.append)
    with _served(path) as (httpd, base):
        reveal_url = f"{base}/s/{httpd.registry.default_sid()}/api/reveal"
        request = urllib.request.Request(
            reveal_url,
            data=json.dumps({"which": "source"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert _get_json(request)["ok"]
        assert revealed == [path.resolve()]

        _http_error(reveal_url, 400, data={"which": "/etc/passwd"})
        _http_error(reveal_url, 404, data={"which": "cache"})
        assert revealed == [path.resolve()]


def test_new_inspection_routes_remain_behind_the_capability_token(associated_svs):
    path, _ = associated_svs
    with _served(path) as (httpd, _):
        bare = f"http://127.0.0.1:{httpd.server_address[1]}"
        for suffix in (
            "/api/inspect",
            "/api/pixel?x=0&y=0",
            "/api/associated/label.jpg",
        ):
            _http_error(bare + suffix, 404)
        _http_error(bare + "/api/reveal", 404, data={"which": "source"})


def test_finder_reveal_uses_an_argument_vector_and_is_platform_guarded(tmp_path):
    path = tmp_path / "slide.svs"
    path.write_bytes(b"slide")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))

    reveal_in_file_manager(path, platform="darwin", runner=runner)
    assert calls == [
        (["/usr/bin/open", "-R", str(path)], {"check": True, "timeout": 10})
    ]
    with pytest.raises(RuntimeError, match="macOS"):
        reveal_in_file_manager(path, platform="linux", runner=runner)
