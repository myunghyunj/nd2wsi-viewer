"""Viewer geometry shared by the main pane and the translucent window: functions, no state."""

import json
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
MODULE = STATIC / "viewer-geometry-v1.js"

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
    result = run_node(script, MODULE, check=True)
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
                 "imageToViewerElementPoint", "panToAstromapRoi", "resetTiledImage"):
        assert f"function {name}(" in app
    assert "window.Nd2ViewerGeometry.makeTileSource({" in app
    assert "window.Nd2ViewerGeometry.centerOnPreservingZoom(" in app
    reset_start = app.index("function resetTiledImage(")
    reset_end = app.index("\n}", reset_start) + 2
    assert "window.Nd2ViewerGeometry.resetTiledImage(" in app[reset_start:reset_end]
    for osd_call in ("imageToViewerElementCoordinates", "viewerElementToImageCoordinates", "getNumTiles",
                     "_setFullyLoaded", "tilesMatrix"):
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
    result = run_node(script, MODULE, check=True)
    out = json.loads(result.stdout)
    assert out["globals"] == ["Nd2ViewerGeometry"]
    assert out["exports"] == [
        "centerOnPreservingZoom",
        "createTileRefreshGate",
        "flipAwareElementPoint",
        "imageToViewerElementPoint",
        "isActiveTile",
        "makeTileSource",
        "normalizeQuery",
        "rectCornersToElementPoints",
        "resetTiledImage",
        "viewerElementToImagePoint",
    ]
    assert out["functions"] is True


def test_reset_tiled_image_clears_matrix_clip_fully_loaded_and_optional_image_loader():
    out = run("""
      const missing = [
        geometry.resetTiledImage(null),
        geometry.resetTiledImage(undefined),
        geometry.resetTiledImage({}),
      ];

      function makeItem({ drawArea = { x: 0, y: 0, width: 100, height: 100 }, clip = { x: 1, y: 2 } } = {}) {
        const events = [];
        const item = {
          tilesMatrix: { 0: { 0: { 0: { id: 'old' } } } },
          viewer: { imageLoader: { clear() { events.push('clearImageLoader'); } } },
          reset() { events.push('reset'); },
          getDrawArea() { events.push('getDrawArea'); return drawArea; },
          _setFullyLoaded(v) { events.push(['_setFullyLoaded', v]); },
          getClip() { events.push('getClip'); return clip; },
          setClip(c) { events.push(['setClip', c]); },
        };
        return { item, events };
      }

      const plain = makeItem();
      const plainOk = geometry.resetTiledImage(plain.item);

      const cleared = makeItem();
      const clearedOk = geometry.resetTiledImage(cleared.item, { clearImageLoader: true });

      const offscreenNull = makeItem({ drawArea: null });
      const offscreenNullOk = geometry.resetTiledImage(offscreenNull.item);

      const offscreenFalse = makeItem({ drawArea: false });
      const offscreenFalseOk = geometry.resetTiledImage(offscreenFalse.item);

      let minimalReset = 0;
      const minimalOk = geometry.resetTiledImage({ reset() { minimalReset++; } }, null);

      return {
        missing,
        plainOk,
        plainEvents: plain.events,
        plainMatrix: plain.item.tilesMatrix,
        clearedOk,
        clearedEvents: cleared.events,
        offscreenNullOk,
        offscreenNullEvents: offscreenNull.events,
        offscreenFalseOk,
        offscreenFalseEvents: offscreenFalse.events,
        minimalOk,
        minimalReset,
      };
    """)
    assert out["missing"] == [False, False, False]
    assert out["plainOk"] is True
    assert out["plainEvents"] == [
        "reset",
        "getDrawArea",
        ["_setFullyLoaded", False],
        "getClip",
        ["setClip", {"x": 1, "y": 2}],
    ]
    assert out["plainMatrix"] == {}
    assert out["clearedOk"] is True
    assert out["clearedEvents"] == [
        "clearImageLoader",
        "reset",
        "getDrawArea",
        ["_setFullyLoaded", False],
        "getClip",
        ["setClip", {"x": 1, "y": 2}],
    ]
    assert out["offscreenNullOk"] is True
    assert out["offscreenNullEvents"] == ["reset", "getDrawArea", "getClip", ["setClip", {"x": 1, "y": 2}]]
    assert out["offscreenFalseOk"] is True
    assert out["offscreenFalseEvents"] == ["reset", "getDrawArea", "getClip", ["setClip", {"x": 1, "y": 2}]]
    assert out["minimalOk"] is True
    assert out["minimalReset"] == 1


