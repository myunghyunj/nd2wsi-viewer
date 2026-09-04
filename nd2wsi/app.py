"""nd2wsi-viewer: the native macOS shell.

A WKWebView window (via pywebview) around the same local server the CLI
uses.  Opening a slide converts it once (pyramid + sidecar live next to the
ND2), then the viewer loads from an ephemeral localhost port.

    nd2wsi-viewer [slide.nd2|.svs]   # window; drag-drop / dialog if no file
    nd2wsi-viewer --smoke slide.nd2  # headless self-test (used by packaging)
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path

APP_NAME = "nd2wsi-viewer"
_UPDATER_HANDLE = None
_UPDATER_COORDINATOR = None


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
  #fill.indet{width:40%;transition:none;
    animation:indet 1.1s ease-in-out infinite alternate}
  @keyframes indet{from{margin-left:0}to{margin-left:60%}}
  #pct{margin-top:7px;font-size:12px;color:rgba(255,255,255,.5);min-height:14px;
    font-family:ui-monospace,"SF Mono",Menlo,monospace}
  #drop.over::after{content:"";position:fixed;inset:14px;border-radius:16px;
    border:2px dashed rgba(10,132,255,.9);background:rgba(10,132,255,.06)}
  #update{position:fixed;right:12px;bottom:10px;height:28px;padding:0 11px;
    border:0;border-radius:7px;background:transparent;color:rgba(255,255,255,.42);
    font:12px -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif}
  #update:hover{background:rgba(255,255,255,.07);color:rgba(255,255,255,.78)}
  #update:disabled{opacity:.45}
</style></head><body>
<div id="drop"><div class="card">
  <div class="pyr"><div class="l1"></div><div class="l2"></div><div class="l3"></div></div>
  <h1>Drop a slide to open</h1>
  <div class="sub"><b>.nd2</b> or <b>.svs</b> &nbsp;·&nbsp; or click anywhere to browse</div>
  <div id="status"></div>
  <div id="bar"><span id="fill"></span></div>
  <div id="pct"></div>
</div></div>
<button id="update" type="button">Check for Updates…</button>
<script>
  let busy = false, polling = null;
  const drop = document.getElementById('drop');
  function setStatus(t){ document.getElementById('status').textContent = t || ''; }
  const bar = document.getElementById('bar'), fill = document.getElementById('fill');
  function poll(){
    pywebview.api.status().then(s => { if (s) setStatus(s); });
    pywebview.api.progress().then(f => {
      const pctEl = document.getElementById('pct');
      if (f === 0) {            /* alive, before the first real tick */
        bar.style.display = 'block';
        fill.style.width = '';
        fill.classList.add('indet');
        pctEl.textContent = '';
      } else if (f > 0) {
        bar.style.display = 'block';
        fill.classList.remove('indet');
        const p = Math.round(f * 100);
        fill.style.width = p + '%';
        pctEl.textContent = p + ' %';
      } else { bar.style.display = 'none'; fill.classList.remove('indet'); pctEl.textContent = ''; }
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
  document.getElementById('update').addEventListener('click', ev => {
    const button = ev.currentTarget;
    button.disabled = true;
    pywebview.api.check_for_updates()
      .then(result => { if (!result.ok) setStatus(result.message || 'Could not check for updates.'); })
      .catch(error => setStatus('Could not check for updates: ' + error))
      .finally(() => { button.disabled = false; });
  });
</script></body></html>"""


def open_or_convert(nd2_path: Path, on_status=None, on_progress=None) -> Path:
    """The path the server should open for this slide.

    An SVS already holds a pyramid, so it is served straight from the file
    and nothing is built or asked. An ND2 has no pyramid inside, so its
    store is built on first open, next to the slide.
    """
    from .convert import ensure_cache, existing_cache_store
    from .plate import is_plate_file
    from .svs import is_svs

    if is_svs(nd2_path) and existing_cache_store(nd2_path) is None:
        return nd2_path  # the registry serves it straight from the file
    if nd2_path.suffix.lower() == ".nd2" and is_plate_file(nd2_path):
        if on_status:
            on_status(f"opening {nd2_path.name} … (plate)")
        return nd2_path  # camera fields over time are served from the file
    if on_status:
        on_status(f"opening {nd2_path.name} …")
    return ensure_cache(nd2_path, on_progress=on_progress)


