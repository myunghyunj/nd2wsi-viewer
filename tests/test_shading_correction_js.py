"""Persistent correction, explicit revocation and source-safe pyramid refresh."""

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
const flush=async()=>{for(let n=0;n<24;n++)await Promise.resolve();};
function fixture(){
  let context={sourceId:'a',generation:'g1',frame:null,key:'a:g1'},nextTimer=0;
  const requests=[],timers=new Map(),states=[],reloads=[],errors=[],notices=[];
  let refresh=async(generation,captured)=>{
    reloads.push({generation,captured});
    context={...context,generation,key:context.sourceId+':'+generation};api.syncContext();
  };
  const api=S.createController({readContext:()=>context,
    fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,reject,
      resolve:(data,ok=true)=>resolve({ok,status:ok?200:409,json:async()=>data})})),
    setTimer:(fn,delay)=>{const id=++nextTimer;timers.set(id,{fn,delay});return id},clearTimer:id=>timers.delete(id),
    onState:s=>states.push(s),onGeneration:(...args)=>refresh(...args),onError:e=>errors.push(e),onNotice:n=>notices.push(n)});
  const payload=(state,extra={})=>({state,generation:'g1',supported:true,reason:'',pct:0,
    applied:state==='ready',operation:null,can_cancel:['preparing','fitting','preparing_display','rebuilding'].includes(state),
    channels:state==='ready'?['FITC']:[],...extra});
  function tick(){const tasks=[...timers.values()];timers.clear();tasks.forEach(task=>task.fn());}
  return {api,requests,timers,states,reloads,errors,notices,payload,tick,
    setContext:c=>{context=c},setRefresh:fn=>{refresh=fn},context:()=>context};
}
function production(source,name){const at=source.indexOf('function '+name+'(');if(at<0)throw Error(name);return source.slice(at,source.indexOf('\n}',at)+2);}
function shellFixture(){
  const source=fs.readFileSync(process.argv[1]+'/shell-v1.js','utf8');
  const nodes=new Map(),messages=[];
  function $(id){if(!nodes.has(id))nodes.set(id,{attrs:{},open:false,classes:new Set(),focus(){},showModal(){this.open=true},close(){this.open=false},getBoundingClientRect:()=>({left:10,right:300,top:10,bottom:200}),classList:{toggle(name,on){on?nodes.get(id).classes.add(name):nodes.get(id).classes.delete(name)}},setAttribute(k,v){this.attrs[k]=v}});return nodes.get(id);}
  const c=vm.createContext({$,window:{Nd2ShadingCorrection:S},shadingStates:new Map(),shadingTokens:new Map([['a','ta'],['b','tb']]),
    active:'a',readyFrames:new Set(['a','b']),quitPreparation:null,shadingCommandSeq:0,shadingConfirmation:null,VIEWPORT_PROTOCOL_VERSION:2,
    postToSlide:(sid,data)=>{messages.push({sid,data});return true}});
  for(const name of ['refreshShadingButton','receiveShadingState','activateShadingCorrection','dismissShadingOutcome','shadingConfirmationMatches','closeShadingConfirmation','continueShadingConfirmation','dismissShadingConfirmation'])vm.runInContext(production(source,name),c);
  function status(sid,extra={}){return {sid,shellToken:'t'+sid,paneInstanceId:'pane-'+sid,seq:1,
    state:'idle',supported:true,applied:false,can_cancel:false,channels:[],generation:'g1',estimated_seconds:225,contextKey:sid+':g1',...extra};}
  return {c,$,messages,status};
}
(async()=>{
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", SCRIPT + body + "\n})().then(r=>process.stdout.write(JSON.stringify(r))).catch(e=>{console.error(e);process.exit(1)});", str(STATIC)],
        text=True, encoding="utf-8", capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_apply_replaces_generation_once_and_requires_confirmation_to_revoke():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();
      const idleDelay=[...f.timers.values()][0].delay;
      f.api.activate('start','a:g1',true);f.requests[1].resolve(f.payload('fitting',{operation:'apply',pct:23,eta_seconds:91}));await flush();
      const progress=S.progress(f.api.snapshot()),busyDelay=[...f.timers.values()][0].delay;f.tick();
      f.requests[2].resolve(f.payload('ready',{generation:'g2',pct:100}));await flush();
      const applied=f.api.snapshot(),unconfirmed=await f.api.activate('revoke','a:g2');
      const before=f.requests.length;f.tick();f.requests[3].resolve(f.payload('ready',{generation:'g2'}));await flush();
      return {progress,idleDelay,busyDelay,applied,unconfirmed,before,reloads:f.reloads,
        start:JSON.parse(f.requests[1].options.body),urls:f.requests.map(r=>r.url),hasRenderToggle:typeof f.api.appendRenderParams};
    """)
    assert out["progress"] == {"visible": True, "title": "Shading Correction", "stage": "Estimating illumination",
                               "canCancel": True, "pct": 23, "eta": "About 2 min remaining"}
    assert out["idleDelay"] == 5000 and out["busyDelay"] == 750
    assert out["applied"]["applied"] is True and out["applied"]["generation"] == "g2"
    assert out["applied"]["contextKey"] == "a:g2" and out["applied"]["view_stale"] is False
    assert out["unconfirmed"] is False and out["before"] == 3
    assert len(out["reloads"]) == 1 and out["reloads"][0]["generation"] == "g2"
    assert out["start"] == {"generation": "g1"}
    assert out["urls"] == ["api/shading/status", "api/shading/start", "api/shading/status", "api/shading/status"]
    assert out["hasRenderToggle"] == "undefined"


def test_reopened_applied_pyramid_is_authoritative_and_does_not_start_any_operation():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('ready'));await flush();
      const wrong=await f.api.activate('start','a:g1',true),unconfirmed=await f.api.activate('revoke','a:g1');
      return {applied:f.api.snapshot().applied,action:S.nextAction(f.api.snapshot()),wrong,unconfirmed,
        requests:f.requests.length,reloads:f.reloads.length,delay:[...f.timers.values()][0].delay};
    """)
    assert out == {"applied": True, "action": "revoke", "wrong": False, "unconfirmed": False,
                   "requests": 1, "reloads": 0, "delay": 5000}


