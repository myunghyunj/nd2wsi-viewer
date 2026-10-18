# nd2wsi-viewer 2.1.15

**Shading Correction** estimates repeating illumination shading in supported stitched ND2 scans and replaces the existing `.nd2svs` viewing pyramid with the corrected pyramid. The result is saved in that cache and remains applied after closing and reopening the slide. The shaded-circle button replaces the tab-strip update button and turns red on hover. Clicking it shows a preparation-time estimate and green **Continue** and red **Cancel** buttons. Continue starts processing; the progress bar shows the current stage and an updated approximate remaining time.

The button then offers **Revoke**, which rebuilds the viewing pyramid from the original ND2 and replaces the corrected cache. Returning to the original display therefore requires a rebuild. Applying and revoking both require the original ND2. Cancellation is available during preparation; cancellation or failure before the final replacement leaves the current cache untouched. If a completed replacement is saved but the viewer cannot activate it immediately, the valid saved cache is retained and the viewer asks you to reopen the slide.

Correction currently targets FITC and CY3, using the same scan's BF channel and recorded camera geometry. It requires a supported single-frame ND2 acquisition and native-resolution pixels. Unsupported acquisitions explain why correction is unavailable. DAPI, BF, and channels without sufficient repeat evidence retain their original values. Inspect the estimated shading pattern, especially near tissue boundaries; its evidence checks do not guarantee improvement in every field.

PNG, JPEG and SVG exports use the saved correction and include `shading-corrected` in their filenames. Native ND2/TIFF exports, including reduced-resolution exports, and the pixel-value readout retain original values. The original ND2, annotations and saved LUTs remain unchanged. Temporary preparation files are released after the operation; the corrected viewing cache remains saved until you revoke it.

Corrected `.nd2svs` caches use a newer format and require version 2.1.15 or later. The format guard in older viewers reports that an update is required and leaves the cache untouched.

On macOS, Finder file-opening handlers are now bound before the native window is created, so documents arriving during startup reach the viewer's opening queue. The native **Check for Updates** menu remains available.

The macOS download targets Apple silicon and uses an ad-hoc code signature plus signed Sparkle updates. It is not Developer ID notarized. The Windows download is an x64 portable build; Windows 11 ARM64 runs it through x64 emulation. The Windows executable is not Authenticode-signed, and Windows updates remain manual.
