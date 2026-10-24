"""Annotation layer: delegated pointer events and translate-only pans.

The production renderer, pointer delegation and viewport refresh run in a
counting fake DOM with a rotating, flipping fake OpenSeadragon viewport.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
APP = STATIC / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SETUP = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const STATIC = process.argv[1];
const source = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');
function section(start, end) {
  const i = source.indexOf(start), j = source.indexOf(end, i);
  if (i < 0 || j < 0) throw new Error('production source not found: ' + start);
  return source.slice(i, j);
}
const production = name => section('function ' + name + '(', '\n}\n') + '\n}\n';

const counts = {elements: 0, replaceChildren: 0, listeners: 0, setAttribute: 0};
const snapshot = () => ({...counts});
const since = before => Object.fromEntries(Object.keys(counts).map(k => [k, counts[k] - before[k]]));
class FakeNode {
  constructor(tag) {
    this.tag = tag; this.attrs = new Map(); this.children = []; this.parentNode = null;
    this.listeners = new Map(); this.text = null;
  }
  setAttribute(k, v) { counts.setAttribute++; this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  adopt(node) {
    if (node.parentNode) node.parentNode.children.splice(node.parentNode.children.indexOf(node), 1);
    node.parentNode = this;
    return node;
  }
  append(...nodes) { for (const node of nodes) this.children.push(this.adopt(node)); }
  insertBefore(node, ref) {
    this.adopt(node);
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, node);
  }
  replaceChildren(...nodes) {
    counts.replaceChildren++;
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...nodes);
  }
  addEventListener(kind, fn) {
    counts.listeners++;
    if (!this.listeners.has(kind)) this.listeners.set(kind, []);
    this.listeners.get(kind).push(fn);
  }
  contains(node) { for (; node; node = node.parentNode) if (node === this) return true; return false; }
  closest(selector) {
    const match = /^\[([\w-]+)\]$/.exec(selector);
    if (!match) throw new Error('unsupported selector ' + selector);
    for (let node = this; node; node = node.parentNode) if (node.attrs.has(match[1])) return node;
    return null;
  }
  set textContent(value) { this.text = String(value); }
  get textContent() { return this.text; }
}
const descendants = node => node.children.flatMap(child => [child, ...descendants(child)]);

// The pane: #stage-wrap holds #ann-layer; a listener on the wrapper sees any
// press the layer did not stop.
const wrap = new FakeNode('div'), layer = new FakeNode('svg');
wrap.append(layer);
const reachedWrap = [];
wrap.addEventListener('pointerdown', event => reachedWrap.push(event.target));
counts.listeners = 0;
const calls = [];
function press(target) {
  calls.length = 0; reachedWrap.length = 0;
  let stopped = false;
  const event = {type: 'pointerdown', target, stopPropagation() { stopped = true; }};
  for (let node = target; node && !stopped; node = node.parentNode)
    for (const fn of node.listeners.get('pointerdown') || []) fn.call(node, event);
  return {calls: calls.slice(), stopped, reachedWrap: reachedWrap.length > 0};
}

// OpenSeadragon's viewport: zoom scales about the centre, rotation turns the
// image about the centre, and itemScale stands for a world item whose size
// changes without a viewport zoom. Flip is applied by viewer-geometry-v1.js.
const OpenSeadragon = {Point: function (x, y) { this.x = x; this.y = y; }};
const camera = {};
function resetCamera() {
  Object.assign(camera, {zoom: 0.75, rotation: 0, flip: false, cx: 900, cy: 700, w: 1280, h: 800, itemScale: 1});
}
resetCamera();
const viewport = {
  getZoom: () => camera.zoom,
  getRotation: () => camera.rotation,
  getFlip: () => camera.flip,
  getContainerSize: () => new OpenSeadragon.Point(camera.w, camera.h),
  imageToViewerElementCoordinates(p) {
    const t = camera.rotation * Math.PI / 180, c = Math.cos(t), s = Math.sin(t);
    const k = camera.zoom * camera.itemScale;
    const x = (p.x - camera.cx) * k, y = (p.y - camera.cy) * k;
    return new OpenSeadragon.Point(camera.w / 2 + c * x - s * y, camera.h / 2 + s * x + c * y);
  },
};

const state = {
  viewer: {viewport}, info: {width: 4000, height: 3000, pixelSizeUm: [0.5, 0.5]},
  annotations: [], annRevision: 0, annContext: 0, annSite: null,
  astromap: {activeId: null}, editingId: null, tempLine: null,
  landmark: {points: [], pointRevision: 0}, plate: null,
};
const context = vm.createContext({
  state, OpenSeadragon, console,
  window: {
    Nd2AnnotationGeoJSON: require(path.join(STATIC, 'annotation-geojson-v1.js')),
    Nd2ViewerGeometry: require(path.join(STATIC, 'viewer-geometry-v1.js')),
  },
  document: {createElementNS(ns, tag) {
    if (ns !== 'http://www.w3.org/2000/svg') throw new Error('not SVG: ' + ns);
    counts.elements++;
    return new FakeNode(tag);
  }},
  $: id => id === 'ann-layer' ? layer : null,
  fmtUm: um => um.toFixed(2) + ' um',
  fmtInt: value => String(Math.round(value)),
  openEditor: id => calls.push(['editor', id]),
  selectAstromapRoi: (id, locate) => calls.push(['select', id, locate]),
});
vm.runInContext([
  production('pixelSize'), production('lineLengthUm'), production('lineLengthLabel'),
  production('imageToViewerElementPoint'), production('toEl'), production('astromapDisplayNote'),
  section('const SVG_NS =', '/* ---- alignment landmarks'),
  section('const LANDMARK_COLOR =', 'function landmarkHint('),
].join('\n'), context);
const c = context;
const toEl = (x, y) => c.toEl(x, y);
const root = () => layer.children[0];

const ring = [[100, 100], [240, 100], [220, 220], [100, 200], [100, 100]];
function mixed() {
  return [
    {id: 'box', type: 'box', x: 100, y: 120, w: 300, h: 200, text: 'Box note'},
    {id: 'astro', type: 'box', astromap: true, x: 600, y: 500, w: 150, h: 90, text: ''},
    {id: 'astro-2', type: 'box', astromap: true, x: 1600, y: 900, w: 120, h: 80, text: 'second'},
    {id: 'astro-off', type: 'box', astromap: true, astromapEnabled: false, x: 900, y: 900, w: 50, h: 50, text: ''},
    {id: 'pin', type: 'pin', x: 250, y: 700, text: 'Pin note'},
    {id: 7, type: 'pin', x: 1250, y: 300, text: ''},
    {id: 'line', type: 'line', x1: 50, y1: 900, x2: 450, y2: 1100, text: 'ruler'},
    {id: 'shape', type: 'geojson', text: '', geojson: {type: 'Feature', properties: {},
      geometry: {type: 'Polygon', coordinates: [ring]}}},
    {id: 'dots', type: 'geojson', text: 'Dots', geojson: {type: 'Feature', properties: {},
      geometry: {type: 'MultiPoint', coordinates: [[1500, 200], [1600, 260]]}}},
  ];
}
function resetState() {
  resetCamera();
  Object.assign(state, {
    info: {width: 4000, height: 3000, pixelSizeUm: [0.5, 0.5]},
    annotations: mixed(), annRevision: 3, annContext: 1, annSite: 2,
    astromap: {activeId: 'astro'}, editingId: null, tempLine: {x1: 300, y1: 300, x2: 700, y2: 450},
    landmark: {points: [{x: 400, y: 400}, {x: 1400, y: 1000}], pointRevision: 2}, plate: {focus: 2},
  });
}
function boxes(n) {
  return Array.from({length: n}, (_, i) => ({id: 'a' + i, type: 'box', x: (i * 37) % 3800,
    y: (i * 53) % 2800, w: 100, h: 80, text: 'Box ' + i}));
}
const translation = node => {
  const value = node.getAttribute('transform');
  if (value === null) return null;
  const match = /^translate\((\S+) (\S+)\)$/.exec(value);
  if (!match) throw new Error('unexpected transform ' + value);
  return [Number(match[1]), Number(match[2])];
};
// Every drawn mark with its screen coordinates moved by (dx, dy).
function picture(group, dx, dy) {
  const X = new Set(['x', 'cx', 'x1', 'x2']), Y = new Set(['y', 'cy', 'y1', 'y2']);
  return descendants(group).map(node => {
    const attrs = {};
    for (const [k, v] of node.attrs) {
      if (X.has(k)) attrs[k] = Number(v) + dx;
      else if (Y.has(k)) attrs[k] = Number(v) + dy;
      else if (k === 'points') attrs[k] = v.split(' ').map(pair => pair.split(',').map(Number)).map(([x, y]) => [x + dx, y + dy]);
      else if (k === 'd') attrs[k] = [...v.matchAll(/([ML])(\S+) (\S+)|Z/g)].map(m => m[1] ? [m[1], Number(m[2]) + dx, Number(m[3]) + dy] : ['Z']);
      else attrs[k] = v;
    }
    return {tag: node.tag, attrs, text: node.text};
  });
}
// Largest coordinate difference between two pictures; Infinity if anything else differs.
function pictureError(a, b) {
  let worst = 0;
  const visit = (u, v) => {
    if (typeof u === 'number' && typeof v === 'number') { worst = Math.max(worst, Math.abs(u - v)); return; }
    if (Array.isArray(u) && Array.isArray(v) && u.length === v.length) { u.forEach((x, i) => visit(x, v[i])); return; }
    if (u && v && typeof u === 'object' && typeof v === 'object') {
      const ku = Object.keys(u).sort(), kv = Object.keys(v).sort();
      if (JSON.stringify(ku) !== JSON.stringify(kv)) { worst = Infinity; return; }
      for (const k of ku) visit(u[k], v[k]);
      return;
    }
    if (u !== v) worst = Infinity;
  };
  visit(a, b);
  return worst;
}
c.wireAnnotationPointer();
"""


