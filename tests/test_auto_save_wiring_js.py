"""Pane wiring of the auto-save icon: pagehide keepalive flush, the periodic
retry and the status-bar icon/tooltip rendering driven by the real tracker.

The production functions (wireAutoSave, retryAutoSave, flushAutoSaveOnPagehide,
renderSaveStatus, wireSaveStatusTooltip) are sliced from ``app.js`` and run
with the real ``save-status-v1.js`` tracker, fake timers and fake controllers.
"""
import json
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"

HARNESS = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8').replace(/\r\n/g,'\n');
const SaveStatus=require(path.join(process.argv[1],'save-status-v1.js'));
let now=1000000,nextTimer=1;
const timers=new Map(),intervals=new Map(),calls=[];
function setTimeout(fn,ms){const id=nextTimer++;timers.set(id,{fn,due:now+ms});return id}
function clearTimeout(id){timers.delete(id)}
function setInterval(fn,ms){const id=nextTimer++;intervals.set(id,{fn,ms,next:now+ms});return id}
function clearInterval(id){intervals.delete(id)}
function advance(ms){
  const end=now+ms;
  while(true){
    const due=[...timers].map(([id,t])=>({id,at:t.due,fn:t.fn,once:true}))
      .concat([...intervals].map(([id,t])=>({id,at:t.next,fn:t.fn,once:false})))
      .filter(t=>t.at<=end).sort((a,b)=>a.at-b.at)[0];
    if(!due)break;
    now=due.at;
    if(due.once)timers.delete(due.id);else intervals.get(due.id).next=now+intervals.get(due.id).ms;
    due.fn();
  }
  now=end;
}
function eventTarget(target={}){
  target.handlers={};
  target.addEventListener=function(k,f){(this.handlers[k]||(this.handlers[k]=[])).push(f)};
  target.removeEventListener=function(k,f){this.handlers[k]=(this.handlers[k]||[]).filter(g=>g!==f)};
  return target;
}
function element(id){
  const el=eventTarget({id,attrs:{},style:{setProperty(k,v){this[k]=String(v)}},dataset:{},hidden:true,writes:0,attrWrites:0,_text:'',children:[],
    rect:{left:700,top:580,right:718,bottom:598,width:18,height:18},
    setAttribute(k,v){this.attrs[k]=String(v);this.attrWrites++},removeAttribute(k){delete this.attrs[k];this.attrWrites++},
    getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null},
    contains(target){return target===this||this.children.some(child=>child.contains(target))},
    getBoundingClientRect(){return this.hidden?{left:0,top:0,right:0,bottom:0,width:0,height:0}:this.rect}});
  Object.defineProperty(el,'textContent',{get(){return this._text},set(v){this._text=v;this.writes++}});
  return el;
}
const nodes=Object.fromEntries(['save-status','save-status-tooltip','save-status-summary','save-status-details',
  'storage-badge','calibration-badge','source-badge','file-dims','ann-editor'].map(id=>[id,element(id)]));
nodes['save-status-tooltip'].children=['save-status-summary','save-status-details','storage-badge','calibration-badge'].map(id=>nodes[id]);
nodes['save-status-tooltip'].rect={left:0,top:0,right:160,bottom:30,width:160,height:30};
nodes['save-status'].attrs['aria-label']='Auto-save'; // as the markup ships it
const state={info:null,quitPreparing:false,lutPersistence:null,saveStatus:null,autoSaveRetry:null};
const annotationStore={
  flushOnPagehide(...args){calls.push(['ann.flushOnPagehide',...args]);return Promise.resolve({ok:true,sent:true})},
  retryFailed(...args){calls.push(['ann.retryFailed',...args]);return Promise.resolve({ok:true,retried:0})}};
const lutPersistence={dirty:false,isDirty(){return this.dirty},
  flush(...args){calls.push(['lut.flush',...args]);return Promise.resolve()}};
