# nd2wsi-viewer 2.6.0

This update adds support for Zeiss CZI (`.czi`) and Leica LIF (`.lif`) microscopy files across 2D
plane scans, stitched mosaic acquisitions, spectral LambdaScan series, and multi-channel / Z-stack
confocal & LIGHTNING deconvolution images. Displayed and exported pixel values for existing ND2,
SVS, SDPC, and raster formats, as well as stored `.nd2svs` cache, annotation, correction, and LUT
formats, are unchanged.

- **Zeiss CZI plane scan & confocal support (`.czi`).** Open 8-bit/16-bit/float32 fluorescence and 24-bit/48-bit RGB `.czi` acquisitions (uncompressed, Zstd0, Zstd1 HiLo, JPEG-XR, JPEG, and LZW) with automatic `.nd2svs` OME-Zarr pyramid caching, multi-tile mosaic stitching, and Z-plane selection (`mid`, `max` projection, or integer Z index).
- **Leica LIF fluorescence & confocal support (`.lif`).** Open LAS AF / LAS X `.lif` multi-series containers (8-bit/16-bit/float32 fluorescence, LIGHTNING deconvolved 16-bit Z-stacks, spectral LambdaScan series, and RGB snapshots) with `.nd2svs` pyramid caching, physical micrometer calibration, `ATLConfocalSettingDefinition` objective magnification, and fluorophore/LUT channel colors.
- **Metadata, LUTs, annotations & ROI export.** Full multi-channel LUT window/gamma adjustment and persistence, sidecar ROI annotations, and native-value TIFF/ND2 and rendered PNG/JPEG/SVG/Astromap ROI exports work seamlessly on `.czi` and `.lif` slides.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
