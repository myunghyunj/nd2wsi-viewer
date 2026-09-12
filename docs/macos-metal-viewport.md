# Native Metal viewport RC (macOS, Apple silicon)

Version 2.1.0rc1 keeps the original `nd2wsi-viewer` name, bundle identity and file
associations. Its macOS entry automatically selects native Metal for supported
Apple-silicon hardware and compatible cached fluorescence slides. Unsupported
input or native startup failure uses the existing standard viewer. The native
window also opens the same slide in a separate standard window for annotations,
measurements and export. Windows distribution remains at the existing release.

Update checks select a compatible OS/CPU asset and version channel. RC installs
accept newer RC or final releases; stable installs do not automatically opt into
RC. Managed multiwindow installations download manually: close all windows
before replacing the application. The stable Sparkle appcast is unchanged.

## Scope and data flow

One existing, non-plate uint16 fluorescence slide (1–8 channels) with a valid
existing cache is opened read-only. Existing reader and cache formats remain
unchanged. Reduced levels are decompressed by the existing storage backend.

The raw adapter supplies bounded CYX little-endian tiles over a capability-
protected loopback connection. The native window copies each received tile
into a shared Metal buffer and retains it for reuse. **This is not disk-to-
display zero-copy**: decoding, packing, transport, and initial buffer staging
remain CPU-side work. Zero additional input work on resident display changes
is the relevant first acceptance criterion.

The native AppKit/MTKView directly presents drawable textures. Each viewport
render pass draws all available visible tiles. A fused fragment shader reads
uint16 channels, applies float32 window/gamma and color lookup, adds channels,
clips, and truncates to 8-bit. There are no intermediate render attachments,
RGB/JPEG responses, or production pixel readbacks. A single final BGRA8Unorm
attachment is cleared and stored. No memoryless attachment is necessary for
this fused computation. The test-only offscreen ABI deliberately reads back
pixels for reference validation and is not part of screen presentation.

## Verification contract

- Test packaged native controls: drag/pointer zoom, fit, window low/high,
  gamma, channel visibility, and color LUT.
- Compare raw geometry at every pyramid level, odd edge tiles, channel order,
  pixel centers, and GPU output against the CPU renderer before JPEG. GPU
  `pow` rounding can differ by one output code value; preserve exact high
  uint16 intensities before display conversion.
- Keep sampling explicit. The first native path uses nearest-neighbor samples
  at a selected pyramid level. The browser filters already-composited JPEG
  pixels; scaled screenshots are not expected to be byte-identical.
- Measure resident display changes and streamed view changes separately.
  Drawable presented timestamps are not interchangeable with command-buffer
  completion or browser requestAnimationFrame callbacks.
- On-demand presentation intervals include idle/action cadence. They are not
  saturation frame times or a claim of sustained FPS.
- Keep capture/profiling runs separate from uninstrumented timing runs. Inspect
  the saved GPU capture rather than treating a software pass counter as proof.
- Report the raw-buffer/request budget separately from actual process memory.
  Codec scratch, drawables, framework allocations, and file-backed pages must
  not silently disappear from a claim about total memory.
- Hardware bandwidth and power are null/unmeasured until a hardware instrument
  supplies them. Logical input byte counters do not estimate system bandwidth.

## Local build

Install the locked project extras and PyInstaller into this worktree's own
environment. `packaging/build_metal_rc.sh /absolute/new-output-directory` builds
the original-name arm64 app and architecture-labelled DMG. It requires pinned
Sparkle 2.9.6 and an isolated `ND2WSI_SMOKE_FILE`; packaged ND2 export and SVS
checks are mandatory. Signing is ad-hoc, not Developer ID notarization. The
script does not install or publish. The older `build_metal_viewport.sh` remains
an isolated developer package builder.

The executable accepts a slide path, `--session-root`, and a **new** `--report`
path. Without a source path, its own native file panel selects a slide.
`--capture-enabled` enables diagnostic GPU capture; use this in a separate run
from ordinary timing measurements. `--renderer browser` explicitly selects the
standard viewer; `--renderer metal` exposes native startup errors instead of
falling back. Automatic mode falls back for unsupported sources; native display
itself never rebuilds or migrates a cache.

## Explicitly deferred

Direct compatible ND2 mmap wrapping, asynchronous raw-buffer reuse beyond the
current bounded tile cache, prefetch policy tuning, display-space bilinear
filtering, full-viewport Auto-LUT statistics, plate playback, H&E, inference,
and clinical or quantitative validation are not claims of this beta.

The acceptance report delivered with a build records what actually passed and
what remains unmeasured. A kernel test or successful build alone is not native
viewport completion.

Apple references: [Apple silicon Metal optimization](https://developer.apple.com/videos/play/wwdc2020/10632/),
[drawable presentation timestamps](https://developer.apple.com/documentation/metal/mtldrawable/presentedtime),
[programmatic capture](https://developer.apple.com/documentation/xcode/capturing-a-metal-workload-programmatically),
and [hardware counter interpretation](https://developer.apple.com/documentation/xcode/analyzing-apple-gpu-performance-using-counter-statistics).