const window=eventTarget({innerWidth:800,innerHeight:600,
  Nd2SaveStatus:{createTracker:options=>SaveStatus.createTracker({...options,now:()=>now,setInterval,clearInterval})}});
const document=eventTarget({});
// The note editor commits its text through annotationsChanged(), which marks the store dirty.
const closeEditor=save=>{calls.push(['closeEditor',save]);nodes['ann-editor'].hidden=true;};
const context={state,window,document,setTimeout,clearTimeout,setInterval,clearInterval,annotationStore,closeEditor,
  fmtInt:value=>Math.round(value).toLocaleString('en-US'),$:id=>nodes[id]||null};
vm.createContext(context);
const start=source.indexOf('/* ---- auto-save status'),end=source.indexOf('function pixelSize(',start);
if(start<0||end<0)throw new Error('auto-save source bounds not found');
vm.runInContext(source.slice(start,end),context);
const badgesStart=source.indexOf('function kindMark('),badgesEnd=source.indexOf('function planeNote(',badgesStart);
if(badgesStart<0||badgesEnd<0)throw new Error('context badge source bounds not found');
vm.runInContext(source.slice(badgesStart,badgesEnd),context);
const button=nodes['save-status'],tooltip=nodes['save-status-tooltip'];
const summary=nodes['save-status-summary'],details=nodes['save-status-details'];
const tooltipText=()=>summary.textContent+(details.hidden||!details.textContent?'':'\n'+details.textContent);
const tooltipWrites=()=>summary.writes+details.writes;
const icon=()=>({hidden:button.hidden,state:button.attrs['data-state']??null,label:button.attrs['aria-label']});
const warning=()=>button.attrs['data-warning']??null;
const loadInfo=(changes={})=>{
  state.info={kind:'slide',width:2048,height:1024,channels:[{name:'C0'},{name:'C1'}],dtype:'uint16',
    storage:'compact',calibrated:true,...changes};
  context.fillContextBadges(state.info);
};
const fire=(target,type,event={})=>{
  if(type==='focus'||type==='focusin')document.activeElement=target;
  if(type==='blur'||type==='focusout')document.activeElement=event.relatedTarget||null;
  (target.handlers[type]||[]).forEach(fn=>fn(event));
};
const report=(channel,event)=>state.saveStatus.report(channel,event);
"""


def run(body):
    result = run_node(
        HARNESS + "(async()=>{" + body + "})().then(v=>process.stdout.write(JSON.stringify(v)))"
        ".catch(e=>{console.error(e);process.exitCode=1});",
        STATIC,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_wiring_registers_one_pagehide_listener_and_the_minute_retry_before_the_lut_controller_exists():
    out = run("""
context.wireAutoSave();
return {pagehide:window.handlers.pagehide.length,intervals:[...intervals.values()].map(t=>t.ms).sort((a,b)=>a-b),
  retry:typeof state.autoSaveRetry,tracker:!!state.saveStatus,icon:icon(),tooltipHidden:tooltip.hidden,
  calls,buttonEvents:Object.keys(button.handlers).sort()};
""")
    assert out["pagehide"] == 1 and out["intervals"] == [1000, 60000]
    assert out["retry"] == "number" and out["tracker"] is True
    assert out["icon"] == {"hidden": True, "state": None, "label": "Auto-save"}
    assert out["tooltipHidden"] is True and out["calls"] == []
    assert out["buttonEvents"] == ["blur", "focus", "pointerenter", "pointerleave"]


@pytest.mark.parametrize("quit_preparing", [False, True])
def test_pagehide_flushes_with_keepalive_only_in_browser_mode_then_stops_retry_and_ticker(quit_preparing):
    out = run(f"""
context.wireAutoSave();
state.lutPersistence=lutPersistence;
lutPersistence.dirty=true;
state.quitPreparing={json.dumps(quit_preparing)};
report('annotations',{{state:'saved'}});
fire(window,'pagehide');
const after={{calls:[...calls],intervals:intervals.size,retry:state.autoSaveRetry,
  buttonListeners:Object.values(button.handlers).reduce((n,list)=>n+list.length,0),
  documentListeners:(document.handlers.keydown||[]).length}};
