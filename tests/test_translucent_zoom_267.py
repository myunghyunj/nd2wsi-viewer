"""Fluid fine-grained translucent wheel zoom and manual numeric zoom HUD popover."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from node_host import run_node

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nd2wsi" / "static"
GEOMETRY_JS = STATIC / "viewer-geometry-v1.js"
TRANSLUCENT_JS = STATIC / "translucent-v1.js"
TRANSLUCENT_HTML = STATIC / "translucent.html"
TRANSLUCENT_CSS = STATIC / "translucent-v1.css"


VM_HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");

const geometrySrc = fs.readFileSync(process.argv[1], "utf8");
const translucentSrc = fs.readFileSync(process.argv[2], "utf8");

function loadInVm() {
  const sandbox = {
    URL,
    URLSearchParams,
    console,
    Math,
    Number,
    Array,
    String,
    Boolean,
    Promise,
    Set,
    Map,
    Error,
  };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(geometrySrc, sandbox, { filename: "viewer-geometry-v1.js" });
  // Load translucent-v1.js via CommonJS wrapper so boot() doesn't run automatically.
  const mod = { exports: {} };
  const fn = vm.runInContext(
    `(function(module, exports, require) { ${translucentSrc}\n })`,
    sandbox,
    { filename: "translucent-v1.js" }
  );
  fn(mod, mod.exports, () => sandbox.Nd2ViewerGeometry);
  return { api: mod.exports, geometry: sandbox.Nd2ViewerGeometry };
}

class Point {
  constructor(x, y) { this.x = x; this.y = y; }
}

function createDom() {
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

  function makeEl(tagName, { id = "", className = "", type = "", hidden = false, dataset = {}, attrs = {} } = {}) {
    const el = {
      tagName: tagName.toUpperCase(),
      id,
      className,
      type,
      hidden,
      disabled: false,
      value: "",
      textContent: "",
      title: "",
      style: {},
      dataset: { ...dataset },
      attributes: { ...attrs },
      children: [],
      parentElement: null,
      clientWidth: 800,
      clientHeight: 500,
      focused: false,
      selected: false,
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
      getBoundingClientRect() { return { left: 10, top: 20, width: this.clientWidth, height: this.clientHeight }; },
      focus() { this.focused = true; doc.activeElement = this; },
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
    path.reverse(); // root -> ... -> target
    // Document capture
    for (const entry of (doc._listeners[type] || [])) {
      if (entry.capture) {
        entry.fn(event);
        if (event.immediateStopped) return event;
      }
    }
    if (event.propagationStopped) return event;
    // Element capture
    for (const node of path) {
      for (const entry of (node._listeners[type] || [])) {
        if (entry.capture) {
          entry.fn(event);
          if (event.immediateStopped) return event;
        }
      }
      if (event.propagationStopped) return event;
    }
    // Element bubble (target -> root)
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
    // Document bubble
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
  const roiControls = windowControls.appendChild(makeEl("DIV", { id: "roi-controls", hidden: true }));
  roiControls.appendChild(makeEl("BUTTON", { id: "roi-previous", type: "button" }));
  roiControls.appendChild(makeEl("BUTTON", { id: "roi-index", type: "button" }));
  roiControls.appendChild(makeEl("BUTTON", { id: "roi-next", type: "button" }));
  windowControls.appendChild(makeEl("BUTTON", { id: "overlay-lock", type: "button" }));
  windowControls.appendChild(makeEl("BUTTON", { id: "overlay-close", type: "button" }));

  const opacityControl = root.appendChild(makeEl("DIV", { id: "opacity-control" }));
  opacityControl.appendChild(makeEl("INPUT", { id: "overlay-opacity", type: "range" }));
  opacityControl.appendChild(makeEl("OUTPUT", { id: "opacity-value" }));

  root.appendChild(makeEl("DIV", { id: "overlay-status" }));
  const review = root.appendChild(makeEl("DETAILS", { id: "roi-import-review", hidden: true }));
  review.appendChild(makeEl("SUMMARY", { id: "roi-import-summary" }));
  review.appendChild(makeEl("PRE", { id: "roi-import-issues" }));
  review.appendChild(makeEl("BUTTON", { id: "roi-import-repair", type: "button", hidden: true }));

  return { doc, root, stage, zoomControl, zoomBadge, zoomPopover, zoomForm, zoomInput, zoomApply, stepButtons, windowControls, dispatch };
}

function mountHarness({
  info = { width: 1000, height: 500, tileSize: 256, rgb: false, generation: "g1",
    levels: [{ width: 1000, height: 500, path: "0" }, { width: 500, height: 250, path: "1" }] },
  contextOverrides = {},
  omitViewportToImageZoom = false,
  minZoomOverride = null,
  maxZoomOverride = null,
} = {}) {
  const { api } = loadInVm();
  const dom = createDom();
  const context = {
    version: 1,
    slide_url: "http://localhost:9876/token/s/abc/",
    query: "c=0&g=g1",
    center: [400, 250],
    zoom: 2,
    rotation: 0,
    flip: false,
    opacity: 0.55,
    ...contextOverrides,
  };
  let width = 800, height = 500, zoom = 2.5, center = new Point(0.4, 0.25), rotation = 0, flipped = false;
  const zoomCalls = [];
  const pointFromPixelCalls = [];
  const handlers = {};
  const viewport = {
    getContainerSize() { return new Point(width, height); },
    getCenter(current) { return new Point(center.x, center.y); },
    getZoom(current) { return zoom; },
    imageToViewportZoom(z) { return z * info.width / width; },
    imageToViewportCoordinates(x, y) {
      return typeof x === "object"
        ? new Point(x.x / info.width, x.y / info.width)
        : new Point(x / info.width, y / info.width);
    },
    pointFromPixel(pt, current) {
      pointFromPixelCalls.push({ x: pt.x, y: pt.y, current });
      return new Point(pt.x / width, pt.y / width);
    },
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
  if (!omitViewportToImageZoom) {
    viewport.viewportToImageZoom = z => z * width / info.width;
  } else {
    delete viewport.imageToViewportZoom;
  }
  if (minZoomOverride !== null) viewport.getMinZoom = () => minZoomOverride;
  if (maxZoomOverride !== null) viewport.getMaxZoom = () => maxZoomOverride;

  const viewer = {
    viewport,
    handlers,
    nav: true,
    redraws: 0,
    addHandler(name, cb) { handlers[name] = cb; },
    forceRedraw() { this.redraws++; },
    setMouseNavEnabled(v) { this.nav = v; },
    destroy() {},
  };
  let osdOptions = null;
  const OSD = opts => { osdOptions = opts; return viewer; };
  OSD.Point = Point;
  const nativeCalls = [];
  const bridge = {
    async set_translucent_locked(v) { nativeCalls.push(["lock", v]); return { ok: true, locked: v }; },
    async set_translucent_opacity(v) { nativeCalls.push(["opacity", v]); return { ok: true, opacity: v }; },
    async close_translucent() { nativeCalls.push(["close"]); return true; },
    async choose_roi_file() { nativeCalls.push(["choose"]); return { ok: false, cancelled: true }; },
    async start_window_drag() { nativeCalls.push(["drag"]); return false; },
  };
  const win = { innerWidth: 800, innerHeight: 500, addEventListener() {}, removeEventListener() {} };
  const mounted = api.mount({ window: win, document: dom.doc, api: bridge, OSD, context, info });
  if (omitViewportToImageZoom) {
    // Set ready manually when viewportToImageZoom is intentionally omitted on a partial mock.
    zoom = context.zoom;
    mounted.state.ready = true;
    if (handlers.zoom) handlers.zoom({});
  } else {
    handlers.open();
  }
  return { api, dom, viewer, mounted, osdOptions, nativeCalls, zoomCalls, pointFromPixelCalls };
}
"""


