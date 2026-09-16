# macOS 2.1.2 LUT controls: verification

The LUT panel starts at the complete integer data range (0–65,535 for uint16).
Auto-fit LUT range is an optional checkbox that crops the histogram axes without
changing image contrast. Scrolling zooms at the pointer; horizontal scrolling or
empty-space dragging pans; double-clicking restores the full axis. Numeric Min/Max
fields remain available. Contrast updates at a bounded 100 ms cadence during a
handle drag, and release flushes the latest pending settings.

Zoom uses finer counts from the same sampled pyramid level; it is not a new
full-resolution census of the source image. Native horizontal input is scoped to
visible LUT plots while existing plate gestures and neighboring controls retain
their routing.

## Validation (2026-09-16, local Apple-silicon macOS)

- Project lint: passed.
- Full non-realdata suite: 1,288 passed, 22 skipped, 5 deselected.
- Lock file: checked against package version 2.1.2.
- Packaged and installed source equality: 38 Python modules and 18 static assets.
- Application signature: deep, strict verification passed.
- Packaged and installed smoke: ND2 read and pixel-exact export; JPEG and
  JPEG2000 SVS pyramids; pixel-exact TIFF region export.
- Native WKWebView pointer replay: contrast changed during an uninterrupted
  drag, before pointer release; the final refresh matched the final LUT values.
- Native graph checks: full/automatic ranges, zoom, pan, drag, double-click reset,
  and contrast preservation passed in isolated Agent windows.

This release updates macOS Apple silicon. The published Windows package remains v2.0.0.
Programmatic input checks do not replace physical trackpad testing on other Macs.
Detailed local logs and preservation inventories are retained outside Git.

## Public release verification

- [v2.1.2](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.2) is tagged from
  `main` commit `8d4fee12cfdf15fcdb8f280e3a75d87d98875215`.
- [CI](https://github.com/myunghyunj/nd2wsi-viewer/actions/runs/35099446383): lint,
  cold package installation, and Python 3.11/3.12/3.13 suites passed.
- [Real ND2](https://github.com/myunghyunj/nd2wsi-viewer/actions/runs/35099567205):
  the exact release commit passed the real-data workflow before tagging.
- [Windows compatibility](https://github.com/myunghyunj/nd2wsi-viewer/actions/runs/35099446401):
  1,183 tests passed, 124 skipped and 5 deselected; 5 real-data tests passed and
  1 skipped. Packaged headless/GUI checks passed on hosted x64 Windows and
  Windows 11 ARM64 using x64 emulation, including Korean and space-containing paths.
  No Windows 2.1.2 package is published by this macOS release.
- The public DMG was downloaded again and its checksum, existing Sparkle
  Ed25519 signature, strict application signature and ND2/SVS export smoke passed.
- All 2,058 file/symlink entries in the downloaded application match the installed
  `/Applications/nd2wsi-viewer.app`; version 2.1.2 is installed.
- The app is ad-hoc signed, not Developer ID notarized.

Artifact: `nd2wsi-viewer-2.1.2-macos-arm64.dmg` (69,269,884 bytes).

SHA-256: `9d4612609c172636ec0705b47bf82f70444e31cdcee12367eb41e04285405849`.
