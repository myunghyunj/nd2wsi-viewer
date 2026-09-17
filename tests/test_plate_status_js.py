"""Plate status polling must describe a real cache and own its request lifetime."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / 'nd2wsi/static/app.js'
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='node is not installed')

HARNESS = r"""
const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
const requests=[],timers=new Map(),events={};let next=1,focusLoads=0,renders=0;
const pl={focusMap:null},state={plate:pl,info:{generation:'g1',plate:{Z:3}}};
const nodes={'plate-cache-cell':{hidden:true,title:''},'plate-cache-val':{textContent:''}};
const context={state,AbortController,$:id=>nodes[id],fmtInt:String,
  renderTimeLine:()=>renders++,loadPlateFocus:()=>focusLoads++,
  window:{addEventListener:(type,handler)=>events[type]=handler},
  setInterval:(fn,delay)=>{const id=next++;timers.set(id,fn);return id},clearInterval:id=>timers.delete(id),
  fetch:(url,opts)=>new Promise((resolve,reject)=>requests.push({url,opts,reject,
    resolve:data=>resolve({ok:true,json:()=>Promise.resolve(data)})}))};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function pollPlateStatus('),source.indexOf('function renderTimeLine(')),context);
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
const tick=()=>[...timers.values()].forEach(fn=>fn());
const status=(extra={})=>({path:null,format:null,total:6,done:0,perT:[0,0],building:false,writer:false,...extra});
(async()=>{
"""


def run(script):
    result = subprocess.run([NODE, '-e', HARNESS + script +
                             "\n})().catch(e=>{console.error(e);process.exit(1)});", str(APP)],
                            check=True, capture_output=True, encoding='utf-8', timeout=20)
    return json.loads(result.stdout)


def test_no_cache_is_not_progress_and_stops_status_and_focus_polling():
    out = run(r"""
context.pollPlateStatus();requests[0].resolve(status());await flush();tick();tick();
process.stdout.write(JSON.stringify({requests:requests.length,timers:timers.size,focusLoads,renders,
  visible:!nodes['plate-cache-cell'].hidden,text:nodes['plate-cache-val'].textContent,pending:!!pl.statusRequest}));
""")
    assert out == {'requests': 1, 'timers': 0, 'focusLoads': 0, 'renders': 1,
                   'visible': True, 'text': 'No viewing cache', 'pending': False}


def test_real_writer_and_readonly_cache_continue_until_cache_and_focus_ready():
    out = run(r"""
context.pollPlateStatus();requests[0].resolve(status({path:'cache.nd2svs',done:2,building:true,writer:true}));await flush();
const partial={text:nodes['plate-cache-val'].textContent,timers:timers.size,focusLoads};
tick();requests[1].resolve(status({path:'cache.nd2svs',done:4}));await flush();
const reader={text:nodes['plate-cache-val'].textContent,timers:timers.size};
pl.focusMap={completeCount:2,total:2};tick();requests[2].resolve(status({path:'cache.nd2svs',done:6}));await flush();tick();
process.stdout.write(JSON.stringify({partial,reader,requests:requests.length,timers:timers.size,hidden:nodes['plate-cache-cell'].hidden}));
""")
    assert out == {'partial': {'text': '33 % · 2 of 6', 'timers': 1, 'focusLoads': 1},
                   'reader': {'text': '66 % · 4 of 6', 'timers': 1},
                   'requests': 3, 'timers': 0, 'hidden': True}


def test_polling_never_overlaps_and_pagehide_ignores_late_response():
    out = run(r"""
context.pollPlateStatus();tick();tick();context.pollPlateStatus();
const active=requests.length;events.pagehide();requests[0].resolve(status());await flush();tick();
process.stdout.write(JSON.stringify({active,aborted:requests[0].opts.signal.aborted,requests:requests.length,
  timers:timers.size,renders,focusLoads,pending:!!pl.statusRequest}));
""")
    assert out == {'active': 1, 'aborted': True, 'requests': 1,
                   'timers': 0, 'renders': 0, 'focusLoads': 0, 'pending': False}


def test_replaced_plate_or_generation_cannot_receive_old_status():
    out = run(r"""
context.pollPlateStatus();state.info.generation='g2';state.plate={};
requests[0].resolve(status({path:'old.nd2svs',done:6}));await flush();tick();
process.stdout.write(JSON.stringify({requests:requests.length,timers:timers.size,renders,newPlate:state.plate}));
""")
    assert out == {'requests': 1, 'timers': 0, 'renders': 0, 'newPlate': {}}


def test_transient_status_failure_can_recover_without_claiming_progress():
    out = run(r"""
context.pollPlateStatus();requests[0].reject(new Error('503'));await flush();
const failed={renders,text:nodes['plate-cache-val'].textContent,timers:timers.size};
tick();requests[1].resolve(status());await flush();
process.stdout.write(JSON.stringify({failed,requests:requests.length,timers:timers.size,text:nodes['plate-cache-val'].textContent}));
""")
    assert out == {'failed': {'renders': 0, 'text': '', 'timers': 1},
                   'requests': 2, 'timers': 0, 'text': 'No viewing cache'}


def test_cancelled_focus_response_cannot_repaint_or_replace_the_focus_map():
    out = run(r"""
state.info.plate.sites=[];
context.renderPlateAuto=()=>renders++;context.renderZSlider=()=>renders++;
vm.runInContext(source.slice(source.indexOf('function loadPlateFocus('),source.indexOf('function pollPlateStatus(')),context);
context.loadPlateFocus();pl.focusRequest.abort();
requests[0].resolve({best:[[2]],complete:[[true]],completeCount:1,total:1});await flush();
process.stdout.write(JSON.stringify({renders,map:pl.focusMap,pending:!!pl.focusRequest}));
""")
    assert out == {'renders': 0, 'map': None, 'pending': False}
