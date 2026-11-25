"""Window-owned, display-only tracing overlays.

The overlay uses the owning viewer's gated tile server, but has its own native
top-level window. It never joins the parent's native child-window hierarchy,
which would make it disappear when the parent is minimized.

This module is the public facade for the translucent overlay subsystem. Pure
validation helpers live in ``nd2wsi.translucent_validation`` and stage-follow /
Win32 focus & click-through coordination live in
``nd2wsi.translucent_stage_follow``.
"""

from __future__ import annotations

import copy
import json
import math
import sys
import threading
import time
import uuid
from pathlib import Path

from . import follow_flips as follow_flips
from . import laser_target, session_log
from . import translucent_stage_follow as _stage_follow_mod
from .translucent_stage_follow import (
    GWL_EXSTYLE,
    HOVER_POLL_INTERVAL_S,
    HOVER_RECT_PADDING_CSS,
    HWND_TOPMOST,
    LOCKED_CLICKTHROUGH_EXSTYLE,
    LOCKED_MAX_OPACITY,
    SW_SHOWNOACTIVATE,
    SWP_FRAMECHANGED,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    SWP_SHOWWINDOW,
    WS_EX_APPWINDOW,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TRANSPARENT,
    StageFollowMixin,
    Win32OverlayFocusController,
    point_in_interactive_rects,
    win32_window_scale,
)
from .translucent_stage_follow import _hwnd_from_native as _hwnd_from_native
from .translucent_stage_follow import _win32_has_per_window_dpi as _win32_has_per_window_dpi
from .translucent_stage_follow import _win32_system_dpi_scale as _win32_system_dpi_scale
from .translucent_validation import _FLOAT32_SAFE_BOUND as _FLOAT32_SAFE_BOUND
from .translucent_validation import _HEX6_RE as _HEX6_RE
from .translucent_validation import _HEX_COLOR_RE as _HEX_COLOR_RE
from .translucent_validation import (
    CLIENT_LOG_FIELD_LIMIT,
    CLIENT_LOG_MAX,
    CLIENT_LOG_STACK_LIMIT,
    CLIENT_LOG_WINDOW,
    DEFAULT_STEP_UM,
    MARK_CONTEXT_KEYS,
    MAX_LOCKED_INTERACTIVE_RECTS,
    MAX_SIZE,
    MAX_STEP_UM,
    MIN_OPACITY,
    MIN_SIZE,
    OPACITY,
    OPACITY_TIMEOUT,
    TARGET_KEYS,
    VIEW_KEYS,
    _number,
    encode_validated_query,
    laser_scope,
    merge_display_query,
    normalize_laser_items,
    normalize_source_format,
    roi_pair_paths,
    validate_context,
    validate_display_params,
    validate_display_query,
    validate_follow_flips,
    validate_laser_item,
    validate_laser_target_request,
    validate_locked_interactive_rects,
    validate_overlay_view,
    validate_roi,
    validate_stage_follow_options,
)
from .translucent_validation import _optional_numbers as _optional_numbers
from .translucent_validation import _polygon_area as _polygon_area
from .window_chrome import NativeTitlebarDrag, _direct_destroy_window

_ORIG_WIN32_OVERLAY_API = _stage_follow_mod._win32_overlay_api
_win32_overlay_api = _ORIG_WIN32_OVERLAY_API


def _resolve_win32_overlay_api():
    """Allow tests to monkeypatch either translucent_window or translucent_stage_follow."""
    if _win32_overlay_api is not _ORIG_WIN32_OVERLAY_API:
        return _win32_overlay_api()
    return _stage_follow_mod._win32_overlay_api()


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
            if (
                self.api._locked
                or self.api._closed
                or event is None
                or native is None
                or event.window() != native
                or time.monotonic() - self._pressed_at > 1
                or not (AppKit.NSEvent.pressedMouseButtons() & 1)
                or not native.isVisible()
            ):
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


