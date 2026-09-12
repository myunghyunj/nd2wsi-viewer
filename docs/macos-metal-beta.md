# macOS Metal beta 2.1.0b1: review and measured scope

This is a separate, local Apple-silicon beta, not a stable release or a Windows update.
It implements exact GPU pyramid reduction only. Existing caches do not need to be
rebuilt, and existing cached-slide rendering is not accelerated by this beta.

## Assessment of the proposed four phases

The architectural direction in WWDC 2020 sessions 10632 and 10686 is sound:
shared physical memory removes PCIe transfer requirements, and reducing unnecessary
bandwidth is valuable. It does **not** imply that arbitrary application pipelines
already avoid copies or that a shared resource requires no synchronization.

1. **GPU pyramid: implement exact math, not replacement filtering.** The existing
   contract is 2x2 box mean, nearest ties-to-even integer rounding, floor-sized
   levels, and repeated edges after an axis collapses to one pixel. Automatic
   mipmap filtering is implementation-dependent. Lanczos is a different filter
   with possible ringing. Both would change cached scientific pixels.
2. **Direct mmap: conditional future optimization.** Modern uncompressed ND2 can
   expose strided mapped views. Compressed/legacy ND2 and computed projections
   cannot generally do so. Current `.nd2svs` files contain compressed Zarr data in
   SQLite, not mmap-ready ushort planes. Mapping compressed BLOBs does not decode
   pixels. `bytesNoCopy` additionally requires page-aligned address and length,
   a single VM region, device size limits, ownership through completion, and
   explicit CPU/GPU synchronization.
3. **Native display: a separate renderer.** Start with one viewport render pass
   drawing multiple visible microscopy tiles and a fused per-pixel window/LUT/
   composite shader. Hardware tile memory is not a 512px microscopy tile.
   Memoryless attachments cannot supply persistent overview pixels or arbitrary
   neighboring source samples. Histograms need cross-tile reduction; gradient
   energy requires boundary halos. LUT/gamma/visibility are uniforms or textures,
   not just load/store actions or blend constants. The existing browser path is
   unchanged in this beta, so JPEG/browser copies remain.
4. **Inference: model-specific future work.** `.all` makes ANE eligible; it does
   not guarantee ANE placement or no internal copies. Model dtype, layout,
   normalization and operator support must be verified. MLMultiArray has no
   uint16 element type. IOSurface-backed image input can remove some copies when
   its format matches the model, but it is not a universal MTLBuffer handoff.

The precision rule is **ushort for raw storage, uint/float32 for exact sums and
quantitation, half only after normalization and a measured display tolerance**.
Direct uint16-to-half loses precision and can exceed half's finite range.

Revised invariant: preserve original scientific values, avoid redundant copies,
and expose unavoidable decode/layout/presentation costs honestly.

## Implemented path

Decoded NumPy block → one copy into owned page-aligned shared mmap → Metal
`bytesNoCopy` input/output wrappers → exact integer reduction → completion wait
→ NumPy view over shared output → unchanged CPU compression and cache writer.

- Opt-in `ND2WSI_GPU_PYRAMID=1`, native Darwin arm64 only. The beta launcher enables
  the flag by default; the normal source launcher remains opt-in.
- uint8/uint16 kernels accumulate into uint and implement ties-to-even exactly.
- Unsupported types/platforms, missing library/device, shape/size limits or Metal
  errors use the existing CPU reducer. Runtime failure is reported once per reason.
- One dispatch is in flight; an input/output pair is capped at 128 MiB. Completed
  output arrays may coexist: the pair limit is **not** a global memory limit.
- No source reader, storage format, browser/server, plate, annotation, measurement,
  export or Windows packaging implementation was changed.
- Distinct bundle ID `com.nd2wsi.viewer.metal-beta`; no Sparkle framework/feed,
  automatic stable update, document associations or stable-app replacement.

This uses a small Objective-C/Metal C-ABI bridge rather than introducing a new
Python GPU framework. It builds with Command Line Tools and compiles the integer
Metal kernels at runtime. Metal is available on the tested M4 Pro outside the
tool sandbox; unavailable hardware is reported, never counted as a GPU success.

## Validation and performance, 2026-09-12

- Full regression suite: **925 passed, 9 skipped**; pre-existing ND2/NumPy
  deprecation warnings remain. The new Metal suite: **57 passed, no skips** on
  actual M4 Pro hardware, including extreme values, ties, odd/noncontiguous input,
  buffer lifetime, concurrent callers, sparse/collapsed levels and error fallback.
- Packaged app: native Metal self-test and ND2 exact-pixel/calibration/channel
  export plus JPEG/JPEG2000 SVS/TIFF checks passed. This is an ad-hoc signed local
  beta, not Apple-notarized or a public update.
