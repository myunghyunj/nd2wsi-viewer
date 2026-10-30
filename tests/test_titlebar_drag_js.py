"""Run the real shell title-bar handlers without moving a desktop window."""
import json
from pathlib import Path

import pytest
from node_host import run_node

SHELL = Path(__file__).resolve().parents[1] / "nd2wsi/static/shell-v1.js"


@pytest.mark.parametrize("platform", ["cocoa", "edgechromium", "browser"])
def test_only_real_left_presses_on_empty_mac_titlebar_start_native_drag(platform):
    script = r"""
const fs=require('fs'),vm=require('vm');
const full=fs.readFileSync(process.argv[1],'utf8');
const source=full.slice(full.indexOf('const bareStrip ='),full.indexOf('\nfunction groupSids()'));
if(!source.includes('start_window_drag'))throw Error('missing native handler');
const handlers={},calls=[];
class Element {constructor(bare){this.classList={contains:()=>bare}}}
const context={Element,Promise,window:{},$:()=>({addEventListener:(name,fn)=>handlers[name]=fn}),
 requestWindowZoom:()=>calls.push('dom-zoom')};
if(process.argv[2]!=='browser')context.window.pywebview={platform:process.argv[2],api:{
 start_window_drag:()=>{calls.push('native');return Promise.resolve(true)}}};
vm.runInNewContext(source,context);
let stamp=0;
function press(bare,extra={}){
 const event={target:new Element(bare),timeStamp:stamp+=1100,isTrusted:true,button:0,detail:1,
  preventDefault(){calls.push('prevent')},...extra};
 handlers.pointerdown(event);handlers.mousedown(event);return event;
}
press(false); // tab / close / plus / menu controls are not drag regions
press(true,{button:2});press(true,{isTrusted:false});
const ignored=calls.slice();
const event=press(true);handlers.dblclick(event);
const real=calls.slice();calls.length=0;
press(false);
stamp-=1050; // Close button disappears; a nearby second click hits blank strip.
press(true,{detail:2});
process.stdout.write(JSON.stringify({ignored,real,closeThenBlank:calls}));
"""
    result = run_node(script, SHELL, platform, check=True)
    observed = json.loads(result.stdout)
    assert observed["ignored"] == []
    assert observed["real"] == (["prevent", "native"] if platform == "cocoa"
                                 else ["prevent", "dom-zoom"])
    assert observed["closeThenBlank"] == []
