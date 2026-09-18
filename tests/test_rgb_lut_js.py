"""Exercise the real channel panel and LUT widgets for RGB and fluorescence."""
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs=require('fs'), path=require('path'), vm=require('vm');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8');
const L=require(path.join(process.argv[1],'lut-controls-v1.js'));
const nodes=[],ctx=new Proxy({}, {get:(o,k)=>o[k]||(()=>{})});
function node(tag){const n={tag,children:[],handlers:{},attrs:{},style:{},disabled:false,
 append(...xs){this.children.push(...xs)},setAttribute(k,v){this.attrs[k]=v},
 addEventListener(k,f){this.handlers[k]=f},getContext:()=>ctx,setPointerCapture(){},
 getBoundingClientRect:()=>({left:0,top:0})};
 n.classList={contains:c=>(n.className||'').split(' ').includes(c),
 toggle(c,on){const s=new Set((n.className||'').split(' '));on?s.add(c):s.delete(c);n.className=[...s].join(' ');}};
 nodes.push(n);return n;}
const controls={'channel-list':node('div'),'lut-auto-range':node('input'),'tb-channels':node('button')};
let closed=false,histograms=0,refreshes=0;
const rgb=process.argv[2]==='true';
const channels=['Red','Green','Blue'].map((label,i)=>({label,color:['FF0000','00FF00','0000FF'][i],
 window:{min:0,max:255,start:0,end:255}}));
const state={info:{rgb,dtype:'uint8',channels},channels:[0,1,2],luts:[null,null,null],
 lutWidgets:[],lutAutoRange:false,histogram:{failed:false},
 windows:{channels:{bodyWidth:()=>240,close:()=>{closed=true;}}}};
const window={Nd2LutControls:L,devicePixelRatio:1,addEventListener(){},removeEventListener(){}};
const c={state,window,URLSearchParams,document:{createElement:node},$:id=>controls[id],
 VIEWPORT_PROTOCOL_VERSION:1,inkColor:()=>'',currentTheme:()=>rgb?'light':'dark',fmtInt:String,
 loadHistograms:()=>histograms++,refreshTiles:()=>refreshes++,
 applyLuts:Object.assign(()=>refreshes++,{flush(){}})};
vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('function macSwitch('),source.indexOf('/* ---- per-channel LUT')),c);
vm.runInContext(source.slice(source.indexOf('function buildLutRow('),source.indexOf('function relayoutLuts(')),c);
vm.runInContext(source.slice(source.indexOf('function lutParam('),source.indexOf('function activeFrameParams(')),c);
c.buildChannelPanel();
const initial={closed,disabled:controls['tb-channels'].disabled,histograms,
 names:nodes.filter(n=>n.className==='name').map(n=>n.textContent),
 axes:nodes.filter(n=>n.tag==='canvas').map(n=>n.attrs['aria-label']),luts:[...state.luts]};
if(state.lutWidgets.length!==3){process.stdout.write(JSON.stringify({initial}));process.exit(0);}
state.lutWidgets[1].setLut({lo:20,hi:220,gamma:2});
const edited=JSON.parse(JSON.stringify(state.luts)),query=c.renderParams(new URLSearchParams()).toString();
const switches=nodes.filter(n=>n.attrs.role==='switch');
switches[2].handlers.click();const hidden=[...state.channels];
switches[2].handlers.click();const restored=[...state.channels];
const resets=nodes.filter(n=>n.className==='lut-reset');resets[1].handlers.click({preventDefault(){}});
const reset=JSON.parse(JSON.stringify(state.luts));
process.stdout.write(JSON.stringify({initial,edited,query,hidden,restored,reset,refreshes}));
"""


@pytest.mark.parametrize("rgb", [True, False])
def test_channels_panel_exposes_independent_luts_toggle_and_reset(rgb):
    result = subprocess.run([NODE, "-e", SCRIPT, str(STATIC), str(rgb).lower()],
                            capture_output=True, encoding="utf-8", check=True, timeout=20)
    out = json.loads(result.stdout)
    assert out["initial"] == {
        "closed": False, "disabled": False, "histograms": 1,
        "names": ["Red", "Green", "Blue"],
        "axes": [f"{name} histogram range 0 to 255" for name in ("Red", "Green", "Blue")],
        "luts": [None, None, None],
    }
    assert out["edited"] == [None, {"lo": 20, "hi": 220, "gamma": 2}, None]
    assert parse_qs(out["query"])["win"] == [",20:220:2,"]
    assert out["hidden"] == [0, 1] and out["restored"] == [0, 1, 2]
    assert out["reset"] == [None, None, None]  # no overrides: use original RGB windows
    assert out["refreshes"] == 4
