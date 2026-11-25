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
    const state={info:config.noInfo?null:{rgb:!!config.rgb,raster:!!config.raster,
      ...(config.annotationScope?{plate:{annotationScope:config.annotationScope}}:{})},
      plate:config.plate?{focus:config.grid?null:2}:null,
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
                                     {"rgb": True}, {"rgb": True, "raster": True}])
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
    ({"noInfo": True}, "image first"),
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


def test_only_current_frame_enabled_astromap_roi_snapshot_is_passed_without_mutation():
    annotations = [
        {"id": "manual-box", "type": "box", "x": 1, "y": 2, "w": 3, "h": 4},
        {"id": "disabled-astromap", "type": "box", "x": 4, "y": 5, "w": 6, "h": 7,
         "astromap": True, "astromapEnabled": False, "text": ""},
        {"id": "manual-astromap", "type": "box", "x": 30, "y": 40, "w": 12, "h": 14,
         "astromap": True, "astromapEnabled": True, "text": ""},
        {"id": "manual-named", "type": "box", "x": 50, "y": 60, "w": 8, "h": 9,
         "astromap": True, "astromapEnabled": True, "text": "fiber"},
        {"id": "imported", "type": "box", "x": 10, "y": 20, "w": 7, "h": 4,
         "astromap": True, "astromapImportIndex": 42, "text": "chosen"},
    ]
    result = launch(annotations=annotations, selected="manual-astromap")
    snapshot = result["calls"][0]["value"]
    assert snapshot["rois"] == [
        {"x": 30, "y": 40, "w": 12, "h": 14, "index": 1, "label": "2"},
        {"x": 50, "y": 60, "w": 8, "h": 9, "index": 2, "label": "fiber"},
        {"x": 10, "y": 20, "w": 7, "h": 4, "index": 3, "label": "chosen"},
    ]
    assert snapshot["roiIndex"] == 0
    assert result["annotations"] == annotations
    other_frame = launch(annotations=annotations, ownerReady=False)
    assert "rois" not in other_frame["calls"][0]["value"]
    assert "laser_items" not in other_frame["calls"][0]["value"]


def test_plane_scope_and_existing_laser_shots_sync_only_when_owner_ready():
    annotations = [
        {"id": "manual", "type": "box", "x": 1, "y": 2, "w": 3, "h": 4},
        {"id": "laser-1", "type": "box", "x": 15, "y": 25, "w": 10, "h": 12,
         "text": "SLACS_ROI #1", "color": "#a0a6ae", "slacs": {"kind": "laser_shot", "shots": 3, "isolation": 1}},
    ]
    result = launch(plate=True, annotationScope="plane", annotations=annotations)
    snapshot = result["calls"][0]["value"]
    assert snapshot["annotation_scope"] == "plane"
    assert len(snapshot["laser_items"]) == 1
    assert snapshot["laser_items"][0]["id"] == "laser-1"
    assert snapshot["laser_items"][0]["slacs"] == {"kind": "laser_shot", "shots": 3, "isolation": 1}
    assert (snapshot["laser_items"][0]["x"], snapshot["laser_items"][0]["y"],
            snapshot["laser_items"][0]["w"], snapshot["laser_items"][0]["h"]) == (15, 25, 10, 12)
    unready = launch(plate=True, annotationScope="plane", annotations=annotations, ownerReady=False)
    assert unready["calls"][0]["value"]["annotation_scope"] == "plane"
    assert "laser_items" not in unready["calls"][0]["value"]


STATIC = APP.parent


def _run_live_sync(body: str, config: dict | None = None):
    harness = r"""
    const fs = require('fs'), path = require('path'), vm = require('vm');
    const source = fs.readFileSync(process.argv[1], 'utf8');
    const config = JSON.parse(process.argv[2]);
    function production(name) {
      const start = source.indexOf('function ' + name + '(');
      if (start < 0) throw Error('Missing function ' + name);
      return source.slice(start, source.indexOf('\n}', start) + 2);
    }
    const serializer = source.slice(
      source.indexOf('function lutParam('),
      source.indexOf('function activeFrameParams(')
    );
    const out = { opens: [], updates: [], toasts: [], resets: [], navResets: 0, platePaints: 0, reopens: 0 };
    const viewport = {
      getCenter: () => ({ x: 0.5, y: 0.5 }),
      viewportToImageCoordinates: () => ({ x: 100, y: 200 }),
      getZoom: () => 2,
      viewportToImageZoom: () => 1,
      getRotation: () => 0,
      getFlip: () => false,
    };
    const item = {
      reset() { out.resets.push(context.tileQuery()); },
      tilesMatrix: {},
      getDrawArea: () => ({ x: 0, y: 0, width: 10, height: 10 }),
      _setFullyLoaded() {},
      getClip: () => null,
      setClip() {},
    };
    const navItem = {
      reset() { out.navResets += 1; },
      tilesMatrix: {},
      getDrawArea: () => ({ x: 0, y: 0, width: 10, height: 10 }),
      _setFullyLoaded() {},
      getClip: () => null,
      setClip() {},
    };
    const viewer = {
      viewport,
      world: { getItemAt: (i) => (i === 0 ? item : null) },
      navigator: { world: { getItemAt: (i) => (i === 0 ? navItem : null) } },
      forceRedraw() {},
    };
    const state = {
      info: {
        rgb: !!config.rgb,
        raster: !!config.raster,
        dtype: config.dtype || 'uint8',
        generation: config.generation ?? 'g1',
        channels: config.channels || [
          { label: 'Ch0', color: '00FF00', metadataColor: '00FF00', window: { min: 0, max: 255, start: 0, end: 255 } },
          { label: 'Ch1', color: 'FF00FF', metadataColor: 'FF00FF', window: { min: 0, max: 255, start: 0, end: 255 } },
        ],
      },
      channels: config.enabled || [0, 1],
      luts: config.luts || [null, null],
      plate: config.plate || null,
      viewer,
      viewportRelay: { reopening: false },
      quitPreparing: false,
      annotations: [],
      annReady: false,
      astromap: { activeId: null },
      translucentSync: {
        launchSeq: 0, launchToken: null, generation: '',
        revision: 0, acceptedQuery: null, inflight: false,
        retryTimer: null, retryCount: 0,
      },
      tileRefreshGate: null,
      navRefreshPending: false,
    };
    const native = {
      open_translucent_viewer: async (sid, snapshot) => {
        out.opens.push({ sid, snapshot });
        return { ok: true, id: 'win-1', launch_token: 'tok-' + out.opens.length };
      },
      update_translucent_display: async (sid, token, revision, query) => {
        out.updates.push({ sid, token, revision, query });
        return { ok: true, applied: true, revision };
      },
    };
    const window = {
      Nd2ViewerGeometry: {
        resetTiledImage(target) { target.reset(); return true; },
      },
      parent: { pywebview: { api: native } },
    };
    const context = {
      state, window, URLSearchParams, Promise, Error,
      postViewportState() {},
      showToast: (m) => out.toasts.push(m),
      spatialImageReady: () => true,
      activeFrameParams: () => (state.plate ? (state.plate.focus === null ? null : { t: state.plate.t, p: state.plate.focus, z: state.plate.z }) : null),
      backingFrameParams: () => (state.plate ? { t: state.plate.t, p: state.plate.focus ?? 0, z: state.plate.z } : null),
      appendFrameParams: (q, f) => {
        if (f) { q.set('t', String(f.t)); q.set('p', String(f.p)); q.set('z', String(f.z)); }
        return q;
      },
      annotationOwnerReady: () => false,
      currentSlideSid: () => 'sid-1',
      paintPlate: () => { out.platePaints += 1; },
      reopenPreservingView: () => { out.reopens += 1; },
    };
    vm.createContext(context);
    const retryConstStart = source.indexOf('const TRANSLUCENT_RETRY_DELAYS_MS =');
    const retryConst = source.slice(retryConstStart, source.indexOf(';', retryConstStart) + 1);
    vm.runInContext(
      retryConst + '\n' +
      serializer + '\n' +
      production('tileQuery') + '\n' +
      production('captureTranslucentView') + '\n' +
      production('cancelTranslucentRetry') + '\n' +
      production('openTranslucentViewer') + '\n' +
      production('clearTranslucentSync') + '\n' +
      production('expireTranslucentSync') + '\n' +
      production('publishTranslucentDisplay') + '\n' +
      production('flushNavigatorTiles') + '\n' +
      production('refreshTiles') + '\n' +
      production('resetTiledImage'),
      context
    );
    const flush = async (n = 16) => { for (let i = 0; i < n; i++) await Promise.resolve(); };
    const deferred = () => {
      let resolve, reject;
      const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
      return { promise, resolve, reject };
    };
    (async () => {
    """ + body + r"""
    })().then((res) => process.stdout.write(JSON.stringify(res)))
      .catch((err) => { console.error(err); process.exit(1); });
    """
    result = run_node(harness, APP, json.dumps(config or {}), check=True, timeout=10)
    return json.loads(result.stdout)