def run_js(body: str):
    script = (
        VM_HARNESS
        + "\n(async () => {\n"
        + body
        + "\n})().then(v => process.stdout.write(JSON.stringify(v)))"
        ".catch(e => { console.error(e); process.exit(1); });"
    )
    result = run_node(script, GEOMETRY_JS, TRANSLUCENT_JS, check=True, timeout=20)
    return json.loads(result.stdout)


def test_wheel_zoom_factor_modes_modifiers_and_clamping():
    out = run_js(
        """
        const { api } = loadInVm();
        const f = api.wheelZoomFactor;
        return {
          normalIn: f({ deltaY: -100, deltaMode: 0 }),
          normalOut: f({ deltaY: 100, deltaMode: 0 }),
          shiftIn: f({ deltaY: -100, deltaMode: 0, shiftKey: true }),
          shiftAxisSwap: f({ deltaY: 0, deltaX: -100, deltaMode: 0, shiftKey: true }),
          horizNoShift: f({ deltaY: 0, deltaX: -100, deltaMode: 0, shiftKey: false }),
          altIn: f({ deltaY: -100, deltaMode: 0, altKey: true }),
          altBeatsShift: f({ deltaY: 0, deltaX: -100, deltaMode: 0, altKey: true, shiftKey: true }),
          pinchSmall: f({ deltaY: -10, deltaMode: 0, ctrlKey: true }),
          pinchNearCap: f({ deltaY: -49, deltaMode: 0, ctrlKey: true }),
          ctrlLargeFallback: f({ deltaY: -100, deltaMode: 0, ctrlKey: true }),
          lineMode: f({ deltaY: -3, deltaMode: 1 }),
          pageModeScaled: f({ deltaY: -0.1, deltaMode: 2 }, 800),
          pageModeFloor: f({ deltaY: -0.1, deltaMode: 2 }, 50),
          pageModeClamped: f({ deltaY: -1, deltaMode: 2 }, 600),
          clampedIn: f({ deltaY: -5000, deltaMode: 0 }),
          clampedOut: f({ deltaY: 5000, deltaMode: 0 }),
          zero: f({ deltaY: 0, deltaMode: 0 }),
          missing: f(null),
        };
        """
    )
    assert out["normalIn"] == pytest.approx(math.exp(0.05))
    assert out["normalOut"] == pytest.approx(math.exp(-0.05))
    assert out["normalIn"] * out["normalOut"] == pytest.approx(1.0)
    assert out["shiftIn"] == pytest.approx(math.exp(0.01))
    assert out["shiftAxisSwap"] == pytest.approx(math.exp(0.01))
    assert out["horizNoShift"] == 1.0
    assert out["altIn"] == pytest.approx(math.exp(0.002))
    assert out["altBeatsShift"] == pytest.approx(math.exp(0.002))
    assert out["pinchSmall"] == pytest.approx(math.exp(0.05))
    assert out["pinchNearCap"] == pytest.approx(math.exp(0.245))
    assert out["ctrlLargeFallback"] == pytest.approx(math.exp(0.05))
    assert out["lineMode"] == pytest.approx(math.exp(0.05))
    assert out["pageModeScaled"] == pytest.approx(math.exp(0.04))
    assert out["pageModeFloor"] == pytest.approx(math.exp(0.005))
    assert out["pageModeClamped"] == pytest.approx(math.exp(0.15))
    assert out["clampedIn"] == pytest.approx(math.exp(0.15))
    assert out["clampedOut"] == pytest.approx(math.exp(-0.15))
    assert out["zero"] == 1.0
    assert out["missing"] == 1.0


