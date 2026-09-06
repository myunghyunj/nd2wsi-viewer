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
const messages = [];
const styleValues = {};
const timers = new Map();
const listeners = new Map();
let timerSeq = 0;
let tokenSeq = 0;
const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {
      id, style:{}, dataset:{}, hidden:false, disabled:false, textContent:'', value:'',
      children:[], options:[], classList:{
        add:(...values)=>values.forEach(value=>classes.add(value)),
        remove:(...values)=>values.forEach(value=>classes.delete(value)),
        contains:value=>classes.has(value),
        toggle:(value,enabled)=>enabled ? classes.add(value) : classes.delete(value),
      },
      getBoundingClientRect:()=>({height:100,width:800,left:0,top:0}),
      setAttribute(){}, removeAttribute(){}, focus(){}, blur(){}, remove(){},
      replaceChildren(...children){this.children=children;},
      append(...children){this.children.push(...children);},
      appendChild(child){this.children.push(child);}, querySelectorAll:()=>[],
      contains:()=>false,
    });
  }
  return elements.get(id);
};
const context = vm.createContext({
  Align, messages, styleValues, VIEWPORT_PROTOCOL_VERSION:2, VIEWPORT_THROTTLE_MS:48,
  LANDMARKS_NEEDED:4, MAX_GROUP:4, structuredClone, crypto:{randomUUID:()=>`token-${++tokenSeq}`},
  document:{documentElement:{style:{setProperty:(key,value)=>styleValues[key]=value}},
    activeElement:null, createElement:()=>element(`created-${++tokenSeq}`)},
  window:{innerWidth:1200,innerHeight:900,addEventListener:(kind,fn)=>{
    if (!listeners.has(kind)) listeners.set(kind,[]);
    listeners.get(kind).push(fn);
  }}, location:{origin:'http://qa.invalid'},
  frames:new Map(), readyFrames:new Set(['a','b','c']),
  slides:['a','b','c'].map(sid=>({sid,name:sid+'.nd2',path:'/'+sid+'.nd2'})),
  active:'a', pairPicker:{open:false,mode:'start',replaceSid:null},
  $:element,
  setTimeout: (fn, delay) => {const id=++timerSeq; timers.set(id,{fn,delay}); return id;},
  clearTimeout: id => timers.delete(id),
  runTimers: delay => {
    for (const [id, timer] of [...timers]) if (timer.delay === delay && timers.has(id)) {
      timers.delete(id); timer.fn();
    }
  },
  getTimerCallbacks:()=>[...timers.values()].map(timer=>timer.fn),
});
// Load every production function, so a new transaction guard cannot silently
// become an untested stub just because the old fixture did not know its name.
for (const match of source.matchAll(/^function (\w+)\(/gm)) {
  vm.runInContext(production(match[1]), context);
}
const compareStart = source.indexOf('const compare = {');
vm.runInContext(source.slice(compareStart, source.indexOf('\n};',compareStart)+3),context);
const compare = vm.runInContext('compare',context);
Object.assign(compare, {
  enabled:true, toolsVisible:true, anchorSid:'a', members:['b','c'], orientationSid:'b',
  groupSessionId:'group-qa',groupEpoch:1,committedRevision:0,
  anchorSet:{id:'anchor-set-qa',revision:0,points:[]},
});
// Only browser/UI boundaries are replaced. Geometry, identity validation,
// timers, transaction replies, fitting, commit and cancellation remain real.
context.renderRealCompareControls=context.updateCompareControls;
Object.assign(context, {
  postToSlide:(sid,message)=>messages.push({sid,...message}),
  showError:message=>messages.push({error:message}),
  updateCompareControls(){}, updateOrientationControls(){},
  render(){}, applyFrameLayout(){}, renderPairPicker(){},
  sendTabShortcutState(){}, scheduleNativeGestureScopes(){},
  closePairPicker(){}, ensureFrame(){}, activate:sid=>{context.active=sid;},
});
const realSyncFromAnchor = context.syncFromAnchor;
context.syncFromAnchor = () => {messages.push({sync:true}); realSyncFromAnchor();};
const realRequestGroupSoon = context.requestGroupSoon;
context.requestGroupSoon = kind => {
  messages.push({deferred:kind}); realRequestGroupSoon(kind);
};
for (const [sid, pixel, center] of [
  ['a', 0.25, {x:321, y:211}], ['b', 0.66, {x:620, y:390}],
  ['c', 0.5, {x:200, y:310}],
]) {
  compare.states.set(sid, {
    sid,seq:1,paneInstanceId:`pane-${sid}`,contextEpoch:1,imageReady:true,
    spatialContext:{key:`context-${sid}`,kind:'slide',sourceGeneration:'gen-'+sid},
    centerPx: center, imagePx: {x:1000, y:800}, pixelSizeUm: {x:pixel, y:pixel},
    spanPx: {x:400, y:300}, containerPx: {x:800, y:600},
  });
  context.frames.set(sid,{dataset:{sid},style:{},contentWindow:{}});
}
for (const sid of compare.members) {
  compare.pairs.set(sid, context.newPair('a.svs', sid + '.nd2'));
  context.ensurePairTransform(sid);
}
// Send frozen snapshot replies through the same production entry point as
// browser postMessage. Calling finishGroupRequest with no responses would
// bypass the very validation these regressions are intended to exercise.
context.replyAll = (pending=compare.pendingRequest, replacements={}) => {
  if (!pending) return;
  const targets = pending.expectedTargets || [compare.anchorSid,...compare.members];
  for (const sid of targets) {
    const snapshot = structuredClone(compare.states.get(sid));
    context.receiveViewportState({
      ...snapshot, ...pending.contexts?.get(sid), ...replacements[sid],
      version:2, requestId:pending.requestId,
      groupSessionId:pending.groupSessionId || compare.groupSessionId,
      groupEpoch:pending.groupEpoch ?? compare.groupEpoch,
      reason:'request', seq:(snapshot?.seq || 0)+1,
    },sid);
  }
};
const pointRevisions = new Map();
context.landmarkMessage = (sid,points) => {
  const mode=messages.filter(m=>m.sid===sid && m.nd2wsi==='landmark-mode').at(-1);
  if (!mode?.active) throw new Error(`No active production landmark-mode for ${sid}`);
  const revision=(pointRevisions.get(sid) || 0)+1;
  pointRevisions.set(sid,revision);
  return {...mode,nd2wsi:'landmark-points',pointRevision:revision,
    points:points.map((p,index)=>({id:`${sid}-point-${index+1}`,...p}))};
};
context.putLandmarks = (sid,points) => {
  const message=context.landmarkMessage(sid,points);
  context.receiveLandmarkPoints(sid,message);
  return message;
};
context.squarePoints=[{x:100,y:100},{x:400,y:100},{x:400,y:400},{x:100,y:400}];
context.fitAll = () => {
  context.startLandmarks();
  for (const sid of [compare.anchorSid,...compare.members]) {
    context.putLandmarks(sid,context.squarePoints);
  }
  return context.commitLandmarkEdit(compare.landmark.edit.editId);
};
// Execute real button, keyboard, and message dispatch bindings too: a disabled
// Done button alone must not stand in for the shared atomic commit gate.
const bindingStart=source.indexOf('$("compare-orientation-target").onchange');
const bindingEnd=source.indexOf('$("compare-picker").addEventListener',bindingStart);
vm.runInContext(source.slice(bindingStart,bindingEnd),context);
const messageStart=source.indexOf('window.addEventListener("message",');
vm.runInContext(source.slice(messageStart,source.indexOf('\n});',messageStart)+4),context);
context.clickControl=id=>element(id).onclick();
context.changeControl=(id,value)=>element(id).onchange({target:{value}});
context.pressKey=key=>{
  for (const listener of listeners.get('keydown') || []) listener({
    key,repeat:false,metaKey:false,ctrlKey:false,altKey:false,
    target:{closest:()=>null},preventDefault(){},
  });
};
context.paneMessage=(sid,data)=>{
  for (const listener of listeners.get('message') || []) listener({
    source:context.frames.get(sid).contentWindow,origin:'http://qa.invalid',data,
  });
};
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
      replyAll(pending);
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
        changeOrientation('{action}'); replyAll();
      }}
      const restored = displayTransformFor('b');
      changeOrientation('rotate-left'); replyAll();
      changeOrientation('reset'); replyAll();
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
      replyAll(pending);
      ({preserved, pose:displayTransformFor('b'),
        deferred:messages.filter(m => m.deferred).map(m => m.deferred)});
    """)
    assert result == {
        "preserved": True, "pose": {"degrees": 90, "flipped": True},
        "deferred": ["sync"],
    }


def test_pane_reload_invalidates_old_target_action_and_request_identity():
    result = run("""
      changeOrientation('transpose');
      const pending = compare.pendingRequest;
      const oldSnapshot={...compare.states.get('b')};
      receiveViewportState({...oldSnapshot,nd2wsi:'viewport-ready',version:2,
        paneInstanceId:'pane-b-reloaded',seq:1},'b');
      paneCameUp('b');
      replyAll(pending);
      ({cancelled:compare.pendingRequest!==pending,
        instance:compare.states.get('b').paneInstanceId,
        action:pending.action,target:pending.targetSid,pose:displayTransformFor('b')});
    """)
    assert result["cancelled"] is True
    assert result["instance"] == "pane-b-reloaded"
    assert result["action"] == "transpose"
    assert result["target"] == "b"
    assert result["pose"] == {"degrees": 0, "flipped": False}


def test_deferred_real_layout_timer_runs_after_orientation_completion():
    result = run("""
      changeOrientation('transpose');
      const pending = compare.pendingRequest;
      requestGroupSoon('sync');
      runTimers(80); // a resize while the action awaits viewport replies
      const samePending = compare.pendingRequest === pending;
      replyAll(pending);
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
      const from=[{id:'b1',x:0,y:0},{id:'b2',x:10,y:0},
        {id:'b3',x:10,y:10},{id:'b4',x:0,y:10}];
      const to=from.map((p,i)=>({...Align.apply(transform,p),id:'a'+(i+1)}));
      for(const p of to) p.x+=10;
      const oldAnchor={id:'set-b',revision:1,points:from};
      const oldPair=newPair();
      Object.assign(oldPair,{mode:'physical',orientation,transform,fitTransform:transform,
        fit:{transform,angleDeg:Align.angleDeg(transform),scale:2,rms:10,reflected:true},
        landmarks:to,landmarkSet:{id:'set-a',revision:2,points:to},
        provenance:{anchorContext:'context-b',memberContext:'context-a',
          anchorSetId:'set-b',anchorRevision:1,memberSetId:'set-a',memberRevision:2,
          from,to,anchorPointIds:from.map(p=>p.id),memberPointIds:to.map(p=>p.id)}});
      compare.memory.set(pairKey('b','a'),{pair:oldPair,anchorSet:oldAnchor,
        anchorContext:'context-b',memberContext:'context-a'});
      const pair = newPair();
      restoreAlignment('a', 'b', pair);
      ({orientation:pair.orientation, expected:Align.invert(orientation),
        angle:pair.fit.angleDeg, expectedAngle:Align.angleDeg(pair.transform),
        scale:pair.fit.scale, rms:pair.fit.rms,
        anchorIds:pair.provenance.anchorPointIds,memberIds:pair.provenance.memberPointIds,
        fitTransformConsistent:JSON.stringify(pair.fit.transform)===JSON.stringify(pair.fitTransform)});
    """)
    assert result["orientation"] == result["expected"]
    assert result["angle"] == result["expectedAngle"]
    assert result["scale"] == .5
    assert result["rms"] == 5
    assert result["anchorIds"] == ["a1", "a2", "a3", "a4"]
    assert result["memberIds"] == ["b1", "b2", "b3", "b4"]
    assert result["fitTransformConsistent"] is True


def test_visible_orientation_controls_are_wired_and_explained():
    html = (STATIC / "shell.html").read_text()
    shell = (STATIC / "shell-v1.js").read_text()
    for control in ("flip-horizontal", "flip-vertical", "rotate-left", "rotate-right", "transpose"):
        assert f'id="compare-{control}"' in html
        assert f'"compare-{control}": "{control}"' in shell
    assert 'id="compare-orientation-target" aria-label="Active linked slide"' in html
    assert '"compare-orientation-reset": "reset"' in shell
    assert '$(id).onclick = () => changeOrientation(action)' in shell
    assert 'Remove Fit' in html + shell
    assert 'Clear Points' in html + shell
    assert 'Keep mirror state' in html + shell
    assert 'Transpose' in (ROOT / "README.md").read_text()