def test_display_params_and_display_query_cover_defaults_none_gamma_and_colors():
    out = _run_live_sync(r"""
      const defaults = {
        display: context.displayQuery(),
        render: context.renderParams(new URLSearchParams()).toString(),
        tile: context.tileQuery(),
      };
      state.channels = [];
      state.luts = [{ lo: 10, hi: 128, gamma: 1.5 }, null];
      state.info.channels[1].color = '00FFFF';
      const custom = {
        display: context.displayQuery(),
        render: context.renderParams(new URLSearchParams()).toString(),
        tile: context.tileQuery(),
      };
      state.channels = [1];
      state.info.rgb = true;
      const rgbDisplay = context.displayQuery();
      return { defaults, custom, rgbDisplay };
    """, config={"plate": {"t": 1, "z": 3, "focus": 2}})
    assert out["defaults"]["display"] == ""
    assert out["defaults"]["render"] == "g=g1"
    assert out["defaults"]["tile"] == "?g=g1&t=1&p=2&z=3"
    assert out["custom"]["display"] == "c=none&win=10%3A128%3A1.5%2C&col=%2C00FFFF"
    assert out["custom"]["render"] == "c=none&win=10%3A128%3A1.5%2C&col=%2C00FFFF&g=g1"
    assert out["custom"]["tile"] == "?c=none&win=10%3A128%3A1.5%2C&col=%2C00FFFF&g=g1&t=1&p=2&z=3"
    assert "col=" not in out["rgbDisplay"]
    assert "c=1" in out["rgbDisplay"]


def test_invalid_reopen_preserves_existing_live_overlay_subscription():
    out = _run_live_sync(r"""
      await context.openTranslucentViewer();
      const before = { ...state.translucentSync };
      // Switch to plate grid: captureTranslucentView throws before mutating translucentSync.
      state.plate.focus = null;
      const reopened = await context.openTranslucentViewer();
      const afterFailedReopen = { ...state.translucentSync };
      // Edits while in plate grid still publish display-only updates to the live overlay!
      state.luts[0] = { lo: 0, hi: 128, gamma: 1 };
      context.refreshTiles();
      await flush();
      return { before, reopened, afterFailedReopen, toasts: out.toasts, updates: out.updates, stale: state.plate.stale };
    """, config={"plate": {"t": 1, "z": 3, "focus": 2}})
    assert out["reopened"] is False
    assert "Choose a site" in out["toasts"][0]
    assert out["afterFailedReopen"] == out["before"]
    assert out["stale"] is True
    assert out["updates"] == [
        {"sid": "sid-1", "token": "tok-1", "revision": 1, "query": "win=0%3A128%2C"}
    ]


def test_mid_open_edit_catches_up_including_restore_to_empty_defaults():
    out = _run_live_sync(r"""
      state.luts = [{ lo: 0, hi: 128, gamma: 1 }, null];
      const gate = deferred();
      native.open_translucent_viewer = async (sid, snapshot) => {
        out.opens.push({ sid, snapshot });
        return gate.promise;
      };
      const opening = context.openTranslucentViewer();
      await flush(2);
      // User resets back to default "" while open_translucent_viewer is still pending.
      state.luts = [null, null];
      context.refreshTiles();
      const duringOpenCalls = out.updates.length;
      gate.resolve({ ok: true, id: 'win-1', launch_token: 'tok-open' });
      await opening;
      await flush();
      // Frame-only change afterwards must be a zero-bridge no-op.
      state.plate.t = 4;
      state.plate.z = 5;
      context.refreshTiles();
      await flush();
      return {
        duringOpenCalls,
        updates: out.updates,
        sync: { ...state.translucentSync },
      };
    """, config={"plate": {"t": 1, "z": 3, "focus": 2}})
    assert out["duringOpenCalls"] == 0
    assert out["updates"] == [
        {"sid": "sid-1", "token": "tok-open", "revision": 1, "query": ""}
    ]
    assert out["sync"]["launchToken"] == "tok-open"
    assert out["sync"]["acceptedQuery"] == ""
    assert out["sync"]["revision"] == 1
    assert out["sync"]["inflight"] is False


def test_single_flight_drain_coalesces_and_resolves_a_b_a_and_stops_on_invalid_or_reject():
    out = _run_live_sync(r"""
      await context.openTranslucentViewer();
      const first = deferred();
      const second = deferred();
      const calls = [];
      native.update_translucent_display = async (sid, token, revision, query) => {
        calls.push({ sid, token, revision, query });
        if (revision === 1) return first.promise;
        if (revision === 2) return second.promise;
        if (revision === 3) return { ok: false, invalid: true, message: 'Bad win' };
        if (revision === 4) throw new Error('Bridge disconnected');
        return { ok: true, applied: true, revision };
      };
      // A ("") -> B -> C -> A while B is in flight.
      state.luts = [{ lo: 0, hi: 192, gamma: 1 }, null];
      context.refreshTiles();
      state.luts = [{ lo: 0, hi: 128, gamma: 1 }, null];
      context.refreshTiles();
      state.luts = [null, null];
      context.refreshTiles();
      const inflightDuringB = calls.length;
      first.resolve({ ok: true, applied: true, revision: 1 });
      await flush();
      const inflightDuringA = calls.length;
      second.resolve({ ok: true, applied: true, revision: 2 });
      await flush();
      const afterABA = { ...state.translucentSync };
      // Invalid response stops loop without tight retry and keeps acceptedQuery unchanged.
      state.luts = [{ lo: 0, hi: 80, gamma: 1 }, null];
      context.refreshTiles();
      await flush();
      const afterInvalid = { ...state.translucentSync };
      // Rejected bridge call also stops cleanly; a subsequent edit can still recover.
      state.luts = [{ lo: 0, hi: 90, gamma: 1 }, null];
      context.refreshTiles();
      await flush();
      const afterReject = { ...state.translucentSync };
      state.luts = [{ lo: 0, hi: 100, gamma: 1 }, null];
      context.refreshTiles();
      await flush();
      const afterRecover = { ...state.translucentSync };
      return { inflightDuringB, inflightDuringA, calls, afterABA, afterInvalid, afterReject, afterRecover };
    """)
    assert out["inflightDuringB"] == 1
    assert out["inflightDuringA"] == 2
    assert [(c["revision"], c["query"]) for c in out["calls"]] == [
        (1, "win=0%3A192%2C"),
        (2, ""),
        (3, "win=0%3A80%2C"),
        (4, "win=0%3A90%2C"),
        (5, "win=0%3A100%2C"),
    ]
    assert out["afterABA"]["acceptedQuery"] == "" and out["afterABA"]["inflight"] is False
    assert out["afterInvalid"]["acceptedQuery"] == "" and out["afterInvalid"]["inflight"] is False
    assert out["afterReject"]["acceptedQuery"] == "" and out["afterReject"]["inflight"] is False
    assert out["afterRecover"]["acceptedQuery"] == "win=0%3A100%2C" and out["afterRecover"]["inflight"] is False


