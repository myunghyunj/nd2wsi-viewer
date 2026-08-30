"""nd2wsi-viewer: the native macOS shell.

A WKWebView window (via pywebview) around the same local server the CLI
uses.  Opening a slide converts it once (pyramid + sidecar live next to the
ND2), then the viewer loads from an ephemeral localhost port.

    nd2wsi-viewer [slide.nd2|.svs]   # window; drag-drop / dialog if no file
    nd2wsi-viewer --smoke slide.nd2  # headless self-test (used by packaging)
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

APP_NAME = "nd2wsi-viewer"


def _dlog(msg: str) -> None:
    """Append one line to ~/Library/Logs/nd2wsi-viewer.log (best effort)."""
    import datetime

    try:
        log = Path.home() / "Library" / "Logs" / "nd2wsi-viewer.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        with open(log, "a") as fh:
            fh.write(f"{stamp} {msg}\n")
    except OSError:
        pass

BOOT_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{height:100%;margin:0;background:#000;color:rgba(255,255,255,.85);
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",sans-serif;
    -webkit-font-smoothing:antialiased;cursor:default;-webkit-user-select:none}
  #drop{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
  .card{text-align:center;pointer-events:none}
  .pyr{width:64px;margin:0 auto 26px;opacity:.9}
  .pyr div{height:11px;border-radius:6px;margin:4px auto;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.35)}
  .pyr .l1{width:26px;background:#7cc0ff}.pyr .l2{width:44px;background:#3fa0ff}
  .pyr .l3{width:62px;background:#0a84ff}
  h1{font-size:15px;font-weight:600;margin:0;color:rgba(255,255,255,.85)}
  .sub{color:rgba(255,255,255,.35);font-size:13px;margin-top:7px}
  .sub b{color:rgba(255,255,255,.55);font-weight:600}
  #status{margin-top:26px;font-size:12px;color:rgba(255,255,255,.4);min-height:16px;
    font-family:ui-monospace,"SF Mono",Menlo,monospace}
  #bar{width:180px;height:4px;border-radius:2px;margin:10px auto 0;
    background:rgba(255,255,255,.12);overflow:hidden;display:none}
  #fill{display:block;height:100%;width:0%;border-radius:2px;background:#b08900;
    transition:width .25s ease}
  #pct{margin-top:7px;font-size:12px;color:rgba(255,255,255,.5);min-height:14px;
    font-family:ui-monospace,"SF Mono",Menlo,monospace}
  #drop.over::after{content:"";position:fixed;inset:14px;border-radius:16px;
    border:2px dashed rgba(10,132,255,.9);background:rgba(10,132,255,.06)}
</style></head><body>
<div id="drop"><div class="card">
  <div class="pyr"><div class="l1"></div><div class="l2"></div><div class="l3"></div></div>
  <h1>Drop a slide to open</h1>
  <div class="sub"><b>.nd2</b> or <b>.svs</b> &nbsp;·&nbsp; or click anywhere to browse</div>
  <div id="status"></div>
  <div id="bar"><span id="fill"></span></div>
  <div id="pct"></div>
</div></div>
<script>
  let busy = false, polling = null;
  const drop = document.getElementById('drop');
  function setStatus(t){ document.getElementById('status').textContent = t || ''; }
  const bar = document.getElementById('bar'), fill = document.getElementById('fill');
  function poll(){
    pywebview.api.status().then(s => { if (s) setStatus(s); });
    pywebview.api.progress().then(f => {
      const pctEl = document.getElementById('pct');
      if (f >= 0) {
        bar.style.display = 'block';
        const p = Math.round(f * 100);
        fill.style.width = p + '%';
        pctEl.textContent = p + ' %';
      } else { bar.style.display = 'none'; pctEl.textContent = ''; }
    });
  }
  function begin(){ busy = true; polling = setInterval(poll, 400); }
  function end(msg){ clearInterval(polling); busy = false; setStatus(msg || ''); bar.style.display='none'; document.getElementById('pct').textContent=''; }
  function go(promise){
    begin();
    promise.then(url => { if (url) location.replace(url); else end(''); })
      .catch(e => end('failed: ' + e));
  }
  drop.addEventListener('click', () => { if (!busy) go(pywebview.api.open_slide()); });
  // pywebview 6 delivers dropped-file paths through a Python-side DOM
  // handler, which calls __pydrop with the real path. The JS listeners
  // below only run the visuals.
  window.__pydrop_multi = paths => { if (!busy) go(pywebview.api.open_paths(paths)); };
  window.__pydrop = path => window.__pydrop_multi([path]);
  document.addEventListener('dragover', ev => ev.preventDefault(), true);
  document.addEventListener('drop', ev => ev.preventDefault(), true);
  window.addEventListener('dragover', ev => { ev.preventDefault(); drop.classList.add('over'); });
  window.addEventListener('dragleave', () => drop.classList.remove('over'));
  window.addEventListener('drop', ev => {
    ev.preventDefault();
    drop.classList.remove('over');
  });
  window.addEventListener('pywebviewready', () => {
    pywebview.api.pending().then(p => { if (p) go(pywebview.api.open_slide()); });
  });
</script></body></html>"""


