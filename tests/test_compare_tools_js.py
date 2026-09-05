"""Keep comparison-tool visibility independent of the linked slide group."""

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
const source = fs.readFileSync(process.argv[1] + '/shell-v1.js', 'utf8');
function production(name) {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing production function: ' + name);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
const calls = [];
const styleValues = {};
const nodes = new Map();
const timers = new Map();
let timerSeq = 0;
const document = {
  activeElement: null,
  documentElement: {style: {setProperty: (k, v) => styleValues[k] = v}},
};
function $(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    const attrs = {};
    const node = {
      id, hidden: false, disabled: false, style: {}, attrs,
      classList: {
        toggle: (name, force) => {
          const value = force === undefined ? !classes.has(name) : force;
          if (value) classes.add(name); else classes.delete(name);
          return value;
        },
        contains: name => classes.has(name),
      },
      setAttribute: (name, value) => attrs[name] = String(value),
      getAttribute: name => attrs[name] ?? null,
      getBoundingClientRect: () => ({height: node.hidden ? 0 : 100}),
      focus: () => {document.activeElement = node; calls.push(['focus', id]);},
      contains: target => target?.id?.startsWith('compare-') &&
        target.id !== 'compare-tools-toggle' && target.id !== 'compare-toggle',
      querySelector: () => $('compare-orientation-target'),
    };
    nodes.set(id, node);
  }
  return nodes.get(id);
}
const originalPair = {mode: 'physical', transform: {a:1,b:0,c:0,d:1,tx:10,ty:20},
  orientation: {a:0,b:1,c:1,d:0,tx:0,ty:0}, landmarks: [{x:3,y:4}]};
const compare = {
  enabled: true, toolsVisible: true, linked: true, toolbarHeight: 120,
  anchorSid: 'a', members: ['b'], orientationSid: 'b', split: 50,
  pairs: new Map([['b', originalPair]]), states: new Map(),
  anchorLandmarks: [{x:1,y:2}], landmark: {active: false, before: null},
  pendingRequest: null,
};
const frames = new Map([['a', $('frame-a')], ['b', $('frame-b')]]);
const context = vm.createContext({
  $, document, compare, calls, styleValues, frames, active: 'a', MAX_GROUP: 4,
  slides: [{sid:'a',name:'A'}, {sid:'b',name:'B'}, {sid:'c',name:'C'}],
  groupSids: () => compare.enabled ? [compare.anchorSid, ...compare.members] : [],
  inGroup: sid => compare.enabled && [compare.anchorSid, ...compare.members].includes(sid),
  closePairPicker: restore => calls.push(['closePicker', restore]),
  scheduleNativeGestureScopes: () => calls.push(['scopes']),
  requestGroupSoon: kind => calls.push(['requestGroupSoon', kind]),
  updateOrientationControls() {}, renderChips() {}, renderLandmarkPanel() {},
  rememberAlignment() {}, broadcastCompareState() {}, render() {},
  alignmentDeltaLabel: () => '',
  rememberSlide() {}, ensureFrame() {},
  attachMember: sid => {compare.members.push(sid); compare.pairs.set(sid, {});},
  applyDisplayTransforms() {},
  clearPendingRequest: () => {compare.pendingRequest = null;},
  clearViewportRoutes() {}, clearDisplayTransforms() {}, sendLandmarkMode() {},
  finishLandmarks: keep => {calls.push(['finishLandmarks', keep]); compare.landmark.active=false;},
  requestAnimationFrame: fn => fn(),
  setTimeout: (fn, delay) => {const id = ++timerSeq; timers.set(id, {fn,delay}); return id;},
  clearTimeout: id => timers.delete(id),
  runTimers: delay => {
    for (const [id, timer] of [...timers]) if (timer.delay === delay && timers.has(id)) {
      timers.delete(id); timer.fn();
    }
  },
});
context.useProduction = names => {
  for (const name of names) vm.runInContext(production(name), context);
};
for (const name of [
  'syncCompareToolsVisibility', 'setCompareToolsVisible', 'syncCompareToolbarSpace',
  'applyFrameLayout', 'updateCompareControls', 'startGroup', 'stopCompare',
]) vm.runInContext(production(name), context);
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


def test_hide_tools_preserves_group_orientation_and_inflight_action():
    result = run("""
      compare.pendingRequest = {requestId:'orient-7',kind:'orientation',targetSid:'b',action:'transpose'};
      const before = JSON.stringify({members:compare.members,pair:compare.pairs.get('b'),
        anchorLandmarks:compare.anchorLandmarks});
      const pair = compare.pairs.get('b');
      const pending = compare.pendingRequest;
      setCompareToolsVisible(false);
      ({enabled:compare.enabled, linked:compare.linked, visible:compare.toolsVisible,
        hidden:$('compare-controls').hidden, samePair:pair===compare.pairs.get('b'),
        samePending:pending===compare.pendingRequest,
        unchanged:before===JSON.stringify({members:compare.members,pair:compare.pairs.get('b'),
          anchorLandmarks:compare.anchorLandmarks}),
        pickerClosed:calls.some(c=>c[0]==='closePicker'&&c[1]===false),
        scopes:calls.some(c=>c[0]==='scopes'), focused:document.activeElement?.id,
        height:styleValues['--compare-toolbar-height']});
    """)
    assert result == {
        "enabled": True, "linked": True, "visible": False, "hidden": True,
        "samePair": True, "samePending": True, "unchanged": True,
        "pickerClosed": True, "scopes": True, "focused": "compare-tools-toggle",
        "height": "0px",
    }