def test_reopen_launch_seq_isolates_superseded_drain_from_new_session():
    out = _run_live_sync(r"""
      await context.openTranslucentViewer();
      const oldUpdate = deferred();
      native.update_translucent_display = async (sid, token, revision, query) => {
        out.updates.push({ sid, token, revision, query });
        if (token === 'tok-1') return oldUpdate.promise;
        return { ok: true, applied: true, revision };
      };
      state.luts = [{ lo: 0, hi: 180, gamma: 1 }, null];
      context.refreshTiles();
      // Reopen while tok-1 update is still in flight.
      await context.openTranslucentViewer();
      // Late stale reply from tok-1 must not retire tok-2.
      oldUpdate.resolve({ ok: false, stale: true });
      await flush();
      const afterOldSettle = { ...state.translucentSync };
      state.luts = [{ lo: 0, hi: 120, gamma: 1 }, null];
      context.refreshTiles();
      await flush();
      return { afterOldSettle, finalSync: { ...state.translucentSync }, updates: out.updates };
    """)
    assert out["afterOldSettle"]["launchToken"] == "tok-2"
    assert out["afterOldSettle"]["inflight"] is False
    assert out["finalSync"]["launchToken"] == "tok-2"
    assert out["finalSync"]["acceptedQuery"] == "win=0%3A120%2C"
    assert out["updates"] == [
        {"sid": "sid-1", "token": "tok-1", "revision": 1, "query": "win=0%3A180%2C"},
        {"sid": "sid-1", "token": "tok-2", "revision": 1, "query": "win=0%3A120%2C"},
    ]


@pytest.mark.parametrize("reply", [
    {"ok": False, "closed": True},
    {"ok": False, "stale": True},
    {"ok": False, "stale_generation": True},
])
def test_lazy_retirement_stops_bridge_traffic_after_closed_stale_or_generation_expiry(reply):
    out = _run_live_sync(r"""
      await context.openTranslucentViewer();
      native.update_translucent_display = async (sid, token, revision, query) => {
        out.updates.push({ sid, token, revision, query });
        return config.reply;
      };
      state.luts = [{ lo: 0, hi: 180, gamma: 1 }, null];
      context.refreshTiles();
      await flush();
      // Subsequent edits must perform zero bridge calls.
      state.luts = [{ lo: 0, hi: 90, gamma: 1 }, null];
      context.refreshTiles();
      await flush();
      return { updates: out.updates, sync: { ...state.translucentSync } };
    """, config={"reply": reply})
    assert len(out["updates"]) == 1
    assert out["sync"]["launchToken"] is None
    assert out["sync"]["inflight"] is False


def test_shading_generation_mismatch_and_refresh_notify_expiry_only_after_commit():
    script = r"""
    const fs = require('fs'), vm = require('vm');
    const source = fs.readFileSync(process.argv[1], 'utf8');
    function production(name, asyncPrefix = false) {
      const prefix = (asyncPrefix ? 'async ' : '') + 'function ' + name + '(';
      const start = source.indexOf(prefix);
      if (start < 0) throw Error('Missing ' + prefix);
      return source.slice(start, source.indexOf('\n}', start) + 2);
    }
    const retryConstStart = source.indexOf('const TRANSLUCENT_RETRY_DELAYS_MS =');
    const retryConst = source.slice(retryConstStart, source.indexOf(';', retryConstStart) + 1);
    const serializer = source.slice(
      source.indexOf('function lutParam('),
      source.indexOf('function activeFrameParams(')
    );
    const updates = [];
    const tokensDuringUpdate = [];
    const info = {
      generation: 'g1', kind: 'slide', width: 100, height: 80, dtype: 'uint8',
      channels: [{ label: 'FITC', color: '00FF00', window: { min: 0, max: 255, start: 0, end: 255 } }],
      levels: [],
    };
    const viewport = {
      getCenter: () => ({ x: 0.5, y: 0.5 }),
      viewportToImageCoordinates: () => ({ x: 50, y: 40 }),
      getZoom: () => 1,
      viewportToImageZoom: () => 1,
      getRotation: () => 0,
      getFlip: () => false,
    };
    const state = {
      info, viewer: { viewport }, channels: [0], luts: [null], lutAutoPending: [false], inspect: null,
      quitPreparing: false, annotations: [], annReady: false, astromap: { activeId: null },
      translucentSync: {
        launchSeq: 1, launchToken: 'tok-1', generation: 'g1',
        revision: 0, acceptedQuery: '', inflight: false, retryTimer: null, retryCount: 0,
      },
    };
    let nextPayload = { ...info, generation: 'g2', width: 999 }; // invalid geometry
    let openResolve = null;
    const native = {
      open_translucent_viewer: () => new Promise((resolve) => { openResolve = resolve; }),
      update_translucent_display: async (...a) => {
        tokensDuringUpdate.push(state.translucentSync.launchToken);
        updates.push(a);
        return { ok: false, stale_generation: true };
      },
    };
    const window = {
      Nd2LutTransfer: { currentLut: (ch) => ({ lo: ch.window.start, hi: ch.window.end, gamma: 1 }) },
      parent: { pywebview: { api: native } },
    };
    const ctx = vm.createContext({
      state, window, URLSearchParams, Promise, Error, Object, Array,
      activeFrameContext: () => ({ key: 's1:' + state.info.generation }),
      activeFrameParams: () => null,
      backingFrameParams: () => null,
      appendFrameParams: (q) => q,
      spatialImageReady: () => true,
      annotationOwnerReady: () => false,
      postViewportState() {},
      showToast() {},
      currentSlideSid: () => 's1',
      fetch: async () => ({ ok: true, json: async () => nextPayload }),
      frameRequests: { pixels: { invalidate() {} }, histograms: { schedule() {} } },
      $: () => ({ replaceChildren() {} }),
      buildLevelLamps() {}, updateScaleStrip() {}, reopenPreservingView: async () => true,
    });
    vm.runInContext(
      retryConst + '\n' +
      serializer + '\n' +
      production('tileQuery') + '\n' +
      production('captureTranslucentView') + '\n' +
      production('cancelTranslucentRetry') + '\n' +
      production('clearTranslucentSync') + '\n' +
      production('expireTranslucentSync') + '\n' +
      production('openTranslucentViewer') + '\n' +
      production('publishTranslucentDisplay') + '\n' +
      production('refreshShadingPyramid', true),
      ctx
    );
    (async () => {
      let validationError = '';
      try { await ctx.refreshShadingPyramid('g2', { key: 's1:g1' }); } catch (e) { validationError = e.message; }
      const tokenAfterFailedShading = state.translucentSync.launchToken;
      const updatesAfterFailedShading = updates.slice();

      nextPayload = { ...info, generation: 'g2', channels: [{ ...info.channels[0] }] };
      await ctx.refreshShadingPyramid('g2', { key: 's1:g1' });
      const tokenAfterCommittedShading = state.translucentSync.launchToken;
      const updatesAfterCommittedShading = updates.slice();

      // Direct generation mismatch in publishTranslucentDisplay expires with rev + 1 and clears sync first.
      state.translucentSync.launchToken = 'tok-stale';
      state.translucentSync.generation = 'g1';
      state.translucentSync.revision = 2;
      state.luts = [{ lo: 0, hi: 100, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      for (let i = 0; i < 6; i++) await Promise.resolve();
      const tokenAfterMismatchPublish = state.translucentSync.launchToken;
      const updatesAfterMismatchPublish = updates.slice();

      // Generation changing mid-openTranslucentViewer expires the newly returned launch_token.
      const opening = ctx.openTranslucentViewer();
      for (let i = 0; i < 4; i++) await Promise.resolve();
      state.info.generation = 'g3';
      openResolve({ ok: true, id: 'win-race', launch_token: 'tok-race' });
      await opening;
      for (let i = 0; i < 6; i++) await Promise.resolve();

      process.stdout.write(JSON.stringify({
        validationError,
        tokenAfterFailedShading,
        updatesAfterFailedShading,
        tokenAfterCommittedShading,
        updatesAfterCommittedShading,
        tokenAfterMismatchPublish,
        updatesAfterMismatchPublish,
        tokenAfterRaceOpen: state.translucentSync.launchToken,
        tokensDuringUpdate,
        updates,
      }));
    })();
    """
    out = json.loads(run_node(script, APP, check=True, timeout=10).stdout)
    assert "does not match" in out["validationError"]
    assert out["tokenAfterFailedShading"] == "tok-1"
    assert out["updatesAfterFailedShading"] == []
    assert out["tokenAfterCommittedShading"] is None
    assert out["updatesAfterCommittedShading"] == [["s1", "tok-1", 1, ""]]
    assert out["tokenAfterMismatchPublish"] is None
    assert out["updatesAfterMismatchPublish"] == [
        ["s1", "tok-1", 1, ""],
        ["s1", "tok-stale", 3, ""],
    ]
    assert out["tokenAfterRaceOpen"] is None
    assert out["updates"] == [
        ["s1", "tok-1", 1, ""],
        ["s1", "tok-stale", 3, ""],
        ["s1", "tok-race", 1, ""],
    ]
    assert out["tokensDuringUpdate"] == [None, None, None]


