"""Serve a viewer HTTP server on a thread for one test, with prompt teardown.

``serve_forever`` polls for ``shutdown()`` every 0.5 s by default, so every
hand-written server thread paid up to half a second per teardown. These helpers
poll faster and always release the listening socket.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from nd2wsi.server import create_server

POLL_INTERVAL = 0.05


@contextmanager
def serving(httpd, *, poll_interval: float = POLL_INTERVAL):
    """Run an already-built ``httpd`` on a daemon thread for the block.

    Teardown order matches the viewer's own: ``shutdown()`` stops the loop
    (and, for a viewer server, closes the slide registry), ``server_close()``
    releases the socket, then the thread is joined.
    """
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": poll_interval}, daemon=True,
    )
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@contextmanager
def running_server(paths, **kw):
    """Create a loopback viewer server (ephemeral port by default) and serve it."""
    kw.setdefault("port", 0)
    with serving(create_server(paths, **kw)) as httpd:
        yield httpd
