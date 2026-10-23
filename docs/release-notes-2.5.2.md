# nd2wsi-viewer 2.5.2

This release improves Astromapper review, large JPEG opening and manual alignment
of linked views.

- **Review ROIs with the arrow keys.** Click a numbered ROI in the open Astromapper panel, then press Left or Right to centre the previous or next region without changing magnification. Navigation follows the displayed list and stops at its ends. Note editing and modified arrow keys retain their normal behaviour.
- **Open large microscopy JPEGs at full resolution.** JPEGs can now contain up to 512 megapixels, subject to a memory check before decoding. This removes the previous 64-megapixel restriction and avoids Pillow's generic large-image rejection through a JPEG-specific, bounded opening path. PNGs retain the 64-megapixel limit. Stored pixel orientation and ROI coordinates remain unchanged.
- **Understand rejected ROI imports.** Errors now identify zero-area or reversed regions by index and report coordinates outside the reference image, including its dimensions. A valid set of 200 regions is supported; the existing limit is 2000. Invalid imports leave existing annotations intact and do not silently resize, skip or reverse manual regions.
- **Adjust one linked view with Shift-drag.** In Link scale + frame, hold Shift while dragging to move one pane while the others stay in place. Finish the drag and release Shift, then pan normally to continue linked movement with the adjusted offset. Option/Alt-drag remains available. In this mode, Shift-drag takes priority over Astromapper's rectangle drawing.
- **Updated alignment guide.** New overview and landmark close-ups show how to select four corresponding histological features in the same order on each image. The images illustrate manual orientation and similarity alignment across serial sections, without claiming cell-to-cell correspondence.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