def test_hide_does_not_relink_a_deliberately_unlinked_group():
    assert run("""
      compare.linked = false;
      setCompareToolsVisible(false);
      setCompareToolsVisible(true);
      ({enabled:compare.enabled,linked:compare.linked});
    """) == {"enabled": True, "linked": False}


def test_hide_layout_timer_does_not_supersede_an_orientation_request():
    result = run("""
      useProduction(['requestGroupSoon','requestGroup']);
      compare.pendingRequest = {requestId:'orient-7',kind:'orientation',targetSid:'b',action:'transpose'};
      const pending = compare.pendingRequest;
      setCompareToolsVisible(false);
      runTimers(80);
      ({samePending:pending===compare.pendingRequest,
        kind:compare.pendingRequest.kind, hidden:$('compare-controls').hidden,
        deferred:Boolean(compare.layoutRequestTimer)});
    """)
    assert result == {
        "samePending": True, "kind": "orientation", "hidden": True, "deferred": True,
    }


def test_hidden_tools_stay_hidden_through_control_and_layout_updates():
    result = run("""
      setCompareToolsVisible(false);
      compare.pendingRequest = {requestId:'layout-8',kind:'sync'};
      updateCompareControls();
      compare.split = 65;
      applyFrameLayout();
      ({visible:compare.toolsVisible,hidden:$('compare-controls').hidden,
        height:styleValues['--compare-toolbar-height'],
        dividerHidden:$('compare-divider').hidden,
        left:$('frame-a').style.width,right:$('frame-b').style.width,
        cells:[...frames.values()].every(f=>f.classList.contains('compare-cell'))});
    """)
    assert result == {
        "visible": False, "hidden": True, "height": "0px", "dividerHidden": False,
        "left": "65%", "right": "35%", "cells": True,
    }


def test_reopen_restores_controls_space_and_accessibility_state():
    result = run("""
      setCompareToolsVisible(false);
      setCompareToolsVisible(true);
      ({visible:compare.toolsVisible,hidden:$('compare-controls').hidden,
        height:styleValues['--compare-toolbar-height'],
        toggleHidden:$('compare-tools-toggle').hidden,
        expanded:$('compare-tools-toggle').getAttribute('aria-expanded'),
        focused:document.activeElement?.id});
    """)
    assert result == {
        "visible": True, "hidden": False, "height": "120px", "toggleHidden": False,
        "expanded": "true", "focused": "compare-close",
    }


def test_hiding_alignment_tools_does_not_commit_or_cancel_landmarks():
    result = run("""
      compare.landmark = {active:true,before:{linked:true,anchorLandmarks:[]}};
      const editing = compare.landmark;
      setCompareToolsVisible(false);
      updateCompareControls();
      ({active:compare.landmark.active,same:editing===compare.landmark,
        ended:calls.some(c=>c[0]==='finishLandmarks'),
        visible:compare.toolsVisible,enabled:compare.enabled,
        label:$('compare-tools-toggle').title});
    """)
    assert result["active"] and result["same"] and result["enabled"]
    assert not result["ended"] and not result["visible"]
    assert "align" in result["label"].lower()


def test_leaving_compare_hides_reopen_button_and_next_group_starts_with_tools():
    result = run("""
      setCompareToolsVisible(false);
      stopCompare();
      const stopped = {enabled:compare.enabled,visible:compare.toolsVisible,
        toggleHidden:$('compare-tools-toggle').hidden,height:styleValues['--compare-toolbar-height']};
      startGroup('a','c');
      ({stopped,restarted:{enabled:compare.enabled,visible:compare.toolsVisible,
        hidden:$('compare-controls').hidden,toggleHidden:$('compare-tools-toggle').hidden,
        height:styleValues['--compare-toolbar-height']}});
    """)
    assert result == {
        "stopped": {"enabled": False, "visible": False, "toggleHidden": True, "height": "0px"},
        "restarted": {"enabled": True, "visible": True, "hidden": False,
                      "toggleHidden": False, "height": "120px"},
    }


def test_close_button_only_hides_tools_and_link_button_still_stops_comparison():
    shell = (STATIC / "shell-v1.js").read_text()
    html = (STATIC / "shell.html").read_text()
    assert '$("compare-close").onclick = () => setCompareToolsVisible(false)' in shell
    assert '$("compare-close").onclick = stopCompare' not in shell
    assert '$("compare-toggle").onclick = toggleCompare' in shell
    assert 'if (compare.enabled) stopCompare();' in shell
    assert 'id="compare-tools-toggle"' in html
    assert 'aria-controls="compare-controls"' in html
    assert 'Hide comparison tools' in html + shell