calls.length=0;advance(120000);
report('annotations',{{state:'dirty'}});
return {{after,late:calls,icon:icon()}};
""")
    expected = [] if quit_preparing else [
        ["ann.flushOnPagehide", {"keepalive": True}], ["lut.flush", {"keepalive": True}]]
    assert out["after"]["calls"] == expected
    assert out["after"]["intervals"] == 0 and out["after"]["retry"] is None
    assert out["after"]["buttonListeners"] == 0 and out["after"]["documentListeners"] == 0
    # nothing runs after pagehide: no retry, no render of the disposed tracker
    assert out["late"] == [] and out["icon"]["state"] == "saved"


def test_pagehide_leaves_the_annotation_decision_to_the_store_and_skips_a_clean_lut_controller():
    out = run("""
context.wireAutoSave();state.lutPersistence=lutPersistence;
fire(window,'pagehide');return calls;
""")
    assert out == [["ann.flushOnPagehide", {"keepalive": True}]]


@pytest.mark.parametrize("quit_preparing", [False, True])
def test_pagehide_commits_the_open_note_editor_before_the_keepalive_flush(quit_preparing):
    out = run(f"""
context.wireAutoSave();state.lutPersistence=lutPersistence;
nodes['ann-editor'].hidden=false;state.quitPreparing={json.dumps(quit_preparing)};
fire(window,'pagehide');return {{calls,editorHidden:nodes['ann-editor'].hidden}};
""")
    if quit_preparing:
        # the native save-before-close owns the editor while the shell quits
        assert out == {"calls": [], "editorHidden": False}
    else:
        assert out == {"calls": [["closeEditor", True], ["ann.flushOnPagehide", {"keepalive": True}]],
                       "editorHidden": True}


# Other pageshow listeners may share these files (the shell restarts its own
# polling on pageshow), so the reload handler is found by name, not position.
PAGESHOW = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');
const NAME='reloadIfRestoredFromCache';
const REGISTRATION='window.addEventListener("pageshow", '+NAME+');';
const results={};
for(const name of ['app.js','shell-v1.js']){
  // Windows CI checks out CRLF; the function is cut out by its '\n}\n' end.
  const source=fs.readFileSync(path.join(process.argv[1],name),'utf8').replace(/\r\n/g,'\n');
  const start=source.indexOf('function '+NAME+'(');
  if(start<0)throw new Error('no '+NAME+' in '+name);
  const handlers=[],reloads=[];
  const context={window:{addEventListener:(type,fn)=>handlers.push([type,fn])},
    location:{reload:()=>reloads.push(true)}};
  vm.createContext(context);
  const registrations=source.split(REGISTRATION).length-1;
  vm.runInContext(source.slice(start,source.indexOf('\n}\n',start)+3)+REGISTRATION.repeat(registrations),
    context);
  handlers.forEach(([,fn])=>fn({persisted:false}));
  const fresh=reloads.length;
  handlers.forEach(([,fn])=>fn({persisted:true}));
  results[name]={types:handlers.map(([type])=>type),fresh,restored:reloads.length-fresh,
    references:source.split(NAME).length-1};
}
process.stdout.write(JSON.stringify(results));
"""


def test_pane_and_shell_reload_only_when_restored_from_the_back_forward_cache():
    result = run_node(PAGESHOW, STATIC)
    assert result.returncode == 0, result.stderr
    # one declaration and exactly one pageshow registration per page
    expected = {"types": ["pageshow"], "fresh": 0, "restored": 1, "references": 2}
    assert json.loads(result.stdout) == {"app.js": expected, "shell-v1.js": expected}


