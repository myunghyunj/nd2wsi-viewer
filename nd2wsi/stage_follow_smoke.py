"""Stage-follow loopback smoke checks and coordinate fixtures for translucent overlays.

Extracted from :mod:`nd2wsi.translucent_smoke`, which re-exports all public and
test-referenced symbols as a flat facade. All input is sent to the supplied
overlay and its private loopback receiver; no operating-system input or
instrument commands are sent.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# The overlay page's own Follow state and its visible Follow control.
SLACS_DISCOVERY_STATE = "window.nd2Translucent.state.slacs"
STAGE_FOLLOW_STATE = """(() => {
  const view=window.nd2Translucent,follow=view.state.follow,button=document.getElementById('stage-follow');
  return {enabled:follow.enabled,pending:follow.pending,status:follow.status,seq:follow.seq,
    session:follow.sessionId,message:follow.message,locked:view.state.locked,
    disabled:button.disabled,pressed:button.getAttribute('aria-pressed'),title:button.title,
    visible:(()=>{const r=button.getBoundingClientRect?button.getBoundingClientRect():null,
      css=window.getComputedStyle?getComputedStyle(button):null;
      return !!r&&r.width>0&&r.height>0&&!(button.closest&&button.closest('[hidden]'))&&
        !(css&&(css.visibility==='hidden'||css.display==='none'));})(),
    hit:(()=>{const r=button.getBoundingClientRect?button.getBoundingClientRect():null;
      const top=r&&document.elementFromPoint?document.elementFromPoint(r.left+r.width/2,r.top+r.height/2):null;
      return !!(top&&top.closest&&top.closest('#stage-follow')===button);})()};
})()"""
STAGE_FOLLOW_ROTATE = ("nd2Translucent.viewer.viewport.setRotation(__DEGREES__,true); "
                       "nd2Translucent.viewer.forceRedraw(); true")
STAGE_FOLLOW_ROTATION_DEGREES = 37
STAGE_FOLLOW_CLICK = "document.getElementById('stage-follow').click(); true"
ROI_STATE = "JSON.stringify({rois:nd2Translucent.state.rois,index:nd2Translucent.state.roiIndex})"
# The commanded screen displacement (right, down), as source pixels of an
# unrotated, unflipped view: a few pixels, different per axis so a swapped or
# mirrored axis cannot pass.
STAGE_FOLLOW_PIXELS = (5.0, 3.0)
STAGE_FOLLOW_TOLERANCE_PX = 1e-3

__all__ = [
    "ROI_STATE",
    "SLACS_DISCOVERY_STATE",
    "STAGE_FOLLOW_CLICK",
    "STAGE_FOLLOW_PIXELS",
    "STAGE_FOLLOW_ROTATE",
    "STAGE_FOLLOW_ROTATION_DEGREES",
    "STAGE_FOLLOW_STATE",
    "STAGE_FOLLOW_TOLERANCE_PX",
    "exercise_stage_follow",
    "stage_follow_calibration",
    "stage_follow_expected_px",
    "stage_follow_post",
    "stage_follow_screen_px",
]


def _check(value, message):
    if not value:
        raise AssertionError(message)


def _smoke_module():
    """Return ``nd2wsi.translucent_smoke`` lazily to avoid import cycles."""
    mod = sys.modules.get("nd2wsi.translucent_smoke")
    if mod is None:
        from . import translucent_smoke as mod
    return mod


def _resolve_hook(name: str, local_value, original_value):
    """Honor monkeypatches applied either here or on ``nd2wsi.translucent_smoke``."""
    if local_value is not original_value:
        return local_value
    smoke_mod = sys.modules.get("nd2wsi.translucent_smoke")
    if smoke_mod is not None:
        return getattr(smoke_mod, name, local_value)
    return local_value


def stage_follow_calibration(context):
    """The overlay's ``pixel_size_um`` as (Y, X) micrometres per base pixel, or None."""
    size = context.get("pixel_size_um")
    valid = (isinstance(size, (list, tuple)) and len(size) == 2
             and all(type(value) in (int, float) and math.isfinite(value) and value > 0 for value in size))
    return (float(size[0]), float(size[1])) if valid else None


def _rotation_terms(degrees):
    turn = degrees % 360
    exact = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}
    if turn in exact:
        return exact[turn]
    radians = math.radians(degrees)
    return math.cos(radians), math.sin(radians)


