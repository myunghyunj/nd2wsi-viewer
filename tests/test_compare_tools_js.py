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
Object.defineProperty(globalThis, 'navigator', {
  value: {platform: 'MacIntel'}, configurable: true,
});
const Align = require(process.argv[1] + '/align-v1.js');
const ShortcutRouter = require(process.argv[1] + '/shortcut-router-v1.js');
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
let tokenSeq = 0;
const document = {
  activeElement: null,
  documentElement: {style: {setProperty: (k, v) => {
    styleValues[k] = v; calls.push(['style',k,v]);
  }}},
};
function $(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    const attrs = {};
    const node = {
      id, hidden: false, disabled: false, style: {}, attrs, dataset: {}, children: [],
      naturalHeight: 100,
      classList: {
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        toggle: (name, force) => {
          const value = force === undefined ? !classes.has(name) : force;
          if (value) classes.add(name); else classes.delete(name);
          return value;
        },
        contains: name => classes.has(name),
      },
      setAttribute: (name, value) => attrs[name] = String(value),
      getAttribute: name => attrs[name] ?? null,
      getBoundingClientRect: () => ({height: node.hidden ? 0
        : node.style.height ? parseFloat(node.style.height) : node.naturalHeight}),
      focus: () => {document.activeElement = node; calls.push(['focus', id]);},
      contains: target => target?.id?.startsWith('compare-') &&
        target.id !== 'compare-tools-toggle' && target.id !== 'compare-toggle',
      querySelector: () => $('compare-orientation-target'),
      replaceChildren(...children) { this.children = children; },
      append(...children) { this.children.push(...children); },
    };
    nodes.set(id, node);
  }
  return nodes.get(id);
}
const originalPair = {mode: 'physical', transform: {a:1,b:0,c:0,d:1,tx:10,ty:20},
  orientation: {a:0,b:1,c:1,d:0,tx:0,ty:0}, landmarks: [{x:3,y:4}]};