def test_minute_retry_requeues_annotations_flushes_dirty_luts_and_counts_down_in_the_failed_text():
    out = run("""
context.wireAutoSave();state.lutPersistence=lutPersistence;
report('annotations',{state:'failed',message:'HTTP 500'});
const texts=[state.saveStatus.describe(now).text];
advance(10000);texts.push(state.saveStatus.describe(now).text);
advance(50000);texts.push(state.saveStatus.describe(now).text);
const firstMinute=[...calls];calls.length=0;
lutPersistence.dirty=true;advance(60000);
const secondMinute=[...calls];calls.length=0;
state.quitPreparing=true;advance(60000);
const quitting=[...calls];calls.length=0;
state.quitPreparing=false;report('annotations',{state:'saved'});advance(60000);
return {texts,firstMinute,secondMinute,quitting,afterSaved:calls,icon:icon(),text:state.saveStatus.current().text};
""")
    assert out["texts"] == [
        "Not saved · retrying in 60 s\nHTTP 500", "Not saved · retrying in 50 s\nHTTP 500",
        "Not saved · retrying in 60 s\nHTTP 500"]
    assert out["firstMinute"] == [["ann.retryFailed"]]
    assert out["secondMinute"] == [["ann.retryFailed"], ["lut.flush"]]
    assert out["quitting"] == []
    assert out["afterSaved"] == [["ann.retryFailed"], ["lut.flush"]]
    assert out["icon"]["state"] == "saved" and out["icon"]["label"] == "Auto-save: saved"
    assert out["text"] == "Auto-saved 1 min ago"


def test_icon_is_hidden_while_idle_and_its_data_state_and_aria_label_follow_the_state_not_the_ticker():
    out = run("""
context.wireAutoSave();
const steps=[icon()],writes=[button.attrWrites];
report('annotations',{state:'dirty'});steps.push(icon());
report('annotations',{state:'saving'});steps.push(icon());
report('annotations',{state:'saved'});steps.push(icon());writes.push(button.attrWrites);
advance(59000);steps.push(icon());writes.push(button.attrWrites);
report('annotations',{state:'saved'});writes.push(button.attrWrites);
report('lut',{state:'failed',message:'HTTP 503'});steps.push(icon());
report('lut',{state:'saved'});report('annotations',{state:'conflict',conflict:true,message:'Conflict'});steps.push(icon());
report('annotations',{state:'saved'});report('lut',{state:'unavailable',message:'no LUT store'});steps.push(icon());
return {steps,writes,text:state.saveStatus.current().text};
""")
    assert out["steps"] == [
        {"hidden": True, "state": None, "label": "Auto-save"},
        {"hidden": False, "state": "dirty", "label": "Auto-save: unsaved changes"},
        {"hidden": False, "state": "saving", "label": "Auto-save: saving"},
        {"hidden": False, "state": "saved", "label": "Auto-save: saved"},
        {"hidden": False, "state": "saved", "label": "Auto-save: saved"},
        {"hidden": False, "state": "failed", "label": "Auto-save: not saved; warning, see status details"},
        {"hidden": False, "state": "conflict", "label": "Auto-save: conflict; warning, see status details"},
        {"hidden": False, "state": "saved", "label": "Auto-save: saved; warning, see status details"},
    ]
    # Attributes track semantic changes, not 59 ticks of "N s ago" or a
    # repeated report of the same state. The popover also owns aria-expanded.
    assert out["writes"][1] > out["writes"][0]
    assert len(set(out["writes"][1:])) == 1
    assert out["text"] == "Auto-saved 0 s ago"


