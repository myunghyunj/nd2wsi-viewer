"""Verify SLACS live LUT updates during active Follow + locked calibration + same-window reopen.

Runs inside one private Agent window via ``qa/run_native_check.py``.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

os.environ["ND2WSI_DISABLE_UPDATES"] = "1"
os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer SLACS Live LUT QA"

if importlib.util.find_spec("nd2wsi") is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import tifffile  # noqa: E402
import webview  # noqa: E402

import nd2wsi  # noqa: E402
from nd2wsi import app  # noqa: E402
from nd2wsi.translucent_smoke import (  # noqa: E402
    bridge_result,
    camera,
    check_camera,
    check_roi_boxes,
    click_shell_cloud,
    native_state,
    wait_for,
)
from nd2wsi.window_sessions import create_window_session  # noqa: E402

qa = Path(os.environ["ASTROMAP_QA_ROOT"]).resolve()
report_path = qa / os.environ.get("ASTROMAP_QA_REPORT", "live-lut-slacs-native-check.json")
session = create_window_session("agent", qa / "sessions")
os.environ["ND2WSI_LUT_SETTINGS_ROOT"] = str(session.exports_root / "lut-settings")
os.environ["ND2WSI_UI_SETTINGS_ROOT"] = str(session.exports_root / "ui-settings")
os.environ["ND2WSI_FOLLOW_FLIPS_ROOT"] = str(session.exports_root / "follow-flips")
os.environ["ND2WSI_LASER_TARGET_ROOT"] = str(session.exports_root / "laser-target")
api, parent = app.create_app_window(None, window_session=session)
report = {
    "ok": False,
    "module": nd2wsi.__file__,
    "session": session.as_dict(),
    "scope": "live-lut-slacs-follow-locked-native",
    "checks": {},
}

CENTER = (256.0, 256.0)
DRAG_TARGETS = (
    ("initial", 255.0, 64.0),
    ("hold_192", 192.0, 85.0),
    ("hold_128", 128.0, 127.0),
    ("release_80", 80.0, 204.0),
)
CAL_TOLERANCE = 8.0
QA_SLACS_SESSION = "slacs-" + uuid.uuid4().hex


class SyntheticDiscovery:
    def __init__(self, session_id: str = QA_SLACS_SESSION):
        self.state = {"available": True, "session_id": session_id, "status": "running"}

    def snapshot(self):
        return dict(self.state)

    def stop(self):
        return True


def write_report():
    report["exports_report"] = str(session.exports_root / "live-lut-slacs-native-report.json")
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (session.exports_root / "live-lut-slacs-native-report.json").write_text(payload, encoding="utf-8")
    report_path.write_text(payload, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


def check(value, message):
    if not value:
        raise AssertionError(message)


def _pane_eval(expression):
    body = expression.strip()
    if ";" in body and not body.startswith(("(()", "(function")):
        head, tail = body.rsplit(";", 1)
        tail = tail.strip()
        stmt = f"{head}; return ({tail});" if tail else f"{head}; return true;"
    else:
        stmt = f"return ({body});"
    return parent.evaluate_js(
        "(() => {"
        " const __frame = globalThis.document.querySelector('#frames iframe.active');"
        " const __win = __frame && __frame.contentWindow;"
        " if (!__win) return null;"
        " const state = __win.nd2ViewerState;"
        " const document = __win.document;"
        " const Nd2ViewerGeometry = __win.Nd2ViewerGeometry;"
        " const OpenSeadragon = __win.OpenSeadragon;"
        " const captureTranslucentView = __win.captureTranslucentView;"
        " const tileQuery = __win.tileQuery;"
        " " + stmt +
        "})()"
    )


_COMPOSITED_PATCH_SAMPLER_JS = """
const sampleCompositedPatch = (viewer, imageX, imageY, includeBackdrop) => {
  if (!viewer || !viewer.viewport || !viewer.drawer) return { error: "viewer not ready" };
  const vp = viewer.viewport;
  const size = vp.getContainerSize();
  const canvas = viewer.drawer.canvas;
  const ctx = viewer.drawer.context || (canvas && canvas.getContext("2d"));
  if (!canvas || !ctx || !(size.x > 0) || !(size.y > 0) || !(canvas.width > 0) || !(canvas.height > 0)) {
    return { error: "canvas not ready" };
  }
  const backdrop = includeBackdrop ? document.getElementById("translucent-backdrop") : null;
  let bgVis = false;
  let bgOpacity = 0;
  let bgCtx = null;
  if (backdrop && !backdrop.hidden && backdrop.width > 0 && backdrop.height > 0) {
    const bgStyle = getComputedStyle(backdrop);
    bgOpacity = Number.parseFloat(bgStyle.opacity) || 0;
    if (bgStyle.display !== "none" && bgStyle.visibility !== "hidden" && bgOpacity > 0) {
      bgCtx = backdrop.getContext("2d");
      if (bgCtx) bgVis = true;
    }
  }
  const fgSx = canvas.width / size.x;
  const fgSy = canvas.height / size.y;
  const fgCoords = [];
  let fgMinX = Infinity, fgMinY = Infinity, fgMaxX = -Infinity, fgMaxY = -Infinity;
  const bgSx = bgVis ? backdrop.width / size.x : 0;
  const bgSy = bgVis ? backdrop.height / size.y : 0;
  const bgCoords = bgVis ? [] : null;
  let bgMinX = Infinity, bgMinY = Infinity, bgMaxX = -Infinity, bgMaxY = -Infinity;
  let hasBgCoords = false;
  for (let dy = -2; dy <= 2; dy++) {
    for (let dx = -2; dx <= 2; dx++) {
      const pt = Nd2ViewerGeometry.imageToViewerElementPoint(
        vp, { x: imageX + dx + 0.5, y: imageY + dy + 0.5 }, OpenSeadragon
      );
      const fx = Math.floor(pt.x * fgSx);
      const fy = Math.floor(pt.y * fgSy);
      if (!Number.isFinite(fx) || !Number.isFinite(fy) ||
          fx < 0 || fx >= canvas.width || fy < 0 || fy >= canvas.height) {
        return { error: "backing coordinate out of bounds", coord: [fx, fy] };
      }
      fgCoords.push([fx, fy]);
      if (fx < fgMinX) fgMinX = fx;
      if (fy < fgMinY) fgMinY = fy;
      if (fx > fgMaxX) fgMaxX = fx;
      if (fy > fgMaxY) fgMaxY = fy;
      if (bgVis) {
        const bx = Math.floor(pt.x * bgSx);
        const by = Math.floor(pt.y * bgSy);
        if (!Number.isFinite(bx) || !Number.isFinite(by) ||
            bx < 0 || bx >= backdrop.width || by < 0 || by >= backdrop.height) {
          bgCoords.push(null);
        } else {
          bgCoords.push([bx, by]);
          hasBgCoords = true;
          if (bx < bgMinX) bgMinX = bx;
          if (by < bgMinY) bgMinY = by;
          if (bx > bgMaxX) bgMaxX = bx;
          if (by > bgMaxY) bgMaxY = by;
        }
      }
    }
  }
  const fgBw = fgMaxX - fgMinX + 1;
  const fgBh = fgMaxY - fgMinY + 1;
  const fgData = ctx.getImageData(fgMinX, fgMinY, fgBw, fgBh).data;
  let bgData = null;
  let bgBw = 0;
  if (bgVis && hasBgCoords) {
    bgBw = bgMaxX - bgMinX + 1;
    const bgBh = bgMaxY - bgMinY + 1;
    bgData = bgCtx.getImageData(bgMinX, bgMinY, bgBw, bgBh).data;
  }
  let sumR = 0, sumG = 0, sumB = 0, minAlpha = 255;
  let fgMinAlpha = 255;
  for (let i = 0; i < fgCoords.length; i++) {
    const [fx, fy] = fgCoords[i];
    const fOff = ((fy - fgMinY) * fgBw + (fx - fgMinX)) * 4;
    const fgR = fgData[fOff], fgG = fgData[fOff + 1], fgB = fgData[fOff + 2], fgA = fgData[fOff + 3];
    if (fgA < fgMinAlpha) fgMinAlpha = fgA;
    let bgR = 0, bgG = 0, bgB = 0, bgA = 0;
    if (bgVis && bgCoords[i] && bgData) {
      const bOff = ((bgCoords[i][1] - bgMinY) * bgBw + (bgCoords[i][0] - bgMinX)) * 4;
      bgR = bgData[bOff]; bgG = bgData[bOff + 1]; bgB = bgData[bOff + 2]; bgA = bgData[bOff + 3];
    }
    const aF = fgA / 255;
    const aB = bgVis ? (bgA / 255) * bgOpacity : 0;
    const aOut = aF + aB * (1 - aF);
    let outR = 0, outG = 0, outB = 0;
    if (aOut > 0) {
      const bW = aB * (1 - aF);
      outR = (fgR * aF + bgR * bW) / aOut;
      outG = (fgG * aF + bgG * bW) / aOut;
      outB = (fgB * aF + bgB * bW) / aOut;
    }
    const outA = Math.round(aOut * 255);
    sumR += outR; sumG += outG; sumB += outB;
    if (outA < minAlpha) minAlpha = outA;
  }
  const n = fgCoords.length;
  const meanRgb = [sumR / n, sumG / n, sumB / n];
  return {
    mean_rgb: meanRgb,
    mean_intensity: (meanRgb[0] + meanRgb[1] + meanRgb[2]) / 3,
    min_alpha: minAlpha,
    fg_min_alpha: fgMinAlpha,
    backdrop_visible: bgVis,
    samples: n,
  };
};
"""


def sample_canvas_patch(window, image_x, image_y, *, is_pane=False, allow_none=False):
    viewer_expr = "state.viewer" if is_pane else "window.nd2Translucent && window.nd2Translucent.viewer"
    include_backdrop_js = "false" if is_pane else "true"
    script = (
        "(() => {"
        + _COMPOSITED_PATCH_SAMPLER_JS
        + f"return sampleCompositedPatch({viewer_expr}, {float(image_x)!r}, {float(image_y)!r}, {include_backdrop_js});"
        + "})()"
    )
    result = None
    for attempt in range(3):
        result = _pane_eval(script) if is_pane else window.evaluate_js(script)
        if result is not None:
            break
        if attempt < 2:
            time.sleep(0.05)
    if result is None and allow_none:
        return None
    if not isinstance(result, dict) or "error" in result:
        raise AssertionError(f"sample_canvas_patch failed ({'pane' if is_pane else 'overlay'}): {result}")
    return result


def build_calibrated_fixture() -> Path:
    """Write a calibrated (0.5 µm/px) 1-channel uint8 OME-TIFF under exports_root."""
    cal_path = session.exports_root / "cal_slacs_1ch.ome.tif"
    arr = np.zeros((1, 512, 512), dtype=np.uint8)
    arr[0, 128:384, 128:384] = 64
    arr[0, :16, :] = 255
    tifffile.imwrite(
        cal_path,
        arr,
        ome=True,
        tile=(256, 256),
        photometric="minisblack",
        metadata={
            "axes": "CYX",
            "PhysicalSizeX": 0.5,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.5,
            "PhysicalSizeYUnit": "µm",
            "Channel": {"Name": ["CalWhite"], "Color": [-1]},
        },
    )
    return cal_path


def _ensure_channels_open():
    _pane_eval("if (state.windows.channels.isHidden()) state.windows.channels.open(); true")
    wait_for(
        lambda: _pane_eval(
            "(() => {"
            " if (!state.info || state.info.channels.length !== 1) return false;"
            " if (state.lutAutoPending.some(Boolean)) return false;"
            " const cs = [...document.querySelectorAll('.lut-canvas')];"
            " if (cs.length !== 1) return false;"
            " const r = cs[0].getBoundingClientRect();"
            " const a = Number(cs[0].getAttribute('data-axis-min'));"
            " const b = Number(cs[0].getAttribute('data-axis-max'));"
            " return r.width >= 150 && r.height >= 60 && Number.isFinite(a) && Number.isFinite(b) && b > a;"
            "})()"
        ),
        timeout=20,
        description="channels panel with 1 LUT canvas",
    )


def _reset_luts(lo=0, hi=255, gamma=1):
    return _pane_eval(
        f"(() => {{ state.lutWidgets[0].setLut({{ lo: {lo}, hi: {hi}, gamma: {gamma} }}); return state.luts[0]; }})()"
    )


def _within(sample, target, tol=CAL_TOLERANCE):
    return sample["min_alpha"] == 255 and abs(sample["mean_intensity"] - target) <= tol


def _dispatch_hi_pointer(event_type, target_value, pointer_id=91):
    return _pane_eval(
        f"(() => {{"
        f" const canvas = document.querySelectorAll('.lut-canvas')[0];"
        f" const r = canvas.getBoundingClientRect();"
        f" const vmin = Number(canvas.getAttribute('data-axis-min'));"
        f" const vmax = Number(canvas.getAttribute('data-axis-max'));"
        f" const x0 = 4, x1 = r.width - 4;"
        f" const frac = Math.max(0, Math.min(1, ({float(target_value)!r} - vmin) / (vmax - vmin)));"
        f" const cx = r.left + x0 + frac * (x1 - x0);"
        f" const cy = r.top + 8;"
        f" canvas.dispatchEvent(new PointerEvent({json.dumps(event_type)}, {{"
        f"   pointerId: {pointer_id}, pointerType: 'mouse', isPrimary: true,"
        f"   button: 0, buttons: {0 if event_type in ('pointerup', 'pointercancel') else 1},"
        f"   clientX: cx, clientY: cy, bubbles: true, cancelable: true"
        f" }}));"
        f" return {{ val: {float(target_value)!r}, luts: state.luts[0] }};"
        f"}})()"
    )


def _dispatch_hi_release(target_value, pointer_id=91):
    return _pane_eval(
        f"(() => {{"
        f" const canvas = document.querySelectorAll('.lut-canvas')[0];"
        f" const r = canvas.getBoundingClientRect();"
        f" const vmin = Number(canvas.getAttribute('data-axis-min'));"
        f" const vmax = Number(canvas.getAttribute('data-axis-max'));"
        f" const x0 = 4, x1 = r.width - 4;"
        f" const frac = Math.max(0, Math.min(1, ({float(target_value)!r} - vmin) / (vmax - vmin)));"
        f" const cx = r.left + x0 + frac * (x1 - x0);"
        f" const cy = r.top + 8;"
        f" const send = (type, buttons) => canvas.dispatchEvent(new PointerEvent(type, {{"
        f"   pointerId: {pointer_id}, pointerType: 'mouse', isPrimary: true,"
        f"   button: 0, buttons, clientX: cx, clientY: cy, bubbles: true, cancelable: true"
        f" }}));"
        f" send('pointermove', 1);"
        f" send('pointerup', 0);"
        f" return {{ val: {float(target_value)!r}, luts: state.luts[0] }};"
        f"}})()"
    )


def _poll_both(child, target, *, hold_seconds):
    t0 = time.monotonic()
    deadline = t0 + hold_seconds
    main_hit = overlay_hit = None
    main_last = overlay_last = None
    while True:
        ms = sample_canvas_patch(parent, *CENTER, is_pane=True, allow_none=True)
        os_ = sample_canvas_patch(child, *CENTER, is_pane=False, allow_none=True)
        if ms is not None:
            main_last = ms
            if main_hit is None and _within(ms, target):
                main_hit = ms
        if os_ is not None:
            overlay_last = os_
            if overlay_hit is None and _within(os_, target):
                overlay_hit = os_
        if main_hit is not None and overlay_hit is not None:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return {
        "target": target,
        "main_hit": main_hit is not None,
        "overlay_hit": overlay_hit is not None,
        "main": main_hit or main_last,
        "overlay": overlay_hit or overlay_last,
        "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 1),
    }


def _send_follow(descriptor, route, **event):
    body = {"version": 1, "session_id": descriptor["session_id"], **event}
    req = urllib.request.Request(
        descriptor["url"] + route,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + descriptor["token"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as response:
        return json.load(response)


def run():
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    try:
        context = api.window_context()
        check(context["role"] == "agent" and context["id"] == session.id, "Wrong Agent window identity")
        wait_for(lambda: parent.evaluate_js("!!window.pywebview?.api"), description="parent bridge")
        bridged = bridge_result(parent, "pywebview.api.window_context()")
        check(bridged["role"] == "agent" and bridged["id"] == session.id, "Parent bridge identity mismatch")
        report["bridge_identity_verified"] = True

        cal_path = build_calibrated_fixture()
        api._httpd, url = app.start_server([cal_path], window_session=session)
        parent.load_url(url)
        wait_for(
            lambda: _pane_eval(
                "!!(state.info && state.info.channels.length === 1 && state.annReady && "
                "state.viewer && state.viewer.isOpen() && state.viewer.world.getItemAt(0)?.getFullyLoaded())"
            ),
            timeout=25,
            description="calibrated 1-channel slide ready",
        )
        _ensure_channels_open()
        _reset_luts(0, 255, 1)
        wait_for(
            lambda: _within(sample_canvas_patch(parent, *CENTER, is_pane=True), 64.0),
            timeout=10,
            description="main baseline 64",
        )

        # Inject synthetic SLACS detection before opening translucent overlay.
        synthetic = SyntheticDiscovery()
        if api._slacs_discovery is not None:
            api._slacs_discovery.stop()
        api._slacs_discovery = synthetic

        sid = next(iter(api._httpd.registry.slides.keys()))
        click_shell_cloud(parent)
        overlay = wait_for(lambda: api._translucent_windows.get(sid), timeout=20, description="SLACS overlay")
        child = overlay._window
        resolve_follow = overlay._stage_follow_options
        overlay._stage_follow_options = lambda options: ("adapter", *resolve_follow(options)[1:])
        wait_for(lambda: child.evaluate_js("!!window.nd2Translucent?.state.ready"), timeout=20, description="overlay ready")
        wait_for(lambda: overlay._configured, timeout=20, description="overlay configured")
        wait_for(lambda: child.evaluate_js("nd2Translucent.viewer.world.getItemAt(0).getFullyLoaded()"),
                 timeout=20, description="overlay tiles")

        # Enable synthetic SLACS detection in the overlay and start Stage Follow.
        overlay.slacs_discovery_state()
        if overlay._discovery is not None:
            overlay._discovery.stop()
        overlay._on_slacs_change(synthetic.snapshot())
        wait_for(
            lambda: child.evaluate_js(
                f"nd2Translucent.state.slacs.session_id === {json.dumps(QA_SLACS_SESSION)} && "
                "!document.getElementById('stage-follow-control').hidden"
            ),
            timeout=15,
            description="overlay SLACS follow controls visible",
        )

        # Add diagnostic ROI + calibrate laser target before locking.
        rois = [
            {"x": 160.0, "y": 160.0, "w": 64.0, "h": 64.0, "index": 1, "label": "SLACS Cal ROI 1"},
            {"x": 280.0, "y": 240.0, "w": 64.0, "h": 64.0, "index": 2, "label": "SLACS Cal ROI 2"},
        ]
        check(
            child.evaluate_js(
                "nd2Translucent.setRois(" + json.dumps({"ok": True, "name": "slacs-qa", "rois": rois}) + ", {pan: false, index: 0})"
            ),
            "Could not load diagnostic ROI boxes",
        )
        check_roi_boxes(child, expected_rois=rois, expected_index=0, timeout=15, description="initial SLACS ROI")

        win_frame = child.evaluate_js("[screenX, screenY, innerWidth, innerHeight]")
        dpr_now = float(child.evaluate_js("Number(devicePixelRatio) || 1"))
        zoom_now = camera(child)["zoom"]
        save_res = overlay.save_laser_target({
            "target": {
                "offset_css": [8.0, -6.0],
                "side_um": 18.0,
                "rotation_deg": 45.0,
                "window": win_frame,
                "dpr": dpr_now,
            },
            "calibrate": True,
            "zoom": zoom_now,
        })
        check(save_res.get("ok") is True, f"save_laser_target failed: {save_res}")

        check(bridge_result(child, "nd2Translucent.startStageFollow()"), "startStageFollow failed")
        descriptor = overlay.start_stage_follow()
        check(descriptor.get("ok") is True, f"overlay.start_stage_follow descriptor failed: {descriptor}")

        def heartbeat():
            while not heartbeat_stop.wait(0.4):
                try:
                    _send_follow(descriptor, "/heartbeat")
                except Exception:
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()

        check(bridge_result(child, "nd2Translucent.toggleLock().then(() => true)"), "toggleLock failed")
        wait_for(lambda: child.evaluate_js("nd2Translucent.state.locked && !nd2Translucent.state.pending"),
                 timeout=15, description="overlay locked")

        # Establish active following at seq=2, then hold stage completely at rest.
        _send_follow(descriptor, "/motion", seq=1, type="reference", position_um=[1000.0, 1000.0])
        _send_follow(descriptor, "/motion", seq=2, type="delta", delta_um=[1.0, 0.0])
        wait_for(lambda: child.evaluate_js("nd2Translucent.state.follow.seq === 2"),
                 timeout=15, description="follow seq=2")
        # Flush any settled view report before beginning the LUT drag observation window.
        time.sleep(0.35)

        # Instrument report_overlay_view + stage receiver + #laser-annotations mutations.
        orig_report_view = overlay.report_overlay_view
        view_reports_during_drag = []

        def tracked_report_view(view_payload):
            view_reports_during_drag.append(view_payload)
            return orig_report_view(view_payload)

        overlay.report_overlay_view = tracked_report_view
        view_at_before = overlay._overlay_view["at"] if overlay._overlay_view else None
        receiver = overlay._stage_follow
        receiver_before = receiver.snapshot()
        before_cam = camera(child)
        before_frame = native_state(child)["frame"]
        before_target = overlay.translucent_context()["laser_target"]
        child.evaluate_js("""(() => {
          const layer = document.getElementById('laser-annotations');
          window.__qaLaserMutations = 0;
          const obs = new MutationObserver(() => { window.__qaLaserMutations += 1; });
          if (layer) obs.observe(layer, { childList: true, subtree: true, attributes: true });
          window.__qaLaserObs = obs;
          return true;
        })()""")

        try:
            steps = {}
            main_init = sample_canvas_patch(parent, *CENTER, is_pane=True)
            ov_init = sample_canvas_patch(child, *CENTER, is_pane=False)
            steps["initial"] = {
                "target": 64.0,
                "main_hit": _within(main_init, 64.0),
                "overlay_hit": _within(ov_init, 64.0),
                "main": main_init,
                "overlay": ov_init,
            }
            _dispatch_hi_pointer("pointerdown", 255.0)
            released = False
            try:
                _dispatch_hi_pointer("pointermove", 192.0)
                steps["hold_192"] = _poll_both(child, 85.0, hold_seconds=1.0)
                _dispatch_hi_pointer("pointermove", 128.0)
                steps["hold_128"] = _poll_both(child, 127.0, hold_seconds=1.0)
                _dispatch_hi_release(80.0)
                released = True
            finally:
                if not released:
                    _dispatch_hi_pointer("pointerup", 80.0)
            steps["release_80"] = _poll_both(child, 204.0, hold_seconds=2.5)
            # Wait >200ms trailing view-report debounce to ensure zero delayed reports fire.
            time.sleep(0.35)
        finally:
            overlay.report_overlay_view = orig_report_view
            laser_mutations = child.evaluate_js(
                "(() => { if (window.__qaLaserObs) window.__qaLaserObs.disconnect(); return window.__qaLaserMutations || 0; })()"
            )

        receiver_after = receiver.snapshot()
        follow_js_after = child.evaluate_js("({ enabled: nd2Translucent.state.follow.enabled, status: nd2Translucent.state.follow.status, seq: nd2Translucent.state.follow.seq, locked: nd2Translucent.state.locked })")
        after_cam = camera(child)
        after_frame = native_state(child)["frame"]
        after_target = overlay.translucent_context()["laser_target"]
        view_at_after = overlay._overlay_view["at"] if overlay._overlay_view else None
        roi_after = check_roi_boxes(child, expected_rois=rois, expected_index=0, timeout=10, description="post-drag SLACS ROI")

        # Verify laser-box prep succeeds without false view_after_press rejection right after LUT drag.
        laser_prep_payload, laser_prep_reason = overlay._prepare_laser_box({
            "locked": True,
            "receiver_id": overlay._stage_follow_receiver_id,
            "press": 1,
            "follow_seq": receiver_after["seq"],
            "observed_at": (view_at_after or time.monotonic()) + 0.05,
            "at": "2026-11-25T00:00:00.000+00:00",
            "slacs_session": QA_SLACS_SESSION,
        })

        main_ok = all(steps[k]["main_hit"] for k, _, _ in DRAG_TARGETS)
        overlay_ok = all(steps[k]["overlay_hit"] for k, _, _ in DRAG_TARGETS)
        follow_errors = []
        if not main_ok or not overlay_ok:
            follow_errors.append(f"Drag intensities missed [64, 85, 127, 204] ± 8: {steps}")
        if not follow_js_after["enabled"] or follow_js_after["status"] not in ("waiting", "following"):
            follow_errors.append(f"Stage Follow stopped or paused during LUT drag: {follow_js_after}")
        if follow_js_after["seq"] != 2 or receiver_after["seq"] != receiver_before["seq"]:
            follow_errors.append(f"Stage Follow seq changed during LUT drag: js={follow_js_after}, py={receiver_after}")
        if follow_js_after["locked"] is not True:
            follow_errors.append("Overlay unlocked during LUT drag")
        if view_reports_during_drag:
            follow_errors.append(f"Spurious report_overlay_view calls during LUT drag: {len(view_reports_during_drag)}")
        if view_at_before is not None and view_at_after != view_at_before:
            follow_errors.append(f"_overlay_view['at'] refreshed during LUT drag: {view_at_before} -> {view_at_after}")
        if laser_mutations != 0:
            follow_errors.append(f"#laser-annotations mutated {laser_mutations} time(s) during LUT drag")
        try:
            check_camera(after_cam, before_cam, description="SLACS Follow-locked live-LUT camera")
        except AssertionError as exc:
            follow_errors.append(str(exc))
        if after_frame != before_frame:
            follow_errors.append(f"Native frame moved during LUT drag: {before_frame} -> {after_frame}")
        if after_target != before_target:
            follow_errors.append(f"Laser calibration changed during LUT drag: {before_target} -> {after_target}")
        if laser_prep_reason is not None or laser_prep_payload is None:
            follow_errors.append(f"Laser-box prep failed after LUT drag: reason={laser_prep_reason}")

        report["checks"]["follow_locked_live_lut"] = {
            "ok": not follow_errors,
            "main_intensities": [round(steps[k]["main"]["mean_intensity"], 2) for k, _, _ in DRAG_TARGETS],
            "overlay_intensities": [round(steps[k]["overlay"]["mean_intensity"], 2) for k, _, _ in DRAG_TARGETS],
            "follow_after": follow_js_after,
            "stage_commands_issued": receiver_after["seq"] - receiver_before["seq"],
            "spurious_view_reports": len(view_reports_during_drag),
            "laser_annotation_mutations": laser_mutations,
            "roi_max_error_px": roi_after["maximum_corner_error_css_px"],
            "laser_prep_ok": laser_prep_reason is None and laser_prep_payload is not None,
            "error": "; ".join(follow_errors) if follow_errors else None,
        }

        # Check 2: Same-window reopen swaps launch_token, rejects old token as stale, accepts new token.
        old_uid = overlay._window.uid
        old_token = overlay.translucent_context()["launch_token"]
        reopen_snapshot = _pane_eval("captureTranslucentView()")
        reopened = api.open_translucent_viewer(sid, reopen_snapshot)
        check(reopened.get("ok") is True, f"Same-window reopen failed: {reopened}")
        new_token = reopened.get("launch_token")
        wait_for(
            lambda: child.evaluate_js(
                f"Boolean(window.nd2Translucent?.state?.ready && window.nd2Translucent.state.launchToken === {json.dumps(new_token)})"
            ),
            timeout=20,
            description="reopened overlay ready with new launch_token",
        )
        wait_for(
            lambda: child.evaluate_js("nd2Translucent.viewer.world.getItemAt(0)?.getFullyLoaded()"),
            timeout=20,
            description="reopened overlay tiles",
        )
        stale_reply = api.update_translucent_display(sid, old_token, 99, "win=0:192")
        fresh_reply = api.update_translucent_display(sid, new_token, 1, "win=0:128")
        reopen_sample = wait_for(
            lambda: (s := sample_canvas_patch(child, *CENTER, is_pane=False, allow_none=True))
            and _within(s, 127.0)
            and s,
            timeout=10,
            description="reopened overlay new launch_token intensity 127",
        )
        reopen_errors = []
        if reopened.get("id") != old_uid:
            reopen_errors.append(f"Same-window reopen created new window: {old_uid} -> {reopened.get('id')}")
        if not new_token or new_token == old_token:
            reopen_errors.append("Same-window reopen did not issue a fresh launch_token")
        if stale_reply != {"ok": False, "stale": True}:
            reopen_errors.append(f"Old launch_token was not rejected as stale: {stale_reply}")
        if fresh_reply != {"ok": True, "applied": True, "revision": 1}:
            reopen_errors.append(f"New launch_token update failed: {fresh_reply}")
        if not math.isclose(reopen_sample["mean_intensity"], 127.0, abs_tol=CAL_TOLERANCE):
            reopen_errors.append(f"New launch_token intensity mismatch: {reopen_sample}")

        report["checks"]["same_window_reopen_tokens"] = {
            "ok": not reopen_errors,
            "same_window_uid": reopened.get("id") == old_uid,
            "token_rotated": bool(new_token and new_token != old_token),
            "stale_reply": stale_reply,
            "fresh_reply": fresh_reply,
            "reopen_intensity": round(reopen_sample["mean_intensity"], 2),
            "error": "; ".join(reopen_errors) if reopen_errors else None,
        }

        report["ok"] = all(bool(item.get("ok")) for item in report["checks"].values())
        if not report["ok"]:
            failed = {k: v.get("error") for k, v in report["checks"].items() if not v.get("ok")}
            report["error"] = f"SLACS live LUT check(s) failed: {failed}"
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=3)
        write_report()
        api._close_translucent_windows()
        parent.destroy()


try:
    webview.start(run, private_mode=True)
except Exception:
    report["error"] = traceback.format_exc()
    write_report()
finally:
    if api._httpd is not None:
        api._stop_server_for_update()
    session.mark_closed()
