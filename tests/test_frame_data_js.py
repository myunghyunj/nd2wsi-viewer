"""Exercise production frame-data controllers with controllable network/timers."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/frame-data-v1.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const path=require('path');
const F=require(process.argv[1]);
const R=require(path.join(path.dirname(process.argv[1]),'request-latest-v1.js'));
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function fixture(){
  let time=0,next=1;const timers=new Map(),requests=[],applied=[],cleared=[],rendered=[],statuses=[];
  const clock={now:()=>time,setTimer:(fn,delay)=>{const id=next++;timers.set(id,{at:time+delay,fn});return id},clearTimer:id=>timers.delete(id)};
  function tick(to){while(true){const due=[...timers].filter(([id,t])=>t.at<=to).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;time=due[1].at;timers.delete(due[0]);due[1].fn()}time=to;}
  function frame(p){const frame={t:2,p,z:3};return {frame,generation:'g1',sourceId:'slide',key:R.identityKey('slide','g1',frame)}}
  let context=frame(0);
  const shared={...clock,readContext:()=>context,matchesResponse:R.responseIdentityMatches,
    appendFrameParams:R.appendFrameParams,isAbortError:R.isAbortError,
    fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,reject,resolve:data=>resolve({ok:true,json:()=>Promise.resolve(data)})}))};
  const histState={requests:new R.LatestRequestGate(),timer:null};
  const pixelState={requests:new R.LatestRequestGate(),timer:null,cursor:null,queued:null,inFlight:null,result:null,resultKey:null,lastStarted:0,retryAfter:0,failed:false};
  const hist=F.createHistogramController({...shared,state:histState,onStatus:s=>statuses.push(s),onClear:unavailable=>cleared.push(unavailable),onHistograms:channels=>applied.push(channels)});
  const pixel=F.createPixelProbeController({...shared,state:pixelState,onRender:()=>rendered.push({result:pixelState.result,failed:pixelState.failed}),onClearCursor:()=>cleared.push('cursor')});
  return {tick,requests,applied,cleared,rendered,statuses,timers,hist,histState,pixel,pixelState,frame,
    setContext:value=>{context=value},payload:(extra={})=>({generation:context.generation,frame:context.frame,...extra})};
}
(async()=>{
"""


def run(script):
    result = subprocess.run(
        [NODE, "-e", HARNESS + script + "\n})().catch(e=>{console.error(e);process.exit(1)});", str(MODULE)],
        check=True, capture_output=True, text=True, timeout=20,
    )
    return json.loads(result.stdout)


def test_histogram_invalidates_before_debounce_and_checks_response_identity():
    out = run(r"""
const f=fixture(); f.hist.schedule(0);const oldPayload=f.payload({channels:['old']});
f.setContext(f.frame(4));f.hist.schedule(300);
f.requests[0].resolve(oldPayload);await flush();
const during={applied:[...f.applied],aborted:f.requests[0].options.signal.aborted,requests:f.requests.length};
f.tick(300);f.requests[1].resolve({...f.payload({channels:['wrong-generation']}),generation:'g0'});await flush();
const mismatch=[...f.applied];f.hist.schedule(0);f.requests[2].resolve(f.payload({channels:['current']}));await flush();
f.setContext(null);f.hist.schedule(0);
process.stdout.write(JSON.stringify({during,mismatch,applied:f.applied,urls:f.requests.map(r=>r.url),cleared:f.cleared}));
""")
    assert out["during"] == {"applied": [], "aborted": True, "requests": 1}
    assert out["mismatch"] == []
    assert out["applied"] == [["current"]]
    assert out["urls"] == [
        "api/histogram?t=2&p=0&z=3", "api/histogram?t=2&p=4&z=3",
        "api/histogram?t=2&p=4&z=3",
    ]
    assert out["cleared"] == [False, False, False, False, True]


def test_stale_histogram_failure_cannot_clear_new_request_or_its_result():
    out = run(r"""
const f=fixture();f.hist.schedule(0);f.setContext(f.frame(1));f.hist.schedule(0);
const current=f.histState.requests._active;
f.requests[0].reject(new Error('old frame failed'));await flush();
const currentSurvives=f.histState.requests.isCurrent(current);
f.requests[1].resolve(f.payload({channels:['new']}));await flush();
process.stdout.write(JSON.stringify({currentSurvives,cleared:f.cleared,applied:f.applied}));
""")
    assert out == {"currentSurvives": True, "cleared": [False, False], "applied": [["new"]]}


def test_histogram_retries_transient_failure_and_recovers_same_frame():
    out = run(r"""
const f=fixture();f.hist.schedule(0);f.requests[0].reject(new Error('HTTP503'));await flush();
f.tick(499);const before=f.requests.length;f.tick(500);
f.requests[1].resolve(f.payload({channels:['recovered']}));await flush();f.tick(60000);
process.stdout.write(JSON.stringify({before,requests:f.requests.length,applied:f.applied,statuses:f.statuses,timers:f.timers.size}));
""")
    assert out == {"before": 1, "requests": 2, "applied": [["recovered"]],
                   "statuses": ["loading", "retrying", "ready"], "timers": 0}


