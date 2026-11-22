# nd2wsi-viewer 2.6.7

This release introduces fluid, fine-grained zooming with an exact numeric zoom popover in the translucent overlay window, modularizes the core reader, server, and frontend architecture, hardens Windows compatibility and security across both the portable ZIP and Setup installer, and fixes a cross-frame channel-swatch serialization hang.

- **Fluid translucent overlay zooming and manual zoom input.** Replace stepped 20% scroll jumps in the translucent overlay with smooth, cursor-anchored exponential mouse-wheel zoom (~5% per notch normally, ~1% with **Shift** + scroll, and ~0.2% with **Alt / Option** + scroll, plus trackpad pinch support). Click the top-left magnification readout (`1.000×`) to open a compact popover for typing an exact magnification (`1.25`, `1.25x`, `125%`) or nudging live in `±0.001` and `±0.01` increments.
- **Windows compatibility and installer hardening.** Distinguish Windows name-surrogate symlinks/junctions from OneDrive Cloud Files and NTFS deduplication reparse points, keep mapped/UNC network recent files from blocking startup or silently disappearing offline, pass an explicit session WebView2 `UserDataFolder` alongside private mode, retry transient Defender file locks during annotation saves and installer staging, scope installer path checks away from OS-owned ancestors, and guard uninstall file-association cleanup so editions never overwrite each other.
- **Codebase modularization and stability fixes.** Split large reader, plate, conversion, rendering, server, and frontend files (`ometiff`, `plate`, `convert`, `render`, `czi`, `server`, `shell-v1.js`, `app.js`) into focused sibling modules while preserving full API compatibility; store channel colour swatch button references in a `WeakMap` so slide metadata remains strictly JSON-serializable across frames; guard shell focus restoration so background windows never steal OS focus; and retire stale Sparkle update probes across navigation.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 per-user NSIS Setup installer (`-setup.exe`, no administrator UAC prompt) and portable ZIP; also supports Windows 11 ARM64 through x64 emulation. For the portable ZIP, extract the whole archive and keep `_internal` beside the executable. WebView2 is required; the installer and executable are not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