def test_parse_zoom_input_and_format_zoom_label():
    out = run_js(
        """
        const { api } = loadInVm();
        return {
          parsed: {
            plain: api.parseZoomInput("1.25"),
            spaced: api.parseZoomInput("  1.25  "),
            lowerX: api.parseZoomInput("1.25x"),
            upperX: api.parseZoomInput("1.25X"),
            timesSign: api.parseZoomInput("1.25×"),
            percent: api.parseZoomInput("125%"),
            percentSmall: api.parseZoomInput("0.5%"),
            leadingDot: api.parseZoomInput(".75"),
            number: api.parseZoomInput(2.5),
          },
          rejected: [
            "", "   ", "NaN", "Infinity", "0", "-1", "-0.5", "-50%",
            "1.25xx", "125%%", "1.25px", "abc", "1.2.3", "0x10", ".", "x", "%",
            null, undefined, 0, -2, NaN, Infinity
          ].map(v => api.parseZoomInput(v)),
          formatted: {
            one: api.formatZoomLabel(1),
            mid: api.formatZoomLabel(1.25),
            boundary: api.formatZoomLabel(0.01),
            belowBoundary: api.formatZoomLabel(0.0099),
            tiny: api.formatZoomLabel(0.0025),
            high: api.formatZoomLabel(16),
            zero: api.formatZoomLabel(0),
            neg: api.formatZoomLabel(-1),
            nan: api.formatZoomLabel(NaN),
          },
        };
        """
    )
    assert out["parsed"] == {
        "plain": 1.25,
        "spaced": 1.25,
        "lowerX": 1.25,
        "upperX": 1.25,
        "timesSign": 1.25,
        "percent": 1.25,
        "percentSmall": 0.005,
        "leadingDot": 0.75,
        "number": 2.5,
    }
    assert all(item is None for item in out["rejected"])
    assert out["formatted"] == {
        "one": "1.000×",
        "mid": "1.250×",
        "boundary": "0.010×",
        "belowBoundary": "0.0099×",
        "tiny": "0.0025×",
        "high": "16.000×",
        "zero": "—",
        "neg": "—",
        "nan": "—",
    }