def test_histogram_retries_are_bounded_and_explicit_retry_can_recover():
    out = run(r"""
const f=fixture();f.hist.schedule(0);
for(const [index,nextTime] of [[0,500],[1,2000],[2,6000],[3,60000]]){
  f.requests[index].reject(new Error('offline'));await flush();f.tick(nextTime);
}
const failed={requests:f.requests.length,status:f.statuses.at(-1),timers:f.timers.size};
f.hist.schedule(0);f.requests[4].resolve(f.payload({channels:['manual-retry']}));await flush();
process.stdout.write(JSON.stringify({failed,status:f.statuses.at(-1),applied:f.applied}));
""")
    assert out == {"failed": {"requests": 4, "status": "failed", "timers": 0},
                   "status": "ready", "applied": [["manual-retry"]]}


def test_new_frame_cancels_pending_histogram_retry_and_dispose_aborts():
    out = run(r"""
const f=fixture();f.hist.schedule(0);f.requests[0].reject(new Error('old'));await flush();
f.setContext(f.frame(2));f.hist.schedule(300);f.tick(300);
f.requests[1].resolve(f.payload({channels:['new-frame']}));await flush();f.tick(10000);
const recovered={requests:f.requests.length,applied:[...f.applied]};
f.hist.schedule(0);const pending=f.requests[2];f.hist.dispose();
pending.reject(new Error('network cancelled'));await flush();f.hist.schedule(0);f.tick(20000);
process.stdout.write(JSON.stringify({recovered,aborted:pending.options.signal.aborted,requests:f.requests.length,timers:f.timers.size,status:f.statuses.at(-1)}));
""")
    assert out == {"recovered": {"requests": 2, "applied": [["new-frame"]]},
                   "aborted": True, "requests": 3, "timers": 0, "status": "loading"}


def test_aborted_and_changed_context_failures_do_not_schedule_retries():
    out = run(r"""
const f=fixture();f.hist.schedule(0);
f.requests[0].reject(Object.assign(new Error('aborted'),{name:'AbortError'}));await flush();
f.hist.schedule(0);f.setContext(f.frame(3));f.requests[1].reject(new Error('stale'));await flush();
f.tick(60000);process.stdout.write(JSON.stringify({requests:f.requests.length,timers:f.timers.size,statuses:f.statuses}));
""")
    assert out == {"requests": 2, "timers": 0, "statuses": ["loading", "loading"]}


def test_pixel_queue_coalesces_cursor_and_preserves_new_frame_ownership():
    out = run(r"""
const f=fixture(),s=f.pixelState;s.cursor={x:10,y:20};f.pixel.queue(10,20);f.tick(100);
s.cursor={x:11,y:21};f.pixel.queue(11,21);s.cursor={x:12,y:22};f.pixel.queue(12,22);
f.requests[0].resolve(f.payload({x:10,y:20,values:['old-cursor']}));await flush();
const coalesced={result:s.result,requests:f.requests.length};f.tick(200);
const oldPayload=f.payload({x:12,y:22,values:['old-frame']});
f.setContext(f.frame(1));f.pixel.invalidate();f.tick(300);const current=s.inFlight;
f.requests[1].resolve(oldPayload);await flush();
const ownsNew=s.inFlight===current&&s.requests.isCurrent(current);
f.requests[2].resolve(f.payload({x:12,y:22,values:['current']}));await flush();
const result=s.result;f.setContext(null);f.pixel.invalidate();
process.stdout.write(JSON.stringify({coalesced,ownsNew,result,urls:f.requests.map(r=>r.url),aborted:f.requests[1].options.signal.aborted,cleared:f.cleared,cursor:s.cursor,finalResult:s.result}));
""")
    assert out["coalesced"] == {"result": None, "requests": 1}
    assert out["ownsNew"] and out["aborted"]
    assert out["result"]["values"] == ["current"]
    assert out["urls"] == [
        "api/pixel?x=10&y=20&t=2&p=0&z=3",
        "api/pixel?x=12&y=22&t=2&p=0&z=3",
        "api/pixel?x=12&y=22&t=2&p=1&z=3",
    ]
    assert out["cleared"] == ["cursor"]
    assert out["cursor"] is None and out["finalResult"] is None


def test_pixel_failure_backoff_and_frame_invalidation_allow_recovery():
    out = run(r"""
const f=fixture(),s=f.pixelState;s.cursor={x:1,y:2};f.pixel.queue(1,2);f.tick(100);
f.requests[0].reject(new Error('busy'));await flush();
const failure={failed:s.failed,retryAfter:s.retryAfter};f.pixel.queue(1,2);f.tick(2599);
const waiting=f.requests.length;f.setContext(f.frame(3));f.pixel.invalidate();f.tick(2599);
f.requests[1].resolve(f.payload({x:1,y:2,values:['recovered']}));await flush();
process.stdout.write(JSON.stringify({failure,waiting,requests:f.requests.length,failed:s.failed,retryAfter:s.retryAfter,result:s.result.values}));
""")
    assert out == {
        "failure": {"failed": True, "retryAfter": 2600}, "waiting": 1,
        "requests": 2, "failed": False, "retryAfter": 0, "result": ["recovered"],
    }
