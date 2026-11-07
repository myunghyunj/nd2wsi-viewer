# nd2wsi-viewer 2.5.9

This update keeps active update downloads and notices visible when opening a slide from the start
window, closes a slide's translucent tracing window when its tab closes, and keeps open-file errors
and progress tabs consistent across multiple windows. Displayed and exported pixel values, and
stored annotation, correction and LUT formats, are unchanged.

- **Update card across slide opens.** Opening the first slide from the start window keeps an active update offer or download visible in the bottom-right update card instead of cancelling it, and hides the start window's update button while the card is shown.
- **Translucent tracing windows.** Closing a slide tab closes its translucent tracing window automatically, and reopening a slide after a cancelled update allows the translucent viewer to open again.
- **Slide opening feedback.** Unsupported or missing files keep their error message visible on the start window and after update recovery, and the opening progress tab remains visible until the new slide tab appears.
- **Multiple windows.** File dialogs and title-bar zoom actions target the active viewer window instead of always using the first window.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