def test_normalize_query_strips_leading_question_mark_and_handles_non_strings():
    out = run("""
      const fs = require('fs'), vm = require('vm');
      const vmCtx = {};
      vm.createContext(vmCtx);
      vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), vmCtx);
      const nullProto = Object.create(null);
      return {
        empty: [
          geometry.normalizeQuery(),
          geometry.normalizeQuery(null),
          geometry.normalizeQuery(''),
          geometry.normalizeQuery('?'),
          geometry.normalizeQuery(0),
          geometry.normalizeQuery(false),
          geometry.normalizeQuery(nullProto),
        ],
        stripped: geometry.normalizeQuery('?c=0,1&win=0:255&g=g1'),
        encoded: geometry.normalizeQuery('c=0%2C1&win=0%3A255&g=g1'),
        paramsObj: geometry.normalizeQuery(new URLSearchParams('c=0&win=0:128')),
        numeric: geometry.normalizeQuery(42),
        noUrlSearchParams: vmCtx.Nd2ViewerGeometry.normalizeQuery('?c=0&g=1'),
      };
    """)
    assert out["empty"] == ["", "", "", "", "", "", ""]
    assert out["stripped"] == "c=0%2C1&win=0%3A255&g=g1"
    assert out["encoded"] == "c=0%2C1&win=0%3A255&g=g1"
    assert out["paramsObj"] == "c=0&win=0%3A128"
    assert out["numeric"] == "42="
    assert out["noUrlSearchParams"] == "c=0&g=1"


def test_is_active_tile_uses_tiles_matrix_identity_with_url_fallback():
    out = run("""
      const tileA = { level: 1, x: 2, y: 3, getUrl: () => 'api/tile/1/2/3.jpg?c=0&win=0:128' };
      const tileB = { level: 1, x: 2, y: 3, getUrl: () => 'api/tile/1/2/3.jpg?c=0&win=0:128' };
      const itemWithMatrix = { tilesMatrix: { 1: { 2: { 3: tileB } } } };
      const clearedItem = { tilesMatrix: {} };

      const mockTileMatch = { getUrl: () => 'api/tile/0/0/0.jpg?c=0,1&win=0:128' };
      const mockTileMiss = { getUrl: () => 'api/tile/0/0/0.jpg?c=0,1&win=0:255' };
      const mockTileBare = { getUrl: () => 'api/tile/0/0/0.jpg' };

      return {
        matrixActive: geometry.isActiveTile(itemWithMatrix, tileB, 'different'),
        matrixStaleSameUrl: geometry.isActiveTile(itemWithMatrix, tileA, 'c=0&win=0:128'),
        matrixCleared: geometry.isActiveTile(clearedItem, tileB, 'c=0&win=0:128'),
        urlFallbackMatch: geometry.isActiveTile({}, mockTileMatch, '?c=0%2C1&win=0%3A128'),
        urlFallbackMiss: geometry.isActiveTile(null, mockTileMiss, 'c=0,1&win=0:128'),
        urlFallbackBareEmpty: geometry.isActiveTile({}, mockTileBare, ''),
        urlFallbackBareMismatch: geometry.isActiveTile({}, mockTileBare, 'c=0'),
        minimalFallback: [
          geometry.isActiveTile({}, {}, 'c=0'),
          geometry.isActiveTile(null, null, 'c=0'),
        ],
      };
    """)
    assert out["matrixActive"] is True
    assert out["matrixStaleSameUrl"] is False
    assert out["matrixCleared"] is False
    assert out["urlFallbackMatch"] is True
    assert out["urlFallbackMiss"] is False
    assert out["urlFallbackBareEmpty"] is True
    assert out["urlFallbackBareMismatch"] is False
    assert out["minimalFallback"] == [True, True]


