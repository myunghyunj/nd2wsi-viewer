"""Exercise the public plate controller through transport events and timers."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const {createController}=require(process.argv[1]+'/plate-controller-v1.js');
const nodes={},timers=new Map(),changes=[],saved=[];let timerId=0,clock=1000;
const $=id=>nodes[id]||(nodes[id]={style:{},events:{},attrs:{},children:[],classList:{toggle(){}},
 append(el){this.children.push(el)},setAttribute(k,v){this.attrs[k]=v},
 addEventListener(k,v){this.events[k]=v},querySelectorAll(){return []},
 getBoundingClientRect(){return {left:0,width:100}}});
const state={info:{plate:{T:3,Z:4,timesMs:[100,1100,10100]}},plate:{t:0,z:1,focus:0,auto:true,fps:5}};
const host={document:{createElement:()=>({style:{}})},localStorage:{setItem:(...v)=>saved.push(v)},
 performance:{now:()=>clock},
 setInterval:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id},clearInterval:id=>timers.delete(id),
 setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms,once:true});return id},clearTimeout:id=>timers.delete(id)};
// Run the next pending timer as the browser would: a one-shot timer is gone once it fires.
function fire(){const [id,timer]=[...timers][0];if(timer.once)timers.delete(id);timer.fn();return timer.ms;}
let shown=true;
const control=createController({state,$,clamp:(v,a,b)=>Math.max(a,Math.min(b,v)),fmtTickLabel:String,
 plateFrameChanged:()=>changes.push([state.plate.t,state.plate.z]),plateFrameShown:()=>shown},host);
control.buildTimeLine();
const key=key=>$('t-track').events.keydown({key,preventDefault(){},stopPropagation(){}});
"""


def run(script):
    result = subprocess.run([NODE, "-e", HARNESS + script, str(STATIC)], check=True,
                            capture_output=True, encoding="utf-8", timeout=20)
    return json.loads(result.stdout)


def test_irregular_time_scrubbing_keyboard_and_playback_share_frame_state():
    out = run(r"""
$('t-track').events.pointerdown({clientX:40,preventDefault(){}});const scrubbed=state.plate.t;
key('End');const end=state.plate.t;
control.setPlatePlaying(true);const period=[...timers.values()][0].ms;
clock+=period;fire();const wrapped=state.plate.t;
$('t-next').onclick();key('PageUp');key('Home');
control.setPlatePlaying(false);
console.log(JSON.stringify({scrubbed,end,wrapped,period,changes,timers:timers.size,playing:state.plate.playing}));
""")
    assert out == {"scrubbed": 1, "end": 2, "wrapped": 0, "period": 200,
                   "changes": [[1, 1], [2, 1], [0, 1], [1, 1], [2, 1], [0, 1]],
                   "timers": 0, "playing": False}


def test_single_frame_disables_transport_and_never_starts_a_timer():
    out = run(r"""
state.info.plate.T=1;state.info.plate.timesMs=[0];control.buildTimeLine();
control.setPlatePlaying(true);key('End');
console.log(JSON.stringify({disabled:$('t-play').disabled,playing:state.plate.playing,timers:timers.size,t:state.plate.t}));
""")
    assert out == {"disabled": True, "playing": False, "timers": 0, "t": 0}


def test_unavailable_storage_does_not_prevent_startup_or_manual_z_handoff():
    out = run(r"""
Object.defineProperty(host,'localStorage',{get(){throw new Error('Storage denied')}});
$('t-auto').querySelector=()=>$('auto-label');
const guarded=createController({state,$,clamp:(v,a,b)=>Math.max(a,Math.min(b,v)),
 currentFocusSummary:()=>({ready:1,total:1}),paintPlate(){},plateFrameChanged(){changes.push(state.plate.z)}},host);
guarded.setPlateZ(2);
console.log(JSON.stringify({z:state.plate.z,auto:state.plate.auto,changes}));
""")
    assert out == {"z": 2, "auto": False, "changes": [2]}