def stage_follow_expected_px(delta_um, calibration, rotation, flipped):
    """Canonical image pixels the view centre moves for a Follow displacement.

    ``delta_um`` (dx, dy) moves the view dx to the right and dy down on screen as
    displayed. The image-to-screen projection is ``check_roi_boxes``'s: rotate,
    then mirror x when flipped; this inverts it for each screen axis and spans
    that axis's micrometres at the image's (Y, X) ``calibration`` along it.
    """
    pixel_y, pixel_x = calibration
    cosine, sine = _rotation_terms(rotation)
    sign = -1.0 if flipped else 1.0
    moved = [0.0, 0.0]
    for um, (x, y) in zip(delta_um, ((sign * cosine, -sign * sine), (sine, cosine))):
        um_per_pixel = math.hypot(x * pixel_x, y * pixel_y)
        moved = [moved[0] + um * x / um_per_pixel, moved[1] + um * y / um_per_pixel]
    return moved


def stage_follow_screen_px(image_px, view):
    """Screen CSS pixels the view centre moved, projected as ``check_roi_boxes`` does."""
    cosine, sine = _rotation_terms(view["rotation"])
    dx, dy = image_px[0] * view["zoom"], image_px[1] * view["zoom"]
    x, y = dx * cosine - dy * sine, dx * sine + dy * cosine
    return [-x if view["flip"] else x, y]


