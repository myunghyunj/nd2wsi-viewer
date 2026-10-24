"""Per-file LUT persistence: strict restore and serialized durable-save lifecycle."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/lut-persistence-v1.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const {createController,FORMAT}=require(process.argv[1]);
const channel=(start=0,end=255)=>({label:'same label',window:{start,end}});
const lut=(lo=10,hi=200,gamma=0.37)=>({lo,hi,gamma});
const ok=payload=>({ok:true,json:async()=>payload});
const loaded=luts=>ok({ok:true,available:true,settings:luts===null?null:{format:FORMAT,luts}});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
const rejected=async promise=>{try{await promise;return null}catch(error){return error.message}};
function setup(options={}) {
  let now=0,id=0,luts=options.luts||[null];
  const timers=new Map(),calls=[],warnings=[],events=[];
  const fetch=options.fetch|| (async(url,opts)=>opts.method==='POST'?ok({ok:true}):loaded(null));
  const controller=createController({info:options.info||{dtype:'uint16',channels:[channel()]},
    readLuts:()=>luts,fetch:(url,opts={})=>{calls.push({url,...opts});return fetch(url,opts)},
    reportError:text=>warnings.push(text),endpoint:options.endpoint||'api/lut-settings',
    onSaveState:event=>events.push(event),now:()=>now,
    setTimeout:(fn,ms)=>{const key=++id;timers.set(key,{fn,due:now+ms});return key},
    clearTimeout:key=>timers.delete(key)});
  const advance=async ms=>{const end=now+ms;while(true){
    const next=[...timers].filter(([,v])=>v.due<=end).sort((a,b)=>a[1].due-b[1].due)[0];
    if(!next)break;now=next[1].due;timers.delete(next[0]);next[1].fn();await settle();
  }now=end;await settle()};
  return {controller,calls,warnings,timers,events,advance,set:value=>{luts=value},
    states:()=>events.map(event=>event.state),
    posts:()=>calls.filter(call=>call.method==='POST').map(call=>JSON.parse(call.body))};
}
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", HARNESS + "(async()=>{" + body
         + "})().then(value=>console.log(JSON.stringify(value))).catch(error=>{console.error(error);process.exitCode=1});", str(MODULE)],
        capture_output=True, encoding="utf-8", check=True, timeout=20,
    )
    return json.loads(result.stdout)


def test_restore_precedes_auto_fallback_and_unchanged_close_never_posts():
    out = run("""
const wait=deferred(),h=setup({fetch:()=>wait.promise});let auto=0;
const initializing=h.controller.load().then(luts=>{if(!luts)auto++;return luts});
await settle();const before=auto;wait.resolve(loaded([lut()]));
const restored=await initializing;restored[0].gamma=4;
await h.controller.flush();const again=await h.controller.load();
return {before,auto,posts:h.posts(),calls:h.calls.length,again,warnings:h.warnings};
""")
    assert out == {"before": 0, "auto": 0, "posts": [], "calls": 1, "again": None, "warnings": []}


def test_absent_settings_allow_auto_without_saving_on_load_or_close():
    out = run("""
const h=setup();let auto=0;if(!(await h.controller.load()))auto++;
await h.controller.flush();return {auto,posts:h.posts(),warnings:h.warnings};
""")
    assert out == {"auto": 1, "posts": [], "warnings": []}


def test_backend_warning_is_visible_without_writing_on_read_or_unchanged_close():
    out = run("""
const h=setup({fetch:async()=>ok({ok:true,available:true,settings:null,
 warning:'Saved LUT settings do not match this source.'})});
const restored=await h.controller.load();await h.controller.flush();
return {restored,posts:h.posts(),warnings:h.warnings};
""")
    assert out == {"restored": None, "posts": [],
                   "warnings": ["Saved LUT settings do not match this source."]}


def test_saves_effective_defaults_exact_fractional_values_and_only_lut_fields():
    out = run("""
const h=setup({info:{dtype:'uint16',channels:[channel(0,0.75),channel(129,672)]},
  luts:[null,lut(127.25,4363.75,0.37)]});
