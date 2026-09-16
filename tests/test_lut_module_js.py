"""LUT module wiring, shared channel edits and native pointer ownership."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8');
const L=require(path.join(process.argv[1],'lut-controls-v1.js'));
const nodes=[],handlers={message:[],pagehide:[]},context2d=new Proxy({},{get:(o,k)=>o[k]||(()=>{})});
function node(tag){const n={tag,children:[],handlers:{},attrs:{},style:{},append(...xs){this.children.push(...xs)},
setAttribute(k,v){this.attrs[k]=v},addEventListener(k,f){this.handlers[k]=f},getContext:()=>context2d,
setPointerCapture(){},getBoundingClientRect:()=>({left:0,top:0})};nodes.push(n);return n;}
let target=null,changes=0,flushes=0;
const toggle={checked:true},state={luts:[null,null],lutWidgets:[],lutAutoRange:true,windows:{channels:{bodyWidth:()=>240}}};
const window={Nd2LutControls:L,devicePixelRatio:1,location:{origin:'https://viewer.test'},parent:{},
addEventListener:(type,fn)=>handlers[type].push(fn),removeEventListener:(type,fn)=>handlers[type]=handlers[type].filter(f=>f!==fn)};
const context={state,window,document:{createElement:node,elementFromPoint:()=>target},$:()=>toggle,
VIEWPORT_PROTOCOL_VERSION:1,inkColor:()=>'',currentTheme:()=> 'dark',fmtInt:v=>String(Math.round(v)),
applyLuts:Object.assign(()=>changes++,{flush:()=>flushes++})};vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function buildLutRow('),source.indexOf('function relayoutLuts(')),context);
for(let i=0;i<2;i++)context.buildLutRow(i,{label:'C'+i,color:'FF0000',window:{min:0,max:65535,start:102,end:192}},{});
const widgets=state.lutWidgets,canvases=nodes.filter(n=>n.tag==='canvas');
const bounds=i=>[+canvases[i].attrs['data-axis-min'],+canvases[i].attrs['data-axis-max']];
const histogram=(lo,hi)=>({bins:[1,2,3],vmin:0,vmax:65535,detail:{values:[100,200,400],counts:[3,2,1]},autoHistogram:{bins:[1,2,3],vmin:lo,vmax:hi}});
widgets[0].setHistogram(histogram(96,239));widgets[1].setHistogram(histogram(123,789));
const event=(extra={})=>({clientX:120,clientY:40,button:0,pointerId:1,preventDefault(){},stopPropagation(){},...extra});
canvases[0].handlers.wheel(event({deltaX:0,deltaY:-20,deltaMode:0}));
const navigated={toggle:toggle.checked,auto:state.lutAutoRange,other:bounds(1),changes};
widgets[1].setHistogram(histogram(20,1000));const frozen= bounds(1);
target=canvases[0];const before=bounds(0);
const native={origin:window.location.origin,source:window.parent,data:{nd2wsi:'native-trackpad',version:1,clientX:120,clientY:40,deltaX:-10}};
for(const invalid of [{...native,origin:'https://other.test'},{...native,source:{}},{...native,data:{...native.data,version:2}}])handlers.message.forEach(fn=>fn(invalid));
const invalid=bounds(0);target=null;handlers.message.forEach(fn=>fn(native));const outside=bounds(0);
target=canvases[0];handlers.message.forEach(fn=>fn(native));const nativePan={bounds:bounds(0),other:bounds(1),changes};
canvases[0].handlers.dblclick(event());canvases[0].handlers.pointerdown(event({clientX:4+192/65535*232,clientY:5,shiftKey:true}));
canvases[0].handlers.pointermove(event({clientX:120,clientY:5,shiftKey:true}));canvases[0].handlers.pointerup();
const shift={luts:state.luts,changes,flushes};handlers.pagehide.forEach(fn=>fn());
process.stdout.write(JSON.stringify({navigated,frozen,before,invalid,outside,nativePan,shift,listeners:handlers.message.length}));
"""


def test_real_app_adapter_freezes_channels_routes_native_scroll_and_batches_shift_drag():
    result = subprocess.run([NODE, "-e", SCRIPT, str(STATIC)], check=True,
                            capture_output=True, text=True, timeout=20)
    out = json.loads(result.stdout)
    assert out["navigated"] == {"toggle": False, "auto": False, "other": [123, 789], "changes": 0}
    assert out["frozen"] == [123, 789]
    assert out["invalid"] == out["outside"] == out["before"]
    assert out["nativePan"]["bounds"][0] > out["before"][0]
    assert out["nativePan"]["other"] == [123, 789]
    assert out["nativePan"]["changes"] == 0
    assert out["shift"]["luts"][0] == out["shift"]["luts"][1]
    assert out["shift"]["luts"][0]["hi"] == pytest.approx(32767.5)
    assert out["shift"]["changes"] == 4 and out["shift"]["flushes"] == 1
    assert out["listeners"] == 0


def test_browser_modules_load_before_application_without_commonjs_or_dom_side_effects():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    modules = {
        "lut-controls-v1.js": "Nd2LutControls",
        "frame-data-v1.js": "Nd2FrameData",
        "floating-windows-v1.js": "Nd2FloatingWindows",
    }
    for filename in modules:
        assert index.index(filename) < index.index("app.js")
    script = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');const context={};vm.createContext(context);
for(const filename of JSON.parse(process.argv[2]))vm.runInContext(fs.readFileSync(path.join(process.argv[1],filename),'utf8'),context);
process.stdout.write(JSON.stringify(Object.keys(context).sort()));
"""
    result = subprocess.run([NODE, "-e", script, str(STATIC), json.dumps(list(modules))],
                            check=True, capture_output=True, text=True, timeout=20)
    assert json.loads(result.stdout) == sorted(modules.values())