def test_stale_generation_rejection_refreshes_status_without_replaying_operation():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();
      f.api.activate('start','a:g1',true);f.requests[1].resolve({error:'The pyramid changed'},false);await flush();
      f.requests[2].resolve(f.payload('ready',{generation:'g2'}));await flush();
      return {applied:f.api.snapshot().applied,generation:f.api.snapshot().generation,
        urls:f.requests.map(r=>r.url),errors:f.errors};
    """)
    assert out == {"applied": True, "generation": "g2", "urls": ["api/shading/status", "api/shading/start", "api/shading/status"], "errors": []}


def test_revoke_rebuild_has_progress_and_changes_applied_only_after_commit():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('ready'));await flush();
      f.api.activate('revoke','a:g1',true);
      const pending=f.api.snapshot();f.requests[1].resolve(f.payload('rebuilding',{applied:true,operation:'revoke',pct:41,eta_seconds:64}));await flush();
      const rebuilding=f.api.snapshot(),progress=S.progress(rebuilding);f.tick();
      f.requests[2].resolve(f.payload('idle',{applied:false,generation:'g2',operation:'revoke',pct:100}));await flush();
      return {pending:pending.applied,rebuilding:rebuilding.applied,progress,result:f.api.snapshot(),
        urls:f.requests.map(r=>r.url),body:JSON.parse(f.requests[1].options.body),reloads:f.reloads.length};
    """)
    assert out["pending"] is True and out["rebuilding"] is True
    assert out["progress"] == {"visible": True, "title": "Revoke Shading Correction", "stage": "Rebuilding original pyramid",
                               "canCancel": True, "pct": 41, "eta": "About 2 min remaining"}
    assert out["result"]["applied"] is False and out["result"]["generation"] == "g2"
    assert out["urls"][1] == "api/shading/revoke" and out["body"] == {"generation": "g1"}
    assert out["reloads"] == 1


@pytest.mark.parametrize("failure", ["f.requests[1].reject(Error('Disk full'))",
    "f.requests[1].resolve(f.payload('failed',{applied:true,operation:'revoke',reason:'Rebuild failed'}))"])
def test_failed_revoke_preserves_committed_correction_and_only_retries_revoke(failure):
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('ready'));await flush();f.api.activate('revoke','a:g1',true);
      FAILURE;await flush();const failed=f.api.snapshot();const wrong=await f.api.activate('start','a:g1',true);
      f.api.activate('revoke','a:g1',true);
      return {state:failed.state,applied:failed.applied,wrong,errors:f.errors.length,reloads:f.reloads.length,retry:f.requests[2].url};
    """.replace("FAILURE", failure))
    assert out == {"state": "failed", "applied": True, "wrong": False, "errors": 1, "reloads": 0, "retry": "api/shading/revoke"}


def test_cancel_keeps_committed_pyramid_and_ignores_inflight_poll():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('ready'));await flush();f.api.activate('revoke','a:g1',true);
      f.requests[1].resolve(f.payload('rebuilding',{applied:true,operation:'revoke'}));await flush();f.tick();
      f.api.activate('cancel','a:g1');f.requests[3].resolve(f.payload('cancelled',{applied:true,operation:'revoke'}));await flush();
      f.requests[2].resolve(f.payload('idle',{generation:'g2',applied:false}));await flush();
      return {state:f.api.snapshot().state,applied:f.api.snapshot().applied,generation:f.api.snapshot().generation,
        reloads:f.reloads.length,cancel:JSON.parse(f.requests[3].options.body),aborted:f.requests[2].options.signal.aborted};
    """)
    assert out == {"state": "cancelled", "applied": True, "generation": "g1", "reloads": 0,
                   "cancel": {"generation": "g1"}, "aborted": True}


