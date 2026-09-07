"""Physical screen-horizontal scale is projection-aware and independent of DPR."""

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
const nodes = {scalebar: {style:{}}, 'scalebar-label': {textContent:''}};
const angle = config.degrees*Math.PI/180, sign = config.mirror ? -1 : 1;
const a = sign*Math.cos(angle)*config.zoom, b = -sign*Math.sin(angle)*config.zoom;
const c = Math.sin(angle)*config.zoom, d = Math.cos(angle)*config.zoom;
const det = a*d-b*c;
const context = {
  window:{devicePixelRatio:config.dpr},
  state:{viewer:{viewport:{getContainerSize:()=>({x:config.width,y:config.height})}},
    plate:config.grid ? {focus:null} : null},
  pixelSize:()=>config.pixelSize, $:id=>nodes[id],
  viewerElementToImagePoint:p=>({x:(d*p.x-b*p.y)/det+57,y:(-c*p.x+a*p.y)/det+92}),
};
vm.createContext(context);
const start = source.indexOf('function screenHorizontalUmPerCssPixel(');
const end = source.indexOf('/* ---- tools: ROI select', start);
vm.runInContext(source.slice(start,end),context);
const scale = context.screenHorizontalUmPerCssPixel();
context.updateScalebar(config.zoom);
const output = {scale, width:nodes.scalebar.style.width, display:nodes.scalebar.style.display,
  label:nodes['scalebar-label'].textContent};
// Returning to calibrated data must restore a bar hidden on uncalibrated data.
const old = config.pixelSize; config.pixelSize = null; context.updateScalebar(config.zoom);
output.uncalibratedDisplay = nodes.scalebar.style.display;
output.uncalibratedLabel = nodes['scalebar-label'].textContent;
config.pixelSize=old; context.updateScalebar(config.zoom);
output.restoredDisplay = nodes.scalebar.style.display;
process.stdout.write(JSON.stringify(output));
"""


def _run(config):
    result = subprocess.run([NODE, "-e", SCRIPT, str(APP), json.dumps(config)],
                            check=True, capture_output=True, text=True, timeout=20)
    return json.loads(result.stdout)


@pytest.mark.parametrize("degrees,mirror", [(d, m) for d in (0, 37, 90, 180, 270)
                                           for m in (False, True)])
@pytest.mark.parametrize("pixel_size", [[0.50, 0.25], [0.71, 0.33], [0.25, 0.25]])
@pytest.mark.parametrize("zoom,dpr,width,height", [(0.3, 1, 640, 480), (2.5, 2, 1200, 900)])
def test_scalebar_measures_physical_screen_horizontal_distance(
    degrees, mirror, pixel_size, zoom, dpr, width, height,
):
    out = _run({"degrees": degrees, "mirror": mirror, "pixelSize": pixel_size,
                "zoom": zoom, "dpr": dpr, "width": width, "height": height})
    angle = math.radians(degrees)
    expected = math.hypot(math.cos(angle) * pixel_size[1],
                          math.sin(angle) * pixel_size[0]) / zoom
    assert out["scale"] == pytest.approx(expected)
    length, unit = out["label"].split()
    physical = float(length) * (1000 if unit == "mm" else 1)
    assert float(out["width"].removesuffix("px")) * expected == pytest.approx(physical)
    assert out["display"] == ""
    assert out["uncalibratedDisplay"] == "none"
    assert out["uncalibratedLabel"] == ""
    assert out["restoredDisplay"] == ""


def test_dpr_alone_never_changes_css_scalebar_and_grid_ignores_hidden_viewer_pose():
    config = {"degrees": 37, "mirror": True, "pixelSize": [0.5, 0.25],
              "zoom": 1.5, "dpr": 1, "width": 800, "height": 600}
    assert _run(config) == _run({**config, "dpr": 3})
    grid = _run({**config, "grid": True})
    unrotated_grid = _run({**config, "degrees": 0, "grid": True})
    assert grid["width"] == unrotated_grid["width"]
    assert grid["label"] == unrotated_grid["label"]
    assert float(grid["width"].removesuffix("px")) * 0.25 / 1.5 == pytest.approx(
        float(grid["label"].split()[0])
    )


def test_unknown_invalid_calibration_and_zero_size_hide_bar():
    config = {"degrees": 90, "mirror": False, "pixelSize": None,
              "zoom": 1, "dpr": 2, "width": 800, "height": 600}
    for changes in ({}, {"pixelSize": [0, 1]}, {"pixelSize": [0.5, 0.25], "width": 0}):
        out = _run({**config, **changes})
        assert out["scale"] is None
        assert out["display"] == "none"
        assert out["label"] == ""


def test_scalebar_refreshes_on_rotation_flip_resize_and_viewport_changes():
    source = APP.read_text(encoding="utf-8")
    assert 'for (const event of ["rotate", "flip", "resize"])' in source
    viewport = source[source.index('viewer.addHandler("update-viewport"'):
                      source.index('viewer.addHandler("open", renderAnnotations)')]
    assert "updateScalebar(" in viewport
