"""Raw viewport transport must preserve scientific pixels and shared data."""

import hashlib
import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlsplit

import numpy as np
import pytest

from nd2wsi import render
from nd2wsi.metal_viewport.source import (
    MAX_INFLIGHT,
    REQUEST_MEMORY_BUDGET,
    RawTileSource,
    create_source_server,
)
from nd2wsi.server import annotations_sidecar
from nd2wsi.storage import ZarrV2Storage
from nd2wsi.window_sessions import create_window_session


def _store(tmp_path, *, channels=2, dtype=np.uint16, rgb=False):
    path = tmp_path / "research" / "specimen.ome.zarr"
    storage = ZarrV2Storage()
    root = storage.create_group(path)
    values = np.arange(channels * 13 * 19, dtype=dtype).reshape(channels, 13, 19)
    values[-1, -1, -1] = np.iinfo(dtype).max
    levels, arrays = [], {}
    while True:
        key = str(len(levels))
        array = storage.create_array(root, key, values.shape, values.dtype, tile=4)
        array[:] = values
        arrays[key] = values.copy()
        levels.append(
            {
                "path": key,
                "width": values.shape[2],
                "height": values.shape[1],
                "downsample": 2 ** len(levels),
            }
        )
        if values.shape[-2:] == (1, 1):
            break
        h, w = values.shape[-2:]
        # Sparse 1xN tail levels have the production repeat-edge convention.
        if h == 1:
            values = np.repeat(values, 2, axis=1)
        if w == 1:
            values = np.repeat(values, 2, axis=2)
        h, w = values.shape[-2:]
        values = np.rint(
            values[:, : h // 2 * 2, : w // 2 * 2]
            .reshape(channels, h // 2, 2, w // 2, 2)
            .mean(axis=(2, 4))
        ).astype(dtype)
    attrs = {
        "multiscales": [{"datasets": [{"path": x["path"]} for x in levels]}],
        "omero": {
            "channels": [
                {
                    "label": f"channel-{i}",
                    "window": {"start": 10, "end": 4095},
                    "color": "00FF00" if i == 0 else "FF00FF",
                }
                for i in range(channels)
            ]
        },
        "nd2wsi": {
            "source": "specimen.nd2",
            "dtype": str(np.dtype(dtype)),
            "rgb": rgb,
            "tile": 4,
            "levels": levels,
            "pixel_size_um": [0.31, 0.32],
            "selection": {"t": 0, "p": 0, "z": "mid"},
        },
    }
    root.attrs.update(attrs)
    return path, attrs, arrays


def _snapshot(path):
    return {
        str(p.relative_to(path)): (
            p.stat().st_size,
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in path.rglob("*")
        if p.is_file()
    }


@contextmanager
def _source(tmp_path, **kwargs):
    path, attrs, arrays = _store(tmp_path, **kwargs)
    session = create_window_session("agent", tmp_path / "sessions")
    source = RawTileSource(path, session)
    try:
        yield source, path, attrs, arrays
    finally:
        source.close()


@contextmanager
def _server(tmp_path):
    path, attrs, arrays = _store(tmp_path)
    session = create_window_session("agent", tmp_path / "sessions")
    server, base = create_source_server(path, session)
    try:
        yield server, base, path, attrs, arrays
    finally:
        server.close()


def _request(url, *, method="GET", headers=None, data=None):
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.headers, response.read()


def test_metadata_has_exact_dimensions_defaults_and_no_tile_reads(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("metadata must not read raw pixels or build CPU display images")

    monkeypatch.setattr(render, "_read_region", forbidden)
    monkeypatch.setattr(render, "composite", forbidden)
    with _source(tmp_path) as (source, _, attrs, _):
        metadata = source.metadata()
        assert (metadata["width"], metadata["height"], metadata["tile_size"]) == (19, 13, 4)
        assert metadata["levels"] == attrs["nd2wsi"]["levels"]
        assert metadata["channels"][0] == {
            "label": "channel-0",
            "window": [10.0, 4095.0],
            "color": [0, 255, 0],
        }
        assert metadata["channels"][1]["color"] == [255, 0, 255]
        assert metadata["pixel_size_um"] == [0.31, 0.32]
        assert metadata["session"]["role"] == "agent"
        assert source.metrics()["raw_tile_reads"] == 0
        # Returned metadata cannot mutate the source's routing/geometry.
        metadata["levels"][0]["width"] = 1
        assert source.metadata()["width"] == 19


def test_every_level_tile_reconstructs_exact_pixels_and_odd_edges(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("raw viewport must never call CPU composite/JPEG encoding")

    monkeypatch.setattr(render, "composite", forbidden)
    monkeypatch.setattr(render, "render_tile", forbidden)
    with _source(tmp_path) as (source, _, _, arrays):
        reads = total = 0
        for level in source.metadata()["levels"]:
            expected = arrays[level["path"]]
            reconstructed = np.zeros_like(expected)
            coverage = np.zeros(expected.shape[-2:], dtype=np.uint8)
            for ty in range((level["height"] + 3) // 4):
                for tx in range((level["width"] + 3) // 4):
                    geometry, payload = source.read_tile(level["path"], tx, ty)
                    assert len(payload) == geometry.nbytes
                    tile = np.frombuffer(payload, dtype="<u2").reshape(
                        geometry.channels, geometry.height, geometry.width
                    )
                    ys = slice(geometry.y, geometry.y + geometry.height)
                    xs = slice(geometry.x, geometry.x + geometry.width)
                    np.testing.assert_array_equal(tile, expected[:, ys, xs])
                    reconstructed[:, ys, xs] = tile
                    coverage[ys, xs] += 1
                    reads += 1
                    total += len(payload)
            np.testing.assert_array_equal(reconstructed, expected)
            assert np.all(coverage == 1)
        metrics = source.metrics()
        assert metrics["raw_tile_reads"] == reads
        assert metrics["raw_pixel_bytes"] == total
        assert metrics["cpu_rgb_operations"] == metrics["cpu_jpeg_operations"] == 0
        assert metrics["decoded_tile_cache_bytes"] == 0


def test_strided_channels_and_big_endian_are_packed_correctly(tmp_path, monkeypatch):
    with _source(tmp_path) as (source, _, _, arrays):
        expected = arrays["0"][:, :4, :4]
        storage = np.zeros((4, 4, 2), dtype=">u2")
        view = np.moveaxis(storage, -1, 0)
        view[:] = expected
        assert not view.flags.c_contiguous
        monkeypatch.setattr(render, "_read_region", lambda *args: view)
        _, payload = source.read_tile("0", 0, 0)
        assert payload == expected.astype("<u2").tobytes(order="C")


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"rgb": True}, "non-RGB"),
        ({"dtype": np.uint8}, "uint16"),
        ({"channels": 9}, "channel count"),
    ],
)
def test_unsupported_source_fails_closed_without_rewriting(tmp_path, kwargs, reason):
    path, _, _ = _store(tmp_path, **kwargs)
    before = _snapshot(path.parent)
    session = create_window_session("agent", tmp_path / "sessions")
    with pytest.raises(ValueError, match=reason):
        RawTileSource(path, session)
    assert _snapshot(path.parent) == before


def test_user_session_cannot_be_reused(tmp_path):
    path, _, _ = _store(tmp_path)
    user = create_window_session("user", tmp_path / "sessions")
    with pytest.raises(ValueError, match="Agent"):
        RawTileSource(path, user)


def test_explicit_user_source_retains_identity_but_never_writes_shared_data(tmp_path):
    path, attrs, arrays = _store(tmp_path)
    annotations = annotations_sidecar(path, attrs)
    original = b'{"items":[{"id":"manual-ROI","points":[[1,2],[7,8]]}]}'
    annotations.write_bytes(original)
    before = _snapshot(path.parent)
    user = create_window_session("user", tmp_path / "sessions")
    source = RawTileSource(path, user, allow_user=True)
    try:
        metadata = source.metadata()
        assert metadata["session"]["role"] == "user"
        assert metadata["session"]["id"] == user.id
        assert metadata["session"]["annotation_mode"] == "read-only-snapshot"
        assert metadata["read_only"] is True
        assert source.registry.agent_window
        snapshot = source.state.annotations_path
        assert snapshot.is_relative_to(user.annotation_root)
        assert snapshot != annotations and snapshot.read_bytes() == original
        assert source.read_tile("0", 0, 0)[1] == arrays["0"][:, :4, :4].astype("<u2").tobytes()
    finally:
        source.close()
    assert user.role == "user"
    assert _snapshot(path.parent) == before


def test_plate_rejected_before_registry_opens_it(tmp_path, monkeypatch):
    import nd2wsi.plate
    from nd2wsi.server import SlideRegistry

    path = tmp_path / "plate.nd2"
    path.write_bytes(b"metadata check mocked")
    monkeypatch.setattr(nd2wsi.plate, "is_plate_file", lambda path: True)
    monkeypatch.setattr(
        SlideRegistry, "open_path", lambda *args: pytest.fail("plate must not open")
    )
    session = create_window_session("agent", tmp_path / "sessions")
    with pytest.raises(ValueError, match="plate/time-series"):
        RawTileSource(path, session)


@pytest.mark.parametrize(
    "args",
    [
        ("../0", 0, 0),
        ("99", 0, 0),
        (0, 0, 0),
        ("0", -1, 0),
        ("0", 0, -1),
        ("0", 5, 0),
        ("0", 0, 4),
        ("0", True, 0),
        ("0", 0.5, 0),
        ("0", 2**31, 0),
    ],
)
def test_bad_geometry_cannot_read_source(tmp_path, monkeypatch, args):
    with _source(tmp_path) as (source, _, _, _):
        monkeypatch.setattr(
            render, "_read_region", lambda *args: pytest.fail("invalid request read pixels")
        )
        with pytest.raises(ValueError):
            source.read_tile(*args)


def test_http_exact_raw_data_and_capability_scoped_metadata(tmp_path):
    with _server(tmp_path) as (server, base, _, _, arrays):
        status, headers, payload = _request(base + "metadata")
        assert status == 200
        metadata = json.loads(payload)
        assert metadata["dtype"] == "uint16"
        assert metadata["session"]["id"] == server.source.session.id
        status, headers, payload = _request(base + "tile?level=0&tx=4&ty=3")
        assert status == 200
        assert headers["Content-Type"] == "application/octet-stream"
        assert int(headers["Content-Length"]) == len(payload) == 2 * 1 * 3 * 2
        assert headers["X-Tile-Width"] == "3" and headers["X-Tile-Height"] == "1"
        assert payload == arrays["0"][:, 12:13, 16:19].astype("<u2").tobytes()
        status, _, payload = _request(base + "metrics")
        assert status == 200
        metrics = json.loads(payload)
        assert metrics["raw_tile_reads"] == 1
        assert metrics["transport_bytes"] == 12
        assert metrics["cpu_jpeg_operations"] == 0
        status, _, payload = _request(base + "reset-metrics", method="POST", data=b"")
        assert status == 200 and json.loads(payload)["raw_tile_reads"] == 0


@pytest.mark.parametrize(
    "suffix",
    [
        "tile",
        "tile?level=0&tx=-1&ty=0",
        "tile?level=0&tx=0&ty=0&path=/tmp/other",
        "tile?level=0&tx=0&tx=1&ty=0",
        "tile?level=../0&tx=0&ty=0",
        "tile?level=0&tx=999999999&ty=0",
        "tile?level=0&tx=0.5&ty=0",
        "tile?level=0&tx=%2B1&ty=0",
        "tile?level=0&tx=0&ty=",
        "tile?broken",
    ],
)
def test_http_bad_tile_queries_never_read(tmp_path, suffix):
    with _server(tmp_path) as (server, base, _, _, _):
        assert _request(base + suffix)[0] == 400
        assert server.source.metrics()["raw_tile_reads"] == 0


def test_auth_and_cross_site_paths_are_denied(tmp_path):
    with _server(tmp_path) as (server, base, _, _, _):
        assert _request(base.replace(server.token, "not-the-token") + "metadata")[0] == 403
        assert _request(base + "metadata", headers={"Host": "attacker.invalid"})[0] == 403
        assert _request(base + "metadata", headers={"Origin": "https://attacker.invalid"})[0] == 403
        assert _request(base + "open?path=/tmp/other")[0] == 404
        assert _request(base + "reset-metrics", method="POST", data=b"{}")[0] == 400
        assert _request(base + "metadata?path=/tmp/other")[0] == 404
        assert server.source.metrics()["raw_tile_reads"] == 0


def test_two_sessions_read_same_cache_without_touching_originals(tmp_path):
    path, attrs, _ = _store(tmp_path)
    annotations = annotations_sidecar(path, attrs)
    original = b'{"items":[{"id":"manual-ROI","points":[[1.25,2],[7,8]]}],"extension":"preserve"}'
    annotations.write_bytes(original)
    before = _snapshot(path.parent)
    sessions = [create_window_session("agent", tmp_path / "sessions") for _ in range(2)]
    servers = [create_source_server(path, session) for session in sessions]
    try:
        snapshots = []
        for server, base in servers:
            assert _request(base + "tile?level=0&tx=0&ty=0")[0] == 200
            snapshot = server.source.state.annotations_path
            assert snapshot.read_bytes() == original
            snapshots.append(snapshot)
        assert snapshots[0] != snapshots[1]
        assert servers[0][1] != servers[1][1]
        assert _snapshot(path.parent) == before
    finally:
        for server, _ in servers:
            server.close()
    assert _snapshot(path.parent) == before


def test_read_budget_is_nonblocking_and_shutdown_drains(tmp_path, monkeypatch):
    with _source(tmp_path) as (source, _, _, arrays):
        entered = threading.Barrier(MAX_INFLIGHT + 1)
        release = threading.Event()

        def slow_read(*args):
            entered.wait(timeout=5)
            assert release.wait(timeout=5)
            return arrays["0"][:, :4, :4]

        monkeypatch.setattr(render, "_read_region", slow_read)
        with ThreadPoolExecutor(max_workers=MAX_INFLIGHT + 1) as pool:
            calls = [pool.submit(source.read_tile, "0", 0, 0) for _ in range(MAX_INFLIGHT)]
            entered.wait(timeout=5)
            with pytest.raises(BlockingIOError):
                source.read_tile("0", 0, 0)
            with pytest.raises(BlockingIOError):
                source.reset_metrics()
            close = pool.submit(source.close)
            assert not close.done()
            release.set()
            for call in calls:
                assert len(call.result(timeout=5)[1]) == 64
            close.result(timeout=5)
        metrics = source.metrics()
        assert metrics["closed"] and metrics["inflight_reads"] == 0
        assert metrics["peak_inflight_requests"] == MAX_INFLIGHT
        assert metrics["peak_request_working_bytes"] <= REQUEST_MEMORY_BUDGET
        with pytest.raises(RuntimeError, match="closed"):
            source.read_tile("0", 0, 0)


def test_socket_worker_admission_does_not_spawn_unbounded_threads(tmp_path):
    with _server(tmp_path) as (server, base, _, _, _):
        parsed = urlsplit(base)
        clients = []
        try:
            # Send headers without the terminating blank line. The active
            # handlers must remain bounded even before capability parsing.
            for _ in range(MAX_INFLIGHT):
                client = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
                client.sendall(b"GET / HTTP/1.1\r\n")
                clients.append(client)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
            connection.request("GET", parsed.path + "metadata")
            response = connection.getresponse()
            assert response.status == 503
            response.read()
            connection.close()
            assert server.source.metrics()["rejected_requests"] >= 1
        finally:
            for client in clients:
                client.close()


def test_failed_reads_release_request_budget(tmp_path, monkeypatch):
    with _source(tmp_path) as (source, _, _, _):

        def fail(*args):
            raise OSError("simulated corrupt chunk")

        monkeypatch.setattr(render, "_read_region", fail)
        with pytest.raises(OSError):
            source.read_tile("0", 0, 0)
        assert source.metrics()["inflight_reads"] == 0
        assert source.metrics()["failed_reads"] == 1


def test_failed_server_start_closes_bound_socket_and_source(tmp_path, monkeypatch):
    from nd2wsi.metal_viewport.source import RawTileHTTPServer

    path, _, _ = _store(tmp_path)
    session = create_window_session("agent", tmp_path / "sessions")
    instances = []

    def fail_start(server):
        instances.append(server)
        raise RuntimeError("simulated thread startup failure")

    monkeypatch.setattr(RawTileHTTPServer, "start", fail_start)
    with pytest.raises(RuntimeError, match="startup failure"):
        create_source_server(path, session)
    assert instances[0].socket.fileno() == -1
    assert instances[0].source.metrics()["closed"]
