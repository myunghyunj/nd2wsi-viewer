"""Displayed AF planes and keyboard movement use the production UI helpers."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/plate-ui-v1.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(script):
    result = subprocess.run(
        [NODE, "-e", script, str(MODULE)], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_only_complete_current_site_curves_change_the_displayed_plane():
    out = run("""
const ui = require(process.argv[1]);
const map = { best: [[5, 8], [7, 9]], complete: [[true, false], [false, false]] };
const at = (t,p,auto=true) => ui.displayedZ(map,t,p,2,13,auto);
process.stdout.write(JSON.stringify([at(0,0),at(0,1),at(1,0),at(0,0,false),
  ui.displayedZ({best:[[5]]},0,0,2,13,true),
  ui.displayedZ({best:[[99]],complete:[[true]]},0,0,2,13,true)]));
""")
    assert out == [5, 2, 2, 2, 2, 2]


def test_mixed_grid_summary_reports_actual_planes_and_current_time_readiness():
    out = run("""
const ui = require(process.argv[1]);
const map={best:[[9,5,11],[2,4,6]],complete:[[true,false,true],[true,true,true]]};
process.stdout.write(JSON.stringify([
 ui.focusSummary(map,0,[0,1,2],3,13,true),
 ui.focusSummary(map,0,[1],3,13,true),
 ui.focusSummary(map,1,[0,1,2],3,13,true),
 ui.focusSummary(map,0,[0,1,2],3,13,false)
]));
""")
    assert out == [
        {"ready": 2, "total": 3, "min": 3, "max": 11},
        {"ready": 0, "total": 1, "min": 3, "max": 3},
        {"ready": 3, "total": 3, "min": 2, "max": 6},
        {"ready": 2, "total": 3, "min": 3, "max": 3},
    ]


def test_grid_arrows_follow_visual_layout_with_gaps_and_transpose():
    out = run("""
const ui=require(process.argv[1]);
const cells=[{i:5,row:0,col:0},{i:1,row:0,col:2},{i:9,row:1,col:0},{i:3,row:1,col:2}];
const go=(p,key)=>ui.nextGridSite(cells,p,key);
const rotated=cells.map(s=>({...s,row:s.col,col:s.row})).sort((a,b)=>a.row-b.row||a.col-b.col);
process.stdout.write(JSON.stringify([
 go(5,'ArrowRight'),go(5,'ArrowDown'),go(1,'ArrowRight'),go(3,'Home'),go(9,'End'),go(3,'Enter'),
 ui.nextGridSite(rotated,5,'ArrowDown'),ui.nextGridSite([],0,'ArrowRight')
]));
""")
    assert out == [1, 9, 1, 9, 3, None, 1, None]


def test_singleton_hiding_outranks_focused_control_display_rules():
    css = MODULE.with_name("style.css").read_text()
    for control in ("plate-strip", "plate-back", "plate-transpose"):
        # The extra class must beat the later .plate-focus display rule.
        assert f"#stage-wrap.plate-single.plate-focus #{control}" in css