- Existing cache/source/export/unit and new Metal tests with the flag enabled:
  **151 passed, 1 skipped**. Packaged WKWebView GUI self-test opened the example
  slide, verified bridge/version/canvas and closed successfully (1.72s). That GUI
  run reused an existing example cache (zero GPU reducer calls); the separate
  packaged scientific smoke exercised 36 successful GPU reductions.
- Read-only 3,148,984,320-byte, two-channel uint16 ND2; source on external SSD,
  outputs on internal APFS; 512px storage tiles, 4 CPU workers. Every run creates
  fresh caches. OS page cache is **not** flushed. Backend initialization is timed
  separately and excluded from construction. No concurrent agent build/test jobs
  ran during the final benchmark, but other user/OS activity was not controlled.

| Final six-run order | Pyramid build (s) | Pack + verify (s) | Total (s) |
|---|---:|---:|---:|
| CPU | 19.01 | 1.93 | 20.94 |
| Metal | 3.05 | 2.00 | 5.04 |
| Metal | 2.85 | 1.94 | 4.79 |
| CPU | 3.65 | 1.91 | 5.56 |
| CPU | 6.09 | 2.03 | 8.12 |
| Metal | 20.53 | 2.14 | 22.67 |

Median total: CPU 8.12s, Metal 5.04s (1.61x in this small sample). The overlapping
ranges and Metal's 22.67s run prevent a stable speedup claim. The test does not
establish true cold-build performance, tail latency or frame-time improvement.
Reduction-only median with staging/wait was 5.17x and 12.59x faster for 1024² and
2048² uint16 inputs; those are not whole-viewer speedups.

All **2,118 compressed pyramid and metadata files** matched across all six runs.
Each Metal build completed 1,620 dispatches with zero fallback. The explicit
input staging copy counter was 4,197,345,760 bytes across all levels; it is **not**
zero and is **not** a total system-memory-bandwidth measurement. Shared output
views totaled 1,049,336,440 bytes. Largest active staging pair was 33,488,896 bytes.
RSS high-water marks were approximately 3.49–3.53GB CPU and 3.32–3.40GB Metal,
including mapped source pages; do not interpret that as private heap allocation.

Not implemented/measured: native viewport Metal display, Instruments bandwidth,
strict end-to-end copy counts, autofocus/histogram tile shaders, Core ML inference,
energy or tiles/sec/watt. Resolve variability with instrumentation before promotion.

## Use and reproduction

Open `nd2wsi-viewer Metal Beta.app` alongside the existing stable app. Use a copied
example initially. GPU acceleration is exercised only when a compatible new
pyramid is built, not when opening an already cached slide. Keep originals and
annotations; do not delete research caches simply to test the beta.

```sh
uv sync --frozen --all-extras
bash nd2wsi/metal/build_native.sh
ND2WSI_GPU_PYRAMID=1 .venv/bin/python -m pytest tests/test_metal_pyramid.py -q
PYTHONPATH=. .venv/bin/python scripts/benchmark_metal_pyramid.py \
  --source /absolute/path/to/scan.nd2 --output /absolute/new/benchmark-directory
```

Packaging additionally needs PyInstaller 6.22.2 and hooks 2026.7. Run
`packaging/build_metal_beta.sh` with a new absolute output directory and
`ND2WSI_SMOKE_FILE` set to a copied public example. No step publishes a release.

## Sources

- Supplied transcripts: WWDC 2020 10632 and 10686.
- [Optimize Metal Performance for Apple silicon Macs](https://developer.apple.com/videos/play/wwdc2020/10632/)
- [Explore the new system architecture of Apple silicon Macs](https://developer.apple.com/videos/play/wwdc2020/10686/)
- [Metal buffer no-copy requirements](https://developer.apple.com/documentation/metal/mtldevice/makebuffer(bytesnocopy:length:options:deallocator:))
- [Mipmap filtering semantics](https://developer.apple.com/documentation/metal/generating-mipmap-data)
- [MPS Lanczos filtering](https://developer.apple.com/documentation/metalperformanceshaders/mpsimagelanczosscale)
- [Shared-resource synchronization](https://developer.apple.com/documentation/metal/mtlstoragemode/shared)
- [GPU capabilities](https://developer.apple.com/metal/capabilities/)
- [Core ML compute-unit choices](https://developer.apple.com/documentation/coreml/mlcomputeunits)
- [Core ML data types](https://developer.apple.com/documentation/coreml/mlmultiarraydatatype)
- [Optimize your Core ML usage](https://developer.apple.com/videos/play/wwdc2022/10027/)
