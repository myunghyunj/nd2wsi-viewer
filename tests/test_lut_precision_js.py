"""Real widget pointer/zoom/Auto interactions for declared numeric pixel types."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / 'nd2wsi/static/lut-controls-v1.js'
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='node is not installed')

SCRIPT = r'''
const {createWidget,autoWindowFromHistogram}=require(process.argv[1]);
const spec=JSON.parse(process.argv[2]);
const changes=[],arcs=[],texts=[],nodes=[];let manual=0,flushes=0;
const context=new Proxy({}, {get:(o,k)=>k==='arc' ? (...args)=>arcs.push(args)
  : k==='fillText' ? text=>texts.push(text) : ()=>{}});
function element(tag) {
 const result={tag,style:{},attrs:{},handlers:{},children:[],
  append(...children){this.children.push(...children);},getContext(){return context;},
  setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,f){this.handlers[k]=f;},
  setPointerCapture(){},getBoundingClientRect(){return {left:0,top:0};}};
 nodes.push(result);return result;
}
const label={};
const widget=createWidget({channel:{label:'Signal',color:'FF0000',window:spec.window},dtype:spec.dtype,
 initialLut:spec.initialLut || null,label,width:240,document:{createElement:element},
 window:{devicePixelRatio:1,addEventListener(){},removeEventListener(){}},protocolVersion:2,
 inkColor:()=>'',currentTheme:()=> 'dark',fmtInt:v=>String(Math.round(v)),
 onChange:lut=>changes.push(lut),onFlush:()=>flushes++,onManualAxis:()=>manual++,onShiftChange(){}});
const canvas=nodes.find(n=>n.tag==='canvas');
const axis=()=>[Number(canvas.attrs['data-axis-min']),Number(canvas.attrs['data-axis-max'])];
const pixel=value=>4+(value-axis()[0])/(axis()[1]-axis()[0])*232;
const pointer=(name,x,y=5)=>canvas.handlers[name]({clientX:x,clientY:y,button:0,pointerId:1,preventDefault(){}});
const drag=(from,to,y=5)=>{pointer('pointerdown',pixel(from),y);pointer('pointermove',pixel(to),y);pointer('pointerup',pixel(to),y);};
const wheel=(dx,dy,x=120)=>canvas.handlers.wheel({deltaX:dx,deltaY:dy,deltaMode:0,
 clientX:x,clientY:40,preventDefault(){},stopPropagation(){}});
const result=eval('(function(){'+process.argv[3]+'})()');
process.stdout.write(JSON.stringify(result));
'''


def run(body, *, dtype='float64', lo=0, hi=1, start=None, end=None):
    spec = {'dtype': dtype, 'window': {'min': lo, 'max': hi,
            'start': lo if start is None else start, 'end': hi if end is None else end}}
    result = subprocess.run([NODE, '-e', SCRIPT, str(MODULE), json.dumps(spec), body],
                            capture_output=True, encoding='utf-8', check=True, timeout=20)
    return json.loads(result.stdout)


@pytest.mark.parametrize('lo,hi', [(0, 1), (-0.75, -0.25), (0, 1e-12)])
def test_manual_handles_keep_subunit_contrast_and_gamma_anchor(lo, hi):
    low, high = lo + (hi - lo) * 0.2, lo + (hi - lo) * 0.4
    out = run('''
      const [lo,hi]=axis(),low=lo+(hi-lo)*0.2,high=lo+(hi-lo)*0.4;
      drag(lo,low);drag(hi,high);
      const contrast=changes.at(-1),before=axis();
      pointer('pointerdown',pixel((low+high)/2),46);
      pointer('pointermove',pixel((low+high)/2),30);
      pointer('pointerup',pixel((low+high)/2),30);
      return {contrast,gamma:changes.at(-1),before,after:axis(),manual,flushes,label:label.textContent};
    ''', lo=lo, hi=hi)
    assert out['contrast'] == pytest.approx({'lo': low, 'hi': high, 'gamma': 1}, abs=(hi-lo)*1e-12)
    assert out['gamma']['lo'] == pytest.approx(low, abs=(hi-lo)*1e-12)
    assert out['gamma']['hi'] == pytest.approx(high, abs=(hi-lo)*1e-12)
    assert out['gamma']['gamma'] == pytest.approx(2.40942084)
    assert out['after'] == out['before'] == [lo, hi]
    assert out['manual'] == 0 and out['flushes'] == 3
    assert out['label'] != '0–0'


@pytest.mark.parametrize('lo,hi', [(0, 0.001), (-0.01, 0), (0, 1e-12)])
def test_float_zoom_and_pan_preserve_contrast_within_actual_bounds(lo, hi):
    out = run('''
      const full=axis();for(let i=0;i<20;i++)wheel(0,-100);
      const zoom=axis();wheel(1e9,0);const right=axis();wheel(-1e9,0);const left=axis();
      canvas.handlers.dblclick({preventDefault(){},stopPropagation(){}});
      return {full,zoom,right,left,restored:axis(),changes};
    ''', lo=lo, hi=hi)
    span = hi - lo
    assert 0 < out['zoom'][1] - out['zoom'][0] < span / 1000
    assert out['zoom'][1] - out['zoom'][0] >= span / 65536 * (1 - 1e-9)
    assert out['right'][1] == pytest.approx(hi, abs=span*1e-12)
    assert out['left'][0] == pytest.approx(lo, abs=span*1e-12)
    assert out['restored'] == [lo, hi] and out['changes'] == []


@pytest.mark.parametrize('dtype', ['float32', 'float64', None])
def test_integer_looking_bounds_do_not_make_a_float_or_unknown_source_discrete(dtype):
    out = run('''
      widget.setLut({lo:0.2,hi:0.4,gamma:1});return changes.at(-1);
    ''', dtype=dtype)
    assert out == {'lo': 0.2, 'hi': 0.4, 'gamma': 1}


def test_only_exact_default_window_and_gamma_return_null():
    out = run('''
      widget.setLut({lo:0.2000000000001,hi:0.4,gamma:1});
      widget.setLut({lo:0.2,hi:0.4,gamma:1.001});widget.reset();return changes;
    ''', start=0.2, end=0.4)
    assert out[0]['lo'] == 0.2000000000001
    assert out[1]['gamma'] == 1.001
    assert out[2] is None


@pytest.mark.parametrize('dtype', ['uint16', 'int16'])
def test_declared_integer_types_keep_unit_handle_width_and_rounded_labels(dtype):
    out = run('''
      widget.setLut({lo:10.2,hi:10.3,gamma:1});
      const value=changes.at(-1),text=label.textContent;widget.reset();
      return {value,text,reset:changes.at(-1)};
    ''', dtype=dtype, lo=0, hi=100, start=0, end=100)
    assert out == {'value': {'lo': 10.2, 'hi': 11.2, 'gamma': 1}, 'text': '10–11', 'reset': None}


def test_float_auto_uses_histogram_bin_width_and_retains_fine_range():
    out = run('''
      const bins=Array(256).fill(0);bins[32]=900;bins[192]=100;
      widget.setHistogram({bins,vmin:-0.01,vmax:0,autoHistogram:{bins,vmin:-0.01,vmax:0}});
      widget.auto();return {lut:changes.at(-1),axis:axis()};
    ''', lo=-0.01, hi=0)
    assert out['lut']['lo'] == pytest.approx(-0.00875)
    assert out['lut']['hi'] == pytest.approx(-0.0024609375)
    assert out['lut']['gamma'] == 1
    assert out['axis'] == [-0.01, 0]


def test_single_peak_auto_and_right_peak_fallback_do_not_expand_to_one_intensity_unit():
    out = run('''
      const a=autoWindowFromHistogram([0,100,0,0],0,0.001);
      const b=autoWindowFromHistogram([1,0,0,999],-0.002,0);
      return {a,b};
    ''')
    assert out == {'a': {'lo': 0.00025, 'hi': 0.0005}, 'b': {'lo': -0.002, 'hi': 0}}


def test_float_labels_distinguish_a_narrow_window_at_large_offset():
    out = run('''
      widget.setLut({lo:1000000.00002,hi:1000000.00004,gamma:1});
      return {label:label.textContent,lut:changes.at(-1)};
    ''', lo=1000000, hi=1000000.0001)
    assert out['label'] == '1000000.00002–1000000.00004'
    assert out['lut'] == {'lo': 1000000.00002, 'hi': 1000000.00004, 'gamma': 1}


def test_nonfinite_input_keeps_the_existing_finite_window():
    out = run('''
      widget.setLut({lo:0.2,hi:0.4,gamma:1});const before=changes.length;
      const nan=widget.setLut({lo:NaN,hi:0.5,gamma:1});
      const inf=widget.setLut({lo:0.2,hi:Infinity,gamma:1});
      const hg=widget.setHistogram({bins:[1],vmin:0,vmax:Infinity});
      return {nan,inf,hg,before,after:changes.length,axis:axis()};
    ''')
    assert out == {'nan': False, 'inf': False, 'hg': False, 'before': 1, 'after': 1, 'axis': [0, 1]}


def test_bool_source_keeps_its_one_unit_range_and_differs_from_float_zero_to_one():
    out = run("""
      widget.setLut({lo:0,hi:0.25,gamma:1});wheel(0,-100);
      return {lut:changes.at(-1),axis:axis()};
    """, dtype='bool')
    assert out == {'lut': None, 'axis': [0, 1]}


def test_adjacent_float_values_remain_a_valid_exact_default():
    out = run("""
      widget.reset();return {lut:changes.at(-1),label:label.textContent};
    """, lo=1.5, hi=1.5000000000000002)
    assert out['lut'] is None
    assert out['label'] == '1.5–1.5000000000000002'


def test_integer_reset_retains_an_exact_fractional_metadata_default():
    out = run("""
      widget.setLut({lo:0,hi:2,gamma:1});widget.reset();return changes;
    """, dtype='uint16', lo=0, hi=65535, start=0, end=0.75)
    assert out[-1] is None
