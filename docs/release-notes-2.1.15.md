# nd2wsi-viewer 2.1.15

**Shading Correction** estimates repeating illumination shading in supported stitched ND2 scans. The shaded-circle button replaces the tab-strip update button and turns red on hover. Clicking it shows a preparation-time estimate and green **Continue** and red **Cancel** buttons. Continue starts processing; the progress bar shows the current stage and an updated approximate remaining time. After preparation, the button switches the active pane between the original and corrected views while retaining its position and LUTs.

Correction currently targets FITC and CY3, using the same scan's BF channel and recorded camera geometry. It requires the original, single-frame ND2 acquisition and native-resolution pixels. Unsupported acquisitions explain why correction is unavailable. DAPI, BF, and channels without sufficient repeat evidence retain their original values. The estimated shading pattern may not improve every field, particularly near tissue boundaries; compare the corrected view with the original.

PNG, JPEG and SVG exports follow the displayed correction and include `shading-corrected` in their filenames. Native ND2/TIFF exports and pixel readouts retain original values. Source images, shared viewing caches, annotations and saved LUTs remain separate from the private correction workspace, which is released when the slide closes.

On macOS, Finder file-opening handlers are now bound before the native window is created, so documents arriving during startup reach the viewer's opening queue. The native **Check for Updates** menu remains available.

The macOS download targets Apple silicon and uses an ad-hoc code signature plus signed Sparkle updates. It is not Developer ID notarized. The Windows download is an x64 portable build; Windows 11 ARM64 runs it through x64 emulation. The Windows executable is not Authenticode-signed, and Windows updates remain manual.
