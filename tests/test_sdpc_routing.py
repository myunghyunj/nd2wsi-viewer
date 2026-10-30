"""SDPC routing contracts; backend decoding is tested independently."""

import copy
import io
import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from http_fixture import running_server

from nd2wsi import app, cli, convert, server
from nd2wsi.annotation_sidecars import _legacy_sidecar_matches
from nd2wsi.reader import ChannelInfo, PlaneSource
from nd2wsi.window_sessions import create_window_session


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "research" / "한글 slide.SDPC"
    path.parent.mkdir()
    path.write_bytes(b"isolated SDPC routing fixture, decoded by a test double")
    return path


@pytest.fixture
def backend(monkeypatch, source):
    from nd2wsi import sdpc

    pixels = (np.arange(64 * 80, dtype=np.uint8).reshape(1, 64, 80)
              + np.array([0, 31, 73], dtype=np.uint8)[:, None, None])
    info = PlaneSource(
        data=None, dtype=pixels.dtype, shape=pixels.shape, rgb=True,
        channels=[ChannelInfo("Red", (255, 0, 0)), ChannelInfo("Green", (0, 255, 0)),
                  ChannelInfo("Blue", (0, 0, 255))],
        pixel_size_um=(0.25, 0.25), source_name=str(source), selection={},
        notes=["test backend"], magnification=40, calibration_source="sdpc-header",
    )
    windows = [{"start": 0., "end": 255., "min": 0., "max": 255.}] * 3
    attrs = convert.build_group_attrs(info, [(64, 80)], 512, windows)
    attrs["nd2wsi"]["direct"] = True

    class Root(dict):
        closed = False

        def close(self, **_):
            self.closed = True

    roots = []

    def open_direct(path):
        assert Path(path) == source.resolve()
        root = Root({"0": pixels})
        roots.append(root)
        return root, copy.deepcopy(attrs)

    opener = Mock(side_effect=open_direct)
    monkeypatch.setattr(sdpc, "open_direct", opener)
    return pixels, opener, roots


@contextmanager
def served(source, session):
    with running_server(source, window_session=session) as httpd:
        base = server.server_url(httpd).rstrip("/")
        yield httpd, base + "/s/" + httpd.registry.default_sid()


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        error.add_note(error.read().decode("utf-8", errors="replace"))
        raise


def test_sdpc_application_and_registry_bypass_all_cache_paths(monkeypatch, source):
    forbidden = Mock(side_effect=AssertionError("SDPC must not inspect or modify viewing caches"))
    monkeypatch.setattr(convert, "ensure_cache", forbidden)
    monkeypatch.setattr(convert, "existing_cache_store", forbidden)
    assert app.open_or_convert(source) == source
    registry = server.SlideRegistry()
    direct = Mock(return_value="sdpc-slide")
    monkeypatch.setattr(registry, "add_direct", direct)
    assert registry.open_path(source) == "sdpc-slide"
    assert registry.add_store(source) == "sdpc-slide"
    assert direct.call_count == 2
    forbidden.assert_not_called()


@pytest.mark.parametrize("entry", ["open_path", "add_store"])
def test_sdpc_reader_rejection_never_falls_back_to_conversion(monkeypatch, source, entry):
    registry = server.SlideRegistry()
    monkeypatch.setattr(registry, "add_direct", Mock(side_effect=NotImplementedError("unsupported SDPC codec")))
    forbidden = Mock(side_effect=AssertionError("no conversion fallback"))
    monkeypatch.setattr(convert, "convert", forbidden)
    monkeypatch.setattr(convert, "ensure_cache", forbidden)
    with pytest.raises(NotImplementedError, match="unsupported SDPC codec"):
        getattr(registry, entry)(source)
    forbidden.assert_not_called()
    assert set(source.parent.iterdir()) == {source}


