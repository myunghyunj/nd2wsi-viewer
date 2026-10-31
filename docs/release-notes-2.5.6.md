# nd2wsi-viewer 2.5.6

Updates now appear in a translucent card at the bottom right of the viewer, in the
same style as its other panels, instead of a separate white window. Displayed and
exported pixel values, and stored annotation, correction and LUT formats, are unchanged.

- **Theme.** Dark `#191919` and light `#FFFFFF` backgrounds share consistent translucent surfaces. The toolbar and status bar show essential image details. Hover over the checkmark for auto-save, cache and calibration details; orange indicates a problem.
- **Update card on macOS.** Updates appear in one card with bullet-point release notes and download progress. When ready, **Install and relaunch** is on the left and **Cancel** is on the right. The card also shows save/export waits and errors. The standard Sparkle window remains the fallback.
- **Update check on Windows.** The start page's update check uses the same card and opens the download for this PC's build in your browser.
- **Windows support.** The Windows build lists Windows 11 (x64, and ARM64 through x64 emulation) as its supported target; Windows 10 is not tested.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