def test_color_input_throttles_through_apply_luts_and_pointer_capture_error_is_safe():
    script = r"""
    const fs = require('fs'), path = require('path'), vm = require('vm');
    const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');
    const LutControls = require(path.join(process.argv[1], 'lut-controls-v1.js'));
    const canvasCtx = new Proxy({}, { get: (o, k) => o[k] || (() => {}) });
    const nodes = [];
    function makeNode(tag) {
      const handlers = {}, attrs = {}, classes = new Set();
      const n = {
        tag, handlers, attrs, style: {}, children: [], value: '', disabled: false, hidden: false,
        dataset: {}, textContent: '', title: '',
        classList: {
          add: (c) => classes.add(c), remove: (c) => classes.delete(c),
          toggle: (c, on) => { const v = on === undefined ? !classes.has(c) : !!on; v ? classes.add(c) : classes.delete(c); },
        },
        append(...xs) { this.children.push(...xs); },
        remove() {},
        setAttribute(k, v) { attrs[k] = String(v); },
        addEventListener(k, fn) { handlers[k] = fn; },
        removeEventListener() {},
        getContext: () => canvasCtx,
        setPointerCapture() { throw new Error('NotFoundError'); },
        getBoundingClientRect: () => ({ left: 0, top: 0, bottom: 20, right: 240, width: 240, height: 84 }),
      };
      nodes.push(n);
      return n;
    }
    let lutUpdates = 0, lutFlushes = 0, directRefreshes = 0;
    const state = {
      info: {
        rgb: false, dtype: 'uint8',
        channels: [{ label: 'FITC', color: '00FF00', window: { min: 0, max: 255, start: 0, end: 255 } }],
      },
      luts: [null],
      lutWidgets: [],
    };
    const body = makeNode('body');
    const ctx = vm.createContext({
      state,
      window: { Nd2LutControls: LutControls, devicePixelRatio: 1, innerWidth: 800, innerHeight: 600, addEventListener() {}, removeEventListener() {} },
      document: { body, createElement: makeNode, addEventListener() {}, removeEventListener() {} },
      $: () => null,
      refreshTiles: () => { directRefreshes += 1; },
      applyLuts: Object.assign(() => { lutUpdates += 1; }, { flush: () => { lutFlushes += 1; } }),
    });
    vm.runInContext(
      source.slice(source.indexOf('const CHANNEL_COLOR_PRESETS'), source.indexOf('function buildChannelPanel(')),
      ctx
    );
    const anchor = makeNode('button');
    const pop = ctx.openChannelColorPopover(0, anchor);
    const colorInput = pop.children[2].children[1];
    colorInput.value = '#112233';
    colorInput.handlers.input();
    colorInput.value = '#445566';
    colorInput.handlers.input();
    colorInput.handlers.change();
    const afterCustom = { lutUpdates, lutFlushes, directRefreshes, color: state.info.channels[0].color };
    // Preset click still refreshes immediately.
    pop.children[1].children[0].handlers.click();
    const afterPreset = { lutUpdates, lutFlushes, directRefreshes, color: state.info.channels[0].color };

    // Verify lut-controls-v1.js pointerdown survives setPointerCapture throwing NotFoundError.
    let widgetChanged = null, widgetFlushed = 0;
    const widget = LutControls.createWidget({
      channel: { label: 'Ch0', color: 'FFFFFF', window: { min: 0, max: 255, start: 0, end: 255 } },
      dtype: 'uint8', label: { textContent: '' }, width: 240,
      document: { createElement: makeNode },
      window: { devicePixelRatio: 1, location: { origin: 'http://x' }, parent: {}, addEventListener() {}, removeEventListener() {} },
      protocolVersion: 2, inkColor: () => '#fff', currentTheme: () => 'dark', fmtInt: String,
      onChange: (l) => { widgetChanged = l; },
      onFlush: () => { widgetFlushed += 1; },
      onManualAxis() {}, onShiftChange() {},
    });
    const lutCanvas = widget.element.children[0];
    lutCanvas.handlers.pointerdown({ button: 0, clientX: 236, clientY: 8, pointerId: 7, preventDefault() {} });
    lutCanvas.handlers.pointermove({ clientX: 120, clientY: 8 });
    lutCanvas.handlers.pointerup();
    process.stdout.write(JSON.stringify({ afterCustom, afterPreset, widgetChanged, widgetFlushed }));
    """
    out = json.loads(run_node(script, STATIC, check=True, timeout=10).stdout)
    assert out["afterCustom"] == {"lutUpdates": 2, "lutFlushes": 1, "directRefreshes": 0, "color": "445566"}
    assert out["afterPreset"] == {"lutUpdates": 2, "lutFlushes": 1, "directRefreshes": 1, "color": "00FFFF"}
    assert out["widgetChanged"] is not None and 120 <= out["widgetChanged"]["hi"] <= 135
    assert out["widgetFlushed"] == 1


def test_refresh_tiles_defers_navigator_on_drag_ticks_and_flushes_on_release():
    out = _run_live_sync(r"""
      state.luts = [{ lo: 0, hi: 180, gamma: 1 }, null];
      context.refreshTiles({ flushNavigator: false });
      const midDrag = { resets: out.resets.length, navResets: out.navResets, pending: state.navRefreshPending };
      context.refreshTiles({ flushNavigator: true });
      const afterRelease = { resets: out.resets.length, navResets: out.navResets, pending: state.navRefreshPending };
      return { midDrag, afterRelease };
    """)
    assert out["midDrag"] == {"resets": 1, "navResets": 0, "pending": True}
    assert out["afterRelease"] == {"resets": 2, "navResets": 1, "pending": False}


