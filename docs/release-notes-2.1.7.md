# nd2wsi-viewer 2.1.7 — RGB brightfield channel controls

- RGB brightfield ND2 files now offer Red, Green and Blue histograms, display
  ranges, gamma controls and visibility switches in **Channels & LUTs** (`C`).
  The automatic light theme is retained; RGB is not reclassified as fluorescence.
- Each RGB component uses its own window and gamma. Previously the RGB renderer
  applied the first component's settings to all three.
- Default 0–255 windows and gamma 1 preserve the original 8-bit RGB appearance.
  Reset restores the channel's default display settings. Hiding a component
  leaves the other two in their original RGB positions.
- These controls also apply to RGB SVS images through the shared display path.
  Rendered tiles and PNG/JPEG/SVG exports use the same display settings.
- Original pixels, raw ND2/TIFF export logic, calibration, viewing-cache format
  and annotation storage are unchanged. Existing fluorescence controls remain
  available. Metal stays opt-in and retains its existing eligibility limits.

This release provides a macOS Apple-silicon DMG (macOS 12 or later) and a Windows
x64 portable ZIP. Windows 11 ARM64 runs the Windows executable through x64
emulation, not natively. The Mac app is ad-hoc signed, not Developer ID notarized;
Sparkle uses the existing update signing identity. The Windows executable is not
Authenticode-signed; no Setup installer is included.

The [GitHub release](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.7)
records the exact source commit, artifact checksums and completed validation.