await h.controller.load();h.controller.changed();await h.controller.flush();return h.posts();
""")
    assert out == [{"format": "nd2wsi-lut-settings/1", "luts": [
        {"lo": 0, "hi": 0.75, "gamma": 1}, {"lo": 127.25, "hi": 4363.75, "gamma": 0.37}]}]


@pytest.mark.parametrize("bad", [
    "[]", "[null]", "[lut(20,10)]", "[lut(0,0.5)]", "[lut(0,200,0.24)]",
    "[lut(0,200,4.01)]", "[lut('1',200)]", "[lut(0,Infinity)]",
    "[{...lut(),color:'FF0000'}]", "[lut(),lut()]",
])
def test_invalid_saved_values_reject_whole_array_without_applying_or_overwriting(bad):
    out = run(f"""
const h=setup({{fetch:async()=>loaded({bad})}});
const restored=await h.controller.load();await h.controller.flush();
h.set([lut(20,150)]);h.controller.changed();const failure=await rejected(h.controller.flush());
return {{restored,posts:h.posts(),warnings:h.warnings,failure}};
""")
    assert out["restored"] is None and out["posts"] == []
    assert out["warnings"] and out["failure"]


def test_entire_array_is_rejected_when_only_one_channel_is_invalid():
    out = run("""
const h=setup({info:{dtype:'uint16',channels:[channel(),channel()]},
  fetch:async()=>loaded([lut(),lut(9,8)])});
return {restored:await h.controller.load(),warnings:h.warnings};
""")
    assert out["restored"] is None and len(out["warnings"]) == 1


def test_restore_dtype_and_metadata_limits_preserve_fractional_default_exception():
    out = run("""
const cases=[{dtype:'uint16',channels:[channel(0,0.75)],value:lut(0,0.75,1)},
  {dtype:'float32',channels:[channel()],value:lut(0,0.75,0.25)},
  {dtype:'uint16',channels:[channel(0,0.75)],value:lut(0,0.75,0.5)},
  {dtype:16,channels:[channel()],value:lut()},
  {dtype:'uint16',channels:[],value:lut()},
  {dtype:'uint16',channels:Array(513).fill(channel()),value:lut()},
  {dtype:'uint16',channels:[channel(3,3)],value:lut()}];
const results=[];for(const item of cases){const h=setup({info:item,fetch:async()=>loaded([item.value])});
  results.push({restored:await h.controller.load(),warned:!!h.warnings.length})}return results;
""")
    assert out[:2] == [
        {"restored": [{"lo": 0, "hi": 0.75, "gamma": 1}], "warned": False},
        {"restored": [{"lo": 0, "hi": 0.75, "gamma": 0.25}], "warned": False},
    ]
    assert all(row == {"restored": None, "warned": True} for row in out[2:])


def test_rapid_changes_debounce_and_snapshot_values_before_delayed_save():
    out = run("""
const h=setup();await h.controller.load();const first=lut();h.set([first]);h.controller.changed();
await h.advance(200);h.set([lut(20,180)]);h.controller.changed();await h.advance(249);
const before=h.posts();await h.advance(1);first.lo=999;await h.controller.flush();
return {before,posts:h.posts()};
""")
    assert out == {"before": [], "posts": [{"format": "nd2wsi-lut-settings/1", "luts": [
        {"lo": 20, "hi": 180, "gamma": 0.37}]}]}


def test_flush_serializes_inflight_changes_and_coalesces_to_latest_revision():
    out = run("""
const waits=[],h=setup({fetch:async(url,opts)=>{if(opts.method!=='POST')return loaded(null);
  const wait=deferred();waits.push(wait);return wait.promise}});
await h.controller.load();h.set([lut(1,100)]);h.controller.changed();let done=false;
const flushing=h.controller.flush().then(()=>{done=true});await settle();
h.set([lut(2,100)]);h.controller.changed();h.set([lut(3,100)]);h.controller.changed();
await h.advance(250);const whileFirst=h.posts().length;waits[0].resolve(ok({ok:true}));await settle();
const beforeLast={done,posts:h.posts()};waits[1].resolve(ok({ok:true}));await flushing;
await h.controller.flush();return {whileFirst,beforeLast,done,posts:h.posts()};
""")
    assert out["whileFirst"] == 1 and out["beforeLast"]["done"] is False
    assert [row["luts"][0]["lo"] for row in out["posts"]] == [1, 3]
    assert out["done"] is True


def test_failed_save_keeps_dirty_and_explicit_flush_retries_same_snapshot():
    out = run("""
let attempts=0;const h=setup({fetch:async(url,opts)=>{
  if(opts.method!=='POST')return loaded(null);if(++attempts===1)throw new Error('offline');return ok({ok:true})}});