def test_build_viewer_wires_paint_gate_max_tiles_and_filters_tile_errors():
    script = r"""
    const fs = require('fs'), vm = require('vm');
    const source = fs.readFileSync(process.argv[1], 'utf8');
    function production(name, asyncPrefix = false) {
      const prefix = (asyncPrefix ? 'async ' : '') + 'function ' + name + '(';
      const start = source.indexOf(prefix);
      if (start < 0) throw Error('Missing ' + prefix);
      return source.slice(start, source.indexOf('\n}', start) + 2);
    }
    let osdConfig = null;
    const handlers = new Map();
    const events = [];
    let toasts = 0, navFlushes = 0, gateOpts = null, gateInited = null;
    const viewer = {
      viewport: { zoomBy() { return { applyConstraints() {} }; }, goHome() {} },
      world: { getItemAt: () => null },
      addHandler(name, fn) {
        if (!handlers.has(name)) handlers.set(name, []);
        handlers.get(name).push(fn);
      },
    };
    const fakeGate = {
      init(q) { gateInited = q; },
      onTileLoaded(ev) { events.push(['loaded', ev.id]); },
      onTiledImageDrawn(ev) { events.push(['drawn', ev.id]); },
      onFullyLoadedChange(ev) { events.push(['full', ev.fullyLoaded]); },
      onTileLoadFailed(ev) {
        events.push(['failed', ev.id, !!ev.active]);
        if (ev.active) gateOpts.onActiveTileError(ev);
        return !!ev.active;
      },
      dispose() { events.push(['disposed']); },
    };
    const elements = new Map();
    const $ = (id) => {
      if (!elements.has(id)) elements.set(id, { style: {}, addEventListener() {}, onclick: null });
      return elements.get(id);
    };
    const state = {
      info: { kind: 'slide', seriesIndex: 0, seriesCount: 2, name: 's.nd2' },
      viewer: null,
      plate: null,
      viewportRelay: {},
      quitPreparing: false,
      sceneSwitching: false,
      lutPersistence: { isDirty: () => false, flush: async () => {} },
      translucentSync: { launchSeq: 1, launchToken: 'tok-scene', generation: 'g1', revision: 0, acceptedQuery: '', inflight: false },
      tileRefreshGate: null,
      navRefreshPending: false,
    };
    const win = {
      devicePixelRatio: 1,
      Nd2ViewerGeometry: {
        createTileRefreshGate(opts) { gateOpts = opts; return fakeGate; },
      },
      addEventListener() {},
      parent: { postMessage() {} },
    };
    const ctx = vm.createContext({
      state, window: win, $, setTimeout, clearTimeout,
      OpenSeadragon: (cfg) => { osdConfig = cfg; return viewer; },
      makeTileSource: () => ({}),
      tileQuery: () => '?g=g1',
      flushNavigatorTiles: () => { navFlushes += 1; },
      debounceToast: () => () => { toasts += 1; },
      renderAnnotations() {}, updateAlignmentDrag() {}, finishAlignmentDrag() {},
      beginAlignmentDrag() {}, elementPoint() {}, viewerElementToImagePoint() {},
      updateCursor() {}, updateScalebar() {}, moveRoiOverlay() {},
      refreshAnnotationsForViewport() {}, scheduleViewportState() {},
      updateReadout() {}, restoreRoiOverlay() {}, postViewportState() {},
      refreshAstromapPanel() {}, applyDesiredDisplayTransform() {}, showToast() {},
      annotationStore: { flushForUpdate: async () => {} },
      closeEditor() {}, setScenePickerOpen() {}, renderScenePicker() {}, resolveSceneUrl: (d) => d.url,
      currentSlideSid: () => 's1', VIEWPORT_PROTOCOL_VERSION: 2,
      location: { origin: 'http://127.0.0.1:8000' },
      fetch: async () => ({ ok: true, json: async () => ({ ok: true, sid: 's2', url: '/t/s/s2/' }) }),
    });
    vm.runInContext(
      production('clearTranslucentSync') + '\n' +
      production('selectScene', true) + '\n' +
      production('buildViewer'),
      ctx
    );
    ctx.buildViewer();
    handlers.get('tile-loaded').forEach((fn) => fn({ id: 1 }));
    handlers.get('tiled-image-drawn').forEach((fn) => fn({ id: 2 }));
    handlers.get('fully-loaded-change').forEach((fn) => fn({ fullyLoaded: true }));
    handlers.get('tile-load-failed').forEach((fn) => fn({ id: 'stale', active: false }));
    handlers.get('tile-load-failed').forEach((fn) => fn({ id: 'live', active: true }));
    state.navRefreshPending = true;
    gateOpts.onPaintComplete('g=g1');
    (async () => {
      await ctx.selectScene(1);
      process.stdout.write(JSON.stringify({
        maxTilesPerFrame: osdConfig.maxTilesPerFrame,
        imageLoaderLimit: osdConfig.imageLoaderLimit,
        gateInited,
        events,
        toasts,
        navFlushes,
        tokenAfterSceneSwitch: state.translucentSync.launchToken,
      }));
    })();
    """
    out = json.loads(run_node(script, APP, check=True, timeout=10).stdout)
    assert out["maxTilesPerFrame"] == 6
    assert out["imageLoaderLimit"] == 6
    assert out["gateInited"] == "?g=g1"
    assert out["events"] == [
        ["loaded", 1],
        ["drawn", 2],
        ["full", True],
        ["failed", "stale", False],
        ["failed", "live", True],
    ]
    assert out["toasts"] == 1
    assert out["navFlushes"] == 1
    assert out["tokenAfterSceneSwitch"] is None


