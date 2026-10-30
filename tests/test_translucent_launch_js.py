"""Launch the exact displayed fluorescence frame without copying user geometry."""

import json
from pathlib import Path

import pytest
from node_host import run_node

APP = Path(__file__).resolve().parents[1] / "nd2wsi/static/app.js"


def launch(**options):
    script = r"""
    const fs=require('fs'),vm=require('vm'), config=JSON.parse(process.argv[2]);
    const source=fs.readFileSync(process.argv[1],'utf8');
    function production(name) {
      const start=source.indexOf('function '+name+'(');
      if(start<0) throw Error('Missing function '+name);
      return source.slice(start,source.indexOf('\n}',start)+2);
    }
    const out={calls:[],toasts:[]};
    const viewport={getCenter:()=>({x:0.4,y:0.3}),
      viewportToImageCoordinates:()=>({x:123.5,y:240.25}),getZoom:()=>2,
      viewportToImageZoom:()=>0.75,getRotation:()=>90,getFlip:()=>true};
    const state={info:{rgb:!!config.rgb,raster:!!config.raster},plate:config.plate?{focus:config.grid?null:2}:null,
      viewer:{viewport},quitPreparing:!!config.closing,
      roi:{x:1,y:2,w:3,h:4},annotations:config.annotations||[{id:'must-not-copy'}],
      annReady:!!config.annotations,astromap:{activeId:config.selected}};
    const native={open_translucent_viewer:async(sid,value)=>{
      out.calls.push({sid,value});await Promise.resolve();
      if(config.reject) throw Error('Window unavailable');
      return config.error?{ok:false,message:'Not supported'}:{ok:true};
    }};
    const window={};window.parent=config.standalone?window:{pywebview:{api:native}};
    if(config.standalone) window.pywebview={api:native};
    if(config.browser) {window.parent=window;delete window.pywebview;}
    const context={state,window,postViewportState(){},showToast:m=>out.toasts.push(m),
      spatialImageReady:()=>config.ready!==false,
      activeFrameParams:()=>config.grid?null:{t:3,p:2,z:4},
      annotationOwnerReady:()=>config.ownerReady!==false,
      tileQuery:()=>'?c=1&win=3%2C100%2C0.25&g=fixture&t=3&p=2&z=4',
      currentSlideSid:()=> '1234abcd'};
    vm.createContext(context);
    vm.runInContext(production('captureTranslucentView')+'\n'+production('openTranslucentViewer'),context);
    (async()=>{await Promise.all([context.openTranslucentViewer(),context.openTranslucentViewer()]);out.opening=!!state.translucentOpening;
      out.roi=state.roi;out.annotations=state.annotations;
      process.stdout.write(JSON.stringify(out));})();
    """
    result = run_node(script, APP, json.dumps(options), check=True, timeout=10)
    return json.loads(result.stdout)


@pytest.mark.parametrize("options", [{}, {"plate": True}, {"standalone": True},
                                     {"rgb": True, "raster": True}])
def test_opens_once_with_current_camera_lut_and_plane_without_roi(options):
    result = launch(**options)
    assert len(result["calls"]) == 1
    assert result["calls"][0] == {"sid": "1234abcd", "value": {
        "version": 1, "query": "?c=1&win=3%2C100%2C0.25&g=fixture&t=3&p=2&z=4",
        "center": [123.5, 240.25], "zoom": .75, "rotation": 90, "flip": True,
    }}
    assert result["roi"] == {"x": 1, "y": 2, "w": 3, "h": 4}
    assert result["annotations"] == [{"id": "must-not-copy"}]
    assert not result["opening"] and not result["toasts"]


@pytest.mark.parametrize("options,reason", [
    ({"plate": True, "grid": True}, "Choose a site"),
    ({"rgb": True}, "fluorescence"),
    ({"ready": False}, "finish opening"),
    ({"closing": True}, "closing"),
    ({"browser": True}, "desktop app"),
])
def test_invalid_launch_does_not_open_a_window(options, reason):
    result = launch(**options)
    assert result["calls"] == []
    assert reason in result["toasts"][0]
    assert not result["opening"]


@pytest.mark.parametrize("options,reason", [({"error": True}, "Not supported"),
                                           ({"reject": True}, "Window unavailable")])
def test_native_failure_is_visible_and_launch_guard_is_released(options, reason):
    result = launch(**options)
    assert result["toasts"] == [reason]
    assert not result["opening"]


def test_only_current_frame_imported_roi_snapshot_is_passed_without_mutation():
    annotations = [
        {"id": "manual", "type": "box", "x": 1, "y": 2, "w": 3, "h": 4},
        {"id": "imported", "type": "box", "x": 10, "y": 20, "w": 7, "h": 4,
         "astromap": True, "astromapImportIndex": 42, "text": "chosen"},
    ]
    result = launch(annotations=annotations, selected="imported")
    snapshot = result["calls"][0]["value"]
    assert snapshot["rois"] == [{"x": 10, "y": 20, "w": 7, "h": 4, "index": 1, "label": "chosen"}]
    assert snapshot["roiIndex"] == 0
    assert result["annotations"] == annotations
    other_frame = launch(annotations=annotations, ownerReady=False)
    assert "rois" not in other_frame["calls"][0]["value"]
