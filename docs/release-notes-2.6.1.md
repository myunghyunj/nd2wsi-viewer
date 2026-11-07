# nd2wsi-viewer 2.6.1

This update enables interactive Z-stack, LambdaScan, time-series, and multi-series navigation for
Zeiss CZI (`.czi`) and Leica LIF (`.lif`) confocal and lightsheet acquisitions. Displayed and
exported pixel values for existing ND2, SVS, SDPC, CZI, LIF, and raster formats, as well as stored
`.nd2svs` cache, annotation, correction, and LUT formats, are unchanged.

- **Interactive Z-stack & time-series navigation for CZI and LIF.** Opening a multi-plane (`Z > 1` or `T > 1`) Zeiss `.czi` or Leica `.lif` confocal, LIGHTNING, LambdaScan, or lightsheet file now activates the right-hand Z-axis slider (`#z-knob`) with physical micrometer step readout, sharpness-scored autofocus (`F`), and timeline transport (`#t-track`) just like Nikon `.nd2` stacks.
- **Multi-series Leica LIF browsing.** Multi-series `.lif` containers with uniform 2D camera fields open in the site selector so each series can be inspected, adjusted, and exported independently.
- **Per-plane LUTs, annotations & ROI export.** Channel LUT windows/gammas, per-plane histograms, sidecar annotations, and native-value TIFF/ND2 and rendered PNG/JPEG/SVG/Astromap ROI exports operate directly on the active `(T, P, Z)` confocal plane.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
