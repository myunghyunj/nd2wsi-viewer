"""Read-only raw-pixel transport for a single, session-scoped Metal viewport.

This is deliberately NOT a display server: it never composites, encodes RGB,
or caches decoded tiles. The native renderer owns its bounded GPU tile cache.
The local HTTP transport does copy packed uint16 pixels; it is not zero-copy.
All routes require an unguessable capability and are bound to IPv4 loopback.
"""

from __future__ import annotations

import copy
import hmac
import json
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np

from .. import render
from ..cache import fingerprints_match, quick_fingerprint, read_manifest
from ..server import SlideRegistry

MAX_INFLIGHT = 4
REQUEST_MEMORY_BUDGET = 32 * 1024 * 1024
MAX_TILE_SIZE = 512


@dataclass(frozen=True)
class TileGeometry:
    path: str
    tx: int
    ty: int
    x: int
    y: int
    width: int
    height: int
    channels: int

    @property
    def nbytes(self):
        return self.channels * self.width * self.height * 2


def _integer(value, label, *, minimum=1, maximum=2**31 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


class RawTileSource:
    """One immutable source selection, with bounded reads and explicit lifetime."""

    def __init__(self, path, session, *, allow_user=False):
        if session is None or (session.role != "agent" and not (allow_user and session.role == "user")):
            raise ValueError("a fresh Agent window session is required for this viewport beta")
        self.path = Path(path).expanduser().resolve(strict=True)
        self.session = session
        self._condition = threading.Condition()
        self._closed = False
        self._active = 0
        self._active_bytes = 0
        self._metrics = self._empty_metrics()
        self.fingerprints_before = {}
        self.fingerprints_after = {}
        # A human's native window is also read-only. Apply the established Agent
        # storage policy (no builds/migrations, private annotation snapshot),
        # without changing the window's actual identity or adopting a User UI.
        self.registry = SlideRegistry(window_session=replace(session, role="agent"))
        try:
            # Fail before PlateSource is constructed; plate rendering has a
            # different frame provider and is an explicitly separate milestone.
            if self.path.suffix.lower() == ".nd2":
                from ..plate import is_plate_file

                if is_plate_file(self.path):
                    raise ValueError("plate/time-series ND2 is not supported by this viewport beta")
            if self.path.suffix.lower() == ".nd2svs":
                manifest = read_manifest(self.path) or {}
                if manifest.get("kind") == "plate":
                    raise ValueError("plate caches are not supported by this viewport beta")
            self._fingerprint(self.path)
            sid = self.registry.open_path(self.path)
            self.state = self.registry.slides[sid]
            if self.state.source_path is not None:
                self._fingerprint(self.state.source_path)
            if self.state.cache_path is not None:
                self._fingerprint(self.state.cache_path)
            self._metadata = self._build_metadata()
            self._levels = {level["path"]: level for level in self._metadata["levels"]}
        except BaseException:
            self.registry.close_all(immediate=True)
            raise

    @staticmethod
    def _empty_metrics():
        return {
            "raw_tile_reads": 0,
            "raw_pixel_bytes": 0,
            "decode_duration_seconds": 0.0,
            "transport_bytes": 0,
            "failed_reads": 0,
            "rejected_requests": 0,
            "peak_inflight_requests": 0,
            "peak_request_working_bytes": 0,
            "cpu_rgb_operations": 0,
            "cpu_jpeg_operations": 0,
        }

    def _fingerprint(self, path):
        path = Path(path)
        if path.is_file() and str(path) not in self.fingerprints_before:
            self.fingerprints_before[str(path)] = quick_fingerprint(path)

    def _build_metadata(self):
        attrs = self.state.attrs
        meta = attrs["nd2wsi"]
        if self.state.plate is not None or meta.get("rgb"):
            raise ValueError("this beta supports non-RGB fluorescence, not plate or RGB sources")
        if meta.get("kind") == "overview-degraded":
            raise ValueError("the original ND2 must be available and match this overview cache")
        if np.dtype(meta["dtype"]).kind != "u" or np.dtype(meta["dtype"]).itemsize != 2:
            raise ValueError("this beta requires raw uint16 fluorescence pixels")
        raw_channels = attrs["omero"]["channels"]
        count = _integer(len(raw_channels), "channel count", maximum=8)
        windows, colors = render.display_params(attrs)
        channels = []
        for index, channel in enumerate(raw_channels):
            lo, hi = windows[index]
            if not all(math.isfinite(v) for v in (lo, hi)) or hi <= lo:
                raise ValueError("invalid stored channel window")
            color = colors[index]
            if len(color) != 3 or any(not 0 <= value <= 255 for value in color):
                raise ValueError("invalid stored channel color")
            channels.append(
                {
                    "label": str(channel.get("label", f"Channel {index + 1}")),
                    "window": [lo, hi],
                    "color": list(color),
                }
            )
        # A smaller transport tile is allowed even if a custom cache uses
        # larger storage chunks. This bound concerns request payloads, not
        # third-party codec scratch allocations or the process's total RSS.
        tile_size = min(_integer(meta["tile"], "tile size"), MAX_TILE_SIZE)
        levels = []
        paths = set()
        previous = None
        for raw in meta["levels"]:
            path = str(raw["path"])
            if not re.fullmatch(r"[0-9]{1,9}", path) or path in paths:
                raise ValueError("invalid or duplicate pyramid level path")
            width = _integer(raw["width"], "level width")
            height = _integer(raw["height"], "level height")
            downsample = _integer(raw["downsample"], "level downsample")
            array = self.state.root[path]
            if tuple(array.shape) != (count, height, width):
                raise ValueError("pyramid metadata does not match raw array shape")
            if np.dtype(array.dtype).kind != "u" or np.dtype(array.dtype).itemsize != 2:
                raise ValueError("all pyramid levels must contain uint16 pixels")
            if previous is not None and (
                downsample <= previous["downsample"]
                or width > previous["width"]
                or height > previous["height"]
            ):
                raise ValueError("pyramid levels must be ordered finest to coarsest")
            previous = {"path": path, "width": width, "height": height, "downsample": downsample}
            levels.append(previous)
            paths.add(path)
        if not levels or levels[0]["downsample"] != 1:
            raise ValueError("a full-resolution level is required")
        return {
            "source": str(meta["source"]),
            "width": levels[0]["width"],
            "height": levels[0]["height"],
            "tile_size": tile_size,
            "dtype": "uint16",
            "byte_order": "little",
            "layout": "CYX",
            "channels": channels,
            "levels": levels,
            "pixel_size_um": meta.get("pixel_size_um"),
            "selection": meta.get("selection", {}),
            "generation": self.state.generation,
            "session": {**self.session.as_dict(), "annotation_mode": "read-only-snapshot"},
            "read_only": True,
        }

    def metadata(self):
        return copy.deepcopy(self._metadata)

    def tile_geometry(self, level, tx, ty):
        if not isinstance(level, str) or level not in self._levels:
            raise ValueError("unknown pyramid level")
        tx = _integer(tx, "tile x", minimum=0)
        ty = _integer(ty, "tile y", minimum=0)
        entry = self._levels[level]
        tile = self._metadata["tile_size"]
        x, y = tx * tile, ty * tile
        if x >= entry["width"] or y >= entry["height"]:
            raise ValueError("tile is outside the pyramid level")
        return TileGeometry(
            level,
            tx,
            ty,
            x,
            y,
            min(tile, entry["width"] - x),
            min(tile, entry["height"] - y),
            len(self._metadata["channels"]),
        )

    def read_tile(self, level, tx, ty):
        """Return packed C,Y,X little-endian bytes, without any display work."""
        geometry = self.tile_geometry(level, tx, ty)
        working_bytes = geometry.nbytes * 2  # decoded pixels + transport bytes
        with self._condition:
            if self._closed:
                raise RuntimeError("source is closed")
            if (
                self._active >= MAX_INFLIGHT
                or self._active_bytes + working_bytes > REQUEST_MEMORY_BUDGET
            ):
                raise BlockingIOError("raw tile request budget is busy; retry later")
            self._active += 1
            self._active_bytes += working_bytes
            self._metrics["peak_inflight_requests"] = max(
                self._metrics["peak_inflight_requests"], self._active
            )
            self._metrics["peak_request_working_bytes"] = max(
                self._metrics["peak_request_working_bytes"], self._active_bytes
            )
        started = time.perf_counter()
        try:
            region = render._read_region(
                self.state.root,
                geometry.path,
                geometry.x,
                geometry.y,
                geometry.width,
                geometry.height,
            )
            if region.shape != (geometry.channels, geometry.height, geometry.width):
                raise ValueError("raw source returned an unexpected tile shape")
            if region.dtype.kind != "u" or region.dtype.itemsize != 2:
                raise ValueError("raw source changed pixel type")
            payload = region.astype("<u2", copy=False).tobytes(order="C")
            with self._condition:
                self._metrics["raw_tile_reads"] += 1
                self._metrics["raw_pixel_bytes"] += len(payload)
                self._metrics["decode_duration_seconds"] += time.perf_counter() - started
            return geometry, payload
        except BaseException:
            with self._condition:
                self._metrics["failed_reads"] += 1
            raise
        finally:
            with self._condition:
                self._active -= 1
                self._active_bytes -= working_bytes
                self._condition.notify_all()

    def metrics(self):
        with self._condition:
            return {
                **self._metrics,
                "inflight_reads": self._active,
                "request_memory_budget_bytes": REQUEST_MEMORY_BUDGET,
                "max_inflight_requests": MAX_INFLIGHT,
                "decoded_tile_cache_bytes": 0,
                "closed": self._closed,
                "notes": "Application raw payload counters, not measured hardware bandwidth. "
                "Read time includes packing. Budget excludes codec scratch and kernel socket buffers.",
            }

    def reset_metrics(self):
        with self._condition:
            if self._active:
                raise BlockingIOError("cannot reset metrics while raw reads are active")
            self._metrics = self._empty_metrics()

    def _account(self, key, value=1):
        with self._condition:
            self._metrics[key] += value

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            while self._active:
                self._condition.wait()
        self.registry.close_all(immediate=True)
        for path in self.fingerprints_before:
            try:
                self.fingerprints_after[path] = quick_fingerprint(path)
            except OSError as exc:
                self.fingerprints_after[path] = {"error": str(exc)}

    def preservation_report(self):
        return {
            "fingerprint_method": "size, mtime, sampled head/middle/tail SHA-256; not a full-file hash",
            "before": copy.deepcopy(self.fingerprints_before),
            "after": copy.deepcopy(self.fingerprints_after),
            "unchanged": (
                all(
                    fingerprints_match(before, self.fingerprints_after.get(path, {}))
                    for path, before in self.fingerprints_before.items()
                )
                if self._closed
                else None
            ),
        }


class RawTileHTTPServer(ThreadingHTTPServer):
    """Bound worker admission before spawning threads; drain before source close."""

    daemon_threads = False
    block_on_close = True
    request_queue_size = 8

    def __init__(self, source):
        self.source = source
        self.token = secrets.token_urlsafe(32)
        self._slots = threading.BoundedSemaphore(MAX_INFLIGHT)
        self._thread = None
        super().__init__(("127.0.0.1", 0), _Handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.source._account("rejected_requests")
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                    b"Content-Length: 0\r\nRetry-After: 1\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def start(self):
        self._thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="Metal raw tile source",
            daemon=True,
        )
        self._thread.start()

    def server_close(self):
        super().server_close()
        self.source.close()

    def close(self):
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # Never leak the capability URL into stderr or app logs.

    def _send(self, status, payload, *, content_type="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def _json(self, status, value):
        self._send(status, json.dumps(value, allow_nan=False).encode())

    def _route(self):
        parsed = urlsplit(self.path)
        parts = parsed.path.split("/")
        expected_host = f"127.0.0.1:{self.server.server_port}"
        # Native URLSession sends no Origin. Reject cross-site browser access
        # and noncanonical Host headers as defense against DNS rebinding.
        if (
            self.headers.get("Host") != expected_host
            or self.headers.get("Origin")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or len(parts) != 3
            or not hmac.compare_digest(parts[1], self.server.token)
        ):
            self._json(403, {"error": "invalid viewport capability"})
            return None
        return parts[2], parsed.query

    def do_GET(self):
        try:
            route = self._route()
            if route is None:
                return
            endpoint, query = route
            if endpoint in {"metadata", "metrics"} and not query:
                value = (
                    self.server.source.metadata()
                    if endpoint == "metadata"
                    else self.server.source.metrics()
                )
                return self._json(200, value)
            if endpoint != "tile":
                return self._json(404, {"error": "unknown raw viewport endpoint"})
            params = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
            if set(params) != {"level", "tx", "ty"} or any(len(v) != 1 for v in params.values()):
                raise ValueError("tile requires exactly level, tx and ty")
            values = {key: value[0] for key, value in params.items()}
            if any(not re.fullmatch(r"[0-9]{1,9}", value) for value in values.values()):
                raise ValueError("tile coordinates must be nonnegative decimal integers")
            geometry, payload = self.server.source.read_tile(
                values["level"], int(values["tx"]), int(values["ty"])
            )
            self._send(
                200,
                payload,
                content_type="application/octet-stream",
                headers={
                    "X-Tile-Width": geometry.width,
                    "X-Tile-Height": geometry.height,
                    "X-Tile-Channels": geometry.channels,
                    "X-Tile-Dtype": "uint16-le",
                    "X-Tile-X": geometry.x,
                    "X-Tile-Y": geometry.y,
                },
            )
            self.server.source._account("transport_bytes", len(payload))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # Discard cancelled viewport requests; never retain payloads.
        except BlockingIOError as exc:
            self._json(503, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "raw tile read failed"})

    def do_POST(self):
        try:
            route = self._route()
            if route is None:
                return
            endpoint, query = route
            if endpoint != "reset-metrics" or query:
                return self._json(404, {"error": "unknown raw viewport endpoint"})
            if self.headers.get("Content-Length", "0") != "0" or self.headers.get(
                "Transfer-Encoding"
            ):
                return self._json(400, {"error": "metric reset accepts no body"})
            self.server.source.reset_metrics()
            self._json(200, self.server.source.metrics())
        except BlockingIOError as exc:
            self._json(503, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


def create_source_server(path, session, *, allow_user=False):
    """Open a read-only source and start its ephemeral capability HTTP server.

    The caller owns the returned server and MUST call ``close()`` (or
    ``shutdown(); server_close()``) after the native window closes. Its source
    remains valid until all admitted requests finish. There is no arbitrary
    file-open, annotation-write, cache-build, or display-settings endpoint.
    """
    source = RawTileSource(path, session, allow_user=allow_user)
    server = None
    try:
        server = RawTileHTTPServer(source)
        server.start()
    except BaseException:
        if server is not None:
            server.server_close()
        else:
            source.close()
        raise
    return server, f"http://127.0.0.1:{server.server_port}/{server.token}/"
