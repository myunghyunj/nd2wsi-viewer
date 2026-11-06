# nd2wsi-viewer 2.5.8

This update improves Windows external-drive launches and file path display, keeps concurrent slide
opens and multi-file drops reliable, and cleans up session lock files when windows close. Displayed
and exported pixel values, and stored annotation, correction and LUT formats, are unchanged.

- **Windows external drives and paths.** Launching from an exFAT or FAT32 drive no longer logs false download-mark warnings, and tab titles and sidecar status messages show only the file name for Windows paths.
- **Opening multiple slides.** Dropping additional files while the first slide is opening keeps every file in the queue, and concurrent conversions each keep their own progress tab.
- **Session locks and sidecars.** Closed or terminated windows clean up their `.window-live` lock files, and legacy annotation sidecar migration skips symbolic links.
- **Update card and recovery.** Release notes remain accessible while an update downloads, manual update retries lock their buttons until the check finishes, and slides can be reopened by drag-and-drop if an update is cancelled after teardown.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
