"""Summarize independently collected viewport measurements without false parity.

This tool does not open slides, run a cache build, or interact with any window.
Feed it reports from an isolated Agent Metal window and an isolated browser
baseline following the same action list. Browser requestAnimationFrame timings
are a presentation *proxy*, not Metal drawable presentation timestamps. The
report deliberately does not calculate speedups between these clock endpoints.

Input schema: ``nd2wsi-viewport-run/1``. Each run contains ``context``,
``timing_kind``, ``samples``, and optional ``resources``/``hardware``. Context
requires machine_id, source_fingerprint, cache_fingerprint, viewport_pixels,
viewport_points, backing_scale, and actions_sha256. A sample has action_id,
phase (resident/fresh_tile), dropped, and optional nonnegative timing fields
listed below. Logical counters are per-action deltas, never hardware bandwidth.
All missing measurements remain null, not zero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # Windows can still run pure report/reference helpers.
    resource = None

RUN_SCHEMA = "nd2wsi-viewport-run/1"
CONTEXT_FIELDS = (
    "machine_id",
    "source_fingerprint",
    "cache_fingerprint",
    "viewport_pixels",
    "viewport_points",
    "backing_scale",
    "actions_sha256",
)
TIMINGS = (
    "input_to_display_ms",
    "frame_interval_ms",
    "gpu_ms",
    "tile_read_ms",
    "upload_ms",
)
RESIDENT_COUNTERS = (
    "source_reads",
    "decoded_tiles",
    "cpu_rgb_tiles",
    "jpeg_encodes",
    "cpu_readback_bytes",
)
TIMING_KINDS = {"metal_drawable_presented", "browser_raf_proxy"}


def replay_actions() -> list[dict]:
    """Common native/browser controls only; color edits and capture are separate."""
    actions = [{"action_id": "step-0", "kind": "fit", "padding": 0.96}]
    actions += [
        {"action_id": f"step-{step}", "kind": "gamma", "channel": 0, "gamma": 1 + (step % 5) * 0.25}
        for step in range(1, 21)
    ]
    actions += [
        {"action_id": "step-21", "kind": "visible", "channel": 0, "visible": False},
        {"action_id": "step-22", "kind": "visible", "channel": 0, "visible": True},
        {"action_id": "step-23", "kind": "gamma", "channel": 0, "gamma": 1},
    ]
    # Anchors are top-left normalized points; AppKit's unflipped view uses y=1-y.
    actions += [
        {"action_id": f"step-{step}", "kind": "zoom", "anchor": [0.37, 0.39], "log_delta": 0.55}
        for step in range(24, 30)
    ]
    actions += [
        {
            "action_id": f"step-{step}",
            "kind": "pan",
            "delta": [(1 if step % 2 else -1) * 0.58, 0.31],
        }
        for step in range(30, 38)
    ]
    actions += [
        {"action_id": f"step-{step}", "kind": "zoom", "anchor": [0.63, 0.71], "log_delta": -0.6}
        for step in range(38, 43)
    ]
    actions += [{"action_id": "step-43", "kind": "fit", "padding": 0.96}]
    return actions


BROWSER_DRIVER = r"""
(async () => {
  'use strict';
  const base = __BENCHMARK_BASE__;
  const config = await fetch(base + '/config').then(r => r.json());
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const raf = () => new Promise(resolve => requestAnimationFrame(resolve));
  const button = document.createElement('button');
  button.id = 'isolated-viewport-benchmark';
  button.textContent = 'Run isolated stable viewport benchmark';
  button.style.cssText = 'position:fixed;bottom:8px;left:8px;z-index:2147483647;padding:12px';
  document.body.append(button);
  const status = document.createElement('pre');
  status.id = 'isolated-viewport-benchmark-status';
  status.style.cssText = 'position:fixed;bottom:55px;left:8px;z-index:2147483647;background:#111;color:white';
  document.body.append(status);
  const points = config.context.viewport_points;
  const stage = document.getElementById('stage');
  Object.assign(stage.style, {position:'fixed',left:'0',top:'0',right:'auto',bottom:'auto',
    width:points[0]+'px',height:points[1]+'px'});
  const waitLoaded = async () => {
    const start = performance.now();
    let stable = 0;
    while (performance.now() - start < 20000) {
      await raf();
      const item = state.viewer?.world?.getItemAt(0);
      if (item && item.getFullyLoaded() && !state.viewer.isAnimating()) stable++; else stable=0;
      if (stable >= 3) return true;
    }
    return false;
  };
  while (!state.viewer?.world?.getItemAt(0)) await sleep(100);
  state.viewer.viewport.resize(new OpenSeadragon.Point(...points), true);
  status.textContent = 'Isolated stable source server. No installed app or User window is used.';
  button.onclick = async () => {
    button.disabled = true;
    const samples = [], actions = config.actions;
    let center = [state.info.width/2, state.info.height/2], zoom = 1, previousProxy = null;
    try {
      if (Math.abs(window.devicePixelRatio - config.context.backing_scale) > 1e-9)
        throw new Error('Browser/native backing scale mismatch: ' + window.devicePixelRatio);
      if (window.innerWidth < points[0] || window.innerHeight < points[1])
        throw new Error('Browser content area clips the required viewport; enlarge the isolated browser panel');
      if (Math.round(stage.clientWidth)!==points[0] || Math.round(stage.clientHeight)!==points[1])
        throw new Error('Viewport point dimensions do not match');
      if (state.info.channels.length < 2) throw new Error('Common replay requires multiple channels');
      await waitLoaded();
      // Navigator is not part of the measured native viewport. Keep it from
      // requesting/rendering a second, unmatched image during each LUT edit.
      if (state.viewer.navigator) { state.viewer.navigator.destroy(); state.viewer.navigator = null; }
      for (const action of actions) {
        status.textContent = 'Running ' + action.action_id + ': ' + action.kind;
        const before = await fetch(base + '/metrics').then(r=>r.json());
        const start = performance.now();
        if (action.kind === 'gamma') {
          const c=action.channel, original=state.info.channels[c].window;
          state.luts[c] = {lo:original.start,hi:original.end,gamma:action.gamma};
          refreshTiles();
        } else if (action.kind === 'visible') {
          const enabled = new Set(state.channels);
          action.visible ? enabled.add(action.channel) : enabled.delete(action.channel);
          state.channels = Array.from(enabled).sort((a,b)=>a-b);
          refreshTiles();
        } else {
          if (action.kind === 'fit') {
            center=[state.info.width/2,state.info.height/2];
            zoom=Math.min(points[0]/state.info.width,points[1]/state.info.height)*action.padding;
          } else if (action.kind === 'pan') {
            center[0]=Math.max(0,Math.min(state.info.width,center[0]-action.delta[0]*points[0]/zoom));
            center[1]=Math.max(0,Math.min(state.info.height,center[1]+action.delta[1]*points[1]/zoom));
          } else if (action.kind === 'zoom') {
            const offset=[(action.anchor[0]-.5)*points[0],(action.anchor[1]-.5)*points[1]];
            const world=[center[0]+offset[0]/zoom,center[1]+offset[1]/zoom];
            const minimum=Math.min(points[0]/state.info.width,points[1]/state.info.height)*.1;
            zoom=Math.min(32,Math.max(minimum,zoom*Math.exp(action.log_delta)));
            center=[world[0]-offset[0]/zoom,world[1]-offset[1]/zoom];
          }
          const bounds=state.viewer.viewport.imageToViewportRectangle(
            center[0]-points[0]/(2*zoom),center[1]-points[1]/(2*zoom),points[0]/zoom,points[1]/zoom);
          state.viewer.viewport.fitBounds(bounds,true);
          state.viewer.forceRedraw();
        }
        const complete = await waitLoaded();
        // Three stable rAF callbacks after OSD reports loaded are deliberately
        // labeled a software proxy. They do not measure compositor presentation.
        const proxy=performance.now();
        const after=await fetch(base + '/metrics').then(r=>r.json());
        const actualBounds=state.viewer.viewport.viewportToImageRectangle(state.viewer.viewport.getBounds(true));
        const observedCamera={center:[actualBounds.x+actualBounds.width/2,actualBounds.y+actualBounds.height/2],
          zoom:points[0]/actualBounds.width,
          bounds:[actualBounds.x,actualBounds.y,actualBounds.width,actualBounds.height]};
        const counters={};
        for(const key of Object.keys(after.counters)) counters[key]=
          after.counters[key]===null||before.counters[key]===null?null:after.counters[key]-before.counters[key];
        samples.push({action_id:action.action_id,
          phase:action.kind==='gamma'||action.kind==='visible'?'resident':'fresh_tile',
          dropped:!complete,input_to_display_ms:complete?proxy-start:null,
          frame_interval_ms:complete&&previousProxy!==null?proxy-previousProxy:null,
          gpu_ms:null,tile_read_ms:after.read_ms-before.read_ms,upload_ms:null,
          logical_counters:counters,center:[...center],zoom,observed_camera:observedCamera,
          browser_observed_raw_region_read:counters.raw_region_reads>0});
        if(complete)previousProxy=proxy;
        await sleep(100);
      }
      const metrics=await fetch(base+'/metrics').then(r=>r.json());
      const report={schema:'nd2wsi-viewport-run/1',context:config.context,timing_kind:'browser_raf_proxy',samples,
        resources:{server_peak_rss_bytes:metrics.peak_rss_bytes,peak_rss_bytes:null},
        hardware:{memory_bandwidth:null,power:null},actions,
        scope:'Unpackaged v2.0.0 stable server/render code in an isolated managed browser; not installed native WebKit app.',
        timing_note:'Action dispatch to three stable requestAnimationFrame callbacks after OSD fully-loaded; includes idle/action cadence. Not photon or compositor presentation time.'};
      const response=await fetch(base+'/result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(report)});
      if(!response.ok)throw new Error(await response.text());
      status.textContent='Benchmark complete: '+samples.length+' actions. Report saved.';
    } catch(error) {
      status.textContent='Benchmark failed: '+error.message;
      await fetch(base+'/failure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({error:String(error),samples})});
    }
  };
})();
"""


def serve_browser_baseline(args) -> None:
    """Run only an explicitly isolated stable cache; never create/migrate one."""
    stable = args.stable_root.resolve(strict=True)
    isolated = args.test_root.resolve(strict=True)
    cache = args.cache.resolve(strict=True)
    if not cache.is_relative_to(isolated) or cache.suffix != ".nd2svs":
        raise ValueError("--cache must be an existing .nd2svs file under --test-root")
    if args.output.exists() or args.ready_file.exists():
        raise ValueError("choose new output and ready-file paths")
    context = json.loads(args.context.read_text())
    context["actions_sha256"] = action_digest(replay_actions())
    # Validate all context fields before importing/opening the stable reader.
    validate_run(
        {
            "schema": RUN_SCHEMA,
            "context": context,
            "timing_kind": "browser_raf_proxy",
            "samples": [{"action_id": "validation", "phase": "resident", "dropped": False}],
        }
    )
    sys.path.insert(0, str(stable))
    from nd2wsi import render, server

    if not Path(server.__file__).resolve().is_relative_to(stable):
        raise ValueError("stable source import resolved to another checkout")
    from nd2wsi.cache import read_manifest

    manifest = read_manifest(cache)
    if not manifest:
        raise ValueError("isolated baseline requires a complete existing cache")
    relative_source = manifest.get("source", {}).get("relative_path")
    if not isinstance(relative_source, str) or not (
        cache.parent / relative_source
    ).resolve().is_relative_to(isolated):
        raise ValueError("cache manifest must refer to the isolated source copy")
    # The cache resolver searches the local source beside its managed directory;
    # never pass an external original as source_path. Registry paths are checked
    # again before accepting any browser requests.
    httpd = server.create_server([cache], host="127.0.0.1", port=0)
    for state in httpd.registry.slides.values():
        source_path = state.source_path
        if source_path is not None and not Path(source_path).resolve().is_relative_to(isolated):
            httpd.server_close()
            raise ValueError("cache resolved a source outside the isolated test root")
    counts = {
        "source_reads": 0,
        "decoded_tiles": None,
        "raw_region_reads": 0,
        "cache_region_reads": 0,
        "cpu_rgb_tiles": 0,
        "jpeg_encodes": 0,
        "cpu_readback_bytes": 0,
    }
    lock = threading.Lock()
    timing = {"read_ms": 0.0}
    original_read, original_composite, original_encode = (
        render._read_region,
        render.composite,
        render.encode_image,
    )

    def read_region(*positional, **keywords):
        start = time.perf_counter()
        result = original_read(*positional, **keywords)
        with lock:
            counts["source_reads"] += int(str(positional[1]) == "0")
            counts["raw_region_reads"] += 1
            counts["cache_region_reads"] += int(str(positional[1]) != "0")
            timing["read_ms"] += (time.perf_counter() - start) * 1000
        return result

    def composite_counted(*positional, **keywords):
        result = original_composite(*positional, **keywords)
        with lock:
            counts["cpu_rgb_tiles"] += 1
        return result

    def encode_counted(*positional, **keywords):
        result = original_encode(*positional, **keywords)
        with lock:
            counts["jpeg_encodes"] += int(positional[1] in ("jpg", "jpeg"))
        return result

    render._read_region, render.composite, render.encode_image = (
        read_region,
        composite_counted,
        encode_counted,
    )
    original_handler = httpd.RequestHandlerClass
    benchmark_base = f"/{httpd.token}/benchmark"

    class BenchmarkHandler(original_handler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == benchmark_base + "/config":
                return self._json({"context": context, "actions": replay_actions()})
            if path == benchmark_base + "/metrics":
                with lock:
                    return self._json(
                        {
                            "counters": dict(counts),
                            **timing,
                            "peak_rss_bytes": (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                                               if resource is not None else None),
                        }
                    )
            if path == benchmark_base + "/driver.js":
                body = BROWSER_DRIVER.replace(
                    "__BENCHMARK_BASE__", json.dumps(benchmark_base)
                ).encode()
                return self._send(200, body, "application/javascript")
            return super().do_GET()

        def _static(self, relative):
            if relative == "index.html":
                body = (server.STATIC_DIR / relative).read_text()
                body = body.replace(
                    "</body>", f'<script src="{benchmark_base}/driver.js"></script></body>'
                )
                return self._send(200, body.encode(), "text/html; charset=utf-8")
            return super()._static(relative)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in (benchmark_base + "/result", benchmark_base + "/failure"):
                return self._error(
                    403, "benchmark is read-only: opening, trash and annotation writes disabled"
                )
            if int(self.headers.get("Content-Length", 0)) > 2_000_000:
                return self._error(413, "benchmark report too large")
            try:
                payload = self._body_json()
                destination = (
                    args.output
                    if path.endswith("/result")
                    else args.output.with_suffix(".failure.json")
                )
                if path.endswith("/result"):
                    validate_run(payload)
                    if payload["context"] != context:
                        raise ValueError("browser context changed")
                with destination.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, allow_nan=False)
                    handle.write("\n")
                return self._json({"ok": True})
            except (ValueError, OSError) as error:
                return self._error(400, str(error))

    httpd.RequestHandlerClass = BenchmarkHandler
    listing = httpd.registry.listing()
    url = server.server_url(httpd) + "s/" + listing[0]["sid"] + "/"
    ready = {
        "url": url,
        "pid": __import__("os").getpid(),
        "context": context,
        "stable_source": str(stable),
        "cache": str(cache),
        "report": str(args.output),
        "scope": "unpackaged stable renderer in isolated browser, not installed app",
    }
    with args.ready_file.open("x", encoding="utf-8") as handle:
        json.dump(ready, handle, indent=2)
        handle.write("\n")
    print(json.dumps(ready), flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return float(value)


def percentiles(values: list[float]) -> dict[str, int | float | None]:
    """Linear interpolation, equivalent to NumPy's default percentile method."""
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None}
    ordered = sorted(_finite_nonnegative(v, "timing") for v in values)
    result: dict[str, int | float | None] = {"count": len(ordered)}
    for percentile in (50, 95, 99):
        index = (len(ordered) - 1) * percentile / 100
        lo = math.floor(index)
        hi = math.ceil(index)
        result[f"p{percentile}"] = ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)
    return result