_PENDING_OPENS: list = []
_OPENS_LOCK = threading.Lock()
_FLUSHER_ALIVE = False
_LAUNCH_PATH: str | None = None  # the file given at launch, resolved
_LAUNCH_AT = 0.0


def _dispatch_open(paths):
    """Queue Finder-opened files and deliver once the page can take them.

    The open event can arrive on the AppKit main thread before the window
    or its page exists. A synchronous evaluate_js there deadlocks startup,
    so the event only queues the paths and a background thread hands them
    to the page when __pydrop_multi is ready.
    """
    global _FLUSHER_ALIVE
    import time

    # a launch from a terminal hands the process its own arguments as open
    # events too, so the file opening right now would open twice at once;
    # the launch file is dropped while the app is still starting up
    if _LAUNCH_PATH and time.time() - _LAUNCH_AT < 15:
        def _same(raw):
            try:
                return str(Path(raw).expanduser().resolve()) == _LAUNCH_PATH
            except OSError:
                return False
        paths = [p for p in paths if not _same(p)]
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


def _native_window(window):
    """The NSWindow behind a pywebview window, or None (main thread only)."""
    import AppKit

    native = getattr(window, "native", None)
    candidate = getattr(native, "window", None) or native
    if isinstance(candidate, AppKit.NSWindow):
        return candidate
    for win in AppKit.NSApplication.sharedApplication().windows():
        if win.isVisible():
            return win
    return None


def title_bar_double_click_action() -> str:
    """What macOS does when a title bar is double-clicked: zoom, minimize, or nothing.

    Mirrors the Desktop & Dock setting. Its stored values are Maximize,
    Fill, Minimize, and None; unset means the default, which zooms.
    """
    try:
        from Foundation import NSUserDefaults

        value = NSUserDefaults.standardUserDefaults().stringForKey_(
            "AppleActionOnDoubleClick"
        )
    except Exception:
        value = None
    if value == "Minimize":
        return "minimize"
    if value == "None":
        return "none"
    return "zoom"