def run(body):
    script = SETUP + "\nconst result = (() => {\n" + body + "\n})();\nprocess.stdout.write(JSON.stringify(result));"
    result = subprocess.run([NODE, "-e", script, str(STATIC)], capture_output=True, text=True,
                            encoding="utf-8", check=True, timeout=30)
    return json.loads(result.stdout)


def test_full_renders_add_no_per_mark_listeners_and_the_layer_keeps_one():
    out = run(r"""
      resetState();
      state.annotations = boxes(500); state.tempLine = null; state.landmark.points = [];
      const before = snapshot();
      c.renderAnnotations();
      const first = since(before);
      for (let i = 0; i < 9; i++) c.renderAnnotations();
      const total = since(before), mixedBefore = snapshot();
      resetState(); c.renderAnnotations();
      const mixedRender = since(mixedBefore);
      return {first, total, mixedListeners: mixedRender.listeners,
        layerListeners: [...layer.listeners].map(([kind, fns]) => [kind, fns.length]),
        markListeners: descendants(layer).filter(node => node.listeners.size).length,
        hitIds: descendants(layer).filter(node => node.getAttribute('class') === 'hit')
          .map(node => node.getAttribute('data-ann-id'))};
    """)
    # one root group plus five nodes per box: hit group + polygon, label group + rect + text
    assert out["first"]["elements"] == 1 + 5 * 500
    assert out["first"]["listeners"] == 0 and out["first"]["replaceChildren"] == 1
    assert out["total"]["listeners"] == 0 and out["total"]["replaceChildren"] == 10
    assert out["mixedListeners"] == 0
    assert out["layerListeners"] == [["pointerdown", 1]]
    assert out["markListeners"] == 0
    assert out["hitIds"] == [
        "box", "box", "astro", "astro", "astro-2", "astro-2", "pin", "pin", "7",
        "line", "shape", "shape", "dots", "dots",
    ]