def test_sdpc_agent_http_pixels_exports_and_annotations_are_isolated(source, backend, tmp_path):
    import tifffile
    from PIL import Image

    pixels, opener, roots = backend
    legacy = source.parent / f"annotations_{source.stem}.json"
    original = json.dumps({"source": {"name": source.name}, "items": [{"id": "manual-roi"}]}).encode()
    legacy.write_bytes(original)
    before = set(source.parent.iterdir())
    session = create_window_session("agent", tmp_path / "sessions")
    with served(source, session) as (httpd, url):
        state = httpd.registry.get(None)
        assert state.annotations_path.is_relative_to(session.root)
        assert state.annotations_path.read_bytes() == original
        assert state.store_path is None and state.trash_path is None
        assert httpd.registry.open_path(source) == httpd.registry.default_sid()
        opener.assert_called_once_with(source.resolve())

        info = json.loads(fetch(url + "/api/inspect"))
        assert (info["width"], info["height"], info["rgb"]) == (80, 64, True)
        assert info["storage"] == "direct" and info["associated"] == []
        assert info["calibration"]["pixel_size_um"] == [0.25, 0.25]
        assert info["objective_magnification"] == 40
        pixel = json.loads(fetch(url + "/api/pixel?x=11&y=13"))
        assert [item["value"] for item in pixel["values"]] == pixels[:, 13, 11].tolist()
        assert pixel["um"] == [2.75, 3.25]
        with Image.open(io.BytesIO(fetch(url + "/api/tile/0/0/0.jpg"))) as image:
            image.load()
            assert image.mode == "RGB" and image.size == (80, 64)
        roi = fetch(url + "/api/roi?level=0&x=11&y=13&w=17&h=19&format=tiff")
        with tifffile.TiffFile(io.BytesIO(roi)) as exported:
            assert np.array_equal(exported.asarray(), np.moveaxis(pixels[:, 13:32, 11:28], 0, -1))
            assert exported.pages[0].tags["XResolution"].value == (40000, 1)
            assert source.name.encode("ascii", "backslashreplace").decode("ascii") in exported.pages[0].description
        with Image.open(io.BytesIO(fetch(url + "/api/roi?level=0&x=11&y=13&w=17&h=19&format=png"))) as image:
            assert np.array_equal(np.asarray(image), np.moveaxis(pixels[:, 13:32, 11:28], 0, -1))
        with pytest.raises(urllib.error.HTTPError) as caught:
            fetch(url + "/api/associated/thumbnail.jpg")
        assert caught.value.code == 404
    assert all(root.closed for root in roots)
    assert set(source.parent.iterdir()) == before
    assert legacy.read_bytes() == original


def test_sdpc_registration_retries_changed_source_and_closes_stale_root(source, backend, monkeypatch, tmp_path):
    from nd2wsi import sdpc

    _, opener, roots = backend
    original_open = opener.side_effect

    def replace_on_first_open(path):
        result = original_open(path)
        if len(roots) == 1:
            source.write_bytes(source.read_bytes() + b" changed")
        return result

    monkeypatch.setattr(sdpc, "open_direct", replace_on_first_open)
    registry = server.SlideRegistry(window_session=create_window_session("agent", tmp_path / "sessions"))
    try:
        sid = registry.open_path(source)
        assert len(roots) == 2 and roots[0].closed and not roots[1].closed
        assert registry.slides[sid].manifest["source"]["size"] == source.stat().st_size
    finally:
        registry.close_all(immediate=True)
    assert all(root.closed for root in roots)


