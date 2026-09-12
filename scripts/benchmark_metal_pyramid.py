"""CPU/Metal exactness and timing on a read-only source, using fresh output stores.

Fresh cache builds are NOT guaranteed cold OS page-cache measurements. No
purge, source mutation, Instruments counters or power sampling is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from nd2wsi.cache import quick_fingerprint, write_manifest
from nd2wsi.convert import convert, open_store
from nd2wsi.metal import device_info, diagnostics, reduce2x, reset_diagnostics
from nd2wsi.migration import pack_directory
from nd2wsi.reader import PlaneSelection


def microbenchmark() -> list[dict]:
    rows = []
    for size in (1024, 2048):
        a = np.random.default_rng(10632).integers(0, 65536, (1, size, size), dtype=np.uint16)
        timings = {}
        expected = None
        for mode in ("cpu", "metal"):
            os.environ["ND2WSI_GPU_PYRAMID"] = "1" if mode == "metal" else "0"
            values = []
            for index in range(9):
                start = time.perf_counter()
                if mode == "cpu":
                    out = np.rint(
                        a.reshape(1, size // 2, 2, size // 2, 2).mean(axis=(2, 4), dtype=np.float32)
                    ).astype(a.dtype)
                else:
                    out = reduce2x(a)
                elapsed = time.perf_counter() - start
                if mode == "cpu":
                    expected = out
                elif out is None or not np.array_equal(out, expected):
                    raise RuntimeError("GPU microbenchmark did not match CPU")
                if index:
                    values.append(elapsed)
            timings[mode] = float(np.median(values))
        rows.append(
            {
                "shape": list(a.shape),
                "cpu_seconds": timings["cpu"],
                "metal_including_copy_wait_seconds": timings["metal"],
                "speedup": timings["cpu"] / timings["metal"],
            }
        )
    return rows


def worker(source: Path, output: Path, mode: str, workers: int, tile: int):
    os.environ["ND2WSI_GPU_PYRAMID"] = "1" if mode == "metal" else "0"
    init_start = time.perf_counter()
    if mode == "metal" and not device_info().get("supported"):
        raise RuntimeError("No actual Metal GPU available; refusing a fake GPU benchmark")
    initialization_seconds = time.perf_counter() - init_start
    before = quick_fingerprint(source)
    output.mkdir(exist_ok=False)
    store = output / "cache" / "store.ome.zarr"
    reset_diagnostics()
    start = time.perf_counter()
    convert(source, store, tile=tile, workers=workers, overview=True, progress=True)
    build_seconds = time.perf_counter() - start
    root, attrs = open_store(store)
    meta = attrs["nd2wsi"]
    ov = meta["overview_of"]
    write_manifest(
        store.parent,
        source,
        before,
        PlaneSelection().describe(),
        meta["selection"]["z"],
        tile,
        (ov["channels"], ov["height"], ov["width"]),
        meta["dtype"],
        kind="overview",
        storage=meta["storage"],
    )
    start = time.perf_counter()
    pack_directory(store.parent, output / "cache.nd2svs", source=source)
    pack_seconds = time.perf_counter() - start
    digest = hashlib.sha256()
    files = 0
    for path in sorted(store.rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as f:
            sha = hashlib.file_digest(f, "sha256").hexdigest()
        digest.update((str(path.relative_to(store)) + ":" + sha + "\n").encode())
        files += 1
    after = quick_fingerprint(source)
    assert before == after, "Source changed during benchmark"
    metrics = diagnostics()
    if mode == "metal" and (not metrics["gpu_calls"] or metrics["cpu_fallbacks"]):
        raise RuntimeError(f"Metal path did not exclusively execute: {metrics}")
    result = {
        "mode": mode,
        "tile": tile,
        "workers": workers,
        "backend_initialization_seconds_excluded_from_build": initialization_seconds,
        "cache_build_seconds": build_seconds,
        "pack_verify_seconds": pack_seconds,
        "total_seconds": build_seconds + pack_seconds,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "pyramid_all_files_digest": digest.hexdigest(),
        "files_hashed": files,
        "source_fingerprint_unchanged": True,
        "source_fingerprint": before,
        "metal": metrics,
        "device": device_info() if mode == "metal" else None,
    }
    (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--tile", type=int, default=512)
    parser.add_argument("--worker", choices=["cpu", "metal"])
    parser.add_argument("--order", default="cpu,metal,metal,cpu")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    if args.worker:
        worker(source, args.output, args.worker, args.workers, args.tile)
        return
    modes = args.order.split(",")
    if set(modes) != {"cpu", "metal"}:
        parser.error("--order must contain both cpu and metal, and no other mode")
    if args.workers < 1 or args.tile < 2:
        parser.error("--workers must be positive and --tile must be at least 2")
    args.output.mkdir(exist_ok=False, parents=True)
    micro = microbenchmark()
    results = []
    for index, mode in enumerate(modes):
        if mode not in ("cpu", "metal"):
            raise ValueError("Invalid mode")
        destination = args.output / f"{index + 1}-{mode}"
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--source",
                str(source),
                "--output",
                str(destination),
                "--worker",
                mode,
                "--workers",
                str(args.workers),
                "--tile",
                str(args.tile),
            ],
            check=True,
        )
        results.append(json.loads((destination / "result.json").read_text()))
    assert len({row["pyramid_all_files_digest"] for row in results}) == 1
    medians = {
        mode: float(np.median([r["total_seconds"] for r in results if r["mode"] == mode]))
        for mode in ("cpu", "metal")
    }
    report = {
        "source": str(source),
        "source_bytes": source.stat().st_size,
        "condition": "fresh cache outputs; OS source-page-cache not flushed; order reported",
        "order": args.order,
        "microbenchmark": micro,
        "runs": results,
        "all_compressed_pyramid_files_and_metadata_identical": True,
        "median_total_seconds": medians,
        "total_speedup": medians["cpu"] / medians["metal"],
        "unmeasured": [
            "true cold OS cache",
            "total system-memory bandwidth",
            "frame time",
            "inference latency",
            "power",
        ],
        "artifacts_retained": True,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