def test_publishing_cannot_be_cancelled_and_keeps_old_applied_state_until_commit():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('publishing',{applied:false,operation:'apply',can_cancel:false,pct:98}));await flush();
      const cancel=await f.api.activate('cancel','a:g1');
      return {cancel,requests:f.requests.length,applied:f.api.snapshot().applied,
        disabled:S.describe(f.api.snapshot()).disabled,canCancel:S.progress(f.api.snapshot()).canCancel};
    """)
    assert out == {"cancel": False, "requests": 1, "applied": False, "disabled": True, "canCancel": False}


def test_source_change_and_disposal_reject_old_completion():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();f.api.activate('start','a:g1',true);
      f.setContext({sourceId:'b',generation:'b1',frame:null,key:'b:b1'});f.api.syncContext();
      f.requests[1].resolve(f.payload('ready',{generation:'g2'}));f.requests[2].resolve(f.payload('idle',{generation:'b1'}));await flush();
      const after=f.api.snapshot();f.api.activate('start','b:b1',true);f.api.dispose();
      f.requests[3].resolve(f.payload('ready',{generation:'b2'}));await flush();
      return {source:after.contextKey,applied:after.applied,reloads:f.reloads.length,
        aborted:[f.requests[1].options.signal.aborted,f.requests[3].options.signal.aborted],timers:f.timers.size};
    """)
    assert out == {"source": "b:b1", "applied": False, "reloads": 0, "aborted": [True, True], "timers": 0}


def test_committed_generation_refresh_failure_stays_applied_and_retries_without_mutation():
    out = run("""
      const f=fixture();f.setRefresh(async()=>{throw Error('Info unavailable')});f.api.load();
      f.requests[0].resolve(f.payload('ready',{generation:'g2'}));await flush();const failed=f.api.snapshot();
      const refused=await f.api.activate('revoke','a:g1',true);
      f.setRefresh(async(generation)=>{f.setContext({sourceId:'a',generation,frame:null,key:'a:'+generation});f.api.syncContext()});
      f.tick();f.requests[1].resolve(f.payload('ready',{generation:'g2'}));await flush();
      return {applied:failed.applied,stale:failed.view_stale,refused,after:f.api.snapshot(),errors:f.errors.length,
        urls:f.requests.map(r=>r.url)};
    """)
    assert out["applied"] is True and out["stale"] is True and out["refused"] is False
    assert out["after"]["applied"] is True and out["after"]["view_stale"] is False
    assert out["errors"] == 1 and out["urls"] == ["api/shading/status", "api/shading/status"]


def test_failed_tile_reopen_retries_even_after_metadata_generation_changed():
    out = run("""
      const f=fixture();f.setRefresh(async(generation)=>{
        f.setContext({sourceId:'a',generation,frame:null,key:'a:'+generation});f.api.syncContext();
        throw Error('Tiles unavailable');
      });f.api.load();f.requests[0].resolve(f.payload('ready',{generation:'g2'}));await flush();
      const failed=f.api.snapshot();f.setRefresh(async()=>{});f.tick();
      f.requests[1].resolve(f.payload('ready',{generation:'g2'}));await flush();
      return {failed,after:f.api.snapshot(),requests:f.requests.length};
    """)
    assert out["failed"]["applied"] is True and out["failed"]["view_stale"] is True
    assert out["failed"]["contextKey"] == "a:g2"
    assert out["after"]["view_stale"] is False and out["requests"] == 2


@pytest.mark.parametrize("state", ["unchanged", "unsupported", "failed"])
def test_no_change_outcome_is_reported_only_to_initiating_pane(state):
    out = run("""
      const initial=fixture(),started=fixture();initial.api.load();started.api.load();
      initial.requests[0].resolve(initial.payload('TERMINAL',{reason:'No usable correction',supported:false}));
      started.requests[0].resolve(started.payload('idle'));await flush();started.api.activate('start','a:g1',true);
      started.requests[1].resolve(started.payload('TERMINAL',{reason:'No usable correction',supported:false}));await flush();
      return {initial:initial.api.snapshot().outcome,outcome:started.api.snapshot().outcome,
        errors:started.errors,applied:started.api.snapshot().applied,action:S.nextAction(started.api.snapshot())};
    """.replace("TERMINAL", state))
    assert out == {"initial": None, "outcome": {"title": "Shading Correction was not applied",
                   "reason": "No usable correction", "note": "The original pyramid is unchanged.", "generation": "g1"},
                   "errors": [], "applied": False, "action": None}


