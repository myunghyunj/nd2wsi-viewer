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
from .direct import _Lifecycle

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
        generation: str = "",
    ):
        self.root = root
        self.attrs = attrs
        self.max_render_mpx = max_render_mpx
        self.histograms: list | None = None  # computed lazily, once
        self.annotations_path = annotations_path
        self.generation = generation  # changes when the pixels could
        self.busy = _Lifecycle()  # in-flight exports, so trash can refuse
        self.lock = threading.Lock()  # zarr reads are thread-safe; lock kept for attrs


def _close_state(st: ViewerState | None) -> None:
    if st is None:
        return
    close = getattr(st.root, "close", None)
    if close:
        close()


def store_generation(store_path: str | Path) -> str:
    """A token that changes whenever this store's pixels could have.

    A managed cache carries its manifest's generation uuid; anything else
    falls back to the metadata file's mtime. Tile URLs carry it, which is
    what lets the browser cache tiles at all: a rebuilt cache mints new
    URLs instead of reviving stale renders.
    """
    from .cache import CACHE_SUFFIX, read_manifest

    store_path = Path(store_path)
    if store_path.parent.name.endswith(CACHE_SUFFIX):
        m = read_manifest(store_path.parent)
        if m and m.get("generation"):
            return str(m["generation"])
    probe = store_path / ".zattrs" if store_path.is_dir() else store_path
    try:
        return format(probe.stat().st_mtime_ns, "x")
    except OSError:
        return ""


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
    """Annotations are work, not cache: ``nd2wsi/annotations/`` holds them.

    Whatever cache gets rebuilt or trashed, this folder is never touched
    by those operations. Sidecars written by older versions — beside the
    slide, or inside a pyramids folder — are migrated here on open.
    """
    from .cache import ANNOTATIONS_DIR, CACHE_SUFFIX, MANAGED_DIR
    from .convert import CACHE_DIR_NAME

    store_path = Path(store_path).resolve()
    stem = Path(attrs["nd2wsi"]["source"]).stem

    # the slide's folder: caches sit either beside it, under pyramids/, or
    # two levels down inside nd2wsi/caches/<container>/
    home = store_path.parent
    if home.name.endswith(CACHE_SUFFIX):
        home = home.parent
    if home.name in (CACHE_DIR_NAME, "caches"):
        home = home.parent
    if home.name == MANAGED_DIR:
        home = home.parent

    target_dir = home / MANAGED_DIR / ANNOTATIONS_DIR
    new = target_dir / f"annotations_{stem}.json"
    for old in (
        home / f"annotations_{stem}.json",
        home / CACHE_DIR_NAME / f"annotations_{stem}.json",
        home / f"{stem}.annotations.json",
        store_path.parent / f"annotations_{stem}.json",
    ):
        if old != new and old.exists() and not new.exists():
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                old.rename(new)
                break
            except OSError:
                return old
    if not new.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
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
        from .cache import CACHE_SUFFIX, read_manifest
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
        if store_path.parent.name.endswith(CACHE_SUFFIX):
            manifest = read_manifest(store_path.parent) or {}
            if manifest.get("kind") == "overview":
                return self._add_overview(store_path, manifest)
        sid = self.sid_for(store_path)
        gen = store_generation(store_path)
        stale = None
        with self._lock:
            st = self.slides.get(sid)
            if st is not None and st.generation == gen:
                return sid
            if st is not None:  # rebuilt underneath: the old root is stale
                stale = self.slides.pop(sid)
            root, attrs = open_store(store_path)
            st = ViewerState(
                root,
                attrs,
                max_render_mpx=self.max_render_mpx,
                annotations_path=annotations_sidecar(store_path, attrs),
                generation=gen,
            )
            st.store_path = store_path
            self.slides[sid] = st
        _close_state(stale)
        return sid

    def _add_overview(self, store_path: Path, manifest: dict) -> str:
        """An overview store: the source file plays level 0 when it can.

        When the source is missing, changed, or cannot back a level at
        runtime, the stored overview still shows — at half resolution,
        with a note saying why — rather than failing to open at all.
        """
        from .cache import fingerprints_match, quick_fingerprint
        from .convert import open_store
        from .direct import open_nd2_backed

        sid = self.sid_for(store_path)
        gen = store_generation(store_path)
        stale = None
        with self._lock:
            src_info = manifest.get("source", {})
            source = (store_path.parent / src_info.get("relative_path", "")).resolve()
            source_ok = False
            try:
                source_ok = source.is_file() and fingerprints_match(
                    src_info, quick_fingerprint(source)
                )
            except OSError:
                source_ok = False
            st = self.slides.get(sid)
            expected = gen + ("" if source_ok else "-degraded")
            if st is not None and st.generation == expected:
                return sid
            if st is not None:  # rebuilt or mode change: the state is stale
                stale = self.slides.pop(sid)
            root = attrs = None
            reason = f"{src_info.get('name', 'source')} is missing"
            if source.is_file() and not source_ok:
                reason = f"{source.name} has changed since this cache was built"
            if source_ok:
                try:
                    root, attrs = open_nd2_backed(source, store_path)
                except NotImplementedError as e:
                    reason = str(e)
            degraded = root is None
            if degraded:
                root, attrs = open_store(store_path)
                meta = attrs["nd2wsi"]
                meta["kind"] = "overview-degraded"
                for lv in meta["levels"]:
                    lv["downsample"] //= 2
                # the base level is now the half-resolution overview, so a
                # pixel is twice as large — otherwise every scalebar and
                # measurement would read half its true length
                if meta.get("pixel_size_um"):
                    meta["pixel_size_um"] = [v * 2 for v in meta["pixel_size_um"]]
                meta["notes"] = meta.get("notes", []) + [
                    f"{reason}; showing the stored overview at half resolution"
                ]
            st = ViewerState(
                root,
                attrs,
                max_render_mpx=self.max_render_mpx,
                # annotations live in level-0 pixels; a degraded view's base
                # is level 1, so editing here would silently corrupt them
                annotations_path=None
                if degraded
                else annotations_sidecar(store_path, attrs),
                # the two modes map levels differently, so tiles cached as
                # immutable in one must never be revived in the other
                generation=gen + ("-degraded" if degraded else ""),
            )
            st.store_path = store_path
            self.slides[sid] = st
        _close_state(stale)
        return sid

    def add_direct(self, slide_path: str | Path) -> str:
        """Serve an SVS straight from the file, writing nothing to disk."""
        from .direct import open_direct

        slide_path = Path(slide_path).resolve()
        sid = self.sid_for(slide_path)
        gen = store_generation(slide_path)
        stale = None
        with self._lock:
            st = self.slides.get(sid)
            if st is not None and st.generation == gen:
                return sid
            if st is not None:  # file replaced underneath
                stale = self.slides.pop(sid)
            root, attrs = open_direct(slide_path)
            st = ViewerState(
                root,
                attrs,
                max_render_mpx=self.max_render_mpx,
                annotations_path=annotations_sidecar(slide_path, attrs),
                generation=gen,
            )
            st.store_path = None  # nothing on disk to trash
            self.slides[sid] = st
        _close_state(stale)
        return sid

    def open_path(self, path: str | Path, on_progress=None) -> str:
        """A slide file (converted on first open) or an existing store."""
        from .convert import ensure_cache, existing_cache_store
        from .svs import is_svs

        path = Path(path).expanduser().resolve()
        if path.suffix.lower() in SLIDE_SUFFIXES:
            if is_svs(path) and existing_cache_store(path) is None:
                try:
                    # the file already holds a pyramid, so serve it as it lies
                    return self.add_direct(path)
                except NotImplementedError:
                    pass  # untiled or off-ladder file: build a store instead
            store = ensure_cache(path, on_progress=on_progress)
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
            from .cache import CACHE_SUFFIX

            if store.parent.name.endswith(CACHE_SUFFIX):
                store = store.parent  # the container owns manifest and store
            if st.busy.active:
                raise ValueError(
                    "an export from this slide is still running — try again "
                    "when it finishes"
                )
            self.slides.pop(sid, None)
        # bar any export that raced the check above and wait it out, so the
        # deletion below can never zero-fill a file someone is still writing
        st.busy.close(timeout=30)
        close = getattr(st.root, "close", None)
        if close:  # a source-backed slide holds the ND2 memory map open
            close()

        from .convert import CACHE_DIR_NAME

        home = store.parent
        if home.name == CACHE_DIR_NAME:
            home = home.parent  # annotations belong beside the slide
        rescue_annotations(store, home)
        from .cache import CacheLock

        # under the build lock, the doomed directory is renamed aside
        # before deletion: a concurrent opener either sees the intact
        # container or none at all, never a half-deleted one
        try:
            lock = CacheLock(store)
            lock.acquire(timeout=10)
        except TimeoutError:
            raise ValueError(
                "this slide's cache is being rebuilt right now — try again "
                "when the build finishes"
            ) from None
        try:
            doomed = store.with_name(f".{store.name}.trashing-{os.getpid()}")
            store.rename(doomed)
        finally:
            lock.release()
        store = doomed
        # two passes, neither holding a path list: a store is hundreds of
        # thousands of files and their names alone would be real memory
        total = 0
        freed = 0
        for walk_root, _, files in os.walk(store):
            total += len(files)
            for name in files:
                try:
                    freed += os.stat(os.path.join(walk_root, name)).st_size
                except OSError:
                    pass
        total = max(1, total)
        step = max(1, total // 100)  # a hundred ticks, whatever the size
        done = 0
        for walk_root, _, files in os.walk(store):
            for name in files:
                try:
                    os.unlink(os.path.join(walk_root, name))
                except OSError:
                    pass
                done += 1
                if on_progress and done % step == 0:
                    on_progress(done / total)
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
        with self._lock:
            return next(iter(self.slides), None)

    def get(self, sid: str | None) -> ViewerState | None:
        with self._lock:
            if sid is None:
                sid = next(iter(self.slides), None)
            return self.slides.get(sid) if sid else None

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.slides.items())
        out = []
        for sid, st in items:
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


