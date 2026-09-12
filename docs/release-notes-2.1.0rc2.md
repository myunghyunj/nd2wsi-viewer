# nd2wsi-viewer v2.1 RC2 — macOS Apple silicon

The original app name, bundle identity and independent multiwindow support are
retained. This is a **prerelease**, not a replacement for stable v2.0.0. No Windows
release or stable appcast changes are included.

## Opening and recovery

- The standard viewer remains the default for both User and Agent windows.
  Metal is opt-in through **Open in Metal**. User defaults will not change before
  annotation/measurement/export parity, even if a future performance gate passes.
- Supported cached uint16 fluorescence slides (1–8 channels, non-RGB, non-plate)
  can use direct Metal display. Unsupported inputs retain the standard path.
  Native inspection never builds, repairs, migrates or quarantines shared caches.
- Each logical opening can fall back at most once, including across processes.
  Persistent GPU failures are recorded locally by sampled file fingerprint,
  full app version and canonical error kind. Timeouts, 503, cache/I/O errors and
  memory pressure are not permanent blocks. **Retry in Metal** explicitly retries.
- Camera center/scale, window/gamma, RGB LUT and channel visibility survive a
  same-role window handoff. Annotations are not merged. Rotated/flipped standard
  views must be reset before opening in Metal; they are not silently discarded.
- Update selection remains OS/CPU/channel-specific. Windows stays on stable;
  stable macOS installs do not automatically enter the RC channel. Managed
  multiwindow updates are manual downloads; close all app windows before replacing.

## Measured packaged replay — 2026-09-13

One Apple M4 Pro Mac, macOS 27, a 2560×1440 display at 1×, and a 1024×650 viewport.
The same isolated copy of a 3.15 GB two-channel ND2 (26160×30092) and its existing
0.53 GB cache were used in the packaged standard WebKit and Metal paths.
One warmup pair was excluded, then **five alternating pairs of 44 actions** were
recorded. No OS page-cache purge or research-cache rebuild was performed.

**These are different timing endpoints, not a speed comparison.** Metal measures
replay dispatch to drawable presentation; WebKit records dispatch to three stable
rAF callbacks after OpenSeadragon reports loaded. Browser input-to-present remains
unmeasured. No speedup ratio is justified, and the automatic Metal gate remains
**unverified/off**. These results do not generalize to untested Macs.

| Fixed action workload | Metal presented p50 / p95 | Browser rAF proxy p50 / p95 | Samples / missing |
| --- | --- | --- | --- |
| Prepared viewport: gamma/visibility changes | 18.80 / 22.60 ms | 37.00 / 68.00 ms | 115 each / 0 |
| Camera movement and zoom (not all streamed) | 36.41 / 62.01 ms | 68.00 / 128.80 ms | Metal 103/105, browser 105/105 |

Across repeats, prepared-viewport p50 ranged 16.27–19.24 ms for Metal and
37.00–39.00 ms for the browser proxy; p95 ranged 21.04–23.93 and 49.90–68.00 ms.
Camera p50 ranges were 34.98–36.44 and 68.00–69.00 ms; p95 ranges were
56.71–64.93 and 108.00–179.00 ms. Ranges are per-run percentiles, not confidence
intervals. They do not resolve the endpoint mismatch.

Actual native raw-tile residency is recorded separately from action type:

| Observed Metal tile state | Presented samples | p50 / p95 | Per-run p50 range / p95 range |
| --- | --- | --- | --- |
| Resident | 138 | 19.07 / 24.05 ms | 16.44–19.26 / 20.92–24.83 ms |
| Streamed | 80 | 39.43 / 64.93 ms | 37.22–42.18 / 57.84–66.06 ms |

Two first-Fit actions have no attributable complete presented-frame timestamp
(runs 1 and 5); these remain missing, not zero latency or a measured success.
No native renderer errors were reported in the five measured runs. All 115
prepared-viewport display changes caused zero additional raw reads/decodes,
CPU RGB/JPEG operations and pixel readbacks by the diagnostic logical counters.
Browser camera drift from the commanded sequence was below 2×10⁻¹² image pixels.

Native process peak RSS ranged 450–456 MiB (physical footprint 373–379 MiB).
Browser server/shell peak RSS was 579–610 MiB **excluding WebKit helper and
compositor processes**; browser total memory is unmeasured. Hardware bandwidth,
power and instrumentation overhead are unmeasured. On-demand frame intervals
include replay cadence and must not be reported as sustained FPS. Metal nearest
sampling and browser-filtered JPEG pixels also differ at scaled views.

The test-only replay input guard consumes input only to its own Agent window;
other windows remain interactive. Earlier interrupted/contaminated preflight
records were retained separately, including one physical-trackpad interference
run. They were not relabelled as successful measured repeats.

## Validation and remaining scope

- Final source regression: **1278 passed, 22 skipped, 5 real-data tests deselected**.
  Existing NumPy/nd2 deprecation warnings remain. Additional opt-in Apple-GPU
  lifecycle/reference tests and packaged fault scenarios were exercised locally.
- Final shipping-package recovery scenarios: **10/10 passed**, with all 18
  isolated sessions closed. These cover metadata timeout/invalid data, fatal GPU and
  memory-pressure branches, first-screen tile failure, 503 recovery, close races,
  duplicate opening IDs, manual handoff and standard-viewer failure.
- Installed-app controls and Metal → standard → Metal state roundtrip were tested
  in fresh isolated windows. Full Python bytecode and static assets match source.
- Source/cache preservation was checked. No original reader/storage format or
  shared annotation data was changed by the validation workflow.
- Input packing (`tobytes`) and the shared-buffer upload remain separate copy
  points; transport may add copies. This is **not disk-to-display zero-copy**.
  The fused display pass has no intermediate attachments or production CPU pixel
  readback. Raw tile budgets are per window, not a global multiwindow memory cap.
- Direct mmap sharing, buffer pooling/MTLHeap, plate Metal playback, inference and
  annotation parity remain outside RC2. A GPU trace has not been inspected in a
  compatible Xcode GUI; software counters are not hardware bandwidth evidence.
- Distribution is ad-hoc signed, **not Developer ID notarized**. This RC includes
  no new Windows binary or claim of Windows hardware verification.