def test_tile_refresh_gate_coalesces_requests_and_preserves_a_b_a_revisits():
    out = run("""
      const log = [];
      let fullyLoaded = true;
      let loaderClears = 0;
      let redraws = 0;
      const item = {
        tilesMatrix: {},
        viewer: { imageLoader: { clear() { loaderClears++; } } },
        reset() { log.push(['reset', gate.getActiveQuery()]); },
        getDrawArea() { return { x: 0, y: 0, width: 100, height: 100 }; },
        _setFullyLoaded(v) { fullyLoaded = v; },
        getFullyLoaded() { return fullyLoaded; },
        getClip() { return null; },
        setClip() {},
      };
      const viewer = { forceRedraw() { redraws++; } };
      const gate = geometry.createTileRefreshGate({
        getItem: () => item,
        getViewer: () => viewer,
        onBeforeReset: () => log.push(['beforeReset', gate.getActiveQuery()]),
        onPaintComplete: (q) => log.push(['complete', q]),
      });

      gate.init('?c=0&win=0:255');
      const initQuery = gate.getActiveQuery();
      const dupIgnored = gate.request('c=0&win=0%3A255');
      const forceAccepted = gate.request('c=0&win=0:255', { force: true });
      const tileInit = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileInit } } };
      fullyLoaded = true;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileInit] });

      // A -> B -> C -> A while B is in flight
      const reqB = gate.request('?c=0&win=0:128');
      const duringB = gate.getActiveQuery();
      const reqC = gate.request('?c=0&win=0:96');
      const reqBackA = gate.request('?c=0&win=0:255');
      const tileB = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileB } } };
      fullyLoaded = true;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileB] });
      const activeAfterB = gate.getActiveQuery();
      const renderingAfterB = gate.isRendering();

      const tileA = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileA } } };
      fullyLoaded = true;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileA] });
      const settledRendering = gate.isRendering();
      const dupAfterA = gate.request('c=0&win=0:255');

      // A -> B -> B: pending matches newly painted query so no redundant reset
      const reqB2 = gate.request('c=0&win=0:128');
      const reqB2Same = gate.request('?c=0&win=0%3A128');
      const tileB2 = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileB2 } } };
      fullyLoaded = true;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileB2] });

      return {
        initQuery, dupIgnored, forceAccepted, reqB, duringB, reqC, reqBackA,
        activeAfterB, renderingAfterB, settledRendering, dupAfterA,
        reqB2, reqB2Same, finalRendering: gate.isRendering(),
        loaderClears, redraws, log,
      };
    """)
    assert out["initQuery"] == "c=0&win=0%3A255"
    assert out["dupIgnored"] is False
    assert out["forceAccepted"] is True
    assert out["reqB"] is True
    assert out["duringB"] == "c=0&win=0%3A128"
    assert out["reqC"] is True
    assert out["reqBackA"] is True
    assert out["activeAfterB"] == "c=0&win=0%3A255"
    assert out["renderingAfterB"] is True
    assert out["settledRendering"] is False
    assert out["dupAfterA"] is False
    assert out["reqB2"] is True
    assert out["reqB2Same"] is True
    assert out["finalRendering"] is False
    assert out["loaderClears"] == 4
    assert out["redraws"] == 4
    assert out["log"] == [
        ["beforeReset", "c=0&win=0%3A255"],
        ["reset", "c=0&win=0%3A255"],
        ["complete", "c=0&win=0%3A255"],
        ["beforeReset", "c=0&win=0%3A128"],
        ["reset", "c=0&win=0%3A128"],
        ["complete", "c=0&win=0%3A128"],
        ["beforeReset", "c=0&win=0%3A255"],
        ["reset", "c=0&win=0%3A255"],
        ["complete", "c=0&win=0%3A255"],
        ["beforeReset", "c=0&win=0%3A128"],
        ["reset", "c=0&win=0%3A128"],
        ["complete", "c=0&win=0%3A128"],
    ]


