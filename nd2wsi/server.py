"""Local viewer server (stdlib only -- no web framework dependency).

Multiple slides are served at once, browser-tab style:

GET  /                          tab shell (one tab per open slide)
GET  /static/<path>             js / css / vendored OpenSeadragon (global)
GET  /api/slides                open slides: [{sid, name, width, height}]
POST /api/open                  {"path": "/…/slide.nd2|.svs|.sdpc|store.ome.zarr"}
                                converts if needed, registers, -> {sid}
POST /api/close                 {"sid": "…"} unregister a slide

Per slide (also reachable bare as /api/… for the first slide, so scripts
keep working):

GET  /s/<sid>/                  viewer page
GET  /s/<sid>/api/info          store metadata as JSON
GET  /s/<sid>/api/inspect       slide provenance and storage details
GET  /s/<sid>/api/pixel         one raw sample at displayed-base x/y
GET  /s/<sid>/api/associated/<thumbnail|label|macro>.jpg
GET  /s/<sid>/api/histogram     per-channel LUT histograms
GET  /s/<sid>/api/tile/<L>/<x>/<y>.jpg?c=0,1&win=…
GET  /s/<sid>/api/plate/frame/<t>/<p>/<z>.jpg?k=8&c=&win=   reduced frame of one site
GET  /s/<sid>/api/plate/status  how much of the thumbnail store is filled
GET  /s/<sid>/api/plate/focus   the sharpest plane per time point and site
GET  /s/<sid>/api/roi?level=&x=&y=&w=&h=&format=nd2|tiff|png|jpg|svg&c=&win=&scalebar=1
GET/POST /s/<sid>/api/annotations   sidecar annotations
"""

from __future__ import annotations

import concurrent.futures
import errno
import hmac
import json
import math
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

from . import render
from .annotation_sidecars import (
    SLIDE_SUFFIXES,
    _manifest_source_path,
    annotations_sidecar,
    confocal_annotations_sidecar,
    plate_annotations_sidecar,
)
from .astromap_export import (
    AstromapConflict,
    AstromapDownloads,
    AstromapError,
    prepare_astromap,
)
from .cache_removal import (
    _delete_verified_cache_file,
    _delete_verified_cache_tree,
    _same_file_identity,
    _validate_file_cache_for_trash,
    rescue_annotations,
)
from .direct_common import LeaseClosed, _Lifecycle
from .platform_io import filesystem_path
from .region_export import JOB_RE, ExportRequestError, parse_job_id, prepare_export
from .server_registry import (
    SlideRegistry,
    ViewerState,
    _auto_record_allowed,
    _is_isolated_settings_env,
    _sidecar_items,
    store_generation,
)
from .server_series import (
    _HEX6_RE,
    _SERIES_META_CACHE,
    _SERIES_META_LOCK,
    _SERIES_META_MAX,
    _accepts_kwarg,
    _add_allocation,
    _allocated_bytes,
    _associated_backend,
    _associated_names,
    _associated_source_path,
    _attrs_with_colors,
    _frame_args,
    _inspect_path_series,
    _inspect_slide_series,
    _inspection_storage_details,
    _path_usage,
    _pixel_payload,
    _render_plate_frame_compat,
    _render_tile_compat,
    _series_item_is_stack,
    _storage_mode,
)
from .ui_settings import (
    list_recent_files,
    load_ui_settings,
    record_recent_file,
    save_ui_settings,
)
from .windows_fs import is_windows_name_surrogate_reparse

STATIC_DIR = Path(__file__).parent / "static"
TILE_RE = re.compile(r"^/api/tile/(\d+)/(\d+)/(\d+)\.(jpg|jpeg|png)$")
PLATE_FRAME_RE = re.compile(r"^/api/(?:plate/)?frame/(\d+)/(\d+)/(\d+)\.(jpg|jpeg|png)$")
ASSOCIATED_RE = re.compile(
    r"^/api/associated/(thumbnail|label|macro|associated-(?:[1-9]|1[0-6]))\.jpg$"
)
SLIDE_RE = re.compile(r"^/s/([0-9a-f]{8})(/.*)?$")
# A rendered ROI of this many pixels or more counts as an export for close
# and update. The viewer's scale-strip previews and size samples are at most
# 512 px a side; streamed ND2/TIFF exports always count.
EXPORT_COUNT_MIN_PIXELS = 4_000_000

# region-export progress, polled by the viewer's status bar
EXPORT_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _job_update(job: str | None, **kw) -> None:
    if not job:
        return
    now = time.time()
    with _JOBS_LOCK:
        for stale in [k for k, v in EXPORT_JOBS.items() if now - v.get("t", 0) > 600]:
            EXPORT_JOBS.pop(stale, None)
        d = EXPORT_JOBS.setdefault(job, {})
        d.update(kw)
        d["t"] = now


def _job_get(job: str) -> dict:
    with _JOBS_LOCK:
        d = dict(EXPORT_JOBS.get(job) or {})
    d.pop("t", None)
    return d or {"state": "unknown", "pct": 0}



_LIMND2_OK: bool | None = None


def _limnd2_available() -> bool:
    """True only when limnd2 actually imports (find_spec lies in bundles)."""
    global _LIMND2_OK
    if _LIMND2_OK is None:
        try:
            import limnd2  # noqa: F401

            _LIMND2_OK = True
        except Exception:
            _LIMND2_OK = False
    return _LIMND2_OK


# ---- UI settings & Recent files delegation -------------------------------

_ORIG_LOAD_UI_SETTINGS = load_ui_settings
_ORIG_SAVE_UI_SETTINGS = save_ui_settings
_ORIG_LIST_RECENT_FILES = list_recent_files
_ORIG_RECORD_RECENT_FILE = record_recent_file


def _ui_settings_fn(name: str, local_fn: Any, orig_fn: Any) -> Any:
    """Honor monkeypatches applied to either ``nd2wsi.server`` or ``nd2wsi.ui_settings``."""
    if local_fn is not orig_fn:
        return local_fn
    ui_mod = sys.modules.get("nd2wsi.ui_settings")
    return getattr(ui_mod, name, local_fn) if ui_mod is not None else local_fn


def _load_ui_settings_safe() -> dict[str, Any]:
    fn = _ui_settings_fn("load_ui_settings", load_ui_settings, _ORIG_LOAD_UI_SETTINGS)
    return dict(fn())


def _save_ui_settings_safe(patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ValueError("UI settings payload must be a JSON object")
    clean_patch = {k: v for k, v in patch.items() if k != "ok"}
    fn = _ui_settings_fn("save_ui_settings", save_ui_settings, _ORIG_SAVE_UI_SETTINGS)
    return dict(fn(clean_patch))


def _list_recent_files_safe(limit: int = 12) -> list[dict[str, Any]]:
    clamped = max(1, min(int(limit), 50))
    fn = _ui_settings_fn("list_recent_files", list_recent_files, _ORIG_LIST_RECENT_FILES)
    return list(fn(limit=clamped))


def _record_recent_file_safe(
    path: str | Path, *, name: str | None = None, window_role: str = "user"
) -> list[dict[str, Any]]:
    if window_role == "agent":
        return []
    fn = _ui_settings_fn("record_recent_file", record_recent_file, _ORIG_RECORD_RECENT_FILE)
    return list(fn(path, name=name, window_role=window_role))


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}


def reveal_in_file_manager(path: Path, *, platform: str | None = None, runner=None) -> None:
    """Reveal one registered path in Finder or Explorer without a shell."""
    import subprocess
    import sys

    platform = platform or sys.platform
    if platform not in ("darwin", "win32"):
        raise RuntimeError("Reveal is available on macOS and Windows")
    if not path.exists():
        raise FileNotFoundError(path)
    run = runner or subprocess.run
    if platform == "win32":
        # Explorer may return a nonzero status when handing off to an existing
        # window. Use the full SystemRoot path to prevent CWD binary hijacking.
        import ntpath

        explorer = ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows", "explorer.exe")
        run([explorer, "/select,", str(path.resolve())], check=False, timeout=10)
    else:
        run(["/usr/bin/open", "-R", "--", str(path.resolve())], check=True, timeout=10)


