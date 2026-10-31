"""Tracing-window canvas keys in one new private Agent window, without OS input.

Opens the example slide in a fresh Agent window, turns the main viewer to 90
degrees + flipped at a non-home zoom, opens the tracing window from the shell's
cloud button, clicks/focuses its OpenSeadragon canvas through its own webview,
waits until the window size is at rest (stage == OpenSeadragon container, inner
size stable), then dispatches DOM keydown/keypress/keyup for every OpenSeadragon
canvas key (R, Shift+R, F, 0, +, -, W/A/S/D, arrows, held Left/Right) on that
canvas and requires the tracing camera to stay exactly where it was; a window
resize is reported separately instead of being blamed on a key. A probe handler
registered after the shipped canvas-key handler proves each keydown reached
OpenSeadragon's keyboard action with preventDefaultAction already set. The
negative control dispatches the same events on a scratch OpenSeadragon viewer,
built in the same page with the tracing viewer's options but without the fix,
requires each key to move it, and requires none of its events to reach the
tracing viewer. Finally L (lock/unlock), Cmd/Ctrl+O (chooser stubbed in the page,
in the overlay API and at the native dialog call, so no dialog can open),
Left/Right ROI navigation and Escape (close, checked last) must still act. No
OS-level key or mouse event is sent.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

os.environ["ND2WSI_DISABLE_UPDATES"] = "1"
os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer Tracing Keys QA"

import webview  # noqa: E402

import nd2wsi  # noqa: E402
from nd2wsi import app  # noqa: E402
from nd2wsi.translucent_smoke import (  # noqa: E402
    bridge_result,
    camera,
    check_camera,
    click_shell_cloud,
    pane_eval,
    wait_for,
)
from nd2wsi.window_sessions import create_window_session  # noqa: E402

qa = Path(os.environ["ASTROMAP_QA_ROOT"]).resolve()
report_path = qa / os.environ.get("ASTROMAP_QA_REPORT", "tracing-keys-native-check.json")
session = create_window_session("agent", qa / "sessions")
api, parent = app.create_app_window(None, window_session=session)
report = {"ok": False, "module": nd2wsi.__file__, "session": session.as_dict(),
          "scope": "tracing-window-openseadragon-keys"}

# (label, key, code, keyCode, shift, repeat, field OSD changes without the fix)
BLOCKED = [dict(zip(("label", "key", "code", "keyCode", "shift", "repeat", "expect"), row)) for row in [
    ("R rotate", "r", "KeyR", 82, False, False, "rotation"),
    ("Shift+R rotate back", "R", "KeyR", 82, True, False, "rotation"),
    ("F flip", "f", "KeyF", 70, False, False, "flip"),
    ("0 re-fit", "0", "Digit0", 48, False, False, "zoom"),
    ("+ zoom in (= key)", "=", "Equal", 187, False, False, "zoom"),
    ("+ zoom in (Shift+=)", "+", "Equal", 187, True, False, "zoom"),
    ("- zoom out", "-", "Minus", 189, False, False, "zoom"),
    ("W pan up", "w", "KeyW", 87, False, False, "center"),
    ("Shift+W zoom in", "W", "KeyW", 87, True, False, "zoom"),
    ("A pan left", "a", "KeyA", 65, False, False, "center"),
    ("S pan down", "s", "KeyS", 83, False, False, "center"),
    ("Shift+S zoom out", "S", "KeyS", 83, True, False, "zoom"),
    ("D pan right", "d", "KeyD", 68, False, False, "center"),
    ("ArrowUp pan up", "ArrowUp", "ArrowUp", 38, False, False, "center"),
    ("Shift+ArrowUp zoom in", "ArrowUp", "ArrowUp", 38, True, False, "zoom"),
    ("ArrowDown pan down", "ArrowDown", "ArrowDown", 40, False, False, "center"),
    ("Shift+ArrowDown zoom out", "ArrowDown", "ArrowDown", 40, True, False, "zoom"),
    # A held arrow repeats; the page's ROI listener ignores repeats, so these
    # are the Left/Right events that used to reach OpenSeadragon's pan.
    ("held ArrowLeft pan left", "ArrowLeft", "ArrowLeft", 37, False, True, "center"),
    ("held ArrowRight pan right", "ArrowRight", "ArrowRight", 39, False, True, "center"),
]]
KEY_L = {"label": "L", "key": "l", "code": "KeyL", "keyCode": 76}
KEY_CMD_O = {"label": "Cmd+O", "key": "o", "code": "KeyO", "keyCode": 79, "meta": True}
KEY_CTRL_O = {"label": "Ctrl+O", "key": "o", "code": "KeyO", "keyCode": 79, "ctrl": True}
KEY_RIGHT = {"label": "ArrowRight (ROI)", "key": "ArrowRight", "code": "ArrowRight", "keyCode": 39}
KEY_LEFT = {"label": "ArrowLeft (ROI)", "key": "ArrowLeft", "code": "ArrowLeft", "keyCode": 37}
KEY_ESCAPE = {"label": "Escape", "key": "Escape", "code": "Escape", "keyCode": 27}

STATE_FIELDS = ("rotation", "rotationNow", "flip", "zoom", "zoomNow", "center", "centerNow",
                "bounds", "boundsNow", "imageZoom", "imageCenter")
TARGET_FIELDS = ("rotation", "flip", "zoom", "center", "bounds")
SETTLE_SECONDS = .35  # animationTime is .15 s in the tracing viewer

PANE_READY = "!!(state.info && state.annReady && state.viewer.isOpen() && state.viewer.world.getItemCount())"

HELPERS = r"""(() => {
  if (window.__qaTracingKeys) return true;
  const OSD = window.OpenSeadragon, view = window.nd2Translucent;
  const pair = p => [p.x, p.y];
  const box = r => [r.x, r.y, r.width, r.height];
  const read = viewer => {
    const p = viewer.viewport;
    const out = {rotation: p.getRotation(), rotationNow: p.getRotation(true), flip: p.getFlip(),
      zoom: p.getZoom(), zoomNow: p.getZoom(true), center: pair(p.getCenter()),
      centerNow: pair(p.getCenter(true)), bounds: box(p.getBounds()), boundsNow: box(p.getBounds(true)),
      container: pair(p.getContainerSize())};
    if (viewer.world.getItemCount() === 1) {
      out.imageZoom = p.viewportToImageZoom(p.getZoom(true));
      out.imageCenter = pair(p.viewportToImageCoordinates(p.getCenter(true)));
    }
    return out;
  };
  // Window/stage/OpenSeadragon sizes. The page's ResizeObserver feeds the
  // stage size into the viewport, so stage === container means no resize is
  // still pending.
  const layout = () => {
    const stage = document.getElementById("translucent-stage"), size = view.viewer.viewport.getContainerSize();
    return {inner: [window.innerWidth, window.innerHeight], stage: [stage.clientWidth, stage.clientHeight],
      container: [size.x, size.y]};
  };
  const near = (a, b) => Math.abs(a - b) <= 1e-10 * Math.max(1, Math.abs(a), Math.abs(b));
  const settled = viewer => {
    const p = viewer.viewport, a = p.getCenter(), b = p.getCenter(true);
    return near(p.getZoom(), p.getZoom(true)) && near(a.x, b.x) && near(a.y, b.y) &&
      near(p.getRotation(), p.getRotation(true));
  };
  // One synthetic event path for every target: keydown, keypress for
  // printable keys, keyup, all dispatched on the given element. Legacy
  // keyCode/charCode/repeat are pinned when the engine ignores their init.
  const press = (target, spec, label) => {
    const patched = [];
    const make = (type, overrides) => {
      const want = {keyCode: spec.keyCode, charCode: 0, repeat: !!spec.repeat, ...(overrides || {})};
      const event = new KeyboardEvent(type, {key: spec.key, code: spec.code, keyCode: want.keyCode,
        which: want.keyCode, charCode: want.charCode, shiftKey: !!spec.shift, metaKey: !!spec.meta,
        ctrlKey: !!spec.ctrl, altKey: false, repeat: want.repeat, bubbles: true, cancelable: true,
        composed: true, view: window});
      for (const [name, value] of [["keyCode", want.keyCode], ["charCode", want.charCode], ["repeat", want.repeat]]) {
        if (event[name] !== value) {
          Object.defineProperty(event, name, {configurable: true, get: () => value});
          patched.push(type + "." + name);
        }
      }
      return event;
    };
    const down = make("keydown");
    const keydownNotCanceled = target.dispatchEvent(down);
    let keypress = null;
    if (spec.key.length === 1 && !spec.meta && !spec.ctrl) {
      const code = spec.key.charCodeAt(0);
      const event = make("keypress", {keyCode: code, charCode: code, repeat: false});
      keypress = {charCode: event.charCode, notCanceled: target.dispatchEvent(event)};
    }
    const up = make("keyup", {repeat: false});
    const keyupNotCanceled = target.dispatchEvent(up);
    return {target: label, isTrusted: down.isTrusted, keySeen: down.key, codeSeen: down.code,
      keyCodeSeen: down.keyCode, shiftSeen: down.shiftKey, repeatSeen: down.repeat,
      metaSeen: down.metaKey, ctrlSeen: down.ctrlKey, keydownNotCanceled,
      keydownDefaultPrevented: down.defaultPrevented, keypress, keyupNotCanceled,
      patchedLegacyFields: patched};
  };
  // Read-only probe: registered last (priority -1), it only records what
  // OpenSeadragon's keyboard action raised and what earlier handlers decided.
  const probes = {};
  const attachProbe = (viewer, name) => {
    const events = [];
    const handler = event => events.push({type: event.originalEvent?.type ?? null,
      key: event.originalEvent?.key ?? null, keyCode: event.originalEvent?.keyCode ?? null,
      preventDefaultAction: event.preventDefaultAction === true});
    const handlersBefore = viewer.numberOfHandlers("canvas-key");
    viewer.addHandler("canvas-key", handler, null, -1);
    probes[name] = {viewer, handler, events};
    return {handlersBefore, handlersAfter: viewer.numberOfHandlers("canvas-key")};
  };
  const detachProbe = name => {
    const probe = probes[name];
    if (!probe) return false;
    probe.viewer.removeHandler("canvas-key", probe.handler);
    delete probes[name];
    return true;
  };
  const probeCount = name => probes[name] ? probes[name].events.length : -1;
  const probeSince = (name, start) => probes[name] ? probes[name].events.slice(start) : null;
  const trial = (viewer, name, spec, label) => {
    const start = probeCount(name), before = read(viewer);
    const dispatch = press(viewer.canvas, spec, label);
    return {before, after: read(viewer), dispatch, probe: probeSince(name, start)};
  };
  const pressTracing = spec => {
    const start = probeCount("tracing");
    const dispatch = press(view.viewer.canvas, spec, "tracing canvas");
    return {dispatch, probe: probeSince("tracing", start)};
  };
  const click = viewer => {
    const c = viewer.canvas, r = c.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const send = type => c.dispatchEvent(new PointerEvent(type, {pointerId: 41, pointerType: "mouse",
      isPrimary: true, button: 0, buttons: type === "pointerdown" ? 1 : 0, clientX: x, clientY: y,
      bubbles: true, cancelable: true}));
    const activeBefore = document.activeElement === c;
    send("pointerdown"); send("pointerup");
    const focusedByClick = document.activeElement === c;
    if (!focusedByClick) c.focus();
    return {activeBefore, focusedByClick, focusedAfter: document.activeElement === c,
      documentHasFocus: document.hasFocus(), canvasClass: c.className, canvasTabIndex: c.tabIndex,
      point: [x, y]};
  };
  let scratch = null;
  const makeScratch = () => {
    if (scratch) throw new Error("scratch viewer already exists");
    const live = view.viewer;
    const host = document.createElement("div");
    host.id = "qa-unfixed-osd";
    host.setAttribute("aria-hidden", "true");
    Object.assign(host.style, {position: "fixed", left: "0px", top: "0px", width: "320px",
      height: "240px", opacity: "0", pointerEvents: "none", zIndex: "-1"});
    document.body.appendChild(host);
    const copied = {};
    for (const name of ["animationTime", "springStiffness", "zoomPerScroll", "zoomPerClick",
        "minZoomImageRatio", "maxZoomPixelRatio", "visibilityRatio", "constrainDuringPan",
        "pixelsPerArrowPress", "rotationIncrement", "panHorizontal", "panVertical", "autoResize",
        "preserveImageSizeOnResize", "imageSmoothingEnabled"]) copied[name] = live[name];
    copied.gestureSettingsMouse = {...live.gestureSettingsMouse};
    copied.gestureSettingsTouch = {...live.gestureSettingsTouch};
    let tileSources = null, source = "none";
    try {
      const s = live.world.getItemAt(0).source;
      tileSources = {width: s.dimensions.x, height: s.dimensions.y, tileSize: s.getTileWidth(s.maxLevel),
        tileOverlap: 0, minLevel: s.minLevel, maxLevel: s.maxLevel,
        getLevelScale: level => s.getLevelScale(level), getNumTiles: level => s.getNumTiles(level),
        getTileUrl: (level, x, y) => s.getTileUrl(level, x, y)};
      source = "tracing image tiles (delegated tile source)";
    } catch (error) { source = "none (" + error.message + ")"; }
    const options = {...copied, element: host, showNavigationControl: false, showNavigator: false,
      showSequenceControl: false, prefixUrl: "", drawer: "canvas", crossOriginPolicy: false,
      imageLoaderLimit: 2, maxImageCacheCount: 30};
    if (tileSources) options.tileSources = tileSources;
    const viewer = OSD(options);
    scratch = {viewer, host, failed: null};
    viewer.addHandler("open-failed", event => { if (scratch) scratch.failed = String(event?.message || "open failed"); });
    return {options: copied, tileSource: source, canvasKeyHandlers: viewer.numberOfHandlers("canvas-key"),
      mouseNav: viewer.isMouseNavEnabled(), osdVersion: OSD.version.versionStr,
      sameOpenSeadragonAsTracing: Object.getPrototypeOf(viewer) === Object.getPrototypeOf(live)};
  };
  const scratchViewer = () => {
    if (!scratch) throw new Error("scratch viewer is missing");
    return scratch.viewer;
  };
  const scratchStatus = () => scratch ? {opened: scratch.viewer.world.getItemCount() > 0, failed: scratch.failed} : null;
  const controlTrial = (spec, rotation, flip) => {
    // Same non-home starting camera before every key, so each key's own
    // effect is measured from a known state.
    const v = scratch.viewer, p = v.viewport;
    p.setRotation(rotation, true); p.setFlip(flip);
    const home = p.getHomeBoundsNoRotate ? p.getHomeBoundsNoRotate() : p.getHomeBounds();
    p.zoomTo(p.getHomeZoom() * 2.5, null, true);
    p.panTo(new OSD.Point(home.x + home.width * .4, home.y + home.height * .4), true);
    v.forceRedraw();
    return trial(v, "scratch", spec, "unfixed scratch canvas");
  };
  const destroyScratch = () => {
    if (!scratch) return false;
    detachProbe("scratch");
    scratch.viewer.destroy();
    scratch.host.remove();
    scratch = null;
    return !document.getElementById("qa-unfixed-osd");
  };
  // Replace only the native file chooser behind Cmd/Ctrl+O, so the key path
  // runs without opening a dialog in this window.
  let chooser = null;
  const stubChooser = () => {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || typeof bridge.choose_roi_file !== "function") return {installed: false, reason: "no bridge function"};
    const original = bridge.choose_roi_file, calls = [];
    const stub = function () { calls.push(Date.now()); return Promise.resolve({ok: false, cancelled: true}); };
    try { bridge.choose_roi_file = stub; } catch (error) { return {installed: false, reason: String(error)}; }
    if (bridge.choose_roi_file !== stub) return {installed: false, reason: "bridge function is read-only"};
    chooser = {bridge, original, stub, calls};
    return {installed: true};
  };
  const chooserCalls = () => chooser ? chooser.calls.length : -1;
  const restoreChooser = () => {
    if (chooser && chooser.bridge.choose_roi_file === chooser.stub) chooser.bridge.choose_roi_file = chooser.original;
    chooser = null;
    return true;
  };
  window.__qaTracingKeys = {read, layout, settled, press, pressTracing, attachProbe, detachProbe, probeCount,
    probeSince, trial, click, makeScratch, scratchViewer, scratchStatus, controlTrial, destroyScratch, stubChooser,
    chooserCalls, restoreChooser};
  return true;
})()"""

LOCK_STATE = """(() => {
  const s=nd2Translucent.state, lock=document.getElementById('overlay-lock');
  return {locked:s.locked, pending:s.pending, ariaPressed:lock.getAttribute('aria-pressed'),
    overlayLocked:document.getElementById('overlay').dataset.locked,
    mouseNav:nd2Translucent.viewer.isMouseNavEnabled(),
    opacityDisabled:document.getElementById('overlay-opacity').disabled};
})()"""

ROI_STATE = """({index:nd2Translucent.state.roiIndex, count:nd2Translucent.state.rois.length,
  label:document.getElementById('roi-index').textContent})"""


def check(value, message):
    if not value:
        raise AssertionError(message)


def write_report():
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    try:
        exported = session.exports_root / "tracing-keys-native-report.json"
        exported.write_text(payload, encoding="utf-8")
        report["exports_report"] = str(exported)
        payload = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    except OSError as exc:
        report["exports_report_error"] = str(exc)
    report_path.write_text(payload, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, default=str), flush=True)


def camera_matches(actual, expected, **options):
    """check_camera as a measured value: (True, None) or (False, its message)."""
    try:
        check_camera(actual, expected, **options)
    except AssertionError as exc:
        return False, str(exc)
    return True, None


def differs(a, b):
    if a is None or b is None:
        return a is not b
    return abs(a - b) > 1e-9 * max(1.0, abs(a), abs(b))


def changed_fields(before, after, fields=STATE_FIELDS):
    changed = []
    for name in fields:
        a, b = before.get(name), after.get(name)
        if a is None and b is None:
            continue
        if isinstance(a, bool) or isinstance(b, bool):
            moved = a != b
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            moved = any(differs(x, y) for x, y in zip(a, b))
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            moved = differs(a, b)
        else:
            moved = a != b
        if moved:
            changed.append(name)
    return changed


def same_orientation(a, b):
    return abs(((a["rotation"] - b["rotation"] + 180) % 360) - 180) < .01 and a["flip"] == b["flip"]


def main_orientation():
    return pane_eval(parent, "({rotation:state.viewer.viewport.getRotation(),flip:state.viewer.viewport.getFlip()})")


def open_example_slide():
    source = Path(os.environ.get("ASTROMAP_QA_INPUT", str(qa.parent / "docs/example_tissue.nd2")))
    report["input"] = {"path": str(source), "from_env": "ASTROMAP_QA_INPUT" in os.environ,
                       "exists": source.is_file()}
    url = api.open_path(str(source))
    check(url, f"open failed: {api._status}")
    report["url"] = url
    parent.load_url(url)
    wait_for(lambda: pane_eval(parent, PANE_READY), timeout=180, description="example slide in the Agent window")
    # The tracing window must inherit a non-trivial orientation and a non-home
    # view, so R/F/0 would visibly change it if they acted.
    pane_eval(parent, """(() => {
      state.viewportRelay.displayRotation=90; state.viewportRelay.displayFlipped=true;
      applyDesiredDisplayTransform();
      const p=state.viewer.viewport, size=state.viewer.world.getItemAt(0).getContentSize();
      p.zoomTo(p.imageToViewportZoom(.8),null,true);
      p.panTo(p.imageToViewportCoordinates(size.x*.42,size.y*.47),true);
      return true;
    })()""")
    wait_for(lambda: pane_eval(parent, "Math.abs(state.viewer.viewport.getRotation()-90)<.01 && "
                                       "state.viewer.viewport.getFlip()===true"),
             description="main viewer at 90 degrees, flipped")
    view = pane_eval(parent, "captureTranslucentView()")
    check(abs(view["rotation"] - 90) < .01 and view["flip"] is True, f"Main viewer orientation not set: {view}")
    report["main_view_before_tracing"] = view
    return view


def open_tracing_window(source_view):
    check(not api._translucent_windows, "Check requires an Agent window without tracing windows")
    click_shell_cloud(parent)
    overlay = wait_for(lambda: next(iter(api._translucent_windows.values()), None), description="tracing window")
    child = overlay._window
    wait_for(lambda: child.evaluate_js("!!window.nd2Translucent?.state.ready"), description="tracing image")
    wait_for(lambda: overlay._configured, description="native tracing window setup")
    handoff = camera(child)
    check_camera(handoff, source_view, description="tracing handoff")
    check(same_orientation(handoff, source_view), f"Tracing window orientation differs: {handoff} vs {source_view}")
    report["tracing_handoff"] = {"camera": handoff, "orientation_matches_main": True}
    return overlay, child


def blocked_key_checks(child, source_view):
    js = "window.__qaTracingKeys"
    pre = child.evaluate_js("""({locked:nd2Translucent.state.locked, pending:nd2Translucent.state.pending,
      mouseNav:nd2Translucent.viewer.isMouseNavEnabled(), rois:nd2Translucent.state.rois.length,
      canvasKeyHandlers:nd2Translucent.viewer.numberOfHandlers('canvas-key'),
      osdVersion:OpenSeadragon.version.versionStr, userAgent:navigator.userAgent})""")
    report["tracing_preconditions"] = pre
    check(not pre["locked"] and not pre["pending"] and pre["mouseNav"],
          f"Tracing window must start unlocked with OpenSeadragon input enabled: {pre}")
    check(pre["canvasKeyHandlers"] == 1, f"Expected exactly the shipped canvas-key handler: {pre}")
    check(child.evaluate_js(HELPERS), "Could not install the page-local key helpers")
    report["tracing_probe"] = child.evaluate_js(f"{js}.attachProbe(nd2Translucent.viewer,'tracing')")
    before_click = child.evaluate_js(f"{js}.read(nd2Translucent.viewer)")
    report["canvas_click_focus"] = child.evaluate_js(f"{js}.click(nd2Translucent.viewer)")
    wait_for(lambda: child.evaluate_js(f"{js}.settled(nd2Translucent.viewer)"), description="tracing camera at rest")
    time.sleep(SETTLE_SECONDS)
    # A late native resize (window setup, display change) moves bounds/zoom in
    # viewport units through resizePreservingScale. Take the baseline only
    # once no resize is pending and the window size has held for 0.3 s.
    samples = report["tracing_layout_samples"] = []

    def stable_layout():
        first = child.evaluate_js(f"{js}.layout()")
        time.sleep(.3)
        second = child.evaluate_js(f"{js}.layout()")
        samples.append([first, second])
        del samples[:-4]
        return second if first == second and second["stage"] == second["container"] else None

    layout = wait_for(stable_layout, timeout=15,
                      description="tracing window size at rest (stage == OpenSeadragon container, stable 0.3 s)")
    baseline = child.evaluate_js(f"{js}.read(nd2Translucent.viewer)")
    report["tracing_state_before_click"] = before_click
    report["tracing_layout_at_baseline"] = layout
    report["tracing_baseline"] = baseline
    check(baseline["container"] == layout["container"], f"Tracing window resized while taking the baseline: "
          f"{layout} vs {baseline['container']}")
    check(same_orientation(baseline, source_view), "Clicking the canvas changed the tracing orientation")
    entries = report["blocked_keys"] = []
    for spec in BLOCKED:
        result = child.evaluate_js(f"{js}.trial(nd2Translucent.viewer,'tracing',{json.dumps(spec)},'tracing canvas')")
        time.sleep(SETTLE_SECONDS)
        settled = child.evaluate_js(f"{js}.read(nd2Translucent.viewer)")
        settled_layout = child.evaluate_js(f"{js}.layout()")
        probe = result["probe"] or []
        dispatch = result["dispatch"]
        entry = {**spec, "dispatch": dispatch, "canvas_key_events": probe,
                 "state_immediately_after": result["after"], "state_after_settle": settled,
                 "layout_after_settle": settled_layout,
                 "changed_immediately": changed_fields(baseline, result["after"]),
                 "changed_after_settle": changed_fields(baseline, settled)}
        # No OpenSeadragon key resizes the viewport, so a container change can
        # only be the window itself; it is reported apart from key effects.
        entry["window_resized"] = (result["after"]["container"] != baseline["container"]
                                   or settled["container"] != baseline["container"]
                                   or settled_layout != layout)
        entry["event_arrived_as_specified"] = (dispatch["keyCodeSeen"] == spec["keyCode"]
                                               and dispatch["shiftSeen"] == spec["shift"]
                                               and dispatch["repeatSeen"] == spec["repeat"])
        entry["reached_osd_key_action"] = (len(probe) == 1 and probe[0]["type"] == "keydown"
                                           and probe[0]["keyCode"] == spec["keyCode"])
        entry["shipped_handler_cancelled_action"] = bool(probe) and all(e["preventDefaultAction"] for e in probe)
        entry["unchanged"] = not entry["changed_immediately"] and not entry["changed_after_settle"]
        entry["passed"] = all(entry[name] for name in ("event_arrived_as_specified", "reached_osd_key_action",
                                                         "shipped_handler_cancelled_action", "unchanged"))
        entries.append(entry)
    failed = [entry["label"] for entry in entries if not entry["passed"]]
    resized = [entry["label"] for entry in entries if entry["window_resized"]]
    final = child.evaluate_js(f"{js}.read(nd2Translucent.viewer)")
    report["blocked_summary"] = {"keys": len(entries), "passed": len(entries) - len(failed), "failed": failed,
                                 "window_resized_during": resized,
                                 "tracing_final_state": final,
                                 "tracing_final_changed": changed_fields(baseline, final),
                                 "main_orientation_after": main_orientation()}
    check(not resized and final["container"] == baseline["container"],
          f"Tracing window resized during the run (not a key action; rerun): after {resized}, "
          f"container {baseline['container']} -> {final['container']}")
    check(not failed, f"OpenSeadragon keys acted on, or did not reach, the tracing viewer: {failed}")
    check(not changed_fields(baseline, final), "Tracing camera drifted during the blocked-key pass")
    check(same_orientation(report["blocked_summary"]["main_orientation_after"], source_view),
          "Main viewer orientation changed during the tracing-key pass")
    return baseline


def negative_control(child, baseline):
    """The same events must move an OpenSeadragon viewer without the fix."""
    js = "window.__qaTracingKeys"
    control = report["negative_control"] = {
        "viewer": "scratch OpenSeadragon viewer in the tracing page, options copied from the tracing viewer, "
                  "no canvas-key handler"}
    try:
        control.update(child.evaluate_js(f"{js}.makeScratch()"))
        check(control["canvasKeyHandlers"] == 0 and control["mouseNav"],
              f"Scratch viewer is not an unfixed, input-enabled viewer: {control}")
        try:
            control["open"] = wait_for(lambda: (lambda s: s if s and (s["opened"] or s["failed"]) else None)(
                child.evaluate_js(f"{js}.scratchStatus()")), timeout=20, description="scratch viewer image")
        except TimeoutError as exc:
            control["open"] = {"opened": False, "failed": str(exc)}
        control["probe"] = child.evaluate_js(f"{js}.attachProbe({js}.scratchViewer(),'scratch')")
        rotation, flip = baseline["rotation"], baseline["flip"]
        entries = control["entries"] = []
        for spec in BLOCKED:
            result = child.evaluate_js(f"{js}.controlTrial({json.dumps(spec)},{json.dumps(rotation)},{json.dumps(flip)})")
            probe = result["probe"] or []
            changed = changed_fields(result["before"], result["after"], TARGET_FIELDS)
            entry = {"label": spec["label"], "expect": spec["expect"], "dispatch": result["dispatch"],
                     "canvas_key_events": probe, "state_before": result["before"],
                     "state_immediately_after": result["after"], "changed": changed,
                     "reached_osd_key_action": (len(probe) == 1 and probe[0]["keyCode"] == spec["keyCode"]
                                                and not probe[0]["preventDefaultAction"]),
                     "moved": spec["expect"] in changed}
            entries.append(entry)
        not_moved = [entry["label"] for entry in entries if not (entry["moved"] and entry["reached_osd_key_action"])]
        control["summary"] = {"keys": len(entries), "moved": len(entries) - len(not_moved), "not_moved": not_moved}
        check(not not_moved, f"Synthetic keys did not move the unfixed viewer, so the check could pass vacuously: {not_moved}")
        control["ok"] = True
    finally:
        try:
            control["scratch_removed"] = child.evaluate_js(f"{js}.destroyScratch()")
        except Exception as exc:
            control["scratch_remove_error"] = str(exc)


def allowed_key_checks(child, overlay, source_view, source_annotations):
    js = "window.__qaTracingKeys"
    allowed = report["allowed_keys"] = {}

    def press(spec):
        return child.evaluate_js(f"{js}.pressTracing({json.dumps(spec)})")

    # L locks, then unlocks, the tracing window.
    for step, locked in (("L_lock", True), ("L_unlock", False)):
        before, state_before = camera(child), child.evaluate_js(LOCK_STATE)
        sent = press(KEY_L)
        wait_for(lambda: child.evaluate_js("nd2Translucent.state.locked===" + json.dumps(locked)
                                           + " && !nd2Translucent.state.pending") and overlay._locked is locked,
                 description=f"L {'locks' if locked else 'unlocks'} the tracing window")
        state_after, after = child.evaluate_js(LOCK_STATE), camera(child)
        allowed[step] = {"dispatch": sent["dispatch"], "canvas_key_events": sent["probe"],
                         "state_before": state_before, "state_after": state_after,
                         "native_locked_after": overlay._locked, "camera_before": camera_summary(before),
                         "camera_after": camera_summary(after)}
        check(state_after["locked"] is locked and state_after["ariaPressed"] == json.dumps(locked)
              and state_after["mouseNav"] is (not locked), f"L did not {step}: {state_after}")
        check_camera(after, before, description=step)
        check(same_orientation(after, source_view), f"{step} changed the orientation")

    # Cmd+O and Ctrl+O reach the chooser; the native dialog is replaced in
    # the page bridge and this overlay's Python API, and the dialog call itself
    # is shadowed as a safety net, so no dialog can open even if both stubs
    # were bypassed (the real choose_roi_file then returns cancelled).
    python_calls, dialog_calls = [], []

    def python_chooser():
        python_calls.append(time.time())
        return {"ok": False, "cancelled": True}

    def no_dialog(*_args, **_kwargs):
        dialog_calls.append(time.time())
        return None

    overlay.choose_roi_file = python_chooser
    overlay._window.create_file_dialog = no_dialog
    try:
        stub = child.evaluate_js(f"{js}.stubChooser()")
        page_stub = bool(stub and stub.get("installed"))
        steps = []
        # Recorded up front so a timeout still leaves the per-key evidence.
        record = allowed["Cmd_Ctrl_O"] = {"page_stub": stub, "chooser_reached_via": "page bridge stub" if page_stub
                                          else "overlay Python API stub", "steps": steps}
        before, rois_before = camera(child), child.evaluate_js("JSON.stringify(nd2Translucent.state.rois)")
        for spec in (KEY_CMD_O, KEY_CTRL_O):
            page_before, python_before = child.evaluate_js(f"{js}.chooserCalls()"), len(python_calls)

            def reached_chooser():
                if child.evaluate_js("nd2Translucent.state.loadingRois"):
                    return False
                if page_stub:
                    return child.evaluate_js(f"{js}.chooserCalls()") == page_before + 1
                return len(python_calls) == python_before + 1

            sent = press(spec)
            step = {"key": spec["label"], "dispatch": sent["dispatch"], "canvas_key_events": sent["probe"],
                    "page_stub_calls_before": page_before, "python_stub_calls_before": python_before}
            steps.append(step)
            try:
                wait_for(reached_chooser, timeout=10, description=f"{spec['label']} reaches the ROI chooser")
            finally:
                try:
                    step["page_stub_calls"] = child.evaluate_js(f"{js}.chooserCalls()")
                except Exception as exc:
                    step["page_stub_calls_error"] = str(exc)
                step.update(python_stub_calls=len(python_calls), dialog_safety_net_calls=len(dialog_calls))
        # Measure first, record what was measured, then judge it.
        after = camera(child)
        rois_after = child.evaluate_js("JSON.stringify(nd2Translucent.state.rois)")
        camera_unchanged, camera_error = camera_matches(after, before, description="Cmd/Ctrl+O")
        record.update({"camera_before": camera_summary(before), "camera_after": camera_summary(after),
                       "camera_unchanged": camera_unchanged, "camera_error": camera_error,
                       "rois_before": rois_before, "rois_after": rois_after,
                       "rois_after_equal": rois_after == rois_before,
                       "dialog_safety_net_calls": len(dialog_calls)})
        check(camera_unchanged, camera_error)
        check(rois_after == rois_before, "Cancelled chooser changed the ROI list")
    finally:
        try:
            child.evaluate_js(f"{js}.restoreChooser()")
        finally:
            overlay.__dict__.pop("choose_roi_file", None)
            overlay._window.__dict__.pop("create_file_dialog", None)

    # Left/Right still step through ROIs, keeping the scale and orientation.
    size = child.evaluate_js("(() => {const s=nd2Translucent.viewer.world.getItemAt(0).getContentSize();return [s.x,s.y];})()")
    width, height = size
    rois = [{"x": round(width * .30), "y": round(height * .30), "w": max(4, round(width * .04)),
             "h": max(4, round(height * .04)), "index": 1, "label": "QA key ROI 1"},
            {"x": round(width * .58), "y": round(height * .55), "w": max(4, round(width * .05)),
             "h": max(4, round(height * .03)), "index": 2, "label": "QA key ROI 2"}]
    payload = {"ok": True, "name": "QA tracing-key ROIs (overlay only)", "rois": rois}
    check(child.evaluate_js("nd2Translucent.setRois(" + json.dumps(payload) + ",{pan:false,index:0})"),
          "Tracing window rejected the overlay-only diagnostic ROIs")
    roi_steps = []
    for spec, index in ((KEY_RIGHT, 1), (KEY_LEFT, 0)):
        before, roi_before = camera(child), child.evaluate_js(ROI_STATE)
        sent = press(spec)
        after, roi_after = camera(child), child.evaluate_js(ROI_STATE)
        time.sleep(SETTLE_SECONDS)
        settled = camera(child)
        roi = rois[index]
        target = [roi["x"] + roi["w"] / 2, roi["y"] + roi["h"] / 2]
        error = max(abs(a - b) for a, b in zip(settled["center"], target))
        roi_steps.append({"key": spec["label"], "dispatch": sent["dispatch"], "canvas_key_events": sent["probe"],
                          "roi_before": roi_before, "roi_after": roi_after,
                          "camera_before": camera_summary(before), "camera_after": camera_summary(after),
                          "camera_after_settle": camera_summary(settled), "expected_center": target,
                          "center_error_px": error})
        check(roi_after["index"] == index, f"{spec['label']} did not select ROI {index + 1}: {roi_after}")
        check(error < .01, f"{spec['label']} did not center ROI {index + 1}: {settled['center']} vs {target}")
        check_camera(settled, before, center=False, description=spec["label"])
        check(same_orientation(settled, source_view), f"{spec['label']} changed the orientation")
    allowed["Left_Right_ROI"] = {"rois": rois, "steps": roi_steps}
    check(pane_eval(parent, "JSON.stringify(state.annotations)") == source_annotations,
          "Tracing keys changed the main viewer's annotations")
    allowed["source_annotations_unchanged"] = True
    allowed["main_orientation_before_escape"] = main_orientation()
    check(same_orientation(allowed["main_orientation_before_escape"], source_view),
          "Main viewer orientation changed during the allowed-key pass")

    # Escape closes the tracing window; checked last, after all evidence.
    child.evaluate_js(f"{js}.detachProbe('tracing')")
    sent = press(KEY_ESCAPE)
    wait_for(lambda: overlay._closed and not api._translucent_windows, description="Escape closes the tracing window")
    allowed["Escape_close"] = {"dispatch": sent["dispatch"], "overlay_closed": overlay._closed,
                               "tracing_windows_left": len(api._translucent_windows)}


def camera_summary(view):
    return {key: view[key] for key in ("center", "zoom", "rotation", "flip")}


def run():
    try:
        context = api.window_context()
        check(context["role"] == "agent" and context["id"] == session.id, "Wrong Agent window identity")
        wait_for(lambda: parent.evaluate_js("!!window.pywebview?.api"), description="parent bridge")
        bridged = bridge_result(parent, "pywebview.api.window_context()")
        check(bridged["role"] == "agent" and bridged["id"] == session.id, "Parent bridge identity mismatch")
        report["bridge_identity_verified"] = True
        parent.show()
        source_view = open_example_slide()
        source_annotations = pane_eval(parent, "JSON.stringify(state.annotations)")
        overlay, child = open_tracing_window(source_view)
        baseline = blocked_key_checks(child, source_view)
        js = "window.__qaTracingKeys"
        tracing_events_before = child.evaluate_js(f"{js}.probeCount('tracing')")
        check(tracing_events_before >= 0, "Tracing probe is not attached before the scratch control")
        negative_control(child, baseline)
        after_control = child.evaluate_js(f"{js}.read(nd2Translucent.viewer)")
        tracing_events_after = child.evaluate_js(f"{js}.probeCount('tracing')")
        report["tracing_after_control"] = after_control
        report["tracing_changed_by_control"] = changed_fields(baseline, after_control)
        report["tracing_unchanged_by_control"] = not report["tracing_changed_by_control"]
        report["tracing_probe_events_during_control"] = {"before": tracing_events_before,
                                                         "after": tracing_events_after}
        check(after_control["container"] == baseline["container"],
              f"Tracing window resized during the scratch control (not a key action; rerun): "
              f"{baseline['container']} -> {after_control['container']}")
        check(tracing_events_after == tracing_events_before,
              f"Scratch-control key events reached the tracing viewer: {tracing_events_before} -> {tracing_events_after}")
        check(report["tracing_unchanged_by_control"],
              f"The scratch control moved the tracing viewer: {report['tracing_changed_by_control']}")
        allowed_key_checks(child, overlay, source_view, source_annotations)
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        try:
            pane_eval(parent, "state.viewportRelay.displayRotation=0; state.viewportRelay.displayFlipped=false; "
                              "applyDesiredDisplayTransform(); true")
            report["main_orientation_reset"] = True
        except Exception as exc:
            report["main_orientation_reset_error"] = str(exc)
        # A failed report write must not leave the GUI loop running: the
        # launcher waits for this process to exit or mark its session closed.
        try:
            write_report()
        finally:
            try:
                api._close_translucent_windows()
            finally:
                parent.destroy()


try:
    webview.start(run, private_mode=True)
except Exception:
    report["error"] = traceback.format_exc()
    write_report()
finally:
    if api._httpd is not None:
        api.stop_server_for_update()
    session.mark_closed()
