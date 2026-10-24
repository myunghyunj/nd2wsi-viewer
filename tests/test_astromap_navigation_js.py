"""Astromap center-only navigation against the bundled OpenSeadragon camera."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs=require('fs'), vm=require('vm');
const root=process.argv[1], config=JSON.parse(process.argv[2]);
const app=fs.readFileSync(root+'/app.js','utf8');
const vendor=fs.readFileSync(root+'/vendor/openseadragon/openseadragon.min.js','utf8');
let clock=1000;
const osd={console,now:()=>clock,positiveModulo:(x,y)=>(x%y+y)%y,
  extend(...args){if(typeof args[0]==='boolean') args.shift();return Object.assign(...args);}};
const context=vm.createContext({OpenSeadragon:osd});
// Reuse the actual geometry, springs and camera implementation, without a DOM.
for(const name of ['Point','Rect','Spring','Viewport']) {
  const marker=vendor.indexOf('.'+name+'=function(');
  const start=vendor.lastIndexOf('!function(',marker);
  const end=vendor.indexOf('}(OpenSeadragon);',marker);
  if(marker<0 || start<0 || end<0) throw Error('Bundled OSD component changed: '+name);
  vm.runInContext(vendor.slice(start,end+'}(OpenSeadragon);'.length),context);
}
function production(name) {
  const start=app.indexOf('function '+name+'(');
  if(start<0) throw Error('Missing production function: '+name);
  return app.slice(start,app.indexOf('\n}',start)+2);
}
const spring=value=>new osd.Spring({initial:value,springStiffness:8,animationTime:0.6});
const viewport=Object.assign(Object.create(osd.Viewport.prototype),{
  containerSize:new osd.Point(1200,800),_containerInnerSize:new osd.Point(1200,800),
  _margins:{left:0,right:0,top:0,bottom:0},
  _contentSizeNoRotate:new osd.Point(40000,20000),
  // Offset and scaled image coordinates ensure no CSS/pixel shortcut can pass.
  _contentBoundsNoRotate:new osd.Rect(0.17,-0.2,2,1),
  centerSpringX:spring(0.8),centerSpringY:spring(0.1),zoomSpring:spring(3.25),
  degreesSpring:spring(config.rotation),flipped:config.flip,
  _oldCenterX:0.8,_oldCenterY:0.1,_oldZoom:3.25,_oldDegrees:config.rotation,
  zoomPoint:null,rotationPivot:null,
});
if(config.animating) {
  viewport.zoomSpring.springTo(12);
  viewport.zoomPoint=new osd.Point(0.4,0.2);
  viewport.centerSpringX.springTo(1.2);
  viewport.centerSpringY.springTo(0.5);
}
viewport.fitBounds=()=>{throw Error('ROI navigation must not fit its bounds');};
viewport.zoomBy=()=>{throw Error('ROI navigation must not apply a zoom factor');};
const zooms=[],pans=[],zoomTo=viewport.zoomTo,panTo=viewport.panTo;
viewport.zoomTo=function(...args){zooms.push(args);return zoomTo.apply(this,args);};
viewport.panTo=function(...args){pans.push(args);return panTo.apply(this,args);};
const item={id:'roi',type:'box',astromap:true,...config.rect};
const state={viewer:{viewport,world:{getItemCount:()=>1}},
  annotations:config.keyboard ? [{...item,id:'previous',x:0,y:0},item] : [item],
  astromap:{activeId:config.keyboard ? 'previous' : null,rows:new Map()},
  windows:{astromap:{isHidden:()=>false}},landmark:{active:false},viewportRelay:{}};
Object.assign(context,{state,renderAnnotations(){},refreshAstromapPanel(){},
  window:{Nd2ShortcutRouter:require(root+'/shortcut-router-v1.js'),
    Nd2ViewerGeometry:require(root+'/viewer-geometry-v1.js')},
  activeFrameContext:()=>({}),annotationOwnerReady:()=>true});
for(const name of ['astromapItems','selectAstromapRoi','panToAstromapRoi','imageToViewerElementPoint',
  'navigateAstromapKey','spatialImageReady'])
  vm.runInContext(production(name),context);
const center={x:item.x+item.w/2,y:item.y+item.h/2};
function scale() {
  const a=context.imageToViewerElementPoint(center);
  const b=context.imageToViewerElementPoint({x:center.x+100,y:center.y});
  return Math.hypot(b.x-a.x,b.y-a.y)/100;
}
const before=JSON.stringify(item),scaleBefore=scale(),zoomBefore=viewport.getZoom(true);
const selected=config.keyboard ? context.navigateAstromapKey({key:'ArrowRight',target:{tagName:'BUTTON'},
  preventDefault(){},stopImmediatePropagation(){}}) : context.selectAstromapRoi(item.id);
const immediate={zoom:viewport.getZoom(true),pixel:context.imageToViewerElementPoint(center),scale:scale()};
clock+=2000;viewport.update();
const later={zoom:viewport.getZoom(true),targetZoom:viewport.getZoom(),
  pixel:context.imageToViewerElementPoint(center),scale:scale()};
const missing=context.selectAstromapRoi('missing');
context.selectAstromapRoi(item.id,false);
process.stdout.write(JSON.stringify({selected,missing,active:state.astromap.activeId,
  unchanged:before===JSON.stringify(item),scaleBefore,zoomBefore,immediate,later,
  rotation:viewport.getRotation(true),flip:viewport.getFlip(),zooms,pans}));
"""


@pytest.mark.parametrize("rotation,flip", [(0, False), (37, True), (90, False), (270, True)])
@pytest.mark.parametrize("animating", [False, True])
@pytest.mark.parametrize("keyboard", [False, True])
@pytest.mark.parametrize("rect", [
    {"x": 12345, "y": 6789, "w": 1, "h": 1},
    {"x": 1000, "y": 2000, "w": 30000, "h": 15000},
])
def test_roi_navigation_centers_source_geometry_without_changing_displayed_scale(rotation, flip, animating, keyboard, rect):
    config = {"rotation": rotation, "flip": flip, "animating": animating, "keyboard": keyboard, "rect": rect}
    result = subprocess.run([NODE, "-e", SCRIPT, str(STATIC), json.dumps(config)],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["selected"] and not out["missing"] and out["active"] == "roi"
    assert out["unchanged"]
    assert out["rotation"] == rotation and out["flip"] == flip
    for frame in (out["immediate"], out["later"]):
        assert frame["zoom"] == out["zoomBefore"]
        assert frame["scale"] == pytest.approx(out["scaleBefore"], abs=1e-11)
        assert frame["pixel"] == pytest.approx({"x": 600, "y": 400}, abs=1e-9)
    assert out["later"]["targetZoom"] == out["zoomBefore"]
    assert len(out["pans"]) == 1 and out["pans"][0][1] is True
