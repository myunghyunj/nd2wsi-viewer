"""Explicit, isolated packaged-WebKit replay diagnostics (never a present clock).

The app opts into this helper only for a fresh Agent process and a source copy
under a caller-specified test root. Normal viewing does not install counters or
run JavaScript. The replay clock ends at stable rAF, not compositor presentation.
"""

from __future__ import annotations

import hashlib
import json
import resource
import threading
import time
from pathlib import Path

_metrics = None
_lock = threading.Lock()


def replay_actions():
    actions = [{"action_id": "step-0", "kind": "fit", "padding": 0.96}]
    actions += [{"action_id": f"step-{i}", "kind": "gamma", "channel": 0,
                 "gamma": 1 + (i % 5) * 0.25} for i in range(1, 21)]
    actions += [{"action_id": "step-21", "kind": "visible", "channel": 0, "visible": False},
                {"action_id": "step-22", "kind": "visible", "channel": 0, "visible": True},
                {"action_id": "step-23", "kind": "gamma", "channel": 0, "gamma": 1}]
    actions += [{"action_id": f"step-{i}", "kind": "zoom", "anchor": [0.37, 0.39],
                 "log_delta": 0.55} for i in range(24, 30)]
    actions += [{"action_id": f"step-{i}", "kind": "pan",
                 "delta": [(1 if i % 2 else -1) * 0.58, 0.31]} for i in range(30, 38)]
    actions += [{"action_id": f"step-{i}", "kind": "zoom", "anchor": [0.63, 0.71],
                 "log_delta": -0.6} for i in range(38, 43)]
    return actions + [{"action_id": "step-43", "kind": "fit", "padding": 0.96}]


