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
  center:[400,250],zoom:2,rotation:90,flip:true,mirror_scan:false,opacity:.55};
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
    viewportToImageCoordinates(point){return new Point(point.x*info.width,point.y*info.width);},
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
function harness({failLock=false,failOpacity=false,opacityResult,initialContext={},bridgeOverrides={},infoOverrides={}}={}){
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
  const windowListeners={},timers=new Map(),intervals=new Map();let timerIndex=0;
  const storage=new Map();
  const win={innerWidth:800,innerHeight:500,addEventListener(name,fn){windowListeners[name]=fn;},
    localStorage:{getItem(k){return storage.has(k)?storage.get(k):null;},
      setItem(k,v){storage.set(k,String(v));},removeItem(k){storage.delete(k);}},
    setTimeout(fn,delay){const id=++timerIndex;timers.set(id,{fn,delay});return id;},
    clearTimeout(id){timers.delete(id);},
    setInterval(fn,delay){const id=++timerIndex;intervals.set(id,{fn,delay});return id;},
    clearInterval(id){intervals.delete(id);}};
  const viewer=fakeViewer();
  let osdOptions;
  const OSD=options=>{osdOptions=options;return viewer;};OSD.Point=Point;
  const nativeCalls=[];
  const bridge={async slacs_discovery_state(){return {available:true,session_id:'test-process',status:'running'};},
    async set_translucent_locked(value){nativeCalls.push(['lock',value]);
      return failLock?{ok:false,error:'lock unavailable'}:{ok:true,locked:value};},
    async set_translucent_opacity(value){nativeCalls.push(['opacity',value]);
      return failOpacity?{ok:false,message:'opacity unavailable'}:{ok:true,opacity:opacityResult??value};},
    async close_translucent(){nativeCalls.push(['close']);return true;},
    async choose_roi_file(){nativeCalls.push(['choose']);return {ok:true,name:'test.txt',
      text:'INDEX\tX1\tY1\tX2\tY2\tNOTE\n1\t0\t0\t3\t3\t',coordinateSize:{width:1000,height:500}};}};
  Object.assign(bridge,bridgeOverrides);
  const mounted=api.mount({window:win,document:doc,api:bridge,OSD,context:{...context,...initialContext},
    info:{...info,...infoOverrides},importer});
  viewer.handlers.open();
  return {mounted,viewer,element,win,storage,nativeCalls,osdOptions,documentListeners,windowListeners,timers,intervals};
}
function followHarness(options={}){
  const snapshot={enabled:true,session_id:'session-one',seq:0,displacement_um:[0,0],status:'waiting'};
  const calls=[];
  const h=harness({...options,infoOverrides:{pixelSizeUm:[2,.5],...options.infoOverrides},
    bridgeOverrides:{async start_stage_follow(){calls.push('start');return {ok:true,session_id:'session-one',
        token:'never-display-this',url:'http://127.0.0.1:1234/motion',protocol:'test'};},
      async stage_follow_state(){calls.push('poll');return {...snapshot};},
      async stop_stage_follow(){calls.push('stop');return {ok:true};},
      async save_stage_follow_connection(){calls.push('save');return {ok:true};},...options.bridgeOverrides}});
  return {...h,snapshot,followCalls:calls};
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


def test_stage_follow_is_opt_in_and_locked_commands_pan_image_and_existing_roi_together():
    result = run("""
      const rois=[{x:390,y:240,w:20,h:20}],h=followHarness({initialContext:{rois,roiIndex:0}}),m=h.mounted;
      const initial={enabled:m.state.follow.enabled,calls:h.followCalls.slice(),timers:[...h.timers.values()].filter(t=>t.delay===50).length};
      await m.startStageFollow();await m.toggleLock();
      const before={camera:api.camera(h.viewer),source:JSON.stringify(m.state.rois),
        points:h.element('roi-overlay').children[0].attributes.points,opacity:m.state.opacity};
      Object.assign(h.snapshot,{seq:1,status:'following',displacement_um:[10,20]});
      await m.pollStageFollow();
      return {initial,before,after:api.camera(h.viewer),
        roiMoved:before.points!==h.element('roi-overlay').children[0].attributes.points,
        sourceUnchanged:before.source===JSON.stringify(m.state.rois),
        rotation:h.viewer.viewport.getRotation(),flip:h.viewer.viewport.getFlip(),opacity:m.state.opacity,
        mouse:h.viewer.nav,manualRoi:m.selectRoi(0),lockTitle:h.element('overlay-lock').title,
        status:h.element('stage-follow-status').textContent,
        delta:h.element('stage-follow-delta').textContent,scale:h.element('stage-follow-scale').textContent};
    """)
    assert result["initial"] == {"enabled": False, "calls": [], "timers": 0}
    assert result["after"]["center"] == pytest.approx({"x": .42, "y": .26})
    assert result["after"]["zoom"] == result["before"]["camera"]["zoom"]
    assert result["roiMoved"] and result["sourceUnchanged"]
    assert result["rotation"] == 90 and result["flip"] is True
    assert result["opacity"] == result["before"]["opacity"] == .55
    assert result["mouse"] is False and result["manualRoi"] is False
    assert "Stage commands still move the image" in result["lockTitle"]
    assert result["status"] == "Following SLACS keys"
    assert result["delta"] == "Last image move: 20.000, 10.000 px"
    assert result["scale"] == "Pixel size: X 0.500 · Y 2.000 µm/px"


def test_unlocked_manual_alignment_is_retained_by_next_cumulative_stage_command():
    result = run("""
      const h=followHarness(),m=h.mounted;
      await m.startStageFollow();await m.toggleLock();
      Object.assign(h.snapshot,{seq:1,status:'following',displacement_um:[10,20]});await m.pollStageFollow();
      await m.toggleLock();const manualEnabled=h.viewer.nav;
      h.viewer.viewport.panTo(new Point(.6,.3));
      Object.assign(h.snapshot,{seq:2,displacement_um:[15,30]});await m.pollStageFollow();
      return {manualEnabled,camera:api.camera(h.viewer),locked:m.state.locked};
    """)
    assert result == {"manualEnabled": True, "camera": {"center": {"x": .61, "y": .305}, "zoom": 2}, "locked": False}


def test_stage_poll_can_skip_events_without_losing_displacement_or_reapplying_duplicates():
    result = run("""
      const h=followHarness(),m=h.mounted;await m.startStageFollow();
      Object.assign(h.snapshot,{seq:1,status:'following',displacement_um:[1.25,2.5]});await m.pollStageFollow();
      Object.assign(h.snapshot,{seq:10,displacement_um:[4.75,8.5]});await m.pollStageFollow();
      const before=h.viewer.calls.filter(c=>c[0]==='pan').length;
      await m.pollStageFollow();
      return {camera:api.camera(h.viewer),delta:m.state.follow.lastDelta,
        duplicateDidNotPan:before===h.viewer.calls.filter(c=>c[0]==='pan').length,
        timerDelays:[...h.timers.values()].map(timer=>timer.delay)};
    """)
    assert result["camera"]["center"] == pytest.approx({"x": .4095, "y": .25425})
    assert result["delta"] == pytest.approx({"x": 7, "y": 3})
    assert result["duplicateDidNotPan"] and result["timerDelays"] == [50]


def test_stage_next_roi_sized_delta_moves_outlines_without_selecting_or_editing_rois():
    result = run("""
      const rois=[{x:390,y:240,w:20,h:20},{x:450,y:250,w:20,h:20}];
      const h=followHarness({initialContext:{rotation:0,flip:false,rois,roiIndex:0}}),m=h.mounted;
      const source=JSON.stringify(m.state.rois),original=JSON.stringify(rois);
      const boxes=()=>h.element('roi-overlay').children.map(b=>({...b.attributes}));
      const before=boxes();await m.startStageFollow();await m.toggleLock();
      Object.assign(h.snapshot,{seq:2,status:'following',displacement_um:[30,20]});await m.pollStageFollow();
      return {before,after:boxes(),sourceUnchanged:source===JSON.stringify(m.state.rois),
        inputUnchanged:original===JSON.stringify(rois),selected:m.state.roiIndex};
    """)
    boxes = {box["data-roi-index"]: box for box in result["after"]}
    assert boxes["1"]["points"] == "380,230 420,230 420,270 380,270"
    assert boxes["0"]["points"] == "260,210 300,210 300,250 260,250"
    assert result["sourceUnchanged"] and result["inputUnchanged"]
    assert result["selected"] == 0


def test_stage_axis_alignment_is_separate_from_display_rotation_and_lock_gated():
    result = run("""
      const h=followHarness(),m=h.mounted;
      const accepted=m.setStageAlignment({angle:90,flipX:true,flipY:false});
      await m.startStageFollow();await m.toggleLock();
      const rejected=m.setStageAlignment({angle:0,flipX:false,flipY:true});
      Object.assign(h.snapshot,{seq:1,status:'following',displacement_um:[10,20]});await m.pollStageFollow();
      return {accepted,rejected,alignment:m.state.follow.alignment,center:api.camera(h.viewer).center,
        rotation:h.viewer.viewport.getRotation(),flip:h.viewer.viewport.getFlip(),
        disabled:h.element('stage-follow-angle').disabled,message:h.element('stage-follow-message').textContent};
    """)
    assert result["accepted"] is True and result["rejected"] is False
    assert result["alignment"] == {"angle": 90, "flipX": True, "flipY": False}
    assert result["center"] == pytest.approx({"x": .36, "y": .245})
    assert result["rotation"] == 90 and result["flip"] is True and result["disabled"] is True
    assert "alignment not verified" in result["message"]


def test_stage_pause_latches_and_stop_leaves_unlocked_manual_navigation_available():
    result = run("""
      const h=followHarness(),m=h.mounted;await m.startStageFollow();const before=api.camera(h.viewer);
      Object.assign(h.snapshot,{seq:4,status:'paused',displacement_um:[50,50],message:'Missing stage update.'});
      await m.pollStageFollow();
      const paused={status:h.element('stage-follow-status').textContent,message:h.element('stage-follow-message').textContent,
        timers:h.timers.size};
      Object.assign(h.snapshot,{seq:5,status:'following',displacement_um:[70,70]});await m.pollStageFollow();
      const calls=h.followCalls.slice();await m.stopStageFollow();await m.pollStageFollow();
      return {paused,calls,finalCalls:h.followCalls,enabled:m.state.follow.enabled,mouse:h.viewer.nav,
        unchanged:JSON.stringify(before)===JSON.stringify(api.camera(h.viewer)),timers:h.timers.size};
    """)
    assert result["paused"]["status"] == "Paused" and result["paused"]["timers"] == 0
    assert "off and on" in result["paused"]["message"]
    assert result["calls"] == ["start", "poll"]
    assert result["finalCalls"] == ["start", "poll", "stop"]
    assert result["unchanged"] and result["mouse"] and not result["enabled"] and result["timers"] == 0


def test_stage_poll_serializes_requests_and_ignores_late_response_after_stop():
    result = run("""
      let finish,polls=0;
      const h=followHarness({bridgeOverrides:{stage_follow_state(){polls++;return new Promise(resolve=>finish=resolve);}}}),m=h.mounted;
      await m.startStageFollow();const before=api.camera(h.viewer),poll=m.pollStageFollow();
      await m.pollStageFollow();await m.stopStageFollow();
      finish({enabled:true,session_id:'session-one',seq:1,status:'following',displacement_um:[10,20]});await poll;
      return {polls,unchanged:JSON.stringify(before)===JSON.stringify(api.camera(h.viewer)),
        timers:h.timers.size,enabled:m.state.follow.enabled};
    """)
    assert result == {"polls": 1, "unchanged": True, "timers": 0, "enabled": False}


def test_stage_close_during_start_orders_native_stop_and_discards_late_session():
    result = run("""
      let finish;const calls=[];
      const h=followHarness({bridgeOverrides:{start_stage_follow(){calls.push('start');return new Promise(resolve=>finish=resolve);},
        async stop_stage_follow(){calls.push('stop');return {ok:true};}}}),m=h.mounted;
      await m.pollSlacsDiscovery();
      const starting=m.startStageFollow();await Promise.resolve();await Promise.resolve();
      h.windowListeners.pagehide();finish({ok:true,session_id:'late-session'});await starting;
      for(let i=0;i<10;i++)await Promise.resolve();
      return {calls,enabled:m.state.follow.enabled,timers:h.timers.size};
    """)
    assert result == {"calls": ["start", "stop"], "enabled": False, "timers": 0}


def test_detection_never_hides_follow_and_ends_only_a_follow_bound_to_a_lost_process():
    result = run("""
      let process={available:false,session_id:null,status:'not_running'};
      const h=followHarness({bridgeOverrides:{async slacs_discovery_state(){return {...process};}}}),m=h.mounted;
      await m.pollSlacsDiscovery();
      const absent={hidden:h.element('stage-follow-control').hidden,started:await m.startStageFollow()};
      process={available:false,session_id:null,status:'error'};await m.pollSlacsDiscovery();
      for(let i=0;i<8;i++)await Promise.resolve();
      const unbound={enabled:m.state.follow.enabled,noticeHidden:h.element('stage-follow-disconnected').hidden};
      await m.stopStageFollow();
      process={available:true,session_id:'one',status:'running'};await m.pollSlacsDiscovery();
      const found={hidden:h.element('stage-follow-control').hidden,enabled:m.state.follow.enabled};
      await m.startStageFollow();
      process={available:false,session_id:null,status:'error'};await m.pollSlacsDiscovery();
      for(let i=0;i<8;i++)await Promise.resolve();
      const lost={hidden:h.element('stage-follow-control').hidden,notice:h.element('stage-follow-disconnected').textContent,
        noticeHidden:h.element('stage-follow-disconnected').hidden,enabled:m.state.follow.enabled,calls:h.followCalls.slice()};
      process={available:true,session_id:'two',status:'running'};await m.pollSlacsDiscovery();
      return {absent,unbound,found,lost,again:{hidden:h.element('stage-follow-control').hidden,
        enabled:m.state.follow.enabled,calls:h.followCalls.slice()}};
    """)
    assert result['absent'] == {'hidden': False, 'started': True}  # detection never gates Follow
    assert result['unbound'] == {'enabled': True, 'noticeHidden': True}  # not bound to a process
    assert result['found'] == {'hidden': False, 'enabled': False}
    assert result['lost']['hidden'] is False and not result['lost']['enabled']
    assert not result['lost']['noticeHidden'] and 'Follow stopped' in result['lost']['notice']
    assert result['lost']['calls'].count('stop') == 2  # the manual stop, then the loss
    assert result['again']['hidden'] is False and result['again']['enabled'] is False
    assert result['again']['calls'].count('start') == 2  # no automatic restart


def test_slacs_loss_during_pending_start_cancels_late_session():
    result = run("""
      let process={available:true,session_id:'one',status:'running'},finish;const calls=[];
      const h=followHarness({bridgeOverrides:{async slacs_discovery_state(){return {...process};},
        start_stage_follow(){calls.push('start');return new Promise(resolve=>finish=resolve);},
        async stop_stage_follow(){calls.push('stop');return {ok:true};}}}),m=h.mounted;
      await m.pollSlacsDiscovery();const starting=m.startStageFollow();
      for(let i=0;i<4;i++)await Promise.resolve();
      process={available:false,session_id:null,status:'not_running'};await m.pollSlacsDiscovery();
      finish({ok:true,session_id:'late'});await starting;
      for(let i=0;i<8;i++)await Promise.resolve();
      return {calls,enabled:m.state.follow.enabled,hidden:h.element('stage-follow-control').hidden};
    """)
    assert result == {'calls': ['start', 'stop'], 'enabled': False, 'hidden': False}


def test_slacs_invalid_or_failed_discovery_is_not_detection_and_keeps_follow_visible():
    result = run("""
      let reply={available:true,session_id:'one',status:'error'};
      const h=followHarness({bridgeOverrides:{async slacs_discovery_state(){return reply;}}}),m=h.mounted;
      await m.pollSlacsDiscovery();
      const malformed={hidden:h.element('stage-follow-control').hidden,detected:m.state.slacs.available};
      reply={available:true,session_id:'',status:'running'};await m.pollSlacsDiscovery();
      const missingId={hidden:h.element('stage-follow-control').hidden,detected:m.state.slacs.available};
      h.windowListeners.pagehide();
      return {malformed,missingId,intervals:h.intervals.size};
    """)
    assert result == {'malformed': {'hidden': False, 'detected': False},
                      'missingId': {'hidden': False, 'detected': False}, 'intervals': 0}


def test_slacs_discovery_timeout_is_not_detection_and_ignores_late_reply():
    result = run("""
      let reply,calls=0;
      const h=followHarness({bridgeOverrides:{slacs_discovery_state(){
        calls++;return calls===1?new Promise(resolve=>reply=resolve):
          Promise.resolve({available:true,session_id:'new',status:'running'});}}}),m=h.mounted;
      const first=m.pollSlacsDiscovery();
      [...h.timers.values()].find(timer=>timer.delay===3000).fn();await first;
      const hiddenAfterTimeout=h.element('stage-follow-control').hidden;
      await m.pollSlacsDiscovery();
      reply({available:true,session_id:'old',status:'running'});
      for(let i=0;i<5;i++)await Promise.resolve();
      return {hiddenAfterTimeout,session:m.state.slacs.session_id,hidden:h.element('stage-follow-control').hidden,calls};
    """)
    assert result == {'hiddenAfterTimeout': False, 'session': 'new', 'hidden': False, 'calls': 2}


LATE_FOLLOW = """
  let process={available:true,session_id:'one',status:'running'};const replies=[];
  const h=followHarness({bridgeOverrides:{async slacs_discovery_state(){return {...process};},
    stage_follow_state(){return new Promise(resolve=>replies.push(resolve));}}}),m=h.mounted;
  await m.pollSlacsDiscovery();await m.startStageFollow();
  const lost='SLACS program stopped or changed. Follow stopped; reconnect manually.';
  const reply=(seq,dx,dy)=>({enabled:true,session_id:'session-one',seq,status:'following',displacement_um:[dx,dy]});
  const settle=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
  const camera=()=>JSON.stringify(api.camera(h.viewer));
  const pollTimers=()=>[...h.timers.values()].filter(timer=>timer.delay===50).length;
"""


def test_follow_reply_computed_before_loss_is_dropped_once_the_revocation_arrives():
    """Reviewer's interleaving: the reply was computed before the backend saw the loss."""
    result = run(LATE_FOLLOW + """
      const before=camera(),pending=m.pollStageFollow();
      process={available:false,session_id:null,status:'not_running'};
      const revoked=h.win.nd2TranslucentRevokeFollow({session_id:'session-one',message:lost});
      replies[0](reply(1,10,20));await pending;await settle();
      const afterReply=camera();
      await m.pollSlacsDiscovery();await settle();
      return {revoked,movedByLateReply:afterReply!==before,movedLater:camera()!==before,
        enabled:m.state.follow.enabled,status:m.state.follow.status,pollTimers:pollTimers(),
        notice:h.element('stage-follow-disconnected').textContent,
        noticeHidden:h.element('stage-follow-disconnected').hidden,calls:h.followCalls};
    """)
    assert result == {"revoked": True, "movedByLateReply": False, "movedLater": False,
                      "enabled": False, "status": "paused", "pollTimers": 0,
                      "notice": "SLACS program stopped or changed. Follow stopped; reconnect manually.",
                      "noticeHidden": False, "calls": ["start", "stop"]}


def test_reply_delivered_before_the_revocation_moves_once_and_nothing_after_it_does():
    """The boundary: a reply that reaches the page first carries motion from before the loss."""
    result = run(LATE_FOLLOW + """
      const before=camera(),first=m.pollStageFollow();
      replies[0](reply(1,10,20));await first;await settle();
      const afterFirst=camera();
      const second=[...h.timers.values()].find(timer=>timer.delay===50).fn();
      await settle();
      h.win.nd2TranslucentRevokeFollow({session_id:'session-one',message:lost});
      replies[1](reply(2,30,40));await second;await settle();
      return {firstMoved:afterFirst!==before,movedAfterRevocation:camera()!==afterFirst,
        polls:replies.length,enabled:m.state.follow.enabled,pollTimers:pollTimers()};
    """)
    assert result == {"firstMoved": True, "movedAfterRevocation": False, "polls": 2,
                      "enabled": False, "pollTimers": 0}


def test_revocation_for_an_older_receiver_leaves_the_current_follow_running():
    result = run(LATE_FOLLOW + """
      const ignored=h.win.nd2TranslucentRevokeFollow({session_id:'session-zero',message:lost});
      const before=camera(),pending=m.pollStageFollow();
      replies[0](reply(1,10,20));await pending;await settle();
      return {ignored,moved:camera()!==before,enabled:m.state.follow.enabled,
        status:m.state.follow.status,noticeHidden:h.element('stage-follow-disconnected').hidden,calls:h.followCalls};
    """)
    assert result == {"ignored": False, "moved": True, "enabled": True, "status": "following",
                      "noticeHidden": True, "calls": ["start"]}


def test_revocation_during_a_pending_start_discards_the_late_session():
    result = run("""
      let finish;const calls=[];
      const h=followHarness({bridgeOverrides:{start_stage_follow(){calls.push('start');return new Promise(resolve=>finish=resolve);},
        async stop_stage_follow(){calls.push('stop');return {ok:true};}}}),m=h.mounted;
      await m.pollSlacsDiscovery();
      const starting=m.startStageFollow();for(let i=0;i<4;i++)await Promise.resolve();
      const revoked=h.win.nd2TranslucentRevokeFollow({session_id:'session-one',message:'lost'});
      finish({ok:true,session_id:'session-one'});await starting;
      for(let i=0;i<10;i++)await Promise.resolve();
      return {revoked,calls,enabled:m.state.follow.enabled,status:m.state.follow.status,
        pollTimers:[...h.timers.values()].filter(timer=>timer.delay===50).length};
    """)
    assert result == {"revoked": True, "calls": ["start", "stop"], "enabled": False,
                      "status": "paused", "pollTimers": 0}


def test_revocation_forgets_detection_until_a_fresh_discovery_reply():
    result = run("""
      let process={available:true,session_id:'one',status:'running'},stale;let discoveryCalls=0;
      const h=followHarness({bridgeOverrides:{slacs_discovery_state(){discoveryCalls++;
        if(discoveryCalls===2)return new Promise(resolve=>stale=resolve);return Promise.resolve({...process});}}}),m=h.mounted;
      await m.pollSlacsDiscovery();await m.startStageFollow();
      const inFlight=m.pollSlacsDiscovery();  // discovery reply computed before the loss, still on its way
      process={available:true,session_id:'two',status:'running'};
      h.win.nd2TranslucentRevokeFollow({session_id:'session-one',message:'lost'});
      const right={hidden:h.element('stage-follow-control').hidden,button:h.element('stage-follow').disabled};
      for(let i=0;i<10;i++)await Promise.resolve();
      stale({available:true,session_id:'one',status:'running'});await inFlight;
      for(let i=0;i<10;i++)await Promise.resolve();
      return {right,session:m.state.slacs.session_id,hidden:h.element('stage-follow-control').hidden,
        enabled:m.state.follow.enabled,stops:h.followCalls.filter(call=>call==='stop').length,discoveryCalls};
    """)
    assert result == {"right": {"hidden": False, "button": True}, "session": "two", "hidden": False,
                      "enabled": False, "stops": 1, "discoveryCalls": 3}


def test_revocation_without_an_active_follow_or_after_close_changes_nothing():
    result = run("""
      const h=followHarness(),m=h.mounted;await m.pollSlacsDiscovery();
      const idle=h.win.nd2TranslucentRevokeFollow({session_id:'session-one',message:'lost'});
      const idleState={status:m.state.follow.status,noticeHidden:h.element('stage-follow-disconnected').hidden};
      await m.startStageFollow();h.windowListeners.pagehide();
      for(let i=0;i<10;i++)await Promise.resolve();
      const calls=h.followCalls.slice();
      const closed=h.win.nd2TranslucentRevokeFollow({session_id:'session-one',message:'lost'});
      return {idle,idleState,closed,callsAfterClose:h.followCalls.length===calls.length};
    """)
    assert result == {"idle": False, "idleState": {"status": "off", "noticeHidden": True},
                      "closed": False, "callsAfterClose": True}


@pytest.mark.parametrize("failure", ["session", "rejection", "watchdog"])
def test_stage_connection_failures_pause_without_any_pan(failure):
    result = run(f"""
      let finish;const failure={json.dumps(failure)};
      const h=followHarness({{bridgeOverrides:{{stage_follow_state(){{
        if(failure==='rejection')return Promise.reject(new Error('connection lost'));
        if(failure==='watchdog')return new Promise(resolve=>finish=resolve);
        return Promise.resolve({{enabled:true,session_id:'different',seq:1,status:'following',displacement_um:[10,20]}});
      }}}}}}),m=h.mounted;
      await m.startStageFollow();const before=api.camera(h.viewer),poll=m.pollStageFollow();
      if(failure==='watchdog'){{
        [...h.timers.values()].find(timer=>timer.delay===3000).fn();
        finish({{enabled:true,session_id:'session-one',seq:1,status:'following',displacement_um:[10,20]}});
      }}
      await poll;
      return {{status:m.state.follow.status,unchanged:JSON.stringify(before)===JSON.stringify(api.camera(h.viewer)),timers:h.timers.size}};
    """)
    assert result == {"status": "paused", "unchanged": True, "timers": 0}


@pytest.mark.parametrize("pixel_size", [None, [], [1], [0, 1], [1, -1], ["1", 1]])
def test_stage_follow_requires_positive_numeric_y_x_pixel_size(pixel_size):
    result = run(f"""
      const h=followHarness({{infoOverrides:{{pixelSizeUm:{json.dumps(pixel_size)}}}}});
      return {{accepted:await h.mounted.startStageFollow(),disabled:h.element('stage-follow').disabled,
        calls:h.followCalls,message:h.element('stage-follow-message').textContent}};
    """)
    assert result == {"accepted": False, "disabled": True, "calls": [],
                      "message": "A calibrated pixel size is needed."}


def test_stage_settings_editors_keep_keyboard_input_without_roi_or_window_shortcuts():
    result = run("""
      const h=followHarness({initialContext:{rois:[{x:100,y:100,w:4,h:4},
        {x:200,y:100,w:4,h:4},{x:300,y:100,w:4,h:4}],roiIndex:1}}),m=h.mounted;
      const before=api.camera(h.viewer);let prevented=0,editor;
      for(const tagName of ['INPUT','SELECT','TEXTAREA','DIV']){
        editor={tagName,isContentEditable:tagName==='DIV',closest(selector){
          return selector.includes('#stage-follow-settings')?h.element('stage-follow-settings'):null;}};
        for(const key of ['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','l','o']){
          h.documentListeners.keydown({key,target:editor,ctrlKey:key==='o',preventDefault(){prevented++;}});
        }
      }
      const editing={prevented,native:h.nativeCalls.slice(),roi:m.state.roiIndex,locked:m.state.locked,
        cameraUnchanged:JSON.stringify(before)===JSON.stringify(api.camera(h.viewer))};
      h.documentListeners.keydown({key:'Escape',target:editor,preventDefault(){prevented++;}});
      await Promise.resolve();
      return {editing,prevented,native:h.nativeCalls};
    """)
    assert result == {"editing": {"prevented": 0, "native": [], "roi": 1,
                                  "locked": False, "cameraUnchanged": True},
                      "prevented": 1, "native": [["close"]]}


@pytest.mark.parametrize("source_flip,mirror,effective", [
    (False, "undefined", True), (True, "undefined", False),
    (False, "true", True), (True, "true", False),
    (False, "false", False), (True, "false", True),
])
def test_translucent_defaults_to_extra_display_mirror_composed_with_source_flip(source_flip, mirror, effective):
    result = run(f"""
      const original={{...context,flip:{str(source_flip).lower()},mirror_scan:{mirror}}};
      const before=JSON.stringify(original),h=harness({{initialContext:original}});
      return {{mirror:h.mounted.state.mirrorScan,checked:h.element('overlay-mirror').checked,
        effective:h.viewer.viewport.getFlip(),sourceUnchanged:before===JSON.stringify(original),
        camera:api.camera(h.viewer),rotation:h.viewer.viewport.getRotation()}};
    """)
    assert result["mirror"] is (mirror != "false")
    assert result["checked"] is result["mirror"]
    assert result["effective"] is effective
    assert result["sourceUnchanged"] is True
    assert result["camera"] == {"center": {"x": .4, "y": .25}, "zoom": 2}
    assert result["rotation"] == 90


def test_mirror_checkbox_reflects_existing_roi_display_without_changing_source_or_camera_and_respects_lock():
    result = run("""
      const rois=[{x:440,y:240,w:20,h:10}];
      const h=followHarness({initialContext:{mirror_scan:undefined,rotation:30,flip:false,rois,roiIndex:0}}),m=h.mounted;
      const points=()=>h.element('roi-overlay').children[0].attributes.points.split(' ').map(p=>p.split(',').map(Number));
      const before={points:points(),camera:api.camera(h.viewer),source:JSON.stringify(m.state.rois)};
      h.element('overlay-mirror').checked=false;
      const accepted=h.element('overlay-mirror').listeners.change(),after=points();
      await m.toggleLock();h.element('overlay-mirror').checked=true;
      const rejected=h.element('overlay-mirror').listeners.change();
      return {accepted,rejected,before,after,camera:api.camera(h.viewer),
        sourceUnchanged:before.source===JSON.stringify(m.state.rois),inputUnchanged:rois,
        mirror:m.state.mirrorScan,checked:h.element('overlay-mirror').checked,
        disabled:h.element('overlay-mirror').disabled,rotation:h.viewer.viewport.getRotation(),
        opacity:m.state.opacity,lockedPoints:points()};
    """)
    assert result["accepted"] is True and result["rejected"] is False
    for before, after in zip(result["before"]["points"], result["after"], strict=True):
        assert after == pytest.approx([800 - before[0], before[1]])
    assert result["camera"] == result["before"]["camera"]
    assert result["sourceUnchanged"] and result["lockedPoints"] == result["after"]
    assert result["inputUnchanged"] == [{"x": 440, "y": 240, "w": 20, "h": 10}]
    assert result["mirror"] is False and result["checked"] is False and result["disabled"] is True
    assert result["rotation"] == 30 and result["opacity"] == .55


def test_display_mirror_does_not_reverse_or_modify_physical_stage_registration():
    result = run("""
      const h=followHarness({initialContext:{mirror_scan:undefined,flip:false}}),m=h.mounted;
      await m.startStageFollow();
      Object.assign(h.snapshot,{seq:1,status:'following',displacement_um:[10,20]});await m.pollStageFollow();
      const mirrored={camera:api.camera(h.viewer),delta:{...m.state.follow.lastDelta},flip:h.viewer.viewport.getFlip()};
      m.setMirrorScan(false);
      Object.assign(h.snapshot,{seq:2,displacement_um:[20,40]});await m.pollStageFollow();
      return {mirrored,after:{camera:api.camera(h.viewer),delta:m.state.follow.lastDelta,flip:h.viewer.viewport.getFlip()},
        alignment:m.state.follow.alignment};
    """)
    assert result["mirrored"]["delta"] == result["after"]["delta"] == {"x": 20, "y": 10}
    assert result["mirrored"]["camera"]["center"] == pytest.approx({"x": .42, "y": .26})
    assert result["after"]["camera"]["center"] == pytest.approx({"x": .44, "y": .27})
    assert result["mirrored"]["flip"] is True and result["after"]["flip"] is False
    assert result["alignment"] == {"angle": 0, "flipX": False, "flipY": False}


def test_step_input_defaults_to_ten_and_is_shown_in_the_settings():
    result = run("""
      const h=followHarness(),m=h.mounted;
      return {step:m.state.stepUm,value:h.element('stage-follow-step').value,
        count:h.element('stage-follow-count').textContent};
    """)
    assert result == {"step": 10, "value": "10", "count": "Key presses: 0"}


def test_step_change_is_clamped_and_remembered_in_local_storage():
    result = run("""
      const h=followHarness(),m=h.mounted,input=h.element('stage-follow-step');
      input.value='25';const accepted=input.listeners.change();
      const stored=h.storage.get('nd2wsi.stageFollow.stepUm');
      input.value='0';const rejectedLow=input.listeners.change();
      input.value='99999';const rejectedHigh=input.listeners.change();
      return {accepted,stored,step:m.state.stepUm,value:input.value,rejectedLow,rejectedHigh};
    """)
    assert result == {"accepted": True, "stored": "25", "step": 25, "value": "25",
                      "rejectedLow": False, "rejectedHigh": False}


def test_step_input_is_disabled_while_following_and_passed_to_start():
    result = run("""
      let startArgs=null;
      const h=followHarness({bridgeOverrides:{async start_stage_follow(opts){startArgs=opts;
        return {ok:true,session_id:'session-one',token:'never',url:'http://127.0.0.1:1/motion',protocol:'test'};}}});
      const m=h.mounted,input=h.element('stage-follow-step');
      m.setStepUm(2.5);
      const beforeDisabled=input.disabled;
      await m.startStageFollow();
      const afterDisabled=input.disabled;
      const rejectedWhileFollowing=m.setStepUm(7);
      return {startArgs,beforeDisabled,afterDisabled,rejectedWhileFollowing,step:m.state.stepUm};
    """)
    assert result["startArgs"] == {"source": "keys", "step_um": 2.5}
    assert result["beforeDisabled"] is False and result["afterDisabled"] is True
    assert result["rejectedWhileFollowing"] is False and result["step"] == 2.5


def test_waiting_and_following_status_texts_mention_slacs_keys():
    result = run("""
      const h=followHarness(),m=h.mounted;
      await m.startStageFollow();
      const waiting=h.element('stage-follow-status').textContent;
      Object.assign(h.snapshot,{seq:1,status:'following',displacement_um:[1,2],keys:{counted:4,slacs_in_front:true}});
      await m.pollStageFollow();
      return {waiting,following:h.element('stage-follow-status').textContent,
        count:h.element('stage-follow-count').textContent};
    """)
    assert result == {"waiting": "Waiting for W/A/S/D in SLACS",
                      "following": "Following SLACS keys", "count": "Key presses: 4"}


def test_status_shows_slacs_not_in_front_without_pausing():
    result = run("""
      const h=followHarness(),m=h.mounted;
      await m.startStageFollow();
      Object.assign(h.snapshot,{seq:0,status:'waiting',displacement_um:[0,0],keys:{counted:0,slacs_in_front:false}});
      await m.pollStageFollow();
      return {status:h.element('stage-follow-status').textContent,
        data:h.element('stage-follow-status').dataset.status,
        followStatus:m.state.follow.status,timers:h.timers.size};
    """)
    # "Not in front" is a status hint only; following is not paused and keeps polling.
    assert result["status"] == "SLACS not in front"
    assert result["data"] == "not-in-front" and result["followStatus"] == "waiting"
    assert result["timers"] == 1


def test_pause_after_slacs_not_in_front_shows_paused():
    result = run("""
      const h=followHarness(),m=h.mounted;
      await m.startStageFollow();
      Object.assign(h.snapshot,{seq:0,status:'waiting',displacement_um:[0,0],keys:{counted:0,slacs_in_front:false}});
      await m.pollStageFollow();
      const before=h.element('stage-follow-status').textContent;
      Object.assign(h.snapshot,{status:'paused',message:'Stage observations stopped.'});
      await m.pollStageFollow();
      return {before,status:h.element('stage-follow-status').textContent,
        data:h.element('stage-follow-status').dataset.status,
        followStatus:m.state.follow.status,timers:h.timers.size};
    """)
    # Polling stops on a pause, so a stale "not in front" hint must not mask it.
    assert result =={"before": "SLACS not in front", "status": "Paused", "data": "paused",
                      "followStatus": "paused", "timers": 0}


def test_client_errors_are_forwarded_rate_limited_and_never_throw():
    result = run("""
      const logged=[];
      const h=followHarness({bridgeOverrides:{log_client_event(payload){logged.push(payload);return {ok:true};}}}),m=h.mounted;
      h.windowListeners.error({message:'boom',filename:'a.js',lineno:1,colno:2,error:{stack:'trace'}});
      h.windowListeners.unhandledrejection({reason:{message:'rejected',stack:'rstack'}});
      for(let i=0;i<40;i++)h.windowListeners.error({message:'flood'});
      return {first:logged[0],second:logged[1],total:logged.length};
    """)
    assert result["first"] == {"source": "translucent", "kind": "error", "message": "boom",
                               "where": "a.js:1:2", "stack": "trace"}
    assert result["second"]["kind"] == "unhandledrejection" and result["second"]["message"] == "rejected"
    assert result["total"] == 20  # rate limited to 20 per window


def test_missing_client_logger_is_tolerated():
    result = run("""
      const h=followHarness(),m=h.mounted;
      let threw=false;
      try{h.windowListeners.error({message:'boom'});}catch(_){threw=true;}
      return {threw};
    """)
    assert result == {"threw": False}