def open_or_convert(nd2_path: Path, on_status=None, on_progress=None) -> Path:
    """Return the pyramid store for an ND2, building it on first open."""
    from .convert import convert, default_store_path

    store = default_store_path(nd2_path)
    if not store.exists():
        if on_status:
            on_status(f"building pyramid for {nd2_path.name} … (first open only)")
        convert(nd2_path, store, progress=False, on_progress=on_progress)
    return store


_PENDING_OPENS: list = []
_OPENS_LOCK = threading.Lock()
_FLUSHER_ALIVE = False


def _dispatch_open(paths):
    """Queue Finder-opened files and deliver once the page can take them.

    The open event can arrive on the AppKit main thread before the window
    or its page exists. A synchronous evaluate_js there deadlocks startup,
    so the event only queues the paths and a background thread hands them
    to the page when __pydrop_multi is ready.
    """
    global _FLUSHER_ALIVE
    if not paths:
        return
    with _OPENS_LOCK:
        _PENDING_OPENS.extend(paths)
        if _FLUSHER_ALIVE:
            return
        _FLUSHER_ALIVE = True
    threading.Thread(target=_flush_opens, daemon=True).start()


def _flush_opens():
    import json as _json
    import time

    import webview

    global _FLUSHER_ALIVE
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if webview.windows:
                try:
                    ready = webview.windows[0].evaluate_js(
                        "typeof window.__pydrop_multi"
                    )
                except Exception:
                    ready = None
                if ready == "function":
                    with _OPENS_LOCK:
                        batch, _PENDING_OPENS[:] = list(_PENDING_OPENS), []
                    if batch:
                        _dlog(f"flushing {len(batch)} queued open(s)")
                        webview.windows[0].evaluate_js(
                            f"window.__pydrop_multi({_json.dumps(batch)})"
                        )
                    return
            time.sleep(0.3)
        _dlog("flush_opens timed out")
    except Exception as e:
        _dlog(f"flush_opens failed: {e}")
    finally:
        with _OPENS_LOCK:
            _FLUSHER_ALIVE = False


def _install_open_files_handler():
    """Accept Finder's open events (double-click, Open With) while running.

    pywebview's cocoa delegate does not implement application:openFiles:,
    so we add the method to its class before the app starts.
    """
    try:
        import objc
        from webview.platforms.cocoa import BrowserView

        def application_openFiles_(self, app, filenames):
            paths = [str(f) for f in filenames]
            _dlog(f"openFiles event {paths}")
            good = [p for p in paths if p.lower().endswith((".nd2", ".svs"))]
            if good:
                _dispatch_open(good)
            try:
                # tell LaunchServices the open succeeded, or Finder shows a
                # "could not be opened" alert even though the slide opened
                app.replyToOpenOrPrint_(0)  # NSApplicationDelegateReplySuccess
            except Exception as e:
                _dlog(f"replyToOpenOrPrint failed: {e}")

        def application_openFile_(self, app, filename):
            _dlog(f"openFile event {filename}")
            p = str(filename)
            if p.lower().endswith((".nd2", ".svs")):
                _dispatch_open([p])
                return True
            return False

        objc.classAddMethods(
            BrowserView.AppDelegate,
            [
                objc.selector(
                    application_openFiles_,
                    selector=b"application:openFiles:",
                    signature=b"v@:@@",
                ),
                objc.selector(
                    application_openFile_,
                    selector=b"application:openFile:",
                    signature=b"B@:@@",
                ),
            ],
        )
        _dlog("openFiles handler installed")
    except Exception as e:
        _dlog(f"openFiles handler install failed: {e}")


