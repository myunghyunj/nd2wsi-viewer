"""nd2wsi-viewer: the native macOS and Windows shell.

A WKWebView or Edge WebView2 window around the same local server the CLI
uses.  Opening a slide converts it once (pyramid + sidecar live next to the
ND2), then the viewer loads from an ephemeral localhost port.

    nd2wsi-viewer [slide.nd2|.svs|.sdpc]  # window; drag-drop / dialog if no file
    nd2wsi-viewer --smoke slide.nd2  # headless self-test (used by packaging)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from . import window_chrome as _window_chrome
from . import window_lifecycle as _window_lifecycle

APP_NAME = os.environ.get("ND2WSI_APP_NAME", "nd2wsi-viewer") if sys.platform == "darwin" else "nd2wsi-viewer"
OPEN_SUFFIXES = (".nd2", ".svs", ".sdpc", ".nd2svs", ".jpg", ".jpeg", ".png")
OPEN_FILE_TYPES = ("Slides and images (*.nd2;*.svs;*.sdpc;*.nd2svs;*.jpg;*.jpeg;*.png)",)
_UPDATER_HANDLE = None
_UPDATER_COORDINATOR = None


def log_path() -> Path:
    """A user-writable log location, including a portable Windows launch."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / APP_NAME / "Logs" / f"{APP_NAME}.log"
    return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"


def _dlog(msg: str) -> None:
    """Append one diagnostic line to the per-user app log (best effort)."""
    import datetime

    try:
        log = log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        with open(log, "a", encoding="utf-8") as fh:
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
  <div class="sub"><b>.nd2</b>, <b>.svs</b>, <b>.sdpc</b>, <b>.nd2svs</b>, or <b>JPEG/PNG</b> &nbsp;·&nbsp; click anywhere to browse</div>
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
    const refreshUpdates = () => pywebview.api.update_status().then(s => {
        const button = document.getElementById('update');
        button.disabled = !s.available;
        if (s.mode === 'download') button.textContent = 'Download Updates…';
      });
    refreshUpdates();
    setTimeout(refreshUpdates, 600);
  });
  document.getElementById('update').addEventListener('click', ev => {
    const button = ev.currentTarget;
    button.disabled = true;
    pywebview.api.check_for_updates()
      .then(result => { if (result.message || !result.ok) setStatus(result.message || 'Could not check for updates.'); })
      .catch(error => setStatus('Could not check for updates: ' + error))
      .finally(() => { button.disabled = false; });
  });
