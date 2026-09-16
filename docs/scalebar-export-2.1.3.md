# 2.1.3: calibrated scale-bar region export

The Region panel adds two muted-gold buttons below Move and Clear Region:
**Scale bar + SVG** and **Scale bar + JPEG**.

- SVG contains the lossless rendered PNG, an editable white rectangle and an
  editable white text label at bottom right. No resampling of the source image.
- JPEG uses the same physical bar geometry, quality 98 and 4:4:4 colour sampling.
- Both follow the selected region, channels, LUT/gamma, frame and export scale.
- The bar uses horizontal pixel calibration multiplied by the exported level's
  downsample. Its length is a 1/2/5 multiple near one fifth of the image width.
- Missing/invalid calibration disables these buttons with an explanation.
  Exports smaller than 96 × 64 pixels require a larger scale or region.
- Raw ND2/TIFF and ordinary PNG/JPEG remain available without an added bar.

SVG preserves vector bar/text quality. The embedded microscopy image retains
the selected raster resolution; saving SVG does not create additional image detail.

## Local verification

- Automated suite: 1,229 passed, 115 skipped, 5 real-data tests deselected.
- Isolated browser Agent preview: both muted-gold buttons are visible below
  Move/Clear Region and start their respective downloads.
- HTTP exports of the approved 796 × 775 rendered example at native and half
  scale both retain a 100 µm bar, with widths 151.5210 and 75.7605 pixels.
- Each SVG embeds exactly the ordinary PNG export bytes. At native scale,
  decoded image pixels also match the approved example without modification.
- Packaged and installed application source equality: 39 Python modules and
  18 static assets. Deep, strict code-signature verification passed.
- Installed version 2.1.3 passed the native WKWebView image/bridge smoke and
  ND2/SVS read, calibrated ND2 round-trip and pixel-exact TIFF export checks.
- Public release requires the exact-source CI and real-data gates, verified
  release download and signed Sparkle feed. Windows binaries remain on v2.0.0.

## Reserved for 2.1.4

The compare link control will offer Off, Link scale, and Link scale + frame.
Link scale will match micrometres per displayed CSS pixel while keeping each
pane's centre independent. Link scale + frame will also synchronize navigation
using the existing alignment/frame mapping. Physical linking must account for
each image's calibration, pyramid level and display transform; missing
calibration must not silently fall back to a claimed physical match.

The 2.1.4 controls are not part of the 2.1.3 changes.

## Published macOS artifact

Version 2.1.3 is published at:
https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.3

- Source candidate: `601dc09c12857fc34f0033705dee25e79fe6cc60`.
- DMG SHA-256: `55bba26e142fbb55907acb6abf81a74c4a446c15a2afe0f53a28ba5498007ce4`.
- Public download checksum, packaged source equality and deep/strict signature
  verification passed. Its executable matches the locally installed 2.1.3 app.
- Sparkle enclosure signature verified with the existing signing identity;
  all nine older feed entries are preserved.
- Exact-source real-data checks passed. The remaining macOS matrix and Windows
  checks were still running at publication; their live results are linked from
  the release notes. Windows v2.0.0 remains the public Windows package.
