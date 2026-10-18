"""Shading preparation preserves source identity and pane-local display choice."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs=require('fs'),vm=require('vm'),S=require(process.argv[1]+'/shading-correction-v1.js');
const flush=async()=>{for(let n=0;n<16;n++)await Promise.resolve();};
function fixture(){
  let context={sourceId:'a',generation:'g1',frame:null,key:'a:g1'},nextTimer=0;
  const requests=[],timers=new Map(),states=[],changes=[],errors=[],notices=[];
  const api=S.createController({readContext:()=>context,
    fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,reject,
      resolve:(data,ok=true)=>resolve({ok,status:ok?200:500,json:async()=>data})})),
    setTimer:fn=>{const id=++nextTimer;timers.set(id,fn);return id},clearTimer:id=>timers.delete(id),
    onState:s=>states.push(s),onChange:id=>changes.push(id),onError:e=>errors.push(e),onNotice:n=>notices.push(n)});
  const payload=(state,extra={})=>({state,generation:'g1',supported:true,reason:'',pct:0,
    correction_id:state==='ready'?'h-1':null,channels:state==='ready'?['FITC']:[],...extra});
  function tick(){const tasks=[...timers.values()];timers.clear();tasks.forEach(fn=>fn());}
  return {api,requests,timers,states,changes,errors,notices,payload,tick,setContext:c=>{context=c}};
}
function production(source,name){const at=source.indexOf('function '+name+'(');if(at<0)throw Error(name);return source.slice(at,source.indexOf('\n}',at)+2);}
function shellFixture(){
  const source=fs.readFileSync(process.argv[1]+'/shell-v1.js','utf8');
  const nodes=new Map(),messages=[];
  function $(id){if(!nodes.has(id))nodes.set(id,{attrs:{},open:false,focus(){},showModal(){this.open=true},close(){this.open=false},getBoundingClientRect:()=>({left:10,right:300,top:10,bottom:200}),classList:{toggle(){}},setAttribute(k,v){this.attrs[k]=v}});return nodes.get(id);}
  const c=vm.createContext({$,window:{Nd2ShadingCorrection:S},shadingStates:new Map(),shadingTokens:new Map([['a','ta'],['b','tb']]),
    active:'a',readyFrames:new Set(['a','b']),quitPreparation:null,shadingCommandSeq:0,shadingConfirmation:null,VIEWPORT_PROTOCOL_VERSION:2,
    postToSlide:(sid,data)=>{messages.push({sid,data});return true}});
  for(const name of ['refreshShadingButton','receiveShadingState','activateShadingCorrection','shadingConfirmationMatches','closeShadingConfirmation','continueShadingConfirmation','dismissShadingConfirmation'])vm.runInContext(production(source,name),c);
  function status(sid,extra={}){return {sid,shellToken:'t'+sid,paneInstanceId:'pane-'+sid,seq:1,
    state:'idle',supported:true,enabled:false,channels:[],generation:'g1',estimated_seconds:225,contextKey:sid+':g1',...extra};}
  return {c,$,messages,status};
}
(async()=>{
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", SCRIPT + body + "\n})().then(r=>process.stdout.write(JSON.stringify(r))).catch(e=>{console.error(e);process.exit(1)});", str(STATIC)],
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_preparation_progress_then_toggle_changes_only_render_identity():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();
      f.api.activate('a:g1',true);f.requests[1].resolve(f.payload('fitting',{pct:23,eta_seconds:91}));await flush();
      const progress=S.progress(f.api.snapshot());f.tick();
      f.requests[2].resolve(f.payload('ready',{pct:100}));await flush();
      const corrected=f.api.appendRenderParams(new URLSearchParams('win=12:200:0.7&g=g1')).toString();
      const enabled=f.api.snapshot().enabled;
      await f.api.activate('a:g1');
      const raw=f.api.appendRenderParams(new URLSearchParams('win=12:200:0.7&g=g1')).toString();
      await f.api.activate('a:g1');
      return {progress,enabled,corrected,raw,changes:f.changes,requests:f.requests.map(r=>[r.url,r.options.method||'GET']),timers:f.timers.size};
    """)
    assert out["progress"] == {"visible": True, "stage": "Estimating illumination", "pct": 23, "eta": "About 2 min remaining"}
    assert out["enabled"] is True
    assert out["corrected"] == "win=12%3A200%3A0.7&g=g1&correction=h-1"
    assert out["raw"] == "win=12%3A200%3A0.7&g=g1"
    assert out["changes"] == ["h-1", None, "h-1"]
    assert out["requests"] == [["api/shading/status", "GET"], ["api/shading/start", "POST"], ["api/shading/status", "GET"]]
    assert out["timers"] == 0