</script></body></html>"""


# The empty entry page has no server URL or menu, but uses the same native-window
# controller and keyboard classification as the loaded slide shell.
BOOT_HTML = BOOT_HTML.replace(
    "</body></html>",
    "<script>" + "\n".join(
        (Path(__file__).parent / "static" / name).read_text(encoding="utf-8")
        for name in ("shortcut-router-v1.js", "window-controls-v1.js")
    ) + "</script></body></html>",
)


def open_or_convert(nd2_path: Path, on_status=None, on_progress=None) -> Path:
    """The path the server should open for this slide.

    SVS and SDPC already hold pyramids, so they can be served from the file
    without building a viewing cache. An ND2 has no pyramid inside, so its
    store is built on first open, next to the slide.
    """
    from .cache import SINGLE_FILE_SUFFIX
    from .convert import ensure_cache, existing_cache_store
    from .plate import is_plate_file
    from .raster import is_raster
    from .sdpc import is_sdpc
    from .svs import is_svs

    if nd2_path.suffix.lower() == SINGLE_FILE_SUFFIX:
        if on_status:
            on_status(f"opening {nd2_path.name} …")
        return nd2_path
    if is_sdpc(nd2_path) or is_raster(nd2_path):
        return nd2_path  # Direct sources need no cache creation or migration.
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
_OPEN_READY_TIMEOUT = 60.0  # how long queued opens wait for the page to take them
_OPEN_CALL_TIMEOUT = 5.0  # one delivery call; the budget window_lifecycle gives a browser call


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


def _start_page_call(window, script):
    """Run ``window.evaluate_js(script)`` on its own daemon thread.

    pywebview's evaluate_js has no deadline (it first waits up to 20 s for
    the page bridge, then for the script's answer), so the caller waits on
    the returned ``done`` event with its own budget. A page that never
    answers strands this helper thread, never the flusher. ``outcome`` holds
    "value" or "error" once ``done`` is set.
    """
    done = threading.Event()
    outcome: dict = {}

    def call():
        try:
            outcome["value"] = window.evaluate_js(script)
        except Exception as exc:
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=call, name="finder-open-js", daemon=True).start()
    return done, outcome


def _flush_opens():
    """Hand queued opens to the page until the queue is empty.

    The flusher stops only when it sees the queue empty, and it clears
    ``_FLUSHER_ALIVE`` under the same lock, so an open queued while a batch
    is being delivered is delivered too. Every page call runs on a helper
    thread: the readiness probe may wait until the deadline (startup can
    hold pywebview's bridge for seconds), a delivery waits at most
    ``_OPEN_CALL_TIMEOUT``. The next call starts only after the previous
    one answered, so a hung page strands at most one helper thread per
    flusher, and after the deadline the flusher gives up with the paths
    still queued; the next open starts a new flusher that delivers them.
    """
    import json as _json
    import time

    import webview

    global _FLUSHER_ALIVE
    owner = True
    deadline = time.monotonic() + _OPEN_READY_TIMEOUT
    running = None  # (done, outcome) of the newest page call
    try:
        while True:
            with _OPENS_LOCK:
                left = len(_PENDING_OPENS)
                if not left or time.monotonic() >= deadline:
                    _FLUSHER_ALIVE = owner = False
            if not owner:
                if left:
                    _dlog(f"flush_opens timed out; {left} open(s) stay queued for the next open")
                return
            if not webview.windows:
                time.sleep(0.3)
                continue
            window = webview.windows[0]
            if running is not None and not running[0].wait(max(0.0, deadline - time.monotonic())):
                continue  # the page still has not answered the previous call
            running = _start_page_call(window, "typeof window.__pydrop_multi")
            if not running[0].wait(max(0.0, deadline - time.monotonic())):
                continue
            if running[1].get("value") != "function":
                time.sleep(0.3)
                continue
            with _OPENS_LOCK:
                batch, _PENDING_OPENS[:] = list(_PENDING_OPENS), []
            _dlog(f"flushing {len(batch)} queued open(s)")
            running = _start_page_call(window, f"window.__pydrop_multi({_json.dumps(batch)})")
            # A late answer still opens the batch, so it is never queued again.
            if not running[0].wait(_OPEN_CALL_TIMEOUT):
                _dlog(f"the viewer did not answer {len(batch)} open(s) within {_OPEN_CALL_TIMEOUT:g} seconds")
            elif "error" in running[1]:
                _dlog(f"flush_opens failed: {running[1]['error']}")
            deadline = time.monotonic() + _OPEN_READY_TIMEOUT
    except Exception as e:
        _dlog(f"flush_opens failed: {e}")
    finally:
        if owner:
            with _OPENS_LOCK:
                _FLUSHER_ALIVE = False


def _install_open_files_handler():
    """Accept Finder's open events before native window creation and while running.

    pywebview's cocoa delegate does not implement application:openFiles:,
    so we add the method and bind its shared delegate before the app starts.
    """
    try:
        import objc
        from webview.platforms.cocoa import BrowserView

        def application_openFiles_(self, app, filenames):
            paths = [str(f) for f in filenames]
            _dlog(f"openFiles event {paths}")
            good = [p for p in paths if p.lower().endswith(OPEN_SUFFIXES)]
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
            if p.lower().endswith(OPEN_SUFFIXES):
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
        # Cocoa may deliver an opening document while the first NSWindow or
        # WKWebView is being created. pywebview normally binds its delegate
        # after those constructors; bind the same retained instance now so
        # the event reaches our queue instead of the default document handler.
        # A repeated installation must not replace an existing app delegate.
        if BrowserView.app.delegate() is None:
            if BrowserView._shared_app_delegate is None:
                BrowserView._shared_app_delegate = BrowserView.AppDelegate.alloc().init()
            BrowserView.app.setDelegate_(BrowserView._shared_app_delegate)
        _dlog("openFiles handler installed")
    except Exception as e:
        _dlog(f"openFiles handler install failed: {e}")


def _native_window(window):
    return _window_chrome.native_window(window)


def title_bar_double_click_action() -> str:
    return _window_chrome.title_bar_double_click_action()


def _inline_traffic_lights(window):
    _window_chrome.inline_traffic_lights(
        window, logger=_dlog, resolve_native_window=_native_window,
    )


def _wire_file_drop(window):
    _window_chrome.wire_file_drop(window, logger=_dlog)


def _server_url(httpd) -> str:
    from .server import server_url

    return server_url(httpd)


def start_server(store: Path | list[Path], window_session=None):
    """Serve the store on an ephemeral localhost port; returns (httpd, url)."""
    from .server import create_server, server_url

    kwargs = {"window_session": window_session} if window_session is not None else {}
    httpd = create_server(store, host="127.0.0.1", port=0, **kwargs)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = server_url(httpd)
    if window_session is not None:
        try:
            window_session.record_endpoint(url)
        except Exception as exc:
            _dlog(f"window endpoint record failed: {exc!r}")
    return httpd, url


class UpdateShutdownCoordinator(_window_lifecycle.UpdateShutdownCoordinator):
    """Compatibility entry point using the app's diagnostic log."""

    def __init__(self, api: Api, window):
        super().__init__(api, window, logger=lambda message: _dlog(message))


class WindowCloseCoordinator(_window_lifecycle.WindowCloseCoordinator):
    """Compatibility entry point for the window-owned close guard."""

    def __init__(self, api: Api, window):
        super().__init__(api, window, logger=lambda message: _dlog(message))


def _guard_native_close(api: Api, window) -> None:
    """Veto this process's main-window close until its saves are confirmed.

    Translucent overlays are display-only and close at once; every
    independent window is its own process with its own guard.
    """
    api._close_coordinator = WindowCloseCoordinator(api, window)
    window.events.closing += api._close_coordinator.on_closing
    window.events.closed += api._close_coordinator.cancel_preparation


def _install_window_menu(api: Api, window) -> None:
    _window_chrome.install_window_menu(api, window, logger=_dlog)


def _install_app_updater(api: Api, window) -> None:
    global _UPDATER_COORDINATOR, _UPDATER_HANDLE
    if sys.platform != "darwin":
        return
    if getattr(api, "_updates_disabled", False):
        return
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

    def __init__(self, initial: Path | None, native_gesture_scopes=None, window_session=None):
        self._initial = initial
        self._launch_source = initial
        self._status = ""
        self._frac = -1.0  # conversion progress, -1 = not converting
        self._httpd = None
        self._updater = None
        self._startup_error = None
        self._window_session = window_session
        self._handoff_state = None
        self._handoff_source = None
        self._handoff_states = {}
        self._browser_benchmark_enabled = False
        self._open_attempt_id = None
        self._fallback_consumed = False
        self._local_beta = os.environ.get("ND2WSI_DISABLE_UPDATES") == "1"
        self._updates_disabled = self._local_beta or (
            window_session is not None and window_session.role == "agent"
        )
        self._server_lock = threading.Lock()
        self._translucent_lock = threading.RLock()
        self._translucent_windows = {}
        self._translucent_owner_closed = False
        if native_gesture_scopes is None:
            from .native_gestures import NativeGestureScopeCache

            native_gesture_scopes = NativeGestureScopeCache()
        self._native_gesture_scopes = native_gesture_scopes

    def new_window(self, role: str = "agent") -> dict:
        """Automation defaults to a separate Agent process, never the User window."""
        from .window_launch import launch_window

        return launch_window(role, app_name=APP_NAME)

    def open_translucent_viewer(self, sid: str, payload: dict) -> dict:
        """Open a display-only tracing window for this viewer's registered slide."""
        if sys.platform not in ("darwin", "win32"):
            return {"ok": False, "message": "The translucent viewer requires the macOS or Windows app."}
        try:
            from urllib.parse import quote

            from .translucent_window import create_translucent_window, validate_context

            with self._server_lock:
                server = self._httpd
                store = server.registry.get(sid) if server is not None and isinstance(sid, str) else None
                if store is None:
                    raise ValueError("Open a fluorescence slide before opening the translucent viewer")
                base_url = _server_url(server).rstrip("/") + "/"
                context = validate_context(store, payload, slide_url=base_url + "s/" + quote(sid, safe="") + "/")

            with self._translucent_lock:
                if self._translucent_owner_closed:
                    raise ValueError("The source viewer is closing")
                previous = self._translucent_windows.pop(sid, None)
                if previous is not None:
                    previous.close_translucent()

                def remove(closed):
                    with self._translucent_lock:
                        if self._translucent_windows.get(sid) is closed:
                            self._translucent_windows.pop(sid, None)

                overlay = create_translucent_window(
                    context, url=base_url + "static/translucent.html",
                    title=APP_NAME + " — Translucent", logger=_dlog, on_closed=remove,
                )
                self._translucent_windows[sid] = overlay
            return {"ok": True, "id": overlay._window.uid}
        except Exception as exc:
            _dlog(f"translucent viewer open failed: {exc!r}")
            return {"ok": False, "message": f"Could not open translucent viewer: {exc}"}

    def _close_translucent_windows(self, *_args):
        with self._translucent_lock:
            self._translucent_owner_closed = True
            overlays = list(self._translucent_windows.values())
            self._translucent_windows.clear()
        for overlay in overlays:
            try:
                overlay.close_translucent()
            except Exception as exc:
                _dlog(f"translucent viewer close failed: {exc!r}")

    def open_in_metal(self, sid: str, view_state: dict, retry: bool = False) -> dict:
        """Open only a registered slide in a fresh process of this same role."""
        if sys.platform != "darwin" or self._window_session is None:
            return {"ok": False, "message": "Metal windows are available only in the macOS app."}
        try:
            from .view_state import validate_view_state, write_handoff
            from .window_launch import launch_window

            if type(retry) is not bool:
                raise ValueError("Retry must be an explicit boolean choice")

            st = self._httpd.registry.get(sid) if self._httpd is not None and isinstance(sid, str) else None
            if st is None or st.source_path is None:
                raise ValueError("Open a source slide before choosing Metal")
            meta = st.attrs["nd2wsi"]
            if meta.get("plate") or meta.get("rgb"):
                raise ValueError("This slide requires the standard viewer")
            level = meta["levels"][0]
            state = validate_view_state(view_state, dimensions=[level["width"], level["height"]],
                                        channel_count=len(st.attrs["omero"]["channels"]))
            path = write_handoff(state, source=st.source_path, role=self._window_session.role,
                                 root=self._window_session.root / "handoffs")
            return launch_window(self._window_session.role, app_name=APP_NAME,
                                 source=st.source_path, prefer_metal=True, handoff_state=path, retry_metal=retry)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "message": f"Could not open in Metal: {exc}"}

    def initial_view_state(self, sid: str) -> dict | None:
        """Only the registered destination slide receives display-only state."""
        return self._handoff_states.get(sid) if isinstance(sid, str) else None

    def benchmark_metrics(self) -> dict:
        if not self._browser_benchmark_enabled:
            return {"ok": False, "message": "Replay diagnostics were not enabled for this process"}
        from .viewport_benchmark import metrics_snapshot

        return metrics_snapshot()

    def _prepare_handoff(self) -> None:
        if self._handoff_state is None or self._httpd is None:
            return
        from copy import deepcopy

        from .view_state import validate_view_state

        for item in self._httpd.registry.listing():
            st = self._httpd.registry.get(item["sid"])
            if st.source_path != self._handoff_source:
                continue
            meta = st.attrs["nd2wsi"]
            level = meta["levels"][0]
            state = validate_view_state(self._handoff_state,
                                        dimensions=[level["width"], level["height"]],
                                        channel_count=len(st.attrs["omero"]["channels"]))
            if meta.get("plate") or meta.get("rgb"):
                raise ValueError("Display handoff requires a fluorescence slide")
            # Colors are renderer defaults in the browser. Change only this
            # process's detached metadata, never zarr/manifest/source attrs.
            attrs = deepcopy(st.attrs)
            for channel, display in zip(attrs["omero"]["channels"], state["channels"]):
                channel["color"] = "".join(f"{component:02X}" for component in display["color"])
            with st.lock:
                st.attrs = attrs
            self._handoff_states[item["sid"]] = state
        self._handoff_state = None

    def window_context(self) -> dict:
        """Identify this window before automation reads or changes viewer state."""
        if self._window_session is None:
            return {"enabled": False, "role": "user", "id": None,
                    "new_window_supported": False,
                    "agent_directive": "Use an isolated native Agent window for automation."}
        return {**self._window_session.as_dict(), "enabled": True, "pid": os.getpid(),
                "new_window_default_role": "agent", "metal_opt_in_supported": sys.platform == "darwin",
                "open_attempt_id": self._open_attempt_id, "fallback_consumed": self._fallback_consumed}

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
        if self._local_beta:
            from . import __version__

            return {"available": False, "version": __version__, "mode": "disabled",
                    "message": "Local beta — updates are disabled."}
        try:
            from importlib.metadata import version

            current = version("nd2wsi-viewer")
        except Exception:
            current = "development"
        manual = sys.platform == "win32"
        if self._updates_disabled:
            return {"available": True, "version": current, "mode": "manual-download",
                    "message": "Check for a compatible macOS download. Close all viewer windows before replacing the app."}
        return {
            "available": manual or self._updater is not None,
            "version": current,
            "mode": "download" if manual else "sparkle",
        }

    def check_for_updates(self) -> dict:
        """Open Windows release downloads or the macOS Sparkle update window."""
        if self._local_beta:
            return {"ok": False, "message": "Local beta — updates are disabled."}
        if self._updates_disabled:
            from .release_selection import check_for_updates

            return check_for_updates()
        if sys.platform == "win32":
            from .release_selection import check_for_updates

            return check_for_updates(channel="stable")
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
        self._close_translucent_windows()
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
                file_types=OPEN_FILE_TYPES,
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
            if p.suffix.lower() not in OPEN_SUFFIXES:
                self._status = f"{p.name}: not a supported slide, cache, or JPEG/PNG image"
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
            file_types=OPEN_FILE_TYPES,
        )
        return [str(p) for p in picked] if picked else None

    def start_window_drag(self) -> bool:
        """Hand the owning shell's mouse-down to native macOS window dragging."""
        drag = getattr(self, "_titlebar_drag", None)
        return drag.request() if drag is not None else False

    def read_lut_clipboard(self) -> dict:
        """Read compatible LUT settings from the system clipboard."""
        from .lut_clipboard import read_lut_clipboard

        if sys.platform == "win32":
            try:
                return read_lut_clipboard(window=self._lut_clipboard_owner())
            except RuntimeError as exc:
                return {"ok": False, "message": str(exc)}
        return read_lut_clipboard()

    def write_lut_clipboard(self, text: str) -> dict:
        """Copy portable LUT settings for another independent viewer window."""
        from .lut_clipboard import write_lut_clipboard

        if sys.platform == "win32":
            try:
                return write_lut_clipboard(text, window=self._lut_clipboard_owner())
            except RuntimeError as exc:
                return {"ok": False, "message": str(exc)}
        return write_lut_clipboard(text)

    def _lut_clipboard_owner(self):
        """Resolve this bridge's one window, never another window's UI thread."""
        import webview

        # pywebview Window stores the constructor's js_api in _js_api.
        # A real Window-construction regression test covers this dependency.
        owners = [window for window in webview.windows if getattr(window, "_js_api", None) is self]
        if len(owners) != 1:
            raise RuntimeError("LUT clipboard requires exactly one window owned by this bridge")
        return owners[0]

    def title_bar_double_click(self) -> str:
        """The tab strip stands in for the title bar: double-clicking it zooms.

        The window is frameless, so AppKit never sees the double-click;
        the shell reports it and this does what the system setting asks.
        In native full screen nothing happens, as with a real title bar.
        Returns the action taken.
        """
        # Windows keeps its own title bar and native maximize behavior.
        if sys.platform != "darwin":
            return "none"
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

    def _open_registered(self, path: Path, note, frac) -> str:
        if self._window_session is not None and self._window_session.role == "agent":
            # Do not call open_or_convert/ensure_cache before the registry can
            # apply Agent policy. Existing ND2 caches, native SVS/SDPC and plates
            # are opened read-only; missing/shared-cache repair is refused.
            note("opening in isolated Agent window")
            with self._server_lock:
                if self._httpd is None:
                    self._httpd, _ = start_server([], window_session=self._window_session)
                self._httpd.registry.open_path(path, on_progress=frac)
                self._prepare_handoff()
                return _server_url(self._httpd)
        store = open_or_convert(path, on_status=note, on_progress=frac)
        with self._server_lock:
            if self._httpd is None:
                kwargs = {"window_session": self._window_session} if self._window_session is not None else {}
                self._httpd, url = start_server(store, **kwargs)
            else:
                self._httpd.registry.add_store(store)
                url = _server_url(self._httpd)
        self._prepare_handoff()
        return url

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
                # a plate file skips conversion; the bar stays up while the
                # registry opens it, which reads a few frames for its window
                url = self._open_registered(p, note, frac)
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
            url = self._open_registered(path, note, frac)
            self._status = "starting viewer …"
            self._frac = -1.0
            return url  # the tab shell at / lists every open slide
        except Exception as e:  # surfaced in the bootstrap page
            self._frac = -1.0
            self._status = f"could not open {path.name}: {e}"
            return None


