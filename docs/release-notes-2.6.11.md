# nd2wsi-viewer 2.6.11

This patch release expands AstroMapper ROI import and export parity for multi-step downscaled reference images, fixes multi-channel OME-TIFF and degraded-overview exports, and hardens cache security, window lifecycle, and cross-OS path handling.

- **AstroMapper multi-step resize import & ROI parity.** Accept 1-step and 2-step whole-slide downscaled JPG/PNG reference images with a dual-guard aspect tolerance (up to 2 px minor-axis drift within 0.5% relative difference) and clear dimension-bearing mismatch errors. Add UTF-16 BOM ROI text decoding, PNG reference parity in the translucent viewer, 4,096-character ROI notes, integer-snapped dragged ROI boxes, a 2,000-ROI guard, checkbox undo/help shortcuts, boundary-clamped AstroMapper exports, and aligned 32 MiB ROI TXT export/import limits for large multi-byte note lists.
- **Multi-channel OME-TIFF & degraded-overview export fixes.** Write standards-compliant multi-IFD OME-TIFF 2016-06 files for multi-channel non-RGB exports, guard ND2 exports clearly when only a degraded overview cache is present, and report pre-stream export failures visibly in the interface.
- **Cache security, lifecycle & cross-OS hardening.** Validate full Zarr v2 metadata and reject symlinks and directory junctions across read/write and directory stores, harden overwrite-conversion metadata validation and preserve `.replaced-*` backups whenever the final cache is missing, improve lockfile descriptor handling, make shading-correction generation recovery atomic, fix hidden and mid-reveal translucent window teardown and gesture-capture edge cases, improve live-LUT display-loop and tile-gate reliability, and unify NFC/NFD lockfiles with extended Windows long-path (`\\?\`) support.

### Downloads

| Platform | Artifact | Requirements |
| --- | --- | --- |
| macOS (Apple silicon) | [`nd2wsi-viewer-2.6.11-macos-arm64.dmg`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.11/nd2wsi-viewer-2.6.11-macos-arm64.dmg) ([`.sha256`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.11/nd2wsi-viewer-2.6.11-macos-arm64.dmg.sha256)) | Apple silicon, macOS 14 or later |
| Windows x64 (Setup) | [`nd2wsi-viewer-2.6.11-windows-x64-setup.exe`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.11/nd2wsi-viewer-2.6.11-windows-x64-setup.exe) ([`.sha256`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.11/nd2wsi-viewer-2.6.11-windows-x64-setup.exe.sha256)) | Windows 11 x64 / ARM64 (emulated) or 64-bit Windows 10 (`10240+`) · WebView2 |
| Windows x64 (Portable) | [`nd2wsi-viewer-2.6.11-windows-x64.zip`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.11/nd2wsi-viewer-2.6.11-windows-x64.zip) ([`.sha256`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.11/nd2wsi-viewer-2.6.11-windows-x64.zip.sha256)) | Extract full archive keeping `_internal` beside `nd2wsi-viewer.exe` · WebView2 |
| LGPL SDPC sources | [`sdpc-codec-sources-macos-arm64.zip`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.11/sdpc-codec-sources-macos-arm64.zip) · [`sdpc-codec-sources-windows-x64.zip`](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.11/sdpc-codec-sources-windows-x64.zip) | Corresponding LGPL decoder sources with `.sha256` checksums |

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 per-user NSIS Setup installer (`-setup.exe`, no administrator UAC prompt) and portable ZIP; also supports Windows 11 ARM64 through x64 emulation. For the portable ZIP, extract the whole archive and keep `_internal` beside the executable. WebView2 is required; the installer and executable are not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