def _wire_file_drop(window):
    """Deliver Finder drops to the page.

    pywebview 6 exposes a dropped file's real path only inside Python-side
    DOM drop handlers (the native layer records paths only while such a
    handler is registered). Page JS never sees pywebviewFullPath, so we
    register here on every page load and hand the path to the page's
    __pydrop function.
    """
    import json as _json

    from webview.dom import DOMEventHandler

    def on_drop(e):
        files = (e.get("dataTransfer") or {}).get("files") or []
        paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
        _dlog(f"drop received {len(files)} file(s), {len(paths)} with paths")
        if paths:
            window.evaluate_js(
                "window.__pydrop_multi ? window.__pydrop_multi(%s)"
                " : (window.__pydrop && window.__pydrop(%s))"
                % (_json.dumps(paths), _json.dumps(paths[0]))
            )

    def attach(*_args):
        try:
            _dlog(f"loaded {window.get_current_url()}")
        except Exception:
            pass
        try:
            window.dom.document.events.drop += DOMEventHandler(
                on_drop, prevent_default=True
            )
        except Exception:
            pass  # not a document we control; the next load re-attaches

    window.events.loaded += attach


def start_server(store: Path):
    """Serve the store on an ephemeral localhost port; returns (httpd, url)."""
    from .server import create_server

    httpd = create_server(store, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


class Api:
    """Bridge exposed to the bootstrap page."""

    def __init__(self, initial: Path | None):
        self._initial = initial
        self._status = ""
        self._frac = -1.0  # conversion progress, -1 = not converting
        self._httpd = None

    def pending(self) -> bool:
        return self._initial is not None

    def status(self) -> str:
        return self._status

    def progress(self) -> float:
        return self._frac

    def open_slide(self):
        import webview

        if self._initial is not None:
            path, self._initial = self._initial, None
        else:
            picked = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("Slide scans (*.nd2;*.svs)",),
            )
            if not picked:
                return None
            return self._launch_many([Path(p) for p in picked])
        return self._launch(path)

    def open_path(self, path: str):
        """A file dropped onto the entry page."""
        return self.open_paths([path])

    def open_paths(self, paths):
        """Several slides at once, converted one by one."""
        good = []
        for raw in paths or []:
            p = Path(raw)
            if p.suffix.lower() not in (".nd2", ".svs"):
                self._status = f"{p.name}: not an .nd2 or .svs file"
            elif not p.is_file():
                self._status = f"could not find {p.name}"
            else:
                good.append(p)
        if not good:
            return None
        return self._launch_many(good)

    def pick_path(self):
        """Kept for older pages. Returns one path."""
        picked = self.pick_paths()
        return picked[0] if picked else None

    def pick_paths(self):
        """Native open dialog for the tab shell's '+' button (no convert)."""
        import webview

        picked = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Slide scans (*.nd2;*.svs)",),
        )
        return [str(p) for p in picked] if picked else None

    def _launch_many(self, paths):
        url = None
        n = len(paths)
        for i, p in enumerate(paths):
            tag = f" ({i + 1} of {n})" if n > 1 else ""

            def note(msg, _tag=tag):
                self._status = msg.replace(" … (first open only)", "") + _tag + " …"

            def frac(f):
                self._frac = f

            _dlog(f"open {p}")
            try:
                store = open_or_convert(p, on_status=note, on_progress=frac)
                self._frac = -1.0
                if self._httpd is None:
                    self._httpd, url = start_server(store)
                else:
                    self._httpd.registry.add_store(store)
                    url = f"http://127.0.0.1:{self._httpd.server_address[1]}/"
            except Exception as e:
                self._frac = -1.0
                self._status = f"could not open {p.name}: {e}"
                _dlog(f"open FAILED {p.name}: {e}")
        self._status = "starting viewer …" if url else self._status
        return url

    def _launch(self, path: Path):
        try:
            def note(msg):
                self._status = msg

            def frac(f):
                self._frac = f

            store = open_or_convert(path, on_status=note, on_progress=frac)
            self._frac = -1.0
            self._status = "starting viewer …"
            if self._httpd is None:
                self._httpd, url = start_server(store)
            else:
                self._httpd.registry.add_store(store)
                url = f"http://127.0.0.1:{self._httpd.server_address[1]}/"
            return url  # the tab shell at / lists every open slide
        except Exception as e:  # surfaced in the bootstrap page
            self._frac = -1.0
            self._status = f"could not open {path.name}: {e}"
            return None


