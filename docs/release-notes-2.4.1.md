# nd2wsi-viewer 2.4.1

- **Keyboard shortcuts fixed.** Use `C` for Channels, `E` for Export and `A` for Annotations. `Cmd+F` / `Ctrl+F` flips the image; `Cmd+T` / `Ctrl+T` turns it 90° clockwise. Annotation boxes, pins and measurement lines follow the image. Shortcuts respect text entry and ignore held-key repeats.
- **TIFF export fixed.** Whole-slide TIFF export now honors the selected resolution, including **1/2 size**. TIFF exports preserve raw channel data at the selected resolution, without applying display LUTs or shading correction.
- **Periodic shading correction.** Fits a repeating illumination function with a spatially varying amplitude for each supported **FITC and CY3** channel, without requiring a BF reference. BF and DAPI remain unchanged; channels without sufficient evidence also remain unchanged.
- **Astromapper import and export.** Draw ROIs with Shift-drag, import AstroMapper TXT coordinates using the matching small JPG, and click numbered ROIs to locate them. Export a ZIP containing a small JPG and matching pixel-coordinate TXT, with an optional annotated PDF. JPG images can also be opened and annotated directly.

## Astromapper

Open Astromapper from the always-visible star or `Cmd+Shift+A` / `Ctrl+Shift+A`.
Pan and zoom remain available while drawing. Clicking an ROI number locates
that region and highlights it in yellow while the other ROIs turn grey.
**Clear all** removes Astromapper ROIs while preserving ordinary annotations.

TXT coordinates refer to the matching whole-image JPG. Import reads that JPG's
dimensions and scales each axis into the current image's pixel coordinates,
including AstroMapper's inclusive end pixels. Use the matching uncropped JPG;
an unrelated crop or a differently oriented image requires more than scaling.
Export converts stored ROI coordinates directly and assembles only the final
small image, avoiding a full-resolution intermediate canvas.

**Export JPG + TXT** creates the ordinary ZIP. **Export ZIP with PDF** adds a
landscape report with numbered ROI views and labels placed clear of the lines.

## Shading correction and saved data

The confirmation shows the saved-pyramid notice and estimated time. Continue
starts correction; opening a slide alone does not. The current method supports
stitched, single-frame ND2 slides with at least one FITC or CY3 channel.
Plate/time-series acquisitions and Z projections remain unsupported.

An accepted correction replaces the `.nd2svs` viewing pyramid and remains
available after reopening. **Revoke** rebuilds the original pyramid from the
source ND2. Existing saved corrections remain readable without being refitted.
Cancellation or failure before publication preserves the current cache.

PNG, JPEG and SVG exports use the corrected display. Native ND2/TIFF exports
and pixel readouts retain the original-data path. Source slides, annotations
and saved LUTs remain unchanged by correction. See the
[method and validation limits](shading-correction-validation.md) for the model
and evidence supporting these behaviors.

## Platform notes

The macOS download requires Apple silicon and macOS 14 or later, matching the
bundled NumPy/SciPy runtime. It is ad-hoc signed and not Developer ID notarized.
Signed Sparkle updates retain their existing signing identity.
Windows uses the x64 application, including x64 emulation on Windows 11 ARM64;
it requires WebView2 and is not Authenticode-signed. Windows updates remain
manual.

SDPC support and its pinned LGPL decoder retain the
[existing format limits](sdpc-support.md). Corresponding codec sources
accompany both platform downloads. Artifact checksums and completed platform
verification are recorded with the release assets.