def content_disposition(filename: str) -> str:
    """An attachment header safe for any filename.

    HTTP headers travel as latin-1; a Korean or accented slide name used
    to raise UnicodeEncodeError after the export was already written. The
    ASCII fallback keeps old clients working and the RFC 5987 filename*
    carries the real name. Control characters (including CR/LF) are stripped
    to prevent response header injection.
    """
    import urllib.parse as _up

    clean = re.sub(r"[\x00-\x1f\x7f]+", "_", str(filename))
    ascii_name = (
        clean.encode("ascii", "replace")
        .decode("ascii")
        .replace('"', "_")
        .replace("\\", "_")
    )
    utf8_name = _up.quote(clean, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


__all__ = [
    "ASSOCIATED_RE",
    "AstromapConflict",
    "AstromapDownloads",
    "AstromapError",
    "EXPORT_COUNT_MIN_PIXELS",
    "EXPORT_JOBS",
    "ExportRequestError",
    "HTTP_MAX_WORKERS",
    "JOB_RE",
    "LeaseClosed",
    "MIME",
    "PLATE_FRAME_RE",
    "SLIDE_RE",
    "SLIDE_SUFFIXES",
    "STATIC_DIR",
    "SlideRegistry",
    "TILE_RE",
    "ViewerState",
    "annotations_sidecar",
    "confocal_annotations_sidecar",
    "content_disposition",
    "create_server",
    "is_windows_name_surrogate_reparse",
    "list_recent_files",
    "load_ui_settings",
    "make_handler",
    "plate_annotations_sidecar",
    "prepare_astromap",
    "prepare_export",
    "record_recent_file",
    "rescue_annotations",
    "reveal_in_file_manager",
    "save_ui_settings",
    "serve",
    "server_url",
    "store_generation",
]

# Private facade symbols kept on ``nd2wsi.server`` for tests and internal callers.
_FACADE_REEXPORTS = (
    _HEX6_RE,
    _Lifecycle,
    _SERIES_META_CACHE,
    _SERIES_META_LOCK,
    _SERIES_META_MAX,
    _accepts_kwarg,
    _add_allocation,
    _allocated_bytes,
    _associated_backend,
    _associated_names,
    _associated_source_path,
    _attrs_with_colors,
    _auto_record_allowed,
    _delete_verified_cache_file,
    _delete_verified_cache_tree,
    _frame_args,
    _inspect_path_series,
    _inspect_slide_series,
    _inspection_storage_details,
    _is_isolated_settings_env,
    _manifest_source_path,
    _path_usage,
    _pixel_payload,
    _render_plate_frame_compat,
    _render_tile_compat,
    _same_file_identity,
    _series_item_is_stack,
    _sidecar_items,
    _storage_mode,
    _validate_file_cache_for_trash,
)


_DISCONNECT_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "EPIPE",
        "ECONNRESET",
        "ECONNABORTED",
        "ENOTCONN",
        "EBADF",
        "ESHUTDOWN",
    )
    if hasattr(errno, name)
)
HTTP_MAX_WORKERS = 16


def _resolve_http_max_workers() -> int:
    raw = os.environ.get("ND2WSI_HTTP_MAX_WORKERS")
    if raw is not None and raw.strip() != "":
        try:
            val = int(raw)
        except ValueError:
            return HTTP_MAX_WORKERS
        if val >= 1:
            return val
    return HTTP_MAX_WORKERS


# pywebview 6's Window.evaluate_js() runs `eval(...)` inside macOS WKWebView's
# main world, so `script-src` must permit `'unsafe-eval'` while still forbidding
# `'unsafe-inline'`, remote/data/blob scripts, plugins, and cross-origin framing.
_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'; "
    "form-action 'self'"
)