def smoke(nd2_path: Path) -> int:
    """Headless release gate run inside the packaged executable."""
    from .smoke import run

    return run(nd2_path)


def _startup_error(message: str, *, show_dialog: bool = True) -> None:
    _dlog(message)
    print(message, file=sys.stderr)
    if sys.platform == "win32" and show_dialog:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        except Exception:
            pass


def _gui_smoke_appkit_key(api: Api, window, context: dict, key: str, *, repeat: bool = False) -> dict:
    """Send one process-local key through AppKit, only to this Agent key window.

    Deliberately use NSApplication.sendEvent_ so native key equivalents and the
    responder chain participate. This is not a physical/global keyboard event.
    """
    import AppKit
    from Foundation import NSOperationQueue, NSThread

    finished, cancelled = threading.Event(), threading.Event()
    outcome = {}

    def send():
        try:
            if cancelled.is_set():
                return
            current = api.window_context()
            if (not current.get("enabled") or current.get("role") != "agent" or
                    not current.get("id") or current.get("id") != context.get("id") or
                    current.get("pid") != os.getpid() or getattr(window, "_js_api", None) is not api):
                raise RuntimeError("Native keyboard smoke requires the same bound Agent session")
            native = getattr(window, "native", None)
            application = AppKit.NSApplication.sharedApplication()
            if native is None or application.keyWindow() != native:
                raise RuntimeError("Native keyboard smoke Agent window is not the exact key window")
            number = int(native.windowNumber())
            codes = {"a": 0, "c": 8, "e": 14, "i": 34}
            modifiers = 0 if key == "e" else AppKit.NSEventModifierFlagCommand
            outcome.update(window_number=number, key=key, command=key != "e", repeat=repeat)
            for event_type in (AppKit.NSEventTypeKeyDown, AppKit.NSEventTypeKeyUp):
                if cancelled.is_set():
                    return
                if application.keyWindow() != native or getattr(window, "native", None) != native:
                    raise RuntimeError("Native keyboard smoke lost its Agent key window")
                event = AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
                    event_type, (0, 0), modifiers, time.monotonic(), number, None,
                    key, key, repeat and event_type == AppKit.NSEventTypeKeyDown, codes[key])
                if event is None:
                    raise RuntimeError("AppKit did not create the smoke key event")
                application.sendEvent_(event)
        except Exception as exc:
            outcome["error"] = str(exc)
        finally:
            finished.set()

    if NSThread.isMainThread():
        send()
    else:
        NSOperationQueue.mainQueue().addOperationWithBlock_(send)
    if not finished.wait(5):
        cancelled.set()  # A late main-queue callback must never inject a key.
        raise TimeoutError("Native keyboard smoke main queue timed out")
    if outcome.get("error"):
        raise RuntimeError(outcome["error"])
    return outcome


