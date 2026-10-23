"""Translucent tracing preserves source rendering and image scale during navigation."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/translucent-v1.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


SETUP = r"""
const api = require(process.argv[1]);
const importer = require(require("path").join(require("path").dirname(process.argv[1]),"astromap-import-v1.js"));
const info = {width:1000,height:500,tileSize:256,rgb:false,generation:'original',
  levels:[{width:1000,height:500,path:'0'},{width:501,height:251,path:'1'}]};
const context={version:1,slide_url:'http://localhost:9876/token/s/abc/',
  query:'c=0%2C2&win=10%3A300%3A0.25%2C%2C2%3A150&g=original&t=2&p=4&z=1',
  center:[400,250],zoom:2,rotation:90,flip:true,opacity:.55};
const Point=class {constructor(x,y){this.x=x;this.y=y;}};
function fakeViewer(){
  let width=800,height=500,zoom=2.5,center=new Point(.4,.25);
  const calls=[],handlers={};
  const viewport={
    getContainerSize(){return new Point(width,height);},
    getCenter(){return {...center};},getZoom(){return zoom;},
    viewportToImageZoom(z){return z*width/info.width;},
    imageToViewportZoom(z){return z*info.width/width;},
    imageToViewportCoordinates(x,y){return typeof x==='object'
      ? new Point(x.x/info.width,x.y/info.width):new Point(x/info.width,y/info.width);},
    resize(size){width=size.x;height=size.y;zoom=99;center=new Point(99,99);},
    zoomTo(value){zoom=value;calls.push(['zoom',value]);},
    panTo(value){center={...value};calls.push(['pan',center]);},
    setRotation(value){calls.push(['rotate',value]);},setFlip(value){calls.push(['flip',value]);},
  };
  return {viewport,calls,handlers,nav:true,addHandler(name,cb){handlers[name]=cb;},
    forceRedraw(){},setMouseNavEnabled(value){this.nav=value;},destroy(){}};
}
function harness({failLock=false,initialContext={}}={}){
  const elements=new Map();
  function element(id){if(!elements.has(id))elements.set(id,{id,style:{},dataset:{},attributes:{},
    listeners:{},clientWidth:800,clientHeight:500,disabled:false,hidden:false,
    addEventListener(name,cb){this.listeners[name]=cb;},
    setAttribute(name,value){this.attributes[name]=value;}});return elements.get(id);}
  const documentListeners={};
  const doc={getElementById:element,querySelectorAll(){return [];},addEventListener(name,cb){documentListeners[name]=cb;}};
  const win={innerWidth:800,innerHeight:500,addEventListener(){}};
  const viewer=fakeViewer();
  let osdOptions;
  const OSD=options=>{osdOptions=options;return viewer;};OSD.Point=Point;
  const nativeCalls=[];
  const bridge={async set_translucent_locked(value){nativeCalls.push(['lock',value]);
      return failLock?{ok:false,error:'lock unavailable'}:{ok:true,locked:value};},
    async close_translucent(){nativeCalls.push(['close']);return true;},
    async choose_roi_file(){nativeCalls.push(['choose']);return {ok:true,name:'test.txt',
      text:'INDEX\tX1\tY1\tX2\tY2\tNOTE\n1\t0\t0\t3\t3\t',coordinateSize:{width:1000,height:500}};}};
  const mounted=api.mount({window:win,document:doc,api:bridge,OSD,context:{...context,...initialContext},info,importer});
  viewer.handlers.open();
  return {mounted,viewer,element,win,nativeCalls,osdOptions,documentListeners};
}
"""


def run(body):
    script = SETUP + "\n(async()=>{\n" + body + "\n})().then(value=>process.stdout.write(JSON.stringify(value))).catch(error=>{console.error(error);process.exit(1);});"
    result = subprocess.run([NODE, "-e", script, str(MODULE)], check=True, timeout=20,
                            capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout)


def test_tile_source_keeps_channels_lut_gamma_generation_and_selected_plate_frame():
    result = run("""
      const source=api.makeTileSource(info,context,{Point});
      return {url:source.getTileUrl(0,1,0),tiles:source.getNumTiles(0),scale:source.getLevelScale(0)};
    """)
    assert result["url"] == (
        "http://localhost:9876/token/s/abc/api/tile/1/1/0.jpg?"
        "c=0%2C2&win=10%3A300%3A0.25%2C%2C2%3A150&g=original&t=2&p=4&z=1"
    )
    assert result["tiles"] == {"x": 2, "y": 1}
    assert result["scale"] == .501


def test_stale_generation_and_external_source_are_rejected_before_tiles_load():
    result = run("""
      const errors=[];
      for(const operation of [()=>api.makeTileSource({...info,generation:'rebuilt'},context,{Point}),
        ()=>api.validateContext({...context,slide_url:'https://outside.example/s/abc/'},context.slide_url),
        ()=>api.validateContext({...context,query:'c=0&c=1'},context.slide_url)]){
        try{operation();errors.push(null);}catch(error){errors.push(error.message);}}
      return errors;
    """)
    assert "source changed" in result[0]
    assert "belong to this viewer" in result[1]
    assert "Invalid image display" in result[2]


@pytest.mark.parametrize("width,height", [(1200, 500), (800, 1000), (220, 160)])
def test_resize_changes_field_of_view_without_scale_or_center_change(width, height):
    result = run(f"""
      const viewer=fakeViewer(),before=api.camera(viewer);
      api.resizePreservingScale(viewer,{width},{height},{{Point}});
      const after=api.camera(viewer),size=viewer.viewport.getContainerSize();
      return {{before,after,field:{{width:size.x/after.zoom,height:size.y/after.zoom}}}};
    """)
    assert result["after"]["center"] == result["before"]["center"]
    assert result["after"]["zoom"] == pytest.approx(result["before"]["zoom"])
    assert result["field"] == pytest.approx({"width": width / 2, "height": height / 2})


def test_roi_jump_only_pans_even_for_tiny_asymmetric_astromapper_rectangles():
    result = run("""
      const viewer=fakeViewer(),before=api.camera(viewer);
      api.focusRoi(viewer,{x:800,y:100,w:2,h:8},info);
      const after=api.camera(viewer),count=viewer.calls.length;
      const blocked=api.focusRoi(viewer,{x:0,y:0,w:4,h:4},info,true);
      return {before,after,blocked,unchanged:count===viewer.calls.length};
    """)
    assert result["after"]["zoom"] == result["before"]["zoom"]
    assert result["after"]["center"] == {"x": .801, "y": .104}
    assert result["blocked"] is False
    assert result["unchanged"] is True


def test_roi_navigation_is_bounded_and_lock_freezes_input_but_leaves_exit_available():
    result = run("""
      const h=harness(),m=h.mounted;
      m.setRois({ok:true,name:'sample.txt',rois:[{x:100,y:100,w:4,h:4},{x:500,y:200,w:2,h:8}]});
      const first={label:h.element('roi-index').textContent,prev:h.element('roi-previous').disabled};
      m.selectRoi(1);
      const last={label:h.element('roi-index').textContent,next:h.element('roi-next').disabled};
      const before=api.camera(h.viewer);
      await m.toggleLock();
      const nav=m.selectRoi(0),replacement=m.setRois({ok:true,rois:[{x:0,y:0,w:10,h:10}]});
      const locked={nav,replacement,camera:api.camera(h.viewer),mouse:h.viewer.nav,
        prev:h.element('roi-previous').disabled,picker:h.element('roi-index').disabled,
        pressed:h.element('overlay-lock').attributes['aria-pressed']};
      await m.close();await m.toggleLock();
      return {first,last,before,locked,unlockedMouse:h.viewer.nav,native:h.nativeCalls};
    """)
    assert result["first"] == {"label": "1/2", "prev": True}
    assert result["last"] == {"label": "2/2", "next": True}
    assert result["locked"] == {
        "nav": False, "replacement": False, "camera": result["before"],
        "mouse": False, "prev": True, "picker": True, "pressed": "true",
    }
    assert result["unlockedMouse"] is True
    assert result["native"] == [["lock", True], ["close"], ["lock", False]]


def test_failed_lock_restores_interaction_and_invalid_roi_import_keeps_previous_list():
    result = run("""
      const h=harness({failLock:true}),m=h.mounted;
      m.setRois({ok:true,name:'valid.txt',rois:[{x:100,y:100,w:4,h:4}]});
      const accepted=m.setRois({ok:true,name:'wrong.txt',rois:[{x:1100,y:100,w:4,h:4}]});
      await m.toggleLock();
      return {accepted,name:m.state.roiName,label:h.element('roi-index').textContent,
        locked:m.state.locked,mouse:h.viewer.nav,error:h.element('overlay-status').textContent};
    """)
    assert result == {"accepted": False, "name": "valid.txt", "label": "1/1",
                      "locked": False, "mouse": True, "error": "lock unavailable"}


def test_native_resize_anchor_follows_minimum_dimensions():
    assert run("return api.resizeGeometry(800,500,'nw',1000,1000);") == {
        "width": 220, "height": 160, "left": 580, "top": 340,
    }


def test_navigation_is_hidden_until_a_valid_pair_is_loaded_and_failed_replace_is_atomic():
    result = run("""
      const h=harness(),m=h.mounted;
      const initial=h.element('roi-controls').hidden;
      m.setRois({ok:false,message:'Select both a JPG reference and TXT coordinate file'});
      const failed=h.element('roi-controls').hidden;
      const message=h.element('overlay-status').textContent;
      m.setRois({ok:true,name:'pair.txt',rois:[{x:10,y:20,w:4,h:4}]});
      const loaded=h.element('roi-controls').hidden;
      const before=api.camera(h.viewer);
      m.setRois({ok:true,name:'invalid.txt',rois:[{x:10,y:20,w:4,h:4},{x:2000,y:20,w:4,h:4}]});
      return {initial,failed,message,loaded,final:h.element('roi-controls').hidden,name:m.state.roiName,
        count:m.state.rois.length,unchanged:JSON.stringify(before)===JSON.stringify(api.camera(h.viewer))};
    """)
    assert result == {"initial": True, "failed": True,
                      "message": "Select both a JPG reference and TXT coordinate file",
                      "loaded": False, "final": False,
                      "name": "pair.txt", "count": 1, "unchanged": True}


def test_pair_picker_contextmenu_and_shortcuts_are_blocked_while_locked():
    result = run("""
      const h=harness();let prevented=0;
      const event=extra=>({key:'o',preventDefault(){prevented++;},...extra});
      h.element('translucent-stage').listeners.contextmenu(event());
      await Promise.resolve();await Promise.resolve();
      h.documentListeners.keydown(event({metaKey:true}));
      await Promise.resolve();await Promise.resolve();
      h.documentListeners.keydown(event({ctrlKey:true}));
      await Promise.resolve();await Promise.resolve();
      await h.mounted.toggleLock();
      h.element('translucent-stage').listeners.contextmenu(event());
      h.documentListeners.keydown(event({ctrlKey:true}));
      await Promise.resolve();
      return {choose:h.nativeCalls.filter(call=>call[0]==='choose').length,prevented,
        hidden:h.element('roi-controls').hidden};
    """)
    assert result == {"choose": 3, "prevented": 5, "hidden": False}


def test_canonical_mapper_retains_inclusive_single_pixels_and_small_reference_scaling():
    result = run(r"""
      const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n7\t1\t2\t1\t2\tTiny ROI';
      const mapped=api.importedRois({text,coordinateSize:{width:100,height:50}},info,importer);
      const identity=api.importedRois({text,coordinateSize:{width:1000,height:500}},info,importer);
      const h=harness(),before=api.camera(h.viewer);
      h.mounted.setRois({ok:true,name:'pair.txt',text,coordinateSize:{width:100,height:50}});
      return {mapped,identity,after:api.camera(h.viewer),zoom:before.zoom};
    """)
    assert result["mapped"] == [{"x": 10, "y": 20, "w": 10, "h": 10,
                                "index": 7, "label": "Tiny ROI"}]
    assert result["identity"] == [{"x": 1, "y": 2, "w": 1, "h": 1,
                                  "index": 7, "label": "Tiny ROI"}]
    assert result["after"]["center"] == {"x": .015, "y": .025}
    assert result["after"]["zoom"] == result["zoom"]


def test_canonical_pair_mapping_failure_keeps_imported_source_boxes_and_camera():
    result = run(r"""
      const h=harness({initialContext:{rois:[{x:10,y:10,w:1,h:1,text:'Saved',astromapImportIndex:42}],roiIndex:0}});
      const before=api.camera(h.viewer);
      const accepted=h.mounted.setRois({ok:true,name:'wrong.txt',
        text:'INDEX\tX1\tY1\tX2\tY2\tNOTE\n7\t1\t2\t1\t2\tTiny ROI',
        coordinateSize:{width:100,height:100}});
      return {accepted,rois:h.mounted.state.rois,label:h.element('roi-index').textContent,
        error:h.element('overlay-status').textContent,unchanged:JSON.stringify(before)===JSON.stringify(api.camera(h.viewer))};
    """)
    assert result["accepted"] is False
    assert result["rois"] == [{"x": 10, "y": 10, "w": 1, "h": 1,
                              "index": 42, "label": "Saved"}]
    assert result["label"] == "1/1"
    assert "aspect ratios" in result["error"]
    assert result["unchanged"] is True


@pytest.mark.parametrize("selected", [-1, 1])
def test_initial_import_handoff_keeps_source_camera_until_explicit_navigation(selected):
    result = run(f"""
      const h=harness({{initialContext:{{rois:[{{x:0,y:0,w:1,h:1}},{{x:500,y:200,w:1,h:1}}],roiIndex:{selected}}}}});
      const initial={{camera:api.camera(h.viewer),label:h.element('roi-index').textContent,
        nextDisabled:h.element('roi-next').disabled,hidden:h.element('roi-controls').hidden}};
      h.mounted.selectRoi(0);
      return {{initial,after:api.camera(h.viewer)}};
    """)
    assert result["initial"]["camera"] == {"center": {"x": .4, "y": .25}, "zoom": 2}
    assert result["initial"]["label"] == ("—/2" if selected == -1 else "2/2")
    assert result["initial"]["nextDisabled"] is (selected == 1)
    assert result["initial"]["hidden"] is False
    assert result["after"] == {"center": {"x": .0005, "y": .0005}, "zoom": 2}


def test_rgb_raster_uses_existing_tile_source_while_brightfield_remains_unsupported():
    result = run("""
      let rejected=false;
      try{api.makeTileSource({...info,rgb:true},context,{Point});}catch(_){rejected=true;}
      const raster=api.makeTileSource({...info,rgb:true,raster:true},context,{Point});
      return {rejected,url:raster.getTileUrl(1,0,0)};
    """)
    assert result["rejected"] is True
    assert "/api/tile/0/0/0.jpg?" in result["url"]