def test_tooltip_shows_on_focus_or_hover_follows_the_ticker_while_shown_and_is_not_written_while_hidden():
    out = run("""
context.wireAutoSave();
fire(button,'focus');const idleFocus=tooltip.hidden;fire(button,'blur');
report('annotations',{state:'saved'});
advance(2000);
const hiddenWrites=tooltipWrites();
fire(button,'focus');
const shown={hidden:tooltip.hidden,text:tooltipText(),left:tooltip.style.left,top:tooltip.style.top};
advance(1000);const tick1=tooltipText();advance(1000);const tick2=tooltipText();
fire(button,'blur');advance(180);const blurred=tooltip.hidden;const writesAtBlur=tooltipWrites();
advance(2820);const writesWhileHidden=tooltipWrites()-writesAtBlur;
fire(button,'pointerenter');const immediately=tooltip.hidden;
advance(599);const early=tooltip.hidden;advance(1);
const hovered={hidden:tooltip.hidden,text:tooltipText()};
report('lut',{state:'unavailable',message:'LUT settings are not saved for this source.'});
const shownDetails={text:tooltipText(),label:button.attrs['aria-label']};
fire(document,'keydown',{key:'Escape'});const escaped=tooltip.hidden;
fire(button,'pointerleave');fire(button,'pointerenter');advance(600);const again=tooltip.hidden;
fire(button,'pointerleave');advance(180);const left=tooltip.hidden;const writesAtLeave=tooltipWrites();
advance(5000);
return {idleFocus,hiddenWrites,shown,tick1,tick2,blurred,writesWhileHidden,immediately,early,hovered,details:shownDetails,
  escaped,again,left,writesAfterLeave:tooltipWrites()-writesAtLeave,timers:timers.size};
""")
    assert out["idleFocus"] is True and out["hiddenWrites"] == 0
    assert out["shown"] == {"hidden": False, "text": "Auto-saved 2 s ago", "left": "629px", "top": "542px"}
    assert out["tick1"] == "Auto-saved 3 s ago" and out["tick2"] == "Auto-saved 4 s ago"
    assert out["blurred"] is True and out["writesWhileHidden"] == 0
    assert out["immediately"] is True and out["early"] is True
    assert out["hovered"] == {"hidden": False, "text": "Auto-saved 7 s ago"}
    assert out["details"] == {"text": "Auto-saved 7 s ago\nLUT settings are not saved for this source.",
                              "label": "Auto-save: saved; warning, see status details"}
    assert out["escaped"] is True and out["again"] is False and out["left"] is True
    assert out["writesAfterLeave"] == 0 and out["timers"] == 0


def test_an_invalid_lut_edit_explains_itself_in_the_tooltip_and_is_not_retried():
    out = run("""
context.wireAutoSave();state.lutPersistence=lutPersistence;
report('lut',{state:'dirty',message:"LUT values are invalid for this image's pixel type."});
fire(button,'focus');
const shown={icon:icon(),text:tooltipText(),warning:warning()};
advance(60000);
return {shown,calls};
""")
    assert out["shown"]["icon"] == {"hidden": False, "state": "dirty",
                                    "label": "Auto-save: unsaved changes; warning, see status details"}
    assert out["shown"]["warning"] == "true"
    assert out["shown"]["text"] == "Unsaved changes…\nLUT values are invalid for this image's pixel type."
    # the controller reports it as not dirty, so the minute retry leaves it alone
    assert out["calls"] == [["ann.retryFailed"]]


def test_opening_a_slide_exposes_metadata_without_claiming_that_an_idle_session_was_saved():
    out = run("""
context.wireAutoSave();loadInfo();
const before={icon:icon(),warning:warning(),status:state.saveStatus.current()};
fire(button,'focus');
const opened={text:tooltipText(),hidden:tooltip.hidden,detailsHidden:details.hidden,
  storage:nodes['storage-badge'].textContent,calibration:nodes['calibration-badge'].textContent};
advance(59000);
return {before,opened,after:tooltipText(),savedAt:state.saveStatus.current().savedAt,calls,
  parentWrites:tooltip.writes,children:tooltip.children.map(child=>child.id)};
""")
    assert out["before"]["icon"] == {"hidden": False, "state": "idle", "label": "Auto-save: no changes to save"}
    assert out["before"]["warning"] is None
    assert out["before"]["status"]["state"] == "idle"
    assert out["before"]["status"]["savedAt"] is None
    assert out["opened"] == {"text": "No changes to save", "hidden": False, "detailsHidden": True,
                             "storage": "Compact cache", "calibration": "Calibrated"}
    assert out["after"] == "No changes to save" and out["savedAt"] is None
    assert out["calls"] == [] and out["parentWrites"] == 0
    assert out["children"] == ["save-status-summary", "save-status-details", "storage-badge", "calibration-badge"]