_GUI_SMOKE_KEYBOARD_SETUP = """(() => {
  const frame = document.querySelector('#frames iframe.active');
  const view = frame && frame.contentWindow, page = frame && frame.contentDocument;
  const toolbar = document.getElementById('newtab');
  const ids = {annot:'tb-annot', channels:'tb-channels', region:'tb-region'};
  const panelNames = [...Object.keys(ids), 'info'];
  if (!page || !toolbar || Object.values(ids).some(id => !page.getElementById(id)))
    throw new Error('Native keyboard smoke controls are unavailable');
  const panels = view.eval('state.windows');
  const original = Object.fromEntries(panelNames.map(id => [id, panels[id].isHidden()]));
  const focused = document.activeElement;
  const input = document.createElement('input');
  input.id = 'nd2wsi-native-keyboard-smoke-input'; input.type = 'text';
  input.style.cssText = 'position:fixed;bottom:0;left:0;width:180px;z-index:99999';
  document.body.appendChild(input);
  const counts = Object.fromEntries(panelNames.map(id => [id, 0]));
  const listeners = Object.entries(ids).map(([id, button]) => {
    const element = page.getElementById(button), listener = () => { counts[id]++; };
    element.addEventListener('click', listener); return [element, listener];
  });
  // Info lives in the shell tools row; its pane action has no proxy button.
  const infoActions = Object.fromEntries(['open', 'close'].map(action => [action, panels.info[action]]));
  for (const [action, originalAction] of Object.entries(infoActions)) {
    panels.info[action] = function(...args) { counts.info++; return originalAction.apply(this, args); };
  }
  let events = [];
  const observe = event => { events.push({key:event.key, code:event.code, repeat:event.repeat,
    trusted:event.isTrusted, target:event.target.id || event.target.tagName}); };
  window.addEventListener('keydown', observe, true);
  window.__nd2wsiNativeKeyboardSmoke = {
    prepare(editable, key) {
      events = []; window.getSelection()?.removeAllRanges(); view.getSelection()?.removeAllRanges();
      if (editable) {
        input.value = key === 'c' || key === 'e' ? '' : 'native smoke';
        input.focus(); input.setSelectionRange(0, 0);
      } else toolbar.focus();
      if (document.activeElement !== (editable ? input : toolbar))
        throw new Error('Native keyboard smoke could not focus its target');
      return this.snapshot();
    },
    snapshot() { return {counts:{...counts},
      hidden:Object.fromEntries(panelNames.map(id => [id, panels[id].isHidden()])),
      events:[...events], focused:document.activeElement.id, value:input.value,
      selection:[input.selectionStart, input.selectionEnd]}; },
    cleanup() {
      window.removeEventListener('keydown', observe, true);
      for (const [element, listener] of listeners) element.removeEventListener('click', listener);
      Object.assign(panels.info, infoActions);
      for (const [id, hidden] of Object.entries(original)) {
        if (panels[id].isHidden() !== hidden) panels[id][hidden ? 'close' : 'open']();
      }
      input.remove(); if (focused?.isConnected) focused.focus();
      delete window.__nd2wsiNativeKeyboardSmoke;
      return true;
    },
  };
  return {ready:true};
})()"""


def _gui_smoke_native_keyboard(api: Api, window, *, slide: bool) -> dict:
    """Bounded native menu/renderer check in the disposable macOS Agent only."""
    report = {"status": "not_assessed", "scope": "process-local AppKit synthetic Latin keys; not physical input"}
    if sys.platform != "darwin":
        return {**report, "reason": "Native keyboard smoke is implemented for macOS only"}
    context = api.window_context()
    if not slide or not context.get("enabled") or context.get("role") != "agent":
        return {**report, "reason": "Requires a loaded slide in a fresh Agent smoke window"}
    report.update(status="failed", session_id=context.get("id"), steps=[])
    setup = False
    try:
        if not context.get("id") or context.get("pid") != os.getpid() or getattr(window, "_js_api", None) is not api:
            raise RuntimeError("Native keyboard smoke Agent identity is not bound to this window")
        window.evaluate_js(_GUI_SMOKE_KEYBOARD_SETUP)
        setup = True
        for editable in (False, True):
            for key, panel in (("a", "annot"), ("c", "channels"), ("e", "region"), ("i", "info")):
                for repeat in ((False,) if editable else (False, True)):
                    before = window.evaluate_js(
                        f"window.__nd2wsiNativeKeyboardSmoke.prepare({str(editable).lower()}, {json.dumps(key)})")
                    native = _gui_smoke_appkit_key(api, window, context, key, repeat=repeat)
                    # WKWebView handles native input asynchronously. Give ignored
                    # repeats/text shortcuts the same bounded observation window.
                    deadline = time.monotonic() + 0.5
                    while True:
                        after = window.evaluate_js("window.__nd2wsiNativeKeyboardSmoke.snapshot()")
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.025)
                    toggled = not editable and not repeat
                    expected_counts = {name: count + int(toggled and name == panel)
                                       for name, count in before["counts"].items()}
                    expected_hidden = {name: (not hidden if toggled and name == panel else hidden)
                                       for name, hidden in before["hidden"].items()}
                    passed = after["counts"] == expected_counts and after["hidden"] == expected_hidden
                    if editable:
                        if key == "e":
                            passed = passed and after["value"] == "e"
                        elif key == "a":
                            passed = passed and after["selection"] == [0, len(before["value"])]
                        else:
                            passed = passed and after["value"] == before["value"]
                    report["steps"].append({**native, "focus": "editable" if editable else "shell-toolbar",
                                            "panel": panel, "passed": passed, "before": before, "after": after})
        report["status"] = "passed" if all(step["passed"] for step in report["steps"]) else "failed"
        if report["status"] == "failed":
            report["error"] = "Native keyboard routing, repeat suppression or editable protection failed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if setup:
            try:
                window.evaluate_js("window.__nd2wsiNativeKeyboardSmoke.cleanup()")
            except Exception as exc:
                report.update(status="failed", cleanup_error=str(exc))
    return report


