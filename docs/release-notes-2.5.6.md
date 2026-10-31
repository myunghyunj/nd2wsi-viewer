# nd2wsi-viewer 2.5.6

Updates now appear in a translucent card at the bottom right of the viewer, in the
same style as its other panels, instead of a separate white window. Displayed and
exported pixel values, and stored annotation, correction and LUT formats, are unchanged.

- **Update card on macOS.** A found update, download progress, the wait for exports or annotation saves, an update ready to install, errors and "up to date" are shown in one card at the bottom right. A ready update offers **Install and relaunch**, or **Install on quit** to install when you close the viewer. Release notes in the card link to the GitHub release. Where the card cannot be shown, for example on the start page, the standard update window appears as before, and a download that is already running continues there.
- **Update check on Windows.** The start page's update check uses the same card and opens the download for this PC's build in your browser.
- **Windows support.** The Windows build lists Windows 11 (x64, and ARM64 through x64 emulation) as its supported target; Windows 10 is not tested.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