@pytest.mark.parametrize("changes,storage,calibration", [
    ({"storage": "overview-degraded"}, "Overview only", "Calibrated"),
    ({"calibrated": False}, "Compact cache", "Pixels only"),
    ({"storage": "overview-degraded", "calibrated": False}, "Overview only", "Pixels only"),
])
def test_metadata_warnings_are_visible_before_an_edit_and_survive_a_real_save(changes, storage, calibration):
    out = run(f"""
context.wireAutoSave();loadInfo({json.dumps(changes)});fire(button,'focus');
const idle={{icon:icon(),warning:warning(),text:tooltipText(),savedAt:state.saveStatus.current().savedAt,
  storage:nodes['storage-badge'].textContent,calibration:nodes['calibration-badge'].textContent}};
report('annotations',{{state:'saved'}});advance(3000);
const saved={{icon:icon(),warning:warning(),text:tooltipText(),savedAt:state.saveStatus.current().savedAt}};
loadInfo();
return {{idle,saved,cleared:{{warning:warning(),icon:icon(),text:tooltipText()}},parentWrites:tooltip.writes}};
""")
    assert out["idle"] == {
        "icon": {"hidden": False, "state": "idle", "label": "Auto-save: no changes to save; warning, see status details"},
        "warning": "true", "text": "No changes to save", "savedAt": None,
        "storage": storage, "calibration": calibration,
    }
    assert out["saved"] == {
        "icon": {"hidden": False, "state": "saved", "label": "Auto-save: saved; warning, see status details"},
        "warning": "true", "text": "Auto-saved 3 s ago", "savedAt": 1000000,
    }
    assert out["cleared"] == {"warning": None,
                              "icon": {"hidden": False, "state": "saved", "label": "Auto-save: saved"},
                              "text": "Auto-saved 3 s ago"}
    assert out["parentWrites"] == 0


def test_persistence_unavailability_is_inspectable_without_creating_a_saved_time():
    out = run("""
context.wireAutoSave();
report('lut',{state:'unavailable',message:'LUT settings are not saved for this source.'});
fire(button,'focus');
return {icon:icon(),warning:warning(),text:tooltipText(),savedAt:state.saveStatus.current().savedAt,
  trackerVisible:state.saveStatus.current().visible,detailsHidden:details.hidden};
""")
    assert out == {
        "icon": {"hidden": False, "state": "idle", "label": "Auto-save: no changes to save; warning, see status details"},
        "warning": "true", "text": "No changes to save\nLUT settings are not saved for this source.",
        "savedAt": None, "trackerVisible": False, "detailsHidden": False,
    }


@pytest.mark.parametrize("save_state", ["failed", "conflict"])
def test_persistence_failures_switch_the_warning_signal_on_and_clear_it_only_after_recovery(save_state):
    out = run(f"""
context.wireAutoSave();loadInfo();fire(button,'focus');
report('annotations',{{state:{json.dumps(save_state)},message:'Save problem'}});
const failed={{warning:warning(),state:icon().state,text:tooltipText()}};
report('annotations',{{state:'saved'}});
return {{failed,recovered:{{warning:warning(),state:icon().state,text:tooltipText()}}}};
""")
    assert out["failed"]["warning"] == "true" and out["failed"]["state"] == save_state
    assert out["failed"]["text"].startswith("Not saved")
    assert out["recovered"] == {"warning": None, "state": "saved", "text": "Auto-saved 0 s ago"}