def make_handler(
    registry: SlideRegistry, token: str = ""
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "nd2wsi"
        # Bound idle keep-alive sockets and stalled unauthenticated connections
        # so they cannot permanently occupy all ThreadPoolExecutor workers.
        timeout = 15.0
        # Set once a download has sent its status line; reset per request.
        _response_started = False

        def version_string(self) -> str:
            return self.server_version

        def send_error(
            self, code: int, message: str | None = None, explain: str | None = None
        ) -> None:
            # Never answer unauthenticated probes (OPTIONS/HEAD/PUT/DELETE or
            # malformed requests) with stdlib HTML or runtime version banners.
            self.close_connection = True
            if self._response_started:
                return
            if getattr(self, "command", None) == "HEAD":
                self._send(404, b"", "application/json")
            else:
                self._error(404, "not found")

        def handle_expect_100(self) -> bool:
            if getattr(self, "command", None) != "POST":
                self.close_connection = True
                self._error(404, "not found")
                return False
            if self._gate() is None:
                return False
            return super().handle_expect_100()

        def handle_one_request(self) -> None:
            if getattr(self.server, "_shutting_down", False):
                self.close_connection = True
                return
            try:
                super().handle_one_request()
            except (ConnectionError, TimeoutError):
                self.close_connection = True
            except OSError as exc:
                if exc.errno in _DISCONNECT_ERRNOS or getattr(
                    self.server, "_shutting_down", False
                ):
                    self.close_connection = True
                    return
                raise
            if getattr(self.server, "_shutting_down", False):
                self.close_connection = True

        def finish(self) -> None:
            try:
                super().finish()
            except (ConnectionError, TimeoutError):
                pass
            except OSError as exc:
                if exc.errno in _DISCONNECT_ERRNOS or getattr(
                    self.server, "_shutting_down", False
                ):
                    return
                raise

        # ---- helpers -----------------------------------------------------
        def _discard_rejected_body(self) -> None:
            """Discard at most 64 KiB / 250 ms after sending a rejection.

            Closing over unread bytes can reset a Windows TCP connection and
            discard the error response. The write side is already shut down,
            and this connection will not be reused: even bytes after an invalid
            or ambiguous body are discarded, never parsed as another request.
            """
            remaining = 65_536
            previous_timeout = self.connection.gettimeout()
            deadline = time.monotonic() + 0.25
            try:
                while remaining:
                    wait = deadline - time.monotonic()
                    if wait <= 0:
                        break
                    self.connection.settimeout(wait)
                    body = self.rfile.read1(min(remaining, 8192))
                    if not body:
                        break
                    remaining -= len(body)
            except OSError:
                pass  # a partial/slow body is dropped when this socket closes
            finally:
                self.connection.settimeout(previous_timeout)

        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str,
            extra: dict | None = None,
            cache: str = "no-store",
        ):
            rejecting_post = self.command == "POST" and code >= 300
            if rejecting_post or code >= 400:
                self.close_connection = True
            else:
                server = getattr(self, "server", None)
                active = getattr(server, "_active_sockets", None)
                max_workers = getattr(server, "max_workers", None)
                if (
                    active is not None
                    and isinstance(max_workers, int)
                    and max_workers > 2
                    and len(active) >= max_workers - 2
                ):
                    # Shed idle keep-alive sockets before the pool saturates.
                    self.close_connection = True
            # As in _stream_file: once the status line may be out, a failed
            # write cannot become an error reply inside this one's declared
            # length. _error drops the connection instead.
            self._response_started = True
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self._policy_headers(cache)
                if self.close_connection:
                    self.send_header("Connection", "close")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                if rejecting_post:
                    import socket

                    self.wfile.flush()
                    try:
                        self.connection.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    self._discard_rejected_body()
            except BaseException:
                self.close_connection = True
                raise

        def _policy_headers(self, cache: str) -> None:
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP_HEADER)

        def _stream_file(
            self,
            write_body,
            *,
            size: int,
            content_type: str,
            filename: str,
        ) -> None:
            """Send a 200 download whose body ``write_body(wfile)`` copies out.

            Once the status line is out, a failure can no longer become an
            error reply: it would land inside the declared Content-Length on
            a keep-alive connection. The connection is closed instead and the
            exception propagates, so the export's own context still sees it.
            """
            disposition = content_disposition(filename)
            self._response_started = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", disposition)
                self._policy_headers("no-store")
                self.end_headers()
                write_body(self.wfile)
            except BaseException:
                self.close_connection = True
                raise

        def _json(self, obj: Any, code: int = 200):
            try:
                body = json.dumps(obj, allow_nan=False).encode()
            except ValueError:
                body = json.dumps(
                    {"error": "internal server error: non-finite value in response"}
                ).encode()
                code = 500
            self._send(code, body, "application/json")

        def _error(self, code: int, msg: str):
            if self._response_started:
                # A download already sent its status and length; drop the
                # connection rather than write JSON into its body.
                self.close_connection = True
                return
            self._json({"error": msg}, code)

        def _body_json(self, limit: int = 10_000_000) -> Any:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError("expected one Content-Length and no Transfer-Encoding")
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= limit:
                raise ValueError("payload missing or too large")
            return json.loads(
                self.rfile.read(n),
                parse_constant=lambda c: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant {c} is not allowed")
                ),
            )

        def log_message(self, fmt, *args):  # quieter default log
            pass

        def _resolve(self, path: str) -> tuple[ViewerState | None, str]:
            """Map a URL path to (slide state, slide-relative path)."""
            m = SLIDE_RE.match(path)
            if m:
                st = registry.get(m.group(1))
                return st, (m.group(2) or "/")
            if path.startswith("/api/"):  # bare API -> first slide (scripts)
                return registry.get(None), path
            return None, path

        # ---- routing -----------------------------------------------------
        def _gate(self) -> str | None:
            """Strip and verify the capability prefix; None means rejected.

            Every route lives under /<token>/. A request without the right
            token gets a bare 404 — tiles, downloads and API alike — so a
            local process or a web page cannot even probe the server. The
            Host header must name loopback (and this server's port if given),
            and any Origin present must match this server's exact origin.
            """
            if getattr(self, "request_version", "") == "HTTP/0.9":
                self.close_connection = True
                self._error(404, "not found")
                return None
            parsed = urllib.parse.urlparse(self.path)
            if parsed.scheme or parsed.netloc:
                self.close_connection = True
                self._error(404, "not found")
                return None
            path = parsed.path
            server_port = getattr(getattr(self, "server", None), "server_port", None)
            if not isinstance(server_port, int) or server_port <= 0:
                server_port = None
            get_all = getattr(self.headers, "get_all", None)
            host_headers = get_all("Host", []) if get_all is not None else []
            raw_host = (host_headers[0] if len(host_headers) == 1 else "").strip()
            host: str | None = None
            host_port: int | None = None
            if raw_host.startswith("["):  # bracketed IPv6, maybe with a port
                if "]" in raw_host:
                    bracket, rest = raw_host.split("]", 1)
                    if rest == "":
                        host = (bracket + "]").lower()
                    elif rest.startswith(":") and rest[1:].isascii() and rest[1:].isdigit():
                        host = (bracket + "]").lower()
                        host_port = int(rest[1:])
            elif ":" in raw_host:
                h, port = raw_host.rsplit(":", 1)
                if h != "" and port.isascii() and port.isdigit():
                    host = h.lower()
                    host_port = int(port)
            elif raw_host:
                host = raw_host.lower()
            if host not in ("127.0.0.1", "localhost", "[::1]") or (
                host_port is not None and server_port is not None and host_port != server_port
            ):
                self.close_connection = True
                self._error(404, "not found")
                return None
            origin_headers = get_all("Origin", []) if get_all is not None else (
                [self.headers.get("Origin")] if self.headers.get("Origin") else []
            )
            if len(origin_headers) > 1:
                self.close_connection = True
                self._error(404, "not found")
                return None
            if origin_headers:
                try:
                    parsed_origin = urllib.parse.urlparse(origin_headers[0])
                    origin_port = parsed_origin.port
                except ValueError:
                    self.close_connection = True
                    self._error(404, "not found")
                    return None
                effective_origin_port = 80 if origin_port is None else origin_port
                if (
                    parsed_origin.scheme != "http"
                    or parsed_origin.hostname not in ("127.0.0.1", "localhost", "::1")
                    or (server_port is not None and effective_origin_port != server_port)
                ):
                    self.close_connection = True
                    self._error(404, "not found")
                    return None
            if not token:
                return path
            if not path.startswith("/"):
                self.close_connection = True
                self._error(404, "not found")
                return None
            seg, sep, tail = path[1:].partition("/")
            if not hmac.compare_digest(seg, token):
                self.close_connection = True
                self._error(404, "not found")
                return None
            if not sep:
                # redirect rather than rewrite: relative asset and API URLs
                # only resolve when the document URL ends with the slash
                target = f"/{token}/"
                if parsed.query:
                    target += "?" + parsed.query
                self._send(
                    HTTPStatus.PERMANENT_REDIRECT, b"", "text/plain",
                    {"Location": target},
                )
                return None
            return "/" + tail

        def do_GET(self):  # noqa: N802 (http.server API)
            self._response_started = False
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = self._gate()
                if path is None:
                    return
                q = urllib.parse.parse_qs(parsed.query)
                if path == "/":
                    return self._static("shell.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/") :])
                if path == "/api/slides":
                    return self._json({"slides": registry.listing()})
                if path == "/api/ui-settings" or path.endswith("/api/ui-settings"):
                    return self._ui_settings_get()
                if path == "/api/recent-files" or path.endswith("/api/recent-files"):
                    return self._recent_files_get(q)
                if path == "/api/roi/progress" or path.endswith("/api/roi/progress"):
                    job = (q.get("job") or [""])[0]
                    if not JOB_RE.match(job):
                        return self._error(400, "bad job id")
                    return self._json(_job_get(job))
                if path == "/favicon.ico":
                    return self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")

                st, sub = self._resolve(path)
                if sub == "/" and st is not None:
                    return self._static("index.html")
                if st is None:
                    return self._error(404, f"no slide for {path}")
                if sub == "/api/info":
                    return self._info(st)
                if sub == "/api/lut-settings":
                    return self._json(registry.lut_settings_for(st).read())
                if sub == "/api/inspect":
                    return self._inspect(st)
                if sub in ("/api/pixel", "/api/histogram"):
                    try:
                        with st.busy:
                            return self._pixel(st, q) if sub.endswith("pixel") else self._histogram(st, q)
                    except ValueError as e:
                        return self._error(409, str(e))
                if sub == "/api/shading/status":
                    try:
                        controller = st.shading
                    except ValueError:
                        if not st._shading_closed:
                            raise
                        # Resolved before the close, which came before any
                        # controller: answer as a closed controller does.
                        from .h_correction import closed_status

                        return self._json(closed_status(st))
                    return self._json(controller.status())
                if sub == "/api/annotations":
                    return self._annotations_get(st, q)
                associated = ASSOCIATED_RE.match(sub)
                if associated:
                    return self._associated(st, associated.group(1), q)
                m = TILE_RE.match(sub)
                if m:
                    return self._tile(st, m, q)
                m = PLATE_FRAME_RE.match(sub)
                if m:
                    return self._plate_frame(st, m, q)
                if sub == "/api/plate/status":
                    if st.plate is None:
                        return self._error(404, "not a plate slide")
                    return self._json(st.plate.status())
                if sub == "/api/plate/focus":
                    if st.plate is None:
                        return self._error(404, "not a plate slide")
                    return self._json(st.plate.focus_map())
                if sub in ("/api/roi", "/api/region"):
                    return self._roi(st, q)
                if sub == "/api/astromap/download":
                    return self._astromap_download(st, q)
                return self._error(404, f"no route for {path}")
            except ConnectionError:
                # BrokenPipe on macOS, ConnectionReset/Aborted on Windows: the
                # client left, so nothing more can be sent on this socket.
                self.close_connection = True
            except Exception as e:  # pragma: no cover - defensive
                try:
                    self._error(500, f"{type(e).__name__}: {e}")
                except Exception:
                    pass

        def do_POST(self):  # noqa: N802 (http.server API)
            self._response_started = False
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = self._gate()
                if path is None:
                    return
                q = urllib.parse.parse_qs(parsed.query)
                ctype = (self.headers.get("Content-Type") or "").split(";")[0]
                if ctype.strip().lower() != "application/json":
                    return self._error(415, "expected application/json")
                if path == "/api/open":
                    return self._open()
                if path == "/api/close":
                    return self._close()
                if path == "/api/trash":
                    return self._trash()
                if path == "/api/ui-settings" or path.endswith("/api/ui-settings"):
                    return self._ui_settings_post()
                if path == "/api/recent-files" or path.endswith("/api/recent-files"):
                    return self._recent_files_post()
                st, sub = self._resolve(path)
                if st is not None and sub == "/api/select-scene":
                    return self._select_scene(st)
                if st is not None and sub in ("/api/shading/start", "/api/shading/revoke", "/api/shading/cancel"):
                    try:
                        data = self._body_json(limit=1000)
                        allowed = ({"generation", "method", "experimental_confirmed"}
                                   if sub == "/api/shading/start" else {"generation"})
                        if (not isinstance(data, dict) or "generation" not in data
                                or set(data) - allowed or not isinstance(data["generation"], str)):
                            raise ValueError("expected the current slide generation")
                        if sub == "/api/shading/start":
                            if data.get("method", "bffree") not in ("h", "bffree"):
                                raise ValueError("Unknown Shading Correction method")
                            if ("experimental_confirmed" in data
                                    and not isinstance(data["experimental_confirmed"], bool)):
                                raise ValueError("Experimental confirmation must be a boolean")
                    except (ValueError, json.JSONDecodeError) as e:
                        return self._error(400, str(e))
                    try:
                        controller = st.shading
                        current = controller.status()
                        if data["generation"] != current["generation"]:
                            return self._error(409, "The pyramid changed; refresh before continuing")
                        if not st.correction_write_allowed:
                            return self._error(403, "Agent windows can replace pyramids only in their private exports folder")
                        if sub == "/api/shading/start":
                            if "method" not in data:
                                return self._json(controller.start())
                            return self._json(controller.start(data["method"],
                                experimental_confirmed=data.get("experimental_confirmed", False)))
                        action = {"/api/shading/revoke": controller.revoke,
                                  "/api/shading/cancel": controller.cancel}[sub]
                        return self._json(action())
                    except ValueError as e:
                        return self._error(409, str(e))
                if st is not None and sub == "/api/lut-settings":
                    return self._lut_settings_post(st)
                if st is not None and sub == "/api/annotations":
                    return self._annotations_post(st, q)
                if st is not None and sub == "/api/astromap":
                    return self._astromap(st)
                if st is not None and sub == "/api/reveal":
                    return self._reveal(st)
                return self._error(404, f"no POST route for {path}")
            except ConnectionError:
                self.close_connection = True
            except Exception as e:  # pragma: no cover - defensive
                try:
                    self._error(500, f"{type(e).__name__}: {e}")
                except Exception:
                    pass

        # ---- global endpoints --------------------------------------------
        def _static(self, rel: str):
            file = (STATIC_DIR / rel).resolve()
            if not file.is_relative_to(STATIC_DIR.resolve()) or not file.is_file():
                return self._error(404, "not found")
            st = file.stat()
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            if self.headers.get("If-None-Match") == etag:
                return self._send(
                    HTTPStatus.NOT_MODIFIED, b"", "text/plain",
                    {"ETag": etag}, cache="private, no-cache",
                )
            body = file.read_bytes()
            self._send(
                200, body, MIME.get(file.suffix, "application/octet-stream"),
                {"ETag": etag}, cache="private, no-cache",
            )

        def _open(self):
            job = None
            try:
                data = self._body_json()
                path = data.get("path") if isinstance(data, dict) else None
                if not isinstance(path, str) or not path.strip():
                    return self._error(400, 'expected {"path": "..."}')
                has_series = isinstance(data, dict) and data.get("series") is not None
                series_arg: int | None = None
                if has_series:
                    raw_s = data["series"]
                    if isinstance(raw_s, bool) or not isinstance(raw_s, int):
                        return self._error(400, "series must be an integer")
                    series_arg = int(raw_s)
                job = data.get("job") if isinstance(data, dict) else None
                if job and not JOB_RE.match(str(job)):
                    job = None
                on_progress = None
                if job:
                    _job_update(job, state="converting", pct=0)

                    def on_progress(frac: float) -> None:
                        _job_update(
                            job, state="converting", pct=int(min(1.0, frac) * 100)
                        )

                if has_series:
                    sid = registry.open_path(path, on_progress=on_progress, series=series_arg)
                else:
                    sid = registry.open_path(path, on_progress=on_progress)
                if job:
                    _job_update(job, state="done", pct=100)
            except ConnectionError:
                raise
            except (
                ValueError,
                FileNotFoundError,
                OSError,
                NotImplementedError,
                RuntimeError,
            ) as e:
                _job_update(job, state="error", error=str(e))
                return self._error(400, str(e))
            except json.JSONDecodeError as e:
                return self._error(400, f"invalid JSON: {e}")
            return self._json({"sid": sid, "slides": registry.listing()})

        def _ui_settings_get(self):
            if registry.agent_window:
                with registry._agent_ui_lock:
                    agent_ui = getattr(registry, "_agent_ui_settings", None)
                    if agent_ui is None:
                        agent_ui = dict(_load_ui_settings_safe())
                        registry._agent_ui_settings = agent_ui
                    snapshot = dict(agent_ui)
                return self._json({"ok": True, **snapshot})
            settings = _load_ui_settings_safe()
            return self._json({"ok": True, **settings})

        def _ui_settings_post(self):
            try:
                data = self._body_json(limit=65_536)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                if registry.agent_window:
                    from . import ui_settings as _ui_mod

                    clean = {k: v for k, v in data.items() if k != "ok"}
                    with registry._agent_ui_lock:
                        current = getattr(registry, "_agent_ui_settings", None)
                        if current is None:
                            current = dict(_load_ui_settings_safe())
                        doc = {
                            "theme": current.get("theme", "auto"),
                            "panels": dict(current.get("panels") or {}),
                            "shellToolsExpanded": current.get("shellToolsExpanded"),
                            "recentFiles": [],
                        }
                        updated = _ui_mod._public_ui_settings(_ui_mod._apply_ui_patch(doc, clean))
                        registry._agent_ui_settings = dict(updated)
                    return self._json({"ok": True, **updated})
                updated = _save_ui_settings_safe(data)
                return self._json({"ok": True, **updated})
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            except OSError as e:
                return self._error(500, f"Could not save UI settings: {e}")

        def _recent_files_get(self, q: dict):
            if registry.agent_window:
                return self._json({"ok": True, "items": []})
            raw_limit = (q.get("limit") or ["12"])[0]
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 12
            items = _list_recent_files_safe(limit=limit)
            return self._json({"ok": True, "items": items})

        def _recent_files_post(self):
            try:
                data = self._body_json(limit=16_384)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                raw_path = data.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    raise ValueError('expected {"path": "..."}')
                if registry.agent_window:
                    return self._json({"ok": True, "items": []})
                candidate = Path(raw_path).expanduser().resolve()
                with registry._lock:
                    slide_states = list(registry.slides.values())
                registered = {
                    p.resolve()
                    for st in slide_states
                    for p in (st.source_path, st.store_path, st.container_path)
                    if p is not None
                }
                allowed_suffixes = {
                    *SLIDE_SUFFIXES,
                    ".tif", ".tiff", ".qptiff", ".svs", ".sdpc", ".nd2svs",
                    ".jpg", ".jpeg", ".png", ".zarr",
                }
                if candidate not in registered and candidate.suffix.lower() not in allowed_suffixes:
                    raise ValueError("path is not a supported slide or registered store")
                raw_name = data.get("name")
                name = str(raw_name) if isinstance(raw_name, str) and raw_name.strip() else None
                items = _record_recent_file_safe(candidate, name=name, window_role="user")
                return self._json({"ok": True, "items": items})
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            except OSError as e:
                return self._error(500, f"Could not record recent file: {e}")

        def _select_scene(self, st: ViewerState):
            try:
                data = self._body_json(limit=10_000)
                if not isinstance(data, dict) or "series" not in data:
                    raise ValueError('expected {"series": <int>}')
                raw_series = data["series"]
                if isinstance(raw_series, bool) or not isinstance(raw_series, int):
                    raise ValueError("series must be an integer")
                target_series = int(raw_series)
                if st.source_path is None or not st.source_path.is_file():
                    raise ValueError("original slide source is unavailable for scene selection")
                series_count, _cur_idx, series_list = _inspect_slide_series(st)
                if not (0 <= target_series < series_count):
                    raise ValueError(
                        f"series {target_series} out of range (0..{series_count - 1})"
                    )
                new_sid = registry.open_path(st.source_path, series=target_series)
                new_st = registry.get(new_sid)
                base_name = (
                    new_st.attrs["nd2wsi"]["source"]
                    if new_st is not None
                    else st.source_path.name
                )
                scene_name = (
                    series_list[target_series]["name"]
                    if target_series < len(series_list)
                    else ""
                )
                display_name = (
                    f"{base_name} · {scene_name}"
                    if (series_count > 1 and scene_name)
                    else base_name
                )
                return self._json(
                    {
                        "ok": True,
                        "sid": new_sid,
                        "name": display_name,
                        "url": f"/s/{new_sid}/",
                    }
                )
            except ConnectionError:
                raise
            except (
                ValueError,
                FileNotFoundError,
                OSError,
                NotImplementedError,
                RuntimeError,
                json.JSONDecodeError,
            ) as e:
                return self._error(400, str(e))

        def _trash(self):
            if registry.window_session is not None:
                return self._error(403, "Shared cache removal is unavailable in multiwindow mode; another window may still be reading it")
            try:
                data = self._body_json(limit=10_000)
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            sid = data.get("sid") if isinstance(data, dict) else None
            job = data.get("job") if isinstance(data, dict) else None
            if job and not JOB_RE.match(str(job)):
                job = None
            on_progress = None
            if job:
                _job_update(job, state="deleting", pct=0)

                def on_progress(frac: float) -> None:
                    _job_update(job, state="deleting", pct=int(min(1.0, frac) * 100))

            try:
                freed = registry.trash_cache(sid or "", on_progress=on_progress)
                if job:
                    _job_update(job, state="done", pct=100)
            except KeyError:
                return self._error(404, f"no open slide {sid}")
            except (ValueError, OSError) as e:
                return self._error(400, str(e))
            return self._json(
                {"ok": True, "freed": freed, "slides": registry.listing()}
            )

        def _close(self):
            try:
                data = self._body_json(limit=10_000)
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            sid = data.get("sid") if isinstance(data, dict) else None
            try:
                if not sid or not registry.remove(sid):
                    return self._error(404, f"no open slide {sid}")
            except ValueError as e:
                return self._error(409, str(e))
            return self._json({"ok": True, "slides": registry.listing()})

        # ---- per-slide endpoints -----------------------------------------
        def _lut_settings_post(self, st: ViewerState):
            from .lut_settings import MAX_BYTES, LutSettingsUnavailable

            try:
                payload = self._body_json(limit=MAX_BYTES)
                return self._json(registry.lut_settings_for(st).write(payload))
            except LutSettingsUnavailable as exc:
                return self._error(409, str(exc))
            except (ValueError, TypeError, RecursionError) as exc:
                return self._error(400, str(exc))
            except OSError as exc:
                return self._error(500, f"Could not save LUT settings: {exc}")

        def _info_payload(self, st: ViewerState) -> dict[str, Any]:
            meta = st.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            is_raster = bool(meta.get("raster", False))
            is_overview_degraded = _storage_mode(meta) == "overview-degraded"
            try:
                dtype_name = np.dtype(meta["dtype"]).name
            except Exception:
                dtype_name = str(meta.get("dtype") or "")
            limnd2_ok = _limnd2_available()
            nd2_export = bool(
                limnd2_ok
                and not is_raster
                and not is_overview_degraded
                and dtype_name in ("uint8", "uint16")
            )
            if nd2_export:
                nd2_export_reason = ""
            elif is_raster:
                nd2_export_reason = (
                    "ND2 export is unavailable for JPEG and PNG images; use TIFF export instead."
                )
            elif is_overview_degraded:
                nd2_export_reason = (
                    "ND2 export requires the original full-resolution slide; use TIFF export instead."
                )
            elif dtype_name not in ("uint8", "uint16"):
                nd2_export_reason = (
                    f"ND2 export supports 8-bit and 16-bit integer images (this slide is {dtype_name}); "
                    "use TIFF export to preserve native values."
                )
            else:
                nd2_export_reason = (
                    "ND2 export requires the optional limnd2 package; use TIFF export instead."
                )
            series_count, series_index, series_list = _inspect_slide_series(st)
            return {
                "name": meta["source"],
                **({"window": registry.window_session.as_dict()} if registry.window_session is not None else {}),
                "width": lv0["width"],
                "height": lv0["height"],
                "tileSize": meta["tile"],
                "dtype": meta["dtype"],
                "rgb": meta["rgb"],
                "pixelSizeUm": meta.get("pixel_size_um"),
                "calibrated": bool(meta.get("pixel_size_um")),
                "levels": meta["levels"],
                "selection": meta.get("selection", {}),
                "notes": meta.get("notes", []),
                "channels": [
                    {
                        "label": ch["label"],
                        "color": ch["color"],
                        "window": ch["window"],
                    }
                    for ch in st.attrs["omero"]["channels"]
                ],
                "maxRenderMpx": st.max_render_mpx,
                "nd2Export": nd2_export,
                "nd2ExportReason": nd2_export_reason,
                "raster": is_raster,
                "direct": bool(meta.get("direct")),
                "trashable": st.trash_path is not None and registry.window_session is None,
                "generation": st.generation,
                "storage": _storage_mode(meta),
                "kind": "plate" if meta.get("plate") else "slide",
                "plate": self._plate_block(st),
                "seriesCount": series_count,
                "seriesIndex": series_index,
                "seriesList": series_list,
            }

        def _plate_block(self, st: ViewerState) -> dict[str, Any] | None:
            meta = st.attrs["nd2wsi"]
            block = meta.get("plate")
            if not block or st.plate is None:
                return block
            status = st.plate.status()
            return {
                **block,
                "thumbsDone": status["done"],
                "thumbsTotal": status["total"],
                "cachePath": status["path"],
            }

        def _info(self, st: ViewerState):
            self._json(self._info_payload(st))

        def _inspect(self, st: ViewerState):
            meta = st.attrs["nd2wsi"]
            source_usage = _path_usage(st.source_path)
            cache_usage = st.cache_usage()
            storage_details = _inspection_storage_details(st, meta)
            calibration = dict(meta.get("calibration") or {})
            calibration.setdefault("status", "unknown")
            calibration.setdefault("source", "unknown")
            calibration["pixel_size_um"] = meta.get("pixel_size_um")
            from .reader import _finite_positive

            mag = meta.get("objective_magnification")
            clean_mag = mag if _finite_positive(mag) else None
            payload = {
                **self._info_payload(st),
                "source_path": str(st.source_path) if st.source_path else None,
                "store_path": str(st.store_path) if st.store_path else None,
                "container_path": (
                    str(st.container_path) if st.container_path else None
                ),
                "cache_path": str(st.cache_path) if st.cache_path else None,
                "source_bytes": source_usage["bytes"] if source_usage else None,
                "source_allocated_bytes": (
                    source_usage["allocated_bytes"] if source_usage else None
                ),
                "cache_bytes": cache_usage["bytes"] if cache_usage else None,
                "cache_allocated_bytes": (
                    cache_usage["allocated_bytes"] if cache_usage else None
                ),
                "storage_details": storage_details,
                "calibration": calibration,
                "objective": clean_mag,
                "objective_magnification": clean_mag,
                "selection": dict(meta.get("selection") or {}),
                "associated": _associated_names(st),
            }
            self._json(payload)

        def _pixel(self, st: ViewerState, q: dict):
            try:
                x_raw = (q.get("x") or [None])[0]
                y_raw = (q.get("y") or [None])[0]
                if x_raw is None or y_raw is None:
                    raise ValueError("missing parameter x or y")
                x, y = float(x_raw), float(y_raw)
                if not math.isfinite(x) or not math.isfinite(y):
                    raise ValueError("x and y must be finite numbers")
                frame = _frame_args(st, q, require_p=True)
                root = st.plate.root_for(*frame) if frame is not None else None
                payload = _pixel_payload(st, x, y, root=root)
                payload["generation"] = st.generation
                payload["frame"] = (
                    {"t": frame[0], "p": frame[1], "z": frame[2]}
                    if frame is not None
                    else None
                )
            except (TypeError, ValueError) as e:
                return self._error(400, str(e))
            self._json(payload)

        def _associated(self, st: ViewerState, name: str, q: dict):
            source = _associated_source_path(st)
            if source is None:
                return self._error(404, "associated image source is unavailable")
            try:
                backend = _associated_backend(source)
                # Two bounded display sizes; clients cannot request arbitrary
                # decode sizes or address a source outside the open slide.
                if q.get("detail") == ["1"]:
                    body = backend.associated_image_jpeg(source, name, max_side=2048)
                else:
                    body = backend.associated_image_jpeg(source, name)
            except (KeyError, OSError, ValueError):
                return self._error(404, f"associated image {name} is unavailable")
            # The path may have been atomically replaced while it was decoded.
            # In that case discard the body instead of mixing auxiliary pixels
            # from a different file with the already-open primary slide root.
            if _associated_source_path(st) != source:
                return self._error(404, "associated image source changed")
            self._send(200, body, "image/jpeg")

        def _reveal(self, st: ViewerState):
            import subprocess

            try:
                data = self._body_json(limit=1000)
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            which = data.get("which") if isinstance(data, dict) else None
            if which not in ("source", "cache"):
                return self._error(400, 'expected {"which": "source"|"cache"}')
            target = st.source_path if which == "source" else st.cache_path
            if target is None or not target.exists():
                return self._error(404, f"registered {which} path is unavailable")
            try:
                reveal_in_file_manager(target)
            except FileNotFoundError:
                return self._error(404, f"registered {which} path is unavailable")
            except RuntimeError as e:
                return self._error(HTTPStatus.NOT_IMPLEMENTED, str(e))
            except (OSError, subprocess.SubprocessError) as e:
                return self._error(500, f"could not reveal {which}: {e}")
            self._json({"ok": True, "which": which})

        def _plate_site(self, st: ViewerState, q: dict) -> tuple[Path | None, int | None]:
            """(sidecar path, site index) for the annotations routes."""
            if st.plate is None:
                return st.annotations_path, None
            if st.attrs["nd2wsi"]["plate"].get("annotationScope") == "plane":
                frame = _frame_args(st, q, require_p=True)
                if st.source_path is None:
                    return None, frame[1]
                return registry.annotation_path(st.source_path, st.attrs, frame=frame), frame[1]
            raw = (q.get("p") or [None])[0]
            if st.plate.P > 1 and (raw is None or raw == ""):
                raise ValueError("p is required; choose a plate site first")
            if raw is None or raw == "":
                raw = "0"
            try:
                p = int(raw)
            except (TypeError, ValueError):
                raise ValueError("p must be an integer") from None
            if not 0 <= p < st.plate.P:
                raise ValueError(f"p={p} out of range (0..{st.plate.P - 1})")
            if st.source_path is None:
                return None, p
            return registry.annotation_path(st.source_path, st.attrs, site=p), p

        def _annotations_get(self, st: ViewerState, q: dict | None = None):
            try:
                p, _site = self._plate_site(st, q or {})
            except ValueError as e:
                return self._error(400, str(e))
            if p is None:
                return self._json({"items": [], "path": None})
            lv0 = st.attrs["nd2wsi"]["levels"][0]

            def _same_geometry(document: Any) -> None:
                src = document.get("source") if isinstance(document, dict) else None
                if not isinstance(src, dict):
                    return
                w, h = src.get("width"), src.get("height")
                if (
                    isinstance(w, int)
                    and not isinstance(w, bool)
                    and isinstance(h, int)
                    and not isinstance(h, bool)
                    and (w, h) != (int(lv0["width"]), int(lv0["height"]))
                ):
                    raise ValueError(
                        f"these annotations were drawn on a {w} x {h} image; this image is "
                        f"{lv0['width']} x {lv0['height']}"
                    )

            # A sidecar that is not an object with an items list is refused,
            # never read as empty: the pane's next save would replace it.
            if registry.annotation_workspace is not None:
                try:
                    document, rev = registry.annotation_workspace.read(p)
                    items = _sidecar_items(document)
                    _same_geometry(document)
                    return self._json({"items": items, "path": str(p),
                                       "revision": rev, "window": registry.window_session.as_dict()})
                except (ValueError, UnicodeError, OSError, TimeoutError) as e:
                    return self._error(500, f"could not read {p.name}: {e}")
            if p.exists() or p.is_symlink():
                try:
                    from .annotation_workspace import _read

                    raw_bytes = _read(p)
                    if raw_bytes is None:
                        items = []
                    else:
                        document = json.loads(raw_bytes.decode("utf-8"))
                        items = _sidecar_items(document, require_items=True)
                        _same_geometry(document)
                except (ValueError, UnicodeError, OSError) as e:
                    return self._error(500, f"could not read {p.name}: {e}")
            else:
                items = []
            self._json({"items": items, "path": str(p)})

        def _annotations_post(self, st: ViewerState, q: dict | None = None):
            try:
                p, site = self._plate_site(st, q or {})
            except ValueError as e:
                return self._error(400, str(e))
            if p is None:
                return self._error(400, "no annotation sidecar path for this store")
            try:
                data = self._body_json()
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return self._error(400, "expected {\"items\": [...]}")
            meta = st.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            cal = meta.get("calibration", {})
            ps = meta.get("pixel_size_um")
            payload = {
                "format": "nd2wsi-annotations/2",
                "coordinate_space": "level-0-pixels",
                "source": {
                    "name": meta["source"],
                    "width": lv0["width"],
                    "height": lv0["height"],
                },
                "calibration": {
                    "status": cal.get("status", "unknown"),
                    "source": cal.get("source", "unknown"),
                    **(
                        {"y_um_per_px": ps[0], "x_um_per_px": ps[1]}
                        if ps
                        else {}
                    ),
                },
                "selection": meta.get("selection", {}),
                "items": data["items"],
            }
            if site is not None:
                if meta["plate"].get("annotationScope") == "plane":
                    t, site, z = _frame_args(st, q or {}, require_p=True)
                    payload["selection"] = {"t": t, "p": site, "z": z}
                else:
                    payload["selection"] = {"p": site}
                sites = (meta.get("plate") or {}).get("sites") or []
                payload["site"] = sites[site]["name"] if site < len(sites) else None
            try:
                raw = json.dumps(payload, indent=1, allow_nan=False).encode("utf-8")
            except ValueError as e:
                return self._error(400, str(e))
            if registry.annotation_workspace is not None:
                from .annotation_workspace import AnnotationConflict
                try:
                    rev = registry.annotation_workspace.write(p, payload, data.get("expected_revision"), source_path=st.source_path)
                except AnnotationConflict as e:
                    return self._json({"ok": False, "error": str(e), "conflict": True,
                                       "current_revision": e.current_revision,
                                       "draft_path": str(e.draft_path) if e.draft_path else None,
                                       "draft_error": e.draft_error}, code=409)
                except (ValueError, UnicodeError, OSError, TimeoutError) as e:
                    return self._error(500, f"could not save {p.name}: {e}")
                return self._json({"ok": True, "path": str(p), "count": len(data["items"]), "revision": rev})
            from contextlib import ExitStack

            from .annotation_workspace import (
                _atomic_write,
                annotation_lock,
                annotation_lock_path,
            )

            # unique, fsynced temp + per-slide and window lock: two tabs or
            # windows saving at once cannot interleave or clobber each other.
            # The window lock is advisory: when it cannot be used here (another
            # account's file, no flock on this filesystem, ...) the atomic
            # write proceeds as in 2.6.2; only real contention is refused.
            lock_path = annotation_lock_path(p)
            failure: tuple[int, str] | None = None
            with st.lock, ExitStack() as held:
                if Path(filesystem_path(lock_path)).exists():
                    try:
                        held.enter_context(annotation_lock(p))
                    except TimeoutError as e:
                        if e.errno is None:  # SessionFileLock contention, not ETIMEDOUT
                            failure = (409, "annotations are open in another window")
                    except OSError:
                        pass
                if failure is None:
                    try:
                        _atomic_write(p, raw)
                    except OSError as e:
                        failure = (500, f"could not save {p.name}: {e}")
            if failure is not None:
                code, message = failure
                if code == 409:
                    return self._json({"error": message}, code=409)
                return self._error(code, message)
            self._json({"ok": True, "path": str(p), "count": len(data["items"])})

        def _histogram(self, st: ViewerState, q: dict | None = None):
            if st.plate is not None:
                try:
                    t, p, z = _frame_args(st, q or {}, require_p=True)
                except ValueError as e:
                    return self._error(400, str(e))
                return self._json(
                    {
                        "channels": st.plate.histogram(t, p, z),
                        "generation": st.generation,
                        "frame": {"t": t, "p": p, "z": z},
                    }
                )
            # Its own lock: LUT settings and annotation saves on st.lock never
            # wait for a histogram. Requests hold busy, and a replacement
            # drains busy before it resets histograms.
            with st._hist_lock:
                if st.histograms is None:
                    st.histograms = render.compute_histograms(st.root, st.attrs)
            self._json(
                {"channels": st.histograms, "generation": st.generation, "frame": None}
            )

        def _immutable_when_current(self, st: ViewerState, q: dict) -> str:
            # a URL that names the cache generation can be cached hard: a
            # rebuild mints a new generation, hence new URLs, so a stale
            # render can never be revived from the browser cache
            gen = (q.get("g") or [None])[0]
            return (
                "private, max-age=31536000, immutable"
                if gen and gen == st.generation
                else "no-store"
            )

        def _tile(self, st: ViewerState, m: re.Match, q: dict):
            try:
                with st.busy:
                    generation = (q.get("g") or [None])[0]
                    if generation and generation != st.generation:
                        return self._error(409, "The pyramid changed; refresh the slide")
                    if q.get("correction"):
                        return self._error(409, "Shading Correction now replaces the pyramid; refresh the slide")
                    return self._tile_impl(st, m, q)
            except ValueError as e:
                return self._error(409, str(e))

        def _tile_impl(self, st: ViewerState, m: re.Match, q: dict):
            level, tx, ty = int(m.group(1)), int(m.group(2)), int(m.group(3))
            fmt = m.group(4)
            n = len(st.attrs["omero"]["channels"])
            try:
                channels = render.parse_channels((q.get("c") or [None])[0], n)
            except ValueError as e:
                return self._error(400, str(e))
            win = (q.get("win") or [None])[0]
            col = (q.get("col") or [None])[0]
            try:
                frame = _frame_args(st, q)
            except ValueError as e:
                return self._error(400, str(e))
            root = st.plate.root_for(*frame) if frame is not None else st.root
            try:
                body = _render_tile_compat(
                    root, st.attrs, level, tx, ty, channels, fmt, win, col
                )
            except KeyError as e:
                return self._error(404, str(e))
            except ValueError as e:
                return self._error(409, str(e))
            ctype = "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"
            self._send(200, body, ctype, cache=self._immutable_when_current(st, q))
            if frame is not None and st.plate is not None and hasattr(st.plate, "prefetch_neighbors"):
                try:
                    st.plate.prefetch_neighbors(*frame)
                except Exception:  # pragma: no cover - prefetch never reaches a client
                    pass

        def _plate_frame(self, st: ViewerState, m: re.Match, q: dict):
            """A reduced frame of one site for the grid."""
            plate = st.plate
            if plate is None:
                return self._error(404, "not a plate slide")
            t, p, z = int(m.group(1)), int(m.group(2)), int(m.group(3))
            fmt = m.group(4)
            if not (t < plate.T and p < plate.P and z < plate.Z):
                return self._error(404, "frame out of range")
            raw_k = (q.get("k") or ["8"])[0]
            try:
                k = int(raw_k)
            except (TypeError, ValueError):
                k = -1
            if k not in (2, 4, 8, 16):
                return self._error(400, "k must be 2, 4, 8 or 16")
            n = len(st.attrs["omero"]["channels"])
            try:
                channels = render.parse_channels((q.get("c") or [None])[0], n)
            except ValueError as e:
                return self._error(400, str(e))
            win = (q.get("win") or [None])[0]
            col = (q.get("col") or [None])[0]
            try:
                body = _render_plate_frame_compat(plate, t, p, z, k, channels, fmt, win, col)
            except ValueError as e:
                if "closed" in str(e):
                    return self._error(409, "slide was closed")
                return self._error(400, str(e))
            ctype = "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"
            self._send(200, body, ctype, cache=self._immutable_when_current(st, q))
            if k == 8:  # neighbours of the stored reduction only; a sharper pass is on demand
                try:
                    plate.prefetch(t, z, k)
                except Exception:  # pragma: no cover - prefetch never reaches a client
                    pass

        def _astromap(self, st: ViewerState):
            try:
                payload = self._body_json(limit=26_000_000)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._error(400, str(exc))
            prefix = (f"agent-{registry.window_session.id[:12]}_" if registry.agent_window else "")
            try:
                # A pyramid replacement drains this lease before changing root,
                # attrs or generation. Do not acquire _replacement_lock here:
                # replacement holds it while waiting for readers to finish.
                with st.busy, st.exports:
                    plan = prepare_astromap(st, payload, resolve_frame=_frame_args,
                                           filename_prefix=prefix)
                    result = registry.astromap_downloads.create(plan, st)
            except AstromapConflict as exc:
                return self._error(409, str(exc))
            except AstromapError as exc:
                return self._error(400, str(exc))
            except ValueError as exc:
                return self._error(409, str(exc))
            return self._json(result)

        def _astromap_download(self, st: ViewerState, q: dict):
            key = (q.get("token") or [""])[0]
            try:
                # The zip is this window's own file, not slide pixels, so it
                # takes no busy lease; it still counts for close and update.
                with st.exports, registry.astromap_downloads.download(key, st) as (item, source):

                    def write_body(output) -> None:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)

                    self._stream_file(
                        write_body,
                        size=item.path.stat().st_size,
                        content_type="application/zip",
                        filename=item.filename,
                    )
            except AstromapError as exc:
                return self._error(404, str(exc))
            except LeaseClosed as exc:
                if exc.lease is not st.exports:
                    raise
                # The slide closed before the download began.
                return self._error(409, "slide was closed")

        def _roi(self, st: ViewerState, q: dict):
            job = parse_job_id(q)
            entered = False
            try:
                with st.busy:
                    entered = True
                    return self._roi_impl(st, q)
            except LeaseClosed as e:
                _job_update(job, state="error", error=str(e))
                return self._error(409, "slide was closed")
            except ValueError as e:
                if not entered:
                    _job_update(job, state="error", error=str(e))
                    return self._error(
                        409, "slide was closed" if "closed" in str(e) else str(e)
                    )
                if "closed" in str(e):
                    _job_update(job, state="error", error=str(e))
                    return self._error(409, "slide was closed")
                raise

        def _roi_impl(self, st: ViewerState, q: dict):
            from contextlib import nullcontext
            from dataclasses import replace as _dc_replace

            job = parse_job_id(q)
            prefix = (
                f"agent-{registry.window_session.id[:12]}_"
                if registry.agent_window else ""
            )
            col = (q.get("col") or [None])[0]
            try:
                export = prepare_export(st, q, resolve_frame=_frame_args, filename_prefix=prefix)
                # Scale-strip previews and size samples are api/roi reads too.
                # They stay plain slide reads so they never block a close.
                counted = (export.streams_file
                           or export.w * export.h >= EXPORT_COUNT_MIN_PIXELS)
                with st.exports if counted else nullcontext():
                    if export.streams_file:
                        with export.file(_job_update) as artifact:
                            self._stream_file(
                                artifact.copy_to,
                                size=artifact.size,
                                content_type=artifact.content_type,
                                filename=artifact.filename,
                            )
                        return
                    if col:
                        if _accepts_kwarg(render.export_roi_rendered, "col"):
                            if export.fmt not in ("png", "jpg", "jpeg", "svg"):
                                raise ExportRequestError(f"unknown format {export.fmt}")
                            if export.w * export.h / 1e6 > export.max_render_mpx:
                                export.rendered()
                            try:
                                body = render.export_roi_rendered(
                                    export.root,
                                    export.attrs,
                                    export.level,
                                    export.x,
                                    export.y,
                                    export.w,
                                    export.h,
                                    export.channels,
                                    export.fmt,
                                    export.window,
                                    scale_bar=export.scale_bar,
                                    col=col,
                                )
                            except ValueError as exc:
                                raise ExportRequestError(str(exc)) from exc
                            ext = "jpg" if export.fmt == "jpeg" else export.fmt
                            ctype = {
                                "png": "image/png",
                                "jpg": "image/jpeg",
                                "svg": "image/svg+xml",
                            }[ext]
                            suffix = "_scalebar" if export.scale_bar else ""
                            return self._send(
                                200,
                                body,
                                ctype,
                                {
                                    "Content-Disposition": content_disposition(
                                        f"{export.filename}{suffix}.{ext}"
                                    )
                                },
                            )
                        export = _dc_replace(
                            export, attrs=_attrs_with_colors(export.attrs, col)
                        )
                    artifact = export.rendered()
                    return self._send(
                        200, artifact.body, artifact.content_type,
                        {"Content-Disposition": content_disposition(artifact.filename)},
                    )
            except ExportRequestError as e:
                _job_update(job, state="error", error=str(e))
                return self._error(400, str(e))

    return Handler


