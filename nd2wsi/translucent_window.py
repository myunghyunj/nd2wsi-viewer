"""Window-owned, display-only fluorescence tracing overlays.

The overlay uses the owning viewer's gated tile server, but has its own native
top-level window. It never joins the parent's native child-window hierarchy,
which would make it disappear when the parent is minimized.
"""

from __future__ import annotations

import copy
import json
import math
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from . import follow_flips, session_log
from .window_chrome import NativeTitlebarDrag

OPACITY = 0.55
MIN_OPACITY = 0.10
OPACITY_TIMEOUT = 5
MIN_SIZE = (220, 160)
MAX_SIZE = 8192
DEFAULT_STEP_UM = 10.0
MAX_STEP_UM = 10000.0
# Client-error forwarding is rate limited so a looping page cannot flood the log.
CLIENT_LOG_MAX = 20
CLIENT_LOG_WINDOW = 10.0
CLIENT_LOG_FIELD_LIMIT = 1000
CLIENT_LOG_STACK_LIMIT = 4000
# What the page may report of its current view (report_overlay_view).
VIEW_KEYS = frozenset(("version", "center_px", "zoom", "rotation", "flip", "locked", "follow_session",
                       "follow_seq", "follow_displacement_um", "container", "window"))
# The overlay's state in each slacs_key record (_mark_context), in record order.
MARK_CONTEXT_KEYS = ("slacs_session", "image_name", "source_format", "image_px", "view", "view_age_ms",
                     "view_follow_seq", "view_displacement_um", "locked", "follow_status", "follow_seq",
                     "displacement_um", "counted", "step_source", "step_um", "roughness_um", "mirror_h",
                     "mirror_v", "reading_age_ms", "slacs_busy", "flip_x", "flip_y")


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range")
    return float(value)


