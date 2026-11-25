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
    vm.runInContext(
      serializer + '\n' +
      production('tileQuery') + '\n' +
      production('captureTranslucentView') + '\n' +
      production('openTranslucentViewer') + '\n' +
      production('clearTranslucentSync') + '\n' +
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


def test_shading_generation_mismatch_and_refresh_clear_sync_only_after_commit():
    script = r"""
    const fs = require('fs'), vm = require('vm');
    const source = fs.readFileSync(process.argv[1], 'utf8');
    function production(name, asyncPrefix = false) {
      const prefix = (asyncPrefix ? 'async ' : '') + 'function ' + name + '(';
      const start = source.indexOf(prefix);
      if (start < 0) throw Error('Missing ' + prefix);
      return source.slice(start, source.indexOf('\n}', start) + 2);
    }
    const serializer = source.slice(
      source.indexOf('function lutParam('),
      source.indexOf('function activeFrameParams(')
    );
    const updates = [];
    const info = {
      generation: 'g1', kind: 'slide', width: 100, height: 80, dtype: 'uint8',
      channels: [{ label: 'FITC', color: '00FF00', window: { min: 0, max: 255, start: 0, end: 255 } }],
      levels: [],
    };
    const state = {
      info, channels: [0], luts: [null], lutAutoPending: [false], inspect: null,
      quitPreparing: false,
      translucentSync: {
        launchSeq: 1, launchToken: 'tok-1', generation: 'g1',
        revision: 0, acceptedQuery: '', inflight: false,
      },
    };
    let nextPayload = { ...info, generation: 'g2', width: 999 }; // invalid geometry
    const window = {
      Nd2LutTransfer: { currentLut: (ch) => ({ lo: ch.window.start, hi: ch.window.end, gamma: 1 }) },
      parent: { pywebview: { api: { update_translucent_display: async (...a) => { updates.push(a); return { ok: true }; } } } },
    };
    const ctx = vm.createContext({
      state, window, URLSearchParams, Promise, Error, Object, Array,
      activeFrameContext: () => ({ key: 's1:' + state.info.generation }),
      currentSlideSid: () => 's1',
      fetch: async () => ({ ok: true, json: async () => nextPayload }),
      frameRequests: { pixels: { invalidate() {} }, histograms: { schedule() {} } },
      $: () => ({ replaceChildren() {} }),
      buildLevelLamps() {}, updateScaleStrip() {}, reopenPreservingView: async () => true,
    });
    vm.runInContext(
      serializer + '\n' +
      production('clearTranslucentSync') + '\n' +
      production('publishTranslucentDisplay') + '\n' +
      production('refreshShadingPyramid', true),
      ctx
    );
    (async () => {
      let validationError = '';
      try { await ctx.refreshShadingPyramid('g2', { key: 's1:g1' }); } catch (e) { validationError = e.message; }
      const tokenAfterFailedShading = state.translucentSync.launchToken;
      nextPayload = { ...info, generation: 'g2', channels: [{ ...info.channels[0] }] };
      await ctx.refreshShadingPyramid('g2', { key: 's1:g1' });
      const tokenAfterCommittedShading = state.translucentSync.launchToken;
      // Direct generation mismatch in publishTranslucentDisplay clears with zero bridge calls.
      state.translucentSync.launchToken = 'tok-stale';
      state.translucentSync.generation = 'g1';
      state.luts = [{ lo: 0, hi: 100, gamma: 1 }];
      ctx.publishTranslucentDisplay();
      for (let i = 0; i < 6; i++) await Promise.resolve();
      process.stdout.write(JSON.stringify({
        validationError,
        tokenAfterFailedShading,
        tokenAfterCommittedShading,
        tokenAfterMismatchPublish: state.translucentSync.launchToken,
        updates,
      }));
    })();
    """
    out = json.loads(run_node(script, APP, check=True, timeout=10).stdout)
    assert "does not match" in out["validationError"]
    assert out["tokenAfterFailedShading"] == "tok-1"
    assert out["tokenAfterCommittedShading"] is None
    assert out["tokenAfterMismatchPublish"] is None
    assert out["updates"] == []


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


