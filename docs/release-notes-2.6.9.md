# nd2wsi-viewer 2.6.9

This patch release restores per-user Windows Setup installer support on pre-1709 64-bit Windows 10 installations (including Enterprise 2015/2016 LTSC microscope workstations) when `.NET Framework 4.6.2+` and WebView2 are available, and hardens pre-1607 Windows 10 DPI scaling, WebView2 floor checks, and verification scripts.

- **Pre-1709 Windows 10 Setup & LTSC workstation support.** Remove the artificial Windows 10 build `16299` (version 1709) gate in NSIS Setup and `verify_windows_installer.ps1`, permitting 64-bit desktop Windows 10 (`10.0.10240+`) and Windows Server 2016+ (`14393+`) installations gated on actual runtime prerequisites (`.NET Framework 4.6.2+`, 64-bit architecture, desktop shell, and WebView2 `>= 109` or bundled Fixed Version), while recording the exact OS version in Setup details and rejection prompts.
- **Pre-1607 Windows 10 DPI fallback & runtime parity.** Add a cached system-DPI (`LOGPIXELSX`) fallback when `GetDpiForWindow` is unavailable on early Windows 10 builds (`10240`/`10586`) so translucent overlay dragging, resizing, and corner rounding stay accurate at >100% display scaling, enforce `>= 109` WebView2 major version detection during Setup, verify bundled VS 2022 MSVC CRT DLLs at build time, and lower PowerShell verifier parse requirements to `5.0`.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 per-user NSIS Setup installer (`-setup.exe`, no administrator UAC prompt) and portable ZIP; also supports Windows 11 ARM64 through x64 emulation. For the portable ZIP, extract the whole archive and keep `_internal` beside the executable. WebView2 is required; the installer and executable are not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
