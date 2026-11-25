"""Window-owned, display-only fluorescence tracing overlays.

The overlay uses the owning viewer's gated tile server, but has its own native
top-level window. It never joins the parent's native child-window hierarchy,
which would make it disappear when the parent is minimized.
"""

from __future__ import annotations

import copy
import json
import math
import re
import sys
import threading
import time
import urllib.parse
import uuid
from functools import lru_cache
from pathlib import Path

from .window_chrome import NativeTitlebarDrag

OPACITY = 0.55
MIN_OPACITY = 0.10
OPACITY_TIMEOUT = 5
MIN_SIZE = (220, 160)
MAX_SIZE = 8192
_LOGPIXELSX = 88
# Match nd2wsi/render.py without eagerly importing numpy at module load time.
_FLOAT32_SAFE_BOUND = 3e38
_HEX6_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


@lru_cache(maxsize=1)
def _win32_has_per_window_dpi() -> bool:
    """True when ``user32.GetDpiForWindow`` (Windows 10 build 14393+) is exported."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        windll_cls = getattr(ctypes, "WinDLL", None)
        if windll_cls is None:
            return False
        user32 = windll_cls("user32", use_last_error=True)
        return getattr(user32, "GetDpiForWindow", None) is not None
    except Exception:
        return False


@lru_cache(maxsize=1)
def _win32_system_dpi_scale() -> float:
    """Cached logon-session system DPI scale (LOGPIXELSX / 96) for pre-1607 Windows 10."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        from ctypes import wintypes

        windll_cls = getattr(ctypes, "WinDLL", None)
        if windll_cls is None:
            return 1.0
        user32 = windll_cls("user32", use_last_error=True)
        gdi32 = windll_cls("gdi32", use_last_error=True)
        get_dc = user32.GetDC
        get_dc.argtypes = [wintypes.HWND]
        get_dc.restype = wintypes.HDC
        release_dc = user32.ReleaseDC
        release_dc.argtypes = [wintypes.HWND, wintypes.HDC]
        release_dc.restype = ctypes.c_int
        get_caps = gdi32.GetDeviceCaps
        get_caps.argtypes = [wintypes.HDC, ctypes.c_int]
        get_caps.restype = ctypes.c_int
        hdc = get_dc(0)
        if not hdc or isinstance(hdc, bool):
            return 1.0
        try:
            dpi = get_caps(hdc, _LOGPIXELSX)
        finally:
            release_dc(0, hdc)
        if (
            not isinstance(dpi, bool)
            and isinstance(dpi, (int, float))
            and math.isfinite(dpi)
            and dpi > 0
        ):
            return float(dpi) / 96.0
    except Exception:
        pass
    return 1.0


def win32_window_scale(native) -> float:
    """Physical-per-CSS pixel scale for a Windows BrowserForm (including pre-1607 Win10)."""
    if sys.platform == "win32" and not _win32_has_per_window_dpi():
        sys_scale = _win32_system_dpi_scale()
        if sys_scale != 1.0:
            return sys_scale
    scale = getattr(native, "_scale", None)
    if (
        not isinstance(scale, bool)
        and isinstance(scale, (int, float))
        and math.isfinite(scale)
        and scale > 0
    ):
        return float(scale)
    return 1.0


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range")
    return float(value)