def _gui_smoke_close(api: Api, window, report: dict, *, timeout: float) -> None:
    """Close the smoke window and settle ``report`` before the GUI loop returns.

    main() writes ``api._gui_smoke_report`` as soon as ``webview.start``
    returns, which can be the moment the window closes. So every outcome is
    recorded before the close that ends the loop: by this thread before a
    direct or forced destroy, or by the coordinator's UI-thread listener after
    its approved destroy was requested and before the loop can return.
    """
    api._gui_smoke_report = report
    coordinator = getattr(api, "_close_coordinator", None)
    if coordinator is None:
        report["close"] = {"path": "direct"}
        window.destroy()
        return
    if not report.get("ok"):
        # A failed or unready viewer cannot answer the save protocol; never
        # let the veto turn a failure report into a CI timeout.
        reason = f"GUI smoke failed in phase {report.get('phase')}"
        report["close"] = {"path": "forced", "reason": reason}
        coordinator.approve_close(reason)
        window.destroy()
        return

    started = time.monotonic()
    phase = report.get("phase")
    lock = threading.Lock()
    outcome = []
    confirmed = threading.Event()

    def saved_close():
        with lock:
            if outcome:
                return
            outcome.append("coordinated")
            report.pop("error", None)
            report.update(ok=True, phase=phase, close={
                "path": "coordinated", "seconds": round(time.monotonic() - started, 2)})
        confirmed.set()

    # Exercise the real veto -> acknowledged flush -> UI-thread approval ->
    # destroy path once. Until it confirms, a closed window is a failure.
    report.update(ok=False, phase="close", error="The window closed without the save handshake",
                  close={"path": "coordinated", "confirmed": False})
    coordinator.add_close_listener(saved_close)
    try:
        coordinator.request_native_close()
    except Exception as exc:
        failure = f"Could not start the native close: {exc}"
    else:
        if confirmed.wait(timeout):
            return
        failure = f"Native close did not complete the save handshake within {timeout:g} seconds"
    with lock:
        if outcome:
            return
        outcome.append("forced")
        if api._status:
            failure += f" (last status: {api._status})"
        report.update(ok=False, phase="close", error=failure, close={
            "path": "forced", "reason": failure, "seconds": round(time.monotonic() - started, 2)})
    coordinator.approve_close(failure)
    window.destroy()


