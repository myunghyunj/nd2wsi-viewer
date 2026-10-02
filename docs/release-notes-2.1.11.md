# nd2wsi-viewer 2.1.11

ND2 images now open with automatic brightness/contrast and a fitted LUT graph. The display estimate ignores zero padding and uses a 99.5th-percentile bright limit, so rare bright pixels are less likely to squeeze dim signal into the bottom of the graph. Bright backgrounds remain part of the estimate, and RGB Auto uses a shared window across components.

Manual contrast edits and display handoffs take priority. Changing time or Z does not repeatedly reset contrast. Full-range histograms, native pixel values, raw exports and annotations remain available unchanged. Auto is a display aid, not artifact detection or a quantitative correction; bright detail above the automatic limit is clipped in the display and can be inspected by widening the window.

Plate time and Z navigation, playback, autofocus and cache-status requests now share a dedicated controller with direct regression tests. The refactor does not claim a performance improvement.

The macOS download targets Apple silicon and uses an ad-hoc code signature plus signed Sparkle updates. It is not Developer ID notarized. The Windows download is an x64 portable build; Windows 11 ARM64 runs it through x64 emulation.
