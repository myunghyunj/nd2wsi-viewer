"""The capability URL is the only door.

The server can open local files, write annotations and delete caches; it
had no authentication at all, so any local process — or a web page using
a preflight-free POST — could drive it. Every route now lives under a
per-run random token, loopback is pinned, and cross-origin browser
requests are refused.
"""

import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")

from nd2wsi.server import create_server, server_url  # noqa: E402


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    rng = np.random.default_rng(2)
    img = rng.integers(0, 255, (700, 900, 3), dtype=np.uint8)
    p = tmp_path_factory.mktemp("sec") / "s.svs"
    with tifffile.TiffWriter(p) as tw:
        opts = dict(tile=(240, 240), photometric="rgb", compression="jpeg2000",
                    compressionargs={"reversible": True})
        tw.write(img, subifds=1, description="Aperio Image|MPP = 0.5", **opts)
        tw.write(img[:700, :896][::4, ::4].copy(), subfiletype=1, **opts)
    httpd = create_server(p, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def _status(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method, data=data,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_without_the_token_everything_is_a_404(served):
    port = served.server_address[1]
    bare = f"http://127.0.0.1:{port}"
    for path in ("/", "/api/slides", "/api/info", "/static/app.js",
                 f"/{'x' * len(served.token)}/api/slides"):
        assert _status(bare + path) == 404, path
    assert _status(bare + "/api/trash", method="POST", data=b"{}",
                   headers={"Content-Type": "application/json"}) == 404


def test_with_the_token_the_viewer_works(served):
    base = server_url(served).rstrip("/")
    slides = json.loads(urllib.request.urlopen(base + "/api/slides", timeout=30).read())
    assert slides["slides"]
    assert _status(base + "/static/app.js") == 200
    assert _status(base + "/static/axis-latch-v1.js") == 200
    assert _status(base + "/api/tile/0/0/0.jpg") == 200


def test_cross_origin_posts_are_refused(served):
    base = server_url(served).rstrip("/")
    # a browser attack ships a foreign Origin; curl-style tools send none
    assert _status(base + "/api/close", method="POST", data=b"{}",
                   headers={"Content-Type": "application/json",
                            "Origin": "https://evil.example"}) == 404
    # and the preflight-free text/plain trick fails on content type
    assert _status(base + "/api/close", method="POST", data=b"{}",
                   headers={"Content-Type": "text/plain"}) == 415


def test_foreign_host_header_is_refused(served):
    base = server_url(served).rstrip("/")
    assert _status(base + "/api/slides",
                   headers={"Host": "evil.example"}) == 404


def test_non_loopback_bind_is_refused(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        create_server([], host="0.0.0.0", port=0)


def test_static_stays_inside_its_directory(served):
    base = server_url(served).rstrip("/")
    for probe in ("/static/../server.py", "/static/%2e%2e/server.py"):
        assert _status(base + probe) == 404, probe


def test_eight_threads_hammering_tiles_and_listing(served):
    """The registry used to iterate its dict unlocked while other threads
    mutated it; polling plus opening raced into RuntimeError."""
    import concurrent.futures

    base = server_url(served).rstrip("/")

    def hit(i):
        if i % 3 == 0:
            return len(urllib.request.urlopen(base + "/api/slides", timeout=30).read())
        tx, ty = i % 2, (i // 2) % 2
        return len(
            urllib.request.urlopen(
                f"{base}/api/tile/0/{tx}/{ty}.jpg", timeout=30
            ).read()
        )

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        sizes = list(ex.map(hit, range(80)))
    assert all(s > 0 for s in sizes)


def test_download_filenames_survive_any_alphabet():
    from nd2wsi.server import content_disposition

    header = content_disposition("세포_L0.nd2")
    header.encode("latin-1")  # must be header-safe
    assert "filename*=UTF-8''%EC%84%B8%ED%8F%AC" in header
    plain = content_disposition("plain.nd2")
    assert 'filename="plain.nd2"' in plain