def test_make_tile_source_pins_urls_to_gate_active_query_and_build_viewer_inits_before_osd():
    script = r"""
    const fs = require('fs'), path = require('path'), vm = require('vm');
    const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');
    const geometry = require(path.join(process.argv[1], 'viewer-geometry-v1.js'));
    function production(name) {
      const start = source.indexOf('function ' + name + '(');
      if (start < 0) throw Error('Missing function ' + name);
      return source.slice(start, source.indexOf('\n}', start) + 2);
    }
    const order = [];
    let liveQuery = '?c=0&g=g1';
    let capturedSource = null;
    const worldHandlers = new Map();
    const viewerHandlers = new Map();
    const pagehideHandlers = [];
    const itemEvents = [];
    function makeItem(id) {
      const h = new Map();
      return {
        id,
        addHandler(name, fn) {
          itemEvents.push(['add', id, name]);
          if (!h.has(name)) h.set(name, []);
          h.get(name).push(fn);
        },
        removeHandler(name, fn) {
          itemEvents.push(['remove', id, name]);
          if (h.has(name)) h.set(name, h.get(name).filter((f) => f !== fn));
        },
        raise(name, ev) { (h.get(name) || []).forEach((fn) => fn(ev)); },
      };
    }
    let currentItem = null;
    const viewer = {
      viewport: { zoomBy() { return { applyConstraints() {} }; }, goHome() {} },
      world: {
        getItemAt: (i) => (i === 0 ? currentItem : null),
        addHandler(name, fn) {
          if (!worldHandlers.has(name)) worldHandlers.set(name, []);
          worldHandlers.get(name).push(fn);
        },
        removeHandler(name, fn) {
          if (worldHandlers.has(name)) worldHandlers.set(name, worldHandlers.get(name).filter((f) => f !== fn));
        },
      },
      addHandler(name, fn) {
        if (!viewerHandlers.has(name)) viewerHandlers.set(name, []);
        viewerHandlers.get(name).push(fn);
      },
    };
    const gateLog = [];
    let gateOpts = null;
    let activeQuery = '';
    const fakeGate = {
      init(q) { order.push(['gate-init', q]); activeQuery = String(q || '').replace(/^\?/, ''); },
      getActiveQuery() { return activeQuery; },
      onTileLoaded(ev) { gateLog.push(['loaded', ev]); },
      onTiledImageDrawn(ev) { gateLog.push(['drawn', ev]); },
      onFullyLoadedChange(ev) { gateLog.push(['full', ev.from]); },
      onTileLoadFailed() { return false; },
    };
    const elements = new Map();
    const $ = (id) => {
      if (!elements.has(id)) elements.set(id, { style: {}, addEventListener() {}, onclick: null });
      return elements.get(id);
    };
    let navFlushes = 0;
    const state = {
      info: {
        kind: 'slide', width: 512, height: 512, tileSize: 256,
        levels: [{ width: 512, height: 512, path: '0' }],
      },
      viewer: null,
      plate: null,
      viewportRelay: {},
      tileRefreshGate: null,
      navRefreshPending: false,
    };
    const OpenSeadragon = (cfg) => {
      order.push(['osd-ctor', state.tileRefreshGate === fakeGate, cfg.tileSources.getTileUrl(0, 0, 0)]);
      capturedSource = cfg.tileSources;
      return viewer;
    };
    OpenSeadragon.Point = function (x, y) { this.x = x; this.y = y; };
    const win = {
      devicePixelRatio: 1,
      Nd2ViewerGeometry: {
        makeTileSource: geometry.makeTileSource,
        createTileRefreshGate(opts) { order.push(['gate-create']); gateOpts = opts; return fakeGate; },
      },
      addEventListener(name, fn) { if (name === 'pagehide') pagehideHandlers.push(fn); },
    };
    const ctx = vm.createContext({
      state, window: win, $, setTimeout, clearTimeout, OpenSeadragon,
      tileQuery: () => liveQuery,
      flushNavigatorTiles: () => { navFlushes += 1; state.navRefreshPending = false; },
      debounceToast: () => () => {},
      renderAnnotations() {}, updateAlignmentDrag() {}, finishAlignmentDrag() {},
      beginAlignmentDrag() {}, elementPoint() {}, viewerElementToImagePoint() {},
      updateCursor() {}, updateScalebar() {}, moveRoiOverlay() {},
      refreshAnnotationsForViewport() {}, scheduleViewportState() {},
      updateReadout() {}, restoreRoiOverlay() {}, postViewportState() {},
      refreshAstromapPanel() {}, applyDesiredDisplayTransform() {}, showToast() {},
    });
    vm.runInContext(production('makeTileSource') + '\n' + production('buildViewer'), ctx);
    ctx.buildViewer();

    // While activeQuery is pinned to c=0&g=g1, changing liveQuery must not leak into tile URLs.
    liveQuery = '?c=1&g=g1';
    const pinnedUrl = capturedSource.getTileUrl(0, 0, 0);
    // Empty activeQuery yields bare tile URL without trailing '?'.
    activeQuery = '';
    const emptyActiveUrl = capturedSource.getTileUrl(0, 0, 0);
    // Without a gate, makeTileSource falls back to live tileQuery().
    const savedGate = state.tileRefreshGate;
    state.tileRefreshGate = null;
    const fallbackUrl = capturedSource.getTileUrl(0, 0, 0);
    state.tileRefreshGate = savedGate;

    // TiledImage add-item / remove-item / open wiring for fully-loaded-change.
    const itemA = makeItem('A');
    currentItem = itemA;
    worldHandlers.get('add-item').forEach((fn) => fn({ item: itemA }));
    itemA.raise('fully-loaded-change', { fullyLoaded: true, from: 'itemA' });
    const itemB = makeItem('B');
    currentItem = itemB;
    worldHandlers.get('remove-item').forEach((fn) => fn({ item: itemA }));
    itemA.raise('fully-loaded-change', { fullyLoaded: true, from: 'itemA-stale' });
    itemB.raise('fully-loaded-change', { fullyLoaded: true, from: 'itemB' });
    // Fallback open wiring attaches itemC if add-item wasn't emitted.
    const itemC = makeItem('C');
    currentItem = itemC;
    viewerHandlers.get('open')[0]();
    itemC.raise('fully-loaded-change', { fullyLoaded: true, from: 'itemC' });
    pagehideHandlers.forEach((fn) => fn());
    itemC.raise('fully-loaded-change', { fullyLoaded: true, from: 'itemC-after-pagehide' });

    // onPaintComplete metadata gating for navigator flush.
    state.navRefreshPending = true;
    gateOpts.onPaintComplete(null, { timedOut: true, hasPending: false });
    const flushAfterNull = navFlushes;
    gateOpts.onPaintComplete('c=0&g=g1', { timedOut: true, hasPending: false });
    const flushAfterTimedOut = navFlushes;
    gateOpts.onPaintComplete('c=0&g=g1', { timedOut: false, hasPending: true });
    const flushAfterHasPending = navFlushes;
    gateOpts.onPaintComplete('c=0&g=g1', { timedOut: false, hasPending: false });
    const flushAfterFinal = navFlushes;

    process.stdout.write(JSON.stringify({
      order, pinnedUrl, emptyActiveUrl, fallbackUrl,
      gateLog, itemEvents,
      flushes: [flushAfterNull, flushAfterTimedOut, flushAfterHasPending, flushAfterFinal],
    }));
    """
    out = json.loads(run_node(script, STATIC, check=True, timeout=10).stdout)
    assert out["order"] == [
        ["gate-create"],
        ["gate-init", "?c=0&g=g1"],
        ["osd-ctor", True, "api/tile/0/0/0.jpg?c=0&g=g1"],
    ]
    assert out["pinnedUrl"] == "api/tile/0/0/0.jpg?c=0&g=g1"
    assert out["emptyActiveUrl"] == "api/tile/0/0/0.jpg"
    assert out["fallbackUrl"] == "api/tile/0/0/0.jpg?c=1&g=g1"
    assert out["gateLog"] == [["full", "itemA"], ["full", "itemB"], ["full", "itemC"]]
    assert out["flushes"] == [0, 0, 0, 1]


