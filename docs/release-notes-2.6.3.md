# nd2wsi-viewer 2.6.3

This update fixes a critical `#`/`?` path data-loss bug affecting `0.8.0–2.6.2`, restores stitched mosaic viewing for hierarchical Leica `.lif` tile scans, refines Zeiss `.czi` and 16-bit RGB `.svs` decoding and windowing, writes single-file `.nd2svs` containers from `nd2wsi convert`, and hardens annotation saves and cache management.

- **Path `#` and `?` cache and convert data-loss fix (`0.8.0–2.6.2`).** Slides or output stores whose path contains `#` or `?` no longer truncate the Zarr 3 store URL at `#` or `?` and delete parent-folder contents during cache creation or `nd2wsi convert`.
- **One-time cache rebuilds on first open.** Caches rebuild once for renamed CZI channels, scene-scoped CZI `--t`, nested LIFs, 2.6.1 RGB CZI plates missing `channel_dtypes`, 11+ channel ND2 fallback metadata, and changed semantics. Rebuilds need free disk space and retain superseded caches with a `.corrupt-` suffix (`nd2wsi tidy`).
- **Saved LUT settings reset for affected scans.** Saved display window and LUT settings reset once for renamed CZI channels, nested LIF parent series, and affected 11+ channel fallback-metadata ND2 scans.
- **Hierarchical Leica `.lif` tile scans.** Nested LIF tile scans open stitched instead of only tile 0. Prior `2.6.0–2.6.2` sidecars with mismatched dimensions vs level 0 are refused, coordinate-matched marks may be offset, and LIFs opening as a slide leave old plane/site sidecars unreachable.
- **CZI scene-scoped `--t` indexing.** `--t` now counts timepoints within the selected scene (`--position`) for Zeiss `.czi` files instead of using global indices across scenes.
- **Single-file `.nd2svs` output from `nd2wsi convert`.** `nd2wsi convert` writes a single-file SQLite `.nd2svs` container instead of a directory store, with cross-drive and symlinked output support. Re-run `nd2wsi convert --overwrite` to convert a 2.6.2 directory export safely; user-named exports are protected from `Remove cache` and in-place shading rewrites.
- **16-bit RGB SVS precision and default window.** 16-bit RGB SVS slides preserve `uint16` precision across direct viewing, histograms, and TIFF ROI export (correcting the `2.6.2` release notes). Sub-16-bit tags (`BitsPerSample < 16`, such as 12-bit) open at `[0, 2^BitsPerSample - 1]`, full 16-bit data opens at `[0, 65535]`, and **Auto** fits the active range.
- **Annotation save and Zstd1 compatibility.** Saves preserve non-finite third-party extension keys, fall back if `.window-lock` cannot lock on shared mounts, serialize legacy claims, and isolate corrupt legacy site sidecars. Zstd1 CZIs with content sizes decode on older `imagecodecs` (`>= 2026.6.6` without).

## Cache cleanup & 2.6.2 `.nd2svs` directory conversion

Cache rebuilds build and verify replacement containers in staging before swapping them into place (so sufficient free disk space is required), retaining superseded stores alongside as `*.corrupt-*` rather than deleting them automatically:

```bash
nd2wsi tidy <folder>
nd2wsi tidy <folder> --remove-corrupt-caches
```

Users who followed the 2.6.2 release-notes example (`nd2wsi convert sample.lif series-1.nd2svs --position 1`) received a Zarr directory store and can safely upgrade it to a single-file container (the new file is validated before removing the old directory):

```bash
nd2wsi convert --overwrite sample.lif series-1.nd2svs --position 1
```

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