def test_hover_delay_and_close_grace_allow_pointer_transfer_into_the_live_popover():
    out = run("""
context.wireAutoSave();loadInfo();report('annotations',{state:'saved'});
fire(button,'pointerenter');advance(599);const early=tooltip.hidden;
fire(button,'pointerleave');advance(1000);const canceled=tooltip.hidden;
fire(button,'pointerenter');advance(600);const shown=tooltip.hidden;
fire(button,'pointerleave');advance(179);const inGap=tooltip.hidden;
fire(tooltip,'pointerenter');advance(1000);
const inside={hidden:tooltip.hidden,text:tooltipText(),expanded:button.attrs['aria-expanded'],timers:timers.size};
fire(tooltip,'pointerleave');advance(179);const leaving=tooltip.hidden;advance(1);
return {early,canceled,shown,inGap,inside,leaving,closed:tooltip.hidden,timers:timers.size,
  expanded:button.attrs['aria-expanded']};
""")
    assert out["early"] is True and out["canceled"] is True
    assert out["shown"] is False and out["inGap"] is False
    assert out["inside"] == {"hidden": False, "text": "Auto-saved 3 s ago", "expanded": "true", "timers": 0}
    assert out["leaving"] is False and out["closed"] is True
    assert out["timers"] == 0 and out["expanded"] == "false"


@pytest.mark.parametrize("left,top,expected", [
    (700, 580, ["629px", "542px", "80px", "30px", "6px"]),
    (790, 580, ["632px", "542px", "160px", "30px", "6px"]),
    (-20, 580, ["8px", "542px", "0px", "30px", "6px"]),
    (100, 5, ["29px", "31px", "80px", "0px", "-6px"]),
])
def test_popover_origin_tracks_the_trigger_after_edge_clamping_and_below_anchor_fallback(left, top, expected):
    out = run(f"""
context.wireAutoSave();loadInfo();
button.rect={{left:{left},top:{top},right:{left + 18},bottom:{top + 18},width:18,height:18}};
fire(button,'focus');
return ['left','top','--status-origin-x','--status-origin-y','--status-enter-y'].map(key=>tooltip.style[key]);
""")
    assert out == expected


def test_popover_motion_uses_unscaled_layout_dimensions_and_age_ticks_do_not_move_the_anchor():
    out = run("""
context.wireAutoSave();loadInfo();report('annotations',{state:'saved'});
tooltip.offsetWidth=160;tooltip.offsetHeight=30;
tooltip.rect={left:0,top:0,right:96,bottom:18,width:96,height:18};
fire(button,'focus');
const position=()=>['left','top','--status-origin-x','--status-origin-y','--status-enter-y'].map(key=>tooltip.style[key]);
const opening=position();
tooltip.rect={left:0,top:0,right:160,bottom:30,width:160,height:30};
advance(1000);
return {opening,settled:position(),text:tooltipText(),expanded:button.attrs['aria-expanded']};
""")
    assert out["opening"] == out["settled"] == ["629px", "542px", "80px", "30px", "6px"]
    assert out["text"] == "Auto-saved 1 s ago" and out["expanded"] == "true"


def test_focus_can_move_inside_the_popover_and_escape_dismisses_it_until_refocus():
    out = run("""
context.wireAutoSave();loadInfo();report('annotations',{state:'saved'});
fire(button,'focus');const focused=tooltip.hidden;
fire(button,'blur',{relatedTarget:summary});fire(tooltip,'focusin');advance(1000);
const transferred=tooltip.hidden;
fire(document,'keydown',{key:'Enter'});const otherKey=tooltip.hidden;
fire(document,'keydown',{key:'Escape'});advance(5000);const escaped=tooltip.hidden;
fire(tooltip,'focusout',{relatedTarget:button});fire(button,'focus');
const refocused=tooltip.hidden;
fire(button,'blur',{relatedTarget:element('outside')});advance(180);
return {focused,transferred,otherKey,escaped,refocused,blurred:tooltip.hidden,timers:timers.size};
""")
    assert out == {"focused": False, "transferred": False, "otherKey": False, "escaped": True,
                   "refocused": False, "blurred": True, "timers": 0}


