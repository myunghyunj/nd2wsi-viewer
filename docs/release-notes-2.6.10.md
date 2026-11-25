# nd2wsi-viewer 2.6.10

This patch release adds real-time live LUT synchronization to the translucent tracing viewer and improves cross-OS viewing cache and annotation sidecar portability between macOS and Windows on external exFAT drives.

- **Live LUT updates in the translucent viewer.** Adjusting channel visibility, contrast ranges, gamma, presets, or channel colours in the main viewer now updates the open translucent overlay in real time during continuous drags and slider edits while keeping tracing pan/zoom and locked ROI alignment intact. Slow-tile redraws are gated on paint completion with retry handling so the floating image never flashes blank, shading-correction cache rebuilds display a clear expiry notice instead of showing stale pixels, and overlay teardown and session lifecycle handling are hardened.
- **Cross-OS external-SSD cache portability (macOS ↔ Windows on exFAT).** Reuse existing `.nd2svs` viewing caches across macOS and Windows on external exFAT drives by tolerating expected exFAT timestamp skew (whole-hour UTC/DST offsets and 2-second/sub-second rounding) without triggering unnecessary cache quarantine or rebuilds while keeping stale-source protection intact. Normalize Unicode NFC/NFD paths consistently for Korean and other non-ASCII filenames across caches, plates, and annotation sidecars.

### Downloads

| Platform | Artifact | Requirements |
| --- | --- | --- |
| macOS (Apple silicon) | [`nd2wsi-viewer-2.6.10-macos-arm64.dmg`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.10/nd2wsi-viewer-2.6.10-macos-arm64.dmg) ([`.sha256`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.10/nd2wsi-viewer-2.6.10-macos-arm64.dmg.sha256)) | Apple silicon, macOS 14 or later |
| Windows x64 (Setup) | [`nd2wsi-viewer-2.6.10-windows-x64-setup.exe`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.10/nd2wsi-viewer-2.6.10-windows-x64-setup.exe) ([`.sha256`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.10/nd2wsi-viewer-2.6.10-windows-x64-setup.exe.sha256)) | Windows 11 x64 / ARM64 (emulated) or 64-bit Windows 10 (`10240+`) · WebView2 |
| Windows x64 (Portable) | [`nd2wsi-viewer-2.6.10-windows-x64.zip`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.10/nd2wsi-viewer-2.6.10-windows-x64.zip) ([`.sha256`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.10/nd2wsi-viewer-2.6.10-windows-x64.zip.sha256)) | Extract full archive keeping `_internal` beside `nd2wsi-viewer.exe` · WebView2 |
| LGPL SDPC sources | [`sdpc-codec-sources-macos-arm64.zip`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.10/sdpc-codec-sources-macos-arm64.zip) · [`sdpc-codec-sources-windows-x64.zip`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.10/sdpc-codec-sources-windows-x64.zip) | Corresponding LGPL decoder sources with `.sha256` checksums |

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 per-user NSIS Setup installer (`-setup.exe`, no administrator UAC prompt) and portable ZIP; also supports Windows 11 ARM64 through x64 emulation. For the portable ZIP, extract the whole archive and keep `_internal` beside the executable. WebView2 is required; the installer and executable are not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