def test_pause_for_a_hidden_pane_stops_the_timer_and_keeps_t_and_z():
    out = run(r"""
control.setPlatePlaying(true);clock+=200;fire();
const before={t:state.plate.t,z:state.plate.z,changes:changes.length};
control.pausePlatePlayback();
const paused={t:state.plate.t,z:state.plate.z,changes:changes.length,timers:timers.size,
  playing:state.plate.playing,title:$('t-play').title};
$('t-play').title='untouched';control.pausePlatePlayback();
console.log(JSON.stringify({before,paused,idleTitle:$('t-play').title}));
""")
    assert out["before"] == {"t": 1, "z": 1, "changes": 1}
    assert out["paused"] == {"t": 1, "z": 1, "changes": 1, "timers": 0,
                             "playing": False, "title": "Play (space)"}
    # pausing a plate that is not playing changes nothing
    assert out["idleTitle"] == "untouched"


def test_playback_steps_only_after_the_frame_on_screen_has_arrived():
    out = run(r"""
state.info.plate.T=10;state.info.plate.timesMs=[...Array(10).keys()];control.buildTimeLine();
shown=false;control.setPlatePlaying(true);
const log=[];
// the first step is due one period after play, but the frame is still loading
clock+=200;log.push({delay:fire(),t:state.plate.t,next:[...timers.values()][0].ms});
clock+=16;log.push({delay:fire(),t:state.plate.t,next:[...timers.values()][0].ms});
shown=true;clock+=16;log.push({delay:fire(),t:state.plate.t,next:[...timers.values()][0].ms});
console.log(JSON.stringify({log,changes,pending:timers.size}));
""")
    assert out["log"] == [
        {"delay": 200, "t": 0, "next": 16},
        {"delay": 16, "t": 0, "next": 16},
        # arrived: one step, and the next is due a full period after it
        {"delay": 16, "t": 1, "next": 200},
    ]
    assert out["changes"] == [[1, 1]] and out["pending"] == 1


def test_a_frame_that_never_arrives_is_given_up_after_the_maximum_wait():
    out = run(r"""
state.info.plate.T=10;state.info.plate.timesMs=[...Array(10).keys()];control.buildTimeLine();
shown=false;control.setPlatePlaying(true);
const steps=[];let waited=0,polls=0,longest=0;
for(let i=0;i<500&&steps.length<3;i++){
  const delay=[...timers.values()][0].ms;longest=Math.max(longest,delay);
  clock+=delay;waited+=delay;const t=state.plate.t;fire();polls++;
  if(state.plate.t!==t){steps.push(waited);waited=0;}
}
console.log(JSON.stringify({steps,longest,t:state.plate.t,playing:state.plate.playing}));
""")
    # each stalled frame holds playback for exactly the maximum wait, then it moves on
    assert out["steps"] == [2000, 2000, 2000]
    assert out["longest"] == 200 and out["t"] == 3 and out["playing"] is True


def test_playback_keeps_its_cadence_while_frames_arrive_in_time():
    out = run(r"""
state.info.plate.T=4;state.info.plate.timesMs=[0,1,2,3];control.buildTimeLine();
const slow=createController({state,$,clamp:(v,a,b)=>Math.max(a,Math.min(b,v)),fmtTickLabel:String,
 plateFrameChanged:()=>{clock+=30;changes.push(state.plate.t);},plateFrameShown:()=>true},host);
slow.buildTimeLine();slow.setPlatePlaying(true);
const delays=[];
for(let i=0;i<5;i++){const delay=[...timers.values()][0].ms;delays.push(delay);clock+=delay;fire();}
slow.setPlatePlaying(false);
console.log(JSON.stringify({delays,changes,timers:timers.size}));
""")
    # a step that takes 30 ms of frame work still starts the next one 200 ms after it began
    assert out["delays"] == [200, 170, 170, 170, 170]
    assert out["changes"] == [1, 2, 3, 0, 1] and out["timers"] == 0