def test_osd_config_disables_binary_scroll_zoom_and_raises_max_zoom_pixel_ratio():
    out = run_js(
        """
        const normal = mountHarness({ contextOverrides: { zoom: 2 } });
        const high = mountHarness({ contextOverrides: { zoom: 24 } });
        return {
          mouse: normal.osdOptions.gestureSettingsMouse,
          touch: normal.osdOptions.gestureSettingsTouch,
          normalMax: normal.osdOptions.maxZoomPixelRatio,
          highMax: high.osdOptions.maxZoomPixelRatio,
        };
        """
    )
    assert out["mouse"] == {
        "clickToZoom": False,
        "dblClickToZoom": False,
        "scrollToZoom": False,
    }
    assert out["touch"]["flickEnabled"] is False
    assert out["touch"]["pinchToZoom"] is True
    assert out["normalMax"] == 16
    assert out["highMax"] == 24


def test_stage_wheel_zoom_is_immediate_cursor_anchored_flip_aware_and_respects_exclusions():
    out = run_js(
        """
        const h = mountHarness({ contextOverrides: { zoom: 2, flip: false } });
        const { dom, viewer, mounted, zoomCalls, pointFromPixelCalls } = h;
        const before = h.api.camera(viewer).zoom;
        const evNormal = dom.dispatch(dom.stage, "wheel", {
          deltaY: -100, deltaMode: 0, clientX: 210, clientY: 120,
        });
        const afterNormal = h.api.camera(viewer).zoom;
        const normalCall = zoomCalls.at(-1);
        const normalPixel = pointFromPixelCalls.at(-1);
        const normalBadge = dom.zoomBadge.textContent;

        // Flip horizontally: cursor at clientX=210 (stage.left=10 -> elemX=200) mirrors to 800-200=600.
        viewer.viewport.setFlip(true);
        dom.dispatch(dom.stage, "wheel", {
          deltaY: 0, deltaX: -100, deltaMode: 0, shiftKey: true, clientX: 210, clientY: 120,
        });
        const afterShift = h.api.camera(viewer).zoom;
        const flippedPixel = pointFromPixelCalls.at(-1);

        // Alt ultra-fine scroll.
        dom.dispatch(dom.stage, "wheel", {
          deltaY: -100, deltaMode: 0, altKey: true, clientX: 210, clientY: 120,
        });
        const afterAlt = h.api.camera(viewer).zoom;

        // Wheel inside #zoom-popover or #window-controls does not zoom the stage.
        const countBeforeExcluded = zoomCalls.length;
        dom.dispatch(dom.zoomInput, "wheel", { deltaY: -100, deltaMode: 0 });
        dom.dispatch(dom.windowControls, "wheel", { deltaY: -100, deltaMode: 0 });
        const countAfterExcluded = zoomCalls.length;

        // Wheel while locked does nothing.
        await mounted.toggleLock();
        const lockedZoomBefore = h.api.camera(viewer).zoom;
        dom.dispatch(dom.stage, "wheel", { deltaY: -100, deltaMode: 0, clientX: 210, clientY: 120 });
        const lockedZoomAfter = h.api.camera(viewer).zoom;

        // Partial viewport mock without viewportToImageZoom still zooms safely and clamps.
        const p = mountHarness({ omitViewportToImageZoom: true, contextOverrides: { zoom: 15.5 } });
        p.dom.dispatch(p.dom.stage, "wheel", { deltaY: -300, deltaMode: 0, clientX: 110, clientY: 120 });
        const partialClamped = p.viewer.viewport.getZoom(true);

        return {
          before,
          afterNormal,
          normalPrevented: evNormal.defaultPrevented,
          normalImmediately: normalCall.immediately,
          normalPixel,
          normalBadge,
          afterShift,
          flippedPixel,
          afterAlt,
          excludedIgnored: countBeforeExcluded === countAfterExcluded,
          lockedUnchanged: lockedZoomBefore === lockedZoomAfter,
          partialClamped,
        };
        """
    )
    assert out["normalPrevented"] is True
    assert out["normalImmediately"] is True
    assert abs(out["afterNormal"] - out["before"]) > 1e-5
    assert out["afterNormal"] == pytest.approx(2.0 * math.exp(0.05))
    assert out["normalPixel"] == {"x": 200, "y": 100, "current": True}
    assert out["normalBadge"] == f"{2.0 * math.exp(0.05):.3f}×"
    assert out["afterShift"] == pytest.approx(out["afterNormal"] * math.exp(0.01))
    assert out["flippedPixel"] == {"x": 600, "y": 100, "current": True}
    assert out["afterAlt"] == pytest.approx(out["afterShift"] * math.exp(0.002))
    assert out["excludedIgnored"] is True
    assert out["lockedUnchanged"] is True
    assert out["partialClamped"] == pytest.approx(16.0)


