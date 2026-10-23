"""GeoJSON import preserves source geometry and appends atomically to owned marks."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SETUP = r"""
const fs=require('fs'),vm=require('vm'),path=require('path');
const geo=require(process.argv[1]);
const source=fs.readFileSync(path.join(path.dirname(process.argv[1]),'app.js'),'utf8');
const section=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const state={info:{name:'slide.nd2',width:1000,height:800,selection:{t:0,p:0,z:'mid'}},
  annotations:[{id:'manual',type:'box',x:10,y:20,w:30,h:40,text:'Keep me'}],
  annContext:0,annSite:null,annRevision:0,annReady:true,plate:null,quitPreparing:false};
const messages=[],statuses=[],saved=[],downloads=[];
let locked=false,ready=true;
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{value:'selected',hidden:true});return elements.get(id);};
const context={window:{Nd2AnnotationGeoJSON:geo},state,console,Blob,Date,Set,JSON,Number,Array,Error,
  annotationOwnerReady:()=>ready,annLocked:()=>locked,$:element,
  annotationsChanged(){state.annRevision++;saved.push(JSON.parse(JSON.stringify(state.annotations)));},
  setAnnStatus:message=>statuses.push(message),showToast:message=>messages.push(message),closeEditor(){},
  pixelSize:()=>[.5,.5],geojsonFilename:()=> 'slide.geojson',
  downloadJsonFile:(doc,filename,mime)=>downloads.push({doc,filename,mime})};
vm.createContext(context);
vm.runInContext('let annCounter=0;\n'+
  section('async function openAnnotationFile','function downloadJsonFile')+
  section('function annotationFeature','function safeFilenamePart')+
  section('function exportAnnotationsGeoJSON','function rebuildAnnList')+
  '\nthis.api={openAnnotationFile,annotationFeature,annotationsToGeoJSON,annotationDocument,annotationItemsForThisSlide,exportAnnotationsGeoJSON};',context);
const api=context.api;
const feature=(geometry,properties={name:'ROI'})=>({type:'Feature',geometry,properties});
const collection=features=>({type:'FeatureCollection',features});
const file=(data,name='import.geojson')=>({name,size:100,text:async()=>JSON.stringify(data)});
const polygon={type:'Polygon',coordinates:[[[100,100],[240,100],[220,220],[100,200],[100,100]],
  [[130,130],[160,130],[150,160],[130,130]]]};
