"""Local viewer server (stdlib only -- no web framework dependency).

Multiple slides are served at once, browser-tab style:

GET  /                          tab shell (one tab per open slide)
GET  /static/<path>             js / css / vendored OpenSeadragon (global)
GET  /api/slides                open slides: [{sid, name, width, height}]
POST /api/open                  {"path": "/…/slide.nd2|.svs|store.ome.zarr"}
                                converts if needed, registers, -> {sid}
POST /api/close                 {"sid": "…"} unregister a slide

Per slide (also reachable bare as /api/… for the first slide, so scripts
keep working):

GET  /s/<sid>/                  viewer page
GET  /s/<sid>/api/info          store metadata as JSON
GET  /s/<sid>/api/histogram     per-channel LUT histograms
GET  /s/<sid>/api/tile/<L>/<x>/<y>.jpg?c=0,1&win=…
GET  /s/<sid>/api/roi?level=&x=&y=&w=&h=&format=nd2|tiff|png|jpg&c=&win=
GET/POST /s/<sid>/api/annotations   sidecar annotations
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import render

STATIC_DIR = Path(__file__).parent / "static"
TILE_RE = re.compile(r"^/api/tile/(\d+)/(\d+)/(\d+)\.(jpg|jpeg|png)$")
SLIDE_RE = re.compile(r"^/s/([0-9a-f]{8})(/.*)?$")
JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

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

SLIDE_SUFFIXES = {".nd2", ".svs"}


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


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}


class ViewerState:
    def __init__(
        self,
        root: Any,
        attrs: dict[str, Any],
        max_render_mpx: float = 400.0,
        annotations_path: Path | None = None,
    ):
        self.root = root
        self.attrs = attrs
        self.max_render_mpx = max_render_mpx
        self.histograms: list | None = None  # computed lazily, once
        self.annotations_path = annotations_path
        self.lock = threading.Lock()  # zarr reads are thread-safe; lock kept for attrs


def rescue_annotations(folder: str | Path, home: str | Path) -> list[Path]:
    """Lift annotation sidecars out of ``folder`` before it is deleted.

    Annotations belong beside the slide, but a store built by an older
    version may hold them, and they are work rather than cache. Returns the
    files moved to safety.
    """
    import shutil

    folder, home = Path(folder), Path(home)
    saved = []
    for path in folder.rglob("annotations_*.json"):
        target = home / path.name
        if target.exists():
            continue
        try:
            shutil.move(str(path), str(target))
        except OSError:
            continue
        saved.append(target)
    return saved


def annotations_sidecar(store_path: str | Path, attrs: dict[str, Any]) -> Path:
    """Annotations live in ``annotations_<slide>.json`` beside the slide.

    They are your work, not cache, so they stay out of the ``pyramids``
    folder and survive emptying it. Sidecars written by older versions, or
    left inside the cache folder, are moved here on open.
    """
    from .convert import CACHE_DIR_NAME

    store_path = Path(store_path).resolve()
    stem = Path(attrs["nd2wsi"]["source"]).stem
    home = store_path.parent
    if home.name == CACHE_DIR_NAME:
        home = home.parent
    new = home / f"annotations_{stem}.json"
    for old in (
        store_path.parent / f"annotations_{stem}.json",
        home / f"{stem}.annotations.json",
        store_path.parent / f"{stem}.annotations.json",
    ):
        if old != new and old.exists() and not new.exists():
            try:
                old.rename(new)
            except OSError:
                return old
    return new


class SlideRegistry:
    """The set of slides this server has open, keyed by a stable short id."""

    def __init__(self, max_render_mpx: float = 400.0):
        self.slides: dict[str, ViewerState] = {}  # insertion-ordered
        self.max_render_mpx = max_render_mpx
        self._lock = threading.Lock()

    @staticmethod
    def sid_for(store_path: Path) -> str:
        return hashlib.sha1(str(store_path.resolve()).encode()).hexdigest()[:8]

    def add_store(self, store_path: str | Path) -> str:
        from .convert import open_store
        from .svs import is_svs

        store_path = Path(store_path).resolve()
        if is_svs(store_path):  # an SVS itself serves with no store
            try:
                return self.add_direct(store_path)
            except NotImplementedError:
                from .convert import convert as _convert
                from .convert import default_store_path as _dsp

                built = _dsp(store_path)
                if not built.exists():
                    _convert(store_path, built, progress=False)
                store_path = built
        sid = self.sid_for(store_path)
        with self._lock:
            if sid in self.slides:
                return sid
            root, attrs = open_store(store_path)
            st = ViewerState(
                root,
                attrs,
                max_render_mpx=self.max_render_mpx,
                annotations_path=annotations_sidecar(store_path, attrs),
            )
            st.store_path = store_path
            self.slides[sid] = st
        return sid

    def add_direct(self, slide_path: str | Path) -> str:
        """Serve an SVS straight from the file, writing nothing to disk."""
        from .direct import open_direct

        slide_path = Path(slide_path).resolve()
        sid = self.sid_for(slide_path)
        with self._lock:
            if sid in self.slides:
                return sid
            root, attrs = open_direct(slide_path)
            st = ViewerState(
                root,
                attrs,
                max_render_mpx=self.max_render_mpx,
                annotations_path=annotations_sidecar(slide_path, attrs),
            )
            st.store_path = None  # nothing on disk to trash
            self.slides[sid] = st
        return sid

    def open_path(self, path: str | Path, on_progress=None) -> str:
        """A slide file (converted on first open) or an existing store."""
        from .convert import convert, default_store_path
        from .svs import is_svs

        path = Path(path).expanduser().resolve()
        if path.suffix.lower() in SLIDE_SUFFIXES:
            store = default_store_path(path)
            if is_svs(path) and not store.exists():
                try:
                    # the file already holds a pyramid, so serve it as it lies
                    return self.add_direct(path)
                except NotImplementedError:
                    pass  # untiled or off-ladder file: build a store instead
            if not store.exists():
                convert(path, store, progress=False, on_progress=on_progress)
        elif path.is_dir():  # a *.ome.zarr store
            store = path
        else:
            raise ValueError(f"not an ND2/SVS slide or pyramid store: {path.name}")
        return self.add_store(store)

    def remove(self, sid: str) -> bool:
        with self._lock:
            st = self.slides.pop(sid, None)
        close = getattr(getattr(st, "root", None), "close", None)
        if close:
            close()
        return st is not None

    def trash_cache(self, sid: str, on_progress=None) -> int:
        """Close the slide and delete its pyramid store. Returns bytes freed.

        Deletes only the registered store directory, never the source slide,
        and never an annotation sidecar that ended up inside it. A store is
        hundreds of thousands of small files, so the caller gets a fraction
        as the files go.
        """
        import os

        with self._lock:
            st = self.slides.get(sid)
            if st is None:
                raise KeyError(sid)
            if getattr(st, "store_path", None) is None:
                raise ValueError(
                    "this slide runs straight from the file, there is no cache"
                )
            store = Path(st.store_path)
            if not (store.is_dir() and store.name.endswith(".ome.zarr")):
                raise ValueError(f"refusing to delete {store}: not a pyramid store")
            self.slides.pop(sid, None)

        from .convert import CACHE_DIR_NAME

        home = store.parent
        if home.name == CACHE_DIR_NAME:
            home = home.parent  # annotations belong beside the slide
        rescue_annotations(store, home)
        victims = []
        freed = 0
        for root, _, files in os.walk(store):
            for name in files:
                path = os.path.join(root, name)
                try:
                    freed += os.stat(path).st_size
                except OSError:
                    pass
                victims.append(path)
        total = max(1, len(victims))
        step = max(1, total // 100)  # a hundred ticks, whatever the store size
        for i, path in enumerate(victims):
            try:
                os.unlink(path)
            except OSError:
                pass
            if on_progress and i % step == 0:
                on_progress(i / total)
        for root, dirs, _ in os.walk(store, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        try:
            store.rmdir()
        except OSError:
            pass
        if on_progress:
            on_progress(1.0)
        return freed

    def default_sid(self) -> str | None:
        return next(iter(self.slides), None)

    def listing(self) -> list[dict[str, Any]]:
        out = []
        for sid, st in self.slides.items():
            meta = st.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            out.append(
                {
                    "sid": sid,
                    "name": meta["source"],
                    "width": lv0["width"],
                    "height": lv0["height"],
                    "rgb": meta["rgb"],
                }
            )
        return out


def make_handler(registry: SlideRegistry) -> type[BaseHTTPRequestHandler]:
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

        def _body_json(self, limit: int = 10_000_000) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= limit:
                raise ValueError("payload missing or too large")
            return json.loads(self.rfile.read(n))

        def log_message(self, fmt, *args):  # quieter default log
            pass

        def _resolve(self, path: str) -> tuple[ViewerState | None, str]:
            """Map a URL path to (slide state, slide-relative path)."""
            m = SLIDE_RE.match(path)
            if m:
                st = registry.slides.get(m.group(1))
                return st, (m.group(2) or "/")
            if path.startswith("/api/"):  # bare API -> first slide (scripts)
                sid = registry.default_sid()
                return (registry.slides.get(sid) if sid else None), path
            return None, path

        # ---- routing -----------------------------------------------------
        def do_GET(self):  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                q = urllib.parse.parse_qs(parsed.query)
                if path == "/":
                    return self._static("shell.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/") :])
                if path == "/api/slides":
                    return self._json({"slides": registry.listing()})
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
                if sub == "/api/histogram":
                    return self._histogram(st)
                if sub == "/api/annotations":
                    return self._annotations_get(st)
                m = TILE_RE.match(sub)
                if m:
                    return self._tile(st, m, q)
                if sub == "/api/roi":
                    return self._roi(st, q)
                return self._error(404, f"no route for {path}")
            except BrokenPipeError:
                pass
            except Exception as e:  # pragma: no cover - defensive
                try:
                    self._error(500, f"{type(e).__name__}: {e}")
                except Exception:
                    pass

        def do_POST(self):  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path == "/api/open":
                    return self._open()
                if path == "/api/close":
                    return self._close()
                if path == "/api/trash":
                    return self._trash()
                st, sub = self._resolve(path)
                if st is not None and sub == "/api/annotations":
                    return self._annotations_post(st)
                return self._error(404, f"no POST route for {path}")
            except BrokenPipeError:
                pass
            except Exception as e:  # pragma: no cover - defensive
                try:
                    self._error(500, f"{type(e).__name__}: {e}")
                except Exception:
                    pass

        # ---- global endpoints --------------------------------------------
        def _static(self, rel: str):
            file = (STATIC_DIR / rel).resolve()
            if not str(file).startswith(str(STATIC_DIR.resolve())) or not file.is_file():
                return self._error(404, "not found")
            body = file.read_bytes()
            self._send(200, body, MIME.get(file.suffix, "application/octet-stream"))

        def _open(self):
            job = None
            try:
                data = self._body_json()
                path = data.get("path") if isinstance(data, dict) else None
                if not path:
                    return self._error(400, 'expected {"path": "..."}')
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

                sid = registry.open_path(path, on_progress=on_progress)
                if job:
                    _job_update(job, state="done", pct=100)
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

        def _trash(self):
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
            if not sid or not registry.remove(sid):
                return self._error(404, f"no open slide {sid}")
            return self._json({"ok": True, "slides": registry.listing()})

        # ---- per-slide endpoints -----------------------------------------
        def _info(self, st: ViewerState):
            meta = st.attrs["nd2wsi"]
            lv0 = meta["levels"][0]
            info = {
                "name": meta["source"],
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
                "nd2Export": _limnd2_available(),
                "direct": bool(meta.get("direct")),
            }
            self._json(info)

        def _annotations_get(self, st: ViewerState):
            p = st.annotations_path
            if p is None:
                return self._json({"items": [], "path": None})
            if p.exists():
                try:
                    data = json.loads(p.read_text())
                    items = data.get("items", []) if isinstance(data, dict) else []
                except (json.JSONDecodeError, OSError) as e:
                    return self._error(500, f"could not read {p.name}: {e}")
            else:
                items = []
            self._json({"items": items, "path": str(p)})

        def _annotations_post(self, st: ViewerState):
            p = st.annotations_path
            if p is None:
                return self._error(400, "no annotation sidecar path for this store")
            try:
                data = self._body_json()
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return self._error(400, "expected {\"items\": [...]}")
            payload = {
                "format": "nd2wsi-annotations/1",
                "source": st.attrs["nd2wsi"]["source"],
                "items": data["items"],
            }
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=1))
            tmp.replace(p)  # atomic
            self._json({"ok": True, "path": str(p), "count": len(data["items"])})

        def _histogram(self, st: ViewerState):
            with st.lock:
                if st.histograms is None:
                    st.histograms = render.compute_histograms(st.root, st.attrs)
            self._json({"channels": st.histograms})

        def _tile(self, st: ViewerState, m: re.Match, q: dict):
            level, tx, ty = int(m.group(1)), int(m.group(2)), int(m.group(3))
            fmt = m.group(4)
            n = len(st.attrs["omero"]["channels"])
            channels = render.parse_channels((q.get("c") or [None])[0], n)
            win = (q.get("win") or [None])[0]
            try:
                body = render.render_tile(
                    st.root, st.attrs, level, tx, ty, channels, fmt, win
                )
            except KeyError as e:
                return self._error(404, str(e))
            ctype = "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"
            self._send(200, body, ctype)

        def _roi(self, st: ViewerState, q: dict):
            def qi(name: str, default: int | None = None) -> int:
                v = q.get(name)
                if not v:
                    if default is None:
                        raise ValueError(f"missing parameter {name}")
                    return default
                return int(float(v[0]))

            meta = st.attrs["nd2wsi"]
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
            fmt = (q.get("format") or ["nd2"])[0].lower()
            n = len(st.attrs["omero"]["channels"])
            channels = render.parse_channels((q.get("c") or [None])[0], n)
            win = (q.get("win") or [None])[0]
            job = (q.get("job") or [None])[0]
            if job and not JOB_RE.match(job):
                job = None
            stem = Path(meta["source"]).stem
            fname = f"{stem}_L{level}_x{x}_y{y}_{w}x{h}"

            if fmt in ("nd2", "tif", "tiff"):
                # write through an on-disk temp file so RAM stays bounded for
                # arbitrarily large regions, then stream it to the client
                is_nd2 = fmt == "nd2"
                ext = ".nd2" if is_nd2 else ".tif"
                ctype = "application/octet-stream" if is_nd2 else "image/tiff"
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                _job_update(job, state="writing", pct=0)

                def on_progress(frac: float) -> None:
                    _job_update(job, state="writing", pct=int(min(1.0, frac) * 100))

                try:
                    tmp.close()
                    if is_nd2:
                        from .export_nd2 import export_roi_nd2

                        try:
                            export_roi_nd2(
                                st.root, st.attrs, tmp.name,
                                level, x, y, w, h, channels,
                                on_progress=on_progress,
                            )
                        except (RuntimeError, ValueError) as e:
                            _job_update(job, state="error", error=str(e))
                            return self._error(400, str(e))
                    else:
                        render.export_roi_tiff(
                            st.root, st.attrs, tmp.name,
                            level, x, y, w, h, channels,
                            on_progress=on_progress,
                        )
                    _job_update(job, state="streaming", pct=100)
                    size = Path(tmp.name).stat().st_size
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(size))
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{fname}{ext}"'
                    )
                    self.end_headers()
                    with open(tmp.name, "rb") as fh:
                        while True:
                            buf = fh.read(1024 * 1024)
                            if not buf:
                                break
                            self.wfile.write(buf)
                    _job_update(job, state="done", pct=100)
                except BrokenPipeError:
                    _job_update(job, state="error", error="client disconnected")
                    raise
                finally:
                    Path(tmp.name).unlink(missing_ok=True)
                return

            if fmt in ("png", "jpg", "jpeg"):
                if w * h / 1e6 > st.max_render_mpx:
                    return self._error(
                        400,
                        f"rendered export capped at {st.max_render_mpx:.0f} MPx; "
                        f"requested {w * h / 1e6:.0f} MPx -- use format=tiff "
                        "(streams any size) or a higher level",
                    )
                body = render.export_roi_rendered(
                    st.root, st.attrs, level, x, y, w, h, channels, fmt, win
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


def create_server(
    store_paths: str | Path | list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8000,
    max_render_mpx: float = 400.0,
) -> ThreadingHTTPServer:
    """Build the viewer HTTP server without running it (port 0 = ephemeral).

    The caller runs ``httpd.serve_forever()`` -- directly (CLI) or in a
    thread (the macOS app shell). More slides can be added afterwards via
    ``httpd.registry`` or POST /api/open.
    """
    if isinstance(store_paths, (str, Path)):
        store_paths = [store_paths]
    registry = SlideRegistry(max_render_mpx=max_render_mpx)
    for p in store_paths:
        registry.add_store(p)
    httpd = ThreadingHTTPServer((host, port), make_handler(registry))
    httpd.registry = registry  # for embedders
    return httpd


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
    print(f"open   http://{host}:{httpd.server_address[1]}/   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