def test_zoom_popover_submit_steppers_keyboard_isolation_and_lock_in_node_vm():
    out = run_js(
        """
        const h = mountHarness({ contextOverrides: { zoom: 2 } });
        const { dom, viewer, mounted, nativeCalls, zoomCalls } = h;
        mounted.setRois({ ok: true, name: "pair.txt", rois: [
          { x: 100, y: 100, w: 10, h: 10 },
          { x: 500, y: 200, w: 10, h: 10 },
        ] });

        const initial = {
          badge: dom.zoomBadge.textContent,
          hidden: dom.zoomPopover.hidden,
          expanded: dom.zoomBadge.getAttribute("aria-expanded"),
        };

        // Click badge -> opens popover, prefills current zoom without ×, focuses & selects input.
        dom.zoomBadge.click();
        const opened = {
          hidden: dom.zoomPopover.hidden,
          expanded: dom.zoomBadge.getAttribute("aria-expanded"),
          inputValue: dom.zoomInput.value,
          focused: dom.zoomInput.focused,
          selected: dom.zoomInput.selected,
        };

        // Typing shortcuts while focused inside #zoom-input must NOT lock, switch ROI, or open ROI picker.
        const roiBeforeKeys = mounted.state.roiIndex;
        const lockBeforeKeys = mounted.state.locked;
        const callsBeforeKeys = nativeCalls.length;
        dom.dispatch(dom.zoomInput, "keydown", { key: "l" });
        dom.dispatch(dom.zoomInput, "keydown", { key: "ArrowRight" });
        dom.dispatch(dom.zoomInput, "keydown", { key: "ArrowLeft" });
        dom.dispatch(dom.zoomInput, "keydown", { key: "o", metaKey: true });
        const shortcutsBlocked = {
          roiUnchanged: mounted.state.roiIndex === roiBeforeKeys,
          lockUnchanged: mounted.state.locked === lockBeforeKeys,
          noNativeCalls: nativeCalls.length === callsBeforeKeys,
        };

        // Invalid submission keeps popover open and marks input invalid without changing zoom.
        const zoomBeforeInvalid = h.api.camera(viewer).zoom;
        dom.zoomInput.value = "not-a-zoom";
        dom.dispatch(dom.zoomForm, "submit");
        const afterInvalid = {
          hidden: dom.zoomPopover.hidden,
          invalid: dom.zoomInput.getAttribute("aria-invalid"),
          zoomUnchanged: h.api.camera(viewer).zoom === zoomBeforeInvalid,
        };

        // Valid submission updates viewport zoom around center, updates badge, and closes popover.
        dom.zoomInput.value = "1.25x";
        dom.dispatch(dom.zoomForm, "submit");
        const submitCall = zoomCalls.at(-1);
        const afterSubmit = {
          zoom: h.api.camera(viewer).zoom,
          badge: dom.zoomBadge.textContent,
          hidden: dom.zoomPopover.hidden,
          expanded: dom.zoomBadge.getAttribute("aria-expanded"),
          immediately: submitCall.immediately,
          refCenter: submitCall.refPoint,
        };

        // Re-open and test step buttons (+0.001, -0.001, +0.01, -0.01) + ArrowUp/Down in input.
        dom.zoomBadge.click();
        dom.stepButtons["0.001"].click();
        const stepUpFine = { zoom: h.api.camera(viewer).zoom, input: dom.zoomInput.value, badge: dom.zoomBadge.textContent, open: !dom.zoomPopover.hidden };
        dom.stepButtons["0.01"].click();
        const stepUpCoarse = { zoom: h.api.camera(viewer).zoom, input: dom.zoomInput.value };
        dom.stepButtons["-0.01"].click();
        dom.stepButtons["-0.001"].click();
        const stepBack = { zoom: h.api.camera(viewer).zoom, input: dom.zoomInput.value };

        // ArrowUp (+0.001) and Shift+ArrowDown (-0.01) inside #zoom-input.
        dom.dispatch(dom.zoomInput, "keydown", { key: "ArrowUp" });
        const afterArrowUp = { zoom: h.api.camera(viewer).zoom, input: dom.zoomInput.value };
        dom.dispatch(dom.zoomInput, "keydown", { key: "ArrowDown", shiftKey: true });
        const afterShiftArrowDown = { zoom: h.api.camera(viewer).zoom, input: dom.zoomInput.value };

        // Escape closes popover FIRST without closing the window!
        const closeCountBeforeEsc = nativeCalls.filter(c => c[0] === "close").length;
        const esc1 = dom.dispatch(dom.zoomInput, "keydown", { key: "Escape" });
        const afterEsc1 = {
          hidden: dom.zoomPopover.hidden,
          prevented: esc1.defaultPrevented,
          closedWindow: nativeCalls.filter(c => c[0] === "close").length > closeCountBeforeEsc,
        };

        // Outside pointerdown closes an open popover; pointerdown inside #zoom-control excludes Alt+drag.
        dom.zoomBadge.click();
        dom.dispatch(dom.zoomControl, "pointerdown", { altKey: true, button: 0, screenX: 10, screenY: 10 });
        const altDragBlocked = mounted.state.gesture === null && !dom.zoomPopover.hidden;
        dom.dispatch(dom.stage, "pointerdown", { button: 0 });
        const closedByOutside = dom.zoomPopover.hidden;

        // Lock closes open popover and disables zoom controls.
        dom.zoomBadge.click();
        await mounted.toggleLock();
        const lockedState = {
          hidden: dom.zoomPopover.hidden,
          badgeDisabled: dom.zoomBadge.disabled,
          inputDisabled: dom.zoomInput.disabled,
          applyDisabled: dom.zoomApply.disabled,
          stepsDisabled: Object.values(dom.stepButtons).every(b => b.disabled),
        };
        dom.zoomBadge.click();
        const staysClosedWhileLocked = dom.zoomPopover.hidden;

        // Escape while popover is closed closes the window as usual.
        dom.dispatch(dom.stage, "keydown", { key: "Escape" });
        const closedOnSecondEsc = nativeCalls.filter(c => c[0] === "close").length === 1;

        return {
          initial,
          opened,
          shortcutsBlocked,
          afterInvalid,
          afterSubmit,
          stepUpFine,
          stepUpCoarse,
          stepBack,
          afterArrowUp,
          afterShiftArrowDown,
          afterEsc1,
          altDragBlocked,
          closedByOutside,
          lockedState,
          staysClosedWhileLocked,
          closedOnSecondEsc,
        };
        """
    )
    assert out["initial"] == {"badge": "2.000×", "hidden": True, "expanded": "false"}
    assert out["opened"] == {
        "hidden": False,
        "expanded": "true",
        "inputValue": "2.000",
        "focused": True,
        "selected": True,
    }
    assert out["shortcutsBlocked"] == {
        "roiUnchanged": True,
        "lockUnchanged": True,
        "noNativeCalls": True,
    }
    assert out["afterInvalid"] == {"hidden": False, "invalid": "true", "zoomUnchanged": True}
    assert out["afterSubmit"]["zoom"] == pytest.approx(1.25)
    assert out["afterSubmit"]["badge"] == "1.250×"
    assert out["afterSubmit"]["hidden"] is True
    assert out["afterSubmit"]["expanded"] == "false"
    assert out["afterSubmit"]["immediately"] is True
    assert out["afterSubmit"]["refCenter"] is not None
    assert out["stepUpFine"]["zoom"] == pytest.approx(1.251)
    assert out["stepUpFine"]["input"] == "1.251"
    assert out["stepUpFine"]["badge"] == "1.251×"
    assert out["stepUpFine"]["open"] is True
    assert out["stepUpCoarse"]["zoom"] == pytest.approx(1.261)
    assert out["stepUpCoarse"]["input"] == "1.261"
    assert out["stepBack"]["zoom"] == pytest.approx(1.25)
    assert out["stepBack"]["input"] == "1.250"
    assert out["afterArrowUp"]["zoom"] == pytest.approx(1.251)
    assert out["afterArrowUp"]["input"] == "1.251"
    assert out["afterShiftArrowDown"]["zoom"] == pytest.approx(1.241)
    assert out["afterShiftArrowDown"]["input"] == "1.241"
    assert out["afterEsc1"] == {"hidden": True, "prevented": True, "closedWindow": False}
    assert out["altDragBlocked"] is True
    assert out["closedByOutside"] is True
    assert out["lockedState"] == {
        "hidden": True,
        "badgeDisabled": True,
        "inputDisabled": True,
        "applyDisabled": True,
        "stepsDisabled": True,
    }
    assert out["staysClosedWhileLocked"] is True
    assert out["closedOnSecondEsc"] is True