def test_delegated_pointerdown_reproduces_each_mark_kind():
    out = run(r"""
      resetState();
      state.astromap.activeId = 'astro-2';
      c.renderAnnotations();
      const nodes = descendants(root());
      const groupOf = id => nodes.filter(node => node.getAttribute('data-ann-id') === id);
      const results = {};
      for (const [id, groups] of [['box', groupOf('box')], ['astro', groupOf('astro')], ['pin', groupOf('pin')],
          ['7', groupOf('7')], ['line', groupOf('line')], ['shape', groupOf('shape')], ['dots', groupOf('dots')]]) {
        results[id] = groups.map(group => group.children.map(child => ({tag: child.tag, ...press(child)})));
      }
      const tempLabel = nodes.filter(node => node.getAttribute('class') === '');
      const landmarkGroup = nodes.find(node => node.getAttribute('class') === 'landmarks');
      const lineBody = nodes.find(node => node.tag === 'g' && node.attrs.size === 0 && node.children[0]?.tag === 'line');
      return {
        results,
        temp: tempLabel.flatMap(group => group.children.map(child => press(child))),
        landmarks: descendants(landmarkGroup).map(child => press(child)),
        lineBody: lineBody.children.map(child => press(child)),
        rootPress: press(root()), layerPress: press(layer),
        missing: (() => { const g = groupOf('box')[0]; g.setAttribute('data-ann-id', 'gone'); return press(g.children[0]); })(),
        tempCount: tempLabel.length,
      };
    """)
    def editor(value):
        return {"calls": [["editor", value]], "stopped": True, "reachedWrap": False}

    def strip(groups):
        return [[{k: v for k, v in r.items() if k != "tag"} for r in g] for g in groups]

    def tags(groups):
        return [[r["tag"] for r in g] for g in groups]

    selected = {"calls": [["select", "astro", False], ["editor", "astro"]],
                "stopped": True, "reachedWrap": False}
    results = out["results"]
    assert tags(results["box"]) == [["polygon"], ["rect", "text"]]
    assert strip(results["box"]) == [[editor("box")], [editor("box")] * 2]
    # an Astromap box and its numbered label both select the region, then open it
    assert strip(results["astro"]) == [[selected], [selected] * 2]
    assert tags(results["pin"]) == [["circle", "circle"], ["rect", "text"]]
    assert strip(results["pin"]) == [[editor("pin")] * 2, [editor("pin")] * 2]
    # a numeric id round-trips through data-ann-id and reaches the editor as itself
    assert strip(results["7"]) == [[editor(7)] * 2]
    # a measurement opens from its label only
    assert tags(results["line"]) == [["rect", "text"]]
    assert strip(results["line"]) == [[editor("line")] * 2]
    assert tags(results["shape"]) == [["path"], ["rect", "text"]]
    assert strip(results["shape"]) == [[editor("shape")], [editor("shape")] * 2]
    assert tags(results["dots"]) == [["circle", "circle"], ["rect", "text"]]
    assert strip(results["dots"]) == [[editor("dots")] * 2, [editor("dots")] * 2]
    passthrough = {"calls": [], "stopped": False, "reachedWrap": True}
    assert out["tempCount"] == 1
    assert out["temp"] == [passthrough] * 2
    assert out["landmarks"] and all(r == passthrough for r in out["landmarks"])
    assert out["lineBody"] == [passthrough] * 3
    assert out["rootPress"] == passthrough and out["layerPress"] == passthrough
    # a mark whose annotation is gone is still swallowed, as its own listener did, but opens nothing
    assert out["missing"] == {"calls": [], "stopped": True, "reachedWrap": False}