def test_pausing_while_a_frame_is_awaited_cancels_the_wait_and_keeps_t():
    out = run(r"""
shown=false;control.setPlatePlaying(true);clock+=200;fire();
const waiting=timers.size;control.setPlatePlaying(false);
console.log(JSON.stringify({waiting,timers:timers.size,t:state.plate.t,changes}));
""")
    assert out == {"waiting": 1, "timers": 0, "t": 0, "changes": []}


APP_FRAME = r"""
const fs=require('fs'),vm=require('vm'),path=require('path');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8').replace(/\r\n/g,'\n');
const i=source.indexOf('function plateFrameShown('),j=source.indexOf('\n}\n',i);
const cell=complete=>({querySelector:sel=>sel==='img'?{complete}:null});
let item=null;
const state={plate:null,viewer:{world:{getItemAt:index=>index===0?item:null}}};
const c=vm.createContext({state});
vm.runInContext(source.slice(i,j)+'\n}\n',c);
const out={};
out.noPlate=c.plateFrameShown();
state.plate={focus:null,gridEls:[cell(true),,cell(true)],stripEls:[cell(false)]};
out.gridLoaded=c.plateFrameShown();
state.plate.gridEls[1]=cell(false);
out.gridLoading=c.plateFrameShown();
state.plate={focus:0,gridEls:[cell(false)],stripEls:[cell(true),cell(true)]};
item={getFullyLoaded:()=>false};out.tilesLoading=c.plateFrameShown();
item={getFullyLoaded:()=>true};out.focusLoaded=c.plateFrameShown();
state.plate.stripEls[1]=cell(false);out.stripLoading=c.plateFrameShown();
item=null;state.plate.stripEls[1]=cell(true);out.reopening=c.plateFrameShown();
console.log(JSON.stringify(out));
"""


def test_the_app_reports_a_frame_shown_once_every_visible_frame_has_settled():
    result = subprocess.run([NODE, "-e", APP_FRAME, str(STATIC)], check=True,
                            capture_output=True, encoding="utf-8", timeout=20)
    out = json.loads(result.stdout)
    # the grid counts in the overview (a hidden strip does not), the strip and
    # the viewer's own tiles while a site is focused; an image that failed is complete
    assert out == {"noPlate": True, "gridLoaded": True, "gridLoading": False, "tilesLoading": False,
                   "focusLoaded": True, "stripLoading": False, "reopening": True}
    app = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    wiring = app[app.index("window.Nd2PlateController.createController({"):]
    wiring = wiring[:wiring.index("});")]
    assert re.search(r"\bplateFrameShown,", wiring)


SHARPEN = r"""
const fs=require('fs'),vm=require('vm'),path=require('path');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8').replace(/\r\n/g,'\n');
const i=source.indexOf('function paintPlate('),j=source.indexOf('\n}\n',i);
const timers=[];
const cell=()=>{const img={dataset:{},src:'',getAttribute:k=>k==='src'?img.src:null};
  return {dataset:{},img,setAttribute(){},classList:{remove(){},toggle(){}},querySelector:()=>img};};
const sites=[{i:0,name:'A1'},{i:1,name:'A2'}];
const state={info:{plate:{sites,Z:1}},plate:{t:3,z:0,auto:false,focus:null,k:4,playing:false,
  gridEls:sites.map(cell),stripEls:sites.map(cell),focusMap:null}};
const c=vm.createContext({state,window:{Nd2PlateUI:{focusReady:()=>true}},plateGroupKey:()=> '0',
  plateZFor:()=>0,siteLabelText:String,plateFrameUrl:(t,p,z,k)=>`${t}/${p}/k${k}`,
  setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){}});
vm.runInContext(source.slice(i,j)+'\n}\n',c);
const srcs=()=>state.plate.gridEls.map(el=>el.img.src);
const out={};
c.paintPlate();out.quick=srcs();
state.plate.playing=true;timers.pop()();out.whilePlaying=srcs();
c.paintPlate();state.plate.playing=false;timers.pop()();out.atRest=srcs();
console.log(JSON.stringify(out));
"""