def test_translucent_html_and_css_contracts_for_zoom_hud():
    html = TRANSLUCENT_HTML.read_text(encoding="utf-8")
    css = TRANSLUCENT_CSS.read_text(encoding="utf-8")

    assert "materials-v1.css" not in html
    assert "materials-v1.css" not in css
    for fragment in (
        'id="zoom-control"',
        'id="zoom-badge"',
        'aria-haspopup="dialog"',
        'aria-expanded="false"',
        'id="zoom-popover"',
        'role="dialog"',
        'id="zoom-form"',
        'id="zoom-input"',
        'inputmode="decimal"',
        'id="zoom-apply"',
        'data-zoom-step="-0.01"',
        'data-zoom-step="-0.001"',
        'data-zoom-step="0.001"',
        'data-zoom-step="0.01"',
        "Shift: fine · Alt: ultra-fine",
    ):
        assert fragment in html, f"Missing HTML fragment: {fragment}"

    for css_fragment in (
        "#zoom-control",
        "position: fixed; top: 13px; left: 19px; z-index: 4;",
        "@media (max-width: 280px)",
        '[data-has-rois="true"] #zoom-control { top: 48px; left: 12px; }',
        "font-variant-numeric: tabular-nums",
        "width: min(204px, calc(100vw - 28px))",
        "#zoom-popover[hidden] { display: none; }",
    ):
        assert css_fragment in css, f"Missing CSS fragment: {css_fragment}"
