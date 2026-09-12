"""Run an isolated packaged RC replay warmup and five alternating pairs.

This diagnostic deliberately cannot pass the Metal default gate: WebKit rAF
and Metal drawable-presented are different clock endpoints. It preserves the
raw action records, variance and actual native residency instead of inventing
a browser input-to-present number or a bandwidth estimate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from benchmark_metal_viewport import compare_runs, normalize_native_report, percentiles


def aggregate_pairs(pairs):
    if len(pairs) < 5:
        raise ValueError("at least five measured pairs are required; exclude warmup")
    rows = []
    for index, pair in enumerate(pairs):
        native, browser = pair["native"], pair["browser"]
        comparison = compare_runs(native, browser)
        if index and comparison["context"] != rows[0]["comparison"]["context"]:
            raise ValueError("conditions changed between paired repeats")
        rows.append({"pair": index + 1, "order": pair["order"], "comparison": comparison})

    def stats(renderer, select):
        repeats = []
        pooled = []
        dropped = missing = total = 0
        for pair in pairs:
            samples = [s for s in pair[renderer]["samples"] if select(s)]
            values = [s["input_to_display_ms"] for s in samples
                      if not s["dropped"] and s.get("input_to_display_ms") is not None]
            repeats.append(percentiles(values))
            pooled.extend(values)
            total += len(samples)
            dropped += sum(s["dropped"] for s in samples)
            missing += sum(s.get("input_to_display_ms") is None for s in samples)
        return {"pooled_ms": percentiles(pooled), "per_run_ms": repeats,
                "run_p50_range_ms": _range([r["p50"] for r in repeats]),
                "run_p95_range_ms": _range([r["p95"] for r in repeats]),
                "actions": total, "dropped": dropped, "missing": missing}

    workloads = {}
    for phase in ("resident", "fresh_tile"):
        workloads[phase] = {renderer: stats(renderer, lambda s, p=phase: s["phase"] == p)
                            for renderer in ("native", "browser")}
    residency = {kind: stats("native", lambda s, k=kind: s.get("observed_native_residency") == k)
                 for kind in ("resident", "streamed")}
    return {"schema": "nd2wsi-rc2-paired/1", "measured_pairs": len(pairs),
            "performance_gate": {"state": "unverified", "automatic_metal_default": False,
                                 "reason": "No validated common presentation endpoint. Browser rAF proxy cannot be compared with Metal drawable-presented latency."},
            "browser_input_to_present_ms": None,
            "fixed_action_workloads": workloads, "actual_native_residency": residency,
            "phase_note": "resident workload = 23 fixed-viewport gamma/visibility actions; fresh_tile workload = 21 camera actions, not all streamed. Actual cache hits/misses remain in per-action records and native residency groups.",
            "comparison_limitations": ["Diagnostic counters and viewport sizing are enabled in both replay processes; overhead has not been isolated.",
                                       "Nearest-neighbor raw Metal samples differ from browser-filtered JPEG display.",
                                       "OS file cache was not purged. New independent processes reset app-local tile caches only.",
                                       "Action dispatch is not physical user input. Bandwidth and power remain unmeasured."],
            "pairs": rows}


def _range(values):
    values = [v for v in values if v is not None]
    return [min(values), max(values)] if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-after-warmup", action="store_true",
                        help="reuse a complete saved warmup only; refuse any existing measured pair")
    args = parser.parse_args()
    root, source = args.test_root.resolve(strict=True), args.source.resolve(strict=True)
    app = args.app.resolve(strict=True)
    if not source.is_relative_to(root) or app.suffix != ".app":
        parser.error("source must be an isolated copy under --test-root and app must be packaged")
    if args.resume_after_warmup:
        if not args.output.is_dir() or list(args.output.glob("pair-*")):
            parser.error("resume requires a completed warmup and no measured pair outputs")
        compare_runs(json.loads((args.output / "warmup-native.normalized.json").read_text()),
                     json.loads((args.output / "warmup-browser.json").read_text()))
    else:
        args.output.mkdir(parents=True, exist_ok=False)
    executable = app / "Contents/MacOS/nd2wsi-viewer"
    context = json.loads(args.context.read_text())
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("ND2WSI_VIEWPORT_", "ND2WSI_DIAGNOSTIC_"))}
    environment.update(ND2WSI_WINDOW_SESSION_ROOT=str(args.output / "sessions"),
                       ND2WSI_RENDERER_STATE_ROOT=str(args.output / "renderer-state"),
                       PYINSTALLER_RESET_ENVIRONMENT="1")
    pairs = []
    for index in range(1 if args.resume_after_warmup else 0, 6):
        label = "warmup" if index == 0 else f"pair-{index}"
        order = ["browser", "native"] if index % 2 else ["native", "browser"]
        pair = {"order": order}
        for renderer in order:
            output = args.output / f"{label}-{renderer}.json"
            env = dict(environment)
            common = [str(executable), "--agent-window", str(source)]
            if renderer == "native":
                env.update(ND2WSI_VIEWPORT_REPLAY="1", ND2WSI_VIEWPORT_AUTOQUIT="1",
                           ND2WSI_VIEWPORT_WIDTH=str(context["viewport_points"][0]),
                           ND2WSI_VIEWPORT_HEIGHT=str(context["viewport_points"][1]))
                command = common + ["--renderer", "metal", "--report", str(output)]
            else:
                command = common + ["--renderer", "browser", "--browser-replay-report", str(output),
                                    "--benchmark-context", str(args.context.resolve()),
                                    "--benchmark-test-root", str(root)]
            print(f"{label}: packaged {renderer} starting", flush=True)
            started = time.monotonic()
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=480)
            log = {"returncode": result.returncode, "elapsed_seconds": time.monotonic()-started,
                   "stdout": result.stdout, "stderr": result.stderr}
            (args.output / f"{label}-{renderer}.process.json").write_text(json.dumps(log, indent=2))
            raw = json.loads(output.read_text())
            if result.returncode and not (renderer == "browser" and len(raw.get("samples", [])) == 44):
                raise RuntimeError(f"{label} {renderer} failed before a complete action record; inspect {output}")
            pair[renderer] = normalize_native_report(raw, context) if renderer == "native" else raw
            # Failed/dropped presentation-proxy samples stay in the measured
            # repeats and release summary; never replace them with a clean run.
            if renderer == "native":
                output.with_suffix(".normalized.json").write_text(json.dumps(pair[renderer], indent=2))
            print(f"{label}: {renderer} finished, {len(pair[renderer]['samples'])} actions", flush=True)
        compare_runs(pair["native"], pair["browser"])
        if index:
            pairs.append(pair)
    summary = aggregate_pairs(pairs)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"gate": summary["performance_gate"], "pairs": len(pairs)}, indent=2))


if __name__ == "__main__":
    main()