@pytest.mark.parametrize("rotation", [0, 90, 37])
@pytest.mark.parametrize("flip", [False, True])
def test_pure_pan_translates_the_root_by_the_display_delta(rotation, flip):
    out = run(r"""
      resetState();
      Object.assign(camera, {rotation: ROTATION, flip: FLIP});
      c.renderAnnotations();
      const group = root(), box = state.annotations[0];
      const corner = () => toEl(box.x + box.w, box.y + box.h);
      const start = corner(), before = snapshot();
      c.refreshAnnotationsForViewport();  // a tile redraw with no camera change
      const still = translation(group);
      camera.cx += 137.25; camera.cy -= 59.5;
      c.refreshAnnotationsForViewport();
      const first = translation(group), firstCorner = corner();
      camera.cx -= 611.125; camera.cy += 20.75;
      c.refreshAnnotationsForViewport();
      const second = translation(group), end = corner(), work = since(before);
      const moved = picture(group, ...second);
      const sameRoot = root() === group && layer.children.length === 1;
      c.renderAnnotations();
      return {still, sameRoot, work,
        firstError: Math.max(Math.abs(first[0] - (firstCorner.x - start.x)), Math.abs(first[1] - (firstCorner.y - start.y))),
        secondError: Math.max(Math.abs(second[0] - (end.x - start.x)), Math.abs(second[1] - (end.y - start.y))),
        pictureError: pictureError(moved, picture(root(), 0, 0)), marks: moved.length,
        moved: Math.hypot(...second)};
    """.replace("ROTATION", str(rotation)).replace("FLIP", json.dumps(flip)))
    assert out["sameRoot"]
    assert out["work"] == {"elements": 0, "replaceChildren": 0, "listeners": 0, "setAttribute": 3}
    assert out["still"] == [0, 0]
    assert out["firstError"] < 1e-9 and out["secondError"] < 1e-9
    assert out["moved"] > 100
    # the translated picture is the one a full render draws, mark for mark
    assert out["marks"] > 40
    assert out["pictureError"] < 1e-9


