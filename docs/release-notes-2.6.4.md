# 2.6.4

Astromap can import valid regions from a TXT that also contains malformed ROI rows.

- **Review individual rows.** The import panel shows how many regions are ready and lists skipped rows with their original line, INDEX, coordinates and note. Confirm the matching image dimensions before importing. Existing annotations and the original TXT remain unchanged.
- **Optional one-pixel recovery.** A legacy rectangle whose inclusive width or height is zero can be restored to one pixel by an explicit choice. The report shows the original and corrected coordinates. Larger endpoint reversals, duplicate INDEX values and out-of-image coordinates are withheld, not guessed or silently clipped.
- **Tracing window support.** JPG/TXT pair loading also accepts valid regions and keeps a visible report of skipped or repaired rows while navigating. A file with no usable regions leaves the previous list intact.
- **Validation remains strict where needed.** Unrecognized headers, resource limits and incompatible image aspect ratios still prevent import. Correcting the reference dimensions rechecks the original rows, so regions are not permanently discarded during review.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
