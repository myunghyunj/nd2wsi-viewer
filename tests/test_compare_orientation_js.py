"""Exercise the production shell's orientation transactions, without a browser."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nd2wsi" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const Align = require(process.argv[1] + '/align-v1.js');
const source = fs.readFileSync(process.argv[1] + '/shell-v1.js', 'utf8');
// Top-level function closing braces are unindented in the production file.
function production(name) {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing production function: ' + name);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
const compare = {
  enabled: true, toolsVisible: true, anchorSid: 'a', members: ['b', 'c'], orientationSid: 'b',
  landmark: {active: false}, pendingRequest: null, requestSeq: 0,
  states: new Map(), pairs: new Map(), memory: new Map(), anchorLandmarks: [],
};
const messages = [];
const styleValues = {};
const timers = new Map();
let timerSeq = 0;
const context = vm.createContext({
  Align, compare, messages, styleValues, VIEWPORT_PROTOCOL_VERSION: 2,
  document: {documentElement:{style:{setProperty:(key,value)=>styleValues[key]=value}}},
  $: () => ({getBoundingClientRect:()=>({height:100})}),
  groupSids: () => [compare.anchorSid, ...compare.members],
  postToSlide: (sid, message) => messages.push({sid, ...message}),
  clearViewportRoutes() {}, updateCompareControls() {},
  syncFromAnchor: () => messages.push({sync:true}),
  setTimeout: (fn, delay) => {const id=++timerSeq; timers.set(id,{fn,delay}); return id;},
  clearTimeout: id => timers.delete(id), showError() {},
  runTimers: delay => {
    for (const [id, timer] of [...timers]) if (timer.delay === delay && timers.has(id)) {
      timers.delete(id); timer.fn();
    }
  },
  sendTabShortcutState() {}, scheduleNativeGestureScopes() {}, broadcastCompareState() {},
  sendLandmarkMode() {},
  fitPair() {throw new Error('must not refit during a manual orientation');},
  clonePoints: (points) => (points || []).map(p => ({...p})),
});
for (const name of [
  'mappingMode', 'pxToSpace', 'spaceToPx', 'imageCenterSpace', 'newPair', 'defaultTransform',
  'ensurePairTransform', 'displayTransformFor', 'applyDisplayTransform',
  'clearPendingRequest', 'requestGroup', 'finishGroupRequest', 'changeOrientation',
  'restoreAlignment', 'pairKey', 'orientationNeedsFocusedSite', 'syncCompareToolbarSpace',
  'requestGroupSoon', 'paneCameUp', 'inGroup',
]) vm.runInContext(production(name), context);
const realRequestGroupSoon = context.requestGroupSoon;
context.requestGroupSoon = kind => {
  messages.push({deferred:kind}); realRequestGroupSoon(kind);
};
for (const [sid, pixel, center] of [
  ['a', 0.25, {x:321, y:211}], ['b', 0.66, {x:620, y:390}],
  ['c', 0.5, {x:200, y:310}],
]) {
  compare.states.set(sid, {
    centerPx: center, imagePx: {x:1000, y:800}, pixelSizeUm: {x:pixel, y:pixel},
    spanPx: {x:400, y:300}, containerPx: {x:800, y:600},
  });
}
for (const sid of compare.members) {
  compare.pairs.set(sid, context.newPair('a.svs', sid + '.nd2'));
  context.ensurePairTransform(sid);
}
const out = vm.runInContext(process.argv[2], context);
process.stdout.write(JSON.stringify(out));
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(STATIC), body],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_initial_orientation_is_identity_not_guessed_from_file_format():
    assert run("displayTransformFor('b')") == {"degrees": 0, "flipped": False}


def test_orientation_transaction_captures_target_and_keeps_the_view_center():
    result = run("""
      const beforeC = JSON.stringify(compare.pairs.get('c'));
      changeOrientation('rotate-right');
      const pending = compare.pendingRequest;
      compare.orientationSid = 'c'; // a late selection cannot redirect the request
      finishGroupRequest(pending);
      const pair = compare.pairs.get('b');
      ({target: pending.targetSid, pose: displayTransformFor('b'),
        actual: Align.apply(pair.transform, {x:321*.25, y:211*.25}),
        expected: {x:620*.66, y:390*.66},
        cUnchanged: JSON.stringify(compare.pairs.get('c')) === beforeC,
        zoomResyncs: messages.filter(m => m.sync).length,
        transformed: messages.filter(m => m.nd2wsi === 'display-transform').map(m => m.sid),
        requests: messages.filter(m => m.nd2wsi === 'viewport-request').length});
    """)
    assert result["target"] == "b"
    assert result["pose"] == {"degrees": 90, "flipped": False}
    assert result["actual"] == pytest.approx(result["expected"])
    assert result["cUnchanged"] is True
    assert result["zoomResyncs"] == 0
    assert result["transformed"] == ["b"]
    assert result["requests"] == 3


@pytest.mark.parametrize("action", ["flip-horizontal", "flip-vertical", "transpose"])
def test_two_reflections_restore_orientation_and_reset_keeps_position(action):
    result = run(f"""
      for (let i=0; i<2; i++) {{
        changeOrientation('{action}'); finishGroupRequest(compare.pendingRequest);
      }}
      const restored = displayTransformFor('b');
      changeOrientation('rotate-left'); finishGroupRequest(compare.pendingRequest);
      changeOrientation('reset'); finishGroupRequest(compare.pendingRequest);
      ({{restored, reset: displayTransformFor('b'),
        center: Align.apply(compare.pairs.get('b').transform, {{x:321*.25, y:211*.25}})}});
    """)
    assert result["restored"] == result["reset"] == {"degrees": 0, "flipped": False}
    assert result["center"] == pytest.approx({"x": 620 * .66, "y": 390 * .66})


@pytest.mark.parametrize("block", [
    "compare.landmark.active = true;",
    "compare.pairs.get('b').fit = {pairs:4};",
    "compare.pendingRequest = {requestId:'busy'};",
    "compare.orientationSid = 'a';",
    "compare.orientationSid = 'removed';",
    "compare.states.get('a').plateGrid = true;",
    "compare.states.get('b').plateGrid = true;",
])
def test_orientation_refuses_fitted_reference_removed_and_busy_targets(block):
    assert run(block + " changeOrientation('transpose'); messages.length;") == 0


def test_invalid_action_and_stale_or_removed_target_do_not_mutate():
    result = run("""
      changeOrientation('unrecognized');
      const invalidMessages = messages.length;
      changeOrientation('transpose');
      const old = compare.pendingRequest;
      changeOrientation('flip-horizontal'); // busy request cannot be replaced
      const sameRequest = compare.pendingRequest === old;
      compare.pendingRequest = null;
      finishGroupRequest(old); // cancelled/stale callback
      const stalePose = displayTransformFor('b');
      changeOrientation('transpose');
      const removed = compare.pendingRequest;
      compare.members = ['c'];
      finishGroupRequest(removed);
      ({invalidMessages, sameRequest, stalePose,
        transforms: messages.filter(m => m.nd2wsi === 'display-transform').length});
    """)
    assert result == {
        "invalidMessages": 0, "sameRequest": True,
        "stalePose": {"degrees": 0, "flipped": False}, "transforms": 0,
    }


def test_resize_sync_cannot_cancel_an_inflight_orientation_button():
    result = run("""
      changeOrientation('transpose');
      const pending = compare.pendingRequest;
      requestGroup('sync');
      const preserved = compare.pendingRequest === pending;
      finishGroupRequest(pending);
      ({preserved, pose:displayTransformFor('b'),
        deferred:messages.filter(m => m.deferred).map(m => m.deferred)});
    """)
    assert result == {
        "preserved": True, "pose": {"degrees": 90, "flipped": True},
        "deferred": ["sync"],
    }


def test_pane_reload_retains_target_action_and_request_identity():
    result = run("""
      changeOrientation('transpose');
      const pending = compare.pendingRequest;
      pending.seen.add('b');
      paneCameUp('b');
      const retry = messages.filter(m => m.nd2wsi === 'viewport-request').at(-1);
      const rereadsPane = !pending.seen.has('b');
      finishGroupRequest(pending);
      ({rereadsPane, requestId:retry.requestId, originalId:pending.requestId,
        action:pending.action, target:pending.targetSid, pose:displayTransformFor('b')});
    """)
    assert result["rereadsPane"] is True
    assert result["requestId"] == result["originalId"]
    assert result["action"] == "transpose"
    assert result["target"] == "b"
    assert result["pose"] == {"degrees": 90, "flipped": True}


def test_deferred_real_layout_timer_runs_after_orientation_completion():
    result = run("""
      changeOrientation('transpose');
      const pending = compare.pendingRequest;
      requestGroupSoon('sync');
      runTimers(80); // a resize while the action awaits viewport replies
      const samePending = compare.pendingRequest === pending;
      finishGroupRequest(pending);
      runTimers(80); // finishing must not erase this deferred layout request
      ({samePending, kind:compare.pendingRequest?.kind, pose:displayTransformFor('b')});
    """)
    assert result == {
        "samePending": True, "kind": "sync", "pose": {"degrees": 90, "flipped": True},
    }


def test_uncalibrated_nonsquare_images_keep_isotropic_space_under_quarter_turns():
    result = run("""
      const st = {imagePx:{x:1000,y:300}};
      const point = {x:100,y:200};
      const normalized = pxToSpace(point, st, 'normalized');
      ({normalized, roundtrip:spaceToPx(normalized, st, 'normalized'),
        rotated:spaceToPx(Align.apply(Align.screenOperation('rotate-right'),
          normalized), st, 'normalized')});
    """)
    assert result == {
        "normalized": {"x": .1, "y": .2},
        "roundtrip": {"x": 100, "y": 200}, "rotated": {"x": -200, "y": 100},
    }


def test_toolbar_reserves_space_once_and_releases_it_on_leaving_compare():
    result = run("""
      syncCompareToolbarSpace();
      const reserved = styleValues['--compare-toolbar-height'];
      syncCompareToolbarSpace();
      const updates = messages.filter(m => m.deferred).length;
      compare.enabled = false;
      syncCompareToolbarSpace();
      ({reserved, updates, released:styleValues['--compare-toolbar-height']});
    """)
    assert result == {"reserved": "120px", "updates": 1, "released": "0px"}
    css = (STATIC / "native-shell-v1.css").read_text()
    assert "inset:calc(42px + var(--compare-toolbar-height, 0px))" in css


def test_reverse_pair_restores_inverse_orientation_and_reflected_fit_metadata():
    result = run("""
      const orientation = Align.reorient(Align.reorient(Align.identity(),
        'rotate-right'), 'flip-horizontal');
      const transform = {...orientation, a:orientation.a*2, b:orientation.b*2,
        c:orientation.c*2, d:orientation.d*2, tx:40, ty:60};
      compare.memory.set('b|a', {mode:'physical', orientation, transform,
        fit:{angleDeg:Align.angleDeg(transform), scale:2, rms:10, reflected:true},
        landmarks:[{x:4,y:5}], anchorLandmarks:[{x:6,y:7}]});
      const pair = newPair();
      restoreAlignment('a', 'b', pair);
      ({orientation:pair.orientation, expected:Align.invert(orientation),
        angle:pair.fit.angleDeg, expectedAngle:Align.angleDeg(pair.transform),
        scale:pair.fit.scale, rms:pair.fit.rms});
    """)
    assert result["orientation"] == result["expected"]
    assert result["angle"] == result["expectedAngle"]
    assert result["scale"] == .5
    assert result["rms"] == 5


def test_visible_orientation_controls_are_wired_and_explained():
    html = (STATIC / "shell.html").read_text()
    shell = (STATIC / "shell-v1.js").read_text()
    for control in ("flip-horizontal", "flip-vertical", "rotate-left", "rotate-right", "transpose"):
        assert f'id="compare-{control}"' in html
        assert f'"compare-{control}": "{control}"' in shell
    assert 'id="compare-orientation-target" aria-label="Slide to orient"' in html
    assert '"compare-orientation-reset": "reset"' in shell
    assert '$(id).onclick = () => changeOrientation(action)' in shell
    assert 'Align → Clear' in html + shell
    assert 'Transpose' in (ROOT / "README.md").read_text()
