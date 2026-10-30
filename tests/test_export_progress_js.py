"""Region-export progress: one poll chain per job, every failure reported.

The production trackExport and its gauge helpers are sliced from ``app.js``
and run with fake timers, a fake clock and a scripted progress endpoint.
"""
import json
from pathlib import Path

from node_host import run_node

APP = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "app.js"

HARNESS = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
let now=0,nextTimer=1;
const timers=new Map(),toasts=[],requests=[],inFlight=new Map(),maxInFlight=new Map();
const progress={},held=new Set(),waiting=new Map();
function setTimeout(fn,ms){const id=nextTimer++;timers.set(id,{fn,due:now+ms});return id;}
function clearTimeout(id){timers.delete(id);}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function advance(ms){
  const end=now+ms;
  for(;;){
    await settle();
    const next=[...timers].sort((a,b)=>a[1].due-b[1].due)[0];
    if(!next||next[1].due>end)break;
    timers.delete(next[0]);now=next[1].due;next[1].fn();
  }
  now=end;await settle();
}
function answer(job){
  inFlight.set(job,inFlight.get(job)-1);
  const body=progress[job];
  return body===undefined?{ok:false,json:()=>Promise.reject(new Error('no body'))}
    :{ok:true,json:()=>Promise.resolve(body)};
}
function fetch(url){
  const job=new URL(url,'http://viewer.test/').searchParams.get('job');
  requests.push(job);
  inFlight.set(job,(inFlight.get(job)||0)+1);
  maxInFlight.set(job,Math.max(maxInFlight.get(job)||0,inFlight.get(job)));
  if(held.has(job))return new Promise(resolve=>waiting.set(job,()=>resolve(answer(job))));
  return Promise.resolve(answer(job));
}
function release(job){held.delete(job);const go=waiting.get(job);waiting.delete(job);if(go)go();}
const classes=new Set();
const nodes={'export-cell':{hidden:true,classList:{add:k=>classes.add(k),remove:k=>classes.delete(k)}},
  'export-fill':{style:{}},'export-pct':{textContent:''}};
const gauge=()=>({hidden:nodes['export-cell'].hidden,done:classes.has('done'),
  width:nodes['export-fill'].style.width||'',text:nodes['export-pct'].textContent});
const context={$:id=>nodes[id],fetch,setTimeout,clearTimeout,Date:{now:()=>now},
  showToast:t=>toasts.push(t)};
vm.createContext(context);
const start=source.indexOf('/* Poll the server'),end=source.indexOf('/* ---- slide inspector',start);
if(start<0||end<0)throw new Error('export progress source not found');
vm.runInContext(source.slice(start,end),context);
const tracked=()=>[...vm.runInContext('exportJobs',context).keys()];
"""


def run(body):
    script = HARNESS + "(async()=>{" + body + "})().then(v=>process.stdout.write(JSON.stringify(v)))" \
        ".catch(e=>{console.error(e);process.exitCode=1});"
    result = run_node(script, APP)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_second_export_does_not_hide_the_first_exports_failure():
    out = run("""
progress.a={state:'writing',pct:10};
context.trackExport('a','ND2');await advance(400);
const first=gauge();
progress.b={state:'writing',pct:3};
context.trackExport('b','TIFF');await advance(400);
const second=gauge();
progress.a={state:'error',error:'disk full'};await advance(400);
const afterError={gauge:gauge(),toasts:[...toasts],tracked:tracked()};
progress.b={state:'writing',pct:60};await advance(400);
return {first,second,afterError,later:gauge()};
""")
    assert out["first"] == {"hidden": False, "done": False, "width": "10%", "text": "ND2 10 %"}
    # the newest writing export owns the gauge
    assert out["second"] == {"hidden": False, "done": False, "width": "3%", "text": "TIFF 3 %"}
    assert out["afterError"]["toasts"] == ["export failed: disk full"]
    assert out["afterError"]["tracked"] == ["b"]
    assert out["afterError"]["gauge"]["hidden"] is False
    assert out["later"] == {"hidden": False, "done": False, "width": "60%", "text": "TIFF 60 %"}


def test_each_job_keeps_one_progress_request_in_flight():
    out = run("""
