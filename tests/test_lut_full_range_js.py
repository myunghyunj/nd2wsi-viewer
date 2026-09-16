"""Exercise the real LUT controls with a full uint16 range and dim signal."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / 'nd2wsi/static/app.js'
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='node is not installed')

SCRIPT = r'''
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
let labels = []; let refreshes = 0;
const ctx = new Proxy({}, {get:(o,k)=>k==='fillText' ? ((text)=>labels.push(text)) : (o[k] || (()=>{}))});
const nodes=[];
function node(tag) { const n={tag,children:[],handlers:{},attrs:{},style:{},value:'',
 append(...xs){this.children.push(...xs)},setAttribute(k,v){this.attrs[k]=v},
 addEventListener(k,fn){this.handlers[k]=fn},getContext(){return ctx},
 setPointerCapture(){},getBoundingClientRect(){return {left:0,top:0}}};nodes.push(n);return n; }
const state={luts:[null],lutWidgets:[],windows:{channels:{bodyWidth:()=>240}}};
const context={document:{createElement:node},window:{devicePixelRatio:2,addEventListener(){},removeEventListener(){}},state,
 clamp:(v,l,h)=>Math.max(l,Math.min(h,v)),inkColor:()=>'',currentTheme:()=> 'dark',
 fmtInt:v=>String(Math.round(v)),applyLuts:Object.assign(()=>{refreshes++},{flush(){}})};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function buildLutRow('),source.indexOf('function relayoutLuts(')),context);
const winLabel={};context.buildLutRow(0,{label:'CY5',color:'FF0000',window:{min:0,max:65535,start:102,end:192}},winLabel);
const widget=state.lutWidgets[0],canvas=nodes.find(n=>n.tag==='canvas');
const inputs=nodes.filter(n=>n.tag==='input');
const axis=()=>canvas.attrs['aria-label'];
const initial={axis:axis(),lo:inputs[0].value,hi:inputs[1].value};
const bounds=()=>[Number(canvas.attrs['data-axis-min']),Number(canvas.attrs['data-axis-max'])];
const wheel=(dx,dy,x=120,deltaMode=0)=>canvas.handlers.wheel({deltaX:dx,deltaY:dy,deltaMode,
 clientX:x,clientY:40,preventDefault(){},stopPropagation(){}});
const prior=refreshes;
wheel(0,-100);const zoom=bounds();
wheel(20,0);const pan=bounds();
canvas.handlers.pointerdown({clientX:120,clientY:75,button:0,pointerId:1,preventDefault(){}});
canvas.handlers.pointermove({clientX:140,clientY:75});canvas.handlers.pointercancel();
const dragged=bounds();
canvas.handlers.pointermove({clientX:160,clientY:75});const cancelled=bounds();
wheel(1e9,0);const farRight=bounds();wheel(-1e9,0);const farLeft=bounds();
canvas.handlers.dblclick({preventDefault(){},stopPropagation(){}});
const navigation={zoom,pan,dragged,cancelled,farRight,farLeft,full:bounds(),
 lut:state.luts[0],refreshes:refreshes-prior};

const fine=Array(256).fill(0);fine[12]=10000;fine[172]=500;
widget.setHistogram({bins:Array(256).fill(1),vmin:0,vmax:65535,
 autoHistogram:{bins:fine,vmin:96,vmax:239}});
widget.auto();const auto={axis:axis(),lut:state.luts[0]};
widget.reset();const reset={axis:axis(),lo:inputs[0].value,hi:inputs[1].value};
inputs[1].value='50000';inputs[1].handlers.change();
const exact={axis:axis(),lut:state.luts[0]};
widget.setAutoRange(true);
const cropped={axis:axis(),lut:state.luts[0],max:inputs[1].max};
inputs[1].value='40000';inputs[1].handlers.change();
const croppedEdit=state.luts[0];
widget.clearHistogram();
widget.setHistogram({bins:Array(256).fill(1),vmin:0,vmax:65535,
 autoHistogram:{bins:fine,vmin:100,vmax:500}});
const newFrame={axis:axis(),lut:state.luts[0]};
widget.setAutoRange(false);
const expanded={axis:axis(),lut:state.luts[0]};
inputs[1].value='100000';inputs[1].handlers.change();
const capped=state.luts[0];
inputs[0].value='';inputs[0].handlers.change();const blank=inputs[0].value;
widget.clearHistogram();const cleared=axis();
widget.relayout(500);const resized=axis();
canvas.handlers.pointerdown({clientX:496,clientY:20,pointerId:1,preventDefault(){}});
canvas.handlers.pointerup();const rightEdge=state.luts[0].hi;
process.stdout.write(JSON.stringify({navigation,initial,auto,reset,exact,cropped,croppedEdit,newFrame,expanded,capped,blank,cleared,resized,rightEdge}));
'''


def test_full_axis_survives_auto_reset_clear_resize_and_exact_edits():
    result = subprocess.run([NODE, '-e', SCRIPT, str(APP)], check=True,
                            capture_output=True, text=True, timeout=20)
    out = json.loads(result.stdout)
    axis = 'CY5 histogram range 0 to 65535'
    assert out['initial'] == {'axis': axis, 'lo': '102', 'hi': '192'}
    assert out['reset'] == out['initial']
    assert out['auto']['axis'] == out['exact']['axis'] == out['cleared'] == out['resized'] == axis
    assert 100 < out['auto']['lut']['lo'] < 110
    assert 190 < out['auto']['lut']['hi'] < 200
    assert out['exact']['lut']['hi'] == 50000
    assert out['cropped']['axis'] == 'CY5 histogram range 96 to 239'
    assert out['cropped']['lut'] == out['exact']['lut']
    assert out['cropped']['max'] == '65535'
    assert out['croppedEdit']['hi'] == 40000
    assert out['newFrame']['axis'] == 'CY5 histogram range 100 to 500'
    assert out['expanded']['axis'] == axis
    assert out['expanded']['lut'] == out['croppedEdit'] == out['newFrame']['lut']
    assert out['capped']['hi'] == out['rightEdge'] == 65535
    assert out['blank'] == '102'


def test_zoom_pan_bounds_and_pointer_cancel_do_not_change_contrast():
    result = subprocess.run([NODE, '-e', SCRIPT, str(APP)], check=True,
                            capture_output=True, text=True, timeout=20)
    nav = json.loads(result.stdout)['navigation']
    span = nav['zoom'][1] - nav['zoom'][0]
    assert 0 < span < 65535
    assert sum(nav['zoom']) / 2 == pytest.approx(65535 / 2)
    assert nav['pan'][0] > nav['zoom'][0]
    assert nav['pan'][1] - nav['pan'][0] == pytest.approx(span)
    assert nav['dragged'][0] < nav['pan'][0]
    assert nav['cancelled'] == nav['dragged']
    assert nav['farLeft'][0] == 0
    assert nav['farRight'][1] == 65535
    assert nav['full'] == [0, 65535]
    assert nav['lut'] is None
    assert nav['refreshes'] == 0


LIVE_SCRIPT = r"""
const fs=require('fs'), vm=require('vm'), source=fs.readFileSync(process.argv[1],'utf8');
let now=0, next=1, state=[0,0], calls=[];
const timers=new Map();
const context={performance:{now:()=>now},window:{addEventListener(){}},
 setTimeout:(fn,delay)=>{const id=next++;timers.set(id,{at:now+delay,fn});return id;},
 clearTimeout:id=>timers.delete(id),refreshTiles:()=>calls.push({at:now,values:[...state]})};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const applyLuts ='),source.indexOf('/* Swap the tiles')),context);
const update=vm.runInContext('applyLuts',context);
function tick(to){
 while(true){const due=[...timers].filter(([id,t])=>t.at<=to).sort((a,b)=>a[1].at-b[1].at)[0];
 if(!due)break;now=due[1].at;timers.delete(due[0]);due[1].fn();}
 now=to;
}
state[0]=1;update();state[1]=1;update();tick(0);
const first=calls.map(x=>({...x}));
for(let t=16;t<=944;t+=16){tick(t);state=[t,t];update();}
const during=calls.map(x=>({...x}));
const beforeRelease=calls.length;update.flush();
const release={calls:calls.length,at:calls.at(-1).at,values:calls.at(-1).values};
tick(1200);const afterWait=calls.length;
state=[2000,2000];update();update.cancel();tick(1400);
process.stdout.write(JSON.stringify({first,during,beforeRelease,release,afterWait,afterCancel:calls.length}));
"""


def test_continuous_drag_renders_before_release_and_flushes_latest_values():
    result = subprocess.run([NODE, '-e', LIVE_SCRIPT, str(APP)], check=True,
                            capture_output=True, text=True, timeout=20)
    out = json.loads(result.stdout)
    assert out['first'] == [{'at': 0, 'values': [1, 1]}]
    assert len(out['during']) >= 8  # A trailing debounce would never run during this drag.
    assert all(b['at'] - a['at'] >= 100 for a, b in zip(out['during'], out['during'][1:]))
    assert out['release']['calls'] == out['beforeRelease'] + 1
    assert out['release']['at'] == 944
    assert out['release']['values'] == [944, 944]
    assert out['afterWait'] == out['afterCancel'] == out['release']['calls']
