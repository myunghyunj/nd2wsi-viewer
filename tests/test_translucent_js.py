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
  let width=800,height=500,zoom=2.5,center=new Point(.4,.25),rotation=0,flipped=false;
  const calls=[],handlers={};
  const viewport={
    getContainerSize(){return new Point(width,height);},
    getCenter(){return {...center};},getZoom(){return zoom;},
    viewportToImageZoom(z){return z*width/info.width;},
    imageToViewportZoom(z){return z*info.width/width;},
    imageToViewportCoordinates(x,y){return typeof x==='object'
      ? new Point(x.x/info.width,x.y/info.width):new Point(x/info.width,y/info.width);},
    imageToViewerElementCoordinates(point){
      const scale=zoom*width/info.width,angle=rotation*Math.PI/180;
      const dx=(point.x-center.x*info.width)*scale,dy=(point.y-center.y*info.width)*scale;
      return new Point(width/2+dx*Math.cos(angle)-dy*Math.sin(angle),height/2+dx*Math.sin(angle)+dy*Math.cos(angle));},
    resize(size){width=size.x;height=size.y;zoom=99;center=new Point(99,99);},
    zoomTo(value){zoom=value;calls.push(['zoom',value]);},
    panTo(value){center={...value};calls.push(['pan',center]);},
    setRotation(value){rotation=value;calls.push(['rotate',value]);},getRotation(){return rotation;},
    setFlip(value){flipped=value;calls.push(['flip',value]);},getFlip(){return flipped;},
  };
  return {viewport,calls,handlers,nav:true,addHandler(name,cb){handlers[name]=cb;},
    forceRedraw(){},setMouseNavEnabled(value){this.nav=value;},destroy(){}};
}
function harness({failLock=false,failOpacity=false,opacityResult,initialContext={},bridgeOverrides={}}={}){
  const elements=new Map();
  function newElement(id){return {id,style:{},dataset:{},attributes:{},children:[],
    listeners:{},clientWidth:800,clientHeight:500,disabled:false,hidden:false,
    addEventListener(name,cb){this.listeners[name]=cb;},
    setAttribute(name,value){this.attributes[name]=value;},
    replaceChildren(...children){this.children=children;},
    appendChild(child){this.children=this.children.filter(item=>item!==child);this.children.push(child);return child;},
    get lastElementChild(){return this.children[this.children.length-1];}};}
  function element(id){if(!elements.has(id))elements.set(id,newElement(id));return elements.get(id);}
  const documentListeners={};
  const doc={getElementById:element,createElementNS(namespace,name){return newElement(name);},
    querySelectorAll(){return [];},addEventListener(name,cb){documentListeners[name]=cb;}};
  const win={innerWidth:800,innerHeight:500,addEventListener(){}};
  const viewer=fakeViewer();
  let osdOptions;
  const OSD=options=>{osdOptions=options;return viewer;};OSD.Point=Point;
  const nativeCalls=[];
  const bridge={async set_translucent_locked(value){nativeCalls.push(['lock',value]);
      return failLock?{ok:false,error:'lock unavailable'}:{ok:true,locked:value};},
    async set_translucent_opacity(value){nativeCalls.push(['opacity',value]);
      return failOpacity?{ok:false,message:'opacity unavailable'}:{ok:true,opacity:opacityResult??value};},
    async close_translucent(){nativeCalls.push(['close']);return true;},
    async choose_roi_file(){nativeCalls.push(['choose']);return {ok:true,name:'test.txt',
      text:'INDEX\tX1\tY1\tX2\tY2\tNOTE\n1\t0\t0\t3\t3\t',coordinateSize:{width:1000,height:500}};}};
  Object.assign(bridge,bridgeOverrides);
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


def test_invalid_200_roi_pair_reports_both_data_issues_and_preserves_loaded_list_and_camera():
    result = run(r"""
      const h=harness(),m=h.mounted;
      m.setRois({ok:true,name:'valid.txt',rois:[{x:100,y:100,w:4,h:4,index:7}]});
      const before={camera:api.camera(h.viewer),rois:m.state.rois};
      const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n'+Array.from({length:200},(_,i)=>{
        const index=i+1;
        if(index===199)return '199\t4877\t8248\t4876\t8247\t199';
        const y=index>=58&&index<=182?5900:100;
        return `${index}\t100\t${y}\t110\t${y+10}\t${index}`;
      }).join('\n');
      const accepted=m.setRois({ok:true,name:'200.txt',text,coordinateSize:{width:8000,height:5830}});
      return {accepted,name:m.state.roiName,label:h.element('roi-index').textContent,
        error:h.element('overlay-status').textContent,
        unchanged:JSON.stringify(before)===JSON.stringify({camera:api.camera(h.viewer),rois:m.state.rois})};
    """)
    assert result["accepted"] is False and result["unchanged"]
    assert result["name"] == "valid.txt" and result["label"] == "1/1"
    assert "INDEX 199 has zero area (4877, 8248) → (4876, 8247)" in result["error"]
    assert "125 regions are outside" in result["error"] and "first INDEX 58" in result["error"]


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


@pytest.mark.parametrize("mode,opacity", [("layer", .55), ("layer", .73), ("native", .73)])
def test_opacity_slider_honors_initial_selection_without_double_applying_native_alpha(mode, opacity):
    result = run(f"""
      const h=harness({{initialContext:{{opacity_mode:{json.dumps(mode)},opacity:{opacity}}}}});
      const slider=h.element('overlay-opacity');
      return {{value:slider.value,label:h.element('opacity-value').textContent,
        accessible:slider.attributes['aria-valuetext'],disabled:slider.disabled,
        layer:h.element('translucent-stage').style.opacity,native:h.nativeCalls,
        defaultOpacity:api.validateContext({{...context,opacity:undefined}},context.slide_url).opacity}};
    """)
    percent = str(round(opacity * 100))
    assert result == {"value": percent, "label": percent + "%", "accessible": percent + "%",
                      "disabled": False, "layer": "1" if mode == "native" else str(opacity),
                      "native": [], "defaultOpacity": .55}


@pytest.mark.parametrize("mode", ["layer", "native"])
def test_opacity_slider_updates_live_then_syncs_response_without_moving_image_or_roi(mode):
    result = run(f"""
      const h=harness({{initialContext:{{opacity_mode:{json.dumps(mode)}}},opacityResult:.63}}),m=h.mounted;
      m.setRois({{rois:[{{x:0,y:0,w:1,h:1}},{{x:100,y:100,w:1,h:1}}]}});
      const before=api.camera(h.viewer),slider=h.element('overlay-opacity');
      slider.value='70';const update=slider.listeners.input();
      const live=h.element('translucent-stage').style.opacity;
      await update;
      let prevented=0,stopped=0;
      const event={{key:'ArrowRight',altKey:false,target:{{closest:()=>h.element('opacity-control')}},
        preventDefault(){{prevented++;}},stopPropagation(){{stopped++;}}}};
      h.documentListeners.keydown(event);
      const arrowPrevented=prevented;
      h.element('opacity-control').listeners.pointerdown(event);
      h.element('opacity-control').listeners.wheel(event);
      h.element('overlay').listeners.pointerdown({{...event,altKey:true}});
      return {{live,layer:h.element('translucent-stage').style.opacity,value:slider.value,
        label:h.element('opacity-value').textContent,opacity:m.state.opacity,
        confirmed:m.state.confirmedOpacity,native:h.nativeCalls,
        cameraUnchanged:JSON.stringify(before)===JSON.stringify(api.camera(h.viewer)),
        roi:m.state.roiIndex,arrowPrevented,prevented,stopped,gesture:m.state.gesture}};
    """)
    assert result == {"live": "1" if mode == "native" else "0.7",
                      "layer": "1" if mode == "native" else "0.63",
                      "value": "63", "label": "63%", "opacity": .63, "confirmed": .63,
                      "native": [["opacity", .7]], "cameraUnchanged": True,
                      "roi": 0, "arrowPrevented": 0, "prevented": 1, "stopped": 2, "gesture": None}


def test_lock_drains_in_flight_and_latest_queued_opacity_before_freezing_slider():
    result = run("""
      let finish;const calls=[];
      const h=harness({bridgeOverrides:{set_translucent_opacity(value){
        calls.push(['opacity',value]);
        return value===.3?new Promise(resolve=>{finish=resolve;}):Promise.resolve({ok:true,opacity:value});},
        async set_translucent_locked(value){calls.push(['lock',value]);return {ok:true,locked:value};}}}),m=h.mounted;
      const update=m.setOpacity(.3);await Promise.resolve();
      m.setOpacity(.8);
      const locking=m.toggleLock(),disabled=h.element('overlay-opacity').disabled;
      const rejected=await m.setOpacity(.9),before=calls.slice();
      finish({ok:true,opacity:.3});await update;await locking;
      const locked={value:h.element('overlay-opacity').value,opacity:m.state.opacity,
        disabled:h.element('overlay-opacity').disabled};
      await m.toggleLock();
      return {disabled,rejected,before,locked,calls,unlocked:h.element('overlay-opacity').disabled};
    """)
    assert result == {"disabled": True, "rejected": False, "before": [["opacity", .3]],
                      "locked": {"value": "80", "opacity": .8, "disabled": True},
                      "calls": [["opacity", .3], ["opacity", .8], ["lock", True], ["lock", False]],
                      "unlocked": False}


def test_failed_opacity_change_rolls_back_selection_and_image_to_last_confirmed_value():
    result = run("""
      let failing=false;
      const h=harness({bridgeOverrides:{async set_translucent_opacity(value){
        return failing?{ok:false,message:'opacity unavailable'}:{ok:true,opacity:value};}}}),m=h.mounted;
      await m.setOpacity(.3);failing=true;
      const update=m.setOpacity(.8),live=h.element('translucent-stage').style.opacity;
      const success=await update;
      const failed={success,live,value:h.element('overlay-opacity').value,
        opacity:m.state.opacity,layer:h.element('translucent-stage').style.opacity,
        label:h.element('opacity-value').textContent,error:h.element('overlay-status').textContent};
      failing=false;await m.setOpacity(.7);
      return {failed,recovered:m.state.opacity,statusHidden:h.element('overlay-status').hidden};
    """)
    assert result == {"failed": {"success": False, "live": "0.8", "value": "30", "opacity": .3,
                                 "layer": "0.3", "label": "30%", "error": "opacity unavailable"},
                      "recovered": .7, "statusHidden": True}


def test_roi_boxes_draw_all_source_rectangles_and_selection_changes_without_geometry_edits():
    result = run("""
      const rois=[{x:390,y:240,w:20,h:20},{x:450,y:250,w:10,h:10}];
      const h=harness({initialContext:{rotation:0,flip:false,rois,roiIndex:0}}),m=h.mounted;
      const layer=h.element('roi-overlay'),snapshot=()=>layer.children.map(box=>({...box.attributes}));
      const initial=snapshot(),before=JSON.stringify(m.state.rois),zoom=api.camera(h.viewer).zoom;
      m.selectRoi(1);
      const selected=snapshot();
      await m.setOpacity(.1);await m.toggleLock();
      return {initial,selected,locked:snapshot(),sourceUnchanged:before===JSON.stringify(m.state.rois),
        inputUnchanged:rois,zoomUnchanged:zoom===api.camera(h.viewer).zoom,
        imageOpacity:h.element('translucent-stage').style.opacity,layerOpacity:layer.style.opacity??null};
    """)
    initial = {item["data-roi-index"]: item for item in result["initial"]}
    assert initial["0"]["points"] == "380,230 420,230 420,270 380,270"
    assert initial["1"]["points"] == "500,250 520,250 520,270 500,270"
    assert initial["0"]["data-selected"] == "true"
    assert initial["1"]["data-selected"] == "false"
    assert result["selected"][-1]["data-roi-index"] == "1"
    assert result["selected"][-1]["data-selected"] == "true"
    assert result["selected"][-1]["points"] == "390,240 410,240 410,260 390,260"
    assert result["locked"] == result["selected"]
    assert result["sourceUnchanged"] is True
    assert result["inputUnchanged"] == [{"x": 390, "y": 240, "w": 20, "h": 20},
                                         {"x": 450, "y": 250, "w": 10, "h": 10}]
    assert result["zoomUnchanged"] is True
    assert result["imageOpacity"] == "0.1"
    assert result["layerOpacity"] is None  # Image opacity never writes the sibling ROI layer.


def test_roi_box_points_follow_pan_zoom_and_resize_at_constant_image_scale():
    result = run("""
      const h=harness({initialContext:{rotation:0,flip:false,rois:[{x:400,y:250,w:20,h:10}],roiIndex:0}}),m=h.mounted;
      const layer=h.element('roi-overlay'),points=()=>layer.children[0].attributes.points;
      const initial=points();
      h.viewer.viewport.panTo(new Point(.41,.255));
      h.viewer.viewport.zoomTo(h.viewer.viewport.imageToViewportZoom(3));
      h.viewer.handlers['update-viewport']();
      const moved=points(),camera=api.camera(h.viewer);
      h.element('translucent-stage').clientWidth=220;h.element('translucent-stage').clientHeight=160;
      m.resize();
      return {initial,moved,resized:points(),viewBox:layer.attributes.viewBox,
        cameraUnchanged:JSON.stringify(camera)===JSON.stringify(api.camera(h.viewer)),roi:m.state.rois[0]};
    """)
    assert result == {"initial": "400,250 440,250 440,270 400,270",
                      "moved": "370,235 430,235 430,265 370,265",
                      "resized": "80,65 140,65 140,95 80,95", "viewBox": "0 0 220 160",
                      "cameraUnchanged": True,
                      "roi": {"x": 400, "y": 250, "w": 20, "h": 10, "label": ""}}


@pytest.mark.parametrize("rotation,flip,expected", [
    (90, False, [[420, 230], [420, 270], [400, 270], [400, 230]]),
    (90, True, [[380, 230], [380, 270], [400, 270], [400, 230]]),
    (30, True, [[407.3205080756888, 222.67949192431124],
                [372.6794919243112, 242.67949192431124],
                [382.6794919243112, 260], [417.3205080756888, 240]]),
])
def test_roi_box_uses_four_rotated_and_flipped_corners(rotation, flip, expected):
    result = run(f"""
      const h=harness({{initialContext:{{rotation:0,flip:false,rois:[{{x:390,y:240,w:20,h:10}}],roiIndex:0}}}});
      h.viewer.viewport.setRotation({rotation});h.viewer.viewport.setFlip({str(flip).lower()});
      h.viewer.handlers['update-viewport']();
      return h.element('roi-overlay').children[0].attributes.points.split(' ').map(point=>point.split(',').map(Number));
    """)
    for actual, coordinates in zip(result, expected, strict=True):
        assert actual == pytest.approx(coordinates)


def test_invalid_roi_replacement_keeps_existing_boxes_and_nonfinite_projection_is_hidden():
    result = run("""
      const h=harness({initialContext:{rotation:0,flip:false,rois:[{x:390,y:240,w:20,h:10}],roiIndex:0}}),m=h.mounted;
      const layer=h.element('roi-overlay'),before=JSON.stringify(layer.children[0].attributes);
      const rejected=m.setRois({rois:[{x:1001,y:0,w:1,h:1}]});
      const unchanged=before===JSON.stringify(layer.children[0].attributes);
      h.viewer.viewport.imageToViewerElementCoordinates=()=>({x:NaN,y:0});
      h.viewer.handlers['update-viewport']();
      return {rejected,unchanged,display:layer.children[0].style.display,
        source:m.state.rois[0],count:layer.children.length};
    """)
    assert result == {"rejected": False, "unchanged": True, "display": "none",
                      "source": {"x": 390, "y": 240, "w": 20, "h": 10, "label": ""}, "count": 1}
