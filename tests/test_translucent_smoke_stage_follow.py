"""Packaged GUI smoke: the main viewer's overlay orientation and a real stage-follow round trip.

The overlay page is a fake that mirrors translucent-v1.js's Follow logic; the
receiver, its loopback HTTP server and the overlay bridge object are real.
"""
import copy
import functools
import json
import math
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_translucent_js as js_harness

from nd2wsi import stage_follow
from nd2wsi import translucent_smoke as smoke
from nd2wsi import webview2_runtime as rt
from nd2wsi.translucent_window import TranslucentApi

ROOT = Path(__file__).resolve().parents[1]
# Anisotropic [Y, X] so a swapped axis cannot pass.
PIXELS_YX = [.5, .8]


def make_overlay(pixels=PIXELS_YX, flip=False):
    api = TranslucentApi({"pixel_size_um": pixels, "dimensions": [2048, 2048], "image_name": "fixture.nd2",
                          "flip": flip}, logger=Mock(), on_closed=Mock())
    api._window = Mock()
    api._dispatch = lambda operation: None
    # These unit tests exercise injection and the receiver, never OS discovery.
    api._discovery = Mock()
    api._discovery.stop.return_value = True
    return api


class FakePage:
    """translucent-v1.js Follow behaviour, polled whenever the smoke reads the page.

    A displacement moves the view right and down on screen; the page finds the
    image-pixel move by inverting check_roi_boxes's projection, independently
    of the smoke's own formula. ``mode`` breaks it on purpose: "frozen"
    consumes without moving, "never" stops polling, "mirror-ignored" ignores a
    flipped view, "rotation-ignored" ignores the display rotation (the old
    canonical-axis behaviour), "swapped" uses the wrong pixel axis,
    "reset-rotation" resets the display angle. ``rotatable=False`` models a
    locked page that keeps its rotation.
    """

    def __init__(self, overlay, *, flip=False, rotation=0, locked=True, mode="correct", pixels=PIXELS_YX):
        self.overlay, self.mode, self.locked = overlay, mode, locked
        self.pixel = {"x": pixels[1], "y": pixels[0]}
        # The main viewer's orientation, shown unchanged.
        self.view = {"center": [1000.0, 800.0], "zoom": 1.5, "rotation": rotation, "flip": flip, "size": [744, 528]}
        self.follow = {"enabled": False, "status": "off", "seq": 0, "session": "", "message": "",
                       "displacement": [0, 0]}
        self.disabled = False
        self.visible, self.hit, self.title = True, True, "Follow stage commands"
        self.rotatable = True
        self.rois = [{"x": 10, "y": 20, "w": 30, "h": 40, "index": 7, "label": "QA"}]
        self.scripts = []
        self.native = {"visible": True, "minimized": False, "topmost": True, "opacity": .55,
                       "has_parent": False, "frame": [10, 20, 744, 528]}

    def poll(self):
        follow = self.follow
        if not follow["enabled"] or follow["status"] == "paused" or self.mode == "never":
            return
        state = self.overlay.stage_follow_state()
        if not state.get("enabled") or state.get("session_id") != follow["session"] or state["status"] == "paused":
            follow["status"] = "paused"
            return
        delta = [a - b for a, b in zip(state["displacement_um"], follow["displacement"])]
        if state["status"] == "following" and any(delta):
            dx, dy = self.image_delta(delta)
            if self.mode == "frozen":
                dx = dy = 0
            elif self.mode == "reset-rotation":
                self.view["rotation"] = 0  # a Follow bug that resets the display angle
            self.view["center"] = [self.view["center"][0] + dx, self.view["center"][1] + dy]
        follow.update(displacement=list(state["displacement_um"]), seq=state["seq"], status=state["status"])

    def image_delta(self, um):
        rotation = 0 if self.mode == "rotation-ignored" else self.view["rotation"]
        flip = False if self.mode == "mirror-ignored" else self.view["flip"]
        theta, sign = math.radians(rotation), -1 if flip else 1
        # check_roi_boxes: screen = mirror(rotate(image)); invert that 2x2 map.
        a, b, c, d = sign * math.cos(theta), -sign * math.sin(theta), math.sin(theta), math.cos(theta)
        det = a * d - b * c
        columns = ((d / det, -c / det), (-b / det, a / det))  # image move per screen right, per screen down
        pixel_x, pixel_y = self.pixel["x"], self.pixel["y"]
        if self.mode == "swapped":
            pixel_x, pixel_y = pixel_y, pixel_x
        moved = [0.0, 0.0]
        for value, (x, y) in zip(um, columns):
            length = math.hypot(x * pixel_x, y * pixel_y)
            moved = [moved[0] + value * x / length, moved[1] + value * y / length]
        return moved

    def click_follow(self):
        if self.disabled:
            return
        follow = self.follow
        if follow["enabled"]:
            follow.update(enabled=False, status="off", session="", message="")
            self.overlay.stop_stage_follow()
            return
        started = self.overlay.start_stage_follow()
        if started.get("ok"):
            follow.update(enabled=True, status="waiting", session=started["session_id"], seq=0, displacement=[0, 0])
        else:
            follow["message"] = started.get("message", "")

    def evaluate_js(self, script):
        self.scripts.append(script)
        if script == smoke.SLACS_DISCOVERY_STATE:
            return self.overlay.slacs_discovery_state()
        if script == smoke.CAMERA:
            self.poll()
            return copy.deepcopy(self.view)
        if script == smoke.STAGE_FOLLOW_STATE:
            self.poll()
            follow = self.follow
            detected = self.overlay.slacs_discovery_state()["available"]
            return {"enabled": follow["enabled"], "pending": False, "status": follow["status"], "seq": follow["seq"],
                    "session": follow["session"], "message": follow["message"], "locked": self.locked,
                    "disabled": self.disabled,
                    "pressed": "true" if follow["enabled"] else "false",
                    "title": self.title, "visible": self.visible and detected, "hit": self.hit and detected}
        if script == smoke.STAGE_FOLLOW_CLICK:
            self.click_follow()
            return True
        if script == smoke.ROI_STATE:
            return json.dumps({"rois": self.rois, "index": 0})
        prefix = smoke.STAGE_FOLLOW_ROTATE.split("__DEGREES__")[0]
        if script.startswith(prefix):
            if self.rotatable:
                self.view["rotation"] = float(script[len(prefix):].split(",")[0])
            return True
        raise AssertionError(f"Unexpected overlay script: {script[:80]}")


