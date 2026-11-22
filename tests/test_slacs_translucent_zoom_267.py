"""SLACS-specific translucent fluid zoom, popover, focus, and stage-follow extraction tests."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest
from node_host import run_node

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nd2wsi" / "static"
TRANSLUCENT_JS = STATIC / "translucent-v1.js"
STAGE_FOLLOW_JS = STATIC / "translucent-stage-follow-v1.js"
GEOMETRY_JS = STATIC / "viewer-geometry-v1.js"
TRANSLUCENT_HTML = STATIC / "translucent.html"
TRANSLUCENT_CSS = STATIC / "translucent-v1.css"


SLACS_HARNESS = r"""
const path = require("node:path");
const api = require(process.argv[1]);
const importer = require(path.join(path.dirname(process.argv[1]), "astromap-import-v1.js"));

class Point {
  constructor(x, y) { this.x = x; this.y = y; }
}

function createSlacsDom() {
  const doc = {
    activeElement: null,
    _listeners: {},
    _byId: new Map(),
    addEventListener(type, fn, opts) {
      const capture = typeof opts === "boolean" ? opts : !!opts?.capture;
      (this._listeners[type] ||= []).push({ fn, capture });
    },
    getElementById(id) { return this._byId.get(id) || null; },
    querySelectorAll(selector) {
      const out = [];
      const walk = node => {
        for (const child of node.children) {
          if (matches(child, selector)) out.push(child);
          walk(child);
        }
      };
      walk(root);
      return out;
    },
    createElementNS(_ns, tag) { return makeEl(tag.toUpperCase()); },
  };

  function matchesSingle(el, sel) {
    sel = sel.trim();
    if (!sel) return false;
    if (sel.includes(" ")) {
      const parts = sel.split(/\s+/);
      const tail = parts.pop();
      if (!matchesSingle(el, tail)) return false;
      let cur = el.parentElement;
      const head = parts.join(" ");
      while (cur) {
        if (matchesSingle(cur, head)) return true;
        cur = cur.parentElement;
      }
      return false;
    }
    if (sel.startsWith("#")) return el.id === sel.slice(1);
    if (sel.startsWith(".")) return (el.className || "").split(/\s+/).includes(sel.slice(1));
    if (sel.startsWith("[") && sel.endsWith("]")) {
      const attr = sel.slice(1, -1);
      return attr in el.attributes || (attr === "data-zoom-step" && el.dataset.zoomStep !== undefined);
    }
    return (el.tagName || "").toUpperCase() === sel.toUpperCase();
  }

  function matches(el, selector) {
    return selector.split(",").some(part => matchesSingle(el, part));
  }

  function makeEl(tagName, { id = "", className = "", type = "", hidden = false, dataset = {}, attrs = {}, rect = null } = {}) {
    const el = {
      tagName: tagName.toUpperCase(),
      id,
      className,
      type,
      hidden,
      disabled: false,
      checked: false,
      value: "",
      textContent: "",
      title: "",
      style: {},
      dataset: { ...dataset },
      attributes: { ...attrs },
      children: [],
      parentElement: null,
      clientWidth: rect ? rect.width : 800,
      clientHeight: rect ? rect.height : 500,
      _rect: rect,
      focused: false,
      selected: false,
      blurCount: 0,
      _listeners: {},
      addEventListener(name, fn, opts) {
        const capture = typeof opts === "boolean" ? opts : !!opts?.capture;
        (this._listeners[name] ||= []).push({ fn, capture });
      },
      setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name.startsWith("data-")) {
          const camel = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
          this.dataset[camel] = String(value);
        }
      },
      getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; },
      removeAttribute(name) { delete this.attributes[name]; },
      appendChild(child) {
        if (child.parentElement) {
          child.parentElement.children = child.parentElement.children.filter(c => c !== child);
        }
        child.parentElement = this;
        this.children.push(child);
        return child;
      },
      replaceChildren(...nodes) {
        this.children = [];
        for (const node of nodes) this.appendChild(node);
      },
      get lastElementChild() { return this.children[this.children.length - 1] || null; },
      contains(other) {
        let cur = other;
        while (cur) {
          if (cur === this) return true;
          cur = cur.parentElement;
        }
        return false;
      },
      closest(selector) {
        let cur = this;
        while (cur) {
          if (matches(cur, selector)) return cur;
          cur = cur.parentElement;
        }
        return null;
      },
      querySelectorAll(selector) {
        const out = [];
        const walk = node => {
          for (const child of node.children) {
            if (matches(child, selector)) out.push(child);
            walk(child);
          }
        };
        walk(this);
        return out;
      },
      getBoundingClientRect() {
        if (this._rect) return { ...this._rect };
        return { left: 10, top: 20, width: this.clientWidth, height: this.clientHeight };
      },
      focus() { this.focused = true; doc.activeElement = this; },
      blur() { this.blurCount += 1; this.focused = false; if (doc.activeElement === this) doc.activeElement = null; },
      select() { this.selected = true; },
      click() { return dispatch(this, "click"); },
    };
    if (id) doc._byId.set(id, el);
    return el;
  }

  function dispatch(target, type, props = {}) {
    const event = {
      type,
      target,
      defaultPrevented: false,
      propagationStopped: false,
      immediateStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; },
      stopImmediatePropagation() { this.propagationStopped = true; this.immediateStopped = true; },
      ...props,
    };
    const path = [];
    let cur = target;
    while (cur) { path.push(cur); cur = cur.parentElement; }
    path.reverse();
    for (const entry of (doc._listeners[type] || [])) {
      if (entry.capture) {
        entry.fn(event);
        if (event.immediateStopped) return event;
      }
    }
    if (event.propagationStopped) return event;
    for (const node of path) {
      for (const entry of (node._listeners[type] || [])) {
        if (entry.capture) {
          entry.fn(event);
          if (event.immediateStopped) return event;
        }
      }
      if (event.propagationStopped) return event;
    }
    for (let i = path.length - 1; i >= 0; i--) {
      const node = path[i];
      for (const entry of (node._listeners[type] || [])) {
        if (!entry.capture) {
          entry.fn(event);
          if (event.immediateStopped) return event;
        }
      }
      if (event.propagationStopped) return event;
    }
    for (const entry of (doc._listeners[type] || [])) {
      if (!entry.capture) {
        entry.fn(event);
        if (event.immediateStopped) return event;
      }
    }
    return event;
  }

  const root = makeEl("MAIN", { id: "overlay" });
  const stage = root.appendChild(makeEl("DIV", { id: "translucent-stage" }));
  root.appendChild(makeEl("SVG", { id: "roi-overlay" }));
  root.appendChild(makeEl("SVG", { id: "laser-annotations" }));
  root.appendChild(makeEl("SVG", { id: "laser-target", hidden: true }));
  root.appendChild(makeEl("DIV", { id: "laser-target-capture", hidden: true }));
  root.appendChild(makeEl("DIV", { id: "window-drag" }));

  const zoomControl = root.appendChild(makeEl("DIV", { id: "zoom-control" }));
  const zoomBadge = zoomControl.appendChild(makeEl("BUTTON", { id: "zoom-badge", type: "button" }));
  zoomBadge.textContent = "1.000×";
  const zoomPopover = zoomControl.appendChild(makeEl("DIV", { id: "zoom-popover", hidden: true }));
  const zoomForm = zoomPopover.appendChild(makeEl("FORM", { id: "zoom-form" }));
  const zoomInput = zoomForm.appendChild(makeEl("INPUT", { id: "zoom-input", type: "text" }));
  const zoomApply = zoomForm.appendChild(makeEl("BUTTON", { id: "zoom-apply", type: "submit" }));
  const steps = zoomPopover.appendChild(makeEl("DIV", { className: "zoom-steps" }));
  const stepButtons = {};
  for (const val of ["-0.01", "-0.001", "0.001", "0.01"]) {
    const btn = makeEl("BUTTON", {
      type: "button",
      dataset: { zoomStep: val },
      attrs: { "data-zoom-step": val },
    });
    steps.appendChild(btn);
    stepButtons[val] = btn;
  }

  const windowControls = root.appendChild(makeEl("DIV", { id: "window-controls" }));
  const roiControls = windowControls.appendChild(makeEl("DIV", {
    id: "roi-controls", hidden: true, rect: { left: 620, top: 9, width: 120, height: 36 },
  }));
  roiControls.appendChild(makeEl("BUTTON", { id: "roi-previous", type: "button" }));
  roiControls.appendChild(makeEl("BUTTON", { id: "roi-index", type: "button" }));
  roiControls.appendChild(makeEl("BUTTON", { id: "roi-next", type: "button" }));
  const closeBtn = windowControls.appendChild(makeEl("BUTTON", {
    id: "overlay-close", type: "button", rect: { left: 755, top: 9, width: 36, height: 36 },
  }));
  const lockBtn = root.appendChild(makeEl("BUTTON", {
    id: "overlay-lock", type: "button", rect: { left: 18, top: 456, width: 36, height: 36 },
  }));

  const followControl = root.appendChild(makeEl("DIV", {
    id: "stage-follow-control", hidden: true, rect: { left: 12, top: 50, width: 140, height: 28 },
  }));
  const followBtn = followControl.appendChild(makeEl("BUTTON", { id: "stage-follow", type: "button" }));
  const followStatus = followControl.appendChild(makeEl("SPAN", { id: "stage-follow-status" }));
  const followOptions = followControl.appendChild(makeEl("BUTTON", { id: "stage-follow-options", type: "button" }));
  root.appendChild(makeEl("P", { id: "stage-follow-disconnected", hidden: true }));

  const followSettings = root.appendChild(makeEl("DIV", {
    id: "stage-follow-settings", hidden: true, rect: { left: 12, top: 85, width: 275, height: 210 },
  }));
  const followStepAuto = followSettings.appendChild(makeEl("INPUT", { id: "stage-follow-step-auto", type: "checkbox" }));
  followStepAuto.checked = true;
  followSettings.appendChild(makeEl("OUTPUT", { id: "stage-follow-roughness" }));
  followSettings.appendChild(makeEl("SPAN", { id: "stage-follow-step-label" }));
  followSettings.appendChild(makeEl("INPUT", { id: "stage-follow-step", type: "number" }));
  followSettings.appendChild(makeEl("SPAN", { id: "stage-follow-step-hint", hidden: true }));
  const flipUd = followSettings.appendChild(makeEl("INPUT", { id: "stage-follow-flip-ud", type: "checkbox" }));
  const flipLr = followSettings.appendChild(makeEl("INPUT", { id: "stage-follow-flip-lr", type: "checkbox" }));
  followSettings.appendChild(makeEl("SPAN", { id: "stage-follow-flip-hint", hidden: true }));
  followSettings.appendChild(makeEl("OUTPUT", { id: "laser-target-label" }));
  followSettings.appendChild(makeEl("BUTTON", { id: "laser-target-set", type: "button" }));
  followSettings.appendChild(makeEl("OUTPUT", { id: "laser-target-status" }));
  followSettings.appendChild(makeEl("OUTPUT", { id: "stage-follow-count" }));
  followSettings.appendChild(makeEl("OUTPUT", { id: "stage-follow-scale" }));
  followSettings.appendChild(makeEl("OUTPUT", { id: "stage-follow-delta" }));
  followSettings.appendChild(makeEl("BUTTON", { id: "stage-follow-save", type: "button" }));
  followSettings.appendChild(makeEl("P", { id: "stage-follow-message" }));

  const opacityControl = root.appendChild(makeEl("DIV", {
    id: "opacity-control", rect: { left: 60, top: 464, width: 190, height: 24 },
  }));
  opacityControl.appendChild(makeEl("INPUT", { id: "overlay-opacity", type: "range" }));
  opacityControl.appendChild(makeEl("OUTPUT", { id: "opacity-value" }));

  root.appendChild(makeEl("DIV", { id: "overlay-status" }));
  const review = root.appendChild(makeEl("DETAILS", {
    id: "roi-import-review", hidden: true, rect: { left: 14, top: 380, width: 240, height: 60 },
  }));
  review.appendChild(makeEl("SUMMARY", { id: "roi-import-summary" }));
  review.appendChild(makeEl("PRE", { id: "roi-import-issues" }));
  review.appendChild(makeEl("BUTTON", { id: "roi-import-repair", type: "button", hidden: true }));

  return {
    doc, root, stage, zoomControl, zoomBadge, zoomPopover, zoomForm, zoomInput, zoomApply, stepButtons,
    windowControls, roiControls, closeBtn, lockBtn, followControl, followBtn, followStatus,
    followOptions, followSettings, flipUd, flipLr, opacityControl, review, dispatch,
  };
}

