"""Exercise the public plate controller through transport events and timers."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const {createController}=require(process.argv[1]+'/plate-controller-v1.js');
const nodes={},timers=new Map(),changes=[],saved=[];let timerId=0;
const $=id=>nodes[id]||(nodes[id]={style:{},events:{},attrs:{},children:[],classList:{toggle(){}},
 append(el){this.children.push(el)},setAttribute(k,v){this.attrs[k]=v},
 addEventListener(k,v){this.events[k]=v},querySelectorAll(){return []},
 getBoundingClientRect(){return {left:0,width:100}}});
const state={info:{plate:{T:3,Z:4,timesMs:[100,1100,10100]}},plate:{t:0,z:1,focus:0,auto:true,fps:5}};
const host={document:{createElement:()=>({style:{}})},localStorage:{setItem:(...v)=>saved.push(v)},
 setInterval:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id},clearInterval:id=>timers.delete(id)};
const control=createController({state,$,clamp:(v,a,b)=>Math.max(a,Math.min(b,v)),fmtTickLabel:String,
 plateFrameChanged:()=>changes.push([state.plate.t,state.plate.z])},host);
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
[...timers.values()][0].fn();const wrapped=state.plate.t;
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