def test_the_grid_sharpens_only_once_playback_rests():
    result = subprocess.run([NODE, "-e", SHARPEN, str(STATIC)], check=True,
                            capture_output=True, encoding="utf-8", timeout=20)
    out = json.loads(result.stdout)
    # wide cells first get the quick 8x frames; a frame awaited during playback
    # stays quick, and the sharper 4x pass follows once playback stops
    assert out == {"quick": ["3/0/k8", "3/1/k8"], "whilePlaying": ["3/0/k8", "3/1/k8"],
                   "atRest": ["3/0/k4", "3/1/k4"]}


SHARPEN_PAUSE = r"""
const fs=require('fs'),vm=require('vm'),path=require('path');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8').replace(/\r\n/g,'\n');
const i=source.indexOf('function paintPlate('),j=source.indexOf('\n}\n',i);
const timers=new Map();let timerId=0;
// Run the oldest pending sharpen timer; it is gone once it fires.
function fire(){const [id,timer]=[...timers][0];timers.delete(id);timer.fn();return timer.ms;}
const cell=()=>{const img={dataset:{},src:'',getAttribute:k=>k==='src'?img.src:null};
  return {dataset:{},img,setAttribute(){},classList:{remove(){},toggle(){}},querySelector:()=>img};};
const sites=[{i:0,name:'A1'},{i:1,name:'A2'}];
const state={info:{plate:{sites,Z:1,T:5}},plate:{t:3,z:0,auto:false,focus:null,k:4,playing:false,fps:8,
  gridEls:sites.map(cell),stripEls:sites.map(cell),focusMap:null}};
const c=vm.createContext({state,window:{Nd2PlateUI:{focusReady:()=>true}},plateGroupKey:()=> '0',
  plateZFor:()=>0,siteLabelText:String,plateFrameUrl:(t,p,z,k)=>`${t}/${p}/k${k}`,
  setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id)});
vm.runInContext(source.slice(i,j)+'\n}\n',c);
// the real controller pauses, and its own playback timers stay apart
const {createController}=require(path.join(process.argv[1],'plate-controller-v1.js'));
const nodes={},playback=new Map();
const $=id=>nodes[id]||(nodes[id]={style:{},classList:{toggle(){}},setAttribute(){},querySelectorAll(){return []}});
const control=createController({state,$,clamp:(v,a,b)=>Math.max(a,Math.min(b,v)),
  paintPlate:()=>c.paintPlate(),plateFrameChanged:()=>c.paintPlate(),plateFrameShown:()=>false},
 {document:{},performance:{now:()=>0},
  setTimeout:(fn,ms)=>{const id=++timerId;playback.set(id,fn);return id;},clearTimeout:id=>playback.delete(id)});
const srcs=()=>state.plate.gridEls.map(el=>el.img.src);
const out={};
c.paintPlate();control.setPlatePlaying(true);
// the frame is still awaited each time the sharper pass comes due
out.waits=[fire(),fire()];out.whilePlaying=srcs();out.pending=timers.size;
state.plate.t=4;c.paintPlate();out.stepped={srcs:srcs(),pending:timers.size};
out.waits.push(fire());
control.setPlatePlaying(false);out.paused=srcs();
out.delay=fire();out.atRest=srcs();out.left=timers.size;
console.log(JSON.stringify(out));
"""


def test_pausing_during_a_slow_frame_still_sharpens_the_grid():
    result = subprocess.run([NODE, "-e", SHARPEN_PAUSE, str(STATIC)], check=True,
                            capture_output=True, encoding="utf-8", timeout=20)
    out = json.loads(result.stdout)
    # while playback waits, the sharper pass keeps one timer and looks again;
    # a step replaces it, and a pause with no repaint still sharpens the grid
    assert out == {"waits": [350, 350, 350],
                   "whilePlaying": ["3/0/k8", "3/1/k8"], "pending": 1,
                   "stepped": {"srcs": ["4/0/k8", "4/1/k8"], "pending": 1},
                   "paused": ["4/0/k8", "4/1/k8"], "delay": 350,
                   "atRest": ["4/0/k4", "4/1/k4"], "left": 0}