await h.controller.load();h.set([lut()]);h.controller.changed();
const failure=await rejected(h.controller.flush());await h.controller.flush();await h.controller.flush();
return {failure,attempts,posts:h.posts(),warnings:h.warnings};
""")
    assert "offline" in out["failure"] and out["attempts"] == 2
    assert out["posts"][0] == out["posts"][1] and len(out["warnings"]) == 1


@pytest.mark.parametrize("response", ["{ok:false,json:async()=>({ok:true})}", "ok({ok:false})", "ok(null)"])
def test_http_and_api_save_failures_are_not_reported_as_saved(response):
    out = run(f"""
const h=setup({{fetch:async(url,opts)=>opts.method==='POST'?{response}:loaded(null)}});
await h.controller.load();h.controller.changed();const first=await rejected(h.controller.flush());
const second=await rejected(h.controller.flush());return {{first,second,count:h.posts().length}};
""")
    assert out["first"] and out["second"] and out["count"] == 2


def test_read_failure_is_nonfatal_and_successful_retry_does_not_replace_new_edits():
    out = run("""
let gets=0;const h=setup({fetch:async(url,opts)=>{
  if(opts.method==='POST')return ok({ok:true});if(++gets===1)throw new Error('offline');return loaded([lut(5,50)])}});
const restored=await h.controller.load();await h.controller.flush();const before=gets;
h.set([lut(20,220)]);h.controller.changed();await h.controller.flush();
return {restored,before,gets,posts:h.posts(),warnings:h.warnings};
""")
    assert out["restored"] is None and out["before"] == 1 and out["gets"] == 2
    assert out["posts"][0]["luts"] == [{"lo": 20, "hi": 220, "gamma": 0.37}]
    assert out["warnings"]


def test_unavailable_persistence_disables_saves_without_false_warning():
    out = run("""
const h=setup({fetch:async()=>ok({ok:true,available:false,settings:null})});
await h.controller.load();h.controller.changed();await h.advance(1000);await h.controller.flush();
return {posts:h.posts(),calls:h.calls.length,warnings:h.warnings};
""")
    assert out == {"posts": [], "calls": 1, "warnings": []}


def test_separate_file_controllers_do_not_share_values_endpoints_or_dirty_state():
    out = run("""
const a=setup({endpoint:'file-a/api/lut-settings',fetch:async()=>loaded([lut(1,100)])});
const b=setup({endpoint:'file-b/api/lut-settings'});
const restored=await a.controller.load();await b.controller.load();b.set([lut(2,200)]);
b.controller.changed();await b.controller.flush();await a.controller.flush();
return {restored,a:a.calls,b:b.calls};
""")
    assert out["restored"][0]["lo"] == 1 and len(out["a"]) == 1
    assert all(row["url"] == "file-a/api/lut-settings" for row in out["a"])
    assert all(row["url"] == "file-b/api/lut-settings" for row in out["b"])
    assert len(out["b"]) == 2


def test_dispose_cancels_debounce_and_never_starts_a_save():
    out = run("""
const h=setup();await h.controller.load();h.controller.changed();h.controller.dispose();
await h.advance(1000);const failure=await rejected(h.controller.flush());h.controller.changed();
return {failure,posts:h.posts(),timers:h.timers.size};
""")
    assert "unsaved" in out["failure"] and out["posts"] == [] and out["timers"] == 0


def test_dispose_during_inflight_save_does_not_send_newer_queued_revision():
    out = run("""
const wait=deferred(),h=setup({fetch:async(url,opts)=>opts.method==='POST'?wait.promise:loaded(null)});
await h.controller.load();h.set([lut(1,100)]);h.controller.changed();
const pending=rejected(h.controller.flush());await settle();
h.set([lut(2,100)]);h.controller.changed();h.controller.dispose();wait.resolve(ok({ok:true}));
return {failure:await pending,posts:h.posts(),timers:h.timers.size};
""")
    assert "unsaved" in out["failure"] and len(out["posts"]) == 1 and out["timers"] == 0


def test_reverting_while_save_is_inflight_restores_original_persisted_values():
    out = run("""
const wait=deferred();let posts=0;const h=setup({fetch:async(url,opts)=>{
 if(opts.method!=='POST')return loaded([lut(1,100)]);return ++posts===1?wait.promise:ok({ok:true})}});