def test_tile_refresh_gate_waits_for_full_visible_coverage_and_active_tiles():
    out = run("""
      const completed = [];
      let fullyLoaded = false;
      const otherItem = {};
      const item = {
        tilesMatrix: {},
        reset() {},
        getDrawArea() { return { x: 0, y: 0, width: 200, height: 200 }; },
        _setFullyLoaded(v) { fullyLoaded = v; },
        getFullyLoaded() { return fullyLoaded; },
      };
      const gate = geometry.createTileRefreshGate({
        getItem: () => item,
        getViewer: () => ({ forceRedraw() {} }),
        onPaintComplete: (q) => completed.push(q),
      });

      const staleTile = { level: 1, x: 0, y: 0 };
      item.tilesMatrix = { 1: { 0: { 0: staleTile } } };
      gate.init('c=0&win=0:255');
      gate.request('c=0&win=0:128');
      gate.request('c=0&win=0:80');

      // Blank draw, wrong tiledImage, and stale tile must not complete.
      gate.onTiledImageDrawn({ tiledImage: otherItem, tiles: [staleTile] });
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [] });
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [staleTile] });
      const stillRenderingAfterStale = gate.isRendering();

      // First of two visible tiles draws while getFullyLoaded() is still false.
      const tile1 = { level: 1, x: 0, y: 0 };
      const tile2 = { level: 1, x: 1, y: 0 };
      item.tilesMatrix = { 1: { 0: { 0: tile1 }, 1: { 0: tile2 } } };
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tile1] });
      gate.onFullyLoadedChange({ fullyLoaded: false });
      const stillRenderingPartial = gate.isRendering();

      // Second tile finishes -> getFullyLoaded() becomes true -> completes via onTiledImageDrawn
      // and advances to queued win=0:80.
      fullyLoaded = true;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tile1, tile2] });
      const queryAfterFirst = gate.getActiveQuery();
      const renderingSecond = gate.isRendering();

      // Second render: fully-loaded-change before draw must NOT complete prematurely;
      // mixed frames with stale tiles must NOT complete; active draw completes cleanly.
      fullyLoaded = false;
      gate.onFullyLoadedChange({ fullyLoaded: true });
      const blockedByGetFullyLoaded = gate.isRendering();
      fullyLoaded = true;
      gate.onFullyLoadedChange({ fullyLoaded: true });
      const blockedBeforeDraw = gate.isRendering();
      const tile3 = { level: 1, x: 0, y: 0 };
      item.tilesMatrix = { 1: { 0: { 0: tile3 } } };
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tile3, staleTile] });
      const blockedByMixedFrame = gate.isRendering();
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tile3] });

      return {
        stillRenderingAfterStale,
        stillRenderingPartial,
        queryAfterFirst,
        renderingSecond,
        blockedByGetFullyLoaded,
        blockedBeforeDraw,
        blockedByMixedFrame,
        finalRendering: gate.isRendering(),
        completed,
      };
    """)
    assert out["stillRenderingAfterStale"] is True
    assert out["stillRenderingPartial"] is True
    assert out["queryAfterFirst"] == "c=0&win=0%3A80"
    assert out["renderingSecond"] is True
    assert out["blockedByGetFullyLoaded"] is True
    assert out["blockedBeforeDraw"] is True
    assert out["blockedByMixedFrame"] is True
    assert out["finalRendering"] is False
    assert out["completed"] == ["c=0&win=0%3A128", "c=0&win=0%3A80"]


def test_tile_refresh_gate_progress_watchdog_resets_on_tile_loaded_and_recovers_on_stall():
    out = run("""
      // Start timer IDs at 0 to prove falsy timer ID 0 is still cleared properly.
      let nextTimerId = 0;
      const activeTimers = new Map();
      const timerLog = [];
      const setTimer = (fn, ms) => {
        const id = nextTimerId++;
        activeTimers.set(id, { fn, ms });
        timerLog.push(['set', id, ms]);
        return id;
      };
      const clearTimer = (id) => {
        activeTimers.delete(id);
        timerLog.push(['clear', id]);
      };

      const completed = [];
      let fullyLoaded = false;
      const item = {
        tilesMatrix: {},
        reset() {},
        getDrawArea() { return { x: 0, y: 0, width: 100, height: 100 }; },
        _setFullyLoaded(v) { fullyLoaded = v; },
        getFullyLoaded() { return fullyLoaded; },
      };
      const gate = geometry.createTileRefreshGate({
        getItem: () => item,
        getViewer: () => ({ forceRedraw() {} }),
        onPaintComplete: (q) => completed.push(q),
        setTimer,
        clearTimer,
        stallTimeoutMs: 2000,
      });

      const staleTile = { level: 0, x: 0, y: 0 };
      gate.init('c=0&win=0:255');
      gate.request('c=0&win=0:128');
      const idAfterStart = [...activeTimers.keys()];

      // Stale tile progress must NOT reset the watchdog.
      gate.onTileLoaded({ tiledImage: item, tile: staleTile });
      const idsAfterStaleLoad = [...activeTimers.keys()];

      // Active tile progress clears timer 0 and arms timer 1.
      const activeTileB = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: activeTileB } } };
      gate.onTileLoaded({ tiledImage: item, tile: activeTileB });
      const idsAfterActiveLoad = [...activeTimers.keys()];

      // Queue return to original A (A -> B -> A) and expire B's watchdog.
      gate.request('c=0&win=0:255');
      const timer1 = activeTimers.get(1);
      activeTimers.delete(1);
      timer1.fn();
      const afterTimeoutActive = gate.getActiveQuery();
      const afterTimeoutRendering = gate.isRendering();
      const idsAfterTimeout = [...activeTimers.keys()];

      // Now expire A's watchdog with NO pending query, then verify late draw recovery.
      const timer2 = activeTimers.get(2);
      activeTimers.delete(2);
      timer2.fn();
      const afterSecondTimeoutRendering = gate.isRendering();
      // Late draw for A when fully loaded records paintedQuery = A so identical request becomes no-op.
      const activeTileA = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: activeTileA } } };
      fullyLoaded = true;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [activeTileA] });
      const dupAfterLateDraw = gate.request('c=0&win=0:255');

      // Also verify that after a timeout WITHOUT late draw, requesting prior A still resets!
      gate.request('c=0&win=0:64');
      const timer3 = activeTimers.get(3);
      activeTimers.delete(3);
      timer3.fn();
      const reqAfterUnpaintedTimeout = gate.request('c=0&win=0:255');

      return {
        idAfterStart,
        idsAfterStaleLoad,
        idsAfterActiveLoad,
        afterTimeoutActive,
        afterTimeoutRendering,
        idsAfterTimeout,
        afterSecondTimeoutRendering,
        dupAfterLateDraw,
        reqAfterUnpaintedTimeout,
        completed,
        timerLog,
      };
    """)
    assert out["idAfterStart"] == [0]
    assert out["idsAfterStaleLoad"] == [0]
    assert out["idsAfterActiveLoad"] == [1]
    assert out["afterTimeoutActive"] == "c=0&win=0%3A255"
    assert out["afterTimeoutRendering"] is True
    assert out["idsAfterTimeout"] == [2]
    assert out["afterSecondTimeoutRendering"] is False
    assert out["dupAfterLateDraw"] is False
    assert out["reqAfterUnpaintedTimeout"] is True
    assert out["completed"] == [None, None, "c=0&win=0%3A255", None]
    assert out["timerLog"][:4] == [
        ["set", 0, 2000],
        ["clear", 0],
        ["set", 1, 2000],
        ["set", 2, 2000],
    ]