"""


def run(body):
    script = SETUP + "\n(async()=>{\n" + body + "\n})().then(value=>process.stdout.write(JSON.stringify(value))).catch(error=>{console.error(error);process.exit(1);});"
    result = subprocess.run([NODE, "-e", script, str(STATIC / "annotation-geojson-v1.js")],
                            capture_output=True, text=True, encoding="utf-8", check=True, timeout=20)
    return json.loads(result.stdout)


def test_native_export_import_preserves_pixel_geometry_names_and_colors():
    result = run("""
      const items=[{type:'pin',x:12.25,y:34.5,text:'pin',color:'#d55e00'},
        {type:'line',x1:20.5,y1:40.25,x2:70.75,y2:90,text:'ruler'},
        {type:'box',x:100.5,y:200.25,w:30.75,h:40.125,text:'box'}];
      const exported=api.annotationsToGeoJSON(items),parsed=geo.parse(exported,state.info);
      return {types:parsed.map(item=>item.type),same:JSON.stringify(api.annotationsToGeoJSON(parsed))===JSON.stringify(exported),
        source:items,parsed:parsed.map(({geojson,...item})=>item)};
    """)
    assert result["types"] == ["pin", "line", "box"]
    assert result["same"] is True
    assert result["parsed"] == result["source"]


@pytest.mark.parametrize("kind", ["FeatureCollection", "Feature", "array"])
def test_qupath_polygons_holes_and_foreign_metadata_roundtrip_without_repair(kind):
    result = run(f"""
      const original=feature({{...polygon,plane:{{c:-1,z:0,t:0}},isEllipse:false}},
        {{name:'Tumour',objectType:'annotation',classification:{{names:['Tumour','Low grade'],color:[213,94,0,180]}},
         isLocked:true,measurements:{{Area:123.4}},metadata:{{review:'accepted'}}}});
      original.id='qupath-id';original.bbox=[100,100,240,220];original.nucleusGeometry={{type:'Point',coordinates:[140,140]}};
      const data={json.dumps(kind)}==='FeatureCollection'?collection([original]):
        {json.dumps(kind)}==='Feature'?original:[original];
      const before=JSON.stringify(data),items=geo.parse(data,state.info),restored=geo.featureForAnnotation(items[0]);
      return {{same:JSON.stringify(original)===JSON.stringify(restored),unchanged:before===JSON.stringify(data),
        type:items[0].type,color:items[0].color,bounds:geo.bounds(restored.geometry)}};
    """)
    assert result == {"same": True, "unchanged": True, "type": "geojson", "color": "#d55e00",
                      "bounds": {"x": 100, "y": 100, "w": 140, "h": 120}}


def test_multipart_shapes_and_polylines_keep_parts_and_holes_on_display_and_export():
    result = run("""
      const geometries=[{type:'MultiPoint',coordinates:[[1,2],[3,4]]},
        {type:'LineString',coordinates:[[1,2],[3,4],[5,2]]},
        {type:'MultiLineString',coordinates:[[[1,2],[3,4]],[[5,6],[7,8]]]},
        {type:'MultiPolygon',coordinates:[polygon.coordinates,[[[300,300],[320,300],[310,320],[300,300]]]]}];
      return geometries.map(geometry=>{
        const original=feature(geometry),item=geo.parse(original,state.info)[0];
        const parts=geo.displayParts(item.geojson.geometry,(x,y)=>({x:1000-y*2,y:x*2}));
        return {same:JSON.stringify(geo.featureForAnnotation(item))===JSON.stringify(original),
          paths:parts.paths.length,points:parts.points.length,
          holes:parts.paths.map(path=>(path.d.match(/ Z/g)||[]).length)};});
    """)
    assert result == [
        {"same": True, "paths": 0, "points": 2, "holes": []},
        {"same": True, "paths": 1, "points": 0, "holes": [0]},
        {"same": True, "paths": 2, "points": 0, "holes": [0, 0]},
        {"same": True, "paths": 2, "points": 0, "holes": [2, 1]},
    ]


def test_legacy_qupath_color_rgb_is_used_and_preserved():
    result = run("""
      const original=feature({type:'Point',coordinates:[1,2]},{colorRGB:-2793984});
      const item=geo.parse(original,state.info)[0];
      return {color:item.color,same:JSON.stringify(geo.featureForAnnotation(item))===JSON.stringify(original)};
    """)
    assert result == {"color": "#d55e00", "same": True}


def test_import_appends_unique_ids_and_persists_through_native_annotation_document():
    result = run("""
      const manual=state.annotations[0],before=JSON.stringify(manual);
      const input=collection([feature(polygon),feature({type:'Point',coordinates:[80,90]})]);
      input.features[0].id='manual';input.features[1].id='manual';
      const accepted=await api.openAnnotationFile(file(input));
      const doc=api.annotationDocument(),reopened=api.annotationItemsForThisSlide(JSON.parse(JSON.stringify(doc)));
      return {accepted,manualSame:state.annotations[0]===manual&&JSON.stringify(manual)===before,
        count:state.annotations.length,unique:new Set(state.annotations.map(a=>a.id)).size,
        saveCount:saved.length,saved:saved[0],reopened:reopened.items,status:statuses.at(-1)};
    """)
    assert result["accepted"] is True
    assert result["manualSame"] is True
    assert result["count"] == result["unique"] == 3
    assert result["saveCount"] == 1
    assert result["saved"] == result["reopened"]
    assert "added to existing annotations" in result["status"]
    assert "source not verified" in result["status"]


@pytest.mark.parametrize("problem", [
    "{type:'Point',coordinates:[-1,20]}",
    "{type:'Point',coordinates:[1001,20]}",
    "{type:'Point',coordinates:[1,2,3]}",
    "{type:'Point',coordinates:['1',2]}",
    "{type:'LineString',coordinates:[[1,2]]}",
    "{type:'Polygon',coordinates:[[[1,2],[3,4],[5,6],[8,9]]]}",
    "{type:'Point',coordinates:[1,2],plane:{c:-1,z:1,t:0}}",
    "{type:'Point',coordinates:[1,2],plane:{c:0,z:0,t:0}}",
    "{type:'GeometryCollection',geometries:[]}",
    "{type:'Polygon',coordinates:[[[1,2],[4,2],[4,5],[1,5],[1,2]]],isEllipse:true}",
])
def test_bad_later_feature_rejects_whole_import_without_altering_manual_marks(problem):
    result = run(f"""
      const before=JSON.stringify(state.annotations),original=state.annotations;
      const accepted=await api.openAnnotationFile(file(collection([feature(polygon),feature({problem})])));
      return {{accepted,unchanged:before===JSON.stringify(state.annotations)&&original===state.annotations,
        saves:saved.length,error:messages.at(-1)}};
    """)
    assert result["accepted"] is False
    assert result["unchanged"] is True
    assert result["saves"] == 0
    assert "Feature 2:" in result["error"]


@pytest.mark.parametrize("metadata", [
    {"source": {"name": "other.nd2", "width": 1000, "height": 800}},
    {"source": {"name": "slide.nd2", "width": 999, "height": 800}},
    {"source": {"name": "slide.nd2", "width": 1000, "height": 800}, "selection": {"t": 1}},
    {"selection": {"t": 1}},
    {"source": "slide.nd2", "selection": {"t": 1}},
    {"coordinate_space": "micrometres"},
    {"crs": {"type": "name", "properties": {"name": "EPSG:4326"}}},
])
def test_source_plane_or_coordinate_space_mismatch_cannot_replace_existing_marks(metadata):
    result = run(f"""
      const before=JSON.stringify(state.annotations),input={{...collection([feature(polygon)]),...{json.dumps(metadata)}}};
      const accepted=await api.openAnnotationFile(file(input));
      return {{accepted,unchanged:before===JSON.stringify(state.annotations),saves:saved.length}};
    """)
    assert result == {"accepted": False, "unchanged": True, "saves": 0}


@pytest.mark.parametrize("change", ["state.annContext++", "state.annSite=1", "locked=true", "ready=false", "state.quitPreparing=true"])
def test_pending_file_read_cannot_import_into_changed_or_locked_annotation_owner(change):
    result = run(f"""
      let finish;const before=JSON.stringify(state.annotations);
      const pending=api.openAnnotationFile({{name:'pending.geojson',size:100,text:()=>new Promise(resolve=>{{finish=resolve;}})}});
      {change};finish(JSON.stringify(collection([feature(polygon)])));
      const accepted=await pending;
      return {{accepted,unchanged:before===JSON.stringify(state.annotations),saves:saved.length}};
    """)
    assert result == {"accepted": False, "unchanged": True, "saves": 0}


def test_newer_import_wins_and_concurrent_manual_edits_survive_geojson_append():
    result = run("""
      let finish;
      const old=api.openAnnotationFile({name:'old.geojson',size:100,text:()=>new Promise(resolve=>{finish=resolve;})});
      state.annotations.push({id:'new-manual',type:'pin',x:7,y:8});state.annRevision++;
      const latest=await api.openAnnotationFile(file(feature({type:'Point',coordinates:[20,30]}),'new.geojson'));
      finish(JSON.stringify(feature({type:'Point',coordinates:[80,90]})));
      return {latest,old:await old,count:state.annotations.length,manual:state.annotations[1],
        imported:state.annotations[2].geojson.geometry,status:statuses.at(-1),saves:saved.length};
    """)
    assert result["latest"] is True
    assert result["old"] is False
    assert result["count"] == 3
    assert result["manual"] == {"id": "new-manual", "type": "pin", "x": 7, "y": 8}
    assert result["imported"] == {"type": "Point", "coordinates": [20, 30]}
    assert result["saves"] == 1
    assert "new.geojson" in result["status"]


def test_geojson_empty_array_and_oversized_file_never_clear_manual_annotations():
    result = run("""
      const before=JSON.stringify(state.annotations);let read=false;
      const empty=await api.openAnnotationFile(file([]));
      const oversized=await api.openAnnotationFile({name:'large.geojson',size:geo.MAX_BYTES+1,text:()=>{read=true;return '[]';}});
      return {empty,oversized,read,unchanged:before===JSON.stringify(state.annotations),saves:saved.length};
    """)
    assert result == {"empty": False, "oversized": False, "read": False, "unchanged": True, "saves": 0}


def test_geojson_export_adds_source_binding_and_roundtrip_validates_it():
    result = run("""
      api.exportAnnotationsGeoJSON();const output=downloads[0];
      const parsed=api.annotationItemsForThisSlide(output.doc);
      return {source:output.doc.source,space:output.doc.coordinate_space,selection:output.doc.selection,
        mime:output.mime,verified:parsed.verified,append:parsed.append,type:parsed.items[0].type};
    """)
    assert result == {"source": {"name": "slide.nd2", "width": 1000, "height": 800},
                      "space": "level-0-pixels", "selection": {"t": 0, "p": 0, "z": "mid"},
                      "mime": "application/geo+json", "verified": True, "append": True, "type": "box"}


def test_renderer_uses_existing_display_transform_and_evenodd_holes_without_editing_geometry():
    result = run("""
      const nodes=[];const item=geo.parse(feature(polygon),state.info)[0];item.id='shape';
      const before=JSON.stringify(item);
      Object.assign(context,{toEl:(x,y)=>({x:1000-y*2,y:x*2}),annColor:()=> '#d55e00',
        hexAlpha:()=> 'rgba(213,94,0,.08)',chip(){},
        svgEl:(tag,attributes,parent)=>{const node={tag,attributes,parent,addEventListener(){}};nodes.push(node);return node;}});
      vm.runInContext(section('function renderImportedGeoJSON','/* ---- alignment landmarks'),context);
      context.renderImportedGeoJSON(item,{});
      return {path:nodes.find(node=>node.tag==='path').attributes,
        unchanged:before===JSON.stringify(item)};
    """)
    assert result["unchanged"] is True
    assert result["path"]["d"] == (
        "M800 200 L800 480 L560 440 L600 200 L800 200 Z "
        "M740 260 L740 320 L680 300 L740 260 Z"
    )
    assert result["path"]["fill-rule"] == "evenodd"
    assert result["path"]["stroke-width"] == 1.5


def test_annotation_file_picker_loads_geojson_parser_before_app():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'accept=".json,.geojson,application/json,application/geo+json"' in html
    assert html.index("annotation-geojson-v1.js") < html.index("static/app.js")
