"""nd2wsi-viewer: the native macOS and Windows shell.

A WKWebView or Edge WebView2 window around the same local server the CLI
uses.  Opening a slide converts it once (pyramid + sidecar live next to the
ND2), then the viewer loads from an ephemeral localhost port.

    nd2wsi-viewer [slide.nd2|.svs]   # window; drag-drop / dialog if no file
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
RELEASES_URL = "https://github.com/myunghyunj/nd2wsi-viewer/releases/latest"
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
  <div class="sub"><b>.nd2</b>, <b>.svs</b>, or <b>.nd2svs</b> &nbsp;·&nbsp; click anywhere to browse</div>
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
    from .cache import SINGLE_FILE_SUFFIX
    from .convert import ensure_cache, existing_cache_store
    from .plate import is_plate_file
    from .svs import is_svs

    if nd2_path.suffix.lower() == SINGLE_FILE_SUFFIX:
        if on_status:
            on_status(f"opening {nd2_path.name} …")
        return nd2_path
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
            good = [p for p in paths if p.lower().endswith((".nd2", ".svs", ".nd2svs"))]
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
            if p.lower().endswith((".nd2", ".svs", ".nd2svs")):
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
        self._updates_disabled = sys.platform == "darwin" and (
            window_session is not None or os.environ.get("ND2WSI_WINDOW_CHILD") == "1"
        )
        self._server_lock = threading.Lock()
        if native_gesture_scopes is None:
            from .native_gestures import NativeGestureScopeCache

            native_gesture_scopes = NativeGestureScopeCache()
        self._native_gesture_scopes = native_gesture_scopes

    def new_window(self, role: str = "agent") -> dict:
        """Automation defaults to a separate Agent process, never the User window."""
        from .window_launch import launch_window

        return launch_window(role, app_name=APP_NAME)

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
                    "agent_directive": "Use an isolated macOS Agent window for automation."}
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
                file_types=("Slides and caches (*.nd2;*.svs;*.nd2svs)",),
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
            if p.suffix.lower() not in (".nd2", ".svs", ".nd2svs"):
                self._status = f"{p.name}: not an .nd2, .svs, or .nd2svs file"
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
            file_types=("Slides and caches (*.nd2;*.svs;*.nd2svs)",),
        )
        return [str(p) for p in picked] if picked else None

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
            # apply Agent policy. Existing ND2 caches, native SVS and plates
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


def gui_smoke(api: Api, window, *, expect_slide: bool = False, timeout: float = 90) -> dict:
    """Exercise the real renderer, bridge and slide DOM, then close the app."""
    started = time.monotonic()
    report = {"ok": False, "platform": sys.platform, "renderer": None}
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
            try:
                result = window.evaluate_js("""(() => {
                  const api = window.pywebview && window.pywebview.api;
                  if (!api || !api.update_status || document.readyState !== 'complete') return null;
                  if (!window.__nd2wsiSmokeBridge) {
                    window.__nd2wsiSmokeBridge = 'pending';
                    api.update_status().then(status => { window.__nd2wsiSmokeBridge = status; })
                      .catch(() => { window.__nd2wsiSmokeBridge = 'failed'; });
                  }
                  const bridge = window.__nd2wsiSmokeBridge;
                  if (!bridge || typeof bridge !== 'object' || !bridge.version) return null;
                  const frame = document.querySelector('#frames iframe.active');
                  const page = frame && frame.contentDocument;
                  const canvas = page && page.querySelector('#stage canvas');
                  const plate = page && page.querySelector('#plate img');
                  const handoff = __HANDOFF_CHECK__ && frame?.contentWindow.nd2HandoffApplied === true;
                  return {bridge: true, version: bridge.version,
                    ...(__HANDOFF_CHECK__ ? {handoff_applied:handoff,
                      observed_display:handoff ? frame.contentWindow.nd2CaptureViewState() : null} : {}),
                    boot: !!document.getElementById('drop'),
                    slide: !!(frame && typeof readyFrames !== 'undefined' &&
                      readyFrames.has(frame.dataset.sid) &&
                      ((canvas && canvas.width > 0 && canvas.height > 0) ||
                      (plate && plate.complete && plate.naturalWidth > 0)))};
                })()""".replace("__HANDOFF_CHECK__", "true" if expected is not None else "false"))
                if result and result.get("bridge") and result.get("slide" if expect_slide else "boot"):
                    if expected is not None:
                        from .view_state import matches_view_state

                        if not result.get("handoff_applied"):
                            time.sleep(0.05)
                            continue
                        report.update(result)
                        if not matches_view_state(expected, result.get("observed_display")):
                            raise ValueError("Actual browser display state differs from the requested handoff")
                    report.update(result, ok=True)
                    break
            except Exception as exc:
                report["last_error"] = str(exc)
            time.sleep(0.25)
        else:
            raise TimeoutError("GUI did not finish loading the app bridge and slide within 90 seconds")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        # Set the result before destroy; the GUI's main loop can return at once.
        api._gui_smoke_report = report
        window.destroy()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME, description=__doc__)
    ap.add_argument("nd2", nargs="?", help="ND2 or SVS file to open at launch")
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
    role_args.add_argument("--new-window", action="store_true", help="open a fresh macOS User window")
    role_args.add_argument("--agent-window", action="store_true", help="open an isolated macOS Agent window")
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
        if sys.platform == "darwin":
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
        if sys.platform == "darwin":
            kwargs["private_mode"] = True
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
        report = getattr(api, "_gui_smoke_report", {"ok": False, "error": "GUI closed before verification"})
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
                api.stop_server_for_update()
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
    if sys.platform == "darwin" and window_session is None:
        from .window_sessions import create_window_session

        window_session = create_window_session("user")
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
    _wire_file_drop(window)
    if sys.platform == "darwin":
        _inline_traffic_lights(window)
        _install_open_files_handler()
        window.events.shown += lambda: _install_window_menu(api, window)
        api._close_coordinator = WindowCloseCoordinator(api, window)
        window.events.closing += api._close_coordinator.on_closing
        window.events.closed += api._close_coordinator.cancel_preparation
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
    return api, window


if __name__ == "__main__":
    sys.exit(main())