def test_actual_repeat_cell_limit_shows_persistent_result_without_success_or_progress():
    out = run("""
      const f=fixture(),shell=shellFixture(),c=shell.c;
      const reason='Too few usable repeated fields in the independent validation rows to verify a correction for this scan.';
      f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();f.api.activate('start','a:g1',true);
      f.requests[1].resolve(f.payload('fitting',{operation:'apply',pct:42}));await flush();
      c.receiveShadingState('a',shell.status('a',{...f.api.snapshot(),seq:1}));
      const before={hidden:shell.$('shading-progress').hidden,pct:shell.$('shading-progress-bar').value};
      f.tick();f.requests[2].resolve(f.payload('unsupported',{operation:'apply',supported:false,pct:42,
        reason,reason_code:'insufficient_repeat_cells_heldout_rows'}));await flush();
      c.receiveShadingState('a',shell.status('a',{...f.api.snapshot(),seq:2}));f.api.syncContext();c.refreshShadingButton();
      const after={hidden:shell.$('shading-progress').hidden,title:shell.$('shading-progress-title').textContent,
        reason:shell.$('shading-stage').textContent,note:shell.$('shading-eta').textContent,
        progressHidden:shell.$('shading-progress-bar').hidden,pctHidden:shell.$('shading-pct').hidden,
        button:shell.$('shading-cancel').textContent,dismissDisabled:shell.$('shading-cancel').disabled,
        applied:shell.$('shading-correction').attrs['aria-pressed'],correctionDisabled:shell.$('shading-correction').disabled};
      c.dismissShadingOutcome();return {before,after,message:shell.messages[0],
        pct:f.api.snapshot().pct,generation:f.api.snapshot().generation,reloads:f.reloads.length};
    """)
    assert out["before"] == {"hidden": False, "pct": 42}
    assert out["after"] == {"hidden": False, "title": "Shading Correction was not applied",
                            "reason": "Too few usable repeated fields in the independent validation rows to verify a correction for this scan.",
                            "note": "The original pyramid is unchanged.", "progressHidden": True, "pctHidden": True,
                            "button": "Dismiss", "dismissDisabled": False, "applied": "false", "correctionDisabled": True}
    assert out["message"]["sid"] == "a" and out["message"]["data"]["action"] == "dismiss"
    assert out["message"]["data"]["contextKey"] == "a:g1"
    assert out["pct"] == 42 and out["generation"] == "g1" and out["reloads"] == 0


