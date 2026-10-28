"""The shell tells panes when they are hidden, and a hidden plate stops playing.

The shell's real applyFrameLayout and activate, and the pane's real
wireCompareRelay, run against fake frames and a fake parent window.
"""
import json
from pathlib import Path

from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"

SHELL = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1]+'/shell-v1.js','utf8');
function production(name){
  const at=source.indexOf('function '+name+'(');
  if(at<0)throw new Error('missing production function: '+name);
  return source.slice(at,source.indexOf('\n}',at)+2);
}
const posts=[];
function frame(sid){
  const classes=new Set();
  return {dataset:{sid},style:{},classList:{toggle:(k,on)=>on?classes.add(k):classes.delete(k),
    contains:k=>classes.has(k)},
    contentWindow:{postMessage:(data,origin)=>posts.push([sid,data,origin])}};
}
const frames=new Map([['a',frame('a')],['b',frame('b')],['c',frame('c')]]);
const divider={setAttribute(){}};
const compare={enabled:false,anchorSid:null,members:[],split:50};
const context=vm.createContext({frames,compare,active:null,VIEWPORT_PROTOCOL_VERSION:2,
  location:{origin:'http://qa.invalid'},
  document:{documentElement:{style:{setProperty(){}}}},$:()=>divider,
  groupSids:()=>compare.enabled?[compare.anchorSid,...compare.members]:[],
  inGroup:sid=>compare.enabled&&[compare.anchorSid,...compare.members].includes(sid),
  syncCompareToolsVisibility(){},closePairPicker(){},stopCompare(){compare.enabled=false;},
  rememberSlide(){},ensureFrame(){},render(){},focusActivePane(){}});
for(const name of ['applyFrameLayout','activate'])vm.runInContext(production(name),context);
const visible=()=>Object.fromEntries([...frames].map(([sid,f])=>[sid,
  f.classList.contains('active')||f.classList.contains('compare-cell')]));
const take=()=>posts.splice(0).filter(([,d])=>d.nd2wsi==='pane-visibility').map(([sid,d,o])=>
  ({sid,visible:d.visible,version:d.version,origin:o}));
"""


def run_shell(body):
    result = run_node(
        SHELL + "process.stdout.write(JSON.stringify((()=>{" + body + "})()));",
        STATIC,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_tab_switches_post_only_visibility_changes_and_match_what_is_displayed():
    out = run_shell("""
context.activate('a');const first=take();
context.activate('a');context.applyFrameLayout();const repeated=take();
context.activate('b');const switched=take();
return {first,repeated,switched,shown:visible()};
""")
    origin = {"version": 2, "origin": "http://qa.invalid"}
    assert out["first"] == [{"sid": "a", "visible": True, **origin},
                            {"sid": "b", "visible": False, **origin},
                            {"sid": "c", "visible": False, **origin}]
    # a divider drag or a refresh re-runs the layout without new messages
    assert out["repeated"] == []
    assert out["switched"] == [{"sid": "a", "visible": False, **origin},
                               {"sid": "b", "visible": True, **origin}]
    assert out["shown"] == {"a": False, "b": True, "c": False}


def test_compare_cells_are_visible_and_leaving_the_group_hides_the_pane():
    out = run_shell("""
context.activate('a');take();
Object.assign(compare,{enabled:true,anchorSid:'a',members:['c']});context.applyFrameLayout();
const grouped={posts:take(),shown:visible()};
compare.members=[];context.applyFrameLayout();
const left={posts:take(),shown:visible()};
return {grouped,left};
""")
    assert out["grouped"]["shown"] == {"a": True, "b": False, "c": True}
    assert [(p["sid"], p["visible"]) for p in out["grouped"]["posts"]] == [("c", True)]
    assert out["left"]["shown"] == {"a": True, "b": False, "c": False}
    assert [(p["sid"], p["visible"]) for p in out["left"]["posts"]] == [("c", False)]


PANE = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1]+'/app.js','utf8');
const at=source.indexOf('function wireCompareRelay(');
const listeners=[],paused=[];
const parent={};
const context=vm.createContext({VIEWPORT_PROTOCOL_VERSION:2,location:{origin:'http://qa.invalid'},
  window:{parent,addEventListener:(type,fn)=>listeners.push([type,fn])},
  $:()=>({addEventListener(){}}),state:{viewer:{addHandler(){}},quitPreparing:false},
  refreshSpatialContext(){},postSpatialReadiness(){},pausePlatePlayback:()=>paused.push(true)});
vm.runInContext(source.slice(at,source.indexOf('\n}',at)+2),context);
context.wireCompareRelay();
const send=(data,extra={})=>listeners.filter(([type])=>type==='message').forEach(([,fn])=>fn(
  {source:parent,origin:'http://qa.invalid',data:{nd2wsi:'pane-visibility',version:2,...data},...extra}));
send({visible:true});const shown=paused.length;
send({visible:false},{source:{}});send({visible:false},{origin:'http://other.invalid'});send({visible:false,version:1});
const foreign=paused.length;
send({visible:false});
process.stdout.write(JSON.stringify({shown,foreign,hidden:paused.length}));
"""


def test_a_hidden_pane_pauses_plate_playback_only_on_the_shells_versioned_message():
    result = run_node(PANE, STATIC)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"shown": 0, "foreign": 0, "hidden": 1}