def test_sdpc_associated_images_use_registered_source_and_bounded_detail(
    source, backend, monkeypatch, tmp_path,
):
    from nd2wsi import sdpc

    names = Mock(return_value=["thumbnail", "associated-1", "associated-2"])
    jpeg = Mock(return_value=b"bounded synthetic JPEG")
    monkeypatch.setattr(sdpc, "associated_image_names", names)
    monkeypatch.setattr(sdpc, "associated_image_jpeg", jpeg)
    session = create_window_session("agent", tmp_path / "sessions")
    with served(source, session) as (_, url):
        assert json.loads(fetch(url + "/api/inspect"))["associated"] == names.return_value
        assert fetch(url + "/api/associated/associated-1.jpg") == jpeg.return_value
        jpeg.assert_called_with(source.resolve(), "associated-1")
        fetch(url + "/api/associated/associated-2.jpg?detail=1&path=/unregistered.sdpc")
        jpeg.assert_called_with(source.resolve(), "associated-2", max_side=2048)
        fetch(url + "/api/associated/thumbnail.jpg?detail=999999")
        jpeg.assert_called_with(source.resolve(), "thumbnail")
        before = jpeg.call_count
        for name in ("associated-0", "associated-17", "patient", "baseline"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                fetch(url + f"/api/associated/{name}.jpg")
            assert caught.value.code == 404
        assert jpeg.call_count == before

        def replace_during_decode(*args, **kwargs):
            source.write_bytes(source.read_bytes() + b" replaced")
            return b"must not serve stale image"

        jpeg.side_effect = replace_during_decode
        with pytest.raises(urllib.error.HTTPError) as caught:
            fetch(url + "/api/associated/associated-1.jpg?detail=1")
        assert caught.value.code == 404
        assert json.loads(fetch(url + "/api/inspect"))["associated"] == []


def test_sdpc_annotation_setup_failure_closes_new_reader(source, backend, monkeypatch):
    _, _, roots = backend
    registry = server.SlideRegistry()
    monkeypatch.setattr(registry, "annotation_path", Mock(side_effect=OSError("annotation location unavailable")))
    with pytest.raises(OSError, match="annotation location unavailable"):
        registry.open_path(source)
    assert len(roots) == 1 and roots[0].closed
    assert not registry.slides


@pytest.mark.parametrize("operation", ["convert", "ensure_cache", "estimate_store_bytes"])
def test_sdpc_conversion_api_rejects_before_any_filesystem_changes(source, tmp_path, operation):
    output = tmp_path / "not-created" / "store.ome.zarr"
    with pytest.raises(ValueError, match="SDPC"):
        if operation == "convert":
            convert.convert(source, output, progress=False)
        else:
            getattr(convert, operation)(source)
    assert not output.parent.exists()
    assert set(source.parent.iterdir()) == {source}


def test_sdpc_cli_view_and_info_use_direct_backend(source, monkeypatch, capsys):
    from nd2wsi import sdpc

    serve = Mock()
    monkeypatch.setattr(server, "serve", serve)
    monkeypatch.setattr(convert, "ensure_cache", Mock(side_effect=AssertionError("no cache")))
    assert cli.main(["view", str(source)]) == 0
    assert serve.call_args.args[0] == [source]
    capsys.readouterr()
    metadata = {"width": 80, "height": 64, "pyramid": [{"level": 0}]}
    inspect = Mock(return_value=metadata)
    monkeypatch.setattr(sdpc, "sdpc_info", inspect)
    assert cli.main(["info", str(source), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == metadata
    inspect.assert_called_once_with(str(source))


@pytest.mark.parametrize("arguments", [
    ["--store", "out.zarr"], ["--overwrite"], ["--t", "0"], ["--z=mid"],
    ["--position", "0"], ["--tile", "512"], ["--workers", "1"], ["--yes"],
])
def test_sdpc_cli_rejects_explicit_options_before_opening_any_slide(source, monkeypatch, capsys, arguments):
    forbidden = Mock(side_effect=AssertionError("must reject before opening any slide"))
    monkeypatch.setattr(convert, "ensure_cache", forbidden)
    monkeypatch.setattr(server, "serve", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main(["view", "earlier.nd2", str(source), *arguments])
    assert caught.value.code == 2
    assert "SDPC supports direct viewing only" in capsys.readouterr().err
    forbidden.assert_not_called()


def test_sdpc_cli_convert_fails_clearly_without_creating_output(source, tmp_path, capsys):
    output = tmp_path / "not-created" / "store.ome.zarr"
    with pytest.raises(SystemExit) as caught:
        cli.main(["convert", str(source), str(output)])
    assert caught.value.code == 2
    assert "not pyramid conversion" in capsys.readouterr().err
    assert not output.parent.exists()


def test_sdpc_drop_is_accepted_and_same_stem_legacy_data_stays_unassigned(source, monkeypatch):
    api = app.Api(None)
    launch = Mock(return_value="opened")
    monkeypatch.setattr(api, "_launch_many", launch)
    assert api.open_paths([str(source)]) == "opened"
    launch.assert_called_once_with([source])
    sibling = source.with_suffix(".svs")
    sibling.write_bytes(b"other source")
    legacy = source.parent / f"annotations_{source.stem}.json"
    legacy.write_text('{"items": []}')
    assert not _legacy_sidecar_matches(legacy, source.name, source.parent)
    assert not convert._legacy_store_matches_source(source.parent / "legacy.ome.zarr", source, {})