def test_outcome_persists_status_polls_and_dismissal_does_not_post_or_reappear():
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();
      f.api.activate('start','a:g1',true);f.requests[1].resolve(f.payload('failed',{reason:'The rebuild failed'}));await flush();
      f.tick();f.requests[2].resolve(f.payload('failed',{reason:'The rebuild failed'}));await flush();
      const persistent=f.api.snapshot().outcome;const wrong=f.api.dismissOutcome('b:g1');
      const before=f.requests.length,dismissed=f.api.dismissOutcome('a:g1'),after=f.requests.length;
      f.tick();f.requests[3].resolve(f.payload('failed',{reason:'The rebuild failed'}));await flush();
      return {persistent:!!persistent,wrong,before,dismissed,after,outcome:f.api.snapshot().outcome,
        posts:f.requests.filter(request=>request.options.method==='POST').map(request=>request.url)};
    """)
    assert out == {"persistent": True, "wrong": False, "before": 3, "dismissed": True, "after": 3,
                   "outcome": None, "posts": ["api/shading/start"]}


@pytest.mark.parametrize("completion", [
    "f.requests[1].reject(Error('Connection lost'))",
    "f.requests[1].resolve(f.payload('cancelled'))",
    "f.requests[1].resolve(f.payload('failed',{generation:'g2'}))",
    "f.requests[1].resolve(f.payload('failed',{applied:true}))",
])
def test_uncertain_cancelled_or_changed_pyramid_cannot_claim_original_unchanged(completion):
    out = run("""
      const f=fixture();f.api.load();f.requests[0].resolve(f.payload('idle'));await flush();
      f.api.activate('start','a:g1',true);COMPLETION;await flush();return f.api.snapshot().outcome;
    """.replace("COMPLETION", completion))
    assert out is None


def test_initial_unsupported_has_no_result_card_and_new_work_restores_progress():
    out = run("""
      const f=shellFixture(),c=f.c;
      c.receiveShadingState('a',f.status('a',{state:'unsupported',supported:false}));
      const initialHidden=f.$('shading-progress').hidden;
      c.receiveShadingState('a',f.status('a',{seq:2,state:'failed',outcome:{title:'Not applied',reason:'Failed',note:'Unchanged'}}));
      c.receiveShadingState('a',f.status('a',{seq:3,state:'fitting',pct:15,operation:'apply',can_cancel:true}));
      return {initialHidden,progressHidden:f.$('shading-progress').hidden,barHidden:f.$('shading-progress-bar').hidden,
        pctHidden:f.$('shading-pct').hidden,button:f.$('shading-cancel').textContent,pct:f.$('shading-progress-bar').value};
    """)
    assert out == {"initialHidden": True, "progressHidden": False, "barHidden": False,
                   "pctHidden": False, "button": "Cancel", "pct": 15}


def test_pane_dismissal_requires_current_token_pane_context_and_command_sequence():
    out = run("""
      const source=fs.readFileSync(process.argv[1]+'/app.js','utf8'),calls=[];
      const status={state:'unsupported',applied:false,supported:false,contextKey:'a:g1',outcome:{title:'Not applied'}};
      const c=vm.createContext({state:{shadingShellToken:'token',shadingCommandSeq:-1,
        viewportRelay:{commandGate:{paneInstanceId:'pane'}},shading:{snapshot:()=>status,
          dismissOutcome:key=>calls.push(key),activate:()=>{throw Error('must not mutate')}}},
        window:{Nd2ShadingCorrection:S},currentSlideSid:()=> 'a',postShadingState:()=>{}});
      vm.runInContext(production(source,'receiveShadingCommand'),c);
      const base={nd2wsi:'shading-command',sid:'a',shellToken:'token',paneInstanceId:'pane',commandSeq:1,
        contextKey:'a:g1',action:'dismiss'};
      c.receiveShadingCommand({...base,shellToken:'old'});c.receiveShadingCommand({...base,paneInstanceId:'old'});
      c.receiveShadingCommand({...base,contextKey:'b:g1'});c.receiveShadingCommand(base);c.receiveShadingCommand(base);
      return calls;
    """)
    assert out == ["a:g1"]


@pytest.mark.parametrize("applied,action,title,button", [
    (False, "start", "Shading Correction", "Continue"),
    (True, "revoke", "Revoke Shading Correction?", "Revoke"),
])
def test_confirmation_targets_exact_operation_and_captured_pane_once(applied, action, title, button):
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a',{applied:APPLIED}));c.activateShadingCorrection();
      const before={open:f.$('shading-confirm').open,estimate:f.$('shading-confirm-estimate').textContent,
        title:f.$('shading-confirm-title').textContent,button:f.$('shading-confirm-continue').textContent,
        message:f.$('shading-confirm-message').textContent,messages:f.messages.length,revoke:f.$('shading-confirm').classes.has('revoke')};
      const first=c.continueShadingConfirmation(),second=c.continueShadingConfirmation();
      return {before,first,second,open:f.$('shading-confirm').open,messages:f.messages};
    """.replace("APPLIED", str(applied).lower()))
    assert out["before"] == {"open": True, "estimate": "Est. time: about 4 min", "title": title, "button": button,
                             "message": "Rebuild the original pyramid from the source ND2." if applied else "Shadow correction best works in large scans.",
                             "messages": 0, "revoke": applied}
    assert out["first"] is True and out["second"] is False and out["open"] is False
    command, = out["messages"]
    assert command["sid"] == "a" and command["data"]["confirmed"] is True
    assert command["data"]["action"] == action and command["data"]["nd2wsi"] == "shading-command"
    assert command["data"]["generation"] == "g1" and command["data"]["contextKey"] == "a:g1"


@pytest.mark.parametrize("dismiss", ["c.closeShadingConfirmation()",
    "c.dismissShadingConfirmation({type:'cancel',preventDefault(){}})",
    "c.dismissShadingConfirmation({type:'click',target:f.$('shading-confirm'),clientX:0,clientY:0})"])