def action_digest(actions: list[dict]) -> str:
    """A canonical hash of the complete ordered action list, including settings."""
    return hashlib.sha256(
        json.dumps(actions, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_run(run: dict) -> None:
    if not isinstance(run, dict) or run.get("schema") != RUN_SCHEMA:
        raise ValueError(f"run schema must be {RUN_SCHEMA}")
    context = run.get("context")
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    for key in CONTEXT_FIELDS:
        if key not in context or context[key] in (None, "", {}, []):
            raise ValueError(f"context.{key} is required")
    for key in ("viewport_pixels", "viewport_points"):
        dimensions = context[key]
        if not isinstance(dimensions, list) or len(dimensions) != 2:
            raise ValueError(f"context.{key} must contain width and height")
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in dimensions):
            raise ValueError(f"context.{key} dimensions must be positive integers")
    scale = _finite_nonnegative(context["backing_scale"], "backing_scale")
    if scale <= 0:
        raise ValueError("backing_scale must be positive")
    for physical, points in zip(
        context["viewport_pixels"], context["viewport_points"], strict=True
    ):
        if abs(physical - points * scale) > 1.0:
            raise ValueError("viewport_pixels is inconsistent with viewport_points/backing_scale")
    digest = context["actions_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("actions_sha256 must be a lowercase SHA-256 digest")
    if run.get("timing_kind") not in TIMING_KINDS:
        raise ValueError(f"timing_kind must be one of {sorted(TIMING_KINDS)}")
    samples = run.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("samples must be a nonempty array")
    seen = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("sample must be an object")
        action_id = sample.get("action_id")
        if not isinstance(action_id, str) or not action_id or action_id in seen:
            raise ValueError("sample action_id must be nonempty and unique")
        seen.add(action_id)
        if sample.get("phase") not in {"resident", "fresh_tile"}:
            raise ValueError("sample phase must be resident or fresh_tile")
        if not isinstance(sample.get("dropped"), bool):
            raise ValueError("each sample requires an explicit dropped boolean")
        for key in TIMINGS:
            if sample.get(key) is not None:
                _finite_nonnegative(sample[key], key)
        if sample["dropped"] and sample.get("input_to_display_ms") is not None:
            raise ValueError("a dropped frame cannot have a measured presentation latency")
        counters = sample.get("logical_counters", {})
        if not isinstance(counters, dict):
            raise ValueError("logical_counters must be an object")
        for key, value in counters.items():
            if value is not None:
                if _finite_nonnegative(value, key) != int(value):
                    raise ValueError(f"logical counter {key} must be an integer")
    for key, value in run.get("resources", {}).items():
        if key == "memory_note":
            if not isinstance(value, str):
                raise ValueError("memory_note must describe the measured process scope")
            continue
        if value is not None:
            _finite_nonnegative(value, key)
    for key, measurement in run.get("hardware", {}).items():
        if measurement is None:
            continue
        if not isinstance(measurement, dict):
            raise ValueError(f"hardware.{key} requires value, unit, method, scope")
        _finite_nonnegative(measurement.get("value"), key)
        for field in ("unit", "method", "scope"):
            if not isinstance(measurement.get(field), str) or not measurement[field].strip():
                raise ValueError(f"hardware.{key}.{field} is required")


def summarize_run(run: dict) -> dict:
    validate_run(run)
    phases = {}
    for phase in ("resident", "fresh_tile"):
        samples = [s for s in run["samples"] if s["phase"] == phase]
        phases[phase] = {
            "actions": len(samples),
            "dropped": sum(s["dropped"] for s in samples),
            "timings_ms": {
                key: percentiles(
                    [s[key] for s in samples if not s["dropped"] and s.get(key) is not None]
                )
                for key in TIMINGS
            },
        }
    resident = [s for s in run["samples"] if s["phase"] == "resident"]
    checks = {}
    for key in RESIDENT_COUNTERS:
        values = [s.get("logical_counters", {}).get(key) for s in resident]
        fully_measured = bool(values) and all(v is not None for v in values)
        checks[key] = {
            "measured_actions": sum(v is not None for v in values),
            "sum": sum(values) if fully_measured else None,
            "all_zero": all(v == 0 for v in values) if fully_measured else None,
        }
    observed = None
    if any("observed_native_residency" in sample for sample in run["samples"]):
        observed = {
            kind: {
                metric: percentiles(
                    [
                        sample[metric]
                        for sample in run["samples"]
                        if sample.get("observed_native_residency") == kind
                        and not sample["dropped"]
                        and sample.get(metric) is not None
                    ]
                )
                for metric in TIMINGS
            }
            for kind in ("resident", "streamed")
        }
    return {
        "timing_kind": run["timing_kind"],
        "phases": phases,
        "resident_no_cpu_pipeline": checks,
        "actual_native_raw_tile_residency_timings_ms": observed,
        "phase_note": "Phase groups describe the common action workload: fixed-viewport display changes versus camera changes. Camera changes may hit cached tiles; actual native residency is reported separately.",
        "resources": {"peak_rss_bytes": None, **run.get("resources", {})},
        "hardware": {"memory_bandwidth": None, "power": None, **run.get("hardware", {})},
        "logical_counter_warning": "Logical call/byte counters are not measured physical memory traffic.",
    }


def compare_runs(native: dict, browser: dict) -> dict:
    validate_run(native)
    validate_run(browser)
    mismatches = [
        key for key in CONTEXT_FIELDS if native["context"][key] != browser["context"][key]
    ]
    if mismatches:
        raise ValueError("run contexts do not match: " + ", ".join(mismatches))

    def action_keys(run):
        return [(s["action_id"], s["phase"]) for s in run["samples"]]

    if action_keys(native) != action_keys(browser):
        raise ValueError("ordered action IDs and phases do not match")
    endpoints_match = native["timing_kind"] == browser["timing_kind"]
    return {
        "schema": "nd2wsi-viewport-comparison/1",
        "context": native["context"],
        "conditions": "Same declared source/cache, machine, viewport and ordered actions. OS page cache is not purged.",
        "native": summarize_run(native),
        "browser": summarize_run(browser),
        "presentation_endpoints_match": endpoints_match,
        "presentation_speedup": None,
        "comparison_warning": (
            "Endpoint types match; inspect clock origin, action cadence and sampling before drawing conclusions."
            if endpoints_match
            else "Metal presented timestamps and browser requestAnimationFrame proxies are different endpoints; no speedup ratio is valid."
        ),
        "unmeasured": [
            "Input-device-to-photon latency (requires external instrumentation)",
            "True cold OS page-cache access",
            "Hardware bandwidth/power when corresponding hardware fields are null",
        ],
    }


def normalize_native_report(raw: dict, context: dict) -> dict:
    """First complete presented frame per replay action; no fabricated metrics."""
    if raw.get("schema") != "nd2wsi-native-viewport-v1":
        raise ValueError("expected an actual native viewport diagnostic report")
    if raw.get("errors"):
        raise ValueError("native report contains errors; collect a clean replay")
    viewport = raw.get("viewport", {})
    if (
        viewport.get("points") != context["viewport_points"]
        or viewport.get("drawable_pixels") != context["viewport_pixels"]
    ):
        raise ValueError("native actual viewport does not match benchmark context")
    context = {**context, "actions_sha256": action_digest(replay_actions())}
    recorded = {a["action_id"]: a for a in raw.get("actions", []) if "action_id" in a}
    samples = []
    for specification in replay_actions():
        action_id = specification["action_id"]
        action = recorded.get(action_id)
        if action is None:
            raise ValueError(f"native replay action missing: {action_id}")
        frames = [
            f
            for f in raw.get("frames", [])
            if f.get("action_id") == action_id
            and f.get("mode") == "benchmark"
            and f.get("complete_viewport")
            and f.get("presented_time", 0) > 0
            and not f.get("dropped", False)
        ]
        first = min(frames, key=lambda frame: frame["presented_time"]) if frames else None
        display_only = specification["kind"] in ("gamma", "visible")
        counters = {key: None for key in RESIDENT_COUNTERS}
        if first:
            if first.get("cpu_readbacks") == 0:
                counters["cpu_readback_bytes"] = 0
            if raw.get("transfers", {}).get("cpu_rgb_or_jpeg_frames") == 0:
                counters["cpu_rgb_tiles"] = counters["jpeg_encodes"] = 0
            if display_only:
                start_requests, end_requests = (
                    action.get("request_count"),
                    first.get("request_count"),
                )
                start_bytes, end_bytes = action.get("uploaded_bytes"), first.get("uploaded_bytes")
                if (
                    first.get("class") == "resident"
                    and start_requests is not None
                    and end_requests is not None
                    and start_requests == end_requests
                    and start_bytes is not None
                    and end_bytes == start_bytes
                ):
                    # An unchanged raw-tile request count proves that this LUT
                    # action caused no source or compressed-cache read/decode.
                    counters["source_reads"] = counters["decoded_tiles"] = 0
                counters["raw_tile_requests"] = (
                    end_requests - start_requests
                    if start_requests is not None and end_requests is not None
                    else None
                )
                counters["input_copy_bytes"] = (
                    end_bytes - start_bytes
                    if start_bytes is not None and end_bytes is not None
                    else None
                )
        samples.append(
            {
                "action_id": action_id,
                "phase": "resident" if display_only else "fresh_tile",
                "dropped": first is None,
                "input_to_display_ms": first.get("input_to_present_ms") if first else None,
                "frame_interval_ms": first.get("presentation_interval_ms") if first else None,
                "gpu_ms": first.get("gpu_ms") if first else None,
                "tile_read_ms": None,
                "upload_ms": None,
                "logical_counters": counters,
                "observed_native_residency": first.get("class") if first else None,
                "level": first.get("level") if first else None,
                "center": action.get("center"),
                "zoom": action.get("zoom"),
            }
        )
    run = {
        "schema": RUN_SCHEMA,
        "context": context,
        "timing_kind": "metal_drawable_presented",
        "samples": samples,
        "resources": raw.get("resource", {}),
        "hardware": {"memory_bandwidth": None, "power": None},
        "actions": replay_actions(),
        "phase_note": "resident means fixed-viewport display actions; fresh_tile means camera actions that may hit cached tiles. observed_native_residency records actual native raw-tile residency separately.",
    }
    validate_run(run)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, help="Native Agent-window run JSON")
    parser.add_argument("--browser", type=Path, help="Isolated browser baseline run JSON")
    parser.add_argument(
        "--output", type=Path, required=True, help="New comparison JSON; never overwritten"
    )
    parser.add_argument(
        "--serve-browser", action="store_true", help="Serve a protected isolated stable baseline"
    )
    parser.add_argument(
        "--normalize-native",
        action="store_true",
        help="Normalize native diagnostics with independently verified context",
    )
    parser.add_argument("--stable-root", type=Path)
    parser.add_argument("--test-root", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument(
        "--context", type=Path, help="Benchmark context JSON, including exact viewport points/DPR"
    )
    parser.add_argument("--ready-file", type=Path, help="New JSON with the isolated browser URL")
    args = parser.parse_args()
    try:
        if args.serve_browser:
            if not all(
                [args.stable_root, args.test_root, args.cache, args.context, args.ready_file]
            ):
                parser.error(
                    "--serve-browser requires --stable-root, --test-root, --cache, --context, --ready-file"
                )
            serve_browser_baseline(args)
            return
        if args.normalize_native:
            if not args.native or not args.context:
                parser.error("--normalize-native requires --native and --context")
            report = normalize_native_report(
                json.loads(args.native.read_text()), json.loads(args.context.read_text())
            )
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, allow_nan=False)
                handle.write("\n")
            print(json.dumps(report, indent=2, allow_nan=False))
            return
        if not args.native or not args.browser:
            parser.error("comparison requires both --native and --browser")
        report = compare_runs(
            json.loads(args.native.read_text()), json.loads(args.browser.read_text())
        )
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