def test_tile_refresh_gate_handles_offscreen_missing_item_errors_reset_state_and_dispose():
    out = run("""
      let nextId = 1;
      const timers = new Map();
      const cleared = [];
      const setTimer = (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; };
      const clearTimer = (id) => { timers.delete(id); cleared.push(id); };

      const completed = [];
      const errors = [];
      let drawArea = null;
      let currentItem = null;
      let redraws = 0;

      const makeItem = () => ({
        tilesMatrix: {},
        viewer: { imageLoader: { clear() {} } },
        reset() {},
        getDrawArea() { return drawArea; },
        _setFullyLoaded() {},
        getFullyLoaded() { return false; },
      });

      const gate = geometry.createTileRefreshGate({
        getItem: () => currentItem,
        getViewer: () => ({ forceRedraw() { redraws++; } }),
        onPaintComplete: (q) => completed.push(q),
        onActiveTileError: (ev) => errors.push(ev.message),
        setTimer,
        clearTimer,
      });

      // Missing item completes immediately without arming watchdog.
      gate.init('c=0');
      gate.request('c=1');
      const missingState = { rendering: gate.isRendering(), timers: timers.size, redraws };

      // Offscreen item (null and false drawArea) completes immediately without arming watchdog.
      currentItem = makeItem();
      drawArea = null;
      gate.request('c=2');
      const offscreenNull = { rendering: gate.isRendering(), timers: timers.size };
      drawArea = false;
      gate.request('c=3');
      const offscreenFalse = { rendering: gate.isRendering(), timers: timers.size };

      // Onscreen item: test stale vs active tile-load-failed.
      drawArea = { x: 0, y: 0, width: 100, height: 100 };
      const staleTile = { level: 0, x: 0, y: 0 };
      gate.request('c=4');
      gate.request('c=5'); // queued pending
      const activeTile = { level: 0, x: 0, y: 0 };
      currentItem.tilesMatrix = { 0: { 0: { 0: activeTile } } };

      const staleFailed = gate.onTileLoadFailed({ tiledImage: currentItem, tile: staleTile, message: 'stale' });
      const wrongItemFailed = gate.onTileLoadFailed({ tiledImage: {}, tile: activeTile, message: 'wrong-item' });
      const activeFailed = gate.onTileLoadFailed({ tiledImage: currentItem, tile: activeTile, message: 'active-404' });
      const advancedAfterError = gate.getActiveQuery();
      const renderingAfterError = gate.isRendering();

      // resetState clears pending/rendering/watchdog and records normalized query.
      gate.resetState('?c=9&g=g2');
      const afterResetState = {
        active: gate.getActiveQuery(),
        rendering: gate.isRendering(),
        timers: timers.size,
        dupNoop: gate.request('c=9&g=g2'),
      };

      // dispose clears watchdog and ignores all further requests and events.
      gate.request('c=10');
      const timersBeforeDispose = timers.size;
      gate.dispose();
      const afterDispose = {
        rendering: gate.isRendering(),
        timers: timers.size,
        reqIgnored: gate.request('c=11'),
        failIgnored: gate.onTileLoadFailed({ tiledImage: currentItem, tile: activeTile, message: 'after-dispose' }),
      };

      return {
        missingState,
        offscreenNull,
        offscreenFalse,
        staleFailed,
        wrongItemFailed,
        activeFailed,
        advancedAfterError,
        renderingAfterError,
        afterResetState,
        timersBeforeDispose,
        afterDispose,
        completed,
        errors,
        clearedCount: cleared.length,
      };
    """)
    assert out["missingState"] == {"rendering": False, "timers": 0, "redraws": 1}
    assert out["offscreenNull"] == {"rendering": False, "timers": 0}
    assert out["offscreenFalse"] == {"rendering": False, "timers": 0}
    assert out["staleFailed"] is False
    assert out["wrongItemFailed"] is False
    assert out["activeFailed"] is True
    assert out["advancedAfterError"] == "c=5"
    assert out["renderingAfterError"] is True
    assert out["afterResetState"] == {
        "active": "c=9&g=g2",
        "rendering": False,
        "timers": 0,
        "dupNoop": False,
    }
    assert out["timersBeforeDispose"] == 1
    assert out["afterDispose"] == {
        "rendering": False,
        "timers": 0,
        "reqIgnored": False,
        "failIgnored": False,
    }
    assert out["completed"] == ["c=1", "c=2", "c=3"]
    assert out["errors"] == ["active-404"]
    assert out["clearedCount"] >= 3