def validate_roi(store, payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid ROI rectangle")
    level = store.attrs["nd2wsi"]["levels"][0]
    width, height = level["width"], level["height"]
    rectangle = {key: _number(payload.get(key), "ROI rectangle", 0, limit)
                 for key, limit in (("x", width), ("y", height), ("w", width), ("h", height))}
    if (rectangle["w"] <= 0 or rectangle["h"] <= 0
            or rectangle["x"] + rectangle["w"] > width
            or rectangle["y"] + rectangle["h"] > height):
        raise ValueError("ROI rectangle is outside the slide")
    return rectangle


def validate_display_params(store, params) -> list[tuple[str, str]]:
    """Validate and canonicalize display-only parameters (c, win, col)."""
    if not isinstance(params, dict):
        raise ValueError("Invalid display settings")
    channels = store.attrs["omero"]["channels"]
    n_channels = len(channels)
    result: list[tuple[str, str]] = []

    selected = params.get("c")
    if selected not in (None, ""):
        if not isinstance(selected, str):
            raise ValueError("Invalid channel selection")
        if selected == "none":
            result.append(("c", "none"))
        else:
            indices = selected.split(",")
            seen: set[int] = set()
            for token in indices:
                if (
                    not token
                    or not token.isascii()
                    or not token.isdecimal()
                    or (len(token) > 1 and token.startswith("0"))
                ):
                    raise ValueError("Invalid channel selection")
                idx = int(token)
                if not 0 <= idx < n_channels or idx in seen:
                    raise ValueError("Invalid channel selection")
                seen.add(idx)
            result.append(("c", selected))

    raw_win = params.get("win")
    if raw_win is not None:
        if not isinstance(raw_win, str):
            raise ValueError("Invalid channel window")
        slots = raw_win.split(",")
        if len(slots) > n_channels:
            raise ValueError("Too many channel windows")
        for slot in slots:
            if slot == "":
                continue
            parts = slot.split(":")
            if len(parts) not in (2, 3):
                raise ValueError("Invalid channel window")
            values: list[float] = []
            for tok in parts:
                if not tok or tok != tok.strip() or any(ch.isspace() for ch in tok) or "_" in tok:
                    raise ValueError("Invalid channel window")
                try:
                    val = float(tok)
                except ValueError as exc:
                    raise ValueError("Invalid channel window") from exc
                if not math.isfinite(val):
                    raise ValueError("Invalid channel window")
                values.append(val)
            lo, hi = values[0], values[1]
            if (
                abs(lo) >= _FLOAT32_SAFE_BOUND
                or abs(hi) >= _FLOAT32_SAFE_BOUND
                or not math.isfinite(hi - lo)
                or hi <= lo
            ):
                raise ValueError("Invalid channel window")
            if len(values) == 3 and not 0.1 <= values[2] <= 10.0:
                raise ValueError("Invalid channel gamma")
        if any(slot != "" for slot in slots):
            result.append(("win", raw_win))

    raw_col = params.get("col")
    if raw_col is not None:
        if not isinstance(raw_col, str):
            raise ValueError("Invalid channel color")
        tokens = raw_col.split(",")
        if len(tokens) > n_channels:
            raise ValueError("Too many channel colors")
        cleaned_tokens: list[str] = []
        for token in tokens:
            if token == "":
                cleaned_tokens.append("")
                continue
            cleaned = token[1:] if token.startswith("#") else token
            if not _HEX6_RE.fullmatch(cleaned):
                raise ValueError("Invalid channel color")
            cleaned_tokens.append(cleaned)
        if any(cleaned_tokens):
            result.append(("col", ",".join(cleaned_tokens)))

    return result


def validate_display_query(store, display_query: str) -> list[tuple[str, str]]:
    """Validate a display-only query string containing only c, win, and/or col."""
    if not isinstance(display_query, str) or len(display_query) > 16384:
        raise ValueError("Invalid display settings")
    body = display_query.removeprefix("?")
    pairs = urllib.parse.parse_qsl(
        body,
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=3,
    )
    params = dict(pairs)
    if len(params) != len(pairs) or set(params) - {"c", "win", "col"}:
        raise ValueError("Unsupported display setting")
    return validate_display_params(store, params)


def merge_display_query(store, base_query: str, display_query: str) -> str:
    """Merge validated display overrides onto immutable launch base keys (g, t, p, z)."""
    display_pairs = validate_display_query(store, display_query)
    if not isinstance(base_query, str):
        raise ValueError("Invalid base query")
    base_pairs = urllib.parse.parse_qsl(base_query.removeprefix("?"), keep_blank_values=True)
    base_map = dict(base_pairs)
    preserved = [(key, base_map[key]) for key in ("g", "t", "p", "z") if key in base_map]
    merged = display_pairs + preserved
    return "?" + urllib.parse.urlencode(merged) if merged else ""


def validate_context(store, payload, *, slide_url):
    """Accept only a camera and tile-render settings for a registered slide."""
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Unsupported translucent-viewer state")
    meta = store.attrs["nd2wsi"]
    if meta.get("rgb") and meta.get("raster") is not True:
        raise ValueError("The translucent viewer requires a fluorescence slide")
    width, height = (meta["levels"][0][key] for key in ("width", "height"))
    center = payload.get("center")
    if not isinstance(center, list) or len(center) != 2:
        raise ValueError("Invalid image center")
    # OSD permits a little space outside an image while panning. Preserve it.
    center = [_number(value, "Image center", -8 * limit, 9 * limit)
              for value, limit in zip(center, (width, height))]
    zoom = _number(payload.get("zoom"), "Image scale", 1e-12, 1024)
    rotation = _number(payload.get("rotation", 0), "Rotation", -360000, 360000) % 360
    flip = payload.get("flip", False)
    if type(flip) is not bool:
        raise ValueError("Flip must be boolean")
    query = payload.get("query", "")
    if not isinstance(query, str) or len(query) > 16384:
        raise ValueError("Invalid display settings")
    pairs = urllib.parse.parse_qsl(query.removeprefix("?"), keep_blank_values=True,
                                  strict_parsing=True, max_num_fields=7)
    params = dict(pairs)
    if len(params) != len(pairs) or set(params) - {"c", "win", "g", "t", "p", "z", "col"}:
        raise ValueError("Unsupported display setting")
    display_map = dict(validate_display_params(store, params))
    generation = getattr(store, "generation", "")
    if params.get("g", "") != generation:
        raise ValueError("The image changed; refresh the slide before opening the overlay")
    plate = getattr(store, "plate", None)
    if plate is not None:
        for name, size in (("t", plate.T), ("p", plate.P), ("z", plate.Z)):
            value = params.get(name, "")
            if not value.isascii() or not value.isdecimal() or not 0 <= int(value) < size:
                raise ValueError("Choose a valid plate site, time and Z plane first")
    elif set(params) & {"t", "p", "z"}:
        raise ValueError("Plane selection does not match this slide")
    canonical_pairs = []
    for key, value in pairs:
        if key in ("c", "win", "col"):
            if key in display_map:
                canonical_pairs.append((key, display_map[key]))
        else:
            canonical_pairs.append((key, value))
    cleaned = urllib.parse.urlencode(canonical_pairs)
    context = {"version": 1, "slide_url": slide_url, "query": "?" + cleaned if cleaned else "",
               "center": center, "zoom": zoom, "rotation": rotation, "flip": flip,
               "dimensions": [width, height],
               "opacity": OPACITY, "opacity_mode": "native" if sys.platform == "win32" else "layer",
               "locked": False}
    if payload.get("roi") is not None:
        context["roi"] = validate_roi(store, payload["roi"])
    if "rois" in payload:
        rois = payload["rois"]
        if not isinstance(rois, list) or len(rois) > 2000:
            raise ValueError("The translucent viewer supports at most 2000 imported ROIs")
        cleaned_rois, indices = [], set()
        for roi in rois:
            rectangle = validate_roi(store, roi)
            label, index = roi.get("label"), roi.get("index")
            if not isinstance(label, str) or len(label) > 1000:
                raise ValueError("Invalid ROI label")
            if type(index) is not int or not 1 <= index <= 2**53 - 1 or index in indices:
                raise ValueError("ROI indices must be positive, unique safe integers")
            indices.add(index)
            cleaned_rois.append({**rectangle, "label": label, "index": index})
        context["rois"] = cleaned_rois
    if "roiIndex" in payload:
        selected = payload["roiIndex"]
        if type(selected) is not int or not 0 <= selected < len(context.get("rois", [])):
            raise ValueError("Invalid selected ROI")
        context["roiIndex"] = selected
    return context


class _OverlayDrag(NativeTitlebarDrag):
    """Reuse the owned AppKit mouse monitor without shell double-click zoom."""

    def __init__(self, api, window, *, logger):
        self.api = api
        super().__init__(window, logger=logger)

    def _begin(self):
        try:
            import AppKit

            event, native = self._press, self.native
            self._clear()
            if (self.api._locked or self.api._closed or event is None or native is None
                    or event.window() != native or time.monotonic() - self._pressed_at > 1
                    or not (AppKit.NSEvent.pressedMouseButtons() & 1) or not native.isVisible()):
                return
            native.performWindowDragWithEvent_(event)
        except Exception as exc:
            self.logger(f"translucent window drag failed: {exc!r}")


class _DisplaySession:
    """Ephemeral per-launch display state and condition waiter."""

    def __init__(
        self,
        *,
        launch_token: str,
        launch_generation: str,
        base_query: str,
        query: str,
        cond: threading.Condition,
    ):
        self.launch_token = launch_token
        self.launch_generation = launch_generation
        self.base_query = base_query
        self.query = query
        self.revision = 0
        self.expired = False
        self.expired_reason = ""
        self.retired = False
        self.cond = cond


class TranslucentApi:
    """Display-only bridge with read-only, user-selected JPG/TXT ROI import."""

    def __init__(self, context, *, logger, on_closed, launch_token=None, launch_generation=""):
        self._context = copy.deepcopy(context)
        self._context.setdefault("opacity", OPACITY)
        self._context["opacity_mode"] = "native" if sys.platform == "win32" else "layer"
        self._logger = logger
        self._on_closed = on_closed
        self._window = None
        self._drag = None
        self._locked = False
        self._closed = False
        self._close_notified = False
        self._configured = False
        self._drop_document = None
        self._epoch = 0
        self._lock = threading.RLock()
        initial_query = self._context.get("query", "")
        if not isinstance(initial_query, str):
            initial_query = ""
        token = launch_token if isinstance(launch_token, str) and launch_token else uuid.uuid4().hex
        gen = launch_generation if isinstance(launch_generation, str) else ""
        self._session = _DisplaySession(
            launch_token=token,
            launch_generation=gen,
            base_query=initial_query,
            query=initial_query,
            cond=threading.Condition(self._lock),
        )

    def translucent_context(self):
        with self._lock:
            s = self._session
            return {
                **copy.deepcopy(self._context),
                "query": s.query,
                "launch_token": s.launch_token,
                "display_revision": s.revision,
                "locked": self._locked,
            }

    def translucent_display_state(self) -> dict:
        with self._lock:
            s = self._session
            if self._closed or s.retired:
                return {"ok": False, "closed": True}
            return {
                "ok": True,
                "launch_token": s.launch_token,
                "revision": s.revision,
                "query": s.query,
                "expired": s.expired,
                "reason": s.expired_reason,
            }

    def wait_translucent_display(
        self,
        launch_token: str,
        after_revision: int,
        timeout_ms: int = 25000,
    ) -> dict:
        if (
            not isinstance(launch_token, str)
            or type(after_revision) is not int
            or not 0 <= after_revision <= 2**53 - 1
            or isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, (int, float))
            or not math.isfinite(timeout_ms)
            or not 1 <= timeout_ms <= 60000
        ):
            return {"ok": False, "message": "Invalid display wait request"}
        deadline = time.monotonic() + float(timeout_ms) / 1000.0
        with self._lock:
            s = self._session
            if self._closed or s.retired or s.launch_token != launch_token:
                return {"ok": False, "closed": True}
            while not (
                self._closed
                or s.retired
                or self._session is not s
                or s.launch_token != launch_token
                or s.expired
                or s.revision > after_revision
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                s.cond.wait(timeout=remaining)
            if self._closed or s.retired or self._session is not s or s.launch_token != launch_token:
                return {"ok": False, "closed": True}
            if s.expired:
                return {
                    "ok": True,
                    "launch_token": s.launch_token,
                    "revision": s.revision,
                    "query": s.query,
                    "expired": True,
                    "reason": s.expired_reason,
                }
            if s.revision > after_revision:
                return {
                    "ok": True,
                    "launch_token": s.launch_token,
                    "revision": s.revision,
                    "query": s.query,
                    "expired": False,
                    "unchanged": False,
                }
            return {
                "ok": True,
                "launch_token": s.launch_token,
                "revision": s.revision,
                "query": s.query,
                "expired": False,
                "unchanged": True,
            }

    def _session_snapshot(self) -> tuple[str, str, str, bool]:
        with self._lock:
            s = self._session
            return (s.launch_token, s.launch_generation, s.base_query, self._closed or s.retired)

    def _apply_display_query(self, launch_token: str, revision: int, merged_query: str) -> dict:
        with self._lock:
            s = self._session
            if self._closed or s.retired:
                return {"ok": False, "closed": True}
            if s.launch_token != launch_token:
                return {"ok": False, "stale": True}
            if s.expired:
                return {
                    "ok": False,
                    "stale_generation": True,
                    "message": "The source image changed. Reopen the translucent viewer.",
                }
            if revision <= s.revision:
                return {"ok": True, "applied": False, "revision": s.revision}
            s.revision = revision
            s.query = merged_query
            self._context["query"] = merged_query
            s.cond.notify_all()
            return {"ok": True, "applied": True, "revision": revision}

    def _expire_display_session(self, launch_token: str, reason: str = "generation_changed") -> bool:
        with self._lock:
            s = self._session
            if not self._closed and not s.retired and s.launch_token == launch_token:
                s.expired = True
                s.expired_reason = reason
                s.cond.notify_all()
                return True
            return False

    def _notify_closed(self):
        with self._lock:
            if self._close_notified:
                return
            self._close_notified = True
            self._session.retired = True
            self._session.cond.notify_all()
        self._on_closed(self)

    def choose_roi_file(self):
        """Choose a coordinate-reference JPG and its AstroMapper TXT together."""
        if self._closed or self._locked:
            return {"ok": False, "message": "Unlock the translucent viewer to load ROIs."}
        import webview

        selected = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("AstroMapper JPG and ROI coordinates (*.jpg;*.jpeg;*.txt)",),
        )
        if not selected:
            return {"ok": False, "cancelled": True}
        try:
            paths = self._roi_pair_paths(selected, require_pair=False)
            if len(paths) == 1:
                if self._closed or self._locked:
                    return {"ok": False, "message": "Unlock the translucent viewer to load ROIs."}
                chosen = paths[0]
                image_needed = chosen.suffix.casefold() == ".txt"
                companion = self._window.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False,
                    directory=str(chosen.parent),
                    file_types=(("Matching coordinate reference JPG (*.jpg;*.jpeg)" if image_needed
                                 else "Matching AstroMapper ROI coordinates (*.txt)"),),
                )
                if not companion:
                    return {"ok": False, "cancelled": True}
                paths.extend(Path(path) for path in companion)
            return self._load_roi_files(paths)
        except (OSError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}

    @staticmethod
    def _roi_pair_paths(paths, *, require_pair=True):
        """Resolve one explicitly selected pair; never guess among siblings."""
        if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 2:
            raise ValueError("Choose one matching JPG and TXT pair.")
        paths = [Path(path) for path in paths]
        if any(path.suffix.casefold() not in (".txt", ".jpg", ".jpeg") for path in paths):
            raise ValueError("Choose a coordinate-reference JPG and AstroMapper TXT.")
        if len(paths) == 1:
            chosen = paths[0]
            suffixes = (".jpg", ".jpeg") if chosen.suffix.casefold() == ".txt" else (".txt",)
            companions = [path for path in chosen.parent.iterdir()
                          if path.stem.casefold() == chosen.stem.casefold()
                          and path.suffix.casefold() in suffixes and path.is_file()]
            if len(companions) == 1:
                paths.append(companions[0])
            elif require_pair:
                message = ("More than one matching reference file was found. Drop the intended JPG and TXT together."
                           if companions else "The matching JPG/TXT is missing. Drop both files together.")
                raise ValueError(message)
            else:
                return paths
        text = [path for path in paths if path.suffix.casefold() == ".txt"]
        images = [path for path in paths if path.suffix.casefold() in (".jpg", ".jpeg")]
        if len(text) != 1 or len(images) != 1:
            raise ValueError("Choose one JPG and one TXT file.")
        if text[0].stem.casefold() != images[0].stem.casefold():
            raise ValueError("The reference JPG and TXT must have the same filename before the extension.")
        return [text[0], images[0]]

    def _load_roi_files(self, paths):
        """Read the chosen pair for the shared AstroMapper browser importer."""
        if self._closed or self._locked:
            return {"ok": False, "message": "Unlock the translucent viewer to load ROIs."}
        try:
            text, image = self._roi_pair_paths(paths)
            return self._load_roi_file(text, image_path=image)
        except (OSError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}

    def _load_roi_file(self, path, *, image_path=None):
        if self._closed or self._locked:
            return {"ok": False, "message": "Unlock the translucent viewer to load ROIs."}
        try:
            from .roi_list import load_roi_file

            width, height = self._context["dimensions"]
            return load_roi_file(path, width=width, height=height, image_path=image_path)
        except (OSError, ValueError, UnicodeError) as exc:
            return {"ok": False, "message": str(exc)}

    def _wire_drop(self):
        from webview.dom import DOMEventHandler

        document = self._window.dom.document
        if self._drop_document is document:
            return
        self._drop_document = document

        def dropped(event):
            paths = [item.get("pywebviewFullPath") for item in
                     (event.get("dataTransfer", {}).get("files", []))]
            paths = [path for path in paths if isinstance(path, str)]
            result = self._load_roi_files(paths)
            self._window.evaluate_js("window.nd2TranslucentSetRois?.(" +
                                     json.dumps(result, allow_nan=False) + "); true")

        document.events.drop += DOMEventHandler(dropped, prevent_default=True)

    def _dispatch(self, operation):
        """Run only against this overlay, on its platform's UI thread."""
        def guarded():
            try:
                operation()
            except Exception as exc:
                self._logger(f"translucent window operation failed: {exc!r}")

        if sys.platform == "darwin":
            from Foundation import NSOperationQueue

            NSOperationQueue.mainQueue().addOperationWithBlock_(guarded)
        elif sys.platform == "win32":
            from System import Action

            native = self._window.native
            if native.InvokeRequired:
                native.BeginInvoke(Action(guarded))
            else:
                guarded()
        else:
            raise RuntimeError("The translucent viewer requires macOS or Windows")

    def _configure_and_show(self, *_args):
        def configure():
            with self._lock:
                if self._closed or self._configured:
                    return
                opacity = self._context["opacity"]
            native = self._window.native
            if sys.platform == "darwin":
                import AppKit

                frame = native.frame()
                native.setStyleMask_(AppKit.NSWindowStyleMaskBorderless)
                native.setFrame_display_(frame, True)
                native.setOpaque_(False)
                native.setBackgroundColor_(AppKit.NSColor.clearColor())
                native.setHasShadow_(False)
                native.setHidesOnDeactivate_(False)
                native.setMovable_(True)
                native.setMovableByWindowBackground_(False)
                native.setLevel_(AppKit.NSFloatingWindowLevel)
                native.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
                # transparent=True configures WKWebView itself before loading.
                native.makeKeyAndOrderFront_(None)
                if self._drag is not None:
                    self._drag._install()
            else:
                # WebView2's ordinary HWND host does not provide per-pixel
                # desktop alpha. Native form opacity supplies the tracing
                # effect (including controls) without applying alpha twice.
                native.Opacity = opacity
                native.TopMost = True
                native.ShowInTaskbar = False
                self._round_windows_region()
                native.Show()
            with self._lock:
                self._configured = True

        try:
            self._dispatch(configure)
        except Exception as exc:
            self._logger(f"translucent window setup failed: {exc!r}")
            self.close_translucent()

    def _round_windows_region(self):
        from System.Drawing import Region
        from System.Drawing.Drawing2D import GraphicsPath

        native = self._window.native
        radius = max(2, int(18 * win32_window_scale(native)))
        diameter = radius * 2
        width, height = native.ClientSize.Width, native.ClientSize.Height
        path = GraphicsPath()
        for x, y, angle in ((0, 0, 180), (width - diameter, 0, 270),
                            (width - diameter, height - diameter, 0), (0, height - diameter, 90)):
            path.AddArc(x, y, diameter, diameter, angle, 90)
        path.CloseFigure()
        previous = native.Region
        native.Region = Region(path)
        path.Dispose()
        if previous is not None:
            previous.Dispose()

    def set_translucent_opacity(self, value):
        """Change only this overlay; report the applied value for UI rollback."""
        def rejected(message):
            return {"ok": False, "opacity": self._context["opacity"], "message": message}

        try:
            value = _number(value, "Opacity", MIN_OPACITY, 1.0)
        except ValueError as exc:
            return rejected(str(exc))
        with self._lock:
            if self._closed or self._locked:
                return rejected("Unlock the translucent viewer to change opacity.")
            if sys.platform == "darwin":
                # The browser applies alpha to image pixels only. Controls keep
                # their original alpha and the native window remains clear.
                self._context["opacity"] = value
                return {"ok": True, "opacity": value}
            if sys.platform != "win32" or self._window is None:
                return rejected("Native opacity is unavailable.")
            epoch = self._epoch

        complete, cancelled = threading.Event(), threading.Event()
        outcome = {}

        def apply():
            try:
                with self._lock:
                    if cancelled.is_set() or self._closed or self._locked or epoch != self._epoch:
                        outcome.update(rejected("Opacity change was cancelled because the window state changed."))
                        return
                    native = self._window.native
                    native.Opacity = value
                    actual = _number(float(native.Opacity), "Native opacity", MIN_OPACITY, 1.0)
                    self._context["opacity"] = actual
                    outcome.update(ok=True, opacity=actual)
            except Exception as exc:
                outcome.update(rejected(f"Could not change opacity: {exc}"))
            finally:
                complete.set()

        try:
            self._dispatch(apply)
        except Exception as exc:
            return rejected(f"Could not change opacity: {exc}")
        if not complete.wait(OPACITY_TIMEOUT):
            cancelled.set()  # A delayed UI callback must not apply a stale value.
            return rejected("Native opacity change timed out.")
        return outcome

    def set_translucent_locked(self, locked):
        if type(locked) is not bool:
            return {"ok": False, "locked": self._locked}
        with self._lock:
            if self._closed:
                return {"ok": False, "locked": self._locked}
            self._locked = locked
            self._epoch += 1

        def apply():
            if not self._closed and sys.platform == "darwin":
                self._window.native.setMovable_(not self._locked)

        try:
            self._dispatch(apply)
        except Exception as exc:
            self._logger(f"translucent lock update failed: {exc!r}")
        return {"ok": True, "locked": locked}

    def start_window_drag(self):
        if self._closed or self._locked or self._drag is None:
            return False
        return self._drag.request()

    def _geometry(self, operation):
        with self._lock:
            if self._closed or self._locked:
                return False
            epoch = self._epoch

        def apply():
            # A lock/unlock cycle also discards moves queued before locking.
            with self._lock:
                if not self._closed and not self._locked and epoch == self._epoch:
                    operation(self._window.native)

        self._dispatch(apply)
        return True

    def move_window(self, dx, dy):
        try:
            dx, dy = (_number(value, "Window movement", -MAX_SIZE, MAX_SIZE) for value in (dx, dy))
        except ValueError:
            return False

        def move(native):
            if sys.platform == "darwin":
                import AppKit

                origin = native.frame().origin
                # Relative native coordinates avoid pywebview's startup-screen
                # offset bug when moving the overlay across multiple displays.
                native.setFrameOrigin_(AppKit.NSMakePoint(origin.x + dx, origin.y - dy))
            else:
                scale = win32_window_scale(native)
                native.Left += round(dx * scale)
                native.Top += round(dy * scale)

        return self._geometry(move)

    def resize_window(self, width, height):
        try:
            width, height = (int(round(_number(value, "Window size", 1, MAX_SIZE)))
                             for value in (width, height))
        except ValueError:
            return {"ok": False}
        width, height = max(MIN_SIZE[0], width), max(MIN_SIZE[1], height)

        def resize(native):
            if sys.platform == "darwin":
                frame = native.frame()
                frame.origin.y += frame.size.height - height
                frame.size.width, frame.size.height = width, height
                native.setFrame_display_(frame, True)
            else:
                scale = win32_window_scale(native)
                native.Width, native.Height = round(width * scale), round(height * scale)
                self._round_windows_region()

        return {"ok": self._geometry(resize), "width": width, "height": height}

    def close_translucent(self):
        # Closing/unlocking stays available even when locked.
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            self._epoch += 1
            self._session.retired = True
            self._session.cond.notify_all()
        try:
            if self._window is not None:
                self._window.destroy()
        finally:
            self._notify_closed()
        return True

    def _did_close(self, *_args):
        with self._lock:
            self._closed = True
            self._epoch += 1
            self._session.retired = True
            self._session.cond.notify_all()
        self._notify_closed()


def create_translucent_window(
    context,
    *,
    url,
    title,
    logger,
    on_closed,
    launch_token=None,
    launch_generation="",
):
    import webview

    api = TranslucentApi(
        context,
        logger=logger,
        on_closed=on_closed,
        launch_token=launch_token,
        launch_generation=launch_generation,
    )
    window = webview.create_window(
        title, url=url, js_api=api, width=640, height=480, min_size=MIN_SIZE,
        frameless=True, easy_drag=False, resizable=False, on_top=True,
        transparent=sys.platform == "darwin", shadow=False,
        hidden=True, background_color="#000000",
    )
    if window is None:
        raise RuntimeError("The overlay window could not be created")
    api._window = window
    if sys.platform == "darwin":
        api._drag = _OverlayDrag(api, window, logger=logger)
    window.events.loaded += api._configure_and_show
    window.events.loaded += api._wire_drop
    window.events.closed += api._did_close
    if getattr(window.events.closed, "is_set", lambda: False)():
        api._did_close()
    # Dynamic window creation can load before callbacks are registered.
    elif window.events.loaded.is_set():
        api._configure_and_show()
        api._wire_drop()
    return api