def smoke(nd2_path: Path) -> int:
    """Headless self-test for the packaged binary: convert, serve, fetch."""
    import json
    import urllib.request

    store = open_or_convert(nd2_path, on_status=print)
    httpd, url = start_server(store)
    info = json.loads(urllib.request.urlopen(url + "api/info", timeout=30).read())
    tile = urllib.request.urlopen(url + "api/tile/0/0/0.jpg", timeout=30).read()

    # ND2 export must work in the shipped bundle: crop 64x64 and read it back
    nd2_ok = "no"
    try:
        roi = urllib.request.urlopen(
            url + "api/roi?level=0&x=0&y=0&w=64&h=64&format=nd2", timeout=60
        ).read()
        import tempfile

        import nd2 as nd2lib

        with tempfile.NamedTemporaryFile(suffix=".nd2", delete=False) as tf:
            tf.write(roi)
        with nd2lib.ND2File(tf.name) as f:
            assert f.sizes["X"] == 64 and f.sizes["Y"] == 64
        Path(tf.name).unlink(missing_ok=True)
        nd2_ok = f"yes ({len(roi)} bytes)"
    except urllib.error.HTTPError as e:
        print(f"smoke FAIL: ND2 export -> HTTP {e.code}: {e.read().decode()[:300]}",
              file=sys.stderr)
    except Exception as e:
        print(f"smoke FAIL: ND2 export -> {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        httpd.shutdown()

    print(
        f"smoke ok: {info['name']} {info['width']}x{info['height']} "
        f"{len(info['levels'])} levels, tile {len(tile)} bytes, "
        f"nd2 export {nd2_ok}"
    )
    return 0 if nd2_ok.startswith("yes") else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME, description=__doc__)
    ap.add_argument("nd2", nargs="?", help="ND2 or SVS file to open at launch")
    ap.add_argument("--smoke", action="store_true", help="headless self-test")
    args, _ = ap.parse_known_args(argv)  # tolerate Finder's -psn_* args

    initial = Path(args.nd2).expanduser() if args.nd2 else None
    if args.smoke:
        if not initial:
            print("--smoke needs an ND2 path", file=sys.stderr)
            return 2
        return smoke(initial)

    try:
        import webview
    except ImportError:
        print(
            "the app shell needs pywebview:  pip install 'nd2wsi-viewer[app]'",
            file=sys.stderr,
        )
        return 1

    try:
        webview.settings["ALLOW_DOWNLOADS"] = True  # ROI exports save natively
    except (AttributeError, TypeError, KeyError):
        pass
    api = Api(initial)
    window = webview.create_window(
        APP_NAME,
        html=BOOT_HTML,
        js_api=api,
        width=1280,
        height=860,
        min_size=(760, 520),
        background_color="#161618",
    )
    _wire_file_drop(window)
    _install_open_files_handler()
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