def _inline_traffic_lights(window):
    """Hide the macOS title bar so the tab strip hosts the traffic lights.

    Safari-style chrome: the window keeps its close/minimize/zoom buttons,
    which float over the web content, and the shell pads its tab strip to
    make room (the ``native-chrome`` class). pywebview installs the
    WKWebView as the content view only after the first navigation, which
    can rebuild the frame, so the tweak re-runs on every ``loaded`` event
    as well as on ``shown``. Purely cosmetic — on any failure the stock
    title bar simply stays.
    """

    def apply(trigger):
        try:
            import AppKit
            from Foundation import NSOperationQueue
        except Exception:
            return

        def tweak():
            try:
                ns = _native_window(window)
                if ns is None:
                    return
                # the style-mask change rebuilds the title bar, wiping any
                # appearance set before it — so the mask goes first
                ns.setStyleMask_(
                    ns.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView
                )
                ns.setTitlebarAppearsTransparent_(True)
                ns.setTitleVisibility_(AppKit.NSWindowTitleHidden)
                if hasattr(ns, "setTitlebarSeparatorStyle_"):
                    ns.setTitlebarSeparatorStyle_(
                        getattr(AppKit, "NSTitlebarSeparatorStyleNone", 1)
                    )

                frame_view = ns.contentView().superview()
                # the content view must truly span the frame: when the web
                # view was installed before the mask changed, its frame
                # still excludes the old title bar
                content = ns.contentView()
                content.setFrame_(frame_view.bounds())
                content.setAutoresizingMask_(
                    AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
                )

                # an empty toolbar is how Chrome and Notion get their
                # traffic lights vertically centered: it makes the title
                # bar toolbar-height and the system re-seats the buttons
                if ns.toolbar() is None:
                    bar = AppKit.NSToolbar.alloc().initWithIdentifier_(
                        "nd2wsi.titlebar.spacer"
                    )
                    bar.setShowsBaselineSeparator_(False)
                    ns.setToolbar_(bar)
                if hasattr(ns, "setToolbarStyle_"):
                    ns.setToolbarStyle_(
                        getattr(AppKit, "NSWindowToolbarStyleUnifiedCompact", 4)
                    )

                # pywebview's frameless mode hides the standard buttons;
                # this app wants them, floating over the tab strip
                for which in (
                    AppKit.NSWindowCloseButton,
                    AppKit.NSWindowMiniaturizeButton,
                    AppKit.NSWindowZoomButton,
                ):
                    button = ns.standardWindowButton_(which)
                    if button is not None:
                        button.setHidden_(False)

                close_button = ns.standardWindowButton_(AppKit.NSWindowCloseButton)
                zoom_button = ns.standardWindowButton_(AppKit.NSWindowZoomButton)
                if close_button is not None and zoom_button is not None:
                    bar_h = close_button.superview().frame().size.height
                    cf, zf = close_button.frame(), zoom_button.frame()
                    _dlog(
                        f"lights: bar h={bar_h:.0f} close={cf.origin.x:.0f},"
                        f"{cf.origin.y:.0f} {cf.size.width:.0f}x{cf.size.height:.0f} "
                        f"zoom-right={zf.origin.x + zf.size.width:.0f}"
                    )

                # macOS 26+ draws a glass decoration layer over the title
                # bar that ignores titlebarAppearsTransparent; hide it (it
                # returns after frame rebuilds, hence the re-run)
                for sub in frame_view.subviews():
                    if "TitlebarContainer" not in str(sub.className()):
                        continue
                    for inner in sub.subviews():
                        name = str(inner.className())
                        if "Decoration" in name or "Background" in name:
                            inner.setHidden_(True)

            except Exception as e:
                _dlog(f"titlebar tweak failed ({trigger}): {e!r}")

        NSOperationQueue.mainQueue().addOperationWithBlock_(tweak)

    window.events.shown += lambda: apply("shown")
    window.events.loaded += lambda: apply("loaded")


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
                f"window.__pydrop_multi ? window.__pydrop_multi({_json.dumps(paths)})"
                f" : (window.__pydrop && window.__pydrop({_json.dumps(paths[0])}))"
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


def _server_url(httpd) -> str:
    from .server import server_url

    return server_url(httpd)