class TranslucentApi(StageFollowMixin):
    """Display-only bridge with read-only, user-selected JPG/TXT ROI import.

    ``flip_store`` (a ``follow_flips.FollowFlipStore``) remembers Follow's flips
    per image format; without one they last only as long as this overlay.
    """

    def __init__(
        self,
        context,
        *,
        logger,
        on_closed,
        flip_store=None,
        target_store=None,
        on_laser_box=None,
        is_active=None,
        on_lock=None,
        launch_token=None,
        launch_generation="",
    ):
        self._context = copy.deepcopy(context)
        self._context.setdefault("opacity", OPACITY)
        self._context["opacity_mode"] = "native" if sys.platform == "win32" else "layer"
        self._logger = logger
        self._on_closed = on_closed
        self._window = None
        self._page_url = None
        self._drag = None
        self._locked = False
        self._closed = False
        self._close_notified = False
        self._configured = False
        self._revealing = False
        self._drop_document = None
        self._epoch = 0
        self._context_epoch = 0
        self._lock = threading.RLock()
        self._refresh_lock = threading.RLock()
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
        self._stage_follow = None
        self._stage_follower = None
        self._stage_follow_source = None
        self._stage_follow_status = None
        self._client_log_window_start = None
        self._client_log_count = 0
        self._stage_follow_session = None
        self._stage_follow_receiver_id = None
        self._stage_follow_lost = ""
        self._slacs_snapshot = {"available": False, "session_id": None, "status": "waiting"}
        self._slacs_logged = None  # the last detection state written to the session log
        self._discovery = None
        self._source_format = normalize_source_format(self._context.get("source_format"))
        self._flip_store = flip_store
        self._flip_save_lock = threading.Lock()
        self._follow_flips = (False, False)  # (flip_x, flip_y); replaced whole, never mutated
        self._lock_reported = None
        self._last_locked_center = None
        self._realign_from = None
        self._overlay_view = None  # the page's last reported view; replaced whole, never mutated
        self._overlay_view_epoch = None
        self._on_lock = on_lock
        self._is_active = is_active or (lambda: True)
        self._laser_delivery = None
        self._on_laser_box = on_laser_box
        self._laser_items = normalize_laser_items(self._context.pop("laser_items", None))
        self._pending_laser_scope = None
        self._pending_laser_epoch = None
        self._pending_laser_context_epoch: int | None = None
        self._load_timer = None
        self._target_store = target_store
        self._target_save_lock = threading.Lock()
        self._laser_target = laser_target.validate(laser_target.DEFAULT)
        self._win32_focus = Win32OverlayFocusController(
            slacs_pid_provider=self._slacs_pid,
            logger=logger,
            api_factory=_resolve_win32_overlay_api,
        )
        if target_store is not None:
            self._laser_target, warning = target_store.load()
            if warning:
                session_log.event("laser_target_load_failed", message=warning)
        if flip_store is not None:
            self._restore_flips()

    def translucent_context(self):
        with self._lock:
            s = self._session
            flip_x, flip_y = self._follow_flips
            return {
                **copy.deepcopy(self._context),
                "query": s.query,
                "launch_token": s.launch_token,
                "display_revision": s.revision,
                "locked": self._locked,
                "source_format": self._source_format,
                "follow_flips": {"flip_x": flip_x, "flip_y": flip_y},
                "laser_target": copy.deepcopy(self._laser_target),
                "laser_items": copy.deepcopy(list(self._laser_items.values())),
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

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed or self._session.retired

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

    def choose_roi_file(self):
        """Choose a coordinate-reference JPG/PNG and its AstroMapper TXT together."""
        if self._closed or self._locked:
            return {"ok": False, "message": "Unlock the translucent viewer to load ROIs."}
        import webview

        selected = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("ROI list and image (*.txt;*.jpg;*.jpeg;*.png)",),
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
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    directory=str(chosen.parent),
                    file_types=(
                        (
                            "Reference image (*.jpg;*.jpeg;*.png)"
                            if image_needed
                            else "Matching AstroMapper ROI coordinates (*.txt)"
                        ),
                    ),
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
        return roi_pair_paths(paths, require_pair=require_pair)

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
            paths = [
                item.get("pywebviewFullPath")
                for item in (event.get("dataTransfer", {}).get("files", []))
            ]
            paths = [path for path in paths if isinstance(path, str)]
            result = self._load_roi_files(paths)
            self._window.evaluate_js(
                "window.nd2TranslucentSetRois?.("
                + json.dumps(result, allow_nan=False)
                + "); true"
            )

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
            native = self._window.native
            if getattr(native, "InvokeRequired", False):
                from System import Action

                native.BeginInvoke(Action(guarded))
            else:
                guarded()
        else:
            raise RuntimeError("The translucent viewer requires macOS or Windows")

    def _on_before_show(self, *_args):
        """Apply ``WS_EX_TOOLWINDOW`` before the WinForms BrowserForm is shown."""
        if self._closed or sys.platform != "win32":
            return
        window = self._window
        native = getattr(window, "native", None) if window is not None else None
        if native is None:
            return
        try:
            self._win32_focus.apply_toolwindow(native)
        except Exception as exc:
            self._logger(f"translucent before_show style setup failed: {exc!r}")

    def _show_windows_noactivate_if_needed(self, native, *, force_noactivate: bool = False) -> bool:
        """Show without stealing activation when locked or SLACS is foreground."""
        if not (force_noactivate or self._locked or self._win32_focus.is_slacs_foreground()):
            return False
        return self._win32_focus.show_noactivate(native)

    def _configure_and_show(self, *_args):
        if self._load_timer is not None:
            self._load_timer.cancel()

        def configure():
            with self._lock:
                if self._closed or self._configured or self._revealing:
                    return
                self._revealing = True
                opacity = self._context["opacity"]
            completed = False
            try:
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
                    self._win32_focus.apply_toolwindow(native)
                    if self._locked:
                        self._win32_focus.apply_locked(native, self._context["opacity"])
                    if not self._show_windows_noactivate_if_needed(native):
                        native.Show()
                completed = True
            finally:
                with self._lock:
                    if completed:
                        self._configured = True
                    self._revealing = False

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
        for x, y, angle in (
            (0, 0, 180),
            (width - diameter, 0, 270),
            (width - diameter, height - diameter, 0),
            (0, height - diameter, 90),
        ):
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
                        outcome.update(
                            rejected("Opacity change was cancelled because the window state changed.")
                        )
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
            epoch = self._epoch
        if sys.platform == "win32" and not locked:
            # Stop the hover timer immediately so no tick can race with unlock.
            self._win32_focus.stop_hover_timer()

        def apply():
            with self._lock:
                if self._closed or epoch != self._epoch:
                    return
                current_locked = self._locked
                current_opacity = self._context["opacity"]
            window = self._window
            native = getattr(window, "native", None) if window is not None else None
            if native is None:
                return
            if sys.platform == "darwin":
                native.setMovable_(not current_locked)
            elif sys.platform == "win32":
                if current_locked:
                    self._win32_focus.apply_locked(native, current_opacity)
                else:
                    self._win32_focus.apply_unlocked(native, current_opacity)

        try:
            self._dispatch(apply)
        except Exception as exc:
            self._logger(f"translucent lock update failed: {exc!r}")
        if self._on_lock is not None:
            self._on_lock(locked)
        if locked:
            self._yield_focus_to_slacs()
        result = {"ok": True, "locked": locked}
        if locked and self._stage_follow is not None:
            result["stage_follow"] = self.stage_follow_state()
        return result

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
            width, height = (
                int(round(_number(value, "Window size", 1, MAX_SIZE)))
                for value in (width, height)
            )
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

    def _refresh_context(
        self,
        context,
        *,
        launch_token=None,
        launch_generation="",
        _skip_stop: bool = False,
    ) -> str:
        """Reuse this process's visible tracing window with the source pane's view."""
        token = launch_token if isinstance(launch_token, str) and launch_token else uuid.uuid4().hex
        gen = launch_generation if isinstance(launch_generation, str) else ""
        with self._refresh_lock:
            with self._lock:
                if self._closed or not self._configured:
                    raise ValueError("The tracing window is still opening or closing.")
            if not _skip_stop:
                self.stop_stage_follow()
            was_locked = False
            with self._lock:
                if self._closed or not self._configured:
                    raise ValueError("The tracing window is still opening or closing.")
                scope_changed = self._laser_scope(context) != self._laser_scope(self._context)
                next_context = copy.deepcopy(context)
                incoming_laser = next_context.pop("laser_items", None)
                if incoming_laser is not None:
                    self._laser_items = normalize_laser_items(incoming_laser)
                elif scope_changed:
                    self._laser_items = {}
                if "source_format" in next_context:
                    new_format = normalize_source_format(next_context.get("source_format"))
                    format_changed = new_format != self._source_format
                    self._source_format = new_format
                else:
                    format_changed = False
                self._context = {
                    **next_context,
                    "opacity": self._context["opacity"],
                    "opacity_mode": self._context["opacity_mode"],
                }
                if format_changed:
                    self._follow_flips = (False, False)
                    if self._flip_store is not None:
                        self._restore_flips()
                initial_query = self._context.get("query", "")
                if not isinstance(initial_query, str):
                    initial_query = ""
                old_session = self._session
                old_session.retired = True
                old_session.cond.notify_all()
                self._session = _DisplaySession(
                    launch_token=token,
                    launch_generation=gen,
                    base_query=initial_query,
                    query=initial_query,
                    cond=threading.Condition(self._lock),
                )
                self._overlay_view = None
                self._overlay_view_epoch = None
                self._lock_reported = None
                self._last_locked_center = None
                self._realign_from = None
                if self._locked:
                    self._locked = False
                    was_locked = True
                self._epoch += 1
                self._context_epoch += 1
                epoch = self._epoch
                requested_opacity = self._context["opacity"]
            if was_locked:
                if sys.platform == "win32":
                    self._win32_focus.stop_hover_timer()

                def unlock():
                    with self._lock:
                        if self._closed or epoch != self._epoch:
                            return
                    native = getattr(self._window, "native", None)
                    if native is None:
                        return
                    if sys.platform == "darwin":
                        native.setMovable_(True)
                    elif sys.platform == "win32":
                        self._win32_focus.apply_unlocked(native, requested_opacity)

                try:
                    self._dispatch(unlock)
                except Exception:
                    pass
                if self._on_lock is not None:
                    self._on_lock(False)
            with self._lock:
                if self._closed or self._session.launch_token != token:
                    return token
            self._window.load_url(self._page_url)
            with self._lock:
                if self._closed or self._session.launch_token != token:
                    return token
            if sys.platform == "win32" and (
                was_locked or self._locked or self._win32_focus.is_slacs_foreground()
            ):
                native = getattr(self._window, "native", None)
                if self._win32_focus.can_control(native):
                    def show_noactivate():
                        with self._lock:
                            if self._closed or epoch != self._epoch or self._session.launch_token != token:
                                return
                        if not self._win32_focus.show_noactivate(native):
                            self._window.show()

                    try:
                        self._dispatch(show_noactivate)
                    except Exception:
                        with self._lock:
                            if self._closed or self._session.launch_token != token:
                                return token
                        if not self._win32_focus.show_noactivate(native):
                            self._window.show()
                    return token
            self._window.show()
            return token

    def _initial_load_timeout(self):
        with self._lock:
            if self._closed or self._window.events.loaded.is_set():
                return
        self._logger("Tracing page did not load; closing its hidden window")
        try:
            self.close_translucent()
        except Exception as exc:
            self._logger(f"Tracing load-timeout teardown: {exc!r}")
        finally:
            # This window was never shown. _closed prevents a late loaded event
            # from showing it after its failed-open claim has been released.
            self._did_close()

    def close_translucent(self):
        # Closing/unlocking stays available even when locked.
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            self._epoch += 1
            self._context_epoch += 1
            self._pending_laser_epoch = None
            self._pending_laser_context_epoch = None
            self._pending_laser_scope = None
            self._session.retired = True
            self._session.cond.notify_all()
            win = self._window
            configured = self._configured
            revealing = self._revealing
        self._win32_focus.close()
        if self._laser_delivery is not None:
            self._laser_delivery.close()
        self.stop_stage_follow()
        if self._discovery is not None:
            self._discovery.stop()
        shown_event = getattr(getattr(win, "events", None), "shown", None)
        unshown = (
            win is not None
            and callable(getattr(shown_event, "is_set", None))
            and not shown_event.is_set()
            and not configured
            and not revealing
        )
        if win is None:
            self._did_close()
        elif unshown:
            try:
                _direct_destroy_window(win)
            finally:
                self._did_close()
        else:
            # Native closed owns claim release; destroy may return before the event.
            win.destroy()
        return True

    def _did_close(self, *_args):
        with self._lock:
            if self._close_notified:
                return
            self._close_notified = True
            if self._load_timer is not None:
                self._load_timer.cancel()
            self._closed = True
            self._epoch += 1
            self._context_epoch += 1
            self._pending_laser_epoch = None
            self._pending_laser_context_epoch = None
            self._pending_laser_scope = None
            self._session.retired = True
            self._session.cond.notify_all()
        self._win32_focus.close()
        if self._laser_delivery is not None:
            self._laser_delivery.close()
        self.stop_stage_follow()
        if self._discovery is not None:
            self._discovery.stop()
        self._on_closed(self)


def create_translucent_window(
    context,
    *,
    url,
    title,
    logger,
    on_closed,
    flip_store=None,
    target_store=None,
    on_laser_box=None,
    is_active=None,
    on_lock=None,
    launch_token=None,
    launch_generation="",
):
    import webview

    api = TranslucentApi(
        context,
        logger=logger,
        on_closed=on_closed,
        flip_store=flip_store,
        target_store=target_store,
        on_laser_box=on_laser_box,
        is_active=is_active,
        on_lock=on_lock,
        launch_token=launch_token,
        launch_generation=launch_generation,
    )
    try:
        window = webview.create_window(
            title,
            url=url,
            js_api=api,
            width=640,
            height=480,
            min_size=MIN_SIZE,
            frameless=True,
            easy_drag=False,
            resizable=False,
            on_top=True,
            transparent=sys.platform == "darwin",
            shadow=False,
            hidden=True,
            background_color="#000000",
        )
        if window is None:
            raise RuntimeError("The overlay window could not be created")
    except Exception:
        api._did_close()
        raise
    api._page_url = url
    api._window = window
    if sys.platform == "darwin":
        api._drag = _OverlayDrag(api, window, logger=logger)
    before_show = getattr(getattr(window, "events", None), "before_show", None)
    if before_show is not None:
        before_show += api._on_before_show
    window.events.loaded += api._configure_and_show
    window.events.loaded += api._wire_drop
    window.events.closed += api._did_close
    if getattr(window.events.closed, "is_set", lambda: False)():
        api._did_close()
    else:
        api._load_timer = threading.Timer(30, api._initial_load_timeout)
        api._load_timer.daemon = True
        api._load_timer.start()
        # Dynamic window creation can load before callbacks are registered.
        if window.events.loaded.is_set():
            api._configure_and_show()
            api._wire_drop()
    return api


__all__ = [
    "CLIENT_LOG_FIELD_LIMIT",
    "CLIENT_LOG_MAX",
    "CLIENT_LOG_STACK_LIMIT",
    "CLIENT_LOG_WINDOW",
    "DEFAULT_STEP_UM",
    "GWL_EXSTYLE",
    "HOVER_POLL_INTERVAL_S",
    "HOVER_RECT_PADDING_CSS",
    "HWND_TOPMOST",
    "LOCKED_CLICKTHROUGH_EXSTYLE",
    "LOCKED_MAX_OPACITY",
    "MARK_CONTEXT_KEYS",
    "MAX_LOCKED_INTERACTIVE_RECTS",
    "MAX_SIZE",
    "MAX_STEP_UM",
    "MIN_OPACITY",
    "MIN_SIZE",
    "OPACITY",
    "OPACITY_TIMEOUT",
    "SWP_FRAMECHANGED",
    "SWP_NOACTIVATE",
    "SWP_NOMOVE",
    "SWP_NOSIZE",
    "SWP_NOZORDER",
    "SWP_SHOWWINDOW",
    "SW_SHOWNOACTIVATE",
    "TARGET_KEYS",
    "VIEW_KEYS",
    "WS_EX_APPWINDOW",
    "WS_EX_LAYERED",
    "WS_EX_NOACTIVATE",
    "WS_EX_TOOLWINDOW",
    "WS_EX_TRANSPARENT",
    "StageFollowMixin",
    "TranslucentApi",
    "Win32OverlayFocusController",
    "create_translucent_window",
    "encode_validated_query",
    "laser_scope",
    "merge_display_query",
    "point_in_interactive_rects",
    "roi_pair_paths",
    "validate_context",
    "validate_display_params",
    "validate_display_query",
    "validate_follow_flips",
    "validate_laser_item",
    "validate_laser_target_request",
    "validate_locked_interactive_rects",
    "validate_overlay_view",
    "validate_roi",
    "validate_stage_follow_options",
]
