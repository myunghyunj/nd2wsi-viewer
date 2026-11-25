"""LUT module wiring, shared channel edits and native pointer ownership."""
import json
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"

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
    result = run_node(SCRIPT, STATIC, check=True)
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


DELEGATED = r"""
const path=require('path');
const L=require(path.join(process.argv[1],'lut-controls-v1.js'));
const context2d=new Proxy({},{get:(o,k)=>o[k]||(()=>{})});
const handlers={message:[],pagehide:[]};let hits=0,target=null;
function node(tag){return {tag,style:{},attrs:{},children:[],append(...xs){this.children.push(...xs)},
  setAttribute(k,v){this.attrs[k]=v},addEventListener(){},getContext:()=>context2d,
  getBoundingClientRect:()=>({left:0,top:0})};}
const parent={};
const window={devicePixelRatio:1,location:{origin:'https://viewer.test'},parent,
  addEventListener:(type,fn)=>handlers[type].push(fn),
  removeEventListener:(type,fn)=>handlers[type]=handlers[type].filter(f=>f!==fn)};
const document={createElement:node,elementFromPoint:()=>{hits++;return target;}};
const scrolled=[];
const widgets=[0,1,2,3].map(i=>L.createWidget({channel:{window:{min:0,max:4095,start:0,end:4095}},
  dtype:'uint16',label:{textContent:''},width:240,document,window,protocolVersion:2,inkColor:()=>'',currentTheme:()=>'dark',
  fmtInt:String,onChange(){},onFlush(){},onManualAxis:()=>scrolled.push(i)}));
const canvases=widgets.map(w=>w.element.children[0]);
const send=data=>handlers.message.forEach(fn=>fn({origin:'https://viewer.test',source:parent,
  data:{nd2wsi:'native-trackpad',version:2,clientX:120,clientY:40,deltaX:-30,...data}}));
const listeners=handlers.message.length;
target=canvases[2];send({});const one={hits,scrolled:[...scrolled]};
target=null;send({});const outside={hits,scrolled:[...scrolled]};
handlers.pagehide.forEach(fn=>fn());
process.stdout.write(JSON.stringify({listeners,one,outside,after:handlers.message.length}));
"""


def test_native_trackpad_events_hit_test_once_for_every_channel_widget():
    result = run_node(DELEGATED, STATIC, check=True)
    out = json.loads(result.stdout)
    # four channel widgets share one listener and one hit test per message
    assert out["listeners"] == 1
    assert out["one"] == {"hits": 1, "scrolled": [2]}
    assert out["outside"] == {"hits": 2, "scrolled": [2]}
    assert out["after"] == 0


def test_browser_modules_load_before_application_without_commonjs_or_dom_side_effects():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    modules = {
        "viewer-geometry-v1.js": "Nd2ViewerGeometry",
        "lut-controls-v1.js": "Nd2LutControls",
        "frame-data-v1.js": "Nd2FrameData",
        "floating-windows-v1.js": "Nd2FloatingWindows",
        "annotation-store-v1.js": "Nd2AnnotationStore",
        "save-status-v1.js": "Nd2SaveStatus",
    }
    for filename in modules:
        assert index.index(filename) < index.index("app.js")
    script = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');const context={};vm.createContext(context);
for(const filename of JSON.parse(process.argv[2]))vm.runInContext(fs.readFileSync(path.join(process.argv[1],filename),'utf8'),context);
process.stdout.write(JSON.stringify(Object.keys(context).sort()));
"""
    result = run_node(script, STATIC, json.dumps(list(modules)), check=True)
    assert json.loads(result.stdout) == sorted(modules.values())


POINTERUP_HARDENING = r"""
const path = require('path');
const L = require(path.join(process.argv[1], 'lut-controls-v1.js'));
const context2d = new Proxy({}, { get: (o, k) => o[k] || (() => {}) });
function makeNode(tag) {
  return {
    tag, style: {}, attrs: {}, children: [], handlers: {},
    append(...xs) { this.children.push(...xs); },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(k, fn) { this.handlers[k] = fn; },
    getContext: () => context2d,
    setPointerCapture() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 240, height: 96 }),
  };
}
function setup() {
  const window = {
    devicePixelRatio: 1,
    location: { origin: 'https://viewer.test' },
    parent: {},
    addEventListener() {},
    removeEventListener() {},
  };
  const document = { createElement: makeNode, elementFromPoint: () => null };
  const events = [];
  const changes = [];
  let flushes = 0;
  let manualAxes = 0;
  const widget = L.createWidget({
    channel: { label: 'Cal', color: 'FFFFFF', window: { min: 0, max: 255, start: 0, end: 255 } },
    dtype: 'uint8',
    label: { textContent: '' },
    width: 240,
    document,
    window,
    protocolVersion: 2,
    inkColor: () => '#fff',
    currentTheme: () => 'dark',
    fmtInt: v => String(Math.round(v)),
    onChange(val) { events.push('change'); changes.push(val); },
    onFlush() { events.push('flush'); flushes++; },
    onManualAxis() { manualAxes++; },
    onShiftChange() {},
  });
  const canvas = widget.element.children[0];
  return { widget, canvas, events, changes, get flushes() { return flushes; }, get manualAxes() { return manualAxes; } };
}
const ev = (x, y, extra = {}) => ({
  clientX: x, clientY: y, button: 0, pointerId: 9,
  preventDefault() {}, stopPropagation() {}, ...extra,
});