def start_server(store: Path):
    """Serve the store on an ephemeral localhost port; returns (httpd, url)."""
    from .server import create_server, server_url

    httpd = create_server(store, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, server_url(httpd)


class UpdateShutdownCoordinator:
    """Flush browser work and release native resources before Sparkle relaunches."""

    def __init__(self, api: Api, window):
        self.api = api
        self.window = window
        self._lock = threading.Lock()
        self._running = False
        self._request_id = ""
        self._completion = None
        self._failure = None

    def prepare_for_update(self, completion, failure) -> None:
        with self._lock:
            if self._running:
                failure("update preparation is already running")
                return
            self._running = True
            self._request_id = uuid.uuid4().hex
            self._completion = completion
            self._failure = failure
        self._flush_panes()

    def _notice(self, message: str) -> None:
        try:
            payload = json.dumps(str(message))
            self.window.evaluate_js(
                f"window.nd2wsiUpdateNotice && window.nd2wsiUpdateNotice({payload})"
            )
        except Exception as exc:
            _dlog(f"update notice failed: {exc!r}")

    def _report_wait(self, message: str) -> None:
        self._notice(message)
        failure = self._failure
        if failure is not None:
            failure(message)

    def _flush_panes(self) -> None:
        # Every retry has a distinct wire id so a late iframe reply from an
        # earlier timed-out attempt cannot satisfy the new attempt.
        request = json.dumps(f"{self._request_id}-{uuid.uuid4().hex}")
        script = (
            "typeof window.nd2wsiPrepareForUpdate === 'function' "
            f"? window.nd2wsiPrepareForUpdate({request}) "
            ": Promise.resolve({ok:true, panes:0})"
        )
        try:
            self.window.evaluate_js(script, callback=self._after_flush)
        except Exception as exc:
            self._retry_flush(f"Could not prepare the viewer: {exc}")

    def _after_flush(self, result) -> None:
        if not isinstance(result, dict) or not result.get("ok"):
            if isinstance(result, dict):
                message = str(result.get("error") or "annotations were not saved")
            else:
                message = "the viewer did not confirm its saved state"
            self._retry_flush(f"Update waiting: {message}")
            return
        threading.Thread(
            target=self._drain_and_close,
            name="updater-safe-relaunch",
            daemon=True,
        ).start()

    def _retry_flush(self, message: str) -> None:
        self._report_wait(message + "; retrying…")
        retry = threading.Timer(5.0, self._flush_panes)
        retry.daemon = True
        retry.start()

    def _drain_and_close(self) -> None:
        last_reason = None
        try:
            while True:
                reason = self.api.update_block_reason()
                if reason is None:
                    break
                if reason != last_reason:
                    self._notice(reason)
                    last_reason = reason
                time.sleep(0.5)
            self.api.stop_server_for_update()
        except Exception as exc:
            message = f"Update could not close the viewer safely: {exc}"
            self._report_wait(message)
            _dlog(message)
            return

        completion = self._completion
        if completion is None:
            return
        try:
            from Foundation import NSOperationQueue

            NSOperationQueue.mainQueue().addOperationWithBlock_(completion)
        except Exception:
            completion()


def _install_app_updater(api: Api, window) -> None:
    global _UPDATER_COORDINATOR, _UPDATER_HANDLE
    if _UPDATER_COORDINATOR is not None:
        return

    def install():
        global _UPDATER_COORDINATOR, _UPDATER_HANDLE
        try:
            from .updater import install_sparkle_updater

            coordinator = UpdateShutdownCoordinator(api, window)
            handle = install_sparkle_updater(
                shutdown_coordinator=coordinator,
                logger=_dlog,
            )
            _UPDATER_COORDINATOR = coordinator
            _UPDATER_HANDLE = handle
            api.attach_updater(handle)
        except Exception as exc:
            _dlog(f"updater setup failed: {exc!r}")

    try:
        from Foundation import NSOperationQueue

        NSOperationQueue.mainQueue().addOperationWithBlock_(install)
    except Exception:
        install()


class Api:
    """Bridge exposed to the bootstrap page."""

    def __init__(self, initial: Path | None, native_gesture_scopes=None):
        self._initial = initial
        self._status = ""
        self._frac = -1.0  # conversion progress, -1 = not converting
        self._httpd = None
        self._updater = None
        self._server_lock = threading.Lock()
        if native_gesture_scopes is None:
            from .native_gestures import NativeGestureScopeCache

            native_gesture_scopes = NativeGestureScopeCache()
        self._native_gesture_scopes = native_gesture_scopes

    def pending(self) -> bool:
        return self._initial is not None

    def status(self) -> str:
        return self._status

    def progress(self) -> float:
        return self._frac

    def set_native_gesture_scopes(self, payload) -> dict:
        """Replace the plate-stage geometry used by the AppKit event monitor."""

        return self._native_gesture_scopes.replace(payload)

    def begin_native_gesture_scope_session(self) -> dict:
        """Issue a token so late calls from an older page cannot restore scopes."""

        token = uuid.uuid4().hex
        self._native_gesture_scopes.begin(token)
        return {"ok": True, "token": token}

    def clear_native_gesture_scopes(self) -> None:
        self._native_gesture_scopes.clear()

    def update_status(self) -> dict:
        """Status shown by the shell's persistent update button."""
        try:
            from importlib.metadata import version

            current = version("nd2wsi-viewer")
        except Exception:
            current = "development"
        return {"available": self._updater is not None, "version": current}

    def check_for_updates(self) -> dict:
        """Show Sparkle's standard update window on AppKit's main thread."""
        handle = self._updater
        if handle is None:
            return {
                "ok": False,
                "message": "Updates are available in the installed macOS app.",
            }
        try:
            from Foundation import NSOperationQueue

            NSOperationQueue.mainQueue().addOperationWithBlock_(
                handle.check_for_updates
            )
            return {"ok": True}
        except Exception as exc:
            _dlog(f"manual update check failed: {exc!r}")
            return {"ok": False, "message": "Could not open the update window."}

    def attach_updater(self, handle) -> None:
        self._updater = handle

    def update_block_reason(self) -> str | None:
        if self._frac >= 0:
            return "Waiting for the slide which is opening…"
        with self._server_lock:
            server = self._httpd
        if server is None:
            return None
        count = server.registry.active_export_count()
        if count:
            noun = "export" if count == 1 else "exports"
            return f"Waiting for {count} {noun} to finish…"
        return None

    def stop_server_for_update(self) -> None:
        """Synchronously release sockets, ND2 handles, and plate writers."""
        with self._server_lock:
            server, self._httpd = self._httpd, None
        if server is None:
            return
        shutdown = getattr(server, "shutdown_for_relaunch", server.shutdown)
        shutdown()
        server.server_close()

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

    def pick_paths(self):
        """Native open dialog for the tab shell's '+' button (no convert)."""
        import webview

        picked = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Slide scans (*.nd2;*.svs)",),
        )
        return [str(p) for p in picked] if picked else None

    def title_bar_double_click(self) -> str:
        """The tab strip stands in for the title bar: double-clicking it zooms.

        The window is frameless, so AppKit never sees the double-click;
        the shell reports it and this does what the system setting asks.
        In native full screen nothing happens, as with a real title bar.
        Returns the action taken.
        """
        import webview

        action = title_bar_double_click_action()
        if action == "none" or not webview.windows:
            return action
        try:
            from Foundation import NSOperationQueue
        except Exception:
            return "none"
        window = webview.windows[0]

        def act():
            try:
                import AppKit

                ns = _native_window(window)
                if ns is None:
                    return
                if ns.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
                    return
                if action == "minimize":
                    ns.miniaturize_(None)
                else:
                    ns.zoom_(None)
            except Exception as e:
                _dlog(f"title bar double-click failed: {e!r}")

        NSOperationQueue.mainQueue().addOperationWithBlock_(act)
        return action

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
                self._frac = 0.0  # bar up while the file opens, before ticks
                store = open_or_convert(p, on_status=note, on_progress=frac)
                # a plate file skips conversion; the bar stays up while the
                # registry opens it, which reads a few frames for its window
                with self._server_lock:
                    if self._httpd is None:
                        self._httpd, url = start_server(store)
                    else:
                        self._httpd.registry.add_store(store)
                        url = _server_url(self._httpd)
                self._frac = -1.0
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

            self._frac = 0.0  # bar up while the file opens, before ticks
            store = open_or_convert(path, on_status=note, on_progress=frac)
            self._status = "starting viewer …"
            with self._server_lock:
                if self._httpd is None:
                    self._httpd, url = start_server(store)
                else:
                    self._httpd.registry.add_store(store)
                    url = _server_url(self._httpd)
            self._frac = -1.0
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
        httpd.server_close()
        httpd.registry.close_all(immediate=True)

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

    api, _window = create_app_window(initial)
    webview.start()
    return 0


