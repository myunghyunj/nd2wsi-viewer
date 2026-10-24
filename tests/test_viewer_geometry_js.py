"""Viewer geometry shared by the main pane and the translucent window: functions, no state."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
MODULE = STATIC / "viewer-geometry-v1.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SETUP = r"""
const geometry=require(process.argv[1]);
class Point{constructor(x,y){this.x=x;this.y=y;}}
const OSD={Point};
// Server order: level 0 is full resolution. 768/1092 is a level whose
// scale*width drifts to 768.0000000000001 and would request a fourth column.
const info={width:1092,height:500,tileSize:256,
  levels:[{width:1092,height:500,path:'0'},{width:768,height:256,path:'1'},
    {width:501,height:251,path:'2'},{width:256,height:128,path:'3'}]};
function fakeViewport({flip=false,width=800,height=600,zoom=2.5,scale=.5,offset={x:100,y:50}}={}){
  // element = image * scale + offset; the flip is the module's job, not OSD's.
  const calls=[];
  return {calls,
    getFlip(){return flip;},
    getContainerSize(){return new Point(width,height);},
    imageToViewerElementCoordinates(p){return new Point(p.x*scale+offset.x,p.y*scale+offset.y);},
    viewerElementToImageCoordinates(p){return new Point((p.x-offset.x)/scale,(p.y-offset.y)/scale);},
    getZoom(current){calls.push(['getZoom',current]);return zoom;},
    zoomTo(...args){calls.push(['zoomTo',...args]);zoom=args[0];},
    panTo(...args){calls.push(['panTo',...args]);},
    imageToViewportCoordinates(x,y){calls.push(['toViewport',typeof x]);
      return typeof x==='object'?new Point(x.x/info.width,x.y/info.width):new Point(x/info.width,y/info.width);},
  };
}
"""


def run(body):
    script = SETUP + "\nprocess.stdout.write(JSON.stringify((()=>{\n" + body + "\n})()));"
    result = subprocess.run([NODE, "-e", script, str(MODULE)], check=True, timeout=20,
                            capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout)


def test_tile_source_reverses_levels_and_counts_exact_tiles_on_odd_sized_levels():
    out = run("""
      const source=geometry.makeTileSource({info,OSD});
      const levels=[0,1,2,3];
      return {shape:[source.width,source.height,source.tileSize,source.tileOverlap,source.minLevel,source.maxLevel],
        scales:levels.map(l=>source.getLevelScale(l)),
        tiles:levels.map(l=>source.getNumTiles(l)),
        pointTiles:source.getNumTiles(0) instanceof Point,
        drifted:levels.map(l=>Math.ceil(source.getLevelScale(l)*info.width/info.tileSize)),
        url:source.getTileUrl(1,2,0)};
    """)
    assert out["shape"] == [1092, 500, 256, 0, 0, 3]
    assert out["scales"] == pytest.approx([256 / 1092, 501 / 1092, 768 / 1092, 1])
    assert out["tiles"] == [{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 3, "y": 1}, {"x": 5, "y": 2}]
    assert out["pointTiles"] is True
    # OSD's default scale-based count would ask for one tile past the edge here.
    assert out["drifted"][2] == 4
    assert out["url"] == "api/tile/2/2/0.jpg"


def test_tile_url_prefixes_base_url_and_reads_a_query_function_per_request():
    out = run("""
      const fixed=geometry.makeTileSource({info,OSD,baseUrl:'http://localhost:9876/token/s/abc/',
        query:'?c=0%2C2&g=original'});
      let current='?c=0',reads=0;
      const live=geometry.makeTileSource({info,OSD,query:()=>{reads++;return current;}});
      const before=reads;
      const first=live.getTileUrl(3,1,0);
      current='?c=1&t=2';
      const second=live.getTileUrl(3,1,0);
      current='';
      const third=live.getTileUrl(0,0,0);
      return {fixed:fixed.getTileUrl(0,3,1),before,first,second,third,reads};
    """)
    assert out["fixed"] == "http://localhost:9876/token/s/abc/api/tile/3/3/1.jpg?c=0%2C2&g=original"
    assert out["before"] == 0
    assert out["first"] == "api/tile/0/1/0.jpg?c=0"
    assert out["second"] == "api/tile/0/1/0.jpg?c=1&t=2"
    assert out["third"] == "api/tile/3/0/0.jpg"
    assert out["reads"] == 3


@pytest.mark.parametrize("flip", [False, True])
def test_point_conversions_mirror_x_only_when_the_viewport_is_flipped(flip):
    out = run(f"""
      const viewport=fakeViewport({{flip:{json.dumps(flip)}}});
      const element=geometry.imageToViewerElementPoint(viewport,{{x:200,y:100}},OSD);
      const image=geometry.viewerElementToImagePoint(viewport,{{x:200,y:100}},OSD);
      const coerced=geometry.flipAwareElementPoint(viewport,{{x:'200',y:'100'}},OSD);
      const absent=geometry.flipAwareElementPoint(undefined,{{x:200,y:100}},OSD);
      const back=geometry.viewerElementToImagePoint(viewport,element,OSD);
      return {{element,image,coerced,absent,back,points:[element,image,coerced].every(p=>p instanceof Point)}};
    """)
    assert out["element"] == ({"x": 600, "y": 100} if flip else {"x": 200, "y": 100})
    assert out["image"] == ({"x": 1000, "y": 100} if flip else {"x": 200, "y": 100})
    assert out["coerced"] == ({"x": 600, "y": 100} if flip else {"x": 200, "y": 100})
    assert out["absent"] == {"x": 200, "y": 100}
    assert out["back"] == {"x": 200, "y": 100}
    assert out["points"] is True


@pytest.mark.parametrize("flip", [False, True])
def test_rect_corners_project_clockwise_from_the_top_left(flip):
    out = run(f"""
      const viewport=fakeViewport({{flip:{json.dumps(flip)}}});
      const named=geometry.rectCornersToElementPoints(viewport,{{x:10,y:20,width:30,height:40}},OSD);
      const short=geometry.rectCornersToElementPoints(viewport,{{x:10,y:20,w:30,h:40}},OSD);
      return {{named,short}};
    """)
    mirror = (lambda x: 800 - x) if flip else (lambda x: x)
    expected = [{"x": mirror(105), "y": 60}, {"x": mirror(120), "y": 60},
                {"x": mirror(120), "y": 80}, {"x": mirror(105), "y": 80}]
    assert out["named"] == expected
    assert out["short"] == expected


@pytest.mark.parametrize("with_osd", [True, False])
def test_center_on_preserving_zoom_keeps_the_zoom_and_pans_immediately(with_osd):
    out = run(f"""
      const viewport=fakeViewport({{zoom:2.5}});
      const center=geometry.centerOnPreservingZoom(viewport,{{x:819,y:109.2}},{"OSD" if with_osd else "undefined"});
      return {{center,calls:viewport.calls,zoom:viewport.getZoom(true)}};
    """)
    assert out["center"] == pytest.approx({"x": .75, "y": .1})
    assert out["calls"][0] == ["toViewport", "object" if with_osd else "number"]
    assert out["calls"][1:4] == [["getZoom", True], ["zoomTo", 2.5, None, True], ["panTo", out["center"], True]]
    assert out["zoom"] == 2.5


def test_module_loads_before_both_viewers_and_the_callers_delegate_to_it():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    translucent_page = (STATIC / "translucent.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    translucent = (STATIC / "translucent-v1.js").read_text(encoding="utf-8")
    assert index.index("viewer-geometry-v1.js") < index.index("app.js")
    assert translucent_page.index("viewer-geometry-v1.js") < translucent_page.index("translucent-v1.js")
    # The pane keeps its function names (tests slice app.js by them) as thin callers.
    for name in ("makeTileSource", "flipAwareViewerElementPoint", "viewerElementToImagePoint",
                 "imageToViewerElementPoint", "panToAstromapRoi"):
        assert f"function {name}(" in app
    assert "window.Nd2ViewerGeometry.makeTileSource({" in app
    assert "window.Nd2ViewerGeometry.centerOnPreservingZoom(" in app
    for osd_call in ("imageToViewerElementCoordinates", "viewerElementToImageCoordinates", "getNumTiles"):
        assert osd_call not in app and osd_call not in translucent
    assert 'require("./viewer-geometry-v1.js")' in translucent
    assert "root.Nd2ViewerGeometry" in translucent
    script = r"""
const fs=require('fs'),vm=require('vm');const context={};vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const api=context.Nd2ViewerGeometry;
process.stdout.write(JSON.stringify({globals:Object.keys(context),exports:Object.keys(api).sort(),
  functions:Object.values(api).every(value=>typeof value==='function')}));
"""
    result = subprocess.run([NODE, "-e", script, str(MODULE)], check=True, capture_output=True,
                            text=True, timeout=20)
    out = json.loads(result.stdout)
    assert out["globals"] == ["Nd2ViewerGeometry"]
    assert out["exports"] == ["centerOnPreservingZoom", "flipAwareElementPoint", "imageToViewerElementPoint",
                              "makeTileSource", "rectCornersToElementPoints", "viewerElementToImagePoint"]
    assert out["functions"] is True
