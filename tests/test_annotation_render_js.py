"""Annotation layer: image-space outlines, delegated pointer events and cheap camera frames.

The production renderer, pointer delegation and viewport refresh run in a
counting fake DOM with a rotating, flipping fake OpenSeadragon viewport. A
picture composes every transform down to element pixels, so a repositioned
layer can be compared mark for mark with a fresh full render.
"""

import json
import re
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
APP = STATIC / "app.js"

SETUP = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const STATIC = process.argv[1];
// Windows checkouts may use CRLF; the slice markers below assume LF.
const source = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8').replace(/\r\n/g, '\n');
function section(start, end) {
  const i = source.indexOf(start), j = source.indexOf(end, i);
  if (i < 0 || j < 0) throw new Error('production source not found: ' + start);
  return source.slice(i, j);
}
const production = name => section('function ' + name + '(', '\n}\n') + '\n}\n';

const counts = {elements: 0, replaceChildren: 0, listeners: 0, setAttribute: 0, removeAttribute: 0};
const snapshot = () => ({...counts});
const since = before => Object.fromEntries(Object.keys(counts).map(k => [k, counts[k] - before[k]]));
class FakeNode {
  constructor(tag) {
    this.tag = tag; this.attrs = new Map(); this.children = []; this.parentNode = null;
    this.listeners = new Map(); this.text = null;
  }
  setAttribute(k, v) { counts.setAttribute++; this.attrs.set(k, String(v)); }
  removeAttribute(k) { counts.removeAttribute++; this.attrs.delete(k); }
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

const geo = require(path.join(STATIC, 'annotation-geojson-v1.js'));
const geoCalls = {displayParts: 0, imageParts: 0};
const geoApi = {...geo,
  displayParts(...args) { geoCalls.displayParts++; return geo.displayParts(...args); },
  imageParts(...args) { geoCalls.imageParts++; return geo.imageParts(...args); }};
const state = {
  viewer: {viewport}, info: {width: 4000, height: 3000, pixelSizeUm: [0.5, 0.5]},
  annotations: [], annRevision: 0, annContext: 0, annSite: null,
  astromap: {activeId: null}, editingId: null, tempLine: null,
  landmark: {points: [], pointRevision: 0}, plate: null,
};
const context = vm.createContext({
  state, OpenSeadragon, console,
  window: {
    Nd2AnnotationGeoJSON: geoApi,
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
const imageGroup = () => root().children[0];

const ring = [[100, 100], [240, 100], [220, 220], [100, 200], [100, 100]];
const hole = [[130, 130], [160, 130], [150, 160], [130, 130]];
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
      geometry: {type: 'Polygon', coordinates: [ring, hole]}}},
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

// Every drawn shape in element pixels, every transform composed: polygon,
// path and line vertices, circle centres and rect/text origins, plus the
// effective stroke width (a non-scaling stroke keeps its own width) and the
// linear part of the transform applied to screen-sized shapes.
function parseTransform(value) {
  if (value === null) return [1, 0, 0, 1, 0, 0];
  let m = /^translate\((\S+) (\S+)\)$/.exec(value);
  if (m) return [1, 0, 0, 1, +m[1], +m[2]];
  m = /^matrix\((\S+) (\S+) (\S+) (\S+) (\S+) (\S+)\)$/.exec(value);
  if (m) return m.slice(1).map(Number);
  throw new Error('unexpected transform ' + value);
}
const multiply = (A, B) => [A[0] * B[0] + A[2] * B[1], A[1] * B[0] + A[3] * B[1],
  A[0] * B[2] + A[2] * B[3], A[1] * B[2] + A[3] * B[3],
  A[0] * B[4] + A[2] * B[5] + A[4], A[1] * B[4] + A[3] * B[5] + A[5]];
const apply = (M, x, y) => [M[0] * x + M[2] * y + M[4], M[1] * x + M[3] * y + M[5]];
const COORDS = new Set(['transform', 'x', 'y', 'cx', 'cy', 'x1', 'y1', 'x2', 'y2', 'points', 'd', 'stroke-width', 'vector-effect']);
function picture(top = layer) {
  const out = [];
  const walk = (node, M, hit) => {
    M = multiply(M, parseTransform(node.getAttribute('transform')));
    if (node.getAttribute('class') === 'hit') hit = node.getAttribute('data-ann-id');
    const a = node.attrs, num = k => Number(a.get(k) || 0);
    if (node.tag !== 'g' && node.tag !== 'svg') {
      const record = {tag: node.tag, hit: hit || null, text: node.text, linear: M.slice(0, 4)};
      for (const [k, v] of a) if (!COORDS.has(k)) record[k] = v;
      if (a.has('stroke-width')) {
        const scale = Math.sqrt(Math.abs(M[0] * M[3] - M[1] * M[2]));
        record.stroke = [a.get('stroke'), a.get('vector-effect') === 'non-scaling-stroke' ? num('stroke-width') : num('stroke-width') * scale];
      }
      if (node.tag === 'polygon') record.points = a.get('points').split(' ').map(p => apply(M, ...p.split(',').map(Number)));
      else if (node.tag === 'path') record.points = [...a.get('d').matchAll(/([ML])(\S+) (\S+)|Z/g)].map(m => m[1] ? apply(M, +m[2], +m[3]) : 'Z');
      else if (node.tag === 'line') record.points = [apply(M, num('x1'), num('y1')), apply(M, num('x2'), num('y2'))];
      else if (node.tag === 'circle') record.points = [apply(M, num('cx'), num('cy'))];
      else record.points = [apply(M, num('x'), num('y'))];
      out.push(record);
    }
    for (const child of node.children) walk(child, M, hit);
  };
  walk(top, [1, 0, 0, 1, 0, 0], null);
  return out;
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
// What a fresh full render draws for the current camera, leaving the current
// root and annotation frame in place.
function freshPicture() {
  const keptRoot = root(), keptFrame = vm.runInContext('annotationFrame', context);
  c.renderAnnotations();
  const fresh = picture();
  layer.replaceChildren(keptRoot);
  context.keptFrame = keptFrame;
  vm.runInContext('annotationFrame = keptFrame', context);
  return fresh;
}
c.wireAnnotationPointer();
"""


def run(body):
    script = SETUP + "\nconst result = (() => {\n" + body + "\n})();\nprocess.stdout.write(JSON.stringify(result));"
    result = run_node(script, STATIC, check=True, timeout=30)
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
    # root and image groups plus five nodes per box: hit group + polygon, label group + rect + text
    assert out["first"]["elements"] == 2 + 5 * 500
    assert out["first"]["listeners"] == 0 and out["first"]["replaceChildren"] == 1
    assert out["total"]["listeners"] == 0 and out["total"]["replaceChildren"] == 10
    assert out["mixedListeners"] == 0
    assert out["layerListeners"] == [["pointerdown", 1]]
    assert out["markListeners"] == 0
    # outlines first (the image group), then the screen-sized marks in annotation order
    assert out["hitIds"] == [
        "box", "astro", "astro-2", "shape",
        "box", "astro", "astro-2", "pin", "pin", "7", "line", "shape", "dots", "dots",
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
        rootPress: press(root()), imagePress: press(imageGroup()), layerPress: press(layer),
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
    assert out["rootPress"] == passthrough and out["imagePress"] == passthrough
    assert out["layerPress"] == passthrough
    # a mark whose annotation is gone is still swallowed, as its own listener did, but opens nothing
    assert out["missing"] == {"calls": [], "stopped": True, "reachedWrap": False}


@pytest.mark.parametrize("rotation", [0, 90, 37])
@pytest.mark.parametrize("flip", [False, True])
def test_outlines_stay_in_image_pixels_and_every_mark_lands_where_toel_puts_it(rotation, flip):
    out = run(r"""
      resetState();
      Object.assign(camera, {rotation: ROTATION, flip: FLIP, zoom: 0.0123, cx: 51234.5, cy: 40321.25});
      state.info = {width: 100000, height: 80000, pixelSizeUm: [0.5, 0.5]};
      state.landmark.points = [{x: 2000, y: 75000}];
      const far = [[100, 100], [90000, 100], [90000, 70000], [100, 100]];
      state.annotations = [
        {id: 'far', type: 'box', x: 99000, y: 79000, w: 500, h: 400, text: ''},
        {id: 'pin', type: 'pin', x: 250, y: 70000, text: 'p'},
        {id: 'line', type: 'line', x1: 10, y1: 20, x2: 98000, y2: 77000, text: ''},
        {id: 'shape', type: 'geojson', text: '', geojson: {type: 'Feature', properties: {},
          geometry: {type: 'Polygon', coordinates: [far]}}},
        {id: 'dot', type: 'geojson', text: '', geojson: {type: 'Feature', properties: {},
          geometry: {type: 'Point', coordinates: [60000, 5000]}}},
      ];
      c.renderAnnotations();
      const drawn = picture(), errors = [];
      const near = (p, x, y, dx = 0, dy = 0) => {
        const q = toEl(x, y);
        errors.push(Math.max(Math.abs(p[0] - q.x - dx), Math.abs(p[1] - q.y - dy)));
      };
      const of = (tag, hit) => drawn.filter(r => r.tag === tag && r.hit === hit);
      const box = state.annotations[0];
      [[box.x, box.y], [box.x + box.w, box.y], [box.x + box.w, box.y + box.h], [box.x, box.y + box.h]]
        .forEach(([x, y], i) => near(of('polygon', 'far')[0].points[i], x, y));
      of('circle', 'pin').forEach(r => near(r.points[0], 250, 70000));
      const [body, tickA, tickB] = drawn.filter(r => r.tag === 'line' && r.hit === null && r.stroke[1] === 1.5);
      near(body.points[0], 10, 20); near(body.points[1], 98000, 77000);
      far.forEach(([x, y], i) => near(of('path', 'shape')[0].points[i], x, y));
      near(of('circle', 'dot')[0].points[0], 60000, 5000);
      // a box label sits 22 px above the box's top-left element corner
      const corners = [[box.x, box.y], [box.x + box.w, box.y], [box.x + box.w, box.y + box.h], [box.x, box.y + box.h]].map(([x, y]) => toEl(x, y));
      const label = of('rect', 'far')[0].points[0];
      errors.push(Math.max(Math.abs(label[0] - Math.min(...corners.map(p => p.x))), Math.abs(label[1] + 22 - Math.min(...corners.map(p => p.y)))));
      near(of('rect', 'pin')[0].points[0], 250, 70000, 10, -9);
      near(drawn.find(r => r.tag === 'circle' && r.fill === '#ff9f0a').points[0], 2000, 75000);
      // tick ends are 5 px from the line ends, perpendicular to it
      const tickLength = Math.hypot(tickA.points[1][0] - tickA.points[0][0], tickA.points[1][1] - tickA.points[0][1]);
      const polygon = descendants(imageGroup()).find(n => n.tag === 'polygon');
      const path = descendants(imageGroup()).find(n => n.tag === 'path');
      const scaled = drawn.filter(r => r.tag === 'polygon' || r.tag === 'path');
      const screenSized = drawn.filter(r => !(r.tag === 'polygon' || r.tag === 'path'));
      return {worst: Math.max(...errors), checked: errors.length, tickLength,
        polygon: polygon.getAttribute('points'), path: path.getAttribute('d'),
        outlinesInImage: descendants(imageGroup()).filter(n => n.tag === 'polygon' || n.tag === 'path').length,
        imageFirst: root().children[0] === imageGroup() && imageGroup().getAttribute('data-ann-image') === '',
        scaledStrokes: scaled.map(r => [r['stroke-linejoin'], r.stroke[1]]),
        screenSizedLinear: [...new Set(screenSized.map(r => JSON.stringify(r.linear)))]};
    """.replace("ROTATION", str(rotation)).replace("FLIP", json.dumps(flip)))
    assert out["checked"] == 4 + 2 + 2 + 4 + 1 + 1 + 1 + 1
    assert out["worst"] < 1e-6
    assert abs(out["tickLength"] - 10) < 1e-9
    # outlines are written once in image pixels; the image group maps them
    assert out["polygon"] == "99000,79000 99500,79000 99500,79400 99000,79400"
    assert out["path"] == "M100 100 L90000 100 L90000 70000 L100 100 Z"
    assert out["outlinesInImage"] == 2 and out["imageFirst"]
    # their strokes stay 1.5 element px however far the image group scales them
    assert out["scaledStrokes"] == [["round", 1.5], ["round", 1.5]]
    # labels, pins, points, measurements and landmarks never scale, turn or mirror
    assert out["screenSizedLinear"] == ["[1,0,0,1]"]


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
      const moved = picture();
      const sameRoot = root() === group && layer.children.length === 1;
      return {still, sameRoot, work,
        firstError: Math.max(Math.abs(first[0] - (firstCorner.x - start.x)), Math.abs(first[1] - (firstCorner.y - start.y))),
        secondError: Math.max(Math.abs(second[0] - (end.x - start.x)), Math.abs(second[1] - (end.y - start.y))),
        pictureError: pictureError(moved, freshPicture()), marks: moved.length,
        moved: Math.hypot(...second)};
    """.replace("ROTATION", str(rotation)).replace("FLIP", json.dumps(flip)))
    assert out["sameRoot"]
    assert out["work"] == {"elements": 0, "replaceChildren": 0, "listeners": 0, "setAttribute": 3, "removeAttribute": 0}
    assert out["still"] == [0, 0]
    assert out["firstError"] < 1e-9 and out["secondError"] < 1e-9
    assert out["moved"] > 100
    # the translated picture is the one a full render draws, mark for mark
    assert out["marks"] == 40
    assert out["pictureError"] < 1e-9


def test_camera_changes_place_the_same_nodes_again_without_rebuilding():
    out = run(r"""
      const changes = {
        zoom: () => { camera.zoom *= 1.25; },
        'zoom out': () => { camera.zoom /= 3; },
        rotation: () => { camera.rotation += 90; },
        'rotation step': () => { camera.rotation += 0.5; },
        flip: () => { camera.flip = !camera.flip; },
        'container width': () => { camera.w += 10; },
        'container height': () => { camera.h -= 10; },
        'image scale': () => { camera.itemScale = 2; },
        'zoom and pan': () => { camera.zoom *= 0.8; camera.cx += 300; },
      };
      const out = {};
      for (const [name, change] of Object.entries(changes)) {
        resetState();
        c.renderAnnotations();
        camera.cx += 40; c.refreshAnnotationsForViewport();
        const panned = root(), pannedBy = translation(panned), nodes = descendants(layer);
        change();
        const before = snapshot();
        c.refreshAnnotationsForViewport();
        const work = since(before), untranslated = panned.getAttribute('transform') === null;
        const placed = picture(), error = pictureError(placed, freshPicture());
        const after = snapshot();
        camera.cx -= 25; c.refreshAnnotationsForViewport();
        out[name] = {panned: pannedBy !== null && pannedBy[0] !== 0,
          sameNodes: root() === panned && descendants(layer).every((node, i) => node === nodes[i]),
          created: work.elements, rebuilt: work.replaceChildren, untranslated,
          matchesFreshRender: error < 1e-9, fastAgain: since(after).setAttribute === 1 && translation(panned) !== null};
      }
      return out;
    """)
    expected = {"panned": True, "sameNodes": True, "created": 0, "rebuilt": 0, "untranslated": True,
                "matchesFreshRender": True, "fastAgain": True}
    assert list(out) and {name: result for name, result in out.items() if result != expected} == {}


def test_every_drawn_input_change_forces_a_full_render():
    out = run(r"""
      const changes = {
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


def test_camera_sequences_keep_the_picture_of_a_fresh_render():
    out = run(r"""
      let seed = 11;
      const random = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
      const steps = [
        () => { camera.zoom *= 0.5 + random(); },
        () => { camera.rotation += random() * 360 - 180; },
        () => { camera.flip = !camera.flip; },
        () => { camera.cx += random() * 800 - 400; camera.cy += random() * 800 - 400; },
        () => { camera.w = 900 + Math.round(random() * 800); },
        () => { camera.itemScale = 0.5 + random(); },
      ];
      let worst = 0, checked = 0, created = 0, rebuilt = 0;
      for (let trial = 0; trial < 12; trial++) {
        resetState();
        Object.assign(camera, {zoom: 0.05 + random() * 2, rotation: random() * 360, flip: random() < 0.5});
        c.renderAnnotations();
        for (let i = 0; i < 10; i++) {
          steps[Math.floor(random() * steps.length)]();
          const before = snapshot();
          c.refreshAnnotationsForViewport();
          const work = since(before);
          created += work.elements; rebuilt += work.replaceChildren;
          worst = Math.max(worst, pictureError(picture(), freshPicture()));
          checked++;
        }
      }
      return {worst, checked, created, rebuilt};
    """)
    assert out["checked"] == 120 and out["created"] == 0 and out["rebuilt"] == 0
    assert out["worst"] < 1e-9


def test_frame_work_for_two_thousand_astromap_regions():
    out = run(r"""
      resetState();
      state.annotations = boxes(2000).map((a, i) => ({...a, astromap: true, astromapImportIndex: i + 1, text: String(i + 1)}));
      state.astromap.activeId = 'a0'; state.tempLine = null; state.landmark.points = [];
      let before = snapshot();
      c.renderAnnotations();
      const full = since(before);
      const pans = [], zooms = [];
      for (let i = 0; i < 30; i++) {
        before = snapshot();
        camera.cx += 7.5; camera.cy -= 3.25;
        c.refreshAnnotationsForViewport();
        pans.push(since(before));
      }
      for (let i = 0; i < 10; i++) {
        before = snapshot();
        camera.zoom *= 1.05;
        c.refreshAnnotationsForViewport();
        zooms.push(since(before));
      }
      return {full, pans, zooms};
    """)
    assert out["full"] == {"elements": 2 + 5 * 2000, "replaceChildren": 1, "listeners": 0,
                           "setAttribute": out["full"]["setAttribute"], "removeAttribute": 0}
    pan = {"elements": 0, "replaceChildren": 0, "listeners": 0, "setAttribute": 1, "removeAttribute": 0}
    assert out["pans"] == [pan] * 30
    # a zoom frame writes the image matrix and one position per label; the first also drops the pan
    zoom = {"elements": 0, "replaceChildren": 0, "listeners": 0, "setAttribute": 1 + 2000, "removeAttribute": 0}
    assert out["zooms"] == [{**zoom, "removeAttribute": 1}] + [zoom] * 9


def test_viewport_handler_uses_the_frame_path_and_the_listener_is_installed_once():
    app = APP.read_text(encoding="utf-8").replace("\r\n", "\n")
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


def test_a_full_render_converts_through_openseadragon_a_fixed_number_of_times():
    out = run(r"""
      resetState();
      Object.assign(camera, {rotation: 37, flip: true});
      state.tempLine = null; state.landmark.points = [];
      const calls = {n: 0}, convert = viewport.imageToViewerElementCoordinates;
      viewport.imageToViewerElementCoordinates = function (p) { calls.n++; return convert.call(this, p); };
      const counts = [];
      for (const n of [10, 2000]) {
        state.annotations = boxes(n); state.annRevision += 1;
        calls.n = 0; c.renderAnnotations(); counts.push(calls.n);
        calls.n = 0; camera.zoom *= 1.1; c.refreshAnnotationsForViewport(); counts.push(calls.n);
      }
      return counts;
    """)
    # three probes per full render and per camera frame, at any size
    assert out == [3, 3, 3, 3]


def test_imported_outlines_are_built_once_per_geometry_and_never_edited():
    out = run(r"""
      resetState();
      const shape = state.annotations.find(a => a.id === 'shape');
      const before = JSON.stringify(state.annotations);
      geoCalls.displayParts = geoCalls.imageParts = 0;
      c.renderAnnotations();
      const first = descendants(imageGroup()).find(n => n.tag === 'path').getAttribute('d');
      state.annRevision += 1; c.renderAnnotations();
      camera.zoom *= 2; camera.rotation = 30; c.refreshAnnotationsForViewport();
      const second = descendants(imageGroup()).find(n => n.tag === 'path').getAttribute('d');
      const cached = geo.imageParts(shape.geojson.geometry) === geo.imageParts(shape.geojson.geometry);
      // a broken legacy geometry is skipped and drawn by nothing
      state.annotations.push({id: 'broken', type: 'geojson', text: '', geojson: {type: 'Feature', properties: {}, geometry: null}});
      state.annRevision += 1; c.renderAnnotations();
      return {first, same: first === second, cached, calls: {...geoCalls},
        unchanged: before === JSON.stringify(state.annotations.filter(a => a.id !== 'broken')),
        broken: descendants(layer).filter(n => n.getAttribute('data-ann-id') === 'broken').length};
    """)
    assert out["first"] == "M100 100 L240 100 L220 220 L100 200 L100 100 Z M130 130 L160 130 L150 160 L130 130 Z"
    assert out["same"] and out["cached"] and out["unchanged"] and out["broken"] == 0
    # the renderer asks for image-pixel parts only; the zoom frame asks for nothing
    assert out["calls"] == {"displayParts": 0, "imageParts": 2 + 2 + 3}
