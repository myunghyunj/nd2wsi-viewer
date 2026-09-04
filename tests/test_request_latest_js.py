"""Frame ownership and stale async completion guards in production JavaScript."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = (
    Path(__file__).resolve().parents[1]
    / "nd2wsi"
    / "static"
    / "request-latest-v1.js"
)
APP = MODULE.with_name("app.js")
INDEX = MODULE.with_name("index.html")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(script):
    result = subprocess.run(
        [NODE, "-e", script, str(MODULE)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_production_loads_helper_and_separates_backing_from_user_frames():
    app = APP.read_text()
    index = INDEX.read_text()

    assert index.index("request-latest-v1.js") < index.index("app.js")
    assert "function activeFrameParams()" in app
    assert "function backingFrameParams()" in app
    assert "appendFrameParams(renderParams(new URLSearchParams()), backingFrameParams())" in app
    assert "plateFrameParams" not in app
    assert "withPlateParams" not in app
    assert 'fetch("api/pixel?"' in app and "requested.context.frame" in app
    assert 'fetch("api/histogram"' in app and "responseMatchesFrameContext(data, context)" in app
    assert "function normalizeAnnotationSite(site)" in app
    assert "normalizeAnnotationSite(site === undefined ? state.annSite : site)" in app
    assert "appendFrameParams(q, context.frame); // explicit active frame" in app


def test_grid_has_no_active_frame_but_osd_has_an_explicit_backing_frame():
    out = _run(
        r"""
const {plateFrame,appendFrameParams,identityKey}=require(process.argv[1]);
const plate={t:4,z:2,focus:null};
const active=plateFrame(plate,(p)=>plate.z,false);
const backing=plateFrame(plate,(p)=>plate.z,true);
const q=new URLSearchParams(); appendFrameParams(q,backing);
plate.focus=3;
const focused=plateFrame(plate,(p)=>p+1,false);
process.stdout.write(JSON.stringify({
  active,backing,query:q.toString(),focused,
  slideKey:identityKey("slide-a","g1",null),
  frameKey:identityKey("slide-a","g1",focused),
}));
"""
    )
    assert out["active"] is None
    assert out["backing"] == {"t": 4, "p": 0, "z": 2}
    assert out["query"] == "t=4&p=0&z=2"
    assert out["focused"] == {"t": 4, "p": 3, "z": 4}
    assert out["slideKey"] != out["frameKey"]


def test_site_owned_state_survives_t_and_z_but_not_site_or_grid_changes():
    out = _run(
        r"""
const {frameOwnsSite}=require(process.argv[1]);
const sameSiteLaterFrame={t:8,p:3,z:5};
process.stdout.write(JSON.stringify({
  same:frameOwnsSite(sameSiteLaterFrame,3),
  other:frameOwnsSite(sameSiteLaterFrame,2),
  grid:frameOwnsSite(null,3),
  nullSite:frameOwnsSite(sameSiteLaterFrame,null),
  undefinedSite:frameOwnsSite(sameSiteLaterFrame,undefined),
  emptySite:frameOwnsSite(sameSiteLaterFrame,""),
}));
"""
    )
    assert out == {
        "same": True,
        "other": False,
        "grid": False,
        "nullSite": False,
        "undefinedSite": False,
        "emptySite": False,
    }


def test_only_latest_async_success_and_finalizer_can_apply():
    out = _run(
        r"""
const {LatestRequestGate}=require(process.argv[1]);
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
const gate=new LatestRequestGate();
const applied=[]; const finished=[];
const run=(key,d)=>{
  const ticket=gate.begin(key);
  const promise=d.promise.then((value)=>{
    if(gate.isCurrent(ticket,key)) applied.push(value);
  }).finally(()=>{ if(gate.finish(ticket)) finished.push(key); });
  return {ticket,promise};
};
(async()=>{
  const a=deferred(), b=deferred();
  const ar=run("A",a), br=run("B",b);
  a.resolve("old"); await ar.promise;
  const bStillCurrent=gate.isCurrent(br.ticket,"B");
  b.resolve("new"); await br.promise;
  process.stdout.write(JSON.stringify({
    applied,finished,bStillCurrent,aAborted:ar.ticket.signal.aborted,
  }));
})().catch((error)=>{console.error(error);process.exit(1)});
"""
    )
    assert out == {
        "applied": ["new"],
        "finished": ["B"],
        "bStillCurrent": True,
        "aAborted": True,
    }


def test_stale_failure_and_identity_mismatch_cannot_replace_current_state():
    out = _run(
        r"""
const {LatestRequestGate,identityKey,responseIdentityMatches,isAbortError}=require(process.argv[1]);
const gate=new LatestRequestGate();
const context={sourceId:"s1",generation:"g2",frame:{t:1,p:2,z:3}};
context.key=identityKey(context.sourceId,context.generation,context.frame);
const old=gate.begin("old");
const current=gate.begin(context.key);
const failures=[];
const recordFailure=(ticket,error)=>{
  if(gate.isCurrent(ticket) && !isAbortError(error)) failures.push(error.message);
};
recordFailure(old,new Error("stale failure"));
recordFailure(current,Object.assign(new Error("cancelled"),{name:"AbortError"}));
const exact=responseIdentityMatches({generation:"g2",frame:{t:1,p:2,z:3}},context);
const wrongFrame=responseIdentityMatches({generation:"g2",frame:{t:1,p:2,z:4}},context);
const missing=responseIdentityMatches({frame:{t:1,p:2,z:3}},context);
process.stdout.write(JSON.stringify({failures,exact,wrongFrame,missing,oldAborted:old.signal.aborted}));
"""
    )
    assert out == {
        "failures": [],
        "exact": True,
        "wrongFrame": False,
        "missing": False,
        "oldAborted": True,
    }