def test_every_drawn_input_change_forces_a_full_render():
    out = run(r"""
      const changes = {
        zoom: () => { camera.zoom *= 1.25; },
        rotation: () => { camera.rotation += 90; },
        'rotation step': () => { camera.rotation += 0.5; },
        flip: () => { camera.flip = !camera.flip; },
        'container width': () => { camera.w += 10; },
        'container height': () => { camera.h -= 10; },
        'image scale': () => { camera.itemScale = 2; },
        'annotation added': () => { state.annotations.push({id: 'new', type: 'pin', x: 5, y: 5, text: ''}); },
        'annotation removed': () => { state.annotations = state.annotations.filter(a => a.id !== 'pin'); },
        'annotations replaced': () => { state.annotations = state.annotations.slice(); },
        'text edited': () => { state.annotations[0].text = 'edited'; state.annRevision += 1; },
        'annRevision': () => { state.annRevision += 1; },
        'annContext': () => { state.annContext += 1; },
        'Astromap activeId': () => { state.astromap.activeId = 'astro-2'; },
        editingId: () => { state.editingId = 'box'; },
        'tempLine cleared': () => { state.tempLine = null; },
        'tempLine moved': () => { state.tempLine = {...state.tempLine, x2: 900}; },
        'landmark added': () => { state.landmark.points.push({x: 10, y: 10}); state.landmark.pointRevision += 1; },
        'landmark moved': () => { state.landmark.points[0] = {x: 11, y: 12}; state.landmark.pointRevision += 1; },
        'landmarks replaced': () => { state.landmark.points = []; },
        'plate site': () => { state.annSite = 5; },
        'plate focus': () => { state.plate.focus = null; },
        'image info': () => { state.info = {...state.info}; },
        'layer replaced': () => { layer.replaceChildren(); },
      };
      const out = {};
      for (const [name, change] of Object.entries(changes)) {
        resetState();
        c.renderAnnotations();
        camera.cx += 40; c.refreshAnnotationsForViewport();
        const panned = root(), pannedBy = translation(panned);
        change();
        const before = snapshot();
        c.refreshAnnotationsForViewport();
        const work = since(before);
        const fresh = root();
        const transform = fresh.getAttribute('transform');
        const after = snapshot();
        camera.cx -= 25; c.refreshAnnotationsForViewport();
        out[name] = {panned: pannedBy !== null && pannedBy[0] !== 0, rebuilt: work.replaceChildren === 1,
          newRoot: fresh !== panned && panned.parentNode === null, transform,
          fastAgain: since(after).elements === 0 && root() === fresh && translation(fresh) !== null};
      }
      return out;
    """)
    expected = {"panned": True, "rebuilt": True, "newRoot": True, "transform": None, "fastAgain": True}
    assert list(out) and {name: result for name, result in out.items() if result != expected} == {}