function mountSlacs({ contextOverrides = {}, bridgeOverrides = {} } = {}) {
  const dom = createSlacsDom();
  const info = {
    width: 1000, height: 500, tileSize: 256, rgb: false, generation: "g1",
    pixelSizeUm: [0.5, 0.5],
    levels: [{ width: 1000, height: 500, path: "0" }, { width: 500, height: 250, path: "1" }],
  };
  const context = {
    version: 1,
    slide_url: "http://localhost:9876/token/s/abc/",
    query: "c=0&g=g1",
    center: [400, 250],
    zoom: 2,
    rotation: 0,
    flip: false,
    opacity: 0.55,
    source_format: "nd2",
    ...contextOverrides,
  };
  let width = 800, height = 500, zoom = 2.5, center = new Point(0.4, 0.25), rotation = 0, flipped = false;
  const handlers = {}, zoomCalls = [], viewReports = [], rectReports = [], nativeCalls = [], flipCalls = [];
  const viewport = {
    getContainerSize() { return new Point(width, height); },
    getCenter() { return new Point(center.x, center.y); },
    getZoom() { return zoom; },
    viewportToImageZoom(z) { return z * width / info.width; },
    imageToViewportZoom(z) { return z * info.width / width; },
    imageToViewportCoordinates(x, y) {
      return typeof x === "object"
        ? new Point(x.x / info.width, x.y / info.width)
        : new Point(x / info.width, y / info.width);
    },
    viewportToImageCoordinates(pt) { return new Point(pt.x * info.width, pt.y * info.width); },
    viewerElementToImageCoordinates(pt) {
      const scale = zoom * width / info.width, angle = rotation * Math.PI / 180;
      const dx = pt.x - width / 2, dy = pt.y - height / 2;
      return new Point(
        center.x * info.width + (dx * Math.cos(angle) + dy * Math.sin(angle)) / scale,
        center.y * info.width + (-dx * Math.sin(angle) + dy * Math.cos(angle)) / scale
      );
    },
    imageToViewerElementCoordinates(pt) {
      const scale = zoom * width / info.width, angle = rotation * Math.PI / 180;
      const dx = (pt.x - center.x * info.width) * scale, dy = (pt.y - center.y * info.width) * scale;
      return new Point(
        width / 2 + dx * Math.cos(angle) - dy * Math.sin(angle),
        height / 2 + dx * Math.sin(angle) + dy * Math.cos(angle)
      );
    },
    pointFromPixel(pt) { return new Point(pt.x / width, pt.y / width); },
    resize(size) { width = size.x; height = size.y; },
    zoomTo(value, refPoint, immediately) {
      zoom = value;
      zoomCalls.push({ value, refPoint: refPoint ? { x: refPoint.x, y: refPoint.y } : null, immediately });
      if (handlers.zoom) handlers.zoom({ zoom: value, refPoint, immediately });
    },
    panTo(value) { center = new Point(value.x, value.y); },
    setRotation(value) { rotation = value; },
    getRotation() { return rotation; },
    setFlip(value) { flipped = value; },
    getFlip() { return flipped; },
  };
  const viewer = {
    viewport, handlers, nav: true,
    addHandler(name, cb) { handlers[name] = cb; },
    forceRedraw() {},
    setMouseNavEnabled(v) { this.nav = v; },
    destroy() {},
  };
  const OSD = () => viewer;
  OSD.Point = Point;
  const timers = new Map(), intervals = new Map();
  let timerId = 0;
  const win = {
    innerWidth: 800, innerHeight: 500, screenX: 10, screenY: 20, devicePixelRatio: 1,
    addEventListener() {}, removeEventListener() {},
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, delay) { const id = ++timerId; intervals.set(id, { fn, delay }); return id; },
    clearInterval(id) { intervals.delete(id); },
  };
  const bridge = {
    async slacs_discovery_state() { return { available: true, session_id: "slacs-1", status: "running" }; },
    async start_stage_follow() { nativeCalls.push(["follow-start"]); return { ok: true, session_id: "sess-1" }; },
    async stage_follow_state() { return { enabled: true, session_id: "sess-1", seq: 0, status: "waiting", displacement_um: [0, 0] }; },
    async stop_stage_follow() { nativeCalls.push(["follow-stop"]); return { ok: true }; },
    async set_stage_follow_flips(v) { flipCalls.push(v); return { ok: true, ...v }; },
    async set_translucent_locked(v) { nativeCalls.push(["lock", v]); return { ok: true, locked: v }; },
    async set_translucent_opacity(v) { nativeCalls.push(["opacity", v]); return { ok: true, opacity: v }; },
    async close_translucent() { nativeCalls.push(["close"]); return true; },
    async choose_roi_file() { nativeCalls.push(["choose"]); return { ok: false, cancelled: true }; },
    async report_overlay_view(view) { viewReports.push(JSON.parse(JSON.stringify(view))); return { ok: true }; },
    async set_locked_interactive_rects(rects) { rectReports.push(JSON.parse(JSON.stringify(rects))); return { ok: true, count: rects.length }; },
    ...bridgeOverrides,
  };
  const mounted = api.mount({ window: win, document: dom.doc, api: bridge, OSD, context, info, importer });
  handlers.open();
  return { api, dom, viewer, mounted, win, zoomCalls, viewReports, rectReports, nativeCalls, flipCalls };
}
"""


def run_js(body: str):
    script = (
        SLACS_HARNESS
        + "\n(async () => {\n"
        + body
        + "\n})().then(v => process.stdout.write(JSON.stringify(v)))"
        ".catch(e => { console.error(e); process.exit(1); });"
    )
    result = run_node(script, TRANSLUCENT_JS, check=True, timeout=20)
    return json.loads(result.stdout)


def test_slacs_wheel_zoom_and_popover_apply_step_report_view_immediately():
    out = run_js(
        """
        const h = mountSlacs({ contextOverrides: { zoom: 2 } });
        const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
        await h.mounted.pollSlacsDiscovery();
        await settle();

        const initialCount = h.viewReports.length;
        const initialZoom = h.viewReports.at(-1).zoom;
        const initialBadge = h.dom.zoomBadge.textContent;

        // 1. Fluid wheel zoom on stage updates #zoom-badge and reports view immediately.
        h.dom.dispatch(h.dom.stage, "wheel", {
          deltaY: -100, deltaMode: 0, clientX: 210, clientY: 120,
        });
        await settle();
        const afterWheel = {
          countDelta: h.viewReports.length - initialCount,
          reportedZoom: h.viewReports.at(-1).zoom,
          cameraZoom: h.api.camera(h.viewer).zoom,
          badge: h.dom.zoomBadge.textContent,
          cornersBefore: h.viewReports[0].target_corners_px,
          cornersAfter: h.viewReports.at(-1).target_corners_px,
        };

        // Wheel over stage-follow controls/settings does not zoom the stage.
        const countBeforeFollowWheel = h.zoomCalls.length;
        h.dom.followSettings.hidden = false;
        h.dom.dispatch(h.dom.followControl, "wheel", { deltaY: -100, deltaMode: 0 });
        h.dom.dispatch(h.dom.followSettings, "wheel", { deltaY: -100, deltaMode: 0 });
        const followWheelIgnored = h.zoomCalls.length === countBeforeFollowWheel;

        // 2. Popover manual zoom submit reports view immediately and closes popover.
        h.dom.zoomBadge.click();
        h.dom.zoomInput.value = "1.375x";
        const countBeforeSubmit = h.viewReports.length;
        h.dom.dispatch(h.dom.zoomForm, "submit");
        await settle();
        const afterSubmit = {
          countDelta: h.viewReports.length - countBeforeSubmit,
          reportedZoom: h.viewReports.at(-1).zoom,
          badge: h.dom.zoomBadge.textContent,
          popoverHidden: h.dom.zoomPopover.hidden,
        };

        // 3. Popover step buttons (+0.001, +0.01) report view immediately and keep popover open.
        h.dom.zoomBadge.click();
        const countBeforeStep1 = h.viewReports.length;
        h.dom.stepButtons["0.001"].click();
        await settle();
        const afterStep1 = {
          countDelta: h.viewReports.length - countBeforeStep1,
          reportedZoom: h.viewReports.at(-1).zoom,
          badge: h.dom.zoomBadge.textContent,
          input: h.dom.zoomInput.value,
          popoverOpen: !h.dom.zoomPopover.hidden,
        };

        const countBeforeStep2 = h.viewReports.length;
        h.dom.stepButtons["0.01"].click();
        await settle();
        const afterStep2 = {
          countDelta: h.viewReports.length - countBeforeStep2,
          reportedZoom: h.viewReports.at(-1).zoom,
          badge: h.dom.zoomBadge.textContent,
          input: h.dom.zoomInput.value,
        };

        // 4. Typing inside zoom popover blocks L, arrows, Cmd/Ctrl+O; Escape closes popover first.
        h.mounted.setRois({ ok: true, name: "pair.txt", rois: [
          { x: 100, y: 100, w: 10, h: 10 }, { x: 400, y: 200, w: 10, h: 10 },
        ] });
        const roiBefore = h.mounted.state.roiIndex;
        const callsBefore = h.nativeCalls.length;
        h.dom.dispatch(h.dom.zoomInput, "keydown", { key: "l" });
        h.dom.dispatch(h.dom.zoomInput, "keydown", { key: "ArrowRight" });
        h.dom.dispatch(h.dom.zoomInput, "keydown", { key: "o", ctrlKey: true });
        const keysBlocked = {
          locked: h.mounted.state.locked,
          roiUnchanged: h.mounted.state.roiIndex === roiBefore,
          noNativeCalls: h.nativeCalls.length === callsBefore,
        };

        h.dom.dispatch(h.dom.zoomInput, "keydown", { key: "Escape" });
        const escClosedPopoverOnly = h.dom.zoomPopover.hidden &&
          h.nativeCalls.filter(c => c[0] === "close").length === 0;

        return {
          initialZoom, initialBadge, afterWheel, followWheelIgnored,
          afterSubmit, afterStep1, afterStep2, keysBlocked, escClosedPopoverOnly,
        };
        """
    )
    assert out["initialZoom"] == pytest.approx(2.0)
    assert out["initialBadge"] == "2.000×"
    assert out["afterWheel"]["countDelta"] >= 1
    assert out["afterWheel"]["reportedZoom"] == pytest.approx(2.0 * math.exp(0.05))
    assert out["afterWheel"]["reportedZoom"] == pytest.approx(out["afterWheel"]["cameraZoom"])
    assert out["afterWheel"]["badge"] == f"{2.0 * math.exp(0.05):.3f}×"
    assert out["afterWheel"]["cornersBefore"] != out["afterWheel"]["cornersAfter"]
    assert out["followWheelIgnored"] is True
    assert out["afterSubmit"] == {
        "countDelta": 1,
        "reportedZoom": pytest.approx(1.375),
        "badge": "1.375×",
        "popoverHidden": True,
    }
    assert out["afterStep1"] == {
        "countDelta": 1,
        "reportedZoom": pytest.approx(1.376),
        "badge": "1.376×",
        "input": "1.376",
        "popoverOpen": True,
    }
    assert out["afterStep2"] == {
        "countDelta": 1,
        "reportedZoom": pytest.approx(1.386),
        "badge": "1.386×",
        "input": "1.386",
    }
    assert out["keysBlocked"] == {"locked": False, "roiUnchanged": True, "noNativeCalls": True}
    assert out["escClosedPopoverOnly"] is True


def test_zoom_badge_updates_on_apply_context_and_never_collides_with_stage_follow():
    out = run_js(
        """
        const h = mountSlacs({ contextOverrides: { zoom: 2 } });
        const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
        await settle();

        h.mounted.applyContext({ zoom: 3.125, center: [300, 200], rotation: 90, flip: true });
        await settle();
        const mid = {
          badge: h.dom.zoomBadge.textContent,
          zoom: h.api.camera(h.viewer).zoom,
          reportedZoom: h.viewReports.at(-1).zoom,
          rotation: h.viewer.viewport.getRotation(),
          flip: h.viewer.viewport.getFlip(),
        };

        h.mounted.applyContext({ zoom: 0.005 });
        await settle();
        const tiny = {
          badge: h.dom.zoomBadge.textContent,
          zoom: h.api.camera(h.viewer).zoom,
        };

        h.mounted.applyContext({ zoom: 24 });
        await settle();
        const high = {
          badge: h.dom.zoomBadge.textContent,
          zoom: h.api.camera(h.viewer).zoom,
        };

        return { mid, tiny, high };
        """
    )
    assert out["mid"]["badge"] == "3.125×"
    assert out["mid"]["zoom"] == pytest.approx(3.125)
    assert out["mid"]["reportedZoom"] == pytest.approx(3.125)
    assert out["mid"]["rotation"] == 90
    assert out["mid"]["flip"] is True
    assert out["tiny"]["badge"] == "0.0050×"
    assert out["tiny"]["zoom"] == pytest.approx(0.005)
    assert out["high"]["badge"] == "24.000×"
    assert out["high"]["zoom"] == pytest.approx(24.0)

    css = TRANSLUCENT_CSS.read_text(encoding="utf-8")
    zoom_match = re.search(r"#zoom-control\s*\{([^}]+)\}", css)
    follow_match = re.search(r"#stage-follow-control\s*\{([^}]+)\}", css)
    assert zoom_match is not None and follow_match is not None
    zoom_top = int(re.search(r"top:\s*(\d+)px", zoom_match.group(1)).group(1))
    follow_top = int(re.search(r"top:\s*(\d+)px", follow_match.group(1)).group(1))
    assert zoom_top == 13
    assert follow_top == 50
    assert follow_top - zoom_top >= 32  # Clears zoom badge height cleanly.
    assert '#zoom-control[data-open="true"] { z-index: 5; }' in css
    assert '[data-has-rois="true"] #stage-follow-control { top: 82px; }' in css


def test_stage_follow_not_in_front_amber_warning_css_rule():
    css = TRANSLUCENT_CSS.read_text(encoding="utf-8")
    match = re.search(
        r'#stage-follow-status\[data-status="paused"\][^{]*\{([^}]+)\}',
        css,
    )
    assert match is not None
    selector_line = css[match.start() : match.end()]
    assert '#stage-follow-status[data-status="not-in-front"]' in selector_line
    assert '#stage-follow-status[data-status="unlocked"]' in selector_line
    assert "color: #ffd966;" in match.group(1)


def test_locked_interactive_rects_and_control_blur_on_interaction():
    out = run_js(
        """
        const h = mountSlacs({ contextOverrides: { zoom: 2 } });
        const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
        await h.mounted.pollSlacsDiscovery();
        await settle();

        // Click Follow -> blurs Follow button.
        h.dom.followBtn.focus();
        h.dom.followBtn.click();
        await settle();
        const followBlurs = h.dom.followBtn.blurCount;

        // Toggle Flip W/S and Flip A/D -> blurs both checkboxes.
        h.dom.flipUd.focus();
        h.dom.flipUd.checked = true;
        h.dom.dispatch(h.dom.flipUd, "change");
        await settle();
        h.dom.flipLr.focus();
        h.dom.flipLr.checked = true;
        h.dom.dispatch(h.dom.flipLr, "change");
        await settle();
        const flipBlurs = [h.dom.flipUd.blurCount, h.dom.flipLr.blurCount];

        // While unlocked, toggling settings or resizing does NOT spam locked rects.
        const rectsBeforeLock = h.rectReports.length;
        h.mounted.resize();
        const rectsAfterUnlockedResize = h.rectReports.length;

        // Click Lock -> blurs Lock button and reports visible locked control rects.
        h.dom.lockBtn.focus();
        h.dom.lockBtn.click();
        await settle();
        const lockBlurs = h.dom.lockBtn.blurCount;
        const lockedRects = h.rectReports.at(-1);
        const countAfterLock = h.rectReports.length;

        // Toggle Follow Settings while locked -> blurs toggle & adds settings rect.
        h.dom.followOptions.focus();
        h.dom.followOptions.click();
        await settle();
        const optionsBlurs = h.dom.followOptions.blurCount;
        const withSettingsRects = h.rectReports.at(-1);
        const countAfterSettingsOpen = h.rectReports.length;

        // Resize while locked -> re-reports updated rects.
        h.dom.stage.clientWidth = 640;
        h.dom.stage.clientHeight = 420;
        h.mounted.resize();
        const countAfterLockedResize = h.rectReports.length;

        // applyContext updating ROI visibility while locked -> includes #roi-controls rect.
        h.mounted.applyContext({ rois: [{ x: 10, y: 10, w: 5, h: 5 }], roiIndex: 0 });
        await settle();
        const withRoisRects = h.rectReports.at(-1);

        // Unlock -> sends empty [] rect list to backend.
        await h.mounted.toggleLock();
        await settle();
        const unlockedRects = h.rectReports.at(-1);

        return {
          followBlurs, flipBlurs, lockBlurs, optionsBlurs,
          rectsBeforeLock, rectsAfterUnlockedResize,
          lockedRects, withSettingsRects, withRoisRects, unlockedRects,
          countAfterLock, countAfterSettingsOpen, countAfterLockedResize,
        };
        """
    )
    assert out["followBlurs"] == 1
    assert out["flipBlurs"] == [1, 1]
    assert out["lockBlurs"] == 1
    assert out["optionsBlurs"] == 1
    assert out["rectsBeforeLock"] == 0
    assert out["rectsAfterUnlockedResize"] == 0

    # Visible when locked (before opening settings or loading ROIs):
    # overlay-lock, overlay-close, stage-follow-control, opacity-control.
    assert out["lockedRects"] == [
        {"x": 18, "y": 456, "width": 36, "height": 36},
        {"x": 755, "y": 9, "width": 36, "height": 36},
        {"x": 12, "y": 50, "width": 140, "height": 28},
        {"x": 60, "y": 464, "width": 190, "height": 24},
    ]
    assert out["countAfterSettingsOpen"] == out["countAfterLock"] + 1
    assert {"x": 12, "y": 85, "width": 275, "height": 210} in out["withSettingsRects"]
    assert out["countAfterLockedResize"] == out["countAfterSettingsOpen"] + 1
    assert {"x": 620, "y": 9, "width": 120, "height": 36} in out["withRoisRects"]
    assert out["unlockedRects"] == []


def test_slacs_translucent_html_and_module_boundaries():
    html = TRANSLUCENT_HTML.read_text(encoding="utf-8")
    css = TRANSLUCENT_CSS.read_text(encoding="utf-8")
    translucent_js = TRANSLUCENT_JS.read_text(encoding="utf-8")
    stage_follow_js = STAGE_FOLLOW_JS.read_text(encoding="utf-8")

    assert "fluorescence" not in html.lower()
    assert "materials-v1.css" not in html
    assert "materials-v1.css" not in css

    # translucent-stage-follow-v1.js must load immediately before translucent-v1.js.
    sf_tag = '<script src="translucent-stage-follow-v1.js"></script>'
    tr_tag = '<script src="translucent-v1.js"></script>'
    assert sf_tag in html and tr_tag in html
    assert html.index("viewer-geometry-v1.js") < html.index(sf_tag) < html.index(tr_tag)
    assert f"{sf_tag}\n{tr_tag}" in html

    # Neither translucent module may call raw non-flip-aware OSD coordinate APIs.
    for banned in ("imageToViewerElementCoordinates", "viewerElementToImageCoordinates", "getNumTiles"):
        assert banned not in translucent_js
        assert banned not in stage_follow_js

    # Browser UMD global check for translucent-stage-follow-v1.js.
    result = run_node(
        r"""
        const fs = require("node:fs");
        const vm = require("node:vm");
        const sandbox = {};
        sandbox.globalThis = sandbox;
        vm.createContext(sandbox);
        vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
        vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
        const mod = sandbox.Nd2TranslucentStageFollow;
        process.stdout.write(JSON.stringify({
          loaded: typeof mod === "object" && mod !== null,
          exports: Object.keys(mod).sort(),
          delta: mod.screenImageDelta([10, 0], { x: 0.5, y: 0.5 }, 0, false),
        }));
        """,
        GEOMETRY_JS,
        STAGE_FOLLOW_JS,
        check=True,
        timeout=20,
    )
    out = json.loads(result.stdout)
    assert out["loaded"] is True
    assert "mount" in out["exports"]
    assert "screenImageDelta" in out["exports"]
    assert "slacsRoughnessText" in out["exports"]
    assert out["delta"] == {"x": 20, "y": 0}
