# nd2wsi-viewer 2.1.4 — physical scale linking

The comparison link control now offers three choices:

- **Off**: zoom and move each image independently.
- **Link scale**: match physical magnification while moving each image independently.
- **Link scale + frame**: match physical magnification and link movement through the existing alignment.

Each image's pixel calibration determines its displayed physical scale. This
keeps comparisons consistent across different pixel sizes, pane widths,
pyramid levels and Retina displays. The same zoom percentage is not required.
Here, frame means viewport position; acquisition T/P/Z controls are not coupled.

Physical linking requires valid calibration in every view. Unsupported pixel
anisotropy/orientation and non-overlapping zoom ranges show an explanation.
Scale-only movement preserves the saved alignment. Mode changes discard old
pending commands and resynchronize after the linking menu changes the layout.

The LUT panel is simpler: Min/Max number-entry boxes are removed. Histogram
handles, displayed limits, gamma, Auto/Reset, full-range and optional Auto-fit
axes, graph zoom/pan and live contrast remain available. Calibrated SVG/JPEG
scale-bar exports from 2.1.3 are also retained.

This release provides the macOS Apple-silicon DMG (macOS 12 or later).
The app is ad-hoc signed, not Developer ID notarized. Its Sparkle update is
signed with the existing update identity. Windows continues to build and run
in CI; the published Windows download remains
[v2.0.0](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.0.0).

The release follows local interactive approval. Main CI and tag real-data
checks run after publication; their current results are reported on the
GitHub release page. Local checks include physical comparison behavior,
LUT interactions, package source/signature verification, ND2 and SVS scientific
export smoke tests, and the installed native WKWebView renderer.
