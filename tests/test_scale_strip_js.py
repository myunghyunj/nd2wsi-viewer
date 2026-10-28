"""The ROI export-scale strip: frame steps are debounced, superseded requests
are aborted, and the size estimate is cached by level, format and channels.

The production strip, its request helpers and the query helpers are sliced
from ``app.js`` and run with fake timers and a scripted ``api/roi``.
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from node_host import run_node

APP = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "app.js"

HARNESS = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
function between(a,b){const i=source.indexOf(a),j=source.indexOf(b,i+a.length);
  if(i<0||j<0)throw new Error('production source not found: '+a);return source.slice(i,j);}
let now=0,nextTimer=1;
const timers=new Map();
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
// api/roi: every request is held until answered, so aborts can be observed
const requests=[];
function fetch(url,init={}){
  const entry={url:String(url),signal:init.signal,aborted:false,answered:false};
  requests.push(entry);
  return new Promise((resolve,reject)=>{
    entry.answer=(size=1000)=>{entry.answered=true;
      resolve({ok:true,blob:()=>Promise.resolve({size})});};
    entry.fail=()=>{entry.answered=true;
      resolve({ok:false,status:500,blob:()=>Promise.resolve({size:777})});};
    init.signal?.addEventListener('abort',()=>{entry.aborted=true;
      const error=new Error('aborted');error.name='AbortError';reject(error);});
  });
}
const answerAll=size=>requests.filter(r=>!r.answered&&!r.aborted).forEach(r=>r.answer(size));
let objectUrls=0;const revoked=[];
const URLs={createObjectURL:()=>'blob:preview-'+(++objectUrls),revokeObjectURL:url=>revoked.push(url)};
function node(tag){
  return {tag,children:[],className:'',textContent:'',src:'',
    append(...items){this.children.push(...items);},
    set innerHTML(value){this.children=[];}};
}
const nodes={'roi-scales':node('div'),'roi-scale-dims':node('span'),'roi-scale-note':node('span')};
const state={roi:{x:1000,y:2000,w:4000,h:3000},renderScale:null,channels:[0,1],luts:[],
  info:{channels:[{},{}],generation:'gen-1',maxRenderMpx:400,
    levels:[{path:'0',downsample:1},{path:'1',downsample:2},{path:'2',downsample:4}]}};
const frame={t:0,p:0,z:0};
let contextKey='site-0';
const context={state,URLSearchParams,AbortController,URL:URLs,Math,Number,String,Array,
  setTimeout,clearTimeout,fetch,$:id=>nodes[id],
  document:{createElement:tag=>node(tag)},
  syncScaleBarExportControls(){},frameOwnsRoi:()=>true,
  activeFrameContext:()=>({key:contextKey,frame:{...frame}}),
  appendFrameParams:(q,f)=>{for(const [k,v] of Object.entries(f))q.set(k,v);return q;},
  lutParam:()=>state.win||'',fmtInt:v=>String(v),fmtBytes:b=>Math.round(b)+' B'};
vm.createContext(context);
vm.runInContext([
  between('function debounce(','/* ---- readout strip'),
  between('function channelQuery(','function activeFrameParams('),
  between('function scaleOptions(','/* The calibration for the µm fields'),
].join('\n'),context);
// previews are 256, 128 and 64 px; the size samples are 512 px
const isSample=r=>/[?&]w=512(&|$)/.test(r.url);
const samples=()=>requests.filter(isSample);
const note=()=>nodes['roi-scale-note'].textContent;
const images=()=>nodes['roi-scales'].children.map(btn=>btn.children[0].src);
"""


def run(body):
    script = HARNESS + "(async()=>{" + body + "})().then(v=>process.stdout.write(JSON.stringify(v)))" \
        ".catch(e=>{console.error(e);process.exitCode=1});"
    result = run_node(script, APP)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_playback_frame_steps_refresh_the_strip_once_the_frame_rests():
    out = run("""
context.updateScaleStrip();answerAll(2000);await advance(0);answerAll(2000);await advance(0);
const first=requests.length;
for(let t=1;t<=8;t++){frame.t=t;context.scheduleScaleStrip();await advance(125);}
const duringPlayback=requests.length-first;
await advance(299-125);const beforeRest=requests.length-first;
await advance(1);
return {first,duringPlayback,beforeRest,afterRest:requests.slice(first).map(r=>r.url)};
""")
    # three scale previews and a JPEG plus a PNG size sample
    assert out["first"] == 5
    assert out["duringPlayback"] == 0 and out["beforeRest"] == 0
    # one refresh, for the frame playback stopped on; the size samples are cached
    assert len(out["afterRest"]) == 3
    for url in out["afterRest"]:
        q = parse_qs(urlparse(url).query)
        assert q["t"] == ["8"] and q["format"] == ["jpg"] and q["g"] == ["gen-1"]