def test_cancel_escape_and_outside_dismiss_without_starting_mutation(dismiss):
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a',{applied:true}));c.activateShadingCorrection();
      DISMISS;c.continueShadingConfirmation();
      return {open:f.$('shading-confirm').open,pending:c.shadingConfirmation,messages:f.messages,sequence:c.shadingCommandSeq};
    """.replace("DISMISS", dismiss))
    assert out == {"open": False, "pending": None, "messages": [], "sequence": 0}


@pytest.mark.parametrize("change", ["c.active='b'",
    "c.receiveShadingState('a',f.status('a',{seq:2,contextKey:'a:g1:frame2'}))",
    "c.receiveShadingState('a',f.status('a',{seq:2,generation:'g2'}))",
    "c.receiveShadingState('a',f.status('a',{seq:2,applied:true}))",
    "c.shadingTokens.set('a','reloaded-pane')"])
def test_changed_operation_source_or_pane_lifetime_invalidates_confirmation(change):
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a'));c.activateShadingCorrection();
      CHANGE;c.refreshShadingButton();const continued=c.continueShadingConfirmation();
      return {continued,open:f.$('shading-confirm').open,messages:f.messages,pending:c.shadingConfirmation};
    """.replace("CHANGE", change))
    assert out == {"continued": False, "open": False, "messages": [], "pending": None}


