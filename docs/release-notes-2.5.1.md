# nd2wsi-viewer 2.5.1

This release adds a translucent reference window and improves annotation exchange,
ROI navigation and cache creation on external drives.

- **Translucent reference:** use the cloud button to keep the current image above
  other apps. Adjust opacity, pan and zoom, or lock the window. Resizing changes
  the field of view while preserving image scale.
- **ROI navigation:** a matching whole-image JPG and Astromapper TXT adds numbered
  ROI boxes and previous/next navigation to the reference window. Clicking an ROI
  number in the main Astromapper panel now centres it without changing the current
  zoom, including while a zoom animation is running.
- **More workspace:** collapse the top tools, and use **⌘N / Ctrl+N** to open a
  separate viewer window.
- **GeoJSON import:** bring points, lines, polygons with holes and multipart
  annotations into the open image. Imports append to existing marks and preserve
  image coordinates and feature metadata. Invalid or unsupported features reject
  the whole import. QuPath ellipses, geometry collections and non-default planes
  remain unsupported; see the [annotation guide](user-guide.md#measuring-and-marking).
- **External-drive cache fix:** new viewing caches on macOS exFAT volumes no longer
  fail because SQLite sees the newly created database file as having moved.
- **Updated documentation:** the README and user guide now show the current UI,
  explain LUT persistence and distinguish shading illustrations from the
  development validation results.

The original scans, saved annotations and LUT preferences are preserved. Shading
illustrations use the current method with identical display settings before and
after correction; they are not independent biological validation.

The macOS app requires Apple silicon and macOS 14 or later. It is ad-hoc signed
and uses signed Sparkle updates; it is not notarized. The Windows portable ZIP
contains an x64 app and supports Windows 11 ARM64 through x64 emulation.

On macOS only the image layer changes opacity. Windows applies whole-window
opacity, including to controls and ROI outlines. Keep the source tab open while
using its translucent reference, and use the corresponding uncropped JPG in the
same orientation when importing ROI coordinates.