def metrics_snapshot():
    with _lock:
        if _metrics is None:
            return {"ok": False, "error": "diagnostic counters are not enabled"}
        return {"ok": True, "counters": dict(_metrics["counters"]),
                "read_ms": _metrics["read_ms"],
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def _install_counters():
    from . import render

    global _metrics
    with _lock:
        if _metrics is not None:
            raise RuntimeError("benchmark already active in this process")
        _metrics = {"read_ms": 0.0, "counters": {
            "source_reads": 0, "raw_region_reads": 0, "cache_region_reads": 0,
            "decoded_tiles": None, "cpu_rgb_tiles": 0, "jpeg_encodes": 0,
            "cpu_readback_bytes": 0}}
    originals = render._read_region, render.composite, render.encode_image
    counts = _metrics

    def read(*args, **kwargs):
        start = time.perf_counter()
        result = originals[0](*args, **kwargs)
        with _lock:
            counts["read_ms"] += (time.perf_counter() - start) * 1000
            counts["counters"]["source_reads"] += int(str(args[1]) == "0")
            counts["counters"]["raw_region_reads"] += 1
            counts["counters"]["cache_region_reads"] += int(str(args[1]) != "0")
        return result

    def composite(*args, **kwargs):
        result = originals[1](*args, **kwargs)
        with _lock:
            counts["counters"]["cpu_rgb_tiles"] += 1
        return result

    def encode(*args, **kwargs):
        result = originals[2](*args, **kwargs)
        with _lock:
            counts["counters"]["jpeg_encodes"] += int(args[1] in ("jpg", "jpeg"))
        return result

    render._read_region, render.composite, render.encode_image = read, composite, encode

    def restore():
        global _metrics
        render._read_region, render.composite, render.encode_image = originals
        with _lock:
            _metrics = None

    return restore


_DRIVER = r"""
(async () => {
  const config = __CONFIG__, points = config.context.viewport_points;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const raf = () => new Promise(resolve => requestAnimationFrame(resolve));
  const metrics = () => window.__rc2Metrics();
  const stage = document.getElementById('stage');
  const samples = [];
  try {
    Object.assign(stage.style, {position:'fixed',left:'0',top:'0',right:'auto',bottom:'auto',
      width:points[0]+'px',height:points[1]+'px'});
    if (Math.abs(window.devicePixelRatio-config.context.backing_scale)>1e-9)
      throw new Error('Backing scale mismatch: observed '+window.devicePixelRatio);
    if (innerWidth<points[0] || innerHeight<points[1]) throw new Error('Viewport is clipped');
    if (state.info.channels.length<2) throw new Error('Replay needs multiple channels');
    state.viewer.viewport.resize(new OpenSeadragon.Point(...points),true);
    if (state.viewer.navigator) { state.viewer.navigator.destroy(); state.viewer.navigator=null; }
    const waitLoaded = async () => {
      const start=performance.now(); let stable=0;
      while(performance.now()-start<20000) {
        await raf();
        const item=state.viewer.world.getItemAt(0);
        if(item && item.getFullyLoaded() && !state.viewer.isAnimating()) stable++; else stable=0;
        if(stable>=3) return true;
      }
      return false;
    };
    const warmupStart=performance.now();
    if(!await waitLoaded()) throw new Error('Warmup did not load');
    const warmupMs=performance.now()-warmupStart;
    let center=[state.info.width/2,state.info.height/2], zoom=1, previous=null;
    for(const action of config.actions) {
      const before=await metrics(), start=performance.now();
      if(action.kind==='gamma') {
        const c=action.channel, original=state.info.channels[c].window;
        state.luts[c]={lo:original.start,hi:original.end,gamma:action.gamma}; refreshTiles();
      } else if(action.kind==='visible') {
        const enabled=new Set(state.channels);
        action.visible?enabled.add(action.channel):enabled.delete(action.channel);
        state.channels=Array.from(enabled).sort((a,b)=>a-b); refreshTiles();
      } else {
        if(action.kind==='fit') {
          center=[state.info.width/2,state.info.height/2];
          zoom=Math.min(points[0]/state.info.width,points[1]/state.info.height)*action.padding;
        } else if(action.kind==='pan') {
          center=[Math.max(0,Math.min(state.info.width,center[0]-action.delta[0]*points[0]/zoom)),
            Math.max(0,Math.min(state.info.height,center[1]+action.delta[1]*points[1]/zoom))];
        } else if(action.kind==='zoom') {
          const offset=[(action.anchor[0]-.5)*points[0],(action.anchor[1]-.5)*points[1]],
            world=[center[0]+offset[0]/zoom,center[1]+offset[1]/zoom],
            minimum=Math.min(points[0]/state.info.width,points[1]/state.info.height)*.1;
          zoom=Math.min(32,Math.max(minimum,zoom*Math.exp(action.log_delta)));
          center=[world[0]-offset[0]/zoom,world[1]-offset[1]/zoom];
        }
        state.viewer.viewport.fitBounds(state.viewer.viewport.imageToViewportRectangle(
          center[0]-points[0]/(2*zoom),center[1]-points[1]/(2*zoom),points[0]/zoom,points[1]/zoom),true);
        state.viewer.forceRedraw();
      }
      const complete=await waitLoaded(), proxy=performance.now(), after=await metrics();
      const counters={};
      for(const key of Object.keys(after.counters)) counters[key]=
        after.counters[key]===null||before.counters[key]===null?null:after.counters[key]-before.counters[key];
      const bounds=state.viewer.viewport.viewportToImageRectangle(state.viewer.viewport.getBounds(true));
      samples.push({action_id:action.action_id,
        phase:['gamma','visible'].includes(action.kind)?'resident':'fresh_tile', dropped:!complete,
        input_to_display_ms:complete?proxy-start:null,
        frame_interval_ms:complete&&previous!==null?proxy-previous:null,
        gpu_ms:null,tile_read_ms:after.read_ms-before.read_ms,upload_ms:null,
        logical_counters:counters,center:[...center],zoom,
        observed_camera:{center:[bounds.x+bounds.width/2,bounds.y+bounds.height/2],
          zoom:points[0]/bounds.width,bounds:[bounds.x,bounds.y,bounds.width,bounds.height]},
        browser_observed_raw_region_read:counters.raw_region_reads>0});
      if(complete) previous=proxy;
      await sleep(100);
    }
    const finalMetrics=await metrics();
    window.__rc2ReplayResult={ok:samples.every(s=>!s.dropped),schema:'nd2wsi-viewport-run/1',
      context:config.context,timing_kind:'browser_raf_proxy',samples,actions:config.actions,warmup_ms:warmupMs,
      resources:{peak_rss_bytes:null,server_and_shell_process_peak_rss_bytes:finalMetrics.peak_rss_bytes,
        browser_total_memory_bytes:null,
        memory_note:'Host Python/server process only; WebKit helper/compositor processes are not measured.'},
      hardware:{memory_bandwidth:null,power:null},
      scope:'Packaged macOS WebKit standard viewer, isolated Agent process. Diagnostic stage sizing and navigator disabled to match the native viewport workload.',
      timing_note:'Replay dispatch to three stable rAF callbacks after OSD fully-loaded. This is NOT input-to-present or a compositor/photon endpoint.'};
  } catch(error) { window.__rc2ReplayResult={ok:false,error:String(error),samples}; }
})();
"""


def run_browser_replay(api, window, report_path, context_path, test_root):
    """Self-driven QA of this process only; close even on bounded failure."""
    from .cache import quick_fingerprint

    report_path, context_path, test_root = map(Path, (report_path, context_path, test_root))
    restore = None
    restore_input = None
    report = {"ok": False}
    started = time.monotonic()
    source = None
    before = None
    cache_before = {}
    handle = None
    try:
        if api._window_session is None or api._window_session.role != "agent":
            raise ValueError("packaged replay requires a fresh Agent window")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        handle = report_path.open("x", encoding="utf-8")
        root = test_root.resolve(strict=True)
        source = Path(api._launch_source).resolve(strict=True)
        if not source.is_relative_to(root):
            raise ValueError("benchmark source must be under the isolated test root")
        before = quick_fingerprint(source)
        context = json.loads(context_path.read_text())
        if any(context.get("source_fingerprint", {}).get(k) != before[k]
               for k in ("size", "mtime_ns", "quick_sha256")):
            raise ValueError("declared benchmark source fingerprint does not match the actual copy")
        actions = replay_actions()
        context["actions_sha256"] = hashlib.sha256(
            json.dumps(actions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        restore = _install_counters()
        while time.monotonic()-started < 90:
            ready = window.evaluate_js("""(() => {
              const f=document.querySelector('#frames iframe.active');
              return !!(f && f.contentWindow && f.contentWindow.eval(
                'typeof state !== "undefined" && state.viewer?.world?.getItemAt(0) && state.info'));
            })()""")
            if ready:
                break
            if api._startup_error:
                raise RuntimeError(api._startup_error)
            time.sleep(.25)
        else:
            raise TimeoutError("packaged browser did not open the isolated slide")
        from .native_gestures import install_replay_input_guard

        restore_input = install_replay_input_guard(window)
        for state in api._httpd.registry.slides.values():
            for path in (state.source_path, state.cache_path):
                if path is not None and not Path(path).resolve().is_relative_to(root):
                    raise ValueError("benchmark resolved input/cache outside isolated test root")
            if state.cache_path is None or not Path(state.cache_path).is_file():
                raise ValueError("paired benchmark requires an existing single-file cache")
            actual_cache = quick_fingerprint(state.cache_path)
            if any(context.get("cache_fingerprint", {}).get(k) != actual_cache[k]
                   for k in ("size", "mtime_ns", "quick_sha256")):
                raise ValueError("declared cache fingerprint does not match the cache actually opened")
            cache_before[Path(state.cache_path)] = actual_cache
        driver = _DRIVER.replace("__CONFIG__", json.dumps({"context": context, "actions": actions}))
        window.evaluate_js("""(() => {
          const f=document.querySelector('#frames iframe.active');
          f.contentWindow.__rc2Metrics=()=>window.pywebview.api.benchmark_metrics();
          f.contentWindow.eval(""" + json.dumps(driver) + "); return true; })()")
        while time.monotonic()-started < 480:
            result = window.evaluate_js("document.querySelector('#frames iframe.active')?.contentWindow.__rc2ReplayResult || null")
            if isinstance(result, dict):
                report = result
                break
            time.sleep(.25)
        else:
            raise TimeoutError("packaged browser replay exceeded its time limit")
    except Exception as exc:
        report.update(ok=False, error=str(exc))
    finally:
        if restore_input is not None:
            restore_input()
        if restore is not None:
            restore()
        report["elapsed_seconds"] = time.monotonic()-started
        report["session"] = api._window_session.as_dict() if api._window_session else None
        report["window_context"] = api.window_context() if hasattr(api, "window_context") else None
        report["open_attempt_id"] = getattr(api, "_open_attempt_id", None)
        report["fallback_consumed"] = getattr(api, "_fallback_consumed", False)
        report["physical_input_guard"] = "own Agent replay window only" if restore_input else "not installed"
        if before is not None:
            try:
                report["source_preserved"] = before == quick_fingerprint(source)
                if not report["source_preserved"]:
                    report["ok"] = False
            except OSError:
                report.update(ok=False, source_preserved=False)
        report["cache_preserved"] = None
        if cache_before:
            try:
                report["cache_preserved"] = all(quick_fingerprint(p) == fp for p, fp in cache_before.items())
                if not report["cache_preserved"]:
                    report["ok"] = False
            except OSError:
                report.update(ok=False, cache_preserved=False)
        if handle is not None:
            with handle:
                json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
        api._browser_replay_report = report
        window.destroy()
    return report
