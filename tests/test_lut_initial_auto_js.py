"""Real app initial Auto, asynchronous manual edits, and explicit API bounds."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_nd2_initial_auto_is_once_only_and_respects_manual_edits_and_handoff():
    script = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8');
const L=require(path.join(process.argv[1],'lut-controls-v1.js'));
const ctx=new Proxy({},{get:(o,k)=>o[k]||(()=>{})});
const node=()=>({style:{},append(){},setAttribute(){},addEventListener(){},getContext:()=>ctx});
let changes=0;
const state={info:{name:'dim.ND2',dtype:'uint16',channels:[{},{}]},luts:[null,null],lutWidgets:[],
 windows:{channels:{bodyWidth:()=>240}}};
const c={state,document:{createElement:node},window:{Nd2LutControls:L,devicePixelRatio:1,addEventListener(){},removeEventListener(){}},
 VIEWPORT_PROTOCOL_VERSION:1,inkColor:()=>'',currentTheme:()=> 'dark',fmtInt:String,
 applyLuts:Object.assign(()=>changes++,{flush(){}})};
vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('function initializeLutAuto('),source.indexOf('function createFrameRequests(')),c);
vm.runInContext(source.slice(source.indexOf('function buildLutRow('),source.indexOf('function relayoutLuts(')),c);
c.initializeLutAuto();const initial={axis:state.lutAutoRange,pending:[...state.lutAutoPending]};
for(let i=0;i<2;i++)c.buildLutRow(i,{color:'FFFFFF',window:{min:0,max:65535,start:0,end:65535}},{});
state.lutWidgets[1].reset(); // even a reset to null before the response is a manual choice
const h={bins:[1,1],vmin:0,vmax:65535,autoHistogram:{bins:[1,1],vmin:0,vmax:500},autoWindow:{lo:123,hi:321}};
c.acceptLutHistograms([h,h]);const first=state.luts.map(v=>v&&({...v}));
state.lutWidgets[0].setLut({lo:12,hi:234,gamma:2});
c.acceptLutHistograms([{...h,autoWindow:{lo:22,hi:44}},h]);const afterFrame=state.luts.map(v=>v&&({...v}));
state.lutWidgets[0].auto();const explicit=state.luts[0];
state.initialViewHandoff={};state.luts=[null,null];c.initializeLutAuto();const handoff=[...state.lutAutoPending];
delete state.initialViewHandoff;state.luts=[{lo:2,hi:8,gamma:1},null];c.initializeLutAuto();const restored=[...state.lutAutoPending];
state.info.name='bright.svs';c.initializeLutAuto();const svs={axis:state.lutAutoRange,pending:[...state.lutAutoPending]};
process.stdout.write(JSON.stringify({initial,first,afterFrame,explicit,handoff,restored,svs,changes}));
"""
    result = subprocess.run([NODE, "-e", script, str(STATIC)], check=True,
                            capture_output=True, text=True, timeout=20)
    out = json.loads(result.stdout)
    assert out["initial"] == {"axis": True, "pending": [True, True]}
    assert out["first"] == [{"lo": 123, "hi": 321, "gamma": 1}, None]
    assert out["afterFrame"] == [{"lo": 12, "hi": 234, "gamma": 2}, None]
    assert out["explicit"] == {"lo": 22, "hi": 44, "gamma": 2}
    assert out["handoff"] == [False, False]
    assert out["restored"] == [False, True]
    assert out["svs"] == {"axis": False, "pending": [False, False]}
    assert out["changes"] == 4