def test_plate_focus_reopen_drift_and_deferred_navigator_gate_sync():
    script = r"""
    const fs = require('fs'), path = require('path'), vm = require('vm');
    const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');
    const geometry = require(path.join(process.argv[1], 'viewer-geometry-v1.js'));
    function production(name) {
      const start = source.indexOf('function ' + name + '(');
      if (start < 0) throw Error('Missing function ' + name);
      return source.slice(start, source.indexOf('\n}', start) + 2);
    }
    const serializer = source.slice(
      source.indexOf('function lutParam('),
      source.indexOf('function activeFrameParams(')
    );
    const gateCalls = [];
    const viewerHandlers = new Map();
    let navFlushes = 0;
    let activeQuery = 'g=g1&t=0&p=0&z=0';
    let gateBusy = false;
    const item = { reset() {}, tilesMatrix: {}, getDrawArea: () => ({ x: 0, y: 0, width: 10, height: 10 }), getClip: () => null, setClip() {} };
    const navItem = { reset() { navFlushes += 1; }, tilesMatrix: {}, getDrawArea: () => ({ x: 0, y: 0, width: 10, height: 10 }), getClip: () => null, setClip() {} };
    const viewer = {
      viewport: {
        getBounds: () => ({ x: 0, y: 0, width: 1, height: 1 }),
        fitBounds() {},
        goHome() {},
      },
      world: { getItemAt: (i) => (i === 0 ? item : null) },
      navigator: { world: { getItemAt: (i) => (i === 0 ? navItem : null) }, update() {} },
      addHandler(name, fn) {
        if (!viewerHandlers.has(name)) viewerHandlers.set(name, []);
        viewerHandlers.get(name).push(fn);
      },
      removeHandler(name, fn) {
        if (viewerHandlers.has(name)) viewerHandlers.set(name, viewerHandlers.get(name).filter((f) => f !== fn));
      },
      open() {},
      forceRedraw() {},
    };
    const fakeGate = {
      getActiveQuery: () => activeQuery,
      resetState(q, opts) {
        gateCalls.push(['resetState', q, opts || null]);
        activeQuery = geometry.normalizeQuery(q);
      },
      request(q, opts) {
        gateCalls.push(['request', q, opts || null]);
        if (!gateBusy) activeQuery = geometry.normalizeQuery(q);
        return true;
      },
    };
    const state = {
      info: {
        kind: 'plate', generation: 'g1', dtype: 'uint8',
        plate: { T: 2, P: 4, Z: 3 },
        channels: [{ label: 'Ch0', color: '00FF00', metadataColor: '00FF00', window: { min: 0, max: 255, start: 0, end: 255 } }],
      },
      channels: [0],
      luts: [null],
      plate: { t: 1, z: 2, focus: null, everFocused: false, stale: false },
      viewer,
      viewportRelay: {
        timer: null, reopening: false, contextChanging: false, suppressUntil: 0,
        commandGate: { latestCommandSeq: 1, envelope: () => ({ generation: 'g1' }) },
      },
      tileRefreshGate: fakeGate,
      navRefreshPending: false,
      quitPreparing: false,
      sceneSwitching: false,
      editingId: null,
      tool: null,
      roi: null,
    };
    const ctx = vm.createContext({
      state, URLSearchParams, Promise, Date, Number, JSON,
      window: { Nd2ViewerGeometry: geometry },
      clamp: (v, lo, hi) => Math.max(lo, Math.min(hi, v)),
      $: () => ({ classList: { toggle() {} } }),
      clearTimeout() {},
      backingFrameParams: () => ({ t: state.plate.t, p: state.plate.focus ?? 0, z: state.plate.z }),
      appendFrameParams: (q, f) => { q.set('t', String(f.t)); q.set('p', String(f.p)); q.set('z', String(f.z)); return q; },
      annotationStore: { saveDeparting: () => true, resetOwner() {} },
      refreshSpatialContext() {},
      setPlatePlaying() {},
      layoutPlate() {},
      loadAnnotations() {},
      paintPlate() {},
      plateFrameChanged() { ctx.refreshTiles(); },
      updateReadout() {},
      postSpatialReadiness() {},
      postViewportState() {},
      applyDesiredDisplayTransform() {},
      restoreRoiOverlay() {},
      makeTileSource: () => ({}),
      publishTranslucentDisplay() {},
    });
    vm.runInContext(
      serializer + '\n' +
      production('tileQuery') + '\n' +
      production('resetTiledImage') + '\n' +
      production('flushNavigatorTiles') + '\n' +
      production('reopenPreservingView') + '\n' +
      production('refreshTiles') + '\n' +
      production('setPlateFocus'),
      ctx
    );

    // 1. Grid refreshTiles keeps stale=true and syncs gate with { painted: false }.
    state.luts = [{ lo: 0, hi: 200, gamma: 1 }];
    ctx.refreshTiles();
    const afterGridRefresh = { stale: state.plate.stale, calls: gateCalls.splice(0) };

    // 2. Entering plate focus resets gate with valid g/t/p/z and { painted: false } (not ""), then requests.
    ctx.setPlateFocus(2);
    const afterEnterFocus = { calls: gateCalls.splice(0), activeQuery };

    // 3. While gate is busy rendering an older query, flushNavigator=true must defer navigator flush.
    gateBusy = true;
    navFlushes = 0;
    state.navRefreshPending = false;
    state.luts = [{ lo: 0, hi: 120, gamma: 1 }];
    ctx.refreshTiles({ flushNavigator: true });
    const afterBusyRelease = {
      navFlushes,
      navRefreshPending: state.navRefreshPending,
      calls: gateCalls.splice(0),
    };
    // When idle and activeQuery matches, flushNavigator=true flushes immediately.
    gateBusy = false;
    state.luts = [{ lo: 0, hi: 90, gamma: 1 }];
    ctx.refreshTiles({ flushNavigator: true });
    const afterIdleRelease = {
      navFlushes,
      navRefreshPending: state.navRefreshPending,
      calls: gateCalls.splice(0),
    };

    // 4. Leaving plate focus back to grid syncs gate with valid g/t/p/z and { painted: false }.
    ctx.setPlateFocus(null);
    const afterLeaveFocus = { calls: gateCalls.splice(0) };

    // 5. reopenPreservingView: edit during reopening syncs gate and forces request on restored.
    state.plate.focus = 2;
    const reopenDriftPromise = ctx.reopenPreservingView();
    state.luts = [{ lo: 0, hi: 64, gamma: 1 }];
    ctx.refreshTiles();
    viewerHandlers.get('open').slice().forEach((fn) => fn());
    const afterReopenDrift = {
      navRefreshPending: state.navRefreshPending,
      calls: gateCalls.splice(0),
    };

    // 6. reopenPreservingView: without drift, restored calls resetState(currentQuery).
    const reopenCleanPromise = ctx.reopenPreservingView();
    viewerHandlers.get('open').slice().forEach((fn) => fn());
    const afterReopenClean = { calls: gateCalls.splice(0) };

    Promise.all([reopenDriftPromise, reopenCleanPromise]).then(() => {
      process.stdout.write(JSON.stringify({
        afterGridRefresh,
        afterEnterFocus,
        afterBusyRelease,
        afterIdleRelease,
        afterLeaveFocus,
        afterReopenDrift,
        afterReopenClean,
      }));
    });
    """
    out = json.loads(run_node(script, STATIC, check=True, timeout=10).stdout)
    assert out["afterGridRefresh"]["stale"] is True
    assert out["afterGridRefresh"]["calls"] == [
        ["resetState", "?win=0%3A200&g=g1&t=1&p=0&z=2", {"painted": False}]
    ]
    assert out["afterEnterFocus"]["calls"][0] == [
        "resetState",
        "?win=0%3A200&g=g1&t=1&p=2&z=2",
        {"painted": False},
    ]
    assert out["afterEnterFocus"]["calls"][1] == [
        "request",
        "?win=0%3A200&g=g1&t=1&p=2&z=2",
        {"force": True},
    ]
    assert out["afterBusyRelease"]["navFlushes"] == 0
    assert out["afterBusyRelease"]["navRefreshPending"] is True
    assert out["afterIdleRelease"]["navFlushes"] == 1
    assert out["afterIdleRelease"]["navRefreshPending"] is False
    assert out["afterLeaveFocus"]["calls"][0] == [
        "resetState",
        "?win=0%3A90&g=g1&t=1&p=0&z=2",
        {"painted": False},
    ]
    assert out["afterReopenDrift"]["navRefreshPending"] is True
    assert out["afterReopenDrift"]["calls"] == [
        ["resetState", "?win=0%3A90&g=g1&t=1&p=2&z=2", {"painted": False}],
        ["resetState", "?win=0%3A64&g=g1&t=1&p=2&z=2", {"painted": False}],
        ["request", "?win=0%3A64&g=g1&t=1&p=2&z=2", {"force": True}],
    ]
    assert out["afterReopenClean"]["calls"] == [
        ["resetState", "?win=0%3A64&g=g1&t=1&p=2&z=2", {"painted": False}],
        ["resetState", "?win=0%3A64&g=g1&t=1&p=2&z=2", None],
    ]