@pytest.fixture()
def follow_run(monkeypatch):
    """Run exercise_stage_follow against a real overlay receiver and a fake page."""
    made = []
    posts = []
    real_post = smoke.stage_follow_post

    def recording_post(descriptor, route, body, **kwargs):
        posts.append({"token": descriptor["token"], "route": route, "seq": body.get("seq"),
                      "session_id": body.get("session_id")})
        return real_post(descriptor, route, body, **kwargs)

    monkeypatch.setattr(smoke, "stage_follow_post", recording_post)

    def run(*, flip=False, rotation=0, mode="correct", pixels=PIXELS_YX, page_setup=None, **kwargs):
        overlay = make_overlay(pixels, flip)
        discovery_events = []

        def stop_discovery():
            discovery_events.append(("stop", None))
            change_discovery({"available": False, "session_id": None, "status": "stopped"})
            return True

        overlay._discovery.stop.side_effect = stop_discovery
        change_discovery = overlay._on_slacs_change

        def record_discovery(snapshot):
            discovery_events.append(("change", dict(snapshot)))
            change_discovery(snapshot)

        overlay._on_slacs_change = record_discovery
        page = FakePage(overlay, flip=flip, rotation=rotation, mode=mode)
        if page_setup is not None:
            page_setup(page)
        roi_checks = []

        def check_roi_boxes(child, **check):
            roi_checks.append(check)
            return {"camera": child.evaluate_js(smoke.CAMERA)}

        monkeypatch.setattr(smoke, "native_state", lambda window, timeout=30: copy.deepcopy(page.native))
        monkeypatch.setattr(smoke, "check_roi_boxes", check_roi_boxes)
        options = {"follow_timeout": 2, "settle": .1, "heartbeat_interval": .1, "expect_locked": True, **kwargs}
        result = smoke.exercise_stage_follow(overlay, page, **options)
        made.append(overlay)
        return SimpleNamespace(result=result, overlay=overlay, page=page, roi_checks=roi_checks, posts=posts,
                               discovery_events=discovery_events)

    yield run
    for overlay in made:
        overlay.stop_stage_follow()


@pytest.mark.parametrize("mode, succeeds", [("correct", True), ("frozen", False)])
def test_smoke_injects_only_after_stopping_discovery_and_restores_on_success_or_failure(follow_run, mode, succeeds):
    run = follow_run(mode=mode)
    assert run.result["ok"] is succeeds
    assert run.result["discovery"] == {
        "source": "synthetic GUI-smoke session", "process_detection_verified": False,
        "monitor_stopped": True, "frontend_session_verified": True, "state_restored": True,
    }
    prior = {"available": False, "session_id": None, "status": "waiting"}
    assert run.discovery_events == [
        ("stop", None),
        ("change", {"available": True, "session_id": "gui-smoke", "status": "running"}),
        ("change", prior),
    ]
    assert run.overlay.slacs_discovery_state() == prior
    run.overlay._discovery.stop.assert_called_once_with()
    run.overlay._discovery.start.assert_not_called()
    assert run.overlay._stage_follow is None


def test_a_monitor_that_does_not_stop_prevents_diagnostic_injection(follow_run):
    def fail_stop(page):
        page.overlay._discovery.stop.side_effect = RuntimeError("monitor did not stop")

    run = follow_run(page_setup=fail_stop)
    assert run.result["ok"] is False and "monitor did not stop" in run.result["error"]
    assert run.discovery_events == [] and run.posts == []
    assert run.overlay._slacs_snapshot["available"] is False
    assert run.overlay._stage_follow is None


def test_a_monitor_still_joining_prevents_diagnostic_injection(follow_run):
    def keep_running(page):
        page.overlay._discovery.stop.side_effect = None
        page.overlay._discovery.stop.return_value = False

    run = follow_run(page_setup=keep_running)
    assert run.result["ok"] is False and "monitor did not stop before diagnostic injection" in run.result["error"]
    assert run.discovery_events == [] and run.posts == []
    assert run.overlay._slacs_snapshot["available"] is False
    assert run.overlay._stage_follow is None


def test_a_page_that_has_not_observed_the_synthetic_session_cannot_start_follow(follow_run):
    def stale_discovery(page):
        evaluate = page.evaluate_js

        def evaluate_without_injection(script):
            if script == smoke.SLACS_DISCOVERY_STATE:
                return {"available": True, "session_id": "another-session", "status": "running"}
            return evaluate(script)

        page.evaluate_js = evaluate_without_injection

    run = follow_run(page_setup=stale_discovery, follow_timeout=.1)
    assert run.result["ok"] is False and "synthetic SLACS session in the overlay page" in run.result["error"]
    assert run.result["discovery"]["frontend_session_verified"] is False
    assert run.result["discovery"]["state_restored"] is True
    assert run.posts == [] and smoke.STAGE_FOLLOW_CLICK not in run.page.scripts