def create_app_window(initial: Path | None):
    """The app window and its bridge, ready for ``webview.start``."""
    import time

    import webview

    global _LAUNCH_PATH, _LAUNCH_AT
    _LAUNCH_PATH = str(Path(initial).expanduser().resolve()) if initial else None
    _LAUNCH_AT = time.time()

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
        # frameless makes pywebview create the NSWindow with
        # FullSizeContentView from birth — the only point where WebKit
        # decides whether the page composites under the title bar. The
        # traffic lights it hides come back in _inline_traffic_lights.
        frameless=True,
        easy_drag=False,
    )
    # WKWebView can consume horizontal trackpad events in a private scroll
    # subview without creating DOM wheel events. A local AppKit monitor routes
    # only clearly horizontal gestures; vertical input keeps its normal path.
    from .native_gestures import wire_native_trackpad_bridge

    wire_native_trackpad_bridge(
        window,
        _dlog,
        scope_cache=api._native_gesture_scopes,
    )
    window.events.before_load += lambda *_args: api.clear_native_gesture_scopes()
    window.events.closed += lambda *_args: api.clear_native_gesture_scopes()
    _inline_traffic_lights(window)
    _wire_file_drop(window)
    _install_open_files_handler()
    window.events.shown += lambda: _install_app_updater(api, window)
    return api, window


if __name__ == "__main__":
    sys.exit(main())