def test_tile_refresh_gate_works_in_timer_free_vm_sandbox():
    script = r"""
const fs = require('fs'), vm = require('vm');
const sandbox = { URLSearchParams };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const out = vm.runInContext(`(() => {
  const g = Nd2ViewerGeometry;
  const completed = [];
  let loaded = false;
  const item = {
    tilesMatrix: {},
    reset() {},
    getDrawArea() { return { x: 0, y: 0, width: 64, height: 64 }; },
    _setFullyLoaded(v) { loaded = v; },
    getFullyLoaded() { return loaded; },
  };
  const gate = g.createTileRefreshGate({
    getItem: () => item,
    getViewer: () => ({ forceRedraw() {} }),
    onPaintComplete: (q) => completed.push(q),
  });
  gate.init('?c=0');
  const started = gate.request('?c=1');
  const tile = { level: 0, x: 0, y: 0 };
  item.tilesMatrix = { 0: { 0: { 0: tile } } };
  gate.onTileLoaded({ tiledImage: item, tile });
  loaded = true;
  gate.onTiledImageDrawn({ tiledImage: item, tiles: [tile] });
  gate.dispose();
  return {
    hasSetTimeout: typeof setTimeout,
    started,
    active: gate.getActiveQuery(),
    rendering: gate.isRendering(),
    completed,
  };
})()`, sandbox);
process.stdout.write(JSON.stringify(out));
"""
    result = run_node(script, MODULE, check=True)
    out = json.loads(result.stdout)
    assert out == {
        "hasSetTimeout": "undefined",
        "started": True,
        "active": "c=1",
        "rendering": False,
        "completed": ["c=1"],
    }


