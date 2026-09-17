"""UI recovery state and exact LUT transport use the production app helpers."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / 'nd2wsi/static/app.js'
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='node is not installed')


def run(script):
    harness = r"""
const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
const buttons=[{dataset:{},title:'Auto contrast',disabled:false,textContent:'Auto'}];
const state={histogram:{},channels:[0,1,2,3],luts:[],info:{channels:[{},{},{},{}],generation:'g1'}};
const c={state,URLSearchParams,document:{querySelectorAll:()=>buttons}};vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('function setHistogramReady('),source.indexOf('function clearHistograms(')),c);
vm.runInContext(source.slice(source.indexOf('function lutParam('),source.indexOf('function activeFrameParams(')),c);
"""
    result = subprocess.run([NODE, '-e', harness + script, str(APP)], check=True,
                            capture_output=True, encoding='utf-8', timeout=20)
    return json.loads(result.stdout)


def test_failed_histogram_exposes_retry_then_restores_auto():
    out = run(r"""
c.setHistogramReady(false,false,'retrying');const waiting={...buttons[0]};
c.setHistogramReady(false,false,'failed');const failed={...buttons[0],failed:state.histogram.failed};
c.setHistogramReady(false);const loading={...buttons[0]};
c.setHistogramReady(true,false,'ready');const ready={...buttons[0],failed:state.histogram.failed};
process.stdout.write(JSON.stringify({waiting,failed,loading,ready}));
""")
    assert out['waiting']['disabled'] and 'retrying' in out['waiting']['title']
    assert not out['failed']['disabled'] and out['failed']['textContent'] == 'Retry'
    assert out['failed']['failed'] and 'Click to retry' in out['failed']['title']
    assert out['loading']['disabled'] and out['loading']['textContent'] == 'Auto'
    assert not out['ready']['disabled'] and not out['ready']['failed']
    assert out['ready']['title'] == 'Auto contrast'


def test_shared_tile_and_export_query_round_trips_fractional_windows():
    out = run(r"""
state.luts=[{lo:.2,hi:.4,gamma:1.002},{lo:-2e-12,hi:5e-12,gamma:1},null,{lo:102,hi:65535,gamma:1.25}];
const query=c.renderParams(new URLSearchParams());
const win=new URLSearchParams(query.toString()).get('win');
process.stdout.write(JSON.stringify({win,decoded:win.split(',').map(s=>s?s.split(':').map(Number):null)}));
""")
    assert out['decoded'] == [[0.2, 0.4, 1.002], [-2e-12, 5e-12], None, [102, 65535, 1.25]]


def test_only_explicit_integer_dtype_retains_discrete_request_limits():
    out = run(r"""
state.luts=[{lo:102.25,hi:192.25,gamma:1.002}];
const byType={};for(const dtype of ['uint16','<u2','bool','float32','float64','']){
  state.info.dtype=dtype;byType[dtype]=c.lutParam();
}
process.stdout.write(JSON.stringify(byType));
""")
    for dtype in ('uint16', '<u2', 'bool'):
        assert out[dtype] == '102:192:1.002,,,'
    for dtype in ('float32', 'float64', ''):
        assert out[dtype] == '102.25:192.25:1.002,,,'
