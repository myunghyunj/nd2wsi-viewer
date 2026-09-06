"""The drag preview, saved source rectangle, and raw export share one footprint."""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "app.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const config = JSON.parse(process.argv[2]);
function between(a, b) {
  const start = source.indexOf(a), end = source.indexOf(b, start + a.length);
  if (start < 0 || end < 0) throw Error('Missing production functions: ' + a);
  return source.slice(start, end);
}
function node(id = '') {
  return {
    id, style: {}, attributes: {}, children: [], events: {},
    get firstElementChild() { return this.children[0] || null; },
    append(child) { this.children.push(child); },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    addEventListener(name, callback) { this.events[name] = callback; },
    getBoundingClientRect() { return {left: 23, top: 31, width: 640, height: 480}; },
    classList: { add() {}, remove() {}, toggle() {} },
    setPointerCapture() {}, remove() {}, click() { exports.push(this.href); },
  };
}
const elements = new Map(), exports = [], annotations = [];
const transform = config.transform;
function project(p) {
  return {x: transform.a*p.x + transform.b*p.y + transform.tx,
    y: transform.c*p.x + transform.d*p.y + transform.ty};
}
function unproject(p) {
  const det = transform.a*transform.d - transform.b*transform.c;
  const x = p.x-transform.tx, y = p.y-transform.ty;
  return {x:(transform.d*x-transform.b*y)/det, y:(-transform.c*x+transform.a*y)/det};
}
const state = {
  info: {width: config.width, height: config.height, channels: [0], nd2Export: true},
  landmark: {active: false}, tool: null, channels: [0],
};
const context = {
  state, URLSearchParams, window: {devicePixelRatio: config.dpr},
  $(id) { if (!elements.has(id)) elements.set(id, node(id)); return elements.get(id); },
  document: {createElement() { return node(); }, createElementNS() { return node(); }, body: node()},
  clamp: (v, lo, hi) => Math.max(lo, Math.min(hi, v)),
  viewerElementToImagePoint: unproject, imageToViewerElementPoint: project,
  setTool(tool) { state.tool = tool; },
  applyRoi(x,y,w,h) { state.roi = {x,y,w,h}; },
  addAnnotation(item) { annotations.push(item); return {...item, id:'test'}; },
  activeFrameContext() { return {frame: null}; }, frameOwnsRoi() { return true; },
  appendFrameParams(q) { return q; },
  openEditor() {}, clearRoi() {}, trackExport() {},
  wireRoiDims() {}, wireAnnotationPanel() {}, showToast() {},
};
vm.createContext(context);
vm.runInContext(
  between('function imgPoint(', '/* ---- annotations: model') +
  between('function svgEl(', 'function chip(') +
  between('function finishSelection(', 'function applyRoi(') +
  between('function roiOverlayPoints(', 'function ensureRoiOverlayLayer(') +
  between('function downloadRoi(', 'let exportTimer') +
  between('function elementPoint(', 'function rectFrom(') +
  '\nconst SVG_NS = "http://www.w3.org/2000/svg"; wireTools();', context);
const stage = context.$('stage');
function event(p) {
  return {clientX:p.x+23, clientY:p.y+31, button:0, pointerId:1,
    preventDefault() {}, stopPropagation() {}};
}
state.tool = 'roi';
stage.events.pointerdown(event(config.start));
stage.events.pointermove(event(config.end));
const preview = context.$('rubber').firstElementChild.firstElementChild.attributes.points;
stage.events.pointerup(event(config.end));
context.downloadRoi('nd2');
context.downloadRoi('tiff');
state.tool = 'box';
stage.events.pointerdown(event(config.start));
stage.events.pointermove(event(config.end));
const boxPreview = context.$('rubber').firstElementChild.firstElementChild.attributes.points;
stage.events.pointerup(event(config.end));
process.stdout.write(JSON.stringify({preview, boxPreview, roi:state.roi, annotations, exports,
  overlay: context.roiOverlayPoints(state.roi), hidden:context.$('rubber').style.display}));