def test_failed_restoration_cannot_report_a_successful_diagnostic(follow_run):
    def fail_restore(page):
        change = page.overlay._on_slacs_change

        def change_or_fail(snapshot):
            if snapshot["available"] is not True:
                raise RuntimeError("restore failed")
            change(snapshot)

        page.overlay._on_slacs_change = change_or_fail

    run = follow_run(page_setup=fail_restore)
    assert run.result["ok"] is False
    assert run.result["discovery"]["state_restored"] is False
    assert "restore failed" in run.result["discovery_cleanup_error"]
    assert run.overlay._stage_follow is None


# The main viewer's display orientation -----------------------------------------

@pytest.mark.parametrize("source_flip", [False, True])
@pytest.mark.parametrize("old_mirror", ["absent", True, False, "true", None])
def test_overlay_flip_is_exactly_the_source_flip(source_flip, old_mirror):
    context = {"flip": source_flip}
    if old_mirror != "absent":
        context["mirror_scan"] = old_mirror  # an older context's own setting is ignored
    assert smoke.expected_overlay_flip(context, source_flip) is source_flip
    assert smoke.expected_overlay_flip({}, source_flip) is source_flip


def test_overlay_flip_rejects_a_context_that_changed_the_source_flip():
    with pytest.raises(AssertionError, match="changed the source handoff flip"):
        smoke.expected_overlay_flip({"flip": True}, False)