def stage_follow_post(descriptor, route, body, *, timeout=5):
    """POST one JSON body to this overlay's own loopback receiver.

    The descriptor comes privately from the overlay's bridge object, never from
    discovery. No proxy is consulted. Returns (HTTP status, JSON reply or None);
    connection failures raise. The token is never part of any message.
    """
    url = urllib.parse.urlsplit(descriptor["url"])
    _check(url.scheme == "http" and url.hostname == "127.0.0.1" and url.port and url.path in ("", "/"),
           "Stage follow receiver is not a loopback HTTP URL")
    request = urllib.request.Request(
        f"http://127.0.0.1:{url.port}{route}", data=json.dumps(body, allow_nan=False).encode("utf-8"),
        method="POST", headers={"Authorization": "Bearer " + descriptor["token"],
                                "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                reply = json.load(exc)
            except ValueError:
                reply = None
        return exc.code, reply


def _same_orientation(after, before):
    """Display rotation, mirror and zoom unchanged (the center may move)."""
    return (after["rotation"] == before["rotation"] and after["flip"] == before["flip"]
            and math.isclose(after["zoom"], before["zoom"], rel_tol=1e-9))


_ORIG_STAGE_FOLLOW_CALIBRATION = stage_follow_calibration
_ORIG_STAGE_FOLLOW_EXPECTED_PX = stage_follow_expected_px
_ORIG_STAGE_FOLLOW_SCREEN_PX = stage_follow_screen_px
_ORIG_STAGE_FOLLOW_POST = stage_follow_post


def exercise_stage_follow(overlay, child, *, timeout=30, follow_timeout=10, settle=.3, heartbeat_interval=.5,
                          expect_locked=None, pixels=STAGE_FOLLOW_PIXELS, tolerance=STAGE_FOLLOW_TOLERANCE_PX):
    """Follow one authenticated loopback ``delta`` through this overlay's own receiver.

    Follow is switched on and off with the overlay's visible control, as a user
    would, and moves the image only while the overlay is locked. The descriptor
    is taken privately from the overlay's receiver; one ``delta`` in
    micrometres must move the view in screen directions (X right, Y down, as
    displayed) by exactly ``stage_follow_expected_px`` canonical image pixels,
    at the overlay's own orientation and again at a rotated view.
    The diagnostic stops this overlay's process monitor and injects a synthetic
    detection through its private callback. This tests the gated controls and
    receiver, not actual SLACS discovery or hardware. The prior detection state
    is restored, without restarting the monitor of this soon-to-close window.
    Returns bounded evidence without the receiver token or session ID; ``ok``
    is false on any mismatch, and the receiver is always stopped.
    """
    from .stage_follow import PROTOCOL

    smoke = _smoke_module()
    result = {"ok": False, "tolerance": tolerance}
    receiver, descriptor, started = None, None, time.monotonic()
    prior_discovery = None
    beats, stop_beats, beat_thread = {"sent": 0, "failure": None}, threading.Event(), None

    def post(desc, route, body, **kwargs):
        post_fn = _resolve_hook("stage_follow_post", stage_follow_post, _ORIG_STAGE_FOLLOW_POST)
        return post_fn(desc, route, body, **kwargs)

    def follow_state():
        return child.evaluate_js(STAGE_FOLLOW_STATE)

    def settled(state):
        return state if not state["pending"] else None

    def heartbeat():
        # The receiver pauses after 3 s without contact; keep it alive while
        # the slower geometry checks run, as an observing sender would.
        while not stop_beats.wait(heartbeat_interval):
            try:
                status, _reply = post(
                    descriptor, "/heartbeat", {"version": 1, "session_id": descriptor["session_id"]})
            except Exception as exc:
                beats["failure"] = type(exc).__name__
                return
            if status != 200:
                beats["failure"] = status
                return
            beats["sent"] += 1

    def stop_heartbeat():
        stop_beats.set()
        if beat_thread is not None:
            beat_thread.join(timeout=heartbeat_interval + 6)

    try:
        _check(getattr(overlay, "_stage_follow", None) is None, "A stage receiver was already running before Follow")
        saved_discovery = overlay.slacs_discovery_state()  # Also starts a lazy monitor.
        _check(overlay._discovery.stop() is True, "The SLACS process monitor did not stop before diagnostic injection")
        prior_discovery = dict(saved_discovery)
        result["discovery"] = {"source": "synthetic GUI-smoke session", "process_detection_verified": False,
                               "monitor_stopped": True, "frontend_session_verified": False, "state_restored": False}
        overlay._on_slacs_change({"available": True, "session_id": "gui-smoke", "status": "running"})

        def synthetic_detected():
            detected = child.evaluate_js(SLACS_DISCOVERY_STATE)
            return (isinstance(detected, dict) and detected.get("available") is True
                    and detected.get("session_id") == "gui-smoke")

        smoke.wait_for(synthetic_detected, timeout=follow_timeout,
                       description="synthetic SLACS session in the overlay page")
        result["discovery"]["frontend_session_verified"] = True
        smoke.wait_for(lambda: (lambda state: state if state.get("visible") else None)(follow_state()),
                       timeout=follow_timeout,
                       description="Follow control after synthetic SLACS detection (not visible)")
        context = overlay.translucent_context()
        calibration_fn = _resolve_hook(
            "stage_follow_calibration", stage_follow_calibration, _ORIG_STAGE_FOLLOW_CALIBRATION)
        expected_px_fn = _resolve_hook(
            "stage_follow_expected_px", stage_follow_expected_px, _ORIG_STAGE_FOLLOW_EXPECTED_PX)
        screen_px_fn = _resolve_hook(
            "stage_follow_screen_px", stage_follow_screen_px, _ORIG_STAGE_FOLLOW_SCREEN_PX)
        calibration = calibration_fn(context)
        if calibration is None:
            # Follow needs calibrated pixels; the product disables it then. Prove
            # that instead of failing a smoke run on an uncalibrated image.
            state = follow_state()
            result.update(skipped="uncalibrated", pixel_size_um=context.get("pixel_size_um"),
                          follow_disabled=bool(state["disabled"]), follow_title=state.get("title"))
            _check(state["disabled"] and "calibrated" in (state.get("title") or ""),
                   "Follow control is enabled for an uncalibrated image")
            _check(getattr(overlay, "_stage_follow", None) is None, "A stage receiver started for an uncalibrated image")
            return result
        pixel_y, pixel_x = calibration
        delta_um = [round(pixels[0] * pixel_x, 6), round(pixels[1] * pixel_y, 6)]
        before = smoke.camera(child)
        expected_px = expected_px_fn(delta_um, calibration, before["rotation"], before["flip"])
        result.update(pixel_size_um_yx=[pixel_y, pixel_x], delta_um=delta_um, expected_px=expected_px)
        _check(math.hypot(*expected_px) > 100 * tolerance, "Stage follow delta is too small to measure")
        before_native = smoke.native_state(child, timeout=timeout)
        before_rois = json.loads(child.evaluate_js(ROI_STATE))
        initial = follow_state()
        result.update(display_rotation=before["rotation"], display_flip=before["flip"], locked=initial["locked"])
        _check(not initial["enabled"] and initial["status"] == "off", f"Follow was not off before the check: {initial}")
        if expect_locked is not None:
            _check(initial["locked"] is expect_locked, f"Follow check expected locked={expect_locked}")
        _check(not initial["disabled"], "The Follow control is disabled (calibration or stage bridge unavailable)")
        # A user can only press a control that is laid out and not hidden. Whether
        # it is also the topmost element at its centre is recorded, not required.
        result.update(control_visible=bool(initial.get("visible")), control_hit_test=bool(initial.get("hit")))
        _check(initial.get("visible"), "The Follow control is not visible, so a user could not press it")

        child.evaluate_js(STAGE_FOLLOW_CLICK)
        enabled = smoke.wait_for(lambda: settled(follow_state()), timeout=follow_timeout,
                                 description="Follow control start")
        _check(enabled["enabled"] and enabled["status"] == "waiting" and enabled["pressed"] == "true",
               f"Follow control did not start: {enabled['status']} {enabled['message']}")
        receiver = overlay._stage_follow
        _check(receiver is not None, "Follow control did not start this overlay's receiver")
        descriptor = overlay.start_stage_follow()  # The running receiver's own descriptor; no new receiver.
        _check(descriptor.get("ok") and overlay._stage_follow is receiver, "Stage receiver changed while reading it")
        _check(descriptor.get("protocol") == PROTOCOL, "Stage receiver speaks an unexpected protocol")
        _check(descriptor.get("session_id") == enabled["session"], "Overlay page follows a different receiver session")
        result["protocol"] = PROTOCOL

        motion = {"version": 1, "session_id": descriptor["session_id"], "seq": 1, "type": "delta", "delta_um": delta_um}
        status, reply = post(descriptor, "/motion", motion)
        result["accepted"] = (reply or {}).get("result")
        _check(status == 200 and result["accepted"] == "accepted", f"Receiver did not accept the delta: HTTP {status}")
        beat_thread = threading.Thread(target=heartbeat, name="nd2wsi-smoke-stage-heartbeat", daemon=True)
        beat_thread.start()
        consumed = smoke.wait_for(lambda: (lambda state: state if state["seq"] >= 1 or state["status"] == "paused"
                                           or not state["enabled"] else None)(follow_state()),
                                  timeout=follow_timeout, description="overlay page following the delta")
        moved = smoke.camera(child)
        # Judge orientation from this snapshot, before anything restores it.
        _check(_same_orientation(moved, before),
               f"Following the delta changed the display rotation, mirror or zoom: {moved}")
        observed = [moved["center"][0] - before["center"][0], moved["center"][1] - before["center"][1]]
        result.update(observed_px=observed, observed_screen_px=screen_px_fn(observed, before),
                      frontend_seq=consumed["seq"], frontend_status=consumed["status"],
                      error_px=max(abs(a - b) for a, b in zip(observed, expected_px)))
        _check(consumed["seq"] == 1 and consumed["status"] == "following",
               f"Overlay page did not follow the delta: {consumed['status']} {consumed['message']}")
        _check(result["error_px"] <= tolerance,
               f"Overlay camera moved {observed} image px, expected {expected_px} for {delta_um} um")

        status, reply = post(descriptor, "/motion", motion)
        result["duplicate_result"] = (reply or {}).get("result")
        result["duplicate_rejected"] = status == 200 and result["duplicate_result"] == "duplicate_or_stale"
        _check(result["duplicate_rejected"], f"Duplicate seq was not rejected: HTTP {status} {result['duplicate_result']}")
        time.sleep(settle)  # Several 50 ms polls of the overlay page.
        after_duplicate, duplicate_state = smoke.camera(child), follow_state()
        result["duplicate_moved"] = (max(abs(a - b) for a, b in zip(after_duplicate["center"], moved["center"])) > tolerance
                                     or not _same_orientation(after_duplicate, moved))
        _check(not result["duplicate_moved"] and duplicate_state["seq"] == 1
               and duplicate_state["status"] == "following", "Duplicate seq moved the overlay or changed its follow state")

        last_seq = 1
        # At a rotated view the same delta still moves the view right and down on
        # screen, so it moves the canonical camera along rotated image axes.
        child.evaluate_js(STAGE_FOLLOW_ROTATE.replace("__DEGREES__", str(STAGE_FOLLOW_ROTATION_DEGREES)))
        rotated = smoke.camera(child)
        result["rotation_checked"] = rotated["rotation"] == STAGE_FOLLOW_ROTATION_DEGREES
        if not result["rotation_checked"]:
            result["rotation_note"] = f"the overlay kept rotation {rotated['rotation']} while locked"
        else:
            motion2 = {**motion, "seq": 2}
            status, reply = post(descriptor, "/motion", motion2)
            _check(status == 200 and (reply or {}).get("result") == "accepted",
                   f"Receiver did not accept the rotated-view delta: HTTP {status}")
            consumed2 = smoke.wait_for(lambda: (lambda state: state if state["seq"] >= 2 or state["status"] == "paused"
                                                or not state["enabled"] else None)(follow_state()),
                                       timeout=follow_timeout, description="overlay page following the rotated-view delta")
            moved2 = smoke.camera(child)
            _check(_same_orientation(moved2, rotated),
                   f"Following the rotated-view delta changed the display rotation, mirror or zoom: {moved2}")
            observed2 = [moved2["center"][0] - rotated["center"][0], moved2["center"][1] - rotated["center"][1]]
            expected2 = expected_px_fn(delta_um, calibration, rotated["rotation"], rotated["flip"])
            result.update(rotated_observed_px=observed2, rotated_expected_px=expected2,
                          rotated_observed_screen_px=screen_px_fn(observed2, rotated),
                          rotated_error_px=max(abs(a - b) for a, b in zip(observed2, expected2)))
            _check(consumed2["seq"] == 2 and consumed2["status"] == "following",
                   f"Overlay page did not follow the rotated-view delta: {consumed2['status']}")
            _check(result["rotated_error_px"] <= tolerance,
                   f"At rotation {STAGE_FOLLOW_ROTATION_DEGREES} the camera moved {observed2} image px, "
                   f"expected {expected2}: the delta did not follow the on-screen direction")
            last_seq = 2
        child.evaluate_js(STAGE_FOLLOW_ROTATE.replace("__DEGREES__", repr(before["rotation"])))
        _check(smoke.camera(child)["rotation"] == before["rotation"], "Display rotation was not restored after the check")

        rois = json.loads(child.evaluate_js(ROI_STATE))
        result["roi_unchanged"] = rois == before_rois
        _check(result["roi_unchanged"], "Stage follow changed ROI source geometry or the selected ROI")
        if before_rois["rois"]:
            # The outlines are redrawn with the moved camera; their source boxes are not recalculated.
            smoke.check_roi_boxes(child, expected_rois=before_rois["rois"], expected_index=before_rois["index"],
                                  expected_flip=before["flip"], timeout=timeout, description="stage-followed ROI")
        after = smoke.camera(child)
        smoke.check_camera(after, before, center=False, description="stage follow")
        _check(after["rotation"] == before["rotation"] and after["flip"] == before["flip"],
               "Stage follow changed the display rotation or mirror")
        result["camera_unchanged"] = {"zoom": after["zoom"], "rotation": after["rotation"], "flip": after["flip"]}
        after_native = smoke.native_state(child, timeout=timeout)
        result["native_unchanged"] = (
            after_native["frame"] == before_native["frame"] and after_native["topmost"] == before_native["topmost"]
            and after_native["visible"] == before_native["visible"]
            and math.isclose(after_native.get("opacity", 1), before_native.get("opacity", 1)))
        _check(result["native_unchanged"], "Stage follow changed the native window frame, opacity or topmost state")
        still = follow_state()
        result["status_before_stop"] = still["status"]
        _check(still["status"] == "following" and still["seq"] == last_seq and beats["failure"] is None,
               f"Follow did not stay connected: {still['status']} {still['message']} (heartbeat {beats['failure']})")

        stop_heartbeat()
        result["heartbeats"] = beats["sent"]
        _check(still.get("visible"), "The Follow control is no longer visible, so a user could not stop following")
        child.evaluate_js(STAGE_FOLLOW_CLICK)
        final = smoke.wait_for(lambda: settled(follow_state()), timeout=follow_timeout,
                               description="Follow control stop")
        # Only a refused connection proves the old port is closed; a timeout or a
        # reset could come from a listener that leaked.
        refused, probe = False, "answered"
        try:
            post(descriptor, "/heartbeat", {"version": 1, "session_id": descriptor["session_id"]})
        except urllib.error.URLError as exc:
            refused, probe = isinstance(exc.reason, ConnectionRefusedError), type(exc.reason).__name__
        except ConnectionRefusedError:
            refused, probe = True, "ConnectionRefusedError"
        except OSError as exc:
            probe = type(exc).__name__
        result["stop_probe"] = probe
        snapshot = receiver.snapshot()
        result["stopped"] = (not final["enabled"] and final["status"] == "off" and final["pressed"] == "false"
                             and overlay._stage_follow is None and not snapshot["enabled"]
                             and overlay.stage_follow_state().get("status") == "off" and refused)
        _check(result["stopped"], "Follow control did not stop this overlay's receiver")
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        stop_heartbeat()
        if getattr(overlay, "_stage_follow", None) is not None:
            try:
                overlay.stop_stage_follow()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        if prior_discovery is not None:
            try:
                overlay._on_slacs_change(prior_discovery)
                with overlay._lock:
                    restored = overlay._slacs_snapshot == prior_discovery
                result["discovery"]["state_restored"] = restored
                _check(restored, "The diagnostic SLACS detection state was not restored")
            except Exception as exc:
                result["ok"] = False
                result["discovery_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        result["seconds"] = round(time.monotonic() - started, 3)
    return result