"""


def _transform(degrees, mirror, zoom, center):
    angle = math.radians(degrees)
    sign = -1 if mirror else 1
    a, b = sign * math.cos(angle) * zoom, -sign * math.sin(angle) * zoom
    c, d = math.sin(angle) * zoom, math.cos(angle) * zoom
    return {"a": a, "b": b, "c": c, "d": d,
            "tx": 160 - a * center[0] - b * center[1],
            "ty": 120 - c * center[0] - d * center[1]}


def _expected(config):
    t, a, b = config["transform"], config["start"], config["end"]
    det = t["a"] * t["d"] - t["b"] * t["c"]
    corners = []
    for x, y in [(a["x"], a["y"]), (b["x"], a["y"]),
                 (b["x"], b["y"]), (a["x"], b["y"])]:
        x, y = x - t["tx"], y - t["ty"]
        corners.append(((t["d"] * x - t["b"] * y) / det,
                        (-t["c"] * x + t["a"] * y) / det))

    def edge(value, limit, lower):
        value = max(0, min(limit, value))
        if abs(value - round(value)) < 1e-7:
            value = round(value)
        return math.floor(value) if lower else math.ceil(value)

    x = edge(min(p[0] for p in corners), config["width"], True)
    y = edge(min(p[1] for p in corners), config["height"], True)
    right = edge(max(p[0] for p in corners), config["width"], False)
    bottom = edge(max(p[1] for p in corners), config["height"], False)
    return {"x": x, "y": y, "w": right - x, "h": bottom - y}


def _run(config):
    result = subprocess.run([NODE, "-e", SCRIPT, str(APP), json.dumps(config)],
                            check=True, capture_output=True, text=True, timeout=20)
    return json.loads(result.stdout)


@pytest.mark.parametrize("degrees,mirror", [(d, m) for d in (0, 90, 180, 270, 37)
                                           for m in (False, True)])
@pytest.mark.parametrize("zoom,dpr,center", [(1, 1, (400, 300)), (0.7, 2, (20, 30)),
                                          (2.5, 3, (785, 590))])
def test_preview_selection_box_and_raw_export_are_the_same_integer_source_rectangle(
    degrees, mirror, zoom, dpr, center,
):
    config = {"width": 800, "height": 600, "dpr": dpr,
              "start": {"x": 100.2, "y": 70.3}, "end": {"x": 210.7, "y": 170.9},
              "transform": _transform(degrees, mirror, zoom, center)}
    out = _run(config)
    expected = _expected(config)
    assert out["roi"] == expected
    assert out["annotations"] == [{"type": "box", **expected, "text": ""}]
    assert out["preview"] == out["boxPreview"]
    points = [[float(n) for n in pair.split(",")] for pair in out["preview"].split()]
    t = config["transform"]
    x, y, w, h = (expected[k] for k in ("x", "y", "w", "h"))
    for point, raw in zip(points, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], strict=True):
        assert point == pytest.approx([t["a"] * raw[0] + t["b"] * raw[1] + t["tx"],
                                      t["c"] * raw[0] + t["d"] * raw[1] + t["ty"]])
    assert points == [[p["x"], p["y"]] for p in out["overlay"]]
    from urllib.parse import parse_qs, urlparse
    for url in out["exports"]:
        query = parse_qs(urlparse(url).query)
        assert {k: int(query[k][0]) for k in expected} == expected
        assert query["level"] == ["0"]
    assert out["hidden"] == "none"


def test_exact_quarter_turn_edges_do_not_expand_for_trigonometric_roundoff():
    config = {"width": 800, "height": 600, "dpr": 2,
              "start": {"x": 100, "y": 70}, "end": {"x": 210, "y": 170},
              "transform": _transform(90, True, 1, (400, 300))}
    out = _run(config)
    assert out["roi"] == {"x": 350, "y": 240, "w": 100, "h": 110}


def test_roi_hint_names_the_source_aligned_selection_contract():
    source = APP.read_text()
    assert 'Source-aligned rectangle · Drag' in source
    assert '$("tool-box").title = "Source-aligned rectangle"' in source