def test_a_superseded_strip_aborts_its_requests_and_its_late_estimate_never_lands():
    out = run("""
context.updateScaleStrip();
const old=requests.slice();
frame.z=3;context.scheduleScaleStrip();
const abortedOnStep=old.map(r=>r.aborted);
await advance(300);
const fresh=requests.slice(old.length);
fresh.filter(r=>!isSample(r)).forEach(r=>r.answer(900));await advance(0);
const beforeEstimate=note();
contextKey='site-1';
for(let i=0;i<2;i++){requests.filter(r=>isSample(r)&&!r.answered&&!r.aborted).forEach(r=>r.answer(50000));await advance(0);}
return {abortedOnStep,fresh:fresh.length,beforeEstimate,afterSiteChange:note(),
  samples:samples().length,images:images()};
""")
    # the three previews and the pending JPEG sample all stop at the frame step
    assert out["abortedOnStep"] == [True] * 4
    assert out["fresh"] == 4
    assert out["beforeEstimate"] == "TIFF uses this scale; ND2 stays native."
    # the estimate finished for the old site: the context-key guard keeps it out
    assert out["samples"] == 3
    assert out["afterSiteChange"] == "TIFF uses this scale; ND2 stays native."
    assert out["images"] == ["blob:preview-1", "blob:preview-2", "blob:preview-3"]


def test_size_estimate_is_cached_across_frames_and_luts_but_not_channels_place_or_scale():
    out = run("""
const step=async change=>{change();context.updateScaleStrip();
  for(let i=0;i<2;i++){answerAll(4096);await advance(0);}return samples().length;};
const counts={};
counts.first=await step(()=>{});
counts.frame=await step(()=>{frame.t=5;frame.z=2;});
counts.lut=await step(()=>{state.win='10:200,30:900:1.5';});
counts.channels=await step(()=>{state.channels=[1];});
counts.place=await step(()=>{state.roi={...state.roi,x:state.roi.x+20000};});
counts.scale=await step(()=>{state.renderScale=2;});
return {counts,note:note(),revoked:revoked.length};
""")
    assert out["counts"] == {"first": 2, "frame": 2, "lut": 2, "channels": 4, "place": 6, "scale": 8}
    assert out["note"].startswith("≈ ") and "JPEG" in out["note"] and "PNG" in out["note"]
    # each rebuild releases the previous strip's preview images
    assert out["revoked"] == 5 * 3


def test_a_failed_size_sample_is_left_out_and_asked_again():
    out = run("""
context.updateScaleStrip();
requests.filter(r=>!isSample(r)).forEach(r=>r.answer(900));
samples()[0].fail();await advance(0);
samples()[1].answer(4096);await advance(0);
const first=note();
context.updateScaleStrip();await advance(0);
return {first,samples:samples().map(r=>parse(r.url).get('format'))};
function parse(url){return new URL(url,'http://viewer.test/').searchParams;}
""")
    # an error body must not become a bytes-per-pixel figure, nor be cached
    assert out["first"].startswith("≈ ") and "PNG" in out["first"] and "JPEG" not in out["first"]
    assert out["samples"] == ["jpg", "png", "jpg"]


def test_a_direct_refresh_supersedes_a_pending_frame_refresh():
    out = run("""
context.updateScaleStrip();for(let i=0;i<2;i++){answerAll(1000);await advance(0);}
const first=requests.length;
frame.t=1;context.scheduleScaleStrip();
await advance(100);state.renderScale=4;context.updateScaleStrip();
const direct=requests.length-first;
await advance(1000);
return {direct,later:requests.length-first};
""")
    # the click refreshes at once (previews plus the new scale's first sample),
    # and the pending frame refresh is dropped
    assert out["direct"] == 3 + 1 and out["later"] == out["direct"]