def test_tile_refresh_gate_paint_complete_ordering_reverse_order_offscreen_and_redraw():
    out = run("""
      const events = [];
      let fullyLoaded = false;
      let needsDraw = true;
      let drawArea = { x: 0, y: 0, width: 256, height: 256 };
      let redraws = 0;
      const otherItem = {};
      const item = {
        tilesMatrix: {},
        reset() { needsDraw = true; },
        getDrawArea() { return drawArea; },
        _setFullyLoaded(v) { fullyLoaded = v; },
        getFullyLoaded() { return fullyLoaded; },
        needsDraw() { return needsDraw; },
      };
      const viewer = { forceRedraw() { redraws += 1; } };
      const gate = geometry.createTileRefreshGate({
        getItem: () => item,
        getViewer: () => viewer,
        onBeforeReset: (_item, meta) => events.push(['before', gate.getActiveQuery(), meta]),
        onPaintComplete: (q, meta) => events.push(['complete', q, meta]),
      });

      gate.init('c=0&win=0:255');

      // 1) Normal OSD 5.0.1 order: tile-loaded -> fully-loaded-change BEFORE tiled-image-drawn.
      gate.request('c=0&win=0:128');
      gate.request('c=0&win=0:64');
      const tileB = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileB } } };
      gate.onTileLoaded({ tiledImage: item, tile: tileB });
      fullyLoaded = true;
      // Wrong eventSource and false fullyLoaded must be ignored.
      gate.onFullyLoadedChange({ eventSource: otherItem, fullyLoaded: true });
      gate.onFullyLoadedChange({ eventSource: item, fullyLoaded: false });
      // Real TiledImage fully-loaded-change fires BEFORE draw: must NOT advance gate!
      gate.onFullyLoadedChange({ eventSource: item, fullyLoaded: true });
      // A subsequent fullyLoaded: false clears fullyLoadedReady until fullyLoaded becomes true again.
      fullyLoaded = false;
      gate.onFullyLoadedChange({ eventSource: item, fullyLoaded: false });
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileB] });
      const stillRenderingAfterResetReady = gate.isRendering();
      gate.onTileLoaded({ tiledImage: item, tile: tileB });
      fullyLoaded = true;
      gate.onFullyLoadedChange({ eventSource: item, fullyLoaded: true });
      const beforeDrawState = {
        active: gate.getActiveQuery(),
        rendering: gate.isRendering(),
        completes: events.filter(e => e[0] === 'complete').length,
      };
      // Now tiled-image-drawn paints active tiles -> completes B with hasPending: true and starts C.
      needsDraw = false;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileB] });
      const afterBDraw = {
        active: gate.getActiveQuery(),
        rendering: gate.isRendering(),
      };

      // 2) Partial draw then 2nd tile loads -> dirtySinceDraw blocks premature completion.
      const tileC1 = { level: 0, x: 0, y: 0 };
      const tileC2 = { level: 0, x: 1, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileC1 }, 1: { 0: tileC2 } } };
      gate.onTileLoaded({ tiledImage: item, tile: tileC1 });
      fullyLoaded = false;
      needsDraw = false;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileC1] });
      // Second tile arrives -> dirtySinceDraw becomes true; even with needsDraw=false, must wait for draw.
      gate.onTileLoaded({ tiledImage: item, tile: tileC2 });
      fullyLoaded = true;
      gate.onFullyLoadedChange({ eventSource: item, fullyLoaded: true });
      const blockedByDirtySinceDraw = gate.isRendering();
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileC1, tileC2] });
      const afterCDraw = gate.isRendering();

      // 3) Reverse order: tiled-image-drawn first, then fully-loaded-change with !dirtySinceDraw & !needsDraw().
      gate.request('c=0&win=0:32');
      const tileD = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileD } } };
      gate.onTileLoaded({ tiledImage: item, tile: tileD });
      fullyLoaded = false;
      needsDraw = false;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileD] });
      const stillWaitingReverse = gate.isRendering();
      fullyLoaded = true;
      gate.onFullyLoadedChange({ eventSource: item, fullyLoaded: true });
      const doneAfterReverse = gate.isRendering();

      // 4) fully-loaded-change with !drawnActive and needsDraw() === false requests a redraw tick.
      gate.request('c=0&win=0:16');
      const redrawsBefore = redraws;
      needsDraw = false;
      fullyLoaded = true;
      gate.onFullyLoadedChange({ eventSource: item, fullyLoaded: true });
      const redrawTriggered = redraws - redrawsBefore;
      const waitingForRedrawTick = gate.isRendering();
      const tileE = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileE } } };
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileE] });

      // 5) fully-loaded-change completes immediately if item moved offscreen mid-render.
      gate.request('c=0&win=0:8');
      drawArea = null;
      fullyLoaded = true;
      gate.onFullyLoadedChange({ eventSource: item, fullyLoaded: true });
      const doneOffscreen = gate.isRendering();
      drawArea = { x: 0, y: 0, width: 256, height: 256 };

      // 6) Pending identical to active query (A -> B -> B) reports hasPending: false.
      gate.request('c=0&win=0:4');
      gate.request('?c=0&win=0%3A4');
      const tileF = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileF } } };
      fullyLoaded = true;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileF] });

      return {
        stillRenderingAfterResetReady,
        beforeDrawState,
        afterBDraw,
        blockedByDirtySinceDraw,
        afterCDraw,
        stillWaitingReverse,
        doneAfterReverse,
        redrawTriggered,
        waitingForRedrawTick,
        doneOffscreen,
        events,
      };
    """)
    assert out["stillRenderingAfterResetReady"] is True
    assert out["beforeDrawState"] == {
        "active": "c=0&win=0%3A128",
        "rendering": True,
        "completes": 0,
    }
    assert out["afterBDraw"] == {"active": "c=0&win=0%3A64", "rendering": True}
    assert out["blockedByDirtySinceDraw"] is True
    assert out["afterCDraw"] is False
    assert out["stillWaitingReverse"] is True
    assert out["doneAfterReverse"] is False
    assert out["redrawTriggered"] == 1
    assert out["waitingForRedrawTick"] is True
    assert out["doneOffscreen"] is False
    completes = [e for e in out["events"] if e[0] == "complete"]
    assert completes == [
        ["complete", "c=0&win=0%3A128", {"timedOut": False, "hasPending": True}],
        ["complete", "c=0&win=0%3A64", {"timedOut": False, "hasPending": False}],
        ["complete", "c=0&win=0%3A32", {"timedOut": False, "hasPending": False}],
        ["complete", "c=0&win=0%3A16", {"timedOut": False, "hasPending": False}],
        ["complete", "c=0&win=0%3A8", {"timedOut": False, "hasPending": False}],
        ["complete", "c=0&win=0%3A4", {"timedOut": False, "hasPending": False}],
    ]
    befores = [e for e in out["events"] if e[0] == "before"]
    assert all(entry[2] == {"hadPainted": True} for entry in befores)


