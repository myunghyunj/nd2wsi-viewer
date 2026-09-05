"""ROI display projection under viewer rotation and reflection."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "nd2wsi" / "static" / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const start = source.indexOf("function roiOverlayPoints(");
const end = source.indexOf("function ensureRoiOverlayLayer(", start);
if (start < 0 || end < 0) throw new Error("production roiOverlayPoints function not found");
const context = {
  operation: null,
  imageToViewerElementPoint(point) {
    const t = context.operation;
    return {
      x: t.a * point.x + t.b * point.y + t.tx,
      y: t.c * point.x + t.d * point.y + t.ty,
    };
  },
};
vm.createContext(context);
vm.runInContext(source.slice(start, end) + "\nthis.project = roiOverlayPoints;", context);
const roi = {x: 10, y: 20, w: 30, h: 40};
const operations = {
  identity: {a: 1, b: 0, c: 0, d: 1, tx: 100, ty: 200},
  rotateRight: {a: 0, b: -1, c: 1, d: 0, tx: 100, ty: 200},
  rotateLeft: {a: 0, b: 1, c: -1, d: 0, tx: 100, ty: 200},
  flipHorizontal: {a: -1, b: 0, c: 0, d: 1, tx: 100, ty: 200},
  flipVertical: {a: 1, b: 0, c: 0, d: -1, tx: 100, ty: 200},
  transpose: {a: 0, b: 1, c: 1, d: 0, tx: 100, ty: 200},
};
const out = {};
for (const [name, operation] of Object.entries(operations)) {
  context.operation = operation;
  out[name] = context.project(roi).map((point) => [point.x, point.y]);
}
out.empty = context.project(null);
out.roiAfter = roi;
process.stdout.write(JSON.stringify(out));
"""


def _run():
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(APP)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_roi_projects_all_four_raw_corners_through_display_transform():
    out = _run()
    assert out["identity"] == [[110, 220], [140, 220], [140, 260], [110, 260]]
    assert out["rotateRight"] == [[80, 210], [80, 240], [40, 240], [40, 210]]
    assert out["rotateLeft"] == [[120, 190], [120, 160], [160, 160], [160, 190]]
    assert out["flipHorizontal"] == [[90, 220], [60, 220], [60, 260], [90, 260]]
    assert out["flipVertical"] == [[110, 180], [140, 180], [140, 140], [110, 140]]
    assert out["transpose"] == [[120, 210], [120, 240], [160, 240], [160, 210]]
    assert out["empty"] == []
    assert out["roiAfter"] == {"x": 10, "y": 20, "w": 30, "h": 40}


def test_roi_uses_a_separate_svg_layer_and_refreshes_with_the_viewport():
    app = APP.read_text()
    draw = app[app.index("function ensureRoiOverlayLayer(") : app.index("function restoreRoiOverlay(")]
    viewport_handler = app[
        app.index('viewer.addHandler("update-viewport"') :
        app.index('viewer.addHandler("open", renderAnnotations)')
    ]
    annotations = app[
        app.index("function renderAnnotations(") : app.index("function renderLandmarks(")
    ]

    assert 'layer.id = "roi-layer"' in draw
    assert '$("ann-layer").before(layer)' in draw
    assert 'document.createElementNS(SVG_NS, "polygon")' in draw
    assert "roiOverlayPoints(r)" in draw
    assert "viewer.addOverlay" not in draw
    assert "viewer.updateOverlay" not in draw
    assert "moveRoiOverlay();" in viewport_handler
    assert 'const layer = $("ann-layer")' in annotations
    assert "layer.replaceChildren()" in annotations
    assert '$("roi-layer")' not in annotations