def test_preparation_requires_explicit_continue_before_any_start_request():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();
      const unconfirmed=await f.api.activate('a:g1');const before=f.requests.length;
      f.api.activate('a:g1',true);
      return {unconfirmed,before,urls:f.requests.map(r=>r.url)};
    """)
    assert out == {"unconfirmed": False, "before": 1, "urls": ["api/shading/status", "api/shading/start"]}


def test_confirmation_continue_targets_captured_pane_once_after_showing_estimate():
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a'));
      c.activateShadingCorrection();
      const before={open:f.$('shading-confirm').open,estimate:f.$('shading-confirm-estimate').textContent,
        messages:f.messages.length,sequence:c.shadingCommandSeq};
      const first=c.continueShadingConfirmation(),second=c.continueShadingConfirmation();
      return {before,first,second,open:f.$('shading-confirm').open,messages:f.messages};
    """)
    assert out["before"] == {"open": True, "estimate": "Est. time: about 4 min", "messages": 0, "sequence": 0}
    assert out["first"] is True and out["second"] is False and out["open"] is False
    assert len(out["messages"]) == 1
    command = out["messages"][0]
    assert command["sid"] == "a" and command["data"]["confirmedStart"] is True
    assert command["data"]["generation"] == "g1" and command["data"]["contextKey"] == "a:g1"


@pytest.mark.parametrize("dismiss", ["c.closeShadingConfirmation()",
    "c.dismissShadingConfirmation({type:'cancel',preventDefault(){}})",
    "c.dismissShadingConfirmation({type:'click',target:f.$('shading-confirm'),clientX:0,clientY:0})"])
def test_cancel_escape_and_outside_dismiss_without_sending_a_start(dismiss):
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a'));c.activateShadingCorrection();
      DISMISS;c.continueShadingConfirmation();
      return {open:f.$('shading-confirm').open,pending:c.shadingConfirmation,messages:f.messages,sequence:c.shadingCommandSeq};
    """.replace("DISMISS", dismiss))
    assert out == {"open": False, "pending": None, "messages": [], "sequence": 0}


@pytest.mark.parametrize("change", ["c.active='b'",
    "c.receiveShadingState('a',f.status('a',{seq:2,contextKey:'a:g1:frame2'}))",
    "c.receiveShadingState('a',f.status('a',{seq:2,generation:'g2'}))",
    "c.shadingTokens.set('a','reloaded-pane')"])
def test_changed_active_pane_frame_generation_or_lifetime_invalidates_confirmation(change):
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a'));c.activateShadingCorrection();
      CHANGE;c.refreshShadingButton();const continued=c.continueShadingConfirmation();
      return {continued,open:f.$('shading-confirm').open,messages:f.messages,pending:c.shadingConfirmation};
    """.replace("CHANGE", change))
    assert out == {"continued": False, "open": False, "messages": [], "pending": None}


@pytest.mark.parametrize("state", ["ready", "fitting"])
def test_ready_toggle_and_busy_cancel_remain_immediate(state):
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a',{state:'STATE',channels:['FITC']}));
      c.activateShadingCorrection();return {open:f.$('shading-confirm').open,messages:f.messages.length,
        confirmed:f.messages[0]?.data.confirmedStart===true};
    """.replace("STATE", state))
    assert out == {"open": False, "messages": 1, "confirmed": False}


def test_existing_or_shared_preparation_never_enables_another_pane():
    out = run("""
      const a=fixture(),b=fixture();a.api.load();b.api.load();
      a.requests[0].resolve(a.payload('ready'));b.requests[0].resolve(b.payload('preparing'));await flush();
      b.tick();b.requests[1].resolve(b.payload('ready'));await flush();
      const before=[a.api.snapshot().enabled,b.api.snapshot().enabled];await a.api.activate('a:g1');
      return {before,after:[a.api.snapshot().enabled,b.api.snapshot().enabled],changes:[a.changes,b.changes]};
    """)
    assert out == {"before": [False, False], "after": [True, False], "changes": [["h-1"], []]}


@pytest.mark.parametrize("failure", ["f.requests[1].reject(Error('Disk full'))", "f.requests[1].resolve(f.payload('failed',{reason:'Fit failed'}))", "f.requests[1].resolve(f.payload('ready',{generation:'old'}))"])
def test_failed_or_wrong_generation_preparation_keeps_original_and_can_retry(failure):
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();f.api.activate('a:g1',true);
      FAILURE;await flush();const failed=f.api.snapshot();
      const raw=f.api.appendRenderParams(new URLSearchParams()).toString();f.api.activate('a:g1',true);
      return {state:failed.state,enabled:failed.enabled,raw,errors:f.errors.length,changes:f.changes,retry:f.requests[2].url};
    """.replace("FAILURE", failure))
    assert out == {"state": "failed", "enabled": False, "raw": "", "errors": 1, "changes": [], "retry": "api/shading/start"}


