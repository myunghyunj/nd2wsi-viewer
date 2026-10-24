"""Pane wiring of the auto-save icon: pagehide keepalive flush, the periodic
retry and the status-bar icon/tooltip rendering driven by the real tracker.

The production functions (wireAutoSave, retryAutoSave, flushAutoSaveOnPagehide,
renderSaveStatus, wireSaveStatusTooltip) are sliced from ``app.js`` and run
with the real ``save-status-v1.js`` tracker, fake timers and fake controllers.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8');
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
  const el=eventTarget({id,attrs:{},style:{},hidden:true,writes:0,attrWrites:0,_text:'',
    rect:{left:700,top:580,right:718,bottom:598,width:18,height:18},
    setAttribute(k,v){this.attrs[k]=String(v);this.attrWrites++},removeAttribute(k){delete this.attrs[k];this.attrWrites++},
    getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null},
    getBoundingClientRect(){return this.hidden?{left:0,top:0,right:0,bottom:0,width:0,height:0}:this.rect}});
  Object.defineProperty(el,'textContent',{get(){return this._text},set(v){this._text=v;this.writes++}});
  return el;
}
const nodes={'save-status':element('save-status'),'save-status-tooltip':element('save-status-tooltip')};
nodes['save-status-tooltip'].rect={left:0,top:0,right:160,bottom:30,width:160,height:30};
nodes['save-status'].attrs['aria-label']='Auto-save'; // as the markup ships it
const state={quitPreparing:false,lutPersistence:null,saveStatus:null,autoSaveRetry:null};
const annotationStore={
  flushOnPagehide(...args){calls.push(['ann.flushOnPagehide',...args]);return Promise.resolve({ok:true,sent:true})},
  retryFailed(...args){calls.push(['ann.retryFailed',...args]);return Promise.resolve({ok:true,retried:0})}};
const lutPersistence={dirty:false,isDirty(){return this.dirty},
  flush(...args){calls.push(['lut.flush',...args]);return Promise.resolve()}};
const window=eventTarget({innerWidth:800,innerHeight:600,
  Nd2SaveStatus:{createTracker:options=>SaveStatus.createTracker({...options,now:()=>now,setInterval,clearInterval})}});
const document=eventTarget({});
const context={state,window,document,setTimeout,clearTimeout,setInterval,clearInterval,annotationStore,$:id=>nodes[id]||null};
vm.createContext(context);
const start=source.indexOf('/* ---- auto-save status'),end=source.indexOf('function pixelSize(',start);
if(start<0||end<0)throw new Error('auto-save source bounds not found');
vm.runInContext(source.slice(start,end),context);
const button=nodes['save-status'],tooltip=nodes['save-status-tooltip'];
const icon=()=>({hidden:button.hidden,state:button.attrs['data-state']??null,label:button.attrs['aria-label']});
const fire=(target,type,event={})=>(target.handlers[type]||[]).forEach(fn=>fn(event));
const report=(channel,event)=>state.saveStatus.report(channel,event);
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", HARNESS + "(async()=>{" + body + "})().then(v=>process.stdout.write(JSON.stringify(v)))"
         ".catch(e=>{console.error(e);process.exitCode=1});", str(STATIC)],
        capture_output=True, text=True, timeout=20,
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
        {"hidden": False, "state": "failed", "label": "Auto-save: not saved"},
        {"hidden": False, "state": "conflict", "label": "Auto-save: conflict"},
        {"hidden": False, "state": "saved", "label": "Auto-save: saved"},
    ]
    # two attribute writes per state change; none for the 59 ticks of "N s ago"
    # nor for a repeated report of the same state
    assert out["writes"] == [0, 6, 6, 6]
    assert out["text"] == "Auto-saved 0 s ago"


def test_tooltip_shows_on_focus_or_hover_follows_the_ticker_while_shown_and_is_not_written_while_hidden():
    out = run("""
context.wireAutoSave();
fire(button,'focus');const idleFocus=tooltip.hidden;fire(button,'blur');
report('annotations',{state:'saved'});
advance(2000);
const hiddenWrites=tooltip.writes;
fire(button,'focus');
const shown={hidden:tooltip.hidden,text:tooltip.textContent,left:tooltip.style.left,top:tooltip.style.top};
advance(1000);const tick1=tooltip.textContent;advance(1000);const tick2=tooltip.textContent;
fire(button,'blur');const blurred=tooltip.hidden;const writesAtBlur=tooltip.writes;
advance(3000);const writesWhileHidden=tooltip.writes-writesAtBlur;
fire(button,'pointerenter');const immediately=tooltip.hidden;
advance(599);const early=tooltip.hidden;advance(1);
const hovered={hidden:tooltip.hidden,text:tooltip.textContent};
report('lut',{state:'unavailable',message:'LUT settings are not saved for this source.'});
const details={text:tooltip.textContent,label:button.attrs['aria-label']};
fire(document,'keydown',{key:'Escape'});const escaped=tooltip.hidden;
fire(button,'pointerleave');fire(button,'pointerenter');advance(600);const again=tooltip.hidden;
fire(button,'pointerleave');const left=tooltip.hidden;const writesAtLeave=tooltip.writes;
advance(5000);
return {idleFocus,hiddenWrites,shown,tick1,tick2,blurred,writesWhileHidden,immediately,early,hovered,details,
  escaped,again,left,writesAfterLeave:tooltip.writes-writesAtLeave,timers:timers.size};
""")
    assert out["idleFocus"] is True and out["hiddenWrites"] == 0
    assert out["shown"] == {"hidden": False, "text": "Auto-saved 2 s ago", "left": "629px", "top": "542px"}
    assert out["tick1"] == "Auto-saved 3 s ago" and out["tick2"] == "Auto-saved 4 s ago"
    assert out["blurred"] is True and out["writesWhileHidden"] == 0
    assert out["immediately"] is True and out["early"] is True
    assert out["hovered"] == {"hidden": False, "text": "Auto-saved 7 s ago"}
    assert out["details"] == {"text": "Auto-saved 7 s ago\nLUT settings are not saved for this source.",
                              "label": "Auto-save: saved"}
    assert out["escaped"] is True and out["again"] is False and out["left"] is True
    assert out["writesAfterLeave"] == 0 and out["timers"] == 0


def test_an_invalid_lut_edit_explains_itself_in_the_tooltip_and_is_not_retried():
    out = run("""
context.wireAutoSave();state.lutPersistence=lutPersistence;
report('lut',{state:'dirty',message:"LUT values are invalid for this image's pixel type."});
fire(button,'focus');
const shown={icon:icon(),text:tooltip.textContent};
advance(60000);
return {shown,calls};
""")
    assert out["shown"]["icon"] == {"hidden": False, "state": "dirty", "label": "Auto-save: unsaved changes"}
    assert out["shown"]["text"] == "Unsaved changes…\nLUT values are invalid for this image's pixel type."
    # the controller reports it as not dirty, so the minute retry leaves it alone
    assert out["calls"] == [["ann.retryFailed"]]


def test_markup_places_the_icon_after_the_scale_bar_and_loads_the_tracker_before_the_app():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    footer = html[html.index('<footer id="statusbar">'):html.index("</footer>")]
    assert footer.index('id="scalebar-wrap"') < footer.index('id="save-status"')
    assert footer.rstrip().endswith("</button>")
    assert '<button type="button" id="save-status" class="save-status" hidden aria-describedby="save-status-tooltip" aria-label="Auto-save">' in footer
    assert '<div id="save-status-tooltip" class="lut-tooltip" role="tooltip" hidden></div>' in html
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
