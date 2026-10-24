# nd2wsi-viewer 2.5.3

This release shows when your work was auto-saved and consolidates shared viewer
code without changing stored formats or pixel values.

- **See that your work is saved.** A small green check at the bottom right of the pane, next to the scale bar, appears once your marks (including Astromapper regions) or LUT edits have been written. Hover over it, or focus it with the keyboard, to see how long ago that was; the time updates every second. The check turns grey while a change is waiting to be written, orange when a save failed and will be retried every minute, and red for a conflict with another window, which still needs the recovery notice in Annotations. Closing or reloading a browser-mode tab now flushes pending saves first. Auto-save itself is unchanged: marks are written moments after each edit and LUTs after each adjustment.
- **Annotation saving.** A dedicated module owns save ordering, plate-site transitions and conflict recovery. Existing annotations and recovery drafts retain their formats and ownership rules; a conflict is never overwritten or merged automatically.
- **Consistent viewer coordinates.** The main pane and the translucent viewer share tile geometry, display-flip calculations and ROI centring at the current zoom. Each window retains its own display state.
- **Preserved image reduction.** Pyramid generation and reduced-resolution source exports share the CPU 2×2 averaging calculation. Fixed reference values cover integer rounding, odd edges, floating-point output and repeated 2× reductions. Corrected-image pyramids keep their existing separate calculation; native exports stay on the CPU path.
- **Compatibility and maintenance.** Shading-correction components have clearer internal names; stored model and provenance identifiers are unchanged. The legacy BF-free model reader remains available, now covered by a frozen compatibility fixture. Unused internal symbols are removed and historical beta build recipes are grouped under `packaging/legacy/`.

This update adds regression coverage for existing behaviour. It does not introduce
a new analysis method or claim a measured performance gain.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