def test_changed_source_and_disposed_pane_cannot_accept_delayed_ready():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();f.api.activate('a:g1',true);
      f.setContext({sourceId:'a',generation:'g2',frame:null,key:'a:g2'});f.api.syncContext();
      f.requests[1].resolve(f.payload('ready'));f.requests[2].resolve(f.payload('idle',{generation:'g2'}));await flush();
      const afterChange=f.api.snapshot();f.api.activate('a:g2',true);f.api.dispose();
      f.requests[3].resolve(f.payload('ready',{generation:'g2'}));await flush();
      return {afterChange,changes:f.changes,aborted:[f.requests[1].options.signal.aborted,f.requests[3].options.signal.aborted],timers:f.timers.size};
    """)
    assert out["afterChange"]["state"] == "idle" and out["afterChange"]["enabled"] is False
    assert out["changes"] == [] and out["aborted"] == [True, True] and out["timers"] == 0


def test_cancel_ignores_inflight_poll_and_zero_corrected_channels_stay_original():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();f.api.activate('a:g1',true);
      f.requests[1].resolve(f.payload('fitting'));await flush();f.tick();f.api.activate('a:g1');
      f.requests[3].resolve(f.payload('cancelled'));await flush();f.requests[2].resolve(f.payload('ready'));await flush();
      const cancelled=f.api.snapshot();f.api.activate('a:g1',true);f.requests[4].resolve(f.payload('ready',{channels:[]}));await flush();
      return {cancelled:cancelled.state,state:f.api.snapshot().state,enabled:f.api.snapshot().enabled,changes:f.changes,timers:f.timers.size};
    """)
    assert out == {"cancelled": "cancelled", "state": "unchanged", "enabled": False, "changes": [], "timers": 0}


@pytest.mark.parametrize("state,outcome", [("unchanged", "image unchanged"), ("unsupported", "unavailable for this image")])
def test_initiating_pane_sees_informational_terminal_outcome_without_initial_status_toast(state, outcome):
    out = run("""
      const initial=fixture(),started=fixture();initial.api.load();started.api.load();
      initial.requests[0].resolve(initial.payload('TERMINAL',{reason:'No usable correction',supported:false}));
      started.requests[0].resolve(started.payload('idle'));await flush();started.api.activate('a:g1',true);
      started.requests[1].resolve(started.payload('fitting'));await flush();started.tick();
      started.requests[2].resolve(started.payload('TERMINAL',{reason:'No usable correction',supported:false}));await flush();
      return {initial:initial.notices,notices:started.notices,errors:started.errors,enabled:started.api.snapshot().enabled};
    """.replace("TERMINAL", state))
    assert out == {"initial": [], "notices": [f"Shading Correction: {outcome}. No usable correction"],
                   "errors": [], "enabled": False}