def test_losing_keyboard_focus_keeps_the_popover_open_while_the_pointer_is_still_over_the_button():
    out = run("""
context.wireAutoSave();loadInfo();
fire(button,'pointerenter');fire(button,'focus');fire(button,'blur');advance(1000);
const hovered=tooltip.hidden;
fire(button,'pointerleave');advance(180);
return {hovered,left:tooltip.hidden,timers:timers.size};
""")
    assert out == {"hovered": False, "left": True, "timers": 0}


@pytest.mark.parametrize("phase", ["opening", "closing"])
def test_pagehide_disposes_a_pending_popover_timer_and_all_its_event_listeners(phase):
    out = run(f"""
context.wireAutoSave();loadInfo();report('annotations',{{state:'saved'}});
fire(button,'pointerenter');
if({json.dumps(phase)}==='closing'){{advance(600);fire(button,'pointerleave')}}
const pending=timers.size;
fire(window,'pagehide');
const disposed={{timers:timers.size,intervals:intervals.size,hidden:tooltip.hidden,
  expanded:button.attrs['aria-expanded'],controller:!!button.saveStatusTooltip,
  buttonListeners:Object.values(button.handlers).flat().length,
  popupListeners:Object.values(tooltip.handlers).flat().length,
  documentListeners:Object.values(document.handlers).flat().length,
  resizeListeners:(window.handlers.resize||[]).length,scrollListeners:(window.handlers.scroll||[]).length}};
const writes=tooltipWrites();calls.length=0;
advance(120000);fire(button,'pointerenter');fire(tooltip,'focusin');fire(window,'resize');
return {{pending,disposed,lateWrites:tooltipWrites()-writes,lateCalls:calls}};
""")
    assert out["pending"] == 1
    assert out["disposed"] == {"timers": 0, "intervals": 0, "hidden": True, "expanded": "false",
                               "controller": False, "buttonListeners": 0, "popupListeners": 0,
                               "documentListeners": 0, "resizeListeners": 0, "scrollListeners": 0}
    assert out["lateWrites"] == 0 and out["lateCalls"] == []


def test_markup_places_the_icon_after_the_scale_bar_and_loads_the_tracker_before_the_app():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    footer = html[html.index('<footer id="statusbar">'):html.index("</footer>")]
    assert footer.index('id="scalebar-wrap"') < footer.index('id="save-status"')
    assert footer.rstrip().endswith("</button>")
    assert '<button type="button" id="save-status" class="save-status" hidden aria-describedby="save-status-tooltip" aria-label="Auto-save">' in footer
    assert '<div id="save-status-tooltip" role="tooltip" hidden>' in html
    assert '<div id="save-status-summary"></div>' in html
    assert '<div id="save-status-details" hidden></div>' in html
    assert html.index("annotation-store-v1.js") < html.index("save-status-v1.js") < html.index("static/app.js")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for token in ("--save-ok", "--save-warn", "--save-bad"):
        assert css.count(token + ":") == 2, token
    for state in ("saved", "failed", "conflict", "saving", "dirty"):
        assert f'.save-status[data-state="{state}"]' in css
    assert ".save-status[hidden] { display: none; }" in css
    assert "@media (prefers-reduced-motion: no-preference)" in css
    # the hover/focus opacity wins over the saving pulse
    pulse = css.index('.save-status[data-state="saving"] { animation: save-status-pulse')
    still = css.index('.save-status[data-state="saving"]:hover, .save-status[data-state="saving"]:focus-visible { animation: none; }')
    assert pulse < still < css.index("@keyframes save-status-pulse")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert app.index("wireAutoSave();") < app.index("await initializeDisplayLuts();")
    assert 'onSaveState: (event) => state.saveStatus?.report(event.channel, event)' in app
    assert app.count("onSaveState:") == 2
