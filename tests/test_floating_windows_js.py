"""Production floating panel interactions, independent of the slide viewer."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/floating-windows-v1.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const {createManager}=require(process.argv[1]);
function node(){
 const classes=new Set();return {style:{},children:[],handlers:{},clientWidth:0,offsetHeight:250,
 classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),toggle(c,on){if(on===undefined)on=!classes.has(c);on?classes.add(c):classes.delete(c);}},
 append(n){this.children.push(n)},addEventListener(name,fn){this.handlers[name]=fn},setPointerCapture(){},
 querySelector(q){return this.parts[q]},parts:{}};
}
const panels=[],values=new Map(),stage={clientWidth:1000,clientHeight:700};
const document={createElement:node,querySelectorAll:()=>panels};
const storage={getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v)};
const manager=createManager({stage,document,storage});
function panel(extra={}){
 const el=node(),toolbar=node();panels.push(el);
 for(const name of ['.win-body','.win-titlebar','.tl-close','.tl-min','.tl-zoom'])el.parts[name]=node();
 let resized=0,opened=0;
 const api=manager.createWindow(el,{key:'channels',def:()=>({x:14,y:14,w:256,h:null}),minW:232,minH:150,maxW:600,zoomW:480,
 toolbarBtn:toolbar,onResize:()=>resized++,onOpen:()=>opened++,...extra});
 return {el,toolbar,api,resized:()=>resized,opened:()=>opened};
}
function event(extra={}){return {clientX:0,clientY:0,pointerId:1,target:{closest:()=>false},preventDefault(){},stopPropagation(){},...extra}}
"""


def run(script):
    result = subprocess.run(
        [NODE, "-e", HARNESS + script, str(MODULE)], check=True,
        capture_output=True, text=True, timeout=20,
    )
    return json.loads(result.stdout)


def test_geometry_and_collapse_persist_but_closed_state_is_session_only():
    out = run(r"""
values.set('nd2wsi.win.channels',JSON.stringify({rect:{x:90,y:30,w:300,h:200},collapsed:true}));
const p=panel({startClosed:true});const initial={hidden:p.api.isHidden(),collapsed:p.el.classList.contains('collapsed'),width:p.el.style.width};
p.toolbar.handlers.click();const opened={hidden:p.api.isHidden(),collapsed:p.el.classList.contains('collapsed'),height:p.el.style.height,opened:p.opened(),resized:p.resized()};
p.el.parts['.tl-min'].handlers.click();p.el.parts['.tl-close'].handlers.click();
const saved=JSON.parse(values.get('nd2wsi.win.channels'));const next=panel();
process.stdout.write(JSON.stringify({initial,opened,saved,reopened:{hidden:next.api.isHidden(),collapsed:next.el.classList.contains('collapsed')},bodyWidth:next.api.bodyWidth()}));
""")
    assert out["initial"] == {"hidden": True, "collapsed": True, "width": "300px"}
    assert out["opened"] == {"hidden": False, "collapsed": False, "height": "200px", "opened": 1, "resized": 1}
    assert out["saved"] == {"rect": {"x": 90, "y": 30, "w": 300, "h": 200}, "collapsed": True}
    assert out["reopened"] == {"hidden": False, "collapsed": True}
    assert out["bodyWidth"] == 274


def test_resize_minimum_zoom_restore_drag_clamp_and_frontmost_panel():
    out = run(r"""
const p=panel({def:()=>({x:90,y:30,w:300,h:200})});
const west=p.el.children.find(n=>n.className==='rz rz-w');
west.handlers.pointerdown(event());west.handlers.pointermove(event({clientX:100}));west.handlers.pointerup();
const resize=JSON.parse(values.get('nd2wsi.win.channels')).rect;
p.el.parts['.tl-zoom'].handlers.click();const zoom=p.el.style.width;p.el.parts['.tl-zoom'].handlers.click();
const restored=p.el.style.width;const title=p.el.parts['.win-titlebar'];
title.handlers.pointerdown(event());title.handlers.pointermove(event({clientX:5000,clientY:5000}));title.handlers.pointerup();
const drag=JSON.parse(values.get('nd2wsi.win.channels')).rect;
const other=panel({key:'info'});other.el.handlers.pointerdown();const above=Number(other.el.style.zIndex)>Number(p.el.style.zIndex);
p.api.open();const front=Number(p.el.style.zIndex)>Number(other.el.style.zIndex)&&!other.el.classList.contains('focused');
stage.clientWidth=300;stage.clientHeight=100;p.api.clampToStage();
process.stdout.write(JSON.stringify({resize,zoom,restored,drag,above,front,constrained:{x:p.el.style.left,y:p.el.style.top},resized:p.resized()}));
""")
    assert out["resize"] == {"x": 158, "y": 30, "w": 232, "h": 200}
    assert out["zoom"] == "480px" and out["restored"] == "232px"
    assert out["drag"] == {"x": 940, "y": 666, "w": 232, "h": 200}
    assert out["above"] and out["front"]
    assert out["constrained"] == {"x": "240px", "y": "66px"}
    assert out["resized"] == 4


def test_denied_local_storage_does_not_prevent_open_or_geometry_changes():
    out = run(r"""
const inaccessible={getItem(){throw new Error('private')},setItem(){throw new Error('private')}};
const isolated=createManager({stage,document,storage:inaccessible});const el=node();
for(const name of ['.win-body','.win-titlebar','.tl-close','.tl-min','.tl-zoom'])el.parts[name]=node();
const p=isolated.createWindow(el,{key:'private',def:()=>({x:14,y:14,w:256,h:null}),minW:232,minH:150,maxW:600,zoomW:480,startClosed:true});
p.open();p.fitContent();p.close();process.stdout.write(JSON.stringify({hidden:p.isHidden(),width:el.style.width,height:el.style.height}));
""")
    assert out == {"hidden": True, "width": "256px", "height": ""}
