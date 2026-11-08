# nd2wsi-viewer 2.6.2

This update resolves pixel decoding, physical calibration, channel identity, cache reuse, and annotation isolation issues for Zeiss CZI (`.czi`) and Leica LIF (`.lif`) confocal and multi-series acquisitions introduced in 2.6.0–2.6.1.

- **Mixed-depth and float/BGRA pixel decoding (`.czi` and `.lif`).** Leica `.lif` acquisitions with mixed 8-bit/16-bit interleaved or multi-pass channels now decode each channel at its declared bit depth with lossless `dtype` promotion (`int32`/`float64` when needed). Zeiss `.czi` `PixelType 8` (`Bgr96Float`) and `PixelType 9` (`Bgra32`) follow the libCZI pixel-type specification, and mixed-depth `.czi` SubBlocks decode using each SubBlock's own `DV` header and directory entry `PixelType`.
- **Full-range integer RGB display windows and channel identity mapping.** `uint16`/`uint32` RGB `.czi` and `.lif` images initialize their default display window from the actual pixel data range instead of clamping to `[0, 255]`, and upgrade persisted `[0, 255]` LUT windows when positive pixels exceed `255`. Sparse or subset `.czi` channels map by channel ID (`Channel:N`) before falling back to distinct positional metadata entries.
- **Leica LIF physical calibration and homogeneous multi-series checks.** LIF `<DimensionDescription>` span (`Length`) is divided by `NumberOfElements - 1` (`N - 1` fencepost intervals) per the Leica LIF specification. Multi-series `.lif` site browsing now verifies matching calibration, magnification, channel identities, geometry, and mosaic tile coordinates across series.
- **Plane-isolated T/P/Z annotations and verified cache reuse.** Confocal `.czi` and `.lif` annotations are isolated per `(T, P, Z)` plane while migrating legacy 2.6.1 shared site sidecars non-destructively. Managed `.nd2svs` discovery, `nd2wsi view`, and direct `.nd2svs` opens validate source file provenance (`source_fingerprint`), selected `(P, T, Z)` plane, channel labels/dtypes, and physical calibration, automatically rebuilding stale 2.6.0/2.6.1 caches when the source file is present.
- **Honest frame-based timing when acquisition timestamps are absent.** Confocal `.czi` and `.lif` time-series without decoded physical timestamps report timing as unavailable rather than synthesizing `1 ms` per frame.

## Opening another series in a heterogeneous LIF

For example, convert the second series to its own cache, then open that cache:

```bash
nd2wsi convert sample.lif series-1.nd2svs --position 1
nd2wsi view series-1.nd2svs
```

This preserves the selected series' own dimensions, calibration, channels, and pixel values. It does not modify the LIF source or combine annotations from different series.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
