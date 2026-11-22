# nd2wsi-viewer 2.6.8

This patch release carries manually drawn Astromap regions directly into the translucent overlay window alongside imported AstroMapper regions.

- **Manually drawn Astromap ROIs in the translucent overlay.** Carry over all enabled Astromap regions — both manually drawn boxes and imported AstroMapper TXT regions — when opening the translucent tracing window, preserving their Astromap numbering, custom notes, and active selection while still excluding disabled Astromap regions and non-Astromap box annotations.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 per-user NSIS Setup installer (`-setup.exe`, no administrator UAC prompt) and portable ZIP; also supports Windows 11 ARM64 through x64 emulation. For the portable ZIP, extract the whole archive and keep `_internal` beside the executable. WebView2 is required; the installer and executable are not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
