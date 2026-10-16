"""Production startup precedence and effective LUT persistence wiring."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.mark.parametrize("saved,handoff,expected,changed", [
    (None, None, [True, True], 0),
    ([{"lo": 0, "hi": 100, "gamma": 1}] * 2, None, [False, False], 0),
    ([{"lo": 1, "hi": 90, "gamma": .5}] * 2,
     [{"lo": 2, "hi": 80, "gamma": .75}] * 2, [False, False], 1),
    (None, [{"lo": 2, "hi": 80, "gamma": 8}] * 2, [False, False], 0),
])
def test_startup_restores_before_auto_and_explicit_handoff_wins(saved, handoff, expected, changed):
    script = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1]+'/app.js','utf8');
const [saved,handoff]=JSON.parse(process.argv[2]);
const state={info:{name:'same.nd2',dtype:'uint16',channels:[{},{}]},luts:[null,null]};
let changed=0, disposed=0, pagehide;
const controller={load:async()=>saved,changed:()=>changed++,dispose:()=>disposed++};
const window={Nd2LutPersistence:{createController:()=>controller},
  Nd2LutTransfer:require(process.argv[1]+'/lut-transfer-v1.js'),
  Nd2ViewState:{applyDisplay:(target,display)=>target.luts=display},
  parent:{pywebview:{api:{initial_view_state:async()=>handoff}}},
  addEventListener:(kind,callback)=>{if(kind==='pagehide')pagehide=callback;}};
const context=vm.createContext({state,window,currentSlideSid:()=> 'slide',showToast(){}});
vm.runInContext(source.slice(source.indexOf('async function initializeDisplayLuts('),
 source.indexOf('function acceptLutHistograms(')),context);
context.initializeDisplayLuts().then(()=>{
 pagehide(); process.stdout.write(JSON.stringify({luts:state.luts,pending:state.lutAutoPending,changed,disposed}));
}).catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([NODE, "-e", script, str(STATIC), json.dumps([saved, handoff])],
                            capture_output=True, text=True, check=True, timeout=20)
    out = json.loads(result.stdout)
    assert out == {"luts": handoff or saved or [None, None], "pending": expected,
                   "changed": changed, "disposed": 1}


def test_persistence_dependency_is_loaded_before_the_viewer():
    html = (STATIC / "index.html").read_text()
    assert html.index('lut-transfer-v1.js') < html.index('lut-persistence-v1.js') < html.index('static/app.js')
