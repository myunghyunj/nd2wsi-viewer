"""Z height follows acquisition direction; stored T/P/Z indices never reverse."""

import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(script, data=None):
    result = subprocess.run(
        [NODE, "-e", script, str(STATIC), json.dumps(data)],
        capture_output=True, encoding="utf-8", check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("stack_type", [3, 7])
@pytest.mark.parametrize("home_um", [2, 6, 8])
def test_reader_home_index_matches_explicit_acquisition_positions(stack_type, home_um):
    pytest.importorskip("nd2")
    from nd2._parse._parse import _parse_z_stack_loop

    # Pinned nd2's parser and limnd2.ExperimentZStackLoop.homeIndex both
    # measure home from the first acquired endpoint (type 7 starts at high).
    # Public example_cell/tissue ND2s contain no Z loop, so this is an explicit
    # metadata fixture, not a claim of validation against a physical z stage.
    loop = _parse_z_stack_loop({
        "uiCount": 6, "dZLow": 0.0, "dZHigh": 10.0, "dZStep": 2.0,
        "dZHome": home_um, "bZInverted": False, "iType": stack_type,
    })
    params = asdict(loop.parameters)
    positions = list(range(0, 11, 2))
    if stack_type == 7:
        positions.reverse()
    assert params["homeIndex"] == positions.index(home_um)
    info = {"Z": loop.count, "zHome": params["homeIndex"],
            "zStepUm": params["stepUm"], "bottomToTop": params["bottomToTop"]}
    out = run("""
const ui=require(process.argv[1]+'/plate-ui-v1.js'), info=JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(Array.from({length:info.Z},(_,z)=>({
  offset:ui.zOffsetUm(info,z),
  roundtrip:ui.zIndexAtSlider(info,ui.zSliderPercent(info,z)/100)
}))));
""", info)
    assert [item["offset"] for item in out] == [z - home_um for z in positions]
    assert [item["roundtrip"] for item in out] == list(range(6))


def test_unknown_direction_and_invalid_calibration_do_not_invent_signed_offsets():
    out = run("""
const ui=require(process.argv[1]+'/plate-ui-v1.js');
const base={Z:6,zHome:2,zStepUm:2,bottomToTop:false};
const variants=[{bottomToTop:undefined},{bottomToTop:null},{bottomToTop:0},
 {bottomToTop:'false'},{zStepUm:0},{zStepUm:NaN},{zStepUm:Infinity},
 {zHome:null},{zHome:-1},{zHome:6}];
process.stdout.write(JSON.stringify({
 offsets:variants.map(v=>ui.zOffsetUm({...base,...v},3)),
 unknown:{percent:ui.zSliderPercent({...base,bottomToTop:null},0),
          step:ui.zIndexStep({...base,bottomToTop:null},1)},
 singleton:{percent:ui.zSliderPercent({Z:1,bottomToTop:false},0),
            index:ui.zIndexAtSlider({Z:1,bottomToTop:false},1)}
}));
""")
    assert out == {"offsets": [None] * 10,
                   "unknown": {"percent": 100, "step": 1},
                   "singleton": {"percent": 50, "index": 0}}


HARNESS = r"""
const fs=require('fs'), ui=require(process.argv[1]+'/plate-ui-v1.js');
const src=fs.readFileSync(process.argv[1]+'/app.js','utf8');
function part(name,next){const start=src.indexOf('function '+name+'(');
 return src.slice(start,src.indexOf('function '+next+'(',start));}
class Element {
 constructor(){this.style={};this.attrs={};this.children=[];this.events={};
  this.classList={toggle:(key,on)=>{this[key]=!!on;}};this.rect={top:0,height:120};}
 append(x){this.children.push(x);} replaceChildren(){this.children=[];}
 addEventListener(name,fn){this.events[name]=fn;} setAttribute(k,v){this.attrs[k]=v;}
 getBoundingClientRect(){return this.rect;}
}
const elements=new Map(), $=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const document={createElement:()=>new Element()}, window={Nd2PlateUI:ui};
const clamp=(v,lo,hi)=>Math.max(lo,Math.min(hi,v)), rawValueLabel=String;
const localStorage={setItem(){}}, selected=[];
const state={info:{plate:{Z:6,zHome:2,zStepUm:2,bottomToTop:false,T:2,P:2,sites:[{i:0},{i:1}]}},
 plate:{z:0,t:0,auto:false,focus:0,focusMap:{best:[[0,1],[4,3]],complete:[[true,true],[true,true]]}}};
function currentFocusSummary(){const p=state.plate,sites=p.focus===null?[0,1]:[p.focus];
 return ui.focusSummary(p.focusMap,p.t,sites,p.z,state.info.plate.Z,p.auto);}
function plateZFor(p){return ui.displayedZ(state.plate.focusMap,state.plate.t,p,state.plate.z,state.info.plate.Z,state.plate.auto);}
function renderPlateAuto(){} function paintPlate(){}
function plateFrameChanged(){selected.push({t:state.plate.t,p:state.plate.focus,z:state.plate.z});renderZSlider();}
eval(part('plateZText','platePlaneNote'));
eval(part('setPlateZ','setPlateAuto'));
eval(part('zSliderPct','timeSpanMs'));
$('z-track').rect={top:10,height:100}; buildZSlider();renderZSlider();
function snapshot(){return {index:state.plate.z,top:$('z-knob').style.top,
 aria:$('z-knob').attrs['aria-valuenow'],accessible:$('z-knob').attrs['aria-valuetext'],
 label:$('z-label').children.map(v=>typeof v==='string'?v:v.textContent).join(''),
 home:state.plate.zTickEls.findIndex(e=>e.className.includes('home')),
 homeTop:state.plate.zTickEls[state.info.plate.zHome].style.top};}
function key(key){$('z-knob').events.keydown({key,preventDefault(){},stopPropagation(){}});return snapshot();}
"""


def test_reverse_slider_pointer_arrows_and_home_marker_agree_without_reindexing():
    out = run(HARNESS + """
const initial=snapshot();
$('z-slider').events.pointerdown({target:$('z-track'),clientY:110});
const bottom=snapshot(), up=key('ArrowUp'), down=key('ArrowDown'),home=key('Home'),end=key('End');
process.stdout.write(JSON.stringify({initial,bottom,up,down,home,end,selected}));
""")
    assert out["initial"]["top"] == "10px"
    assert out["initial"]["label"] == "1 of 6 · +4 µm"
    assert out["initial"]["home"] == 2
    assert out["initial"]["homeTop"] == "40px"
    assert out["bottom"]["index"] == 5
    assert out["bottom"]["top"] == "110px"
    assert out["bottom"]["label"] == "6 of 6 · −6 µm"
    assert out["up"]["index"] == 4
    assert out["up"]["top"] == "90px"
    assert out["down"]["index"] == out["home"]["index"] == 5
    assert out["end"]["index"] == 0
    assert out["bottom"]["aria"] == "6"
    assert "Z 6/6 · −6 µm" == out["bottom"]["accessible"]
    assert all(row["t"] == 0 and row["p"] == 0 for row in out["selected"])


def test_autofocus_uses_current_time_and_site_before_physical_z_step():
    out = run(HARNESS + """
Object.assign(state.plate,{t:1,focus:1,auto:true,z:5});renderZSlider();
const focused=snapshot(), stepped=key('ArrowUp');
Object.assign(state.plate,{focus:null,auto:true});renderZSlider();const grid=snapshot();
state.info.plate.bottomToTop=null;Object.assign(state.plate,{focus:1,auto:false,z:0});
renderZSlider();const unknown=snapshot(),unknownUp=key('ArrowUp');
process.stdout.write(JSON.stringify({focused,stepped,grid,unknown,unknownUp,selected}));
""")
    assert out["focused"]["label"] == "4 of 6 · −2 µm · auto"
    assert out["focused"]["aria"] == "4"
    assert out["stepped"]["index"] == 2
    assert out["stepped"]["label"] == "3 of 6 · +0 µm · home"
    assert out["selected"][0] == {"t": 1, "p": 1, "z": 2}
    assert "µm" not in out["grid"]["label"]
    assert "Z 4–5/6" in out["grid"]["label"]
    assert "µm" not in out["unknown"]["accessible"]
    assert out["unknown"]["top"] == "110px"
    assert out["unknownUp"]["index"] == 1
