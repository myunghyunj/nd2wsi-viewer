"""Trackpad axis selection, exercised through the production JavaScript."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "axis-latch-v1.js"
APP = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "app.js"
INDEX = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(script):
    result = subprocess.run(
        [NODE, "-e", script, str(MODULE)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_diagonal_gesture_changes_only_one_axis_until_more_than_100ms_idle():
    out = _run(
        r"""
const {AxisLatch}=require(process.argv[1]);
const latch=new AxisLatch({idleMs:100,dominance:1.1,threshold:2});
process.stdout.write(JSON.stringify([
  latch.feed(4,2,0),
  latch.feed(1,8,20),
  latch.feed(0,5,40),
  latch.feed(0,5,141),
]));
"""
    )
    assert out == ["x", "x", "x", "y"]


def test_latch_waits_for_dominance_uses_net_motion_and_resets_cleanly():
    out = _run(
        r"""
const {AxisLatch}=require(process.argv[1]);
const latch=new AxisLatch({idleMs:100,dominance:1.2,threshold:4});
const values=[latch.feed(2,2,0),latch.feed(2,-2,10)];
latch.reset();
values.push(latch.feed(3,0.5,20),latch.feed(-3,0.5,30));
latch.reset();
values.push(latch.feed(0,5,40));
process.stdout.write(JSON.stringify(values));
"""
    )
    assert out == [None, "x", None, None, "y"]


def test_modest_horizontal_swipe_gets_a_responsive_first_time_step():
    out = _run(
        r"""
const {WheelGestureSession}=require(process.argv[1]);
const gesture=new WheelGestureSession({
  mode:"grid",idleMs:100,timeStartStep:18,timeStep:60
});
const events=[
  gesture.feed({deltaX:7,deltaY:2,at:0}),
  gesture.feed({deltaX:6,deltaY:-2,at:15}),
  gesture.feed({deltaX:5,deltaY:1,at:30}),
  gesture.feed({deltaX:-9,deltaY:1,at:131}),
  gesture.feed({deltaX:-9,deltaY:-1,at:146}),
];
process.stdout.write(JSON.stringify(events));
"""
    )
    assert [item["axis"] for item in out] == ["x", "x", "x", "x", "x"]
    assert [item["timeSteps"] for item in out] == [0, 0, 1, 0, -1]
    assert all(item["zSteps"] == 0 for item in out)


def test_grid_diagonal_swipe_stays_on_time_and_idle_clears_remainder():
    out = _run(
        r"""
const {WheelGestureSession}=require(process.argv[1]);
const gesture=new WheelGestureSession({
  mode:"grid",idleMs:100,dominance:1.2,threshold:3,timeStep:60,zStep:40
});
const first=[
  gesture.feed({deltaX:25,deltaY:10,at:0}),
  gesture.feed({deltaX:15,deltaY:20,at:20}),
  gesture.feed({deltaX:25,deltaY:10,at:40}),
  gesture.feed({deltaX:15,deltaY:20,at:60}),
];
const afterIdle=gesture.feed({deltaX:0,deltaY:40,at:161});
process.stdout.write(JSON.stringify({first,afterIdle}));
"""
    )
    assert [item["axis"] for item in out["first"]] == ["x", "x", "x", "x"]
    assert sum(item["timeSteps"] for item in out["first"]) == 2
    assert sum(item["zSteps"] for item in out["first"]) == 0
    assert out["afterIdle"]["axis"] == "y"
    assert out["afterIdle"]["zSteps"] == -1
    assert out["afterIdle"]["timeSteps"] == 0


def test_focus_option_mode_is_fixed_by_the_first_event_until_idle():
    out = _run(
        r"""
const {WheelGestureSession}=require(process.argv[1]);
const gesture=new WheelGestureSession({mode:"focus",idleMs:100,threshold:2,zStep:10});
const optionStart=[
  gesture.feed({deltaY:10,altKey:true,at:0}),
  gesture.feed({deltaY:10,altKey:false,at:20}),
];
const zoomStart=[
  gesture.feed({deltaY:10,altKey:false,at:121}),
  gesture.feed({deltaY:10,altKey:true,at:141}),
];
process.stdout.write(JSON.stringify({optionStart,zoomStart}));
"""
    )
    assert [item["focusYMode"] for item in out["optionStart"]] == ["z", "z"]
    assert [item["zSteps"] for item in out["optionStart"]] == [-1, -1]
    assert all(item["consume"] for item in out["optionStart"])
    assert [item["focusYMode"] for item in out["zoomStart"]] == ["zoom", "zoom"]
    assert all(not item["consume"] for item in out["zoomStart"])
    assert all(item["zSteps"] == 0 for item in out["zoomStart"])


def test_line_and_page_wheel_deltas_are_normalized():
    out = _run(
        r"""
const {WheelGestureSession}=require(process.argv[1]);
const horizontal=new WheelGestureSession({mode:"grid",timeStep:60});
const vertical=new WheelGestureSession({mode:"grid",zStep:40});
process.stdout.write(JSON.stringify({
  line:horizontal.feed({deltaX:4,deltaMode:1,at:0}),
  page:vertical.feed({deltaY:1,deltaMode:2,pagePixels:200,at:0}),
}));
"""
    )
    assert out["line"]["timeSteps"] == 1
    assert out["page"]["zSteps"] == -5


def test_page_loads_axis_latch_before_the_app_and_app_uses_sessions():
    index = INDEX.read_text()
    app = APP.read_text()
    assert index.index("axis-latch-v1.js") < index.index("app.js")
    assert "Nd2AxisLatch.WheelGestureSession" in app
    assert "Math.abs(ev.deltaX) > Math.abs(ev.deltaY)" not in app
    assert 'new GestureSession({ mode: "grid", idleMs: 100 })' in app
    assert 'new GestureSession({ mode: "focus", idleMs: 100 })' in app
    assert '$("stage-wrap").addEventListener("wheel"' in app
