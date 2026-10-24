# nd2wsi-viewer 2.5.4

This release makes Windows wait for your saved work before a window closes,
speeds up tile display, panning over many annotations and TIFF export, and
keeps every displayed and exported pixel value unchanged.

- **Windows waits for saved work before closing.** Closing a window with the title-bar button, Alt+F4 or the taskbar now waits, as on macOS, until annotation and LUT changes, saves that failed on other plate sites, note text being typed, recovery drafts and running exports are handled. If the viewer page stops responding, a second close attempt offers to close without confirmed saves; only an explicit OK closes it. The same safeguard applies on macOS. When the viewer answers but work is still in progress, such as an unresolved conflict, a slide that is still opening or a running export, the window stays open with a notice, as before.
- **Faster tile display.** Compositing fluorescence channels into the displayed image is 4–5 times faster for typical 16-bit tiles and 2–4 times faster for shading-corrected tiles. Displayed colours are bit-for-bit identical to 2.5.3.
- **Smooth panning with many annotations.** Panning no longer rebuilds every annotation on each frame. With 2,000 Astromapper regions, panning went from about 20 to 60 frames per second in testing. Zooming behaves as before.
- **Faster TIFF export from reduced levels.** Native TIFF export reads each channel once per tile. Exports at a coarser scale are up to about 25% faster, and up to about 3 times faster on shading-corrected slides. Exported files are byte-identical.
- **Reliability.** Diagnostic runs and closed windows no longer leave a background process running, and release builds retry transient download failures of pinned decoder sources.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