const frames = new Map([['a', $('frame-a')], ['b', $('frame-b')]]);
const context = vm.createContext({
  $, document, calls, styleValues, frames, active: 'a', MAX_GROUP: 4,
  Align, ShortcutRouter, VIEWPORT_PROTOCOL_VERSION: 2, VIEWPORT_THROTTLE_MS: 48, LANDMARKS_NEEDED: 4,
  structuredClone, crypto: {randomUUID: () => `token-${++tokenSeq}`},
  readyFrames: new Set(['a','b','c']),
  window: {innerWidth: 1200, innerHeight: 900}, location: {origin: 'http://qa.invalid'},
  slides: [{sid:'a',name:'A'}, {sid:'b',name:'B'}, {sid:'c',name:'C'}],
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
// Exercise real lifecycle, state ownership, cancellation, memory and request
// guards. Adding a guard must not be papered over with a fixture-only stub.
for (const match of source.matchAll(/^function (\w+)\(/gm)) {
  vm.runInContext(production(match[1]), context);
}
const compareStart = source.indexOf('const compare = {');
vm.runInContext(source.slice(compareStart, source.indexOf('\n};',compareStart)+3),context);
const compare = vm.runInContext('compare',context);
Object.assign(compare, {
  enabled: true, toolsVisible: true, linked: true, toolbarHeight: 120,
  anchorSid: 'a', members: ['b'], orientationSid: 'b', split: 50,
  groupSessionId: 'group-tools-qa', groupEpoch: 1,
  anchorSet: {id:'anchor-set-tools',revision:0,points:[{id:'point-a',x:1,y:2}]},
  anchorLandmarks: [{id:'point-a',x:1,y:2}],
});
for (const sid of ['a','b','c']) {
  compare.states.set(sid, {
    sid, seq:1, paneInstanceId:'pane-'+sid, contextEpoch:1, imageReady:true,
    spatialContext:{key:'context-'+sid,kind:'slide',sourceGeneration:'gen-'+sid},
    centerPx:{x:200,y:150},imagePx:{x:1000,y:800},pixelSizeUm:{x:0.5,y:0.5},
    spanPx:{x:400,y:300},containerPx:{x:800,y:600},
  });
}
compare.pairs.set('b',Object.assign(context.newPair(),originalPair));
// Only external effects and unrelated DOM contents are replaced.
Object.assign(context, {
  closePairPicker: restore => calls.push(['closePicker',restore]),
  scheduleNativeGestureScopes: () => calls.push(['scopes']),
  postToSlide: (sid,message) => calls.push(['post',sid,message]),
  showError: message => calls.push(['error',message]),
  updateOrientationControls() {}, renderChips() {}, renderLandmarkPanel() {}, render() {},
  ensureFrame() {},
});
const realFinishLandmarks = context.finishLandmarks;
context.finishLandmarks = keep => {calls.push(['finishLandmarks',keep]); return realFinishLandmarks(keep);};
const realRequestGroupSoon = context.requestGroupSoon;
context.requestGroupSoon = kind => {calls.push(['requestGroupSoon',kind]); return realRequestGroupSoon(kind);};
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
      startLandmarks();
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


def test_hide_and_reopen_preserve_real_transaction_epoch_and_draft_identity():
    result = run("""
      changeOrientation('transpose');
      const pending = compare.pendingRequest;
      const epoch = compare.groupEpoch, session = compare.groupSessionId;
      setCompareToolsVisible(false);
      runTimers(80); // real layout timer must defer behind the real transaction
      setCompareToolsVisible(true);
      const actionPreserved = compare.pendingRequest === pending;
      clearPendingRequest();
      startLandmarks();
      const edit = compare.landmark.edit;
      const revision = compare.committedRevision;
      const memory = JSON.stringify([...compare.memory]);
      setCompareToolsVisible(false);
      setCompareToolsVisible(true);
      ({actionPreserved,kind:pending.kind,targets:pending.expectedTargets,
        sameEpoch:epoch===compare.groupEpoch,sameSession:session===compare.groupSessionId,
        sameEdit:edit===compare.landmark.edit,editActive:compare.landmark.active,
        unchangedCommitted:revision===compare.committedRevision,
        unchangedMemory:memory===JSON.stringify([...compare.memory])});
    """)
    assert result == {
        "actionPreserved": True, "kind": "orientation", "targets": ["a", "b"],
        "sameEpoch": True, "sameSession": True, "sameEdit": True, "editActive": True,
        "unchangedCommitted": True, "unchangedMemory": True,
    }


def test_link_button_stop_discards_hidden_draft_without_committing_it():
    result = run("""
      const original = JSON.stringify(compare.pairs.get('b'));
      startLandmarks();
      compare.landmark.edit.pairs.get('b').landmarks = [{id:'changed',x:99,y:88}];
      setCompareToolsVisible(false);
      const epoch = compare.groupEpoch;
      toggleCompare();
      const stored = compare.memory.get(JSON.stringify(['context-a','context-b']));
      ({enabled:compare.enabled,editActive:compare.landmark.active,
        newEpoch:compare.groupEpoch>epoch,storedCommitted:JSON.stringify(stored.pair)===original,
        calledCommit:calls.some(c=>c[0]==='finishLandmarks'&&c[1]===true),
        noPending:compare.pendingRequest===null});
    """)
    assert result == {
        "enabled": False, "editActive": False, "newEpoch": True, "storedCommitted": True,
        "calledCommit": False, "noPending": True,
    }


def test_landmark_status_wrapping_cannot_resize_canvases_during_one_edit():
    result = run("""
      let statusHeight = 260;
      renderLandmarkPanel = () => {
        $('compare-controls').naturalHeight = compare.landmark.active ? statusHeight : 100;
      };
      startLandmarks();
      const edit = compare.landmark.edit;
      const initial = styleValues['--compare-toolbar-height'];
      calls.length = 0;
      const heights = [];
      for (statusHeight of [275,240]) {
        updateCompareControls();
        heights.push(styleValues['--compare-toolbar-height']);
      }
      ({initial,heights,locked:edit.toolbarLock,
        outerHeight:$('compare-controls').style.height,
        overflow:$('compare-controls').style.overflowY,
        resizeWrites:calls.filter(c=>c[0]==='style'&&c[1]==='--compare-toolbar-height').length,
        layoutRequests:calls.filter(c=>c[0]==='requestGroupSoon').length,
        sameEdit:compare.landmark.edit===edit});
    """)
    assert result == {
        "initial": "280px", "heights": ["280px", "280px"],
        "locked": {"width": 1200, "height": 280}, "outerHeight": "260px",
        "overflow": "auto", "resizeWrites": 0, "layoutRequests": 0, "sameEdit": True,
    }


def test_hide_reopen_restores_locked_edit_height_and_real_width_resize_remeasures():
    result = run("""
      $('compare-controls').naturalHeight = 260;
      startLandmarks();
      const edit = compare.landmark.edit, epoch = compare.groupEpoch;
      $('compare-controls').naturalHeight = 275;
      setCompareToolsVisible(false);
      const hiddenHeight = styleValues['--compare-toolbar-height'];
      setCompareToolsVisible(true);
      const restored = styleValues['--compare-toolbar-height'];
      window.innerWidth = 900;
      $('compare-controls').naturalHeight = 315;
      calls.length = 0;
      updateCompareControls();
      ({hiddenHeight,restored,resized:styleValues['--compare-toolbar-height'],
        lock:edit.toolbarLock,outerHeight:$('compare-controls').style.height,
        layouts:calls.filter(c=>c[0]==='requestGroupSoon').length,
        sameEdit:compare.landmark.edit===edit,sameEpoch:compare.groupEpoch===epoch});
    """)
    assert result == {
        "hiddenHeight": "0px", "restored": "280px", "resized": "335px",
        "lock": {"width": 900, "height": 335}, "outerHeight": "315px", "layouts": 1,
        "sameEdit": True, "sameEpoch": True,
    }


def test_cancel_releases_toolbar_lock_and_next_edit_measures_its_own_panel():
    result = run("""
      let statusHeight = 260;
      renderLandmarkPanel = () => {
        $('compare-controls').naturalHeight = compare.landmark.active ? statusHeight : 100;
      };
      startLandmarks();
      const previous = compare.landmark.edit;
      finishLandmarks(false);
      const cancelled = {height:styleValues['--compare-toolbar-height'],
        style:$('compare-controls').style.height,overflow:$('compare-controls').style.overflowY};
      statusHeight = 220;
      startLandmarks();
      ({cancelled,newEdit:compare.landmark.edit!==previous,
        height:styleValues['--compare-toolbar-height'],lock:compare.landmark.edit.toolbarLock});
    """)
    assert result == {
        "cancelled": {"height": "120px", "style": "", "overflow": ""},
        "newEdit": True, "height": "240px", "lock": {"width": 1200, "height": 240},
    }


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
