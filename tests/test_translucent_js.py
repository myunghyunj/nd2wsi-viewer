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
function harness({failLock=false,failOpacity=false,opacityResult,initialContext={},bridgeOverrides={},
  infoOverrides={},withBackdrop=false,withWorld=false,winOverrides={},docOverrides={},autoOpen=true}={}){
  const elements=new Map();
  function newElement(id){return {id,style:{},dataset:{},attributes:{},children:[],
    listeners:{},clientWidth:800,clientHeight:500,disabled:false,hidden:false,
    addEventListener(name,cb){this.listeners[name]=cb;},
    setAttribute(name,value){this.attributes[name]=value;},
    replaceChildren(...children){this.children=children;},
    appendChild(child){this.children=this.children.filter(item=>item!==child);this.children.push(child);return child;},
    get lastElementChild(){return this.children[this.children.length-1];}};}
  function element(id){if(!elements.has(id))elements.set(id,newElement(id));return elements.get(id);}
  const backdropLog={draws:[],clears:[]};
  if(withBackdrop){
    const bd=element('translucent-backdrop');
    bd.hidden=true;bd.width=0;bd.height=0;
    const ctx={
      clearRect(x,y,w,h){backdropLog.clears.push([x,y,w,h]);},
      drawImage(src,x,y){backdropLog.draws.push({width:src.width,height:src.height,x,y});},
    };
    bd.getContext=kind=>kind==='2d'?ctx:null;
  }
  const documentListeners={};
  const doc={getElementById:element,createElementNS(namespace,name){return newElement(name);},
    querySelectorAll(){return [];},addEventListener(name,cb){documentListeners[name]=cb;},...docOverrides};
  const winListeners={};
  const win={innerWidth:800,innerHeight:500,addEventListener(name,cb){winListeners[name]=cb;},...winOverrides};
  const viewer=fakeViewer();
  let item=null;
  const loaderClears=[];
  if(withBackdrop){
    viewer.drawer={canvas:{width:800,height:500}};
  }
  if(withWorld){
    viewer.imageLoader={clear(){loaderClears.push(true);}};
    item={
      viewer,
      tilesMatrix:{},
      resets:0,
      fullyLoaded:true,
      drawArea:{x:0,y:0,width:800,height:500},
      clip:null,
      reset(){this.resets+=1;},
      getDrawArea(){return this.drawArea;},
      getFullyLoaded(){return this.fullyLoaded;},
      _setFullyLoaded(v){this.fullyLoaded=Boolean(v);},
      getClip(){return this.clip;},
      setClip(c){this.clip=c;},
    };
    viewer.world={getItemAt(i){return i===0?item:null;}};
  }
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
  const mounted=api.mount({window:win,document:doc,api:bridge,OSD,
    context:{...context,...initialContext},info:{...info,...infoOverrides},importer});
  if(autoOpen)viewer.handlers.open();
  return {mounted,viewer,element,win,winListeners,nativeCalls,osdOptions,documentListeners,backdropLog,item,loaderClears};
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
        issues:h.element('roi-import-issues').textContent,review:m.state.importReview,
        unchanged:JSON.stringify(before)===JSON.stringify({camera:api.camera(h.viewer),rois:m.state.rois})};
    """)
    assert result["accepted"] is False and result["unchanged"]
    assert result["name"] == "valid.txt" and result["label"] == "1/1"
    assert "INDEX 199 has zero area (4877, 8248) → (4876, 8247)" in result["issues"]
    assert result["review"]["skippedCount"] == 126
    assert "first INDEX 58" in result["issues"] and "first INDEX 182" in result["issues"]
    assert "aspect ratios" in result["error"]


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


def test_partial_txt_import_keeps_problem_rows_visible_after_navigation_without_mutating_input():
    result = run(r"""
      const h=harness(),m=h.mounted;
      const input={ok:true,name:'mixed.txt',coordinateSize:{width:1000,height:500},
        text:'INDEX\tX1\tY1\tX2\tY2\tNOTE\n7\t10\t20\t10\t20\t정상\n41\t50\t43\t50\t42\t<img src=x>\n93\t90\t80\t91\t82\tlast'};
      const before=JSON.stringify(input),accepted=m.setRois(input);
      const summary=h.element('roi-import-summary').textContent,issues=h.element('roi-import-issues').textContent;
      m.selectRoi(1);
      return {accepted,rois:m.state.rois,summary,issues,
        after:h.element('roi-import-summary').textContent,hidden:h.element('roi-import-review').hidden,
        repairHidden:h.element('roi-import-repair').hidden,unchanged:before===JSON.stringify(input)};
    """)
    assert result["accepted"] is True and result["unchanged"] is True
    assert [roi["index"] for roi in result["rois"]] == [7, 93]
    assert result["rois"][0]["label"] == "정상"
    assert result["summary"] == result["after"] == "Imported 2 ROIs · 1 skipped · 0 repaired"
    assert "Line 3" in result["issues"] and "INDEX 41" in result["issues"]
    assert "41\t50\t43\t50\t42\t<img src=x>" in result["issues"]
    assert result["hidden"] is False and result["repairHidden"] is False


def test_explicit_tracing_repair_preserves_original_txt_and_selected_roi_without_moving_camera():
    result = run(r"""
      const h=harness(),m=h.mounted;
      const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n41\t50\t43\t50\t42\tpoint\n93\t90\t80\t91\t82\tlast';
      m.setRois({ok:true,name:'mixed.txt',coordinateSize:{width:1000,height:500},text});
      const before=api.camera(h.viewer),selected=m.state.rois[m.state.roiIndex].index;
      const repaired=h.element('roi-import-repair').listeners.click();
      return {repaired,rois:m.state.rois,selected,after:m.state.rois[m.state.roiIndex].index,
        cameraUnchanged:JSON.stringify(before)===JSON.stringify(api.camera(h.viewer)),
        unchanged:m.state.importResult.text===text,summary:h.element('roi-import-summary').textContent,
        issues:h.element('roi-import-issues').textContent,hidden:h.element('roi-import-repair').hidden};
    """)
    assert result["repaired"] and result["cameraUnchanged"] and result["unchanged"]
    assert result["selected"] == result["after"] == 93
    assert result["rois"][0] == {"x": 50, "y": 43, "w": 1, "h": 1,
                                  "index": 41, "label": "point"}
    assert result["summary"] == "Imported 2 ROIs · 0 skipped · 1 repaired"
    assert "Repaired:" in result["issues"] and "41\t50\t43\t50\t42\tpoint" in result["issues"]
    assert result["hidden"] is True


def test_new_header_invalid_txt_clears_old_repair_action_but_preserves_current_rois():
    result = run(r"""
      const h=harness(),m=h.mounted;
      m.setRois({ok:true,name:'mixed.txt',coordinateSize:{width:1000,height:500},
        text:'INDEX\tX1\tY1\tX2\tY2\tNOTE\n41\t50\t43\t50\t42\tpoint\n93\t90\t80\t91\t82\tlast'});
      const before=JSON.stringify(m.state.rois),camera=JSON.stringify(api.camera(h.viewer));
      const accepted=m.setRois({ok:true,name:'bad-header.txt',coordinateSize:{width:1000,height:500},text:'not an ROI header'});
      return {accepted,unchanged:before===JSON.stringify(m.state.rois),
        cameraUnchanged:camera===JSON.stringify(api.camera(h.viewer)),
        result:m.state.importResult,review:m.state.importReview,
        hidden:h.element('roi-import-review').hidden,repairHidden:h.element('roi-import-repair').hidden,
        disabled:h.element('roi-import-repair').disabled,repair:m.repairRois(),
        error:h.element('overlay-status').textContent};
    """)
    assert result["accepted"] is False and result["unchanged"] and result["cameraUnchanged"]
    assert result["result"] is None and result["review"] is None
    assert result["hidden"] and result["repairHidden"] and result["disabled"]
    assert result["repair"] is False and "header" in result["error"]


def test_all_invalid_tracing_import_preserves_current_rois_and_repair_is_blocked_while_locked():
    result = run(r"""
      const h=harness({initialContext:{rois:[{x:10,y:10,w:1,h:1,index:99}],roiIndex:0}}),m=h.mounted;
      const before=api.camera(h.viewer),rois=JSON.stringify(m.state.rois);
      const accepted=m.setRois({ok:true,name:'empty.txt',coordinateSize:{width:1000,height:500},
        text:'INDEX\tX1\tY1\tX2\tY2\tNOTE\n41\t50\t43\t50\t42\tpoint'});
      const retained=rois===JSON.stringify(m.state.rois),cameraUnchanged=JSON.stringify(before)===JSON.stringify(api.camera(h.viewer));
      await m.toggleLock();
      const disabled=h.element('roi-import-repair').disabled,blocked=m.repairRois();
      await m.toggleLock();
      const repaired=m.repairRois();
      return {accepted,retained,cameraUnchanged,disabled,blocked,repaired,rois:m.state.rois,
        summary:h.element('roi-import-summary').textContent};
    """)
    assert result["accepted"] is False and result["retained"] and result["cameraUnchanged"]
    assert result["disabled"] is True and result["blocked"] is False and result["repaired"] is True
    assert result["rois"] == [{"x": 50, "y": 43, "w": 1, "h": 1, "index": 41, "label": "point"}]
    assert result["summary"] == "Imported 1 ROIs · 0 skipped · 1 repaired"


def test_repaired_tracing_coordinates_still_require_matching_aspect_ratio():
    result = run(r"""
      const h=harness({initialContext:{rois:[{x:10,y:10,w:1,h:1,index:99}],roiIndex:0}}),m=h.mounted;
      const before=JSON.stringify(m.state.rois),camera=JSON.stringify(api.camera(h.viewer));
      m.setRois({ok:true,name:'wrong.txt',coordinateSize:{width:1000,height:1000},
        text:'INDEX\tX1\tY1\tX2\tY2\tNOTE\n41\t50\t43\t50\t42\tpoint'});
      const repaired=m.repairRois();
      return {repaired,unchanged:before===JSON.stringify(m.state.rois),
        cameraUnchanged:camera===JSON.stringify(api.camera(h.viewer)),error:h.element('overlay-status').textContent};
    """)
    assert result["repaired"] is False and result["unchanged"] and result["cameraUnchanged"]
    assert "aspect ratios" in result["error"]


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


# Every entry of the bundled OpenSeadragon key table: (keyCode, Shift).
OSD_KEYS = [(38, False), (38, True), (40, False), (40, True), (37, False), (39, False),
            (187, False), (189, False), (48, False), (87, False), (87, True), (83, False),
            (83, True), (65, False), (68, False), (82, False), (82, True), (70, False),
            (74, False), (75, False)]


@pytest.mark.parametrize(("key_code", "shift"), OSD_KEYS,
                         ids=[f"{code}{'-shift' if shift else ''}" for code, shift in OSD_KEYS])
def test_openseadragon_keys_cannot_rotate_flip_pan_or_zoom_the_overlay(key_code, shift):
    # Drive the bundled OSD keyboard action itself: without the tracing
    # window's handler the key moves the view, with it the key does nothing.
    result = run(r"""
      const fs=require('fs'),vm=require('vm'),path=require('path');
      const vendor=fs.readFileSync(path.join(path.dirname(process.argv[1]),
        'vendor/openseadragon/openseadragon.min.js'),'utf8');
      const at=vendor.indexOf('this.raiseEvent("canvas-key",');
      if(at<0)throw Error('Bundled OSD keyboard structure changed; inspect the real handler');
      const action=vm.runInNewContext('('+vendor.slice(vendor.lastIndexOf('function ',at),
        vendor.indexOf('function ',at))+')',{m:{Point:function(x,y){this.x=x;this.y=y;}}});
      const h=harness(),v=h.viewer,acted=[];
      Object.assign(v,{panHorizontal:true,panVertical:true,pixelsPerArrowPress:40,rotationIncrement:90,
        raiseEvent(name,args){if(v.handlers[name])v.handlers[name](args);},
        goToPreviousPage(){acted.push('page');},goToNextPage(){acted.push('page');}});
      Object.assign(v.viewport,{flipped:false,applyConstraints(){},deltaPointsFromPixels:point=>point,
        panBy(){acted.push('pan');},zoomBy(){acted.push('zoom');},goHome(){acted.push('home');},
        toggleFlip(){acted.push('flip');}});
      const press=()=>{
        const calls=v.calls.length,count=acted.length;
        action.call(v,{keyCode:KEY_CODE,shift:SHIFT,ctrl:false,alt:false,meta:false,originalEvent:{}});
        return acted.slice(count).concat(v.calls.slice(calls).map(call=>call[0]));
      };
      const blocked=press(),handler=v.handlers['canvas-key'];
      delete v.handlers['canvas-key'];
      return {registered:typeof handler,blocked,unblocked:press()};
    """.replace("KEY_CODE", str(key_code)).replace("SHIFT", json.dumps(shift)))
    assert result["registered"] == "function"
    assert result["blocked"] == []
    assert result["unblocked"], "the key should act in OpenSeadragon without the handler"


def test_translucent_html_and_css_define_backdrop_canvas_inside_stage():
    static = MODULE.parent
    html = (static / "translucent.html").read_text(encoding="utf-8")
    css = (static / "translucent-v1.css").read_text(encoding="utf-8")

    stage_start = html.index('<div id="translucent-stage"')
    stage_end = html.index("</div>", stage_start)
    stage_inner = html[stage_start:stage_end]
    backdrop_tag = '<canvas id="translucent-backdrop" aria-hidden="true" hidden></canvas>'
    assert backdrop_tag in stage_inner
    assert stage_inner.index(backdrop_tag) == stage_inner.index(">") + 1
    assert html.index('id="translucent-backdrop"') < html.index('id="roi-overlay"')

    assert "#translucent-backdrop { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 0; }" in css
    assert "#translucent-stage .openseadragon-container { position: relative; z-index: 1; }" in css
    assert "isolation: isolate;" in css


def test_dynamic_make_tile_source_and_high_channel_validation_with_fallback():
    result = run("""
      let active = 'c=0&g=original';
      const dyn = api.makeTileSource(info, { ...context, query: '?c=0&g=original' }, { Point }, () => active);
      const urlA = dyn.getTileUrl(0, 1, 0);
      active = 'c=1&win=10%3A200&g=original';
      const urlB = dyn.getTileUrl(0, 1, 0);
      active = '';
      const urlEmpty = dyn.getTileUrl(0, 1, 0);

      // >32 channel col passes validateContext (no hardcoded 32 cap) and normalizes without leading '?'.
      const col40 = Array.from({ length: 40 }, (_, i) => i % 2 === 0 ? '00ff88' : '').join(',');
      const ctx40 = api.validateContext({ ...context, query: '?col=' + encodeURIComponent(col40) + '&g=original' }, context.slide_url);
      const info40 = { ...info, channels: Array.from({ length: 40 }, (_, i) => ({ name: 'C' + i })) };
      const src40 = api.makeTileSource(info40, ctx40, { Point });
      const merged40 = api.validateMergedQuery('?col=' + encodeURIComponent(col40) + '&g=original&t=2&p=4&z=1', context.query, info40);
      const defaultsRestored = api.validateMergedQuery('?g=original&t=2&p=4&z=1', context.query, info40);

      // Minimal info without channels array falls back to syntax-only validation.
      const fallback40 = api.validateMergedQuery('col=' + encodeURIComponent(col40) + '&g=original&t=2&p=4&z=1', context.query, info);

      const col41 = Array.from({ length: 41 }, () => 'ff00aa').join(',');
      const info2 = { ...info, channels: [{ name: 'A' }, { name: 'B' }] };
      const rejections = [];
      for (const op of [
        () => api.validateContext({ ...context, query: 'c=' + '0,'.repeat(8200) }, context.slide_url),
        () => api.validateContext({ ...context, query: 'col=%23ff00aa' }, context.slide_url),
        () => api.makeTileSource(info40, { ...context, query: 'col=' + encodeURIComponent(col41) + '&g=original' }, { Point }),
        () => api.makeTileSource(info2, { ...context, query: 'win=0%3A10%2C0%3A20%2C0%3A30&g=original' }, { Point }),
        () => api.validateMergedQuery('c=0&g=changed&t=2&p=4&z=1', context.query, info2),
        () => api.validateMergedQuery('c=0&g=original&t=2&p=9&z=1', context.query, info2),
        () => api.validateMergedQuery('c=0&g=original&t=2&z=1', context.query, info2),
        () => api.validateMergedQuery('c=0&extra=1&g=original&t=2&p=4&z=1', context.query, info2),
        () => api.validateMergedQuery('col=ff00aa,00ff88,0000ff&g=original&t=2&p=4&z=1', context.query, info2),
        () => api.validateMergedQuery(null, context.query, info2),
      ]) {
        try { op(); rejections.push(null); } catch (e) { rejections.push(e.message); }
      }
      return {
        urlA, urlB, urlEmpty,
        ctx40QueryStartsWithQ: ctx40.query.startsWith('?'),
        src40UrlHasCol: src40.getTileUrl(0, 0, 0).includes('col='),
        merged40HasCol: merged40.includes('col='),
        defaultsRestored,
        fallback40HasCol: fallback40.includes('col='),
        rejections,
      };
    """)
    assert result["urlA"].endswith("/api/tile/1/1/0.jpg?c=0&g=original")
    assert result["urlB"].endswith("/api/tile/1/1/0.jpg?c=1&win=10%3A200&g=original")
    assert result["urlEmpty"].endswith("/api/tile/1/1/0.jpg")
    assert result["ctx40QueryStartsWithQ"] is False
    assert result["src40UrlHasCol"] is True
    assert result["merged40HasCol"] is True
    assert result["defaultsRestored"] == "g=original&t=2&p=4&z=1"
    assert result["fallback40HasCol"] is True
    assert all(msg and "Invalid image display" in msg for msg in result["rejections"])


def test_wait_translucent_display_loop_handles_initial_pull_updates_unchanged_closed_and_expired():
    result = run("""
      const waitCalls = [];
      const queue = [
        { ok: true, launch_token: 'tok-1', revision: 2, query: '?c=1&g=original&t=2&p=4&z=1', expired: false, unchanged: true },
        { ok: true, launch_token: 'wrong-tok', revision: 3, query: '?c=0&g=original&t=2&p=4&z=1', expired: false, unchanged: false },
        { ok: true, launch_token: 'tok-1', revision: 2, query: '?c=0&g=original&t=2&p=4&z=1', expired: false, unchanged: false },
        { ok: true, launch_token: 'tok-1', revision: 3, query: '?c=0&p=999&g=original&t=2&z=1', expired: false, unchanged: false },
        { ok: true, launch_token: 'tok-1', revision: 4, query: '?g=original&t=2&p=4&z=1', expired: false, unchanged: false },
        { ok: false, closed: true },
      ];
      let statePulls = 0;
      const h = harness({
        withWorld: true,
        initialContext: { launch_token: 'tok-1', display_revision: 0 },
        bridgeOverrides: {
          async translucent_display_state() {
            statePulls += 1;
            return { ok: true, launch_token: 'tok-1', revision: 2, query: '?c=1&g=original&t=2&p=4&z=1', expired: false };
          },
          async wait_translucent_display(token, afterRev, timeoutMs) {
            waitCalls.push([token, afterRev, timeoutMs]);
            return queue.shift();
          },
        },
      });
      for (let i = 0; i < 20; i++) await Promise.resolve();
      const urlBeforePaint2 = h.osdOptions.tileSources.getTileUrl(0, 0, 0);
      // fully-loaded-change BEFORE tiled-image-drawn must not advance rev 2 to rev 4.
      h.item.fullyLoaded = true;
      h.viewer.handlers['fully-loaded-change']({ fullyLoaded: true });
      const urlAfterFullyLoadedOnly = h.osdOptions.tileSources.getTileUrl(0, 0, 0);
      // Drawing active tiles completes rev 2 and drains pending rev 4 onto activeQuery.
      const tileRev2 = { level: 0, x: 0, y: 0 };
      h.item.tilesMatrix = { 0: { 0: { 0: tileRev2 } } };
      h.viewer.handlers['tiled-image-drawn']({ tiledImage: h.item, tiles: [tileRev2] });
      const afterClosed = {
        statePulls,
        waitCalls,
        maxTilesPerFrame: h.osdOptions.maxTilesPerFrame,
        query: h.mounted.state.query,
        rev: h.mounted.state.displayRevision,
        urlBeforePaint2,
        urlAfterFullyLoadedOnly,
        url: h.osdOptions.tileSources.getTileUrl(0, 0, 0),
        status: h.element('overlay-status').textContent,
        statusHidden: h.element('overlay-status').hidden,
        syncError: h.mounted.state.syncError,
      };

      // Separate mount verifying expired: true stops the loop and shows reopen banner.
      const expWaitCalls = [];
      const hExp = harness({
        withBackdrop: true,
        withWorld: true,
        initialContext: { launch_token: 'tok-exp', display_revision: 1 },
        bridgeOverrides: {
          async wait_translucent_display(token, afterRev, timeoutMs) {
            expWaitCalls.push([token, afterRev, timeoutMs]);
            if (expWaitCalls.length === 1) {
              return { ok: true, launch_token: 'tok-exp', revision: 1, query: context.query, expired: true, reason: 'generation_changed' };
            }
            return { ok: true, launch_token: 'tok-exp', revision: 2, query: '?g=original&t=2&p=4&z=1', expired: false };
          },
        },
      });
      for (let i = 0; i < 10; i++) await Promise.resolve();
      const postExpiredAccepted = hExp.mounted.applyDisplaySnapshot({
        ok: true, launch_token: 'tok-exp', revision: 5, query: '?g=original&t=2&p=4&z=1',
      });
      return {
        afterClosed,
        expired: {
          calls: expWaitCalls.length,
          flag: hExp.mounted.state.expired,
          status: hExp.element('overlay-status').textContent,
          statusHidden: hExp.element('overlay-status').hidden,
          backdropHidden: hExp.element('translucent-backdrop').hidden,
          postExpiredAccepted,
        },
      };
    """)
    assert result["afterClosed"]["statePulls"] == 1
    assert result["afterClosed"]["maxTilesPerFrame"] == 6
    assert result["afterClosed"]["waitCalls"][0] == ["tok-1", 2, 25000]
    assert result["afterClosed"]["waitCalls"][-1] == ["tok-1", 4, 25000]
    assert result["afterClosed"]["query"] == "g=original&t=2&p=4&z=1"
    assert result["afterClosed"]["rev"] == 4
    assert result["afterClosed"]["urlBeforePaint2"].endswith("/api/tile/1/0/0.jpg?c=1&g=original&t=2&p=4&z=1")
    assert result["afterClosed"]["urlAfterFullyLoadedOnly"] == result["afterClosed"]["urlBeforePaint2"]
    assert result["afterClosed"]["url"].endswith("/api/tile/1/0/0.jpg?g=original&t=2&p=4&z=1")
    assert result["afterClosed"]["status"] == ""
    assert result["afterClosed"]["statusHidden"] is True
    assert result["afterClosed"]["syncError"] == ""
    assert result["expired"] == {
        "calls": 1,
        "flag": True,
        "status": "The source image changed. Close and reopen the translucent viewer.",
        "statusHidden": False,
        "backdropHidden": True,
        "postExpiredAccepted": False,
    }


def test_wait_loop_transient_retry_persistent_sync_error_and_selective_status_clearing():
    result = run("""
      // 1) Single transient failure retries immediately with zero error banner.
      let tCount = 0;
      const hTransient = harness({
        withWorld: true,
        initialContext: { launch_token: 'tok-t', display_revision: 0 },
        bridgeOverrides: {
          async wait_translucent_display() {
            tCount += 1;
            if (tCount === 1) throw new Error('temporary ipc hiccup');
            if (tCount === 2) return { ok: true, launch_token: 'tok-t', revision: 1, query: '?c=0&g=original&t=2&p=4&z=1', expired: false };
            return { ok: false, closed: true };
          },
        },
      });
      for (let i = 0; i < 15; i++) await Promise.resolve();
      const transient = {
        calls: tCount,
        rev: hTransient.mounted.state.displayRevision,
        syncError: hTransient.mounted.state.syncError,
        status: hTransient.element('overlay-status').textContent,
      };

      // 2) Two consecutive failures show sync error without resetting tiles, back off 1000ms, and clear on recovery.
      const timers = [];
      let pCount = 0;
      const hPersist = harness({
        withWorld: true,
        initialContext: { launch_token: 'tok-p', display_revision: 0 },
        winOverrides: {
          setTimeout(fn, ms) { timers.push({ fn, ms, cleared: false }); return timers.length; },
          clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cleared = true; },
        },
        bridgeOverrides: {
          async wait_translucent_display() {
            pCount += 1;
            if (pCount <= 2) throw new Error('bridge down');
            if (pCount === 3) return { ok: true, launch_token: 'tok-p', revision: 1, query: '?c=0&g=original&t=2&p=4&z=1', expired: false };
            return { ok: false, closed: true };
          },
        },
      });
      for (let i = 0; i < 15; i++) await Promise.resolve();
      const duringError = {
        calls: pCount,
        resets: hPersist.item.resets,
        syncError: hPersist.mounted.state.syncError,
        status: hPersist.element('overlay-status').textContent,
        backoffMs: timers.filter(t => t.ms === 1000).map(t => t.ms),
      };
      // Trigger the 1000ms backoff timer -> recovery should clear syncError and status.
      timers.find(t => t.ms === 1000).fn();
      for (let i = 0; i < 15; i++) await Promise.resolve();
      const afterRecovery = {
        rev: hPersist.mounted.state.displayRevision,
        syncError: hPersist.mounted.state.syncError,
        status: hPersist.element('overlay-status').textContent,
        statusHidden: hPersist.element('overlay-status').hidden,
      };

      // 3) Sync recovery must NOT clear an unrelated status banner (e.g. opacity failure).
      const timers2 = [];
      let uCount = 0;
      const hUnrelated = harness({
        withWorld: true,
        failOpacity: true,
        initialContext: { launch_token: 'tok-u', display_revision: 0 },
        winOverrides: {
          setTimeout(fn, ms) { timers2.push({ fn, ms }); return timers2.length; },
          clearTimeout() {},
        },
        bridgeOverrides: {
          async wait_translucent_display() {
            uCount += 1;
            if (uCount <= 2) return { ok: false };
            if (uCount === 3) return { ok: true, launch_token: 'tok-u', revision: 0, query: context.query, unchanged: true };
            return { ok: false, closed: true };
          },
        },
      });
      for (let i = 0; i < 15; i++) await Promise.resolve();
      await hUnrelated.mounted.setOpacity(0.7);
      const beforeRecoverStatus = hUnrelated.element('overlay-status').textContent;
      timers2.find(t => t.ms === 1000).fn();
      for (let i = 0; i < 15; i++) await Promise.resolve();
      const afterUnrelatedRecover = {
        beforeRecoverStatus,
        status: hUnrelated.element('overlay-status').textContent,
        syncError: hUnrelated.mounted.state.syncError,
      };

      return { transient, duringError, afterRecovery, afterUnrelatedRecover };
    """)
    assert result["transient"] == {"calls": 3, "rev": 1, "syncError": "", "status": ""}
    assert result["duringError"] == {
        "calls": 2,
        "resets": 0,
        "syncError": "Could not sync contrast settings.",
        "status": "Could not sync contrast settings.",
        "backoffMs": [1000],
    }
    assert result["afterRecovery"] == {"rev": 1, "syncError": "", "status": "", "statusHidden": True}
    assert result["afterUnrelatedRecover"] == {
        "beforeRecoverStatus": "opacity unavailable",
        "status": "opacity unavailable",
        "syncError": "",
    }


def test_backdrop_captures_before_reset_and_clears_on_paint_camera_resize_error_and_pagehide():
    result = run("""
      const h = harness({
        withBackdrop: true,
        withWorld: true,
        initialContext: { launch_token: 'tok-bd', display_revision: 0 },
      });
      const bd = h.element('translucent-backdrop');
      const m = h.mounted;

      // 1) Update triggers backdrop capture; unchanged update-viewport keeps backdrop visible; full paint clears it.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-bd', revision: 1, query: '?c=0&g=original&t=2&p=4&z=1' });
      const afterReq1 = {
        hidden: bd.hidden,
        draws: h.backdropLog.draws.length,
        clears: h.backdropLog.clears.length,
        loaderClears: h.loaderClears.length,
      };
      // Redraw without camera change keeps backdrop visible.
      h.viewer.handlers['update-viewport']();
      const afterStillViewport = { hidden: bd.hidden };
      // Full paint clears backdrop.
      const tile1 = { level: 0, x: 0, y: 0 };
      h.item.tilesMatrix = { 0: { 0: { 0: tile1 } } };
      h.item.fullyLoaded = true;
      h.viewer.handlers['tiled-image-drawn']({ tiledImage: h.item, tiles: [tile1] });
      const afterPaint = { hidden: bd.hidden, clears: h.backdropLog.clears.length };

      const finishPaint = () => {
        const tile = { level: 0, x: 0, y: 0 };
        h.item.tilesMatrix = { 0: { 0: { 0: tile } } };
        h.item.fullyLoaded = true;
        h.viewer.handlers['fully-loaded-change']({ fullyLoaded: true });
        h.viewer.handlers['tiled-image-drawn']({ tiledImage: h.item, tiles: [tile] });
      };

      // 2) Camera pan/zoom/rotate/flip clears backdrop immediately.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-bd', revision: 2, query: '?c=1&g=original&t=2&p=4&z=1' });
      const visibleBeforePan = !bd.hidden;
      h.viewer.viewport.panTo(new Point(0.45, 0.25));
      h.viewer.handlers['update-viewport']();
      const hiddenAfterPan = bd.hidden;
      // Finish paint for rev 2 so gate settles.
      finishPaint();

      // 3) Rotation or flip change clears backdrop immediately.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-bd', revision: 3, query: '?c=0&g=original&t=2&p=4&z=1' });
      const visibleBeforeFlip = !bd.hidden;
      h.viewer.viewport.setFlip(false);
      h.viewer.handlers['update-viewport']();
      const hiddenAfterFlip = bd.hidden;
      finishPaint();

      // 4) resize() clears backdrop immediately.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-bd', revision: 4, query: '?c=1&g=original&t=2&p=4&z=1' });
      const visibleBeforeResize = !bd.hidden;
      h.element('translucent-stage').clientWidth = 640;
      m.resize();
      const hiddenAfterResize = bd.hidden;
      finishPaint();

      // 5) open-failed clears backdrop immediately.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-bd', revision: 5, query: '?c=0&g=original&t=2&p=4&z=1' });
      const visibleBeforeOpenFail = !bd.hidden;
      h.viewer.handlers['open-failed']();
      const hiddenAfterOpenFail = bd.hidden;
      finishPaint();

      // 6) pagehide clears backdrop immediately.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-bd', revision: 6, query: '?c=1&g=original&t=2&p=4&z=1' });
      const visibleBeforeHide = !bd.hidden;
      h.winListeners.pagehide();
      const hiddenAfterHide = bd.hidden;

      return {
        afterReq1, afterStillViewport, afterPaint,
        visibleBeforePan, hiddenAfterPan,
        visibleBeforeFlip, hiddenAfterFlip,
        visibleBeforeResize, hiddenAfterResize,
        visibleBeforeOpenFail, hiddenAfterOpenFail,
        visibleBeforeHide, hiddenAfterHide,
      };
    """)
    assert result["afterReq1"] == {"hidden": False, "draws": 1, "clears": 1, "loaderClears": 1}
    assert result["afterStillViewport"] == {"hidden": False}
    assert result["afterPaint"] == {"hidden": True, "clears": 2}
    assert result["visibleBeforePan"] is True and result["hiddenAfterPan"] is True
    assert result["visibleBeforeFlip"] is True and result["hiddenAfterFlip"] is True
    assert result["visibleBeforeResize"] is True and result["hiddenAfterResize"] is True
    assert result["visibleBeforeOpenFail"] is True and result["hiddenAfterOpenFail"] is True
    assert result["visibleBeforeHide"] is True and result["hiddenAfterHide"] is True


def test_live_display_a_b_a_locked_roi_camera_invariance_and_stale_tile_error_suppression():
    result = run("""
      const h = harness({
        withBackdrop: true,
        withWorld: true,
        initialContext: {
          rotation: 0, flip: false,
          launch_token: 'tok-aba', display_revision: 0,
          query: 'c=0&win=0%3A255&g=original&t=2&p=4&z=1',
          rois: [{ x: 390, y: 240, w: 20, h: 20 }, { x: 450, y: 250, w: 10, h: 10 }],
          roiIndex: 0,
        },
      });
      const m = h.mounted;
      await m.toggleLock();
      const beforeCamera = api.camera(h.viewer);
      const beforeRoiPoints = h.element('roi-overlay').children.map(b => b.attributes.points);
      const beforeOpacity = m.state.opacity;

      const qA = 'c=0&win=0%3A255&g=original&t=2&p=4&z=1';
      const qB = 'c=0&win=0%3A128&g=original&t=2&p=4&z=1';
      const qC = 'c=0&win=0%3A192&g=original&t=2&p=4&z=1';

      // Start A -> B while locked.
      h.item.fullyLoaded = false;
      const acceptedB = m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-aba', revision: 1, query: '?' + qB });
      const activeDuringB = m.displayGate.getActiveQuery();
      const urlDuringB = h.osdOptions.tileSources.getTileUrl(0, 0, 0);

      // Obsolete tile from A fails -> must be suppressed (no error banner!).
      const activeTileB = { level: 0, x: 0, y: 0, getUrl: () => urlDuringB };
      const staleTileA = { level: 0, x: 0, y: 0, getUrl: () => 'http://localhost:9876/token/s/abc/api/tile/1/0/0.jpg?' + qA };
      h.item.tilesMatrix = { 0: { 0: { 0: activeTileB } } };
      h.viewer.handlers['tile-load-failed']({ tile: staleTileA });
      const statusAfterStaleError = h.element('overlay-status').textContent;

      // Queue C then A while B is still rendering (A -> B -> A coalescing).
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-aba', revision: 2, query: '?' + qC });
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-aba', revision: 3, query: '?' + qA });
      const stillRenderingB = m.displayGate.getActiveQuery();

      // Finish B -> gate must advance straight to A (skipping C).
      h.item.fullyLoaded = true;
      h.viewer.handlers['tiled-image-drawn']({ tiledImage: h.item, tiles: [activeTileB] });
      const activeAfterB = m.displayGate.getActiveQuery();
      const urlAfterB = h.osdOptions.tileSources.getTileUrl(0, 0, 0);

      // Active tile failure on A shows error banner and clears backdrop.
      const activeTileA = { level: 0, x: 0, y: 0, getUrl: () => urlAfterB };
      h.item.tilesMatrix = { 0: { 0: { 0: activeTileA } } };
      h.viewer.handlers['tile-load-failed']({ tile: activeTileA });
      const statusAfterActiveError = h.element('overlay-status').textContent;
      const backdropHiddenAfterError = h.element('translucent-backdrop').hidden;

      // Subsequent update paints cleanly and clears the tile-load-failed status.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-aba', revision: 4, query: '?' + qC });
      const activeTileC = { level: 0, x: 0, y: 0 };
      h.item.tilesMatrix = { 0: { 0: { 0: activeTileC } } };
      h.item.fullyLoaded = true;
      h.viewer.handlers['tiled-image-drawn']({ tiledImage: h.item, tiles: [activeTileC] });

      return {
        acceptedB,
        activeDuringB,
        stillRenderingB,
        activeAfterB,
        statusAfterStaleError,
        statusAfterActiveError,
        backdropHiddenAfterError,
        statusAfterRecovery: h.element('overlay-status').textContent,
        finalActive: m.displayGate.getActiveQuery(),
        locked: m.state.locked,
        opacityUnchanged: m.state.opacity === beforeOpacity,
        cameraUnchanged: JSON.stringify(beforeCamera) === JSON.stringify(api.camera(h.viewer)),
        roisUnchanged: JSON.stringify(beforeRoiPoints) === JSON.stringify(h.element('roi-overlay').children.map(b => b.attributes.points)),
      };
    """)
    assert result["acceptedB"] is True
    assert result["activeDuringB"] == "c=0&win=0%3A128&g=original&t=2&p=4&z=1"
    assert result["stillRenderingB"] == result["activeDuringB"]
    assert result["activeAfterB"] == "c=0&win=0%3A255&g=original&t=2&p=4&z=1"
    assert result["statusAfterStaleError"] == ""
    assert result["statusAfterActiveError"] == "Some image tiles could not load. Close and reopen the translucent viewer."
    assert result["backdropHiddenAfterError"] is True
    assert result["statusAfterRecovery"] == ""
    assert result["finalActive"] == "c=0&win=0%3A192&g=original&t=2&p=4&z=1"
    assert result["locked"] is True
    assert result["opacityUnchanged"] is True
    assert result["cameraUnchanged"] is True
    assert result["roisUnchanged"] is True


def test_pagehide_teardown_under_minimal_and_full_mocks():
    result = run("""
      // 1) Minimal timer-free VM sandbox with no removeEventListener and no backdrop getContext.
      const fs = require('node:fs');
      const vm = require('node:vm');
      const path = require('node:path');
      const dir = path.dirname(process.argv[1]);
      const sandbox = { URL, URLSearchParams, console, Math, Number, Array, String, Boolean, Promise, Set, Map, Error };
      sandbox.globalThis = sandbox;
      sandbox.window = sandbox;
      vm.createContext(sandbox);
      vm.runInContext(fs.readFileSync(path.join(dir, 'viewer-geometry-v1.js'), 'utf8'), sandbox);
      const mod = { exports: {} };
      vm.runInContext(`(function(module, exports, require){ ${fs.readFileSync(process.argv[1], 'utf8')}\n })`, sandbox)(
        mod, mod.exports, () => sandbox.Nd2ViewerGeometry
      );
      const minimalApi = mod.exports;
      const winHandlers = {};
      let destroyed = 0;
      const els = new Map();
      const getEl = id => {
        if (!els.has(id)) {
          els.set(id, { id, style: {}, dataset: {}, attributes: {}, children: [], clientWidth: 800, clientHeight: 500,
            disabled: false, hidden: false, addEventListener() {}, setAttribute(k, v) { this.attributes[k] = v; },
            replaceChildren() {}, appendChild(c) { return c; }, get lastElementChild() { return null; } });
        }
        return els.get(id);
      };
      const minDoc = { getElementById: getEl, createElementNS: () => getEl('svg-child'), querySelectorAll: () => [], addEventListener() {} };
      const minWin = { innerWidth: 800, innerHeight: 500, addEventListener(n, cb) { winHandlers[n] = cb; } };
      const minViewer = fakeViewer();
      minViewer.destroy = () => { destroyed += 1; };
      const minOSD = () => minViewer; minOSD.Point = Point;
      let minWaitCalls = 0;
      const minBridge = {
        async wait_translucent_display() { minWaitCalls += 1; return { ok: false }; },
      };
      const minMounted = minimalApi.mount({
        window: minWin, document: minDoc, api: minBridge, OSD: minOSD,
        context: { ...context, launch_token: 'tok-min', display_revision: 0 }, info,
      });
      minViewer.handlers.open();
      for (let i = 0; i < 10; i++) await Promise.resolve();
      // Timer-free VM exited cleanly after 2 errors without spinning.
      const timerFreeCalls = minWaitCalls;
      const timerFreeError = minMounted.state.syncError;
      winHandlers.pagehide();
      const postDisposeAccepted = minMounted.applyDisplaySnapshot({ ok: true, launch_token: 'tok-min', revision: 1, query: '?c=0&g=original&t=2&p=4&z=1' });

      // 2) Full mock verifies removeEventListener cleanup and backoff timer cancellation on pagehide.
      const removedWin = [], removedDoc = [], clearedTimers = [];
      let timerId = 0;
      let fullCalls = 0;
      const h = harness({
        withWorld: true,
        initialContext: { launch_token: 'tok-full', display_revision: 0 },
        winOverrides: {
          removeEventListener(name) { removedWin.push(name); },
          setTimeout() { timerId += 1; return timerId; },
          clearTimeout(id) { clearedTimers.push(id); },
        },
        docOverrides: {
          removeEventListener(name) { removedDoc.push(name); },
        },
        bridgeOverrides: {
          async wait_translucent_display() { fullCalls += 1; throw new Error('offline'); },
        },
      });
      for (let i = 0; i < 15; i++) await Promise.resolve();
      h.winListeners.pagehide();
      for (let i = 0; i < 5; i++) await Promise.resolve();

      return {
        destroyed,
        timerFreeCalls,
        timerFreeError,
        postDisposeAccepted,
        fullCalls,
        clearedTimers,
        removedWin,
        removedDoc,
      };
    """)
    assert result["destroyed"] == 1
    assert result["timerFreeCalls"] == 2
    assert result["timerFreeError"] == "Could not sync contrast settings."
    assert result["postDisposeAccepted"] is False
    assert result["fullCalls"] == 2
    assert len(result["clearedTimers"]) >= 1
    assert "resize" in result["removedWin"]
    assert set(["pointerdown", "keydown", "wheel"]).issubset(set(result["removedDoc"]))


def test_watchdog_timeout_preserves_backdrop_across_queued_reset_and_clears_on_late_draw():
    result = run("""
      let nextTimerId = 1;
      const activeTimers = new Map();
      const h = harness({
        withBackdrop: true,
        withWorld: true,
        initialContext: { launch_token: 'tok-wd', display_revision: 0 },
        winOverrides: {
          setTimeout(fn, ms) {
            const id = nextTimerId++;
            activeTimers.set(id, { fn, ms });
            return id;
          },
          clearTimeout(id) { activeTimers.delete(id); },
        },
      });
      const bd = h.element('translucent-backdrop');
      const m = h.mounted;
      const fireWatchdog = () => {
        const entry = [...activeTimers.entries()].find(([, v]) => v.ms === 2000);
        if (!entry) throw new Error('expected 2000ms watchdog timer');
        activeTimers.delete(entry[0]);
        entry[1].fn();
      };

      // Rev 1 captures initial painted image into backdrop; queue Rev 2 while Rev 1 stalls.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-wd', revision: 1, query: '?c=0&win=0:128&g=original&t=2&p=4&z=1' });
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-wd', revision: 2, query: '?c=0&win=0:64&g=original&t=2&p=4&z=1' });
      const beforeTimeout = { hidden: bd.hidden, draws: h.backdropLog.draws.length };

      // Watchdog expires on Rev 1 -> keeps backdrop visible AND starts Rev 2 WITHOUT overwriting backdrop.
      fireWatchdog();
      const afterTimeout1 = {
        hidden: bd.hidden,
        draws: h.backdropLog.draws.length,
        active: m.displayGate.getActiveQuery(),
        rendering: m.displayGate.isRendering(),
      };

      // Watchdog expires on Rev 2 with no pending -> backdrop STILL stays visible.
      fireWatchdog();
      const afterTimeout2 = {
        hidden: bd.hidden,
        draws: h.backdropLog.draws.length,
        rendering: m.displayGate.isRendering(),
      };

      // Late draw finally paints Rev 2 -> clears backdrop cleanly.
      const lateTile = { level: 0, x: 0, y: 0 };
      h.item.tilesMatrix = { 0: { 0: { 0: lateTile } } };
      h.item.fullyLoaded = true;
      h.viewer.handlers['tiled-image-drawn']({ tiledImage: h.item, tiles: [lateTile] });
      const afterLateDraw = { hidden: bd.hidden, draws: h.backdropLog.draws.length };

      // After timeout + camera pan (update-viewport clears backdrop), a subsequent reset with hadPainted=false
      // must NOT capture an unpainted/blank canvas.
      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-wd', revision: 3, query: '?c=0&win=0:32&g=original&t=2&p=4&z=1' });
      const drawsAfterRev3 = h.backdropLog.draws.length;
      fireWatchdog();
      h.viewer.viewport.panTo(new Point(0.42, 0.25));
      h.viewer.handlers['update-viewport']();
      const hiddenAfterPan = bd.hidden;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-wd', revision: 4, query: '?c=0&win=0:16&g=original&t=2&p=4&z=1' });
      const afterUnpaintedReset = { hidden: bd.hidden, draws: h.backdropLog.draws.length };

      // Paint Rev 4 cleanly so paintedQuery is non-null again, then verify pose change detected directly in captureBackdrop.
      const tile4 = { level: 0, x: 0, y: 0 };
      h.item.tilesMatrix = { 0: { 0: { 0: tile4 } } };
      h.item.fullyLoaded = true;
      h.viewer.handlers['tiled-image-drawn']({ tiledImage: h.item, tiles: [tile4] });

      h.item.fullyLoaded = false;
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-wd', revision: 5, query: '?c=0&win=0:8&g=original&t=2&p=4&z=1' });
      m.applyDisplaySnapshot({ ok: true, launch_token: 'tok-wd', revision: 6, query: '?c=0&win=0:4&g=original&t=2&p=4&z=1' });
      const drawsAfterRev5 = h.backdropLog.draws.length;
      // Pan WITHOUT firing update-viewport before watchdog advances to Rev 6 (hadPainted=false).
      h.viewer.viewport.panTo(new Point(0.48, 0.25));
      fireWatchdog();
      const afterPoseMismatchInCapture = { hidden: bd.hidden, draws: h.backdropLog.draws.length };

      return {
        beforeTimeout,
        afterTimeout1,
        afterTimeout2,
        afterLateDraw,
        drawsAfterRev3,
        hiddenAfterPan,
        afterUnpaintedReset,
        drawsAfterRev5,
        afterPoseMismatchInCapture,
      };
    """)
    assert result["beforeTimeout"] == {"hidden": False, "draws": 1}
    assert result["afterTimeout1"] == {
        "hidden": False,
        "draws": 1,
        "active": "c=0&win=0%3A64&g=original&t=2&p=4&z=1",
        "rendering": True,
    }
    assert result["afterTimeout2"] == {"hidden": False, "draws": 1, "rendering": False}
    assert result["afterLateDraw"] == {"hidden": True, "draws": 1}
    assert result["drawsAfterRev3"] == 2
    assert result["hiddenAfterPan"] is True
    assert result["afterUnpaintedReset"] == {"hidden": True, "draws": 2}
    assert result["drawsAfterRev5"] == 3
    assert result["afterPoseMismatchInCapture"] == {"hidden": True, "draws": 3}


def test_tiled_image_fully_loaded_change_binds_via_world_events_and_cleans_up_on_pagehide():
    result = run("""
      function makeEventedWorld() {
        const worldHandlers = { 'add-item': [], 'remove-item': [] };
        const items = [];
        return {
          items,
          worldHandlers,
          getItemAt(i) { return items[i] || null; },
          addHandler(name, fn) { (worldHandlers[name] ||= []).push(fn); },
          removeHandler(name, fn) {
            if (!worldHandlers[name]) return;
            worldHandlers[name] = worldHandlers[name].filter(cb => cb !== fn);
          },
          emit(name, ev) { for (const fn of [...(worldHandlers[name] || [])]) fn(ev); },
        };
      }
      function makeTiledItem(viewer) {
        const itemHandlers = {};
        return {
          viewer,
          tilesMatrix: {},
          fullyLoaded: false,
          _needsDraw: true,
          itemHandlers,
          reset() { this._needsDraw = true; },
          getDrawArea() { return { x: 0, y: 0, width: 800, height: 500 }; },
          getFullyLoaded() { return this.fullyLoaded; },
          _setFullyLoaded(v) { this.fullyLoaded = Boolean(v); },
          needsDraw() { return this._needsDraw; },
          getClip() { return null; },
          setClip() {},
          addHandler(name, fn) { (itemHandlers[name] ||= []).push(fn); },
          removeHandler(name, fn) {
            if (!itemHandlers[name]) return;
            itemHandlers[name] = itemHandlers[name].filter(cb => cb !== fn);
          },
          emit(name, ev) {
            for (const fn of [...(itemHandlers[name] || [])]) fn({ eventSource: this, ...ev });
          },
        };
      }

      // 1) World add-item / remove-item lifecycle + OSD event ordering + pagehide cleanup.
      const world = makeEventedWorld();
      const elements = new Map();
      function newEl(id) {
        return {
          id, style: {}, dataset: {}, attributes: {}, children: [], listeners: {},
          clientWidth: 800, clientHeight: 500, disabled: false, hidden: false,
          addEventListener(n, cb) { this.listeners[n] = cb; },
          setAttribute(n, v) { this.attributes[n] = v; },
          replaceChildren(...c) { this.children = c; },
          appendChild(c) { this.children.push(c); return c; },
          get lastElementChild() { return this.children[this.children.length - 1]; },
        };
      }
      function getEl(id) { if (!elements.has(id)) elements.set(id, newEl(id)); return elements.get(id); }
      const bd = getEl('translucent-backdrop');
      bd.hidden = true; bd.width = 0; bd.height = 0;
      bd.getContext = kind => kind === '2d' ? { clearRect() {}, drawImage() {} } : null;
      const winListeners = {};
      const win = { innerWidth: 800, innerHeight: 500, addEventListener(n, cb) { winListeners[n] = cb; }, removeEventListener() {} };
      const doc = { getElementById: getEl, createElementNS: (_, n) => newEl(n), querySelectorAll: () => [], addEventListener() {}, removeEventListener() {} };
      const viewer = fakeViewer();
      viewer.drawer = { canvas: { width: 800, height: 500 } };
      viewer.world = world;
      const OSD = () => viewer; OSD.Point = Point;
      const mounted = api.mount({
        window: win, document: doc, api: {}, OSD,
        context: { ...context, launch_token: 'tok-ti', display_revision: 0 }, info,
      });

      const worldAddBound = world.worldHandlers['add-item'].length;
      const worldRemoveBound = world.worldHandlers['remove-item'].length;

      const item1 = makeTiledItem(viewer);
      world.items.push(item1);
      world.emit('add-item', { item: item1 });
      // Open fallback must NOT attach a duplicate listener on item1.
      viewer.handlers.open();
      const item1HandlersAfterOpen = (item1.itemHandlers['fully-loaded-change'] || []).length;

      // Start rev 1 and queue rev 2: TiledImage fully-loaded-change BEFORE tiled-image-drawn must wait!
      mounted.applyDisplaySnapshot({ ok: true, launch_token: 'tok-ti', revision: 1, query: '?c=0&win=0:128&g=original&t=2&p=4&z=1' });
      mounted.applyDisplaySnapshot({ ok: true, launch_token: 'tok-ti', revision: 2, query: '?c=0&win=0:64&g=original&t=2&p=4&z=1' });
      const tile1 = { level: 0, x: 0, y: 0 };
      item1.tilesMatrix = { 0: { 0: { 0: tile1 } } };
      viewer.handlers['tile-loaded']({ tiledImage: item1, tile: tile1 });
      item1.fullyLoaded = true;
      item1.emit('fully-loaded-change', { fullyLoaded: true });
      const beforeDraw = {
        active: mounted.displayGate.getActiveQuery(),
        backdropHidden: bd.hidden,
      };
      // Active draw completes rev 1 and starts rev 2.
      item1._needsDraw = false;
      viewer.handlers['tiled-image-drawn']({ tiledImage: item1, tiles: [tile1] });
      const afterRev1Draw = {
        active: mounted.displayGate.getActiveQuery(),
        rendering: mounted.displayGate.isRendering(),
      };

      // Reverse order on rev 2: tiled-image-drawn first, then TiledImage fully-loaded-change with !needsDraw().
      const tile2 = { level: 0, x: 0, y: 0 };
      item1.tilesMatrix = { 0: { 0: { 0: tile2 } } };
      viewer.handlers['tile-loaded']({ tiledImage: item1, tile: tile2 });
      item1.fullyLoaded = false;
      item1._needsDraw = false;
      viewer.handlers['tiled-image-drawn']({ tiledImage: item1, tiles: [tile2] });
      const stillRenderingRev2 = mounted.displayGate.isRendering();
      item1.fullyLoaded = true;
      item1.emit('fully-loaded-change', { fullyLoaded: true });
      const afterRev2Reverse = {
        rendering: mounted.displayGate.isRendering(),
        backdropHidden: bd.hidden,
      };

      // remove-item unbinds item1's listener; add-item binds item2; pagehide unbinds world and item2.
      world.items.splice(0, 1);
      world.emit('remove-item', { item: item1 });
      const item1HandlersAfterRemove = (item1.itemHandlers['fully-loaded-change'] || []).length;

      const item2 = makeTiledItem(viewer);
      world.items.push(item2);
      world.emit('add-item', { item: item2 });
      const item2HandlersBeforeHide = (item2.itemHandlers['fully-loaded-change'] || []).length;

      winListeners.pagehide();
      const afterHide = {
        worldAdd: world.worldHandlers['add-item'].length,
        worldRemove: world.worldHandlers['remove-item'].length,
        item2Handlers: (item2.itemHandlers['fully-loaded-change'] || []).length,
      };

      // 2) open fallback when viewer.world has getItemAt but no addHandler.
      const fallbackViewer = fakeViewer();
      const fallbackItem = makeTiledItem(fallbackViewer);
      fallbackViewer.world = { getItemAt(i) { return i === 0 ? fallbackItem : null; } };
      const fbWinListeners = {};
      const fbWin = { innerWidth: 800, innerHeight: 500, addEventListener(n, cb) { fbWinListeners[n] = cb; } };
      const fbEls = new Map();
      const fbGetEl = id => { if (!fbEls.has(id)) fbEls.set(id, newEl(id)); return fbEls.get(id); };
      const fbDoc = { getElementById: fbGetEl, createElementNS: (_, n) => newEl(n), querySelectorAll: () => [], addEventListener() {} };
      const fbOSD = () => fallbackViewer; fbOSD.Point = Point;
      api.mount({ window: fbWin, document: fbDoc, api: {}, OSD: fbOSD, context, info });
      fallbackViewer.handlers.open();
      const fallbackBoundOnOpen = (fallbackItem.itemHandlers['fully-loaded-change'] || []).length;
      fbWinListeners.pagehide();
      const fallbackAfterHide = (fallbackItem.itemHandlers['fully-loaded-change'] || []).length;

      return {
        worldAddBound,
        worldRemoveBound,
        item1HandlersAfterOpen,
        beforeDraw,
        afterRev1Draw,
        stillRenderingRev2,
        afterRev2Reverse,
        item1HandlersAfterRemove,
        item2HandlersBeforeHide,
        afterHide,
        fallbackBoundOnOpen,
        fallbackAfterHide,
      };
    """)
    assert result["worldAddBound"] == 1
    assert result["worldRemoveBound"] == 1
    assert result["item1HandlersAfterOpen"] == 1
    assert result["beforeDraw"] == {
        "active": "c=0&win=0%3A128&g=original&t=2&p=4&z=1",
        "backdropHidden": False,
    }
    assert result["afterRev1Draw"] == {
        "active": "c=0&win=0%3A64&g=original&t=2&p=4&z=1",
        "rendering": True,
    }
    assert result["stillRenderingRev2"] is True
    assert result["afterRev2Reverse"] == {"rendering": False, "backdropHidden": True}
    assert result["item1HandlersAfterRemove"] == 0
    assert result["item2HandlersBeforeHide"] == 1
    assert result["afterHide"] == {"worldAdd": 0, "worldRemove": 0, "item2Handlers": 0}
    assert result["fallbackBoundOnOpen"] == 1
    assert result["fallbackAfterHide"] == 0