def test_tile_refresh_gate_watchdog_metadata_had_painted_and_reset_state_unpainted():
    out = run("""
      let nextId = 1;
      const timers = new Map();
      const setTimer = (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; };
      const clearTimer = (id) => { timers.delete(id); };

      const beforeLog = [];
      const completeLog = [];
      let fullyLoaded = false;
      const item = {
        tilesMatrix: {},
        reset() {},
        getDrawArea() { return { x: 0, y: 0, width: 100, height: 100 }; },
        _setFullyLoaded(v) { fullyLoaded = v; },
        getFullyLoaded() { return fullyLoaded; },
      };
      const gate = geometry.createTileRefreshGate({
        getItem: () => item,
        getViewer: () => ({ forceRedraw() {} }),
        onBeforeReset: (_item, meta) => beforeLog.push([gate.getActiveQuery(), meta]),
        onPaintComplete: (q, meta) => completeLog.push([q, meta]),
        setTimer,
        clearTimer,
        stallTimeoutMs: 2000,
      });

      gate.init('c=0&win=0:255');
      gate.request('c=0&win=0:128');
      gate.request('c=0&win=0:64');
      // Expire B's watchdog with C pending -> timedOut: true, hasPending: true, then C starts with hadPainted: false.
      const t1 = [...timers.keys()][0];
      const cb1 = timers.get(t1).fn;
      timers.delete(t1);
      cb1();

      // Expire C's watchdog with NO pending -> timedOut: true, hasPending: false.
      const t2 = [...timers.keys()][0];
      const cb2 = timers.get(t2).fn;
      timers.delete(t2);
      cb2();

      // Late draw recovers paintedQuery = C and fires timedOut: false, hasPending: false.
      const tileC = { level: 0, x: 0, y: 0 };
      item.tilesMatrix = { 0: { 0: { 0: tileC } } };
      fullyLoaded = true;
      gate.onTiledImageDrawn({ tiledImage: item, tiles: [tileC] });
      const dupAfterLateDraw = gate.request('c=0&win=0:64');

      // resetState(q, { painted: false }) updates activeQuery, clears timers/rendering, and clears paintedQuery.
      gate.request('c=0&win=0:32');
      const timersBeforeReset = timers.size;
      gate.resetState('?c=1&g=g2', { painted: false });
      const afterUnpaintedReset = {
        active: gate.getActiveQuery(),
        rendering: gate.isRendering(),
        timers: timers.size,
      };
      // Requesting the same query still resets because paintedQuery is null, with hadPainted: false.
      const reqSameAfterUnpainted = gate.request('c=1&g=g2');

      // Default resetState(q) marks paintedQuery = activeQuery so identical request is a no-op.
      gate.resetState('?c=2&g=g2');
      const reqSameAfterPainted = gate.request('c=2&g=g2');

      return {
        beforeLog,
        completeLog,
        dupAfterLateDraw,
        timersBeforeReset,
        afterUnpaintedReset,
        reqSameAfterUnpainted,
        reqSameAfterPainted,
      };
    """)
    assert out["beforeLog"] == [
        ["c=0&win=0%3A128", {"hadPainted": True}],
        ["c=0&win=0%3A64", {"hadPainted": False}],
        ["c=0&win=0%3A32", {"hadPainted": True}],
        ["c=1&g=g2", {"hadPainted": False}],
    ]
    assert out["completeLog"] == [
        [None, {"timedOut": True, "hasPending": True}],
        [None, {"timedOut": True, "hasPending": False}],
        ["c=0&win=0%3A64", {"timedOut": False, "hasPending": False}],
    ]
    assert out["dupAfterLateDraw"] is False
    assert out["timersBeforeReset"] == 1
    assert out["afterUnpaintedReset"] == {"active": "c=1&g=g2", "rendering": False, "timers": 0}
    assert out["reqSameAfterUnpainted"] is True
    assert out["reqSameAfterPainted"] is False