def test_translucent_sync_bounded_retry_recovery_exhaustion_and_cancellation():
    script = r"""
    const fs = require('fs'), vm = require('vm');
    const source = fs.readFileSync(process.argv[1], 'utf8');
    function production(name) {
      const start = source.indexOf('function ' + name + '(');
      if (start < 0) throw Error('Missing function ' + name);
      return source.slice(start, source.indexOf('\n}', start) + 2);
    }
    const retryConstStart = source.indexOf('const TRANSLUCENT_RETRY_DELAYS_MS =');
    const retryConst = source.slice(retryConstStart, source.indexOf(';', retryConstStart) + 1);
    const serializer = source.slice(
      source.indexOf('function lutParam('),
      source.indexOf('function activeFrameParams(')
    );
    let nextTimerId = 1;
    const activeTimers = new Map();
    const scheduledDelays = [];
    const clearedIds = [];
    const fakeSetTimeout = (fn, ms) => {
      const id = nextTimerId++;
      activeTimers.set(id, { fn, ms });
      scheduledDelays.push(ms);
      return id;
    };
    const fakeClearTimeout = (id) => {
      if (activeTimers.has(id)) {
        activeTimers.delete(id);
        clearedIds.push(id);
      }
    };
    const fireNextTimer = () => {
      const entry = activeTimers.entries().next();
      if (entry.done) return false;
      const [id, timer] = entry.value;
      activeTimers.delete(id);
      timer.fn();
      return true;
    };
    const flush = async (n = 12) => { for (let i = 0; i < n; i++) await Promise.resolve(); };

    const state = {
      info: {
        rgb: false, dtype: 'uint8', generation: 'g1',
        channels: [{ label: 'Ch0', color: '00FF00', metadataColor: '00FF00', window: { min: 0, max: 255, start: 0, end: 255 } }],
      },
      channels: [0],
      luts: [null],
      quitPreparing: false,
      translucentSync: {
        launchSeq: 1, launchToken: 'tok-1', generation: 'g1',
        revision: 0, acceptedQuery: '', inflight: false, retryTimer: null, retryCount: 0,
      },
    };
    const calls = [];
    let responder = async () => ({ ok: true });
    const native = {
      update_translucent_display: async (sid, token, rev, query) => {
        calls.push({ sid, token, rev, query });
        return responder(sid, token, rev, query);
      },
    };
    const ctx = vm.createContext({
      state, URLSearchParams, Promise, Error,
      setTimeout: fakeSetTimeout,
      clearTimeout: fakeClearTimeout,
      window: { parent: { pywebview: { api: native } } },
      currentSlideSid: () => 'sid-1',
    });
    vm.runInContext(
      retryConst + '\n' +
      serializer + '\n' +
      production('cancelTranslucentRetry') + '\n' +
      production('clearTranslucentSync') + '\n' +
      production('expireTranslucentSync') + '\n' +
      production('publishTranslucentDisplay'),
      ctx
    );

    (async () => {
      // 1. Transient throw then non-ok reply then recovery on retry #2 (delays 150, 350).
      let attempt = 0;
      responder = async () => {
        attempt += 1;
        if (attempt === 1) throw new Error('IPC busy');
        if (attempt === 2) return { ok: false, message: 'temporary' };
        return { ok: true, applied: true };
      };
      state.luts = [{ lo: 0, hi: 180, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      await flush();
      fireNextTimer();
      await flush();
      fireNextTimer();
      await flush();
      const recovery = {
        delays: scheduledDelays.splice(0),
        acceptedQuery: state.translucentSync.acceptedQuery,
        retryCount: state.translucentSync.retryCount,
        pendingTimers: activeTimers.size,
        callCount: calls.length,
      };

      // 2. Persistent transient failure stops cleanly after 4 bounded retries [150, 350, 750, 1500].
      calls.length = 0;
      responder = async () => { throw new Error('still down'); };
      state.luts = [{ lo: 0, hi: 150, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      await flush();
      while (fireNextTimer()) await flush();
      const exhausted = {
        delays: scheduledDelays.splice(0),
        callCount: calls.length,
        retryCount: state.translucentSync.retryCount,
        pendingTimers: activeTimers.size,
        launchToken: state.translucentSync.launchToken,
      };

      // 3. Fresh (!isRetry) edit cancels pending timer, resets retryCount, and delivers immediately.
      calls.length = 0;
      let failOnce = true;
      responder = async () => {
        if (failOnce) { failOnce = false; throw new Error('drop'); }
        return { ok: true, applied: true };
      };
      state.luts = [{ lo: 0, hi: 140, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      await flush();
      const timerCountBeforeFresh = activeTimers.size;
      state.luts = [{ lo: 0, hi: 130, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      await flush();
      const afterFreshEdit = {
        timerCountBeforeFresh,
        pendingTimers: activeTimers.size,
        retryCount: state.translucentSync.retryCount,
        acceptedQuery: state.translucentSync.acceptedQuery,
        queries: calls.map((c) => c.query),
      };

      // 4. invalid and invalid_query replies do not schedule retries and keep launchToken.
      scheduledDelays.length = 0;
      responder = async () => ({ ok: false, invalid_query: true });
      state.luts = [{ lo: 0, hi: 120, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      await flush();
      responder = async () => ({ ok: false, invalid: true });
      state.luts = [{ lo: 0, hi: 110, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      await flush();
      const afterInvalid = {
        delays: scheduledDelays.slice(),
        pendingTimers: activeTimers.size,
        retryCount: state.translucentSync.retryCount,
        launchToken: state.translucentSync.launchToken,
      };

      // 5. clearTranslucentSync cancels armed retry timer (same path used by pagehide & selectScene).
      responder = async () => { throw new Error('down'); };
      state.luts = [{ lo: 0, hi: 100, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      await flush();
      const armedBeforeClear = activeTimers.size;
      ctx.clearTranslucentSync();
      const afterClear = {
        armedBeforeClear,
        pendingTimers: activeTimers.size,
        retryTimer: state.translucentSync.retryTimer,
        retryCount: state.translucentSync.retryCount,
        launchToken: state.translucentSync.launchToken,
      };

      // 6. Terminal closed reply clears sync and schedules no retry.
      state.translucentSync.launchToken = 'tok-2';
      state.translucentSync.generation = 'g1';
      state.translucentSync.acceptedQuery = '';
      scheduledDelays.length = 0;
      responder = async () => ({ ok: false, closed: true });
      state.luts = [{ lo: 0, hi: 90, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      await flush();
      const afterClosed = {
        delays: scheduledDelays.slice(),
        pendingTimers: activeTimers.size,
        launchToken: state.translucentSync.launchToken,
      };

      process.stdout.write(JSON.stringify({
        recovery, exhausted, afterFreshEdit, afterInvalid, afterClear, afterClosed,
      }));
    })();
    """
    out = json.loads(run_node(script, APP, check=True, timeout=10).stdout)
    assert out["recovery"] == {
        "delays": [150, 350],
        "acceptedQuery": "win=0%3A180",
        "retryCount": 0,
        "pendingTimers": 0,
        "callCount": 3,
    }
    assert out["exhausted"] == {
        "delays": [150, 350, 750, 1500],
        "callCount": 5,
        "retryCount": 4,
        "pendingTimers": 0,
        "launchToken": "tok-1",
    }
    assert out["afterFreshEdit"] == {
        "timerCountBeforeFresh": 1,
        "pendingTimers": 0,
        "retryCount": 0,
        "acceptedQuery": "win=0%3A130",
        "queries": ["win=0%3A140", "win=0%3A130"],
    }
    assert out["afterInvalid"] == {
        "delays": [],
        "pendingTimers": 0,
        "retryCount": 0,
        "launchToken": "tok-1",
    }
    assert out["afterClear"] == {
        "armedBeforeClear": 1,
        "pendingTimers": 0,
        "retryTimer": None,
        "retryCount": 0,
        "launchToken": None,
    }
    assert out["afterClosed"] == {
        "delays": [],
        "pendingTimers": 0,
        "launchToken": None,
    }
    # Verify pagehide invokes clearTranslucentSync in production app.js.
    app_src = APP.read_text(encoding="utf-8")
    pagehide_slice = app_src[app_src.index('window.addEventListener("pagehide", () => {\n  applyLuts.cancel();') :]
    pagehide_slice = pagehide_slice[: pagehide_slice.index("}, { once: true });")]
    assert "clearTranslucentSync()" in pagehide_slice