def gui_smoke(api: Api, window, *, expect_slide: bool = False, timeout: float = 90,
              close_timeout: float = 30) -> dict:
    """Exercise the real renderer, bridge and slide DOM, then close the app."""
    started = time.monotonic()
    report = {"ok": False, "platform": sys.platform, "renderer": None, "phase": "app-loading"}
    # The opt-in round trip replaces clipboard text. Enable only on disposable
    # Windows CI desktops, never during local macOS packaged verification.
    clipboard_check = sys.platform == "win32" and os.environ.get("ND2WSI_SMOKE_LUT_CLIPBOARD") == "1"
    expected = getattr(api, "_requested_handoff", None)
    try:
        import webview

        report["window_context"] = api.window_context()
        if expected is not None:
            report["handoff_requested"] = expected
            source = Path(api._launch_source).resolve()
            info = source.stat()
            report["handoff_source"] = {"path": str(source), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
        report["renderer"] = webview.renderer
        if sys.platform == "win32" and webview.renderer != "edgechromium":
            raise RuntimeError("GUI smoke requires the Edge WebView2 renderer")
        while time.monotonic() - started < timeout:
            if api._status.startswith("could not open"):
                raise RuntimeError(api._status)
            result = None
            try:
                result = window.evaluate_js("""(() => {
                  const api = window.pywebview && window.pywebview.api;
                  if (!api || !api.update_status || document.readyState !== 'complete')
                    return {phase: 'app-bridge', document_ready: document.readyState};
                  if (!window.__nd2wsiSmokeBridge) {
                    window.__nd2wsiSmokeBridge = 'pending';
                    api.update_status().then(status => { window.__nd2wsiSmokeBridge = status; })
                      .catch(() => { window.__nd2wsiSmokeBridge = 'failed'; });
                  }
                  const bridge = window.__nd2wsiSmokeBridge;
                  if (!bridge || typeof bridge !== 'object' || !bridge.version)
                    return {phase: 'app-bridge', bridge_status: bridge || 'unavailable'};
                  const frame = document.querySelector('#frames iframe.active');
                  const page = frame && frame.contentDocument;
                  const canvas = page && page.querySelector('#stage canvas');
                  const plate = page && page.querySelector('#plate img');
                  const slide = !!(frame && typeof readyFrames !== 'undefined' &&
                    readyFrames.has(frame.dataset.sid) &&
                    ((canvas && canvas.width > 0 && canvas.height > 0) ||
                    (plate && plate.complete && plate.naturalWidth > 0)));
                  if (slide && !frame.__nd2wsiSmokeLut) {
                    const view = frame.contentWindow;
                    const progress = frame.__nd2wsiSmokeLut = {pending: true, phase: 'lut-toolbar'};
                    const step = async (phase, run, timeoutMs = 10000) => {
                      progress.phase = phase;
                      progress.step_timeout_ms = timeoutMs;
                      let timer;
                      try {
                        return await Promise.race([Promise.resolve().then(run), new Promise((_, reject) => {
                          timer = setTimeout(() => reject(new Error('LUT smoke ' + phase + ' timed out after ' + timeoutMs + ' ms')), timeoutMs);
                        })]);
                      } finally { clearTimeout(timer); }
                    };
                    frame.__nd2wsiSmokeLutTask = (async () => {
                      const transfer = view.Nd2LutTransfer;
                      if (!transfer || ['encode', 'planPaste', 'fitsChannel'].some(name => typeof transfer[name] !== 'function'))
                        throw new Error('Packaged LUT transfer module is unavailable');
                      const buttons = ['lut-type', 'lut-copy', 'lut-paste'].map(id => page.getElementById(id));
                      if (buttons.some(button => !button || typeof button.onclick !== 'function') ||
                          !page.getElementById('lut-dialog') || !page.getElementById('lut-copy-tooltip'))
                        throw new Error('Packaged LUT toolbar is not wired');
                      const shadingButton = document.getElementById('shading-correction');
                      if (!shadingButton || document.getElementById('update-check') ||
                          !document.getElementById('shading-progress-bar') ||
                          !document.getElementById('shading-progress-title') ||
                          !document.getElementById('shading-cancel') ||
                          !['shading-confirm-title', 'shading-confirm-persistence', 'shading-confirm-estimate',
                            'shading-confirm-continue', 'shading-confirm-cancel'].every(id => document.getElementById(id)) ||
                          typeof document.getElementById('shading-confirm')?.showModal !== 'function' ||
                          !window.Nd2ShadingCorrection || !view.Nd2ShadingCorrection ||
                          typeof window.Nd2ShadingCorrection.nextAction !== 'function' ||
                          typeof view.Nd2ShadingCorrection.nextAction !== 'function' ||
                          typeof window.Nd2ShadingCorrection.methodSupport !== 'function')
                        throw new Error('Packaged Shading Correction controls are unavailable');
                      if (document.getElementById('shading-methods') || document.getElementById('shading-confirm-message'))
                        throw new Error('Packaged Shading Correction contains obsolete method controls');
                      await step('shading-status', async () => {
                        const deadline = Date.now() + 10000;
                        while (!view.eval('state.shading && !state.shading.snapshot().refreshing && !state.shading.snapshot().view_stale && state.shading.snapshot().state !== "checking"') ||
                            !shadingStates.has(frame.dataset.sid)) {
                          if (Date.now() >= deadline) throw new Error('Shading Correction status did not arrive');
                          await new Promise(resolve => setTimeout(resolve, 50));
                        }
                      });
                      const shadingStatus = view.eval('state.shading.snapshot()');
                      if (typeof shadingStatus.applied !== 'boolean' || typeof shadingStatus.can_cancel !== 'boolean' ||
                          !(shadingStatus.operation === null || ['apply', 'revoke'].includes(shadingStatus.operation)) ||
                          shadingStatus.generation !== view.eval('state.info.generation') || shadingStatus.state === 'failed' ||
                          !shadingButton.title.includes('Shading Correction'))
                        throw new Error('Shading Correction did not initialize its committed pyramid state');
                      const shading = window.Nd2ShadingCorrection;
                      const persistentActions = {
                        apply: shading.nextAction({state: 'idle', supported: true, applied: false, methods: {bffree: {supported: true}}}),
                        revoke: shading.nextAction({state: 'ready', supported: true, applied: true, method: 'h'}),
                        unavailable: shading.nextAction({state: 'idle', supported: true, applied: false, methods: {h: {supported: true}, bffree: {supported: false}}}),
                        cancel: shading.nextAction({state: 'rebuilding', operation: 'revoke', applied: true, can_cancel: true}),
                        publishing: shading.nextAction({state: 'publishing', operation: 'revoke', applied: true, can_cancel: false}),
                      };
                      if (persistentActions.apply !== 'start' || persistentActions.revoke !== 'revoke' || persistentActions.unavailable !== null ||
                          persistentActions.cancel !== 'cancel' || persistentActions.publishing !== null)
                        throw new Error('Persistent Shading Correction actions are unavailable');
                      const progressCheck = window.Nd2ShadingCorrection.progress({
                        state: 'preparing_display', operation: 'apply', can_cancel: true, pct: 63, eta_seconds: 75});
                      const revokeProgress = window.Nd2ShadingCorrection.progress({
                        state: 'rebuilding', operation: 'revoke', applied: true, can_cancel: true, pct: 41, eta_seconds: 64});
                      if (!progressCheck.visible || progressCheck.pct !== 63 ||
                          progressCheck.stage !== 'Building corrected pyramid' || !progressCheck.canCancel ||
                          !progressCheck.eta.includes('remaining') || !revokeProgress.visible ||
                          revokeProgress.title !== 'Revoke Shading Correction' ||
                          revokeProgress.stage !== 'Rebuilding original pyramid' || !revokeProgress.canCancel ||
                          !revokeProgress.eta.includes('remaining'))
                        throw new Error('Shading Correction progress display is unavailable');
                      if (__CLIPBOARD_CHECK__) {
                        progress.phase = 'clipboard-transport';
                        const native = view.parent?.pywebview;
                        if (native?.platform !== 'edgechromium' ||
                            typeof native.api?.write_lut_clipboard !== 'function' ||
                            typeof native.api?.read_lut_clipboard !== 'function')
                          throw new Error('Native Windows LUT clipboard bridge is unavailable');
                        progress.clipboard_transport = 'native-winforms';
                      }
                      // Auto-LUT loading must settle before a same-values round trip.
                      if (__CLIPBOARD_CHECK__) await step('autolut', async () => {
                        const deadline = Date.now() + 15000;
                        while (view.eval('state.lutAutoPending.some(Boolean)')) {
                          if (Date.now() >= deadline) throw new Error('LUT smoke autolut timed out after 15000 ms');
                          await new Promise(resolve => setTimeout(resolve, 50));
                        }
                      }, 15000);
                      const assets = ['fluent-edit-20-regular.svg', 'fluent-copy-20-regular.svg', 'fluent-clipboard-paste-20-regular.svg',
                        'fluent-checkmark-20-regular.svg', 'fluent-circle-highlight-20-regular.svg'];
                      await step('icons', () => Promise.all(assets.map(async name => {
                        const response = await view.fetch(new URL('../../static/icons/' + name, page.baseURI));
                        if (!response.ok) throw new Error('LUT icon ' + name + ': HTTP ' + response.status);
                        const svg = new view.DOMParser().parseFromString(await response.text(), 'image/svg+xml');
                        if (svg.documentElement.tagName !== 'svg' || svg.documentElement.getAttribute('viewBox') !== '0 0 20 20' ||
                            !svg.querySelector('path')) throw new Error('Invalid packaged LUT icon: ' + name);
                      })));
                      const infoImages = await step('info-images', async () => {
                        const inspector = view.eval('state.windows.info');
                        const wasHidden = inspector.isHidden();
                        await view.loadSlideInspector();
                        inspector.open();
                        const names = view.eval('state.inspect && state.inspect.associated');
                        if (!Array.isArray(names)) throw new Error('Slide Info metadata did not load');
                        const images = [...page.querySelectorAll('#info-associated img')];
                        if (images.length !== names.length) throw new Error('Slide Info omitted embedded images');
                        const loaded = async () => {
                          const deadline = Date.now() + 8000;
                          while (images.some(image => !image.complete || !image.naturalWidth)) {
                            if (Date.now() >= deadline) throw new Error('Slide Info image did not load');
                            await new Promise(resolve => setTimeout(resolve, 40));
                          }
                        };
                        await loaded();
                        const dimensions = images.map(image => [image.naturalWidth, image.naturalHeight]);
                        let enlarged = null;
                        if (images.length) {
                          const image = images.find(item => !item.src.includes('/thumbnail.jpg')) || images[0];
                          const button = image.parentElement;
                          button.click();
                          await loaded();
                          if (button.getAttribute('aria-expanded') !== 'true' ||
                              !image.src.includes('detail=1') ||
                              Math.max(image.naturalWidth, image.naturalHeight) > 2048)
                            throw new Error('Slide Info enlargement is unavailable');
                          enlarged = [image.naturalWidth, image.naturalHeight];
                          button.click();
                        }
                        if (wasHidden) inspector.close();
                        return {names, dimensions, enlarged};
                      });
                      const outcome = {ok: true, phase: 'complete', module: true, handlers: true, icons: assets,
                        info_images: infoImages,
                        shading_correction: {state: shadingStatus.state, applied: shadingStatus.applied,
                          method: shadingStatus.method, methods: shadingStatus.methods,
                          operation: shadingStatus.operation, can_cancel: shadingStatus.can_cancel,
                          generation: shadingStatus.generation, actions: persistentActions,
                          progress: progressCheck, revoke_progress: revokeProgress, toolbar: true, confirmation: true},
                        clipboard_api: {write: typeof view.navigator.clipboard?.writeText === 'function',
                          read: typeof view.navigator.clipboard?.readText === 'function'}};
                      if (__CLIPBOARD_CHECK__) {
                        const current = () => view.eval('window.Nd2LutTransfer.encode(state.info.channels, state.luts, state.info.dtype)');
                        const before = current();
                        const expected = view.eval('window.Nd2LutTransfer.planPaste(window.Nd2LutTransfer.encode(state.info.channels, state.luts, state.info.dtype), state.info.channels, state.info.dtype).updates.length');
                        if (!expected) throw new Error('LUT clipboard fixture has no uniquely named compatible channels');
                        const [, copy, paste] = buttons;
                        const toast = () => page.getElementById('toast')?.textContent || 'No result reported';
                        await step('copy', () => copy.onclick());
                        if (copy.disabled || !copy.classList.contains('is-copied') || toast() !== 'Channel LUTs copied')
                          throw new Error('LUT clipboard copy failed: ' + toast());
                        await step('paste', () => paste.onclick());
                        if (paste.disabled || !toast().startsWith('Pasted ' + expected + ' channel LUT'))
                          throw new Error('LUT clipboard paste failed: ' + toast());
                        if (current() !== before) throw new Error('LUT clipboard round trip changed effective channel values');
                        outcome.clipboard_roundtrip = {ok: true, channels: expected, effective_values_unchanged: true,
                          transport: progress.clipboard_transport};
                      }
                      return outcome;
                    })().then(value => { frame.__nd2wsiSmokeLut = value; })
                      .catch(error => { frame.__nd2wsiSmokeLut = {...progress, pending: false, ok: false,
                        error: error.message || String(error)}; });
                  }
                  const handoff = __HANDOFF_CHECK__ && frame?.contentWindow.nd2HandoffApplied === true;
                  return {bridge: true, version: bridge.version,
                    phase: slide ? frame.__nd2wsiSmokeLut?.phase || 'lut-toolbar' : frame ? 'slide-render' : 'slide-load',
                    ...(__HANDOFF_CHECK__ ? {handoff_applied:handoff,
                      observed_display:handoff ? frame.contentWindow.nd2CaptureViewState() : null} : {}),
                    boot: !!document.getElementById('drop'),
                    slide, lut_toolbar: frame?.__nd2wsiSmokeLut || null};
                })()""".replace("__HANDOFF_CHECK__", "true" if expected is not None else "false")
                    .replace("__CLIPBOARD_CHECK__", "true" if clipboard_check else "false"))
                if result:
                    report["last_result"] = result
                    report["phase"] = result.get("phase", (result.get("lut_toolbar") or {}).get("phase", report["phase"]))
                if result and result.get("bridge") and result.get("slide" if expect_slide else "boot"):
                    if expect_slide and (result.get("lut_toolbar") or {}).get("ok") is not True:
                        if (result.get("lut_toolbar") or {}).get("ok") is False:
                            report.update(result)
                            report["error"] = result["lut_toolbar"]["error"]
                            break
                        time.sleep(0.05)
                        continue
                    if expected is not None:
                        from .view_state import matches_view_state

                        if not result.get("handoff_applied"):
                            time.sleep(0.05)
                            continue
                        report.update(result)
                        if not matches_view_state(expected, result.get("observed_display")):
                            raise ValueError("Actual browser display state differs from the requested handoff")
                    native_keyboard = _gui_smoke_native_keyboard(api, window, slide=bool(result.get("slide")))
                    report.update(result, native_keyboard=native_keyboard,
                                  ok=native_keyboard["status"] != "failed", phase="complete")
                    if native_keyboard["status"] == "failed":
                        report.update(phase="native-keyboard", error=native_keyboard.get("error") or
                                      native_keyboard.get("cleanup_error"))
                    elif expect_slide:
                        from .translucent_smoke import exercise_translucent_viewer

                        tracing = exercise_translucent_viewer(api, window)
                        report["translucent_viewer"] = tracing
                        if not tracing.get("ok"):
                            report.update(ok=False, phase="translucent-viewer",
                                          error=tracing.get("error", "Translucent viewer check failed"))
                    if report["ok"] and expect_slide and sys.platform == "win32":
                        from .window_smoke import exercise_independent_window

                        independent = exercise_independent_window(api)
                        report["independent_window"] = independent
                        if not independent.get("ok"):
                            report.update(ok=False, phase="independent-window",
                                          error=independent.get("error", "Independent window check failed"))
                    break
            except Exception as exc:
                report["last_error"] = str(exc)
            time.sleep(0.25)
        else:
            raise TimeoutError(f"GUI did not finish within {timeout:g} seconds (last phase: {report['phase']})")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        # The GUI's main loop can return as soon as the window closes.
        _gui_smoke_close(api, window, report, timeout=close_timeout)
    return report


class _PywebviewDaemonThread(threading.Thread):
    """``threading.Thread`` for pywebview's bridge, always a daemon."""

    def __init__(self, *args, **kwargs):
        kwargs["daemon"] = True
        super().__init__(*args, **kwargs)


def _daemonize_pywebview_bridge_threads(webview) -> None:
    """Run pywebview's JS-bridge threads as daemons so a closed app can exit.

    ``webview.util`` starts every JavaScript-to-Python call on a fresh
    non-daemon thread whose reply goes back through ``window.evaluate_js``,
    and that waits without a deadline: Cocoa for a main-queue block, WinForms
    for an ExecuteScriptAsync continuation lost once ``Application.Exit`` has
    run. A call racing the close (the shell's gesture-scope update scheduled
    by every save handshake, for one) then never returns, and interpreter
    shutdown joins that thread forever: the window is gone, a GUI smoke has
    written its report, and the process still holds the source memory map.
    Nothing that must outlive the window runs there: saves and exports are
    confirmed before a close is approved, and an unfinished cache build is
    staged and cleaned up like any interrupted one.
    """
    util = getattr(webview, "util", None)
    if util is not None and isinstance(getattr(util, "Thread", None), type):
        util.Thread = _PywebviewDaemonThread


_DIAGNOSTIC_RUN_FLAGS = ("--gui-smoke", "--handoff-check-report", "--browser-replay-report")

# How long an interactive exit waits for slide reads, exports and plate writers
# to let go of their sources once the window has closed.
_EXIT_TEARDOWN_TIMEOUT = 10.0
_EXIT_TEARDOWN_UNFINISHED = threading.Event()


def _stop_server_at_exit(api, *, diagnostic: bool) -> None:
    """Release the server after the GUI loop has returned.

    This is the strict teardown an update uses: it never closes a memory map
    or a plate writer while a reader still holds it. Sparkle's preparation has
    already run it before the app quits, and a diagnostic run waits for it
    without limit, so a teardown that hangs still fails its CI step.

    An interactive run's window is already gone, and a read from a share that
    has gone away never returns. Waiting for it would leave a process nobody
    can see, holding its window-session lock (an update in any other window
    then refuses), its plate writer lease and its memory maps. So the teardown
    gets ``_EXIT_TEARDOWN_TIMEOUT`` seconds on a daemon thread. If it is still
    waiting, it is left running, ``_EXIT_TEARDOWN_UNFINISHED`` is set, and
    ``run()`` ends the process after ``main()`` has recorded the session
    closed. The kernel releases the locks; an abandoned export leaves only its
    temporary file.
    """
    if diagnostic:
        api.stop_server_for_update()
        return
    finished = threading.Event()
    failure = []

    def stop():
        try:
            api.stop_server_for_update()
        except BaseException as exc:
            failure.append(exc)
        finally:
            finished.set()

    threading.Thread(target=stop, name="exit-teardown", daemon=True).start()
    if not finished.wait(_EXIT_TEARDOWN_TIMEOUT):
        _dlog(f"exit: slide teardown still waiting after {_EXIT_TEARDOWN_TIMEOUT:g} s; ending without it")
        _EXIT_TEARDOWN_UNFINISHED.set()
    elif failure:
        raise failure[0]


def run(argv: list[str] | None = None) -> int:
    """Process entry point: ``main()``, then end a diagnostic run at once.

    When ``main()`` returns from a GUI smoke or another diagnostic run, its
    report is written and its server and window session are closed. Exiting
    with ``os._exit`` then means no thread that is still waiting (see
    ``_daemonize_pywebview_bridge_threads``) can turn a finished run into a
    CI timeout. Interactive runs return normally unless their slide teardown
    did not finish in time (``_stop_server_at_exit``); ``main()`` itself always
    returns, so callers and tests can inspect its result.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    result = main(arguments)
    if any(argument.split("=", 1)[0] in _DIAGNOSTIC_RUN_FLAGS for argument in arguments):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(result)
    if _EXIT_TEARDOWN_UNFINISHED.is_set():
        # A slide read still holds a source. Interpreter shutdown joins
        # executor workers, where zarr runs store reads, and could wait for
        # it forever; end at once instead.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(result)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME, description=__doc__)
    ap.add_argument("nd2", nargs="?", help="ND2, SVS, SDPC, JPEG or PNG file to open at launch")
    ap.add_argument("--smoke", action="store_true", help="headless self-test")
    ap.add_argument("--gui-smoke", action="store_true", help="open, verify and close the native window")
    ap.add_argument("--smoke-report", type=Path, help="write the native GUI self-test result as JSON")
    ap.add_argument("--handoff-state", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--handoff-check-report", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--open-attempt-id", help=argparse.SUPPRESS)
    ap.add_argument("--fallback-consumed", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--renderer", choices=("auto", "browser", "metal"), help=argparse.SUPPRESS)
    ap.add_argument("--prefer-metal", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--retry-metal", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--browser-replay-report", type=Path, help="write an isolated Agent browser replay diagnostic")
    ap.add_argument("--benchmark-context", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--benchmark-test-root", type=Path, help=argparse.SUPPRESS)
    role_args = ap.add_mutually_exclusive_group()
    role_args.add_argument("--new-window", action="store_true", help="open a fresh User window")
    role_args.add_argument("--agent-window", action="store_true", help="open an isolated Agent window")
    args, _ = ap.parse_known_args(argv)  # tolerate Finder's -psn_* args

    initial = Path(args.nd2).expanduser() if args.nd2 else None
    if args.handoff_check_report:
        if (sys.platform != "darwin" or not args.agent_window or initial is None
                or args.benchmark_test_root is None
                or not initial.resolve().is_relative_to(args.benchmark_test_root.resolve())):
            print("Window verification requires a macOS Agent and isolated test root", file=sys.stderr)
            return 2
        args.gui_smoke = True
        args.smoke_report = args.handoff_check_report
    if args.browser_replay_report:
        if (sys.platform != "darwin" or not args.agent_window or initial is None
                or args.benchmark_context is None or args.benchmark_test_root is None
                or not initial.resolve().is_relative_to(args.benchmark_test_root.resolve())):
            print("Browser replay requires a macOS Agent window and source inside an explicit test root", file=sys.stderr)
            return 2
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

    api = None
    window_session = None
    try:
        if sys.platform in ("darwin", "win32"):
            from .window_sessions import create_window_session

            window_session = create_window_session("agent" if args.agent_window else "user")
            api, window = create_app_window(initial, window_session=window_session)
            from .renderer_policy import open_attempt_id

            api._open_attempt_id = open_attempt_id(args.open_attempt_id)
            api._fallback_consumed = args.fallback_consumed
        else:
            api, window = create_app_window(initial)
        if args.handoff_state:
            if sys.platform != "darwin" or initial is None:
                raise ValueError("Display handoff requires a macOS source opening")
            from .view_state import read_handoff

            api._handoff_state = read_handoff(args.handoff_state, source=initial, role=window_session.role)
            api._requested_handoff = api._handoff_state
            api._handoff_source = initial.resolve()
        kwargs = {"gui": "edgechromium"} if sys.platform == "win32" else {}
        if window_session is not None:
            kwargs["private_mode"] = True
        _daemonize_pywebview_bridge_threads(webview)
        if args.browser_replay_report:
            from .viewport_benchmark import run_browser_replay

            api._browser_benchmark_enabled = True
            window._nd2_benchmark_input_authorized = window_session.role == "agent"
            webview.start(lambda: run_browser_replay(api, window, args.browser_replay_report,
                                                    args.benchmark_context, args.benchmark_test_root), **kwargs)
        elif args.gui_smoke:
            webview.start(lambda: gui_smoke(api, window, expect_slide=initial is not None), **kwargs)
        else:
            webview.start(**kwargs)
        if api._startup_error:
            raise RuntimeError(api._startup_error)
        # Snapshot: the smoke records its close outcome before the loop returns.
        report = dict(getattr(api, "_gui_smoke_report", {"ok": False, "error": "GUI closed before verification"}))
        result = 0 if not args.gui_smoke or report["ok"] else 4
        if args.browser_replay_report:
            report = getattr(api, "_browser_replay_report", {"ok": False, "error": "Replay did not complete"})
            result = 0 if report.get("ok") else 4
    except Exception as exc:
        message = f"Could not start {APP_NAME}: {exc}"
        _startup_error(message, show_dialog=not (args.gui_smoke or args.browser_replay_report))
        report = {"ok": False, "error": message, "platform": sys.platform}
        result = 4
    finally:
        try:
            if api is not None:
                _stop_server_at_exit(api, diagnostic=bool(args.gui_smoke or args.browser_replay_report))
        finally:
            if window_session is not None:
                try:
                    window_session.mark_closed()
                except Exception as exc:
                    _dlog(f"window session close record failed: {exc!r}")
    if args.gui_smoke:
        print(json.dumps(report, ensure_ascii=False))
        if args.smoke_report:
            args.smoke_report.parent.mkdir(parents=True, exist_ok=True)
            if args.handoff_check_report:
                with args.smoke_report.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            else:
                args.smoke_report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.browser_replay_report and not getattr(api, "_browser_replay_report", None):
        try:
            args.browser_replay_report.parent.mkdir(parents=True, exist_ok=True)
            with args.browser_replay_report.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except OSError as exc:
            print(f"Could not create replay failure report: {exc}", file=sys.stderr)
    return result


def create_app_window(initial: Path | None, window_session=None):
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
    if sys.platform in ("darwin", "win32") and window_session is None:
        from .window_sessions import create_window_session

        window_session = create_window_session("user")
    if sys.platform == "darwin":
        # Cocoa's legacy DOM move handler mixes global mouse coordinates with
        # the startup display's origin. Only the native handoff may move a Mac
        # window; Windows keeps pywebview and its ordinary native title bar.
        webview.settings["DRAG_REGION_SELECTOR"] = ":not(*)"
    api = Api(initial, window_session=window_session)
    title = APP_NAME
    if window_session is not None:
        title += f" — {window_session.role.title()} · {window_session.id}"
    window = webview.create_window(
        title,
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
        frameless=sys.platform == "darwin",
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
    window.events.closed += api._close_translucent_windows
    _wire_file_drop(window)
    if sys.platform == "darwin":
        _inline_traffic_lights(window)
        api._titlebar_drag = _window_chrome.NativeTitlebarDrag(window, logger=_dlog)
        _install_open_files_handler()
        window.events.shown += lambda: _install_window_menu(api, window)
        _guard_native_close(api, window)
        if not api._updates_disabled:
            window.events.shown += lambda: _install_app_updater(api, window)
    elif sys.platform == "win32":
        def require_webview2(renderer):
            # pywebview can silently fall back to legacy MSHTML even when
            # edgechromium was requested. Never show a broken legacy viewer.
            if renderer != "edgechromium":
                api._startup_error = (
                    "Microsoft Edge WebView2 Runtime is required. Install it from "
                    "https://developer.microsoft.com/microsoft-edge/webview2/ and reopen the app."
                )
                return False
            return True

        window.events.initialized += require_webview2
        _guard_native_close(api, window)
    return api, window


if __name__ == "__main__":
    sys.exit(run())
