"""Local viewer server (stdlib only -- no web framework dependency).

Endpoints
---------
GET /                         viewer page
GET /static/<path>            js / css / vendored OpenSeadragon
GET /api/info                 store metadata as JSON
GET /api/tile/<L>/<x>/<y>.jpg?c=0,1   rendered tile of pyramid level L
GET /api/roi?level=&x=&y=&w=&h=&format=tiff|png|jpg&c=
                              region export; tiff streams raw dtype
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import render

STATIC_DIR = Path(__file__).parent / "static"
TILE_RE = re.compile(r"^/api/tile/(\d+)/(\d+)/(\d+)\.(jpg|jpeg|png)$")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}


class ViewerState:
    def __init__(self, root: Any, attrs: dict[str, Any], max_render_mpx: float = 100.0):
        self.root = root
        self.attrs = attrs
        self.max_render_mpx = max_render_mpx
        self.lock = threading.Lock()  # zarr reads are thread-safe; lock kept for attrs


def make_handler(state: ViewerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "nd2wsi"

        # ---- helpers -----------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _error(self, code: int, msg: str):
            self._json({"error": msg}, code)

        def log_message(self, fmt, *args):  # quieter default log
            pass

        # ---- routing -----------------------------------------------------
        def do_GET(self):  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                q = urllib.parse.parse_qs(parsed.query)
                if path == "/":
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/") :])
                if path == "/api/info":
                    return self._info()
                m = TILE_RE.match(path)
                if m:
                    return self._tile(m, q)
                if path == "/api/roi":
                    return self._roi(q)
                if path == "/favicon.ico":
                    return self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return self._error(404, f"no route for {path}")
            except BrokenPipeError:
                pass
            except Exception as e:  # pragma: no cover - defensive
                try:
                    self._error(500, f"{type(e).__name__}: {e}")
                except Exception:
                    pass

        # ---- endpoints ---------------------------------------------------
        def _static(self, rel: str):
            file = (STATIC_DIR / rel).resolve()
            if not str(file).startswith(str(STATIC_DIR.resolve())) or not file.is_file():
                return self._error(404, "not found")
            body = file.read_bytes()
            self._send(200, body, MIME.get(file.suffix, "application/octet-stream"))

        def _info(self):
            meta = state.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            info = {
                "name": meta["source"],
                "width": lv0["width"],
                "height": lv0["height"],
                "tileSize": meta["tile"],
                "dtype": meta["dtype"],
                "rgb": meta["rgb"],
                "pixelSizeUm": meta["pixel_size_um"],
                "levels": meta["levels"],
                "selection": meta.get("selection", {}),
                "notes": meta.get("notes", []),
                "channels": [
                    {
                        "label": ch["label"],
                        "color": ch["color"],
                        "window": ch["window"],
                    }
                    for ch in state.attrs["omero"]["channels"]
                ],
                "maxRenderMpx": state.max_render_mpx,
            }
            self._json(info)

        def _tile(self, m: re.Match, q: dict):
            level, tx, ty = int(m.group(1)), int(m.group(2)), int(m.group(3))
            fmt = m.group(4)
            n = len(state.attrs["omero"]["channels"])
            channels = render.parse_channels((q.get("c") or [None])[0], n)
            try:
                body = render.render_tile(
                    state.root, state.attrs, level, tx, ty, channels, fmt
                )
            except KeyError as e:
                return self._error(404, str(e))
            ctype = "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"
            self._send(200, body, ctype)

        def _roi(self, q: dict):
            def qi(name: str, default: int | None = None) -> int:
                v = q.get(name)
                if not v:
                    if default is None:
                        raise ValueError(f"missing parameter {name}")
                    return default
                return int(float(v[0]))

            meta = state.attrs["nd2wsi"]
            levels = meta["levels"]
            level = qi("level", 0)
            if not 0 <= level < len(levels):
                return self._error(400, f"level {level} out of range")
            lw, lh = levels[level]["width"], levels[level]["height"]
            try:
                x, y, w, h = qi("x"), qi("y"), qi("w"), qi("h")
            except ValueError as e:
                return self._error(400, str(e))
            x, y = max(0, min(x, lw - 1)), max(0, min(y, lh - 1))
            w, h = max(1, min(w, lw - x)), max(1, min(h, lh - y))
            fmt = (q.get("format") or ["tiff"])[0].lower()
            n = len(state.attrs["omero"]["channels"])
            channels = render.parse_channels((q.get("c") or [None])[0], n)
            stem = Path(meta["source"]).stem
            fname = f"{stem}_L{level}_x{x}_y{y}_{w}x{h}"

            if fmt in ("tif", "tiff"):
                # write through an on-disk temp file so RAM stays bounded for
                # arbitrarily large regions, then stream it to the client
                tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                try:
                    tmp.close()
                    render.export_roi_tiff(
                        state.root, state.attrs, tmp.name, level, x, y, w, h, channels
                    )
                    size = Path(tmp.name).stat().st_size
                    self.send_response(200)
                    self.send_header("Content-Type", "image/tiff")
                    self.send_header("Content-Length", str(size))
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{fname}.tif"'
                    )
                    self.end_headers()
                    with open(tmp.name, "rb") as fh:
                        while True:
                            buf = fh.read(1024 * 1024)
                            if not buf:
                                break
                            self.wfile.write(buf)
                finally:
                    Path(tmp.name).unlink(missing_ok=True)
                return

            if fmt in ("png", "jpg", "jpeg"):
                if w * h / 1e6 > state.max_render_mpx:
                    return self._error(
                        400,
                        f"rendered export capped at {state.max_render_mpx:.0f} MPx; "
                        f"requested {w * h / 1e6:.0f} MPx -- use format=tiff "
                        "(streams any size) or a higher level",
                    )
                body = render.export_roi_rendered(
                    state.root, state.attrs, level, x, y, w, h, channels, fmt
                )
                ext = "png" if fmt == "png" else "jpg"
                ctype = "image/png" if fmt == "png" else "image/jpeg"
                return self._send(
                    200,
                    body,
                    ctype,
                    {"Content-Disposition": f'attachment; filename="{fname}.{ext}"'},
                )
            return self._error(400, f"unknown format {fmt}")

    return Handler


def serve(
    store_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    max_render_mpx: float = 100.0,
) -> None:
    from .convert import open_store

    root, attrs = open_store(store_path)
    state = ViewerState(root, attrs, max_render_mpx=max_render_mpx)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    meta = attrs["nd2wsi"]
    lv0 = meta["levels"][0]
    print(
        f"serving {meta['source']}  {lv0['width']} x {lv0['height']} px, "
        f"{len(meta['levels'])} levels"
    )
    print(f"open   http://{host}:{port}/   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
