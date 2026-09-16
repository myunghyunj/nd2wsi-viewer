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

This release provides the macOS Apple-silicon DMG (macOS 12 or later) and
Windows x64 portable ZIP. Windows 11 ARM64 runs the same x64 executable through
emulation. Windows 10 Education and local Parallels were not directly tested.
The macOS app is ad-hoc signed, not Developer ID notarized; its Sparkle update
uses the existing signing identity. Windows updates are manual and the EXE is
not Authenticode-signed. See [Windows distribution](windows.md) for downloads,
checksums, prerequisites and exact verification evidence.

## Completed validation

The original macOS release source is
`596f6bffafe48188d08ba7752366c6bd2829af2d`. Follow-up commit
`dab37448f583b7d91cd987fae6ca3354e1ad64f0` only fixes UTF-8 decoding in a Windows
test and is the source of the Windows archive. Application/build files match
the original tag. Subsequent refactoring on main is not part of these binaries.

- [Main CI passed](https://github.com/myunghyunj/nd2wsi-viewer/actions/runs/35117273867).
- [Real ND2 checks passed](https://github.com/myunghyunj/nd2wsi-viewer/actions/runs/35117314024).
- [Windows build and verification passed](https://github.com/myunghyunj/nd2wsi-viewer/actions/runs/35117273821),
  including 1,237 unit/integration tests, 5 real-data tests, packaged scientific
  smoke, and native WebView2 on x64 and Windows 11 ARM64 via x64 emulation.
- Local macOS checks included 1,342 passing tests, physical comparison behavior,
  LUT interactions, package source/signature verification, ND2/SVS scientific
  export smoke, the installed WKWebView renderer and signed Sparkle feed.

The Windows archive's original READ-ME describes local/unpublished delivery;
these release notes supersede those statements. The tested ZIP is published
unchanged, with its matching checksum. No v2.1.4 Setup installer is published.