// 1. pointerup(coords) without prior pointermove applies final coords before onFlush
const direct = setup();
direct.canvas.handlers.pointerdown(ev(236, 8));
direct.canvas.handlers.pointerup(ev(120, 8));

// 2. pointermove(target) followed by pointerup(target) emits NO duplicate onChange
const dedup = setup();
dedup.canvas.handlers.pointerdown(ev(236, 8));
dedup.canvas.handlers.pointermove(ev(120, 8));
dedup.canvas.handlers.pointerup(ev(120, 8));

// 3. zero-arg pointerup() preserves value from pointermove and flushes once
const zeroArg = setup();
zeroArg.canvas.handlers.pointerdown(ev(236, 8));
zeroArg.canvas.handlers.pointermove(ev(120, 8));
zeroArg.canvas.handlers.pointerup();

// 4. pointercancel(coords) and lostpointercapture(coords) NEVER apply coords
const cancel = setup();
cancel.canvas.handlers.pointerdown(ev(236, 8));
cancel.canvas.handlers.pointercancel(ev(80, 8));

const lost = setup();
lost.canvas.handlers.pointerdown(ev(236, 8));
lost.canvas.handlers.lostpointercapture(ev(80, 8));

// 5. gamma knob pointerup(coords) without prior pointermove applies final gamma before onFlush
// With width=240: H=round(96)=96, PLOT={x0:4,x1:236,y0:14,y1:78}, knob at mid x=120, y=78-0.5*64=46
const gammaDirect = setup();
gammaDirect.canvas.handlers.pointerdown(ev(120, 46));
gammaDirect.canvas.handlers.pointerup(ev(120, 30));

// 6. pan mode pointerup(coords) does not trigger LUT change or flush
const pan = setup();
pan.canvas.handlers.pointerdown(ev(60, 60));
pan.canvas.handlers.pointerup(ev(100, 60));

process.stdout.write(JSON.stringify({
  direct: { events: direct.events, changes: direct.changes, flushes: direct.flushes },
  dedup: { events: dedup.events, changes: dedup.changes, flushes: dedup.flushes },
  zeroArg: { events: zeroArg.events, changes: zeroArg.changes, flushes: zeroArg.flushes },
  cancel: { events: cancel.events, changes: cancel.changes, flushes: cancel.flushes },
  lost: { events: lost.events, changes: lost.changes, flushes: lost.flushes },
  gammaDirect: { events: gammaDirect.events, changes: gammaDirect.changes, flushes: gammaDirect.flushes },
  pan: { events: pan.events, changes: pan.changes, flushes: pan.flushes, manualAxes: pan.manualAxes },
}));
"""


def test_pointerup_hardening_direct_coords_dedup_zero_arg_and_cancel():
    result = run_node(POINTERUP_HARDENING, STATIC, check=True)
    out = json.loads(result.stdout)

    # pointerup(coords) without prior pointermove applies target before flush
    assert out["direct"]["events"] == ["change", "change", "flush"]
    assert out["direct"]["flushes"] == 1
    assert out["direct"]["changes"][-1]["hi"] == pytest.approx(127.5)

    # pointermove(target) + pointerup(target) emits no duplicate onChange
    assert out["dedup"]["events"] == ["change", "change", "flush"]
    assert out["dedup"]["flushes"] == 1
    assert out["dedup"]["changes"][-1]["hi"] == pytest.approx(127.5)

    # zero-arg pointerup() preserves moved value and flushes cleanly
    assert out["zeroArg"]["events"] == ["change", "change", "flush"]
    assert out["zeroArg"]["flushes"] == 1
    assert out["zeroArg"]["changes"][-1]["hi"] == pytest.approx(127.5)

    # pointercancel / lostpointercapture never apply new coordinates
    assert out["cancel"]["events"] == ["change", "flush"]
    assert out["cancel"]["changes"] == [None]
    assert out["lost"]["events"] == ["change", "flush"]
    assert out["lost"]["changes"] == [None]

    # gamma drag pointerup(coords) without prior pointermove updates gamma before flush
    assert out["gammaDirect"]["events"] == ["change", "change", "flush"]
    assert out["gammaDirect"]["changes"][-1]["gamma"] > 2.0

    # pan mode pointerup does not emit LUT change or flush
    assert out["pan"] == {"events": [], "changes": [], "flushes": 0, "manualAxes": 0}