@pytest.fixture()
def handoff(monkeypatch):
    """exercise_translucent_viewer up to its first DOM style read (macOS path)."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def run(*, source_flip, displayed_flip):
        api = SimpleNamespace(_translucent_windows={})
        parent = SimpleNamespace(_js_api=api)
        child = SimpleNamespace(uid="overlay", evaluate_js=Mock(side_effect=[True, RuntimeError("stop after setup")]))
        overlay = SimpleNamespace(_window=child, _configured=True, _closed=False, close_translucent=Mock())
        source = {"query": "c=0", "rotation": 30, "flip": source_flip}
        context = {"query": "c=0", "flip": source_flip}
        monkeypatch.setattr(smoke, "pane_eval", lambda _parent, expression:
                            source if expression == "captureTranslucentView()" else True)
        monkeypatch.setattr(smoke, "click_shell_cloud",
                            lambda *_args, **_kwargs: api._translucent_windows.update(one=overlay))
        monkeypatch.setattr(smoke, "bridge_result", lambda *_args, **_kwargs: context)
        monkeypatch.setattr(smoke, "camera", lambda _child: {"rotation": 30, "flip": displayed_flip})
        monkeypatch.setattr(smoke, "check_camera", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(smoke, "native_state", lambda *_args, **_kwargs: {
            "topmost": True, "has_parent": False, "opaque": False, "background_alpha": 0})
        return smoke.exercise_translucent_viewer(api, parent, timeout=3)

    return run


@pytest.mark.parametrize("source_flip", [False, True])
def test_initial_handoff_expects_the_main_viewer_flip(handoff, source_flip):
    result = handoff(source_flip=source_flip, displayed_flip=source_flip)
    # Orientation passed; the fake stops the check at the next DOM read.
    assert "stop after setup" in result["error"]
    assert result["display_orientation"] == {"source_flip": source_flip, "expected_flip": source_flip,
                                             "flip": source_flip, "rotation": 30}


@pytest.mark.parametrize("source_flip", [False, True])
def test_initial_handoff_rejects_an_overlay_that_mirrors_the_main_viewer(handoff, source_flip):
    result = handoff(source_flip=source_flip, displayed_flip=not source_flip)
    assert result["ok"] is False
    assert "Image orientation was not preserved" in result["error"]


def roi_child(flip):
    """One aligned ROI box under a known camera, projected as check_roi_boxes does."""
    view = {"center": [50.0, 40.0], "zoom": 2.0, "rotation": 0, "flip": flip, "size": [200, 100]}

    def project(x, y):
        sx, sy = 100 + (x - 50) * 2, 50 + (y - 40) * 2
        return [200 - sx if flip else sx, sy]

    box = {"index": 0, "selected": True, "points": [project(*p) for p in ((30, 30), (40, 30), (40, 35), (30, 35))],
           "stroke": "rgb(255, 217, 102)", "width": 1.5, "pointerEvents": "none",
           "vectorEffect": "non-scaling-stroke", "display": "inline"}
    value = {"rois": [{"x": 30, "y": 30, "w": 10, "h": 5}], "index": 0, "boxes": [box],
             "layer": {"width": 200, "height": 100, "opacity": 1, "pointerEvents": "none", "outsideImage": True}}
    return SimpleNamespace(evaluate_js=lambda script: copy.deepcopy(view if script == smoke.CAMERA else value))


@pytest.mark.parametrize("flip", [True, False])
def test_roi_box_check_pins_the_expected_display_flip(flip):
    assert smoke.check_roi_boxes(roi_child(flip), expected_flip=flip, timeout=1)["camera"]["flip"] is flip
    with pytest.raises(AssertionError, match="display flip"):
        smoke.check_roi_boxes(roi_child(flip), expected_flip=not flip, timeout=1)


class RoiPage:
    """Enough of the overlay page for exercise_roi_boxes; ROI geometry is checked elsewhere."""

    def __init__(self, flip):
        self.view = {"center": [500.0, 400.0], "zoom": 1.0, "rotation": 0, "flip": flip, "size": [744, 528]}
        self.initial = copy.deepcopy(self.view)
        self.rois = None

    def evaluate_js(self, script):
        if script == smoke.CAMERA:
            return copy.deepcopy(self.view)
        if "getContentSize" in script:
            return [1000, 800]
        if script.startswith("nd2Translucent.setRois("):
            self.rois = json.loads(script[len("nd2Translucent.setRois("):script.index(",{pan:false")])["rois"]
            return True
        if "roi-next" in script:
            roi = self.rois[1]
            self.view["center"] = [roi["x"] + roi["w"] / 2, roi["y"] + roi["h"] / 2]
            return True
        if "setRotation(37" in script:
            self.view.update(rotation=37, flip=False)
            return True
        if "setFlip(true)" in script:
            self.view["flip"] = True
            return True
        if "saved=" in script:
            self.view = copy.deepcopy(self.initial)
            return True
        if "zoomTo(p.getZoom(true)*1.2" in script or "innerWidth===" in script:
            return True
        raise AssertionError(f"Unexpected ROI script: {script[:80]}")


@pytest.mark.parametrize("flip", [True, False])
def test_roi_exercise_expects_the_overlay_flip_before_and_after_its_explicit_flips(monkeypatch, flip):
    page, checks = RoiPage(flip), []

    def check_roi_boxes(child, *, expected_flip=None, description="ROI boxes", **_kwargs):
        checks.append((description, expected_flip))
        assert expected_flip is None or page.view["flip"] == expected_flip
        return {"camera": copy.deepcopy(page.view)}

    monkeypatch.setattr(smoke, "check_roi_boxes", check_roi_boxes)
    monkeypatch.setattr(smoke, "bridge_result", lambda *_args, **_kwargs: {"ok": True})
    assert smoke.exercise_roi_boxes(page, timeout=1, expected_flip=flip)["ok"]
    assert checks == [("ROI boxes", flip), ("next ROI", flip), ("rotated ROI", False), ("flipped ROI", True),
                      ("pan and zoom ROI", True), ("resized ROI", True), ("restored ROI", flip)]
    with pytest.raises(AssertionError, match="wrong display flip"):
        smoke.exercise_roi_boxes(RoiPage(flip), timeout=1, expected_flip=not flip)


# Stage-follow round trip -----------------------------------------------------

@pytest.mark.parametrize("rotation, flip, expected_px, expected_screen", [
    # [Y, X] = [.5, .8] um/px; 4.0 um right and 1.5 um down. Screen right runs
    # along image X at 0/180 degrees (.8 um/px) and along image Y at 90 (.5 um/px).
    (0, False, [5.0, 3.0], [7.5, 4.5]), (0, True, [-5.0, 3.0], [7.5, 4.5]),
    (90, False, [1.875, -8.0], [12.0, 2.8125]), (90, True, [1.875, 8.0], [12.0, 2.8125]),
    (180, False, [-5.0, -3.0], [7.5, 4.5]),
])
def test_one_delta_moves_the_view_right_and_down_on_screen(follow_run, rotation, flip, expected_px, expected_screen):
    run = follow_run(rotation=rotation, flip=flip)
    result = run.result
    assert result["ok"] is True, result.get("error")
    # [Y, X] calibration: 5 px * 0.8 um in X, 3 px * 0.5 um in Y.
    assert result["delta_um"] == [4.0, 1.5]
    assert result["pixel_size_um_yx"] == PIXELS_YX
    assert result["expected_px"] == pytest.approx(expected_px, abs=1e-12)
    assert max(abs(a - b) for a, b in zip(result["observed_px"], expected_px)) <= result["tolerance"]
    # At zoom 1.5 the view moves right and down on screen, whatever the orientation.
    assert result["observed_screen_px"] == pytest.approx(expected_screen, abs=1e-6)
    assert result["protocol"] == stage_follow.PROTOCOL
    assert result["accepted"] == "accepted" and result["frontend_seq"] == 1
    assert result["duplicate_rejected"] is True and result["duplicate_result"] == "duplicate_or_stale"
    assert result["duplicate_moved"] is False
    assert result["roi_unchanged"] is True and result["native_unchanged"] is True
    assert result["camera_unchanged"] == {"zoom": 1.5, "rotation": rotation, "flip": flip}
    assert result["display_rotation"] == rotation and result["display_flip"] is flip and result["locked"] is True
    assert result["status_before_stop"] == "following" and result["stopped"] is True
    # Follow was started and stopped with the visible control; the receiver is gone.
    assert run.page.scripts.count(smoke.STAGE_FOLLOW_CLICK) == 2
    assert run.overlay._stage_follow is None and run.page.follow["enabled"] is False
    assert run.overlay.stage_follow_state()["status"] == "off"
    # The projected outlines were checked against the unchanged source boxes and flip.
    assert run.roi_checks == [{"expected_rois": run.page.rois, "expected_index": 0, "expected_flip": flip,
                               "timeout": 30, "description": "stage-followed ROI"}]
    # One delta, one duplicate with the same seq, then only seq-free heartbeats and the stop probe.
    # seq 1, its duplicate, then seq 2 at the rotated view.
    assert [post["seq"] for post in run.posts if post["route"] == "/motion"] == [1, 1, 2]
    assert {post["route"] for post in run.posts} == {"/motion", "/heartbeat"}
    serialized = json.dumps(result)
    assert run.posts[0]["token"] not in serialized and run.posts[0]["session_id"] not in serialized


@pytest.mark.parametrize("rotation", [0, 90, 180, 270, 37, -90])
@pytest.mark.parametrize("flipped", [False, True])
def test_expected_pixels_project_back_onto_the_screen_axes(rotation, flipped):
    view = {"rotation": rotation, "flip": flipped, "zoom": 2.0}
    for delta_um, axis in (([3.0, 0.0], 0), ([0.0, 3.0], 1), ([-3.0, 0.0], 0), ([0.0, -3.0], 1)):
        image_px = smoke.stage_follow_expected_px(delta_um, PIXELS_YX, rotation, flipped)
        screen = smoke.stage_follow_screen_px(image_px, view)
        assert abs(screen[1 - axis]) < 1e-9 and screen[axis] * delta_um[axis] > 0
        # The move spans exactly the commanded micrometres at the image's pixel size.
        assert math.hypot(image_px[0] * PIXELS_YX[1], image_px[1] * PIXELS_YX[0]) == pytest.approx(3.0)


def test_heartbeats_keep_the_receiver_following_through_slow_checks(follow_run, monkeypatch):
    # A receiver that pauses after 1 s without contact, and an ROI check that takes longer.
    # (Windows CI runners can stall a thread or a first loopback connection for a few
    # hundred milliseconds, so the margins are generous; the packaged smoke uses 3 s.)
    monkeypatch.setattr(stage_follow, "StageFollowReceiver",
                        functools.partial(stage_follow.StageFollowReceiver, stale_timeout=1.0))

    def slow(page):
        original = page.evaluate_js

        def evaluate_js(script):
            if script == smoke.ROI_STATE and page.follow["seq"]:
                time.sleep(1.6)
            return original(script)

        page.evaluate_js = evaluate_js

    run = follow_run(page_setup=slow, heartbeat_interval=.1)
    assert run.result["ok"] is True, run.result.get("error")
    assert run.result["heartbeats"] >= 3


@pytest.mark.parametrize("mode, flip, message, expected", [
    ("frozen", False, "Overlay camera moved [0.0, 0.0] image px", [5.0, 3.0]),
    ("mirror-ignored", True, "Overlay camera moved [5.0, 3.0] image px", [-5.0, 3.0]),
    ("swapped", False, "Overlay camera moved [8.0, 1.875] image px", [5.0, 3.0]),
])
def test_a_wrong_or_missing_camera_move_fails(follow_run, mode, flip, message, expected):
    run = follow_run(mode=mode, flip=flip)
    assert run.result["ok"] is False
    assert message in run.result["error"]
    assert run.result["expected_px"] == expected
    assert "duplicate_rejected" not in run.result
    # The receiver never outlives a failed check.
    assert run.overlay._stage_follow is None


def test_an_overlay_page_that_never_follows_times_out(follow_run):
    run = follow_run(mode="never", follow_timeout=.4)
    assert run.result["ok"] is False
    assert "Timed out waiting for overlay page following the delta" in run.result["error"]
    assert run.overlay._stage_follow is None


def test_a_flip_bug_shows_only_on_a_flipped_main_viewer(follow_run):
    # Ignoring the flip passes on an unflipped view; the flipped run above fails.
    assert follow_run(flip=False, mode="mirror-ignored").result["ok"] is True


def test_an_accepted_duplicate_fails(follow_run, monkeypatch):
    real_post, seen = smoke.stage_follow_post, []

    def accepting_duplicates(descriptor, route, body, **kwargs):
        if route == "/motion":
            seen.append(body["seq"])
            if len(seen) > 1:
                return 200, {"result": "accepted"}
        return real_post(descriptor, route, body, **kwargs)

    monkeypatch.setattr(smoke, "stage_follow_post", accepting_duplicates)
    run = follow_run()
    assert run.result["ok"] is False
    assert run.result["duplicate_rejected"] is False and run.result["duplicate_result"] == "accepted"
    assert "Duplicate seq was not rejected" in run.result["error"]
    assert run.overlay._stage_follow is None


def test_a_duplicate_that_moves_the_overlay_fails(follow_run):
    def move_on_duplicate(page):
        original = page.evaluate_js
        polls = []

        def evaluate_js(script):
            if script == smoke.CAMERA and page.follow["seq"] == 1:
                polls.append(script)
                if len(polls) == 2:  # The read after the duplicate.
                    page.view["center"][0] += 5
            return original(script)

        page.evaluate_js = evaluate_js

    run = follow_run(page_setup=move_on_duplicate)
    assert run.result["ok"] is False and run.result["duplicate_moved"] is True
    assert "Duplicate seq moved the overlay" in run.result["error"]


def _uncalibrated_page(page):
    # translucent-v1.js disables Follow without a calibrated pixel size.
    page.disabled, page.title = True, "Follow needs a calibrated image pixel size"


@pytest.mark.parametrize("pixels", [None, [1.0], [0, .8], [True, .8], [float("nan"), .8]])
def test_an_uncalibrated_image_proves_follow_is_disabled_instead_of_failing(follow_run, pixels):
    run = follow_run(pixels=pixels, page_setup=_uncalibrated_page)
    assert run.result["ok"] is False and run.result["skipped"] == "uncalibrated"
    assert "error" not in run.result and run.result["follow_disabled"] is True
    assert smoke.STAGE_FOLLOW_CLICK not in run.page.scripts and run.posts == []
    assert run.overlay._stage_follow is None


def test_an_uncalibrated_image_with_an_enabled_follow_control_fails(follow_run):
    run = follow_run(pixels=None)
    assert run.result["ok"] is False and "Follow control is enabled for an uncalibrated image" in run.result["error"]


def test_an_invisible_follow_control_fails(follow_run):
    run = follow_run(page_setup=lambda page: setattr(page, "visible", False))
    assert run.result["ok"] is False and "not visible" in run.result["error"]
    assert smoke.STAGE_FOLLOW_CLICK not in run.page.scripts


def test_hit_testing_is_recorded_but_not_required(follow_run):
    run = follow_run(page_setup=lambda page: setattr(page, "hit", False))
    assert run.result["ok"] is True
    assert run.result["control_visible"] is True and run.result["control_hit_test"] is False


@pytest.mark.parametrize("flip", [False, True])
def test_the_rotated_view_check_follows_the_on_screen_direction(follow_run, flip):
    run = follow_run(rotation=30, flip=flip)
    result = run.result
    assert result["ok"] is True and result["rotation_checked"] is True
    angle = math.radians(smoke.STAGE_FOLLOW_ROTATION_DEGREES)
    # Screen right and down at 37 degrees, in image pixels, as the page's drag would map them.
    right = ((-1 if flip else 1) * math.cos(angle), -(-1 if flip else 1) * math.sin(angle))
    down = (math.sin(angle), math.cos(angle))
    lengths = [math.hypot(x * PIXELS_YX[1], y * PIXELS_YX[0]) for x, y in (right, down)]
    expected = [4.0 * right[0] / lengths[0] + 1.5 * down[0] / lengths[1],
                4.0 * right[1] / lengths[0] + 1.5 * down[1] / lengths[1]]
    assert result["rotated_expected_px"] == pytest.approx(expected)
    assert result["rotated_observed_px"] == pytest.approx(expected)
    assert result["rotated_observed_screen_px"] == pytest.approx([1.5 * 4.0 / lengths[0], 1.5 * 1.5 / lengths[1]])
    assert run.page.view["rotation"] == 30  # restored


def test_a_follow_that_ignores_the_display_rotation_fails(follow_run):
    # Start unrotated so the first delta passes and only the rotated view exposes the bug.
    run = follow_run(mode="rotation-ignored")
    assert run.result["ok"] is False and "did not follow the on-screen direction" in run.result["error"]


def test_a_locked_page_that_keeps_its_rotation_is_recorded(follow_run):
    run = follow_run(page_setup=lambda page: setattr(page, "rotatable", False))
    assert run.result["ok"] is True and run.result["rotation_checked"] is False
    assert "kept rotation" in run.result["rotation_note"]


def test_only_a_refused_connection_proves_the_old_port_closed(follow_run, monkeypatch):
    real_post = smoke.stage_follow_post

    def timing_out_after_stop(descriptor, route, body, **kwargs):
        # After the second Follow click (stop), the old port only times out.
        if route == "/heartbeat" and follow_scripts.count(smoke.STAGE_FOLLOW_CLICK) >= 2:
            raise urllib.error.URLError(TimeoutError("timed out"))
        return real_post(descriptor, route, body, **kwargs)

    follow_scripts = []

    def setup(page):
        original = page.evaluate_js

        def evaluate_js(script):
            follow_scripts.append(script)
            return original(script)

        page.evaluate_js = evaluate_js

    monkeypatch.setattr(smoke, "stage_follow_post", timing_out_after_stop)
    run = follow_run(page_setup=setup)
    assert run.result["ok"] is False and run.result["stop_probe"] == "TimeoutError"
    assert "did not stop" in run.result["error"]


def test_a_disabled_follow_control_fails(follow_run):
    run = follow_run(page_setup=lambda page: setattr(page, "disabled", True))
    assert run.result["ok"] is False
    assert "The Follow control is disabled" in run.result["error"]
    assert run.overlay._stage_follow is None


def test_follow_must_run_in_the_expected_lock_state(follow_run):
    run = follow_run(page_setup=lambda page: setattr(page, "locked", False))
    assert run.result["ok"] is False and "expected locked=True" in run.result["error"]


def test_a_moved_window_fails(follow_run):
    def move_window(page):
        original = page.evaluate_js

        def evaluate_js(script):
            if script == smoke.STAGE_FOLLOW_CLICK and page.follow["enabled"] is False:
                page.native = {**page.native, "frame": [11, 20, 744, 528]}
            return original(script)

        page.evaluate_js = evaluate_js

    run = follow_run(page_setup=move_window)
    assert run.result["ok"] is False and run.result["native_unchanged"] is False


def test_requests_go_only_to_the_loopback_receiver(monkeypatch):
    with pytest.raises(AssertionError, match="loopback"):
        smoke.stage_follow_post({"url": "http://10.0.0.100:701", "token": "t"}, "/motion", {})
    with pytest.raises(AssertionError, match="loopback"):
        smoke.stage_follow_post({"url": "https://127.0.0.1:5000", "token": "t"}, "/motion", {})
    # An environment proxy is never consulted for the loopback receiver.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    receiver = stage_follow.StageFollowReceiver()
    try:
        descriptor = receiver.start()
        status, reply = smoke.stage_follow_post(
            descriptor, "/heartbeat", {"version": 1, "session_id": descriptor["session_id"]})
        assert status == 200 and reply["result"] == "heartbeat"
        status, reply = smoke.stage_follow_post({**descriptor, "token": "wrong"}, "/heartbeat",
                                                {"version": 1, "session_id": descriptor["session_id"]})
        assert status == 401 and "token" in reply["error"]
    finally:
        receiver.close()


# The smoke's own page expressions against the real translucent-v1.js ---------

@pytest.mark.skipif(js_harness.NODE is None, reason="node is not installed")
def test_smoke_expressions_drive_the_real_overlay_page_follow_path():
    # followHarness calibration is [Y, X] = [2, .5]; the main viewer handed over
    # a 90 degree, flipped view, which the overlay shows unchanged.
    pixel_y, pixel_x = 2.0, .5
    delta_um = [smoke.STAGE_FOLLOW_PIXELS[0] * pixel_x, smoke.STAGE_FOLLOW_PIXELS[1] * pixel_y]
    body = """
      const h=followHarness({initialContext:{rois:[{x:390,y:240,w:20,h:20}],roiIndex:0}});
      const m=h.mounted,button=h.element('stage-follow');
      await m.pollSlacsDiscovery();await m.toggleLock();  // aligned and locked, as at the instrument
      // Browser behaviour the fake DOM lacks: attributes and click() on a button.
      button.getAttribute=function(name){return this.attributes[name]??null;};
      button.click=function(){if(!this.disabled)this.listeners.click({});};
      globalThis.innerWidth=800;globalThis.innerHeight=500;h.win.nd2Translucent=m;
      const doc={getElementById:h.element};
      const evaluate=script=>new Function('window','document','nd2Translucent','return eval('+JSON.stringify(script)+')')(h.win,doc,m);
      const flush=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
      const off=evaluate(__STATE__),before=evaluate(__CAMERA__),roisBefore=evaluate(__ROIS__);
      evaluate(__CLICK__);await flush();
      const on=evaluate(__STATE__);
      Object.assign(h.snapshot,{seq:1,status:'following',displacement_um:__DELTA__});
      await m.pollStageFollow();
      const moved=evaluate(__CAMERA__),followed=evaluate(__STATE__),roisAfter=evaluate(__ROIS__);
      evaluate(__CLICK__);await flush();
      return {off,on,followed,stopped:evaluate(__STATE__),before,moved,roisBefore,roisAfter,calls:h.followCalls};
    """
    for name, value in (("__STATE__", smoke.STAGE_FOLLOW_STATE), ("__CAMERA__", smoke.CAMERA),
                        ("__ROIS__", smoke.ROI_STATE), ("__CLICK__", smoke.STAGE_FOLLOW_CLICK),
                        ("__DELTA__", delta_um)):
        body = body.replace(name, json.dumps(value))
    result = js_harness.run(body)
    # The fake DOM has no layout, so visibility reads false; the label is the page's own.
    for key in ("off", "on", "followed", "stopped"):
        state = result[key]
        assert isinstance(state.pop("title"), str) and state.pop("visible") is False and state.pop("hit") is False
    common = {"pending": False, "message": "", "locked": True, "disabled": False}
    assert result["off"] == {**common, "enabled": False, "status": "off", "seq": 0, "session": "", "pressed": "false"}
    assert result["on"] == {**common, "enabled": True, "status": "waiting", "seq": 0, "session": "session-one",
                            "pressed": "true"}
    assert result["followed"] == {**result["on"], "status": "following", "seq": 1}
    # Stopped: off again (the page keeps the last seq until the next start resets it).
    assert result["stopped"] == {**result["off"], "seq": 1}
    assert result["calls"][0] == "start" and result["calls"][-1] == "stop"
    # The smoke's own expectation matches the real page: 2.5 um right is 1.25 image Y px
    # and 6 um down is 12 image X px at this 90 degree flipped view.
    observed = [a - b for a, b in zip(result["moved"]["center"], result["before"]["center"])]
    expected = smoke.stage_follow_expected_px(delta_um, (pixel_y, pixel_x), 90, True)
    assert expected == pytest.approx([12.0, 1.25])
    assert observed == pytest.approx(expected, abs=smoke.STAGE_FOLLOW_TOLERANCE_PX)
    assert result["moved"]["rotation"] == 90
    assert result["moved"]["flip"] is smoke.expected_overlay_flip({"flip": True}, True) is True
    assert {key: result["moved"][key] for key in ("zoom", "rotation", "flip", "size")} == {
        key: result["before"][key] for key in ("zoom", "rotation", "flip", "size")}
    assert result["roisAfter"] == result["roisBefore"]
    assert json.loads(result["roisAfter"]) == {"rois": [{"x": 390, "y": 240, "w": 20, "h": 20, "label": ""}],
                                               "index": 0}


# Placement inside the overlay smoke ------------------------------------------

class OverlayRun:
    """Every step of exercise_translucent_viewer on fakes, with stage follow mocked."""

    def __init__(self, monkeypatch, platform, follow_result):
        monkeypatch.setattr(sys, "platform", platform)
        self.api = SimpleNamespace(_translucent_windows={})
        self.parent_minimized = False
        self.parent = SimpleNamespace(_js_api=self.api, minimize=lambda: setattr(self, "parent_minimized", True),
                                      restore=lambda: setattr(self, "parent_minimized", False))
        self.locked = False
        self.view = {"center": [100.0, 80.0], "zoom": 1.0, "rotation": 0, "flip": False, "size": [744, 528]}
        self.frame = [10, 20, 744, 528]
        self.child = SimpleNamespace(uid="overlay", evaluate_js=self.evaluate_js)
        self.overlay = SimpleNamespace(_window=self.child, _configured=True, _closed=False, close_translucent=Mock())
        source = {"query": "c=0", "rotation": 0, "flip": False, "center": [100.0, 80.0], "zoom": 1.0}
        monkeypatch.setattr(smoke, "pane_eval", lambda _parent, expression: source if expression ==
                            "captureTranslucentView()" else "[]" if "annotations" in expression else True)
        monkeypatch.setattr(smoke, "click_shell_cloud",
                            lambda *_args, **_kwargs: self.api._translucent_windows.update(one=self.overlay))
        monkeypatch.setattr(smoke, "bridge_result", self.bridge)
        monkeypatch.setattr(smoke, "camera", lambda _child: copy.deepcopy(self.view))
        monkeypatch.setattr(smoke, "native_state", self.native)
        monkeypatch.setattr(smoke, "wheel", self.wheel)
        monkeypatch.setattr(smoke, "drag_image", self.drag)
        monkeypatch.setattr(smoke, "exercise_opacity_controls", lambda *_args, **_kwargs: {"ok": True})
        monkeypatch.setattr(smoke, "exercise_roi_boxes", Mock(return_value={"ok": True}))
        self.follow_calls = []

        def follow(overlay, child, **kwargs):
            self.follow_calls.append({"overlay": overlay, "child": child, "locked": self.locked,
                                      "parent_minimized": self.parent_minimized, **kwargs})
            return follow_result

        monkeypatch.setattr(smoke, "exercise_stage_follow", follow)
        monkeypatch.setattr(rt, "window_evidence", Mock(return_value={"bundled": True, "error": None}))
        monkeypatch.delenv(rt.MODE_ENV, raising=False)

    def bridge(self, _window, expression, timeout=30):
        if "translucent_context" in expression:
            # An older context's own mirror setting is ignored.
            return {"query": "c=0", "flip": False, "mirror_scan": True, "opacity": .55}
        if "resize_window" in expression:
            return {"ok": not self.locked}
        if "move_window" in expression:
            if self.locked:
                return False
            self.frame = [self.frame[0] + 31, self.frame[1] + 19, *self.frame[2:]]
            return True
        if "set_translucent_opacity" in expression:
            return {"ok": False, "opacity": .55}
        raise AssertionError(expression)

    def native(self, window, timeout=30):
        if window is self.parent:
            return {"minimized": self.parent_minimized}
        return {"visible": True, "minimized": False, "topmost": True, "opacity": .55, "has_parent": False,
                "opaque": False, "background_alpha": 0, "frame": list(self.frame)}

    def wheel(self, _child):
        if not self.locked:
            self.view["zoom"] *= 1.1
        return True

    def drag(self, _child):
        if not self.locked:
            self.view["center"] = [value + 1 for value in self.view["center"]]
        return True

    def evaluate_js(self, script):
        if "state.ready" in script:
            return True
        if "getComputedStyle(button).backgroundColor" in script:
            return {"background": "rgba(0, 0, 0, 0)", "color": "rgb(255, 255, 255)", "radius": "10px",
                    "navHidden": True}
        if "innerWidth===744" in script:
            return True
        if "overlay-lock').click()" in script:
            self.locked = True
            return True
        if "nd2Translucent.state.locked && !nd2Translucent.state.pending" in script:
            return self.locked
        if "overlay-opacity').disabled" in script:
            return self.locked
        if "overlay-close').click()" in script:
            self.overlay._closed = True
            self.api._translucent_windows.clear()
            return True
        raise AssertionError(f"Unexpected overlay script: {script[:80]}")

    def run(self):
        return smoke.exercise_translucent_viewer(self.api, self.parent, timeout=3)


def test_windows_overlay_smoke_follows_the_stage_while_locked_before_closing(monkeypatch):
    evidence = {"ok": True, "observed_px": [5.0, 3.0]}
    run = OverlayRun(monkeypatch, "win32", evidence)
    result = run.run()
    assert result["ok"] is True, result.get("error")
    assert result["stage_follow"] == evidence
    assert result["display_orientation"]["expected_flip"] is False
    [call] = run.follow_calls
    assert call["overlay"] is run.overlay and call["child"] is run.child
    assert call["timeout"] == 3 and call["expect_locked"] is True
    # After the lock checks, before the parent minimize and the locked close.
    assert call["locked"] is True and call["parent_minimized"] is False
    assert result["lock_blocks_image_and_window"] and result["locked_close"]


def test_a_failed_stage_follow_fails_the_windows_overlay_smoke(monkeypatch):
    evidence = {"ok": False, "error": "AssertionError: Overlay camera moved [0.0, 0.0] image px"}
    run = OverlayRun(monkeypatch, "win32", evidence)
    result = run.run()
    assert result["ok"] is False
    assert result["stage_follow"] == evidence
    assert "Stage follow round trip failed: AssertionError: Overlay camera moved" in result["error"]
    assert "survives_parent_minimize" not in result
    run.overlay.close_translucent.assert_called_once()


def test_the_macos_overlay_smoke_does_not_run_stage_follow(monkeypatch):
    run = OverlayRun(monkeypatch, "darwin", {"ok": False})
    result = run.run()
    assert result["ok"] is True, result.get("error")
    assert run.follow_calls == [] and "stage_follow" not in result


# Packaged Windows verifier ---------------------------------------------------

def test_windows_verifier_requires_the_overlay_stage_follow_proof():
    verifier = (ROOT / "packaging/verify_windows.ps1").read_text(encoding="utf-8")
    proof = "$stageFollow = Get-JsonMember $gui @('translucent_viewer', 'stage_follow')"
    assert proof in verifier
    assert "if ((Get-JsonMember $stageFollow @('ok')) -ne $true) {" in verifier
    assert "$stageFollowVerified = $false" in verifier
    assert "stage_follow_verified = $stageFollowVerified" in verifier
    # In the bundled GUI run's report checks, after its runtime proof and before the headless branch.
    gui = verifier.index("elseif ($mode -eq 'gui-smoke') {")
    assert (gui < verifier.index("Assert-AppContainerAccess -Folder $runtimeRoot") < verifier.index(proof)
            < verifier.index("$stageFollowVerified = $true")
            < verifier.index("elseif (-not (Test-Path -LiteralPath $log)"))


def test_a_follow_that_resets_the_display_rotation_fails_before_any_restore(follow_run):
    # The Codex SLACS chat's reproduction: a page that resets rotation whenever it
    # consumes a delta must not pass because the smoke later restores the angle.
    run = follow_run(mode="reset-rotation", rotation=30)
    assert run.result["ok"] is False
    assert "changed the display rotation, mirror or zoom" in run.result["error"]


def test_a_rotation_reset_only_at_the_rotated_view_fails(follow_run):
    def reset_on_second(page):
        original_poll = page.poll

        def poll():
            before_seq = page.follow["seq"]
            original_poll()
            if before_seq < 2 <= page.follow["seq"]:
                page.view["rotation"] = 0

        page.poll = poll

    run = follow_run(page_setup=reset_on_second)
    assert run.result["ok"] is False
    assert "rotated-view delta changed the display rotation" in run.result["error"]