def test_busy_cancel_is_immediate_and_publish_disables_it():
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a',{state:'rebuilding',operation:'revoke',applied:true,can_cancel:true}));
      c.activateShadingCorrection();const cancel=f.messages[0];
      c.receiveShadingState('a',f.status('a',{seq:2,state:'publishing',operation:'revoke',applied:true,can_cancel:false}));
      const blocked=c.activateShadingCorrection();return {cancel,blocked,messages:f.messages.length,
        dialog:f.$('shading-confirm').open,disabled:f.$('shading-cancel').disabled,title:f.$('shading-progress-title').textContent};
    """)
    assert out["cancel"]["data"]["action"] == "cancel"
    assert out["blocked"] is False and out["messages"] == 1 and out["dialog"] is False and out["disabled"] is True
    assert out["title"] == "Revoke Shading Correction"


def test_shell_active_pane_and_token_keep_mutation_target_unambiguous():
    out = run("""
      const f=shellFixture(),c=f.c;c.receiveShadingState('a',f.status('a'));c.receiveShadingState('b',f.status('b',{applied:true}));
      c.activateShadingCorrection();c.continueShadingConfirmation();c.active='b';c.refreshShadingButton();
      c.receiveShadingState('a',f.status('a',{seq:2,state:'fitting',pct:23,can_cancel:true}));
      const b={label:f.$('shading-correction').title,progress:f.$('shading-progress').hidden};
      const rejected=[c.receiveShadingState('b',f.status('b',{seq:100,shellToken:'old'})),c.receiveShadingState('b',f.status('b',{seq:0}))];
      c.activateShadingCorrection();c.continueShadingConfirmation();c.active='a';c.refreshShadingButton();
      return {targets:f.messages.map(m=>[m.sid,m.data.action]),b,rejected,a:{hidden:f.$('shading-progress').hidden,pct:f.$('shading-progress-bar').value}};
    """)
    assert out == {"targets": [["a", "start"], ["b", "revoke"]],
                   "b": {"label": "Shading Correction applied · Revoke…", "progress": True},
                   "rejected": [False, False], "a": {"hidden": False, "pct": 23}}


def test_delayed_pane_initialization_recovers_handshake_without_resetting_applied_status():
    out = run(r"""
      const f=shellFixture(),c=f.c,origin='http://viewer.test',child={},listeners=[];
      const shellSource=fs.readFileSync(process.argv[1]+'/shell-v1.js','utf8');
      const paneSource=fs.readFileSync(process.argv[1]+'/app.js','utf8');let installed=false,tokens=0,commands=0;
      c.location={origin};c.crypto={randomUUID:()=> 'handshake-'+(++tokens)};
      c.frameSidForSource=source=>source===child?'a':null;c.receiveViewportState=()=>true;c.paneCameUp=()=>{};
      c.window.addEventListener=(kind,listener)=>{if(kind==='message')listeners.push(listener)};
      const pane=vm.createContext({state:{shading:null,shadingShellToken:null,shadingSeq:0,quitPreparing:false,
        viewportRelay:{commandGate:{paneInstanceId:'pane-a'}}},
        window:{parent:{postMessage:data=>listeners.forEach(listener=>listener({origin,source:child,data}))}},
        location:{origin},VIEWPORT_PROTOCOL_VERSION:2,currentSlideSid:()=> 'a',spatialImageReady:()=>true});
      for(const name of ['postShadingState','receiveShadingCommand'])vm.runInContext(production(paneSource,name),pane);
      c.postToSlide=(sid,data)=>{commands++;if(installed)pane.receiveShadingCommand(data);return true};
      vm.runInContext(production(shellSource,'requestShadingState'),c);
      const at=shellSource.indexOf('window.addEventListener("message",');vm.runInContext(shellSource.slice(at,shellSource.indexOf('\n});',at)+4),c);
      c.requestShadingState('a');const loadToken=c.shadingTokens.get('a');
      pane.state.shading={snapshot:()=>({state:'ready',supported:true,applied:true,channels:[],contextKey:'a:g1'})};
      pane.postShadingState();const before=c.shadingStates.has('a');installed=true;
      const ready={origin,source:child,data:{nd2wsi:'viewport-ready',version:2,sid:'a'}};
      listeners.forEach(listener=>listener(ready));const recovered=c.shadingStates.get('a'),afterCommands=commands;
      listeners.forEach(listener=>listener(ready));return {before,recovered:!!recovered,applied:c.shadingStates.get('a').applied,
        sameStatus:c.shadingStates.get('a')===recovered,sameToken:c.shadingTokens.get('a')===loadToken,commands,afterCommands,tokens};
    """)
    assert out == {"before": False, "recovered": True, "applied": True, "sameStatus": True,
                   "sameToken": True, "commands": 2, "afterCommands": 2, "tokens": 1}


def test_pane_rejects_unconfirmed_or_changed_operation_commands():
    out = run("""
      const source=fs.readFileSync(process.argv[1]+'/app.js','utf8'),calls=[];
      const status={state:'ready',applied:true,supported:true,generation:'g1',contextKey:'a:g1'};
      const c=vm.createContext({state:{shadingShellToken:'token',shadingCommandSeq:-1,
        viewportRelay:{commandGate:{paneInstanceId:'pane'}},shading:{snapshot:()=>status,activate:(...args)=>calls.push(args)}},
        window:{Nd2ShadingCorrection:S},currentSlideSid:()=> 'a',postShadingState:()=>{}});
      vm.runInContext(production(source,'receiveShadingCommand'),c);
      const base={nd2wsi:'shading-command',sid:'a',shellToken:'token',paneInstanceId:'pane',commandSeq:1,
        contextKey:'a:g1',generation:'g1',state:'ready'};
      c.receiveShadingCommand({...base,action:'start',confirmed:true});
      c.receiveShadingCommand({...base,action:'revoke'});
      c.receiveShadingCommand({...base,action:'revoke',confirmed:true,generation:'old'});
      c.receiveShadingCommand({...base,action:'revoke',confirmed:true});
      c.receiveShadingCommand({...base,action:'revoke',confirmed:true});return calls;
    """)
    assert out == [["revoke", "a:g1", True]]


def test_generation_refresh_preserves_lut_roi_annotations_and_camera_pose():
    out = run("""
      const source=fs.readFileSync(process.argv[1]+'/app.js','utf8'),events=new Map(),calls=[];
      const info={generation:'g1',kind:'slide',width:100,height:80,channels:[{name:'FITC',window:{min:0,max:65535,start:0,end:5000}}],levels:[]};
      const luts=[{lo:12,hi:200,gamma:.7}],roi={x:3,y:4,w:5,h:6},annotations=[{id:'manual',x:9,y:10}],bounds={x:.2,y:.3,width:.4,height:.5};
      let gateGeneration='g1';const gate={latestCommandSeq:2,envelope:()=>({generation:gateGeneration}),setContext:c=>{const changed=gateGeneration!==c.generation;gateGeneration=c.generation;return changed}};
      const viewer={viewport:{getBounds:()=>bounds,fitBounds:b=>calls.push(['camera',b])},
        addHandler:(name,fn)=>events.set(name,[...(events.get(name)||[]),fn]),
        removeHandler:(name,fn)=>events.set(name,(events.get(name)||[]).filter(f=>f!==fn)),
        open:()=>{calls.push(['open']);for(const fn of [...events.get('open')])fn()}};
      const state={info,luts,roi,annotations,viewer,lutAutoPending:[true],inspect:{},landmark:{active:false,points:[]},
        viewportRelay:{commandGate:gate,displayRotation:37,displayFlipped:true}};
      const context=()=>({sourceId:'a',generation:info.generation,frame:null,key:'a:'+info.generation});
      const c=vm.createContext({state,Promise,clearTimeout:()=>{},activeFrameContext:context,
        fetch:async()=>({ok:true,json:async()=>({...info,generation:'g2'})}),
        window:{Nd2SpatialPane:{spatialContext:c=>c},Nd2LutTransfer:{currentLut:()=>{throw Error('must preserve explicit LUT')}}},
        frameRequests:{pixels:{invalidate:v=>calls.push(['pixels',v])},histograms:{schedule:v=>calls.push(['histograms',v])}},
        $:()=>({replaceChildren(){}}),buildLevelLamps:()=>{},updateScaleStrip:()=>{},
        postSpatialReadiness:()=>{},postViewportState:()=>{},applyDesiredDisplayTransform:()=>{},restoreRoiOverlay:()=>{},makeTileSource:()=>({})});
      for(const name of ['clearPaneSpatialWork','refreshSpatialContext','reopenPreservingView'])vm.runInContext(production(source,name),c);
      vm.runInContext('async '+production(source,'refreshShadingPyramid'),c);
      await c.refreshShadingPyramid('g2',context());
      return {sameInfo:state.info===info,sameLuts:state.luts===luts,luts,sameRoi:state.roi===roi,sameAnnotations:state.annotations===annotations,
        pose:[state.viewportRelay.displayRotation,state.viewportRelay.displayFlipped],generation:info.generation,auto:state.lutAutoPending,calls};
    """)
    assert out["sameInfo"] and out["sameLuts"] and out["sameRoi"] and out["sameAnnotations"]
    assert out["luts"] == [{"lo": 12, "hi": 200, "gamma": 0.7}] and out["pose"] == [37, True]
    assert out["generation"] == "g2" and out["auto"] == [False]
    assert out["calls"] == [["pixels", True], ["histograms", 0], ["open"], ["camera", {"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.5}]]


def test_late_metadata_for_replaced_pane_cannot_overwrite_new_source():
    out = run("""
      const source=fs.readFileSync(process.argv[1]+'/app.js','utf8');let resolve;
      const old={generation:'g1'},state={info:old};let context={sourceId:'a',generation:'g1',frame:null,key:'a:g1'};
      const c=vm.createContext({state,activeFrameContext:()=>context,fetch:()=>new Promise(r=>{resolve=r})});
      vm.runInContext('async '+production(source,'refreshShadingPyramid'),c);
      const pending=c.refreshShadingPyramid('g2',context);state.info={generation:'b1'};context={sourceId:'b',generation:'b1',frame:null,key:'b:b1'};
      resolve({ok:true,json:async()=>({generation:'g2'})});await pending;return state.info;
    """)
    assert out == {"generation": "b1"}


def test_unsupported_absent_blocked_or_stale_pane_cannot_start_and_eta_is_approximate():
    out = run("""
      const f=shellFixture(),c=f.c;c.refreshShadingButton();const absent=f.$('shading-correction').disabled;
      c.receiveShadingState('a',f.status('a',{state:'unsupported',supported:false,reason:'RGB images are unsupported'}));
      const unsupported={disabled:f.$('shading-correction').disabled,label:f.$('shading-correction').title};c.activateShadingCorrection();
      c.receiveShadingState('a',f.status('a',{seq:2,blocked:true}));c.activateShadingCorrection();
      c.receiveShadingState('a',f.status('a',{seq:3,view_stale:true}));c.activateShadingCorrection();
      return {absent,unsupported,messages:f.messages.length,unknown:S.progress({state:'fitting',pct:10,eta_seconds:null}).eta,
        known:S.progress({state:'preparing_display',pct:95,eta_seconds:13}).eta};
    """)
    assert out == {"absent": True, "unsupported": {"disabled": True, "label": "Shading Correction unavailable: RGB images are unsupported"},
                   "messages": 0, "unknown": "Estimating time…", "known": "About 15 sec remaining"}


def test_selected_icon_confirmation_colors_and_original_pixel_label_are_preserved():
    shell = (STATIC / "shell.html").read_text(encoding="utf-8")
    pane = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "native-shell-v1.css").read_text(encoding="utf-8")
    assert shell.index("shading-correction-v1.js") < shell.index("shell-v1.js")
    assert pane.index("shading-correction-v1.js") < pane.index("app.js")
    assert 'id="update-check"' not in shell
    assert 'id="shading-progress-bar"' in shell
    assert 'Shadow correction best works in large scans.' in shell
    assert 'aria-describedby="shading-confirm-message shading-confirm-estimate"' in shell
    assert '#shading-confirm-continue { background:#248a3d; }' in css
    assert '#shading-confirm-cancel { background:#d72c30; }' in css
    assert '#shading-confirm.revoke #shading-confirm-continue { background:#007aff; }' in css
    assert '#shading-confirm.revoke #shading-confirm-cancel' not in css
    assert '#shading-progress-bar[hidden] { display:none; }' in css
    assert '#shading-correction:hover:not(:disabled) { color:#ff453a; }' in css
    assert 'snapshot().applied) note.textContent += " · Original values before Shading Correction"' in app
    assert 'appendRenderParams' not in app and 'shading-toggle' not in app
    manifest = json.loads((STATIC / "icons/fluent-icons.json").read_text(encoding="utf-8"))
    name = "fluent-circle-highlight-20-regular.svg"
    assert name in css and "Circle%20Highlight" in manifest["assets"][name]
    assert (STATIC / "icons" / name).read_text(encoding="utf-8").startswith('<svg width="20" height="20"')
