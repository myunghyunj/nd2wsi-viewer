"""Exercise real update preparation/cancellation across shell and pane messages."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const root = process.argv[1], origin = 'http://qa.invalid';
const appSource = fs.readFileSync(root + '/app.js', 'utf8');
const shellSource = fs.readFileSync(root + '/shell-v1.js', 'utf8');
function production(source, name) {
  const match = new RegExp('^(?:async )?function ' + name + '\\(', 'm').exec(source);
  if (!match) throw Error('Missing production function: ' + name);
  return source.slice(match.index, source.indexOf('\n}', match.index) + 2);
}
function bindings(source) {
  const start = source.indexOf('window.addEventListener("message",');
  return source.slice(start, source.indexOf('\n});', start) + 4);
}
const messages = [], pendingMessages = [], timers = new Map(), panes = new Map();
const shellListeners = new Map(), frames = new Map(), classes = new Set(), notices = [];
let timerId = 0, scopeUpdates = 0;
const setTimeout = (callback, delay) => {
  const id = ++timerId;
  timers.set(id, {callback, delay});
  return id;
};
const clearTimeout = id => timers.delete(id);
function runTimers(delay) {
  for (const [id, timer] of [...timers]) if (timer.delay === delay && timers.has(id)) {
    timers.delete(id); timer.callback();
  }
}
async function pump() {
  for (let turn = 0; turn < 30; turn++) {
    for (const deliver of pendingMessages.splice(0)) deliver();
    await Promise.resolve();
  }
}
const shellWindow = {addEventListener(kind, callback) {
  if (!shellListeners.has(kind)) shellListeners.set(kind, []);
  shellListeners.get(kind).push(callback);
}};
function sendReady(sid, requestId, overrides = {}) {
  const event = {origin, source:frames.get(sid).contentWindow,
    data:{nd2wsi:'quit-ready', version:2, sid, requestId, ok:true, ...overrides}};
  for (const listener of shellListeners.get('message') || []) listener(event);
}
const shell = vm.createContext({
  window:shellWindow, location:{origin}, frames, readyFrames:new Set(['a', 'b']),
  busyTab:null, VIEWPORT_PROTOCOL_VERSION:2, notices, messages, panes, classes,
  setTimeout, clearTimeout, runTimers, pump, sendReady,
  document:{documentElement:{classList:{
    add:name=>classes.add(name), remove:name=>classes.delete(name),
    contains:name=>classes.has(name),
  }}},
  scheduleNativeGestureScopes:()=>scopeUpdates++,
  showError:message=>notices.push(message), slideName:sid=>sid,
});
const quitStart = shellSource.indexOf('let quitPreparation = null;');
vm.runInContext(shellSource.slice(quitStart, shellSource.indexOf('const pairPicker', quitStart)), shell);
vm.runInContext(production(shellSource, 'frameSidForSource'), shell);
const prepareStart = shellSource.indexOf('function postToSlide(');
vm.runInContext(shellSource.slice(prepareStart, shellSource.indexOf('function finitePoint(', prepareStart)), shell);
vm.runInContext(bindings(shellSource), shell);
for (const sid of ['a', 'b']) {
  const listeners = new Map(), elements = new Map(), writes = [], saves = [], statuses = [];
  const state = {
    annotations:[{id:'pin', type:'pin', x:10, y:20, text:'before'}],
    editingId:null, annDirty:false, annRevision:0, annContext:0,
    annSaveTail:Promise.resolve(), annFailedSaves:new Map(),
    quitPreparing:false, quitRequestId:null, plate:null,
    landmark:{active:false}, viewportRelay:{compare:null}, viewer:{addHandler(){}},
  };
  const element = id => {
    if (!elements.has(id)) elements.set(id, {hidden:true, value:'', addEventListener(){}});
    return elements.get(id);
  };
  const pane = {state, elements, writes, saves, statuses, holdSaves:false};
  const parent = {postMessage(data) {
    messages.push({from:sid, to:'shell', ...data});
    pendingMessages.push(()=>sendReady(sid, data.requestId, data));
  }};
  const paneWindow = {parent, addEventListener(kind, callback) {
    if (!listeners.has(kind)) listeners.set(kind, []);
    listeners.get(kind).push(callback);
  }};
  pane.receive = (data, overrides = {}) => {
    const event = {origin, source:parent, data, ...overrides};
    for (const listener of listeners.get('message') || []) listener(event);
  };
  const frameWindow = {postMessage(data) {
    messages.push({from:'shell', to:sid, ...data});
    pendingMessages.push(()=>pane.receive(data));
  }};
  frames.set(sid, {contentWindow:frameWindow});
  const context = vm.createContext({
    state, window:paneWindow, location:{origin}, VIEWPORT_PROTOCOL_VERSION:2,
    $:element, setTimeout, clearTimeout,
    renderAnnotations(){}, rebuildAnnList(){}, annLocked:()=>false,
    setAnnStatus:message=>statuses.push(message), basename:path=>path,
    currentSlideSid:()=>sid, refreshSpatialContext(){}, postSpatialReadiness(){},
    fetch:(_url, options) => new Promise(resolve => {
      const finish = () => {
        writes.push(JSON.parse(options.body));
        resolve({ok:true, json:async()=>({path:'qa-only-annotations.json'})});
      };
      if (pane.holdSaves) saves.push(finish);
      else finish();
    }),
  });
  for (const name of ['debounce', 'annotationsChanged', 'normalizeAnnotationSite',
    'annotationsUrl', 'annotationSaveEntry', 'queueAnnotationSave', 'saveAnnotations',
    'flushAnnotationsForUpdate', 'acknowledgeUpdatePreparation', 'cancelUpdatePreparation',
    'closeEditor', 'wireCompareRelay']) {
    vm.runInContext(production(appSource, name), context);
  }
  vm.runInContext('const scheduleAnnSave = debounce(saveAnnotations, 800); wireCompareRelay();', context);
  pane.edit = text => {
    state.editingId = 'pin';
    element('ann-editor').hidden = false;
    element('ann-text').value = text;
  };
  pane.inputBlocked = () => {
    const event = {prevented:false, preventDefault(){this.prevented=true;}, stopImmediatePropagation(){}};
    for (const listener of listeners.get('beforeinput') || []) listener(event);
    return event.prevented;
  };
  panes.set(sid, pane);
}
Promise.resolve(vm.runInContext('(async()=>{' + process.argv[2] + '})()', shell))
  .then(result=>process.stdout.write(JSON.stringify(result)))
  .catch(error=>{process.stderr.write(String(error.stack || error)); process.exitCode=1;});
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(STATIC), body],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_abort_unlocks_all_prepared_panes_without_discarding_an_inflight_annotation():
    out = run("""
      const a=panes.get('a'), b=panes.get('b');
      a.holdSaves=true; a.edit('unsaved native update marker');
      const pending=window.nd2wsiPrepareForUpdate('attempt-one');
      await pump();
      const before={blocked:[a.inputBlocked(),b.inputBlocked()],writes:a.writes.length};
      const accepted=window.nd2wsiCancelUpdate('attempt-one');
      const result=await pending;
      await pump();
      const after={blocked:[a.inputBlocked(),b.inputBlocked()],locked:classes.has('preparing-update'),
        text:a.state.annotations[0].text,dirty:a.state.annDirty};
      a.saves.shift()(); await pump();
      return {before,accepted,result,after,writes:a.writes,
        lateReady:messages.filter(m=>m.from==='a' && m.nd2wsi==='quit-ready')};
    """)
    assert out["before"] == {"blocked": [True, True], "writes": 0}
    assert out["accepted"] is True
    assert out["result"] == {"ok": False, "cancelled": True, "error": "update cancelled"}
    assert out["after"] == {
        "blocked": [False, False], "locked": False,
        "text": "unsaved native update marker", "dirty": True,
    }
    assert out["writes"][0]["items"][0]["text"] == "unsaved native update marker"
    assert out["lateReady"] == []


def test_abort_after_successful_flush_still_unlocks_and_reports_closed_images():
    out = run("""
      const pending=window.nd2wsiPrepareForUpdate('finished-attempt');
      await pump(); const result=await pending;
      const accepted=window.nd2wsiCancelUpdate('finished-attempt',true);
      await pump();
      return {result,accepted,notices,locked:classes.has('preparing-update'),
        blocked:[...panes.values()].map(p=>p.inputBlocked())};
    """)
    assert out["result"] == {"ok": True, "panes": 2}
    assert out["accepted"] is True
    assert out["locked"] is False
    assert out["blocked"] == [False, False]
    assert out["notices"] == [
        "Update stopped after saving annotations and releasing images. "
        "Reopen the slide or restart the app."
    ]


def test_busy_retry_cancellation_unlocks_panes_prepared_by_the_earlier_attempt():
    out = run("""
      const first=window.nd2wsiPrepareForUpdate('older-attempt'); await pump(); await first;
      busyTab='opening';
      const retry=await window.nd2wsiPrepareForUpdate('latest-attempt');
      const stale=window.nd2wsiCancelUpdate('older-attempt',true);
      const current=window.nd2wsiCancelUpdate('latest-attempt'); await pump();
      return {retry,stale,current,notices,
        blocked:[...panes.values()].map(p=>p.inputBlocked()),
        cancelled:messages.filter(m=>m.nd2wsi==='cancel-quit').map(m=>[m.to,m.requestId])};
    """)
    assert out["retry"] == {"ok": False, "error": "a slide is still opening"}
    assert out["stale"] is False
    assert out["current"] is True
    assert out["notices"] == []
    assert out["blocked"] == [False, False]
    assert out["cancelled"] == [["a", "older-attempt"], ["b", "older-attempt"]]


def test_stale_cancel_and_late_ready_cannot_finish_or_unlock_a_new_attempt():
    out = run("""
      const a=panes.get('a'); a.holdSaves=true; a.edit('still saving');
      const first=window.nd2wsiPrepareForUpdate('first'); await pump();
      const second=window.nd2wsiPrepareForUpdate('second'); await pump();
      const oldResult=await first;
      const stale=window.nd2wsiCancelUpdate('first',true);
      a.receive({nd2wsi:'cancel-quit',version:2,requestId:'first'});
      sendReady('a','first');
      const before={pending:quitPreparation.requestId,blocked:a.inputBlocked(),
        id:a.state.quitRequestId,locked:classes.has('preparing-update')};
      window.nd2wsiCancelUpdate('second'); await pump();
      return {oldResult,stale,before,result:await second,notices};
    """)
    assert out["oldResult"] == {"ok": False, "error": "update preparation restarted"}
    assert out["stale"] is False
    assert out["before"] == {"pending": "second", "blocked": True, "id": "second", "locked": True}
    assert out["result"]["cancelled"] is True
    assert out["notices"] == []


@pytest.mark.parametrize("override", [
    "{origin:'https://unrelated.invalid'}", "{source:{}}", "{data:{nd2wsi:'cancel-quit',version:1,requestId:'current'}}",
])
def test_pane_cancellation_requires_the_current_parent_and_protocol(override):
    out = run("""
      const pending=window.nd2wsiPrepareForUpdate('current'); await pump(); await pending;
      const a=panes.get('a');
      a.receive({nd2wsi:'cancel-quit',version:2,requestId:'current'}, OVERRIDE);
      return {blocked:a.inputBlocked(),id:a.state.quitRequestId};
    """.replace("OVERRIDE", override))
    assert out == {"blocked": True, "id": "current"}


def test_cancel_after_timeout_clears_input_locks_and_late_readiness_is_ignored():
    out = run("""
      const a=panes.get('a'); a.holdSaves=true; a.edit('saved after timeout');
      const pending=window.nd2wsiPrepareForUpdate('timed-out'); await pump();
      runTimers(8000); const result=await pending;
      const accepted=window.nd2wsiCancelUpdate('timed-out'); await pump();
      a.saves.shift()(); await pump();
      sendReady('a','timed-out');
      return {result,accepted,pending:quitPreparation,locked:classes.has('preparing-update'),
        blocked:a.inputBlocked(),text:a.writes[0].items[0].text};
    """)
    assert out["result"] == {"ok": False, "error": "save confirmation timed out for 1 pane(s)"}
    assert out["accepted"] is True
    assert out["pending"] is None
    assert out["locked"] is False
    assert out["blocked"] is False
    assert out["text"] == "saved after timeout"