def create_server(
    store_paths: str | Path | list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8000,
    max_render_mpx: float = 400.0,
    window_session=None,
) -> ThreadingHTTPServer:
    """Build the viewer HTTP server without running it (port 0 = ephemeral).

    The caller runs ``httpd.serve_forever()`` -- directly (CLI) or in a
    thread (the macOS app shell). More slides can be added afterwards via
    ``httpd.registry`` or POST /api/open.
    """
    import ipaddress
    import secrets

    try:
        is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise ValueError(
            f"refusing to bind {host}: this server has no authentication "
            "beyond its capability URL and serves local files -- it binds "
            "loopback only"
        )

    if isinstance(store_paths, (str, Path)):
        store_paths = [store_paths]
    registry = SlideRegistry(max_render_mpx=max_render_mpx, window_session=window_session)
    token = secrets.token_urlsafe(16)

    class _Server(ThreadingHTTPServer):
        # OpenSeadragon opens many tile connections in one burst; the
        # stdlib default backlog of 5 resets the overflow at the kernel.
        request_queue_size = 64
        daemon_threads = True
        max_workers = _resolve_http_max_workers()

        def __init__(self, *args, **kwargs):
            self._pool_lock = threading.Lock()
            self._shutting_down = False
            self._pool_closed = False
            self._pending_sockets: dict[Any, Any] = {}
            self._active_sockets: set[Any] = set()
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="nd2wsi-http",
            )
            try:
                super().__init__(*args, **kwargs)
            except BaseException:
                self._pool_closed = True
                self._executor.shutdown(wait=False, cancel_futures=True)
                raise

        def server_bind(self) -> None:
            # Bypass HTTPServer.server_bind()'s socket.getfqdn(host) reverse DNS /
            # mDNS lookup, which blocks 5s+ per server on offline Macs and macOS CI VMs.
            socketserver.TCPServer.server_bind(self)
            host, port = self.server_address[:2]
            self.server_name = str(host) if host else "127.0.0.1"
            self.server_port = int(port)

        def process_request(self, request, client_address) -> None:
            with self._pool_lock:
                if self._shutting_down or self._pool_closed:
                    abort = True
                else:
                    abort = False
                    key = object()
                    self._pending_sockets[key] = request
                    try:
                        self._executor.submit(
                            self._run_pooled_request, key, request, client_address
                        )
                    except RuntimeError:
                        self._pending_sockets.pop(key, None)
                        abort = True
            if abort:
                self.shutdown_request(request)

        def _run_pooled_request(self, key: Any, request, client_address) -> None:
            with self._pool_lock:
                if self._pending_sockets.pop(key, None) is None:
                    return
                if self._shutting_down or self._pool_closed:
                    abort = True
                else:
                    abort = False
                    self._active_sockets.add(request)
            if abort:
                try:
                    self.shutdown_request(request)
                except Exception:
                    pass
                return
            try:
                self.process_request_thread(request, client_address)
            finally:
                with self._pool_lock:
                    self._active_sockets.discard(request)

        def handle_error(self, request, client_address) -> None:
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionError, TimeoutError)):
                return
            if isinstance(exc, OSError) and (
                self._shutting_down or exc.errno in _DISCONNECT_ERRNOS
            ):
                return
            super().handle_error(request, client_address)

        def _drop_queued_requests(self) -> None:
            with self._pool_lock:
                self._shutting_down = True
                queued = list(self._pending_sockets.values())
                self._pending_sockets.clear()
            for req in queued:
                try:
                    self.shutdown_request(req)
                except Exception:
                    pass

        def _shutdown_pool(self) -> None:
            import socket as _socket

            with self._pool_lock:
                self._shutting_down = True
                already_closed = self._pool_closed
                self._pool_closed = True
                queued = list(self._pending_sockets.values())
                self._pending_sockets.clear()
                active = list(self._active_sockets)
            if not already_closed:
                self._executor.shutdown(wait=False, cancel_futures=True)
            for req in queued:
                try:
                    self.shutdown_request(req)
                except Exception:
                    pass
            for req in active:
                try:
                    req.shutdown(_socket.SHUT_RDWR)
                except Exception:
                    pass
                if os.name == "nt":
                    # On Windows Winsock, shutdown(SD_BOTH) does not wake a thread
                    # blocked in recv() on an idle keep-alive socket, and socket.close()
                    # defers _real_close() while BaseHTTPRequestHandler.rfile holds _io_refs > 0.
                    try:
                        req._real_close()
                    except Exception:
                        pass
            if not already_closed:
                cur = threading.current_thread()
                join_deadline = time.monotonic() + 5.0
                for worker in list(getattr(self._executor, "_threads", ())):
                    if worker is not cur and worker.is_alive():
                        remaining = join_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        worker.join(timeout=remaining)

        def _close_registry(self) -> None:
            if getattr(self, "_registry_closed", False):
                return
            self._registry_closed = True
            registry.astromap_downloads.close()
            registry.close_all(immediate=True)

        def shutdown(self) -> None:
            self._drop_queued_requests()
            super().shutdown()
            self._close_registry()

        def shutdown_for_relaunch(self, timeout: float | None = None) -> None:
            """Stop accepting work and synchronously release update locks.

            With ``timeout``, raise TimeoutError while reads still hold
            sources after it. The release goes on, and calling this again
            waits for it again.
            """
            self._drop_queued_requests()
            super().shutdown()
            if not getattr(self, "_registry_closed", False):
                self._registry_closed = self._released_for_relaunch = True
                registry.astromap_downloads.close()
            if getattr(self, "_released_for_relaunch", False):
                registry.close_all_for_relaunch(timeout=timeout)

        def server_close(self) -> None:
            self._shutdown_pool()
            self._close_registry()
            super().server_close()

    try:
        for p in store_paths:
            registry.add_store(p)
        httpd = _Server((host, port), make_handler(registry, token))
    except BaseException:
        registry.astromap_downloads.close()
        registry.close_all(immediate=True)
        raise
    httpd.registry = registry  # for embedders
    httpd.token = token
    return httpd


def server_url(httpd: ThreadingHTTPServer, host: str = "127.0.0.1") -> str:
    """The capability URL — the only address that reaches this server."""
    return f"http://{host}:{httpd.server_address[1]}/{httpd.token}/"


def serve(
    store_paths: str | Path | list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8000,
    max_render_mpx: float = 400.0,
) -> None:
    httpd = create_server(
        store_paths, host=host, port=port, max_render_mpx=max_render_mpx
    )
    for s in httpd.registry.listing():
        print(f"serving {s['name']}  {s['width']} x {s['height']} px")
    print(f"open   {server_url(httpd, host)}   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