def test_shell_switches_status_and_click_target_without_stale_pane_updates():
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a'));c.receiveShadingState('b',f.status('b'));
      c.activateShadingCorrection();c.continueShadingConfirmation();c.active='b';c.refreshShadingButton();
      c.receiveShadingState('a',f.status('a',{seq:2,state:'fitting',pct:23}));
      const b={label:f.$('shading-correction').title,progress:f.$('shading-progress').hidden};
      const rejected=[c.receiveShadingState('b',f.status('b',{seq:100,shellToken:'old'})),c.receiveShadingState('b',f.status('b',{seq:0}))];
      c.activateShadingCorrection();c.continueShadingConfirmation();c.active='a';c.refreshShadingButton();
      return {targets:f.messages.map(m=>m.sid),b,rejected,a:{hidden:f.$('shading-progress').hidden,pct:f.$('shading-progress-bar').value,eta:f.$('shading-eta').textContent}};
    """)
    assert out == {"targets": ["a", "b"], "b": {"label": "Apply Shading Correction", "progress": True},
                   "rejected": [False, False], "a": {"hidden": False, "pct": 23, "eta": "Estimating time…"}}


def test_delayed_pane_initialization_recovers_lost_load_handshake_without_resetting_live_status():
    out = run(r"""
      const f=shellFixture(),c=f.c,origin='http://viewer.test',child={},listeners=[];
      const shellSource=fs.readFileSync(process.argv[1]+'/shell-v1.js','utf8');
      const paneSource=fs.readFileSync(process.argv[1]+'/app.js','utf8');
      let installed=false,tokens=0,commands=0;
      c.location={origin};c.crypto={randomUUID:()=> 'handshake-'+(++tokens)};
      c.frameSidForSource=source=>source===child?'a':null;
      c.receiveViewportState=()=>true;c.paneCameUp=()=>{};
      c.window.addEventListener=(kind,listener)=>{if(kind==='message')listeners.push(listener)};
      const pane=vm.createContext({
        state:{shading:null,shadingShellToken:null,shadingSeq:0,quitPreparing:false,
          viewportRelay:{commandGate:{paneInstanceId:'pane-a'}}},
        window:{parent:{postMessage:data=>listeners.forEach(listener=>listener({origin,source:child,data}))}},
        location:{origin},VIEWPORT_PROTOCOL_VERSION:2,currentSlideSid:()=> 'a',spatialImageReady:()=>true,
      });
      for(const name of ['postShadingState','receiveShadingCommand'])vm.runInContext(production(paneSource,name),pane);
      c.postToSlide=(sid,data)=>{commands++;if(installed)pane.receiveShadingCommand(data);return true};
      vm.runInContext(production(shellSource,'requestShadingState'),c);
      const at=shellSource.indexOf('window.addEventListener("message",');
      vm.runInContext(shellSource.slice(at,shellSource.indexOf('\n});',at)+4),c);
      c.requestShadingState('a');const loadToken=c.shadingTokens.get('a');
      // The first command arrived before wireCompareRelay; local status cannot
      // publish without its shell token even after asynchronous init finishes.
      pane.state.shading={snapshot:()=>({state:'idle',supported:true,enabled:false,channels:[],contextKey:'a:g1'})};
      pane.postShadingState();const before=c.shadingStates.has('a');installed=true;
      const ready={origin,source:child,data:{nd2wsi:'viewport-ready',version:2,sid:'a'}};
      listeners.forEach(listener=>listener(ready));
      const recovered=c.shadingStates.get('a'),afterCommands=commands;
      // Later readiness events must retain status identity and existing RAW/H state.
      recovered.enabled=true;listeners.forEach(listener=>listener(ready));
      return {before,recovered:!!recovered,enabled:c.shadingStates.get('a').enabled,
        sameStatus:c.shadingStates.get('a')===recovered,sameToken:c.shadingTokens.get('a')===loadToken,
        commands,afterCommands,tokens};
    """)
    assert out == {"before": False, "recovered": True, "enabled": True, "sameStatus": True,
                   "sameToken": True, "commands": 2, "afterCommands": 2, "tokens": 1}


def test_unsupported_absent_or_blocked_pane_cannot_start_and_eta_is_approximate():
    out = run("""
      const f=shellFixture(),c=f.c;c.refreshShadingButton();const absent=f.$('shading-correction').disabled;
      c.receiveShadingState('a',f.status('a',{state:'unsupported',supported:false,reason:'RGB images are unsupported'}));
      const unsupported={disabled:f.$('shading-correction').disabled,label:f.$('shading-correction').title};c.activateShadingCorrection();
      c.receiveShadingState('a',f.status('a',{seq:2,blocked:true}));c.activateShadingCorrection();
      return {absent,unsupported,messages:f.messages.length,unknown:S.progress({state:'fitting',pct:10,eta_seconds:null}).eta,
        known:S.progress({state:'preparing_display',pct:95,eta_seconds:13}).eta};
    """)
    assert out == {"absent": True, "unsupported": {"disabled": True, "label": "Shading Correction unavailable: RGB images are unsupported"},
                   "messages": 0, "unknown": "Estimating time…", "known": "About 15 sec remaining"}


def test_module_and_selected_upstream_icon_are_loaded_in_both_shell_and_pane():
    shell = (STATIC / "shell.html").read_text()
    pane = (STATIC / "index.html").read_text()
    css = (STATIC / "native-shell-v1.css").read_text()
    assert shell.index("shading-correction-v1.js") < shell.index("shell-v1.js")
    assert pane.index("shading-correction-v1.js") < pane.index("app.js")
    assert 'id="update-check"' not in shell
    assert 'id="shading-progress-bar"' in shell
    assert 'Shadow correction best works in large scans.' in shell
    assert 'aria-describedby="shading-confirm-message shading-confirm-estimate"' in shell
    assert '#shading-confirm-continue { background:#248a3d; }' in css
    assert '#shading-confirm-cancel { background:#d72c30; }' in css
    assert '#shading-correction:hover:not(:disabled) { color:#ff453a; }' in css
    manifest = json.loads((STATIC / "icons/fluent-icons.json").read_text())
    name = "fluent-circle-highlight-20-regular.svg"
    assert name in css and "Circle%20Highlight" in manifest["assets"][name]
    assert (STATIC / "icons" / name).read_text().startswith('<svg width="20" height="20"')