def test_direct_render_always_rebuilds_and_clears_the_pan_translation():
    out = run(r"""
      resetState();
      c.renderAnnotations();
      const old = root();
      camera.cx += 300; camera.cy += 120;
      c.refreshAnnotationsForViewport();
      const pannedBy = translation(old);
      const before = snapshot();
      c.renderAnnotations();
      const work = since(before), fresh = root();
      const again = snapshot();
      c.renderAnnotations();
      const repeat = since(again);
      c.refreshAnnotationsForViewport();
      return {pannedBy, work, repeat, detached: old.parentNode === null, newRoot: fresh !== old,
        children: layer.children.length, transform: fresh.getAttribute('transform'),
        rootAttrs: [...fresh.attrs.keys()], afterRefresh: translation(root())};
    """)
    assert out["pannedBy"] is not None and out["pannedBy"] != [0, 0]
    assert out["work"]["replaceChildren"] == 1 and out["work"]["elements"] > 40
    assert out["repeat"] == out["work"]
    assert out["detached"] and out["newRoot"] and out["children"] == 1
    assert out["transform"] is None and out["rootAttrs"] == ["data-ann-root"]
    assert out["afterRefresh"] == [0, 0]


def test_pan_frame_work_for_two_thousand_astromap_regions():
    out = run(r"""
      resetState();
      state.annotations = boxes(2000).map((a, i) => ({...a, astromap: true, astromapImportIndex: i + 1, text: String(i + 1)}));
      state.astromap.activeId = 'a0'; state.tempLine = null; state.landmark.points = [];
      let before = snapshot();
      c.renderAnnotations();
      const full = since(before);
      const frames = [];
      for (let i = 0; i < 30; i++) {
        before = snapshot();
        camera.cx += 7.5; camera.cy -= 3.25;
        c.refreshAnnotationsForViewport();
        frames.push(since(before));
      }
      return {full, frames};
    """)
    assert out["full"] == {"elements": 1 + 5 * 2000, "replaceChildren": 1, "listeners": 0,
                           "setAttribute": out["full"]["setAttribute"]}
    assert out["frames"] == [{"elements": 0, "replaceChildren": 0, "listeners": 0, "setAttribute": 1}] * 30


def test_viewport_handler_uses_the_pan_path_and_the_listener_is_installed_once():
    app = APP.read_text(encoding="utf-8")
    handler = app[app.index('viewer.addHandler("update-viewport"'):app.index('viewer.addHandler("open", renderAnnotations)')]
    assert "refreshAnnotationsForViewport();" in handler and "renderAnnotations()" not in handler
    assert len(re.findall(r"^\s*refreshAnnotationsForViewport\(\);$", app, re.M)) == 1
    init = app[app.index("async function init("):app.index("function initializeShadingCorrection(")]
    assert re.search(r"buildViewer\(\);\n\s*wireAnnotationPointer\(\);", init)
    assert len(re.findall(r"^\s*wireAnnotationPointer\(\);$", app, re.M)) == 1
    marks = app[app.index("function chip("):app.index("/* ---- annotation layer:")]
    marks += app[app.index("function renderAnnotations("):app.index("/* ---- alignment landmarks")]
    assert "addEventListener" not in marks
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    layer_rule = re.search(r"#ann-layer \{([^}]*)\}", css).group(1)
    assert "pointer-events: none" in layer_rule
    assert "#ann-layer .hit { pointer-events: auto; cursor: pointer; }" in css