def _optional_numbers(value, length, name, minimum=-math.inf, maximum=math.inf):
    """None, or ``length`` finite numbers within the bounds, as floats."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"Invalid {name}")
    return [_number(item, name, minimum, maximum) for item in value]


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
    # The overlay shows exactly the main viewer's rotation and flip; it has no
    # orientation settings of its own. An older handoff's "mirror_scan" is
    # ignored. Follow's flips reverse its movement only, never the orientation.
    query = payload.get("query", "")
    if not isinstance(query, str) or len(query) > 16384:
        raise ValueError("Invalid display settings")
    pairs = urllib.parse.parse_qsl(query.removeprefix("?"), keep_blank_values=True,
                                  strict_parsing=True, max_num_fields=6)
    params = dict(pairs)
    if len(params) != len(pairs) or set(params) - {"c", "win", "g", "t", "p", "z"}:
        raise ValueError("Unsupported display setting")
    channels = store.attrs["omero"]["channels"]
    selected = params.get("c")
    if selected not in (None, "", "none"):
        indices = selected.split(",")
        if (any(not index.isascii() or not index.isdecimal() or not 0 <= int(index) < len(channels)
                for index in indices) or len(set(indices)) != len(indices)):
            raise ValueError("Invalid channel selection")
    if "win" in params:
        windows = params["win"].split(",")
        if len(windows) > len(channels):
            raise ValueError("Too many channel windows")
        for window in windows:
            if not window:
                continue
            values = window.split(":")
            if len(values) not in (2, 3):
                raise ValueError("Invalid channel window")
            values = [float(value) for value in values]
            if not all(math.isfinite(value) for value in values) or values[1] <= values[0]:
                raise ValueError("Invalid channel window")
            if len(values) == 3 and not 0.1 <= values[2] <= 10:
                raise ValueError("Invalid channel gamma")
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
    cleaned = urllib.parse.urlencode(pairs)
    context = {"version": 1, "slide_url": slide_url, "query": "?" + cleaned if cleaned else "",
               "center": center, "zoom": zoom, "rotation": rotation, "flip": flip,
               "dimensions": [width, height],
               "pixel_size_um": copy.deepcopy(meta.get("pixel_size_um")),
               "image_name": Path(str(getattr(store, "source_path", "") or "image")).name,
               # Follow's flips are remembered per format of the original file.
               "source_format": follow_flips.source_format(store.attrs, getattr(store, "source_path", None)),
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
        import AppKit

        event, native = self._press, self.native
        self._clear()
        if (self.api._locked or self.api._closed or event is None or native is None
                or event.window() != native or time.monotonic() - self._pressed_at > 1
                or not (AppKit.NSEvent.pressedMouseButtons() & 1) or not native.isVisible()):
            return
        native.performWindowDragWithEvent_(event)


class TranslucentApi:
    """Display-only bridge with read-only, user-selected JPG/TXT ROI import.

    ``flip_store`` (a ``follow_flips.FollowFlipStore``) remembers Follow's flips
    per image format; without one they last only as long as this overlay.
    """

    def __init__(self, context, *, logger, on_closed, flip_store=None):
        self._context = copy.deepcopy(context)
        self._context.setdefault("opacity", OPACITY)
        self._context["opacity_mode"] = "native" if sys.platform == "win32" else "layer"
        self._logger = logger
        self._on_closed = on_closed
        self._window = None
        self._drag = None
        self._locked = False
        self._closed = False
        self._configured = False
        self._drop_document = None
        self._epoch = 0
        self._lock = threading.RLock()
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
        source_format = self._context.get("source_format")
        self._source_format = source_format if source_format in follow_flips.FORMATS else "other"
        self._flip_store = flip_store
        self._flip_save_lock = threading.Lock()
        self._follow_flips = (False, False)  # (flip_x, flip_y); replaced whole, never mutated
        self._overlay_view = None  # the page's last reported view; replaced whole, never mutated
        if flip_store is not None:
            self._restore_flips()

    def _orientation(self):
        """The main viewer's rotation and flip at handoff, as Follow records name them."""
        return {"view_rotation": self._context.get("rotation", 0), "view_flip": self._context.get("flip", False)}

    def _restore_flips(self):
        """Start with the flips remembered for this image's format; never raises."""
        try:
            stored, warning = self._flip_store.load(self._source_format)
        except Exception as exc:  # noqa: BLE001 - a broken store only loses the memory
            stored, warning = None, f"Saved Follow flips were not restored: {exc}"
        if warning:
            session_log.event("follow_flip_load_failed", source_format=self._source_format, message=warning)
        if stored is None:
            return
        self._follow_flips = stored
        session_log.event("follow_flip", source="remembered", source_format=self._source_format,
                          flip_x=stored[0], flip_y=stored[1], image_name=self._context.get("image_name"),
                          **self._orientation())

    def _ensure_slacs_discovery(self):
        from .slacs_discovery import SlacsDiscovery

        with self._lock:
            if not self._closed and self._discovery is None:
                self._discovery = SlacsDiscovery(on_change=self._on_slacs_change, name="translucent")
                self._slacs_snapshot = self._discovery.snapshot()
                logged = self._detection_to_log()
            else:
                logged = None
            discovery = self._discovery
        self._log_detection(logged)
        return discovery

    def _detection_to_log(self):
        """The detection state to record, once per change; call with the lock held."""
        current = self._slacs_snapshot
        state = (current.get("available") is True, current.get("session_id"), current.get("status"))
        if state == self._slacs_logged:
            return None
        self._slacs_logged = state
        return state

    @staticmethod
    def _log_detection(state):
        # So a detection that fails on the instrument PC can be diagnosed afterwards.
        if state is not None:
            session_log.event("slacs_detection", window="translucent", available=state[0],
                              session_id=state[1], status=state[2])

    def _on_slacs_change(self, snapshot):
        """Stop an active receiver as soon as its detected process is lost or replaced.

        Every stage_follow_state reply computed after this point reports the loss
        and carries no movement. A reply computed just before it may still be on its
        way to the page, so the page is also told at once; it drops any follow reply
        that arrives after this revocation (translucent-v1.js revokeStageFollow).
        """
        receiver = follower = source = revoked = message = None
        with self._lock:
            if self._closed:
                return
            available = (snapshot.get("available") is True and snapshot.get("status") == "running"
                         and isinstance(snapshot.get("session_id"), str) and bool(snapshot["session_id"]))
            session = snapshot.get("session_id") if available else None
            self._slacs_snapshot = {"available": available, "session_id": session,
                                    "status": snapshot.get("status", "unavailable")}
            logged = self._detection_to_log()
            # Every Follow is bound to the SLACS process detected when it started;
            # that process's loss or replacement ends it.
            if (self._stage_follow is not None and self._stage_follow_session is not None
                    and session != self._stage_follow_session):
                receiver, self._stage_follow = self._stage_follow, None
                follower, self._stage_follower = self._stage_follower, None
                source, self._stage_follow_source = self._stage_follow_source, None
                self._stage_follow_status = None
                revoked, self._stage_follow_receiver_id = self._stage_follow_receiver_id, None
                self._stage_follow_session = None
                message = self._stage_follow_lost = "SLACS program stopped or changed. Follow stopped; reconnect manually."
        self._log_detection(logged)
        if receiver is not None:
            self._revoke_follow_in_page(revoked, message)
            if follower is not None:
                follower.stop("slacs_lost")
            receiver.close()
            session_log.event("follow_stop", source=source, reason="slacs_lost")

    def _revoke_follow_in_page(self, receiver_id, message):
        """Queue the revocation on the same script channel that carries bridge replies.

        pywebview delivers every bridge reply through ``evaluate_js``, so replies queued
        after this revocation reach the page after it. A separate thread keeps the
        discovery callback quick and never waits on the UI thread.
        """
        window = self._window
        if window is None:
            return
        script = ("window.nd2TranslucentRevokeFollow?.(" +
                  json.dumps({"session_id": receiver_id, "message": message}, allow_nan=False) + "); true")
        try:
            threading.Thread(target=self._push_revocation, args=(window, script),
                             name="nd2wsi-stage-follow-revoke", daemon=True).start()
        except RuntimeError as exc:  # the receiver must still be closed by the caller
            self._logger(f"stage follow revocation could not start: {exc!r}")

    def _push_revocation(self, window, script):
        if self._closed:
            return
        try:
            window.evaluate_js(script)
        except Exception as exc:
            self._logger(f"stage follow revocation could not reach the page: {exc!r}")

    def slacs_discovery_state(self):
        """Report program detection; this does not establish a motion connection."""
        self._ensure_slacs_discovery()
        with self._lock:
            return dict(self._slacs_snapshot)

    def translucent_context(self):
        with self._lock:
            flip_x, flip_y = self._follow_flips
            return {**copy.deepcopy(self._context), "locked": self._locked, "source_format": self._source_format,
                    "follow_flips": {"flip_x": flip_x, "flip_y": flip_y}}

    def _stage_follow_options(self, options):
        """Resolve the stage-follow source, per-key step and step source; never raise.

        The step source is "slacs" (each press moves by SLACS's Roughness, with
        ``step_um`` as the fallback while it cannot be read) or "manual" (always
        ``step_um``). Roughness is read only on Windows, so by default only the
        Windows key source reads it.
        """
        default_source = "keys" if sys.platform == "win32" else "adapter"

        def default_step_source(source):
            return "slacs" if sys.platform == "win32" and source == "keys" else "manual"

        source, step_um = default_source, DEFAULT_STEP_UM
        if options is None:
            return source, step_um, default_step_source(source), None
        if not isinstance(options, dict):
            return source, step_um, default_step_source(source), "Invalid stage follow options."
        requested = options.get("source", default_source)
        if requested not in ("keys", "adapter"):
            return source, step_um, default_step_source(source), "Unknown stage follow source."
        source = requested
        step_source = options.get("step_source", default_step_source(source))
        if step_source not in ("slacs", "manual"):
            return source, step_um, default_step_source(source), "Unknown stage follow step source."
        if "step_um" in options:
            value = options["step_um"]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value <= MAX_STEP_UM):
                return (source, step_um, step_source,
                        "Step per key press must be a number between 0.001 and 10000 µm.")
            step_um = float(value)
        return source, step_um, step_source, None

    def _slacs_pid(self):
        """The detected SLACS process ID for read-only control reads, or None; never raises.

        Runs on the controls monitor's thread, so it takes no overlay lock. The
        detector reports a pid only for the session it currently reports, and a
        lost or replaced session stops Follow (_on_slacs_change).
        """
        discovery = self._discovery
        if self._closed or discovery is None:
            return None
        try:
            pid = discovery.slacs_pid()
        except Exception:  # noqa: BLE001 - an unknown pid only skips a read
            return None
        return pid if type(pid) is int and pid > 0 else None

    def _slacs_controls_monitor(self):
        """A read-only monitor of SLACS's Roughness and Mirror controls, or None and why not.

        Follow never depends on it: without one the key follower moves by the
        manual step, recorded as a fallback.
        """
        try:
            from .slacs_controls import SlacsControlsMonitor

            return SlacsControlsMonitor(self._slacs_pid), None
        except Exception as exc:  # noqa: BLE001 - a missing reader must not stop Follow
            self._logger(f"SLACS controls monitor unavailable: {exc!r}")
            return None, f"{type(exc).__name__}: {exc}"[:CLIENT_LOG_FIELD_LIMIT]

    def start_stage_follow(self, options=None):
        """Opt in to this overlay's receiver; never connect to a controller.

        ``options`` may set ``source`` ("keys" to observe SLACS W/A/S/D presses,
        "adapter" for an external in-process/HTTP sender), the per-key
        ``step_um`` and ``step_source`` ("slacs" to move each press by SLACS's
        Roughness, read from its window, or "manual"). The key follower is a
        strict observer and never blocks, delays, or alters a keystroke; it owns
        the Roughness monitor's start and stop. It starts with this overlay's
        Follow flips and logs SLACS's "," and "." presses with ``_mark_context``.
        Follow is refused unless SLACS is detected, and is bound to that
        detected process.
        """
        from .stage_follow import StageFollowReceiver
        from .stage_keys import SlacsKeyFollower

        source, step_um, step_source, option_error = self._stage_follow_options(options)
        with self._lock:
            if self._closed:
                return {"ok": False, "message": "This translucent viewer is closed."}
            if option_error is not None:
                return {"ok": False, "message": option_error}
            detected = self._slacs_snapshot
            if not (detected.get("available") is True and isinstance(detected.get("session_id"), str)
                    and detected["session_id"]):
                return {"ok": False, "message": "SLACS is not detected. Start SLACS, then turn on Follow."}
            pixels = self._context.get("pixel_size_um")
            if (not isinstance(pixels, (list, tuple)) or len(pixels) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (float, int))
                           or not math.isfinite(v) or v <= 0 for v in pixels)):
                return {"ok": False, "message": "Stage follow requires a calibrated image."}
            if self._stage_follow is not None:
                descriptor = self._stage_follow.start()
                return {"ok": True, "source": self._stage_follow_source, **descriptor}
            # Not re-read from the store: a toggle just before Follow may still be saving.
            flip_x, flip_y = self._follow_flips
            receiver = StageFollowReceiver()
            try:
                descriptor = receiver.start()
            except (OSError, RuntimeError) as exc:
                receiver.close()
                return {"ok": False, "message": "Could not start the local motion receiver: " + str(exc)}
            follower = controls_error = None
            if source == "keys":
                controls = None
                if step_source == "slacs":
                    controls, controls_error = self._slacs_controls_monitor()
                try:
                    # The context's calibration is [Y, X]; the follower takes (X, Y).
                    follower = SlacsKeyFollower(receiver, descriptor["session_id"], step_um=step_um,
                                                step_source=step_source, controls=controls,
                                                pixel_size_um=(float(pixels[1]), float(pixels[0])),
                                                flip_x=flip_x, flip_y=flip_y, mark_context=self._mark_context)
                    snapshot = follower.start()
                except (ValueError, OSError, RuntimeError) as exc:
                    snapshot = {"running": False, "error": str(exc)}
                if not snapshot.get("running"):
                    if follower is not None:
                        follower.stop("start_failed")
                    receiver.close()
                    message = snapshot.get("error") or "Could not start key following."
                    session_log.event("follow_start", source=source, step_source=step_source, ok=False,
                                      message=message)
                    return {"ok": False, "message": message}
            self._stage_follow = receiver
            self._stage_follower = follower
            self._stage_follow_source = source
            self._stage_follow_status = None
            # Bound to the detected SLACS process; see _on_slacs_change.
            bound = self._stage_follow_session = detected["session_id"]
            self._stage_follow_receiver_id = descriptor.get("session_id")
            self._stage_follow_lost = ""
        extra = {"controls_error": controls_error} if controls_error else {}
        session_log.event("follow_start", source=source, step_source=step_source, step_um=step_um, ok=True,
                          slacs_session=bound, source_format=self._source_format, flip_x=flip_x, flip_y=flip_y,
                          **self._orientation(), **extra)
        return {"ok": True, "source": source, **descriptor}

    def set_stage_follow_flips(self, payload):
        """Reverse Follow's on-screen A/D (flip_x) or W/S (flip_y) move; never the image.

        Applies from the next key press, also while Follow runs or the overlay
        is locked: the page applies only differences of the cumulative
        displacement, so its sequence and displacement stay consistent. The new
        pair is remembered for this image's format when the window has a store.
        """
        def current(ok, **extra):
            flip_x, flip_y = self._follow_flips
            return {"ok": ok, "flip_x": flip_x, "flip_y": flip_y, "source_format": self._source_format, **extra}

        if (not isinstance(payload, dict) or set(payload) != {"flip_x", "flip_y"}
                or type(payload["flip_x"]) is not bool or type(payload["flip_y"]) is not bool):
            return current(False, message="Follow flips must be two true/false values.")
        flips = (payload["flip_x"], payload["flip_y"])
        with self._lock:
            if self._closed:
                return current(False, message="This translucent viewer is closed.")
            previous = self._follow_flips
            if flips == previous:
                return current(True, changed=False)
            self._follow_flips = flips
            follower, receiver = self._stage_follower, self._stage_follow
            # Lock order: overlay, then follower, as in stage_follow_state.
            if follower is not None:
                follower.set_flips(*flips)
            # Only the receiver's progress at the toggle: a press already on its way
            # may still arrive later with the old flips. Each follow_key names the
            # flips that press used.
            follow_seq = receiver.snapshot().get("seq") if receiver is not None else None
        saved, message = self._remember_flips()
        session_log.event("follow_flip", source="user", source_format=self._source_format, flip_x=flips[0],
                          flip_y=flips[1], previous=list(previous), following=follower is not None,
                          follow_seq=follow_seq, saved=saved, image_name=self._context.get("image_name"),
                          **self._orientation())
        result = {"ok": True, "changed": True, "flip_x": flips[0], "flip_y": flips[1],
                  "source_format": self._source_format, "saved": saved}
        if message:
            result["message"] = message
        return result

    def _remember_flips(self):
        """Save the current flips for this format: (saved, a message for the page or None).

        Saves the pair current when the save lock is taken, so the last toggle
        always wins on disk.
        """
        store = self._flip_store
        if store is None:
            return False, None
        with self._flip_save_lock:
            flip_x, flip_y = self._follow_flips
            try:
                store.save(self._source_format, flip_x, flip_y)
            except (OSError, ValueError) as exc:
                message = f"Applied for this window, but could not be remembered: {exc}"
                session_log.event("follow_flip_save_failed", source_format=self._source_format,
                                  flip_x=flip_x, flip_y=flip_y, message=message)
                return False, message
        return True, None

    def report_overlay_view(self, payload):
        """Keep the page's current view for SLACS laser-shot records; never logged.

        A refused report forgets the kept view, so a later record shows no
        image centre rather than an old one as if it were current.
        """
        try:
            view = self._validated_view(payload)
        except (TypeError, ValueError) as exc:
            with self._lock:
                self._overlay_view = None
            return {"ok": False, "message": str(exc)}
        with self._lock:
            if self._closed:
                return {"ok": False}
            self._overlay_view = {**view, "at": time.monotonic()}
        return {"ok": True}

    def _validated_view(self, payload):
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("Unsupported overlay view")
        if set(payload) - VIEW_KEYS:
            raise ValueError("Unknown overlay view field")
        center = payload.get("center_px")
        if not isinstance(center, list) or len(center) != 2:
            raise ValueError("Invalid view centre")
        flip, locked = payload.get("flip", False), payload.get("locked", False)
        if type(flip) is not bool or type(locked) is not bool:
            raise ValueError("View flip and lock must be boolean")
        session = payload.get("follow_session")
        if session is not None and (not isinstance(session, str) or len(session) > 128):
            raise ValueError("Invalid Follow session")
        seq = payload.get("follow_seq")
        if seq is not None and (type(seq) is not int or not 0 <= seq <= 2**53 - 1):
            raise ValueError("Invalid Follow sequence number")
        # The centre is only logged, never used to place a camera, so it is bounded
        # generously: the page can pan the image far outside validate_context's slack.
        return {"center_px": [_number(value, "View centre", -1e9, 1e9) for value in center],
                "zoom": _number(payload.get("zoom"), "View scale", 1e-12, 1e6),
                "rotation": _number(payload.get("rotation", 0), "View rotation", -360000, 360000) % 360,
                "flip": flip, "locked": locked, "follow_session": session, "follow_seq": seq,
                "follow_displacement_um": _optional_numbers(payload.get("follow_displacement_um"), 2,
                                                            "Follow displacement"),
                "container": _optional_numbers(payload.get("container"), 2, "view size", 0),
                "window": _optional_numbers(payload.get("window"), 4, "window frame", -1e6, 1e6)}

    def _mark_context(self):
        """This overlay's state for a SLACS "," or "." record, or None once closed; never raises.

        Runs on the Raw Input thread inside the key follower. Like ``_slacs_pid``
        it never takes the overlay lock, because ``start_stage_follow`` holds it
        across the follower's start and stop. References are read one by one and
        may straddle a concurrent start or stop, which is acceptable for a log
        record; the receiver and follower snapshots take only their own locks.
        """
        try:
            if self._closed:
                return None
            receiver, follower = self._stage_follow, self._stage_follower
            receiver_id, view = self._stage_follow_receiver_id, self._overlay_view
            flip_x, flip_y = self._follow_flips
            context = dict.fromkeys(MARK_CONTEXT_KEYS)
            context.update(slacs_session=self._stage_follow_session, image_name=self._context.get("image_name"),
                           source_format=self._source_format, locked=self._locked, flip_x=flip_x, flip_y=flip_y)
            if view is not None:
                center_x, center_y = view["center_px"]
                context.update(image_px=[round(center_x, 1) + 0.0, round(center_y, 1) + 0.0],
                               view={key: view[key] for key in ("zoom", "rotation", "flip", "container", "window")},
                               view_age_ms=int(round((time.monotonic() - view["at"]) * 1000)))
                if receiver_id is not None and view["follow_session"] == receiver_id:
                    context.update(view_follow_seq=view["follow_seq"],
                                   view_displacement_um=view["follow_displacement_um"])
            if receiver is not None:
                snapshot = receiver.snapshot()
                context.update(follow_status=snapshot.get("status"), follow_seq=snapshot.get("seq"),
                               displacement_um=snapshot.get("displacement_um"))
            else:
                context["follow_status"] = "lost" if self._stage_follow_lost else "off"
            if follower is not None:
                keys = follower.snapshot()
                context.update({key: keys.get(key) for key in (
                    "counted", "step_source", "step_um", "roughness_um", "mirror_h", "mirror_v",
                    "reading_age_ms", "slacs_busy")})
            return context
        except Exception as exc:  # noqa: BLE001 - a log record must never break the key observer
            return {"context_error": type(exc).__name__}

    def stage_follow_state(self):
        with self._lock:
            receiver = self._stage_follow
            follower = self._stage_follower
            source = self._stage_follow_source
            if self._closed or receiver is None:
                if not self._closed and self._stage_follow_lost:
                    return {"enabled": False, "status": "paused", "message": self._stage_follow_lost}
                return {"enabled": False, "status": "off", "message": "Stage follow is off."}
            snapshot = receiver.snapshot()
            if follower is not None:
                snapshot = {**snapshot, "keys": follower.snapshot()}
            status = snapshot.get("status")
            changed = status != self._stage_follow_status
            self._stage_follow_status = status
        if changed:
            session_log.event("follow_state", source=source, status=status)
        return snapshot

    def stop_stage_follow(self):
        with self._lock:
            receiver, self._stage_follow = self._stage_follow, None
            follower, self._stage_follower = self._stage_follower, None
            source, self._stage_follow_source = self._stage_follow_source, None
            self._stage_follow_status = None
            self._stage_follow_session = None
            self._stage_follow_receiver_id = None
            self._stage_follow_lost = ""
        if follower is not None:
            follower.stop("stopped")
        if receiver is not None:
            receiver.close()
            session_log.event("follow_stop", source=source)
        return {"ok": True}

    def log_client_event(self, payload):
        """Record a rate-limited overlay-page diagnostic in the session log."""
        with self._lock:
            if self._closed:
                return {"ok": False}
            now = time.monotonic()
            if (self._client_log_window_start is None
                    or now - self._client_log_window_start > CLIENT_LOG_WINDOW):
                self._client_log_window_start = now
                self._client_log_count = 0
            if self._client_log_count >= CLIENT_LOG_MAX:
                return {"ok": True, "rate_limited": True}
            self._client_log_count += 1
        if not isinstance(payload, dict):
            return {"ok": False}

        def capped(key, limit):
            value = payload.get(key)
            if value is None:
                return None
            return str(value)[:limit]

        # "client_kind" rather than "kind": session_log.event's first positional
        # parameter is itself named "kind", so a "kind" field would collide.
        session_log.event(
            "client_error",
            source=capped("source", CLIENT_LOG_FIELD_LIMIT),
            client_kind=capped("kind", CLIENT_LOG_FIELD_LIMIT),
            message=capped("message", CLIENT_LOG_FIELD_LIMIT),
            where=capped("where", CLIENT_LOG_FIELD_LIMIT),
            stack=capped("stack", CLIENT_LOG_STACK_LIMIT),
        )
        return {"ok": True}

    def save_stage_follow_connection(self):
        """Export only an explicitly chosen local bridge connection file."""
        import webview

        with self._lock:
            receiver = self._stage_follow
            if self._closed or receiver is None:
                return {"ok": False, "message": "Enable stage follow first."}
            descriptor = {**receiver.start(), "version": 1,
                          "image_name": self._context.get("image_name", "image"),
                          "dimensions": self._context.get("dimensions"),
                          "pixel_size_um_yx": self._context.get("pixel_size_um"),
                          "motion_source": "commanded", "units": "um"}
        selected = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename="stage-follow-connection.json",
            file_types=("Stage follow connection (*.json)",),
        )
        if not selected:
            return {"ok": True, "cancelled": True}
        target = Path(selected if isinstance(selected, str) else selected[0])
        temporary = None
        try:
            with self._lock:
                if self._closed or self._stage_follow is not receiver:
                    return {"ok": False, "message": "The motion connection changed. Save it again."}
                fd, temporary = tempfile.mkstemp(prefix=".stage-follow-", dir=target.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(descriptor, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
                os.replace(temporary, target)
            return {"ok": True}
        except (OSError, ValueError) as exc:
            return {"ok": False, "message": "Could not save the motion connection: " + str(exc)}
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

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
                    file_types=(("Matching coordinate-reference JPG (*.jpg;*.jpeg)" if image_needed
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
            if self._closed or self._configured:
                return
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
                native.Opacity = self._context["opacity"]
                native.TopMost = True
                native.ShowInTaskbar = False
                self._round_windows_region()
                native.Show()
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
        radius = max(2, int(18 * getattr(native, "_scale", 1)))
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

        self._dispatch(apply)
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
                scale = getattr(native, "_scale", 1)
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
                scale = getattr(native, "_scale", 1)
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
        self.stop_stage_follow()
        if self._discovery is not None:
            self._discovery.stop()
        if self._window is not None:
            self._window.destroy()
        self._on_closed(self)
        return True

    def _did_close(self, *_args):
        with self._lock:
            self._closed = True
            self._epoch += 1
        self.stop_stage_follow()
        if self._discovery is not None:
            self._discovery.stop()
        self._on_closed(self)


def create_translucent_window(context, *, url, title, logger, on_closed, flip_store=None):
    import webview

    api = TranslucentApi(context, logger=logger, on_closed=on_closed, flip_store=flip_store)
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
    # Dynamic window creation can load before callbacks are registered.
    if window.events.loaded.is_set():
        api._configure_and_show()
        api._wire_drop()
    return api