progress.a={state:'writing',pct:5};held.add('a');
context.trackExport('a','ND2');
await advance(5000);
const whileHeld=requests.length;
release('a');await advance(1000);
return {whileHeld,after:requests.length,max:maxInFlight.get('a'),pending:inFlight.get('a')};
""")
    # a slow server collects one poll per job, not one every 350 ms
    assert out["whileHeld"] == 1
    assert out["after"] == 1 + 2 and out["max"] == 1 and out["pending"] == 0


def test_finished_export_shows_done_then_hides_and_stops_polling():
    out = run("""
progress.a={state:'streaming',pct:100};
context.trackExport('a','TIFF');await advance(400);
const saving=gauge();
progress.a={state:'done',pct:100};await advance(400);
const done=gauge(),polls=requests.length;
await advance(1800);const held=gauge();
await advance(200);
return {saving,done,held,hidden:gauge().hidden,polls,later:requests.length,tracked:tracked(),toasts};
""")
    assert out["saving"]["text"] == "TIFF saving…"
    assert out["done"] == {"hidden": False, "done": True, "width": "100%", "text": "TIFF done"}
    assert out["held"]["hidden"] is False and out["hidden"] is True
    assert out["later"] == out["polls"] and out["tracked"] == [] and out["toasts"] == []


def test_an_export_that_finishes_before_its_first_poll_still_shows_done():
    out = run("""
progress.a={state:'done',pct:100};
context.trackExport('a','TIFF');await advance(400);
const done=gauge(),polls=requests.length;
await advance(2000);
return {done,hidden:gauge().hidden,polls,later:requests.length,tracked:tracked(),toasts};
""")
    # ND2 and TIFF exports show no toast, so the gauge is the only word they finished
    assert out["done"] == {"hidden": False, "done": True, "width": "100%", "text": "TIFF done"}
    assert out["hidden"] is True
    assert out["polls"] == out["later"] == 1 and out["tracked"] == [] and out["toasts"] == []


def test_a_fast_export_does_not_take_the_gauge_from_a_newer_one_that_is_writing():
    out = run("""
progress.b={state:'writing',pct:30};held.add('a');
context.trackExport('a','ND2');context.trackExport('b','TIFF');await advance(400);
progress.a={state:'done',pct:100};release('a');await advance(0);
return {gauge:gauge(),tracked:tracked()};
""")
    assert out["gauge"] == {"hidden": False, "done": False, "width": "30%", "text": "TIFF 30 %"}
    assert out["tracked"] == ["b"]


def test_a_finished_export_keeps_done_on_the_gauge_before_an_older_export_takes_it_back():
    out = run("""
progress.a={state:'writing',pct:20};progress.b={state:'writing',pct:50};
context.trackExport('a','ND2');context.trackExport('b','TIFF');await advance(400);
progress.b={state:'done',pct:100};await advance(400);
const done=gauge();
progress.a={state:'writing',pct:40};await advance(1000);
const held=gauge();await advance(1400);
return {done,held,resumed:gauge()};
""")
    assert out["done"]["text"] == "TIFF done"
    assert out["held"]["text"] == "TIFF done"
    assert out["resumed"] == {"hidden": False, "done": False, "width": "40%", "text": "ND2 40 %"}


def test_a_job_that_never_starts_writing_stays_hidden_and_stops_after_the_grace():
    out = run("""
context.trackExport('gone','ND2');
await advance(29000);const early=tracked();
await advance(2000);const polls=requests.length;
await advance(5000);
return {early,late:tracked(),polls,after:requests.length,gauge:gauge(),toasts};
""")
    # an unknown job answers {} (or a failed poll); a slow start is still followed
    assert out["early"] == ["gone"] and out["late"] == []
    assert out["after"] == out["polls"]
    assert out["gauge"]["hidden"] is True and out["toasts"] == []


def test_a_failing_poll_is_retried_and_a_late_error_is_still_reported():
    out = run("""
context.trackExport('a','ND2');
await advance(2000);
progress.a={state:'writing',pct:1};await advance(400);
progress.a={state:'error',error:'client disconnected'};await advance(400);
return {toasts,tracked:tracked(),gauge:gauge()};
""")
    assert out == {"toasts": ["export failed: client disconnected"], "tracked": [],
                   "gauge": {"hidden": True, "done": False, "width": "1%", "text": "ND2 1 %"}}