await h.controller.load();h.set([lut(2,100)]);h.controller.changed();const pending=h.controller.flush();
await settle();h.set([lut(1,100)]);h.controller.changed();wait.resolve(ok({ok:true}));await pending;
return h.posts();
""")
    assert [row["luts"][0]["lo"] for row in out] == [2, 1]


def test_unchanged_widget_event_does_not_overwrite_a_newer_external_save():
    out = run("""
const h=setup({fetch:async()=>loaded([lut()])});const restored=await h.controller.load();
h.set(restored);h.controller.changed();await h.advance(250);await h.controller.flush();
return {posts:h.posts(),warnings:h.warnings};
""")
    assert out == {"posts": [], "warnings": []}


def test_changed_snapshots_mutable_widget_values_without_sharing_references():
    out = run("""
const h=setup(),value=lut();await h.controller.load();h.set([value]);h.controller.changed();
value.lo=999;value.gamma=4;await h.controller.flush();return h.posts();
""")
    assert out[0]["luts"] == [{"lo": 10, "hi": 200, "gamma": 0.37}]


def test_synchronous_read_failure_can_retry_and_later_change_retries_failed_save():
    out = run("""
let gets=0,posts=0;const h=setup({fetch:(url,opts)=>{
  if(opts.method!=='POST'){if(++gets===1)throw new Error('read offline');return Promise.resolve(loaded(null))}
  if(++posts===1)return Promise.reject(new Error('write offline'));return Promise.resolve(ok({ok:true}))}});
await h.controller.load();h.set([lut(1,100)]);h.controller.changed();await h.advance(250);
h.set([lut(2,100)]);h.controller.changed();await h.advance(250);await h.controller.flush();
return {gets,posts,values:h.posts(),warnings:h.warnings};
""")
    assert out["gets"] == 2 and out["posts"] == 2
    assert [row["luts"][0]["lo"] for row in out["values"]] == [1, 2]
    assert out["warnings"]


def test_unavailable_response_needs_no_channel_metadata():
    out = run("""
const h=setup({info:{},fetch:async()=>ok({ok:true,available:false,settings:null})});
await h.controller.load();h.controller.changed();await h.controller.flush();
return {posts:h.posts(),warnings:h.warnings};
""")
    assert out == {"posts": [], "warnings": []}


def test_invalid_current_edit_cannot_be_marked_saved_then_valid_change_recovers():
    out = run("""
const h=setup();await h.controller.load();h.set([lut(10,9)]);h.controller.changed();
const failed=await rejected(h.controller.flush());h.set([lut(5,50)]);h.controller.changed();
await h.controller.flush();return {failed,posts:h.posts()};
""")
    assert out["failed"] and len(out["posts"]) == 1
    assert out["posts"][0]["luts"][0]["lo"] == 5


def test_save_state_events_follow_dirty_saving_saved_with_the_injected_clock():
    out = run("""
const h=setup();await h.controller.load();h.set([lut()]);h.controller.changed();
const afterEdit=h.events.map(event=>[event.channel,event.state,event.at]);
await h.advance(250);
h.set([lut()]);h.controller.changed();await h.advance(250);
return {afterEdit,events:h.events,dirty:h.controller.isDirty(),status:h.controller.status()};
""")
    assert out["afterEdit"] == [["lut", "dirty", 0]]
    assert [e["state"] for e in out["events"]] == ["dirty", "saving", "saved", "idle"]
    assert all(e["channel"] == "lut" for e in out["events"])
    assert [e["at"] for e in out["events"]][1:3] == [250, 250]
    assert out["dirty"] is False and out["status"] == "ready"


def test_flush_forwards_keepalive_to_the_post_and_plain_flushes_do_not():
    out = run("""
const h=setup();await h.controller.load();h.set([lut(1,100)]);h.controller.changed();
await h.controller.flush({keepalive:true});
h.set([lut(2,100)]);h.controller.changed();await h.controller.flush();
return h.calls.filter(call=>call.method==='POST').map(call=>call.keepalive===true?'keepalive':typeof call.keepalive);
""")
    assert out == ["keepalive", "undefined"]


def test_flush_drops_keepalive_for_a_body_over_the_limit_shared_with_the_annotation_store():
    out = run("""
