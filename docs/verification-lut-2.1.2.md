# macOS 2.1.2 LUT controls: local verification

The LUT panel starts at the complete integer data range (0–65,535 for uint16).
Auto-fit LUT range is an optional checkbox that crops the histogram axes without
changing image contrast. Scrolling zooms at the pointer; horizontal scrolling or
empty-space dragging pans; double-clicking restores the full axis. Numeric Min/Max
fields remain available. Contrast updates at a bounded 100 ms cadence during a
handle drag, and release flushes the latest pending settings.

Zoom uses finer counts from the same sampled pyramid level; it is not a new
full-resolution census of the source image. Native horizontal input is scoped to
visible LUT plots while existing plate gestures and neighboring controls retain
their routing.

## Validation (2026-09-16, local Apple-silicon macOS)

- Project lint: passed.
- Full non-realdata suite: 1,288 passed, 22 skipped, 5 deselected.
- Lock file: checked against package version 2.1.2.
- Packaged and installed source equality: 38 Python modules and 18 static assets.
- Application signature: deep, strict verification passed.
- Packaged and installed smoke: ND2 read and pixel-exact export; JPEG and
  JPEG2000 SVS pyramids; pixel-exact TIFF region export.
- Native WKWebView pointer replay: contrast changed during an uninterrupted
  drag, before pointer release; the final refresh matched the final LUT values.
- Native graph checks: full/automatic ranges, zoom, pan, drag, double-click reset,
  and contrast preservation passed in isolated Agent windows.

This is a local macOS update. No public release or Windows update was published.
Programmatic input checks do not replace physical trackpad testing on other Macs.
Detailed local logs, preservation inventories, and a recoverable previous app
are retained under validation/full-lut, outside the Git commit.