def content_disposition(filename: str) -> str:
    """An attachment header safe for any filename.

    HTTP headers travel as latin-1; a Korean or accented slide name used
    to raise UnicodeEncodeError after the export was already written. The
    ASCII fallback keeps old clients working and the RFC 5987 filename*
    carries the real name.
    """
    import urllib.parse as _up

    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    utf8_name = _up.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


def make_handler(
    registry: SlideRegistry, token: str = ""
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "nd2wsi"

        # ---- helpers -----------------------------------------------------
        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str,
            extra: dict | None = None,
            cache: str = "no-store",
        ):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
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
            Host header must name loopback, and any Origin present must be
            this server itself.
            """
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            raw_host = self.headers.get("Host") or ""
            if raw_host.startswith("["):  # bracketed IPv6, maybe with a port
                host = raw_host[: raw_host.find("]") + 1]
            else:
                host = raw_host.rsplit(":", 1)[0]
            if host.lower() not in ("127.0.0.1", "localhost", "[::1]", ""):
                self._error(404, "not found")
                return None
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).hostname not in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                self._error(404, "not found")
                return None
            if not token:
                return path
            prefix = f"/{token}"
            if path == prefix:
                path = prefix + "/"
            if not path.startswith(prefix + "/"):
                self._error(404, "not found")
                return None
            return path[len(prefix) :]

        def do_GET(self):  # noqa: N802 (http.server API)
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
                path = self._gate()
                if path is None:
                    return
                ctype = (self.headers.get("Content-Type") or "").split(";")[0]
                if ctype.strip().lower() != "application/json":
                    return self._error(415, "expected application/json")
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
                "generation": st.generation,
                "storage": (
                    "direct"
                    if meta.get("direct")
                    else {
                        "source-backed": "compact",
                        "overview-degraded": "overview-degraded",
                    }.get(meta.get("kind"), "full")
                ),
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
            import uuid as _uuid

            p = st.annotations_path
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
                "items": data["items"],
            }
            # unique temp + per-slide lock: two tabs saving at once cannot
            # interleave through one shared temp name
            with st.lock:
                tmp = p.with_name(f".{p.name}.{_uuid.uuid4().hex[:8]}.tmp")
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
            # a URL that names the cache generation can be cached hard: a
            # rebuild mints a new generation, hence new URLs, so a stale
            # render can never be revived from the browser cache
            gen = (q.get("g") or [None])[0]
            cache = (
                "private, max-age=31536000, immutable"
                if gen and gen == st.generation
                else "no-store"
            )
            self._send(200, body, ctype, cache=cache)

        def _roi(self, st: ViewerState, q: dict):
            try:
                with st.busy:
                    return self._roi_impl(st, q)
            except ValueError as e:
                if "closed" in str(e):
                    return self._error(409, "slide was closed")
                raise

        def _roi_impl(self, st: ViewerState, q: dict):
            def qi(name: str, default: int | None = None) -> int:
                v = q.get(name)
                if not v:
                    if default is None:
                        raise ValueError(f"missing parameter {name}")
                    return default
                return int(float(v[0]))

            meta = st.attrs["nd2wsi"]
            level = qi("level", 0)
            try:
                lv = render.level_entry(meta["levels"], level)
            except KeyError:
                return self._error(400, f"level {level} out of range")
            lw, lh = lv["width"], lv["height"]
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
                        "Content-Disposition", content_disposition(f"{fname}{ext}")
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
                except Exception as e:
                    _job_update(job, state="error", error=f"{type(e).__name__}: {e}")
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
                    {"Content-Disposition": content_disposition(f"{fname}.{ext}")},
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
    registry = SlideRegistry(max_render_mpx=max_render_mpx)
    for p in store_paths:
        registry.add_store(p)
    token = secrets.token_urlsafe(16)

    class _Server(ThreadingHTTPServer):
        # OpenSeadragon opens many tile connections in one burst; the
        # stdlib default backlog of 5 resets the overflow at the kernel
        request_queue_size = 64
        daemon_threads = True

    httpd = _Server((host, port), make_handler(registry, token))
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