const path=require('path');
const {KEEPALIVE_BODY_LIMIT}=require(process.argv[1]);
const store=require(path.join(path.dirname(process.argv[1]),'annotation-store-v1.js'));
const h=setup();await h.controller.load();h.set([lut(1,100)]);h.controller.changed();
// a real LUT body stays far below the limit (at most 512 channels), so the
// encoder is stubbed to prove the guard is consulted
const Encoder=globalThis.TextEncoder;
globalThis.TextEncoder=class{encode(){return new Uint8Array(KEEPALIVE_BODY_LIMIT+1)}};
try{await h.controller.flush({keepalive:true})}finally{globalThis.TextEncoder=Encoder}
h.set([lut(2,100)]);h.controller.changed();await h.controller.flush({keepalive:true});
return {limit:KEEPALIVE_BODY_LIMIT,shared:store.KEEPALIVE_BODY_LIMIT===KEEPALIVE_BODY_LIMIT,
  modes:h.calls.filter(call=>call.method==='POST').map(call=>call.keepalive===true?'keepalive':typeof call.keepalive)};
""")
    assert out == {"limit": 60 * 1024, "shared": True, "modes": ["undefined", "keepalive"]}


def test_is_dirty_and_status_expose_what_the_periodic_retry_may_flush():
    out = run("""
const h=setup();const before=[h.controller.isDirty(),h.controller.status()];
await h.controller.load();const loaded=[h.controller.isDirty(),h.controller.status()];
h.set([lut()]);h.controller.changed();const edited=[h.controller.isDirty(),h.controller.status()];
await h.controller.flush();const flushed=[h.controller.isDirty(),h.controller.status()];
h.set([lut(10,9)]);h.controller.changed();const invalid=[h.controller.isDirty(),h.controller.status()];
const invalidEvent=h.events[h.events.length-1];
h.controller.dispose();
const u=setup({fetch:async()=>ok({ok:true,available:false,settings:null,warning:'no store here'})});
await u.controller.load();u.controller.changed();
return {before,loaded,edited,flushed,invalid,invalidEvent,disposed:h.controller.isDirty(),
  unavailable:[u.controller.isDirty(),u.controller.status()],unavailableEvents:u.events};
""")
    assert out["before"] == [False, "unloaded"] and out["loaded"] == [False, "ready"]
    assert out["edited"] == [True, "ready"] and out["flushed"] == [False, "ready"]
    # a validation failure is unsaved but not retried: isDirty() stays false
    assert out["invalid"] == [False, "ready"]
    assert out["invalidEvent"]["state"] == "dirty" and "LUT values are invalid" in out["invalidEvent"]["message"]
    assert out["disposed"] is False
    assert out["unavailable"] == [False, "unavailable"]
    assert out["unavailableEvents"] == [{"channel": "lut", "state": "unavailable", "at": 0, "message": "no store here"}]


def test_periodic_flush_after_a_transient_failure_succeeds_and_emits_saved():
    out = run("""
let attempts=0;const h=setup({fetch:async(url,opts)=>{
  if(opts.method!=='POST')return loaded(null);if(++attempts===1)throw new Error('offline');return ok({ok:true})}});
await h.controller.load();h.set([lut()]);h.controller.changed();await h.advance(250);
const failed={states:h.states(),dirty:h.controller.isDirty(),status:h.controller.status(),
  message:h.events[h.events.length-1].message};
await h.advance(60000); // nothing retries on its own
const idle=h.posts().length;
if(h.controller.isDirty())await h.controller.flush().catch(()=>{});
return {failed,idle,states:h.states(),dirty:h.controller.isDirty(),posts:h.posts().length};
""")
    assert out["failed"]["states"] == ["dirty", "saving", "failed"]
    assert out["failed"]["dirty"] is True and out["failed"]["status"] == "ready"
    assert "offline" in out["failed"]["message"]
    assert out["idle"] == 1
    assert out["states"] == ["dirty", "saving", "failed", "saving", "saved"]
    assert out["dirty"] is False and out["posts"] == 2


def test_an_edit_back_to_the_saved_values_reports_idle_not_a_fresh_save():
    out = run("""
const h=setup({fetch:async(url,opts)=>opts.method==='POST'?ok({ok:true}):loaded([lut()])});
const restored=await h.controller.load();h.set([lut(5,50)]);h.controller.changed();
const dirty=h.states();h.set(restored);h.controller.changed();await h.advance(250);
return {dirty,states:h.states(),posts:h.posts()};
""")
    assert out["dirty"] == ["dirty"] and out["states"] == ["dirty", "idle"] and out["posts"] == []
