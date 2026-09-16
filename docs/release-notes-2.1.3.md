# nd2wsi-viewer 2.1.3 — calibrated scale-bar exports

The Region panel adds **Scale bar + SVG** and **Scale bar + JPEG** below Move and
Clear Region, using the selected area, channels, LUT/gamma, frame and export scale.

- SVG embeds the lossless rendered PNG with an editable white vector scale bar
  and text label. Its image pixels retain the chosen raster resolution.
- JPEG includes the same calibrated bar, encoded at quality 98 with 4:4:4 colour sampling.
- Bar length follows horizontal pixel calibration and the exported pyramid level.
  Missing calibration disables these exports; very small outputs require a larger region or scale.
- Ordinary PNG/JPEG and raw ND2/TIFF exports remain available. Rendered exports
  also use the correct level identifier when a reduced pyramid omits level zero.

This release provides the macOS Apple-silicon DMG. Windows testing and portable
builds run in CI, but the published Windows version remains
[v2.0.0](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.0.0).
The app is ad-hoc signed, not Developer ID notarized. The existing signed Sparkle
update identity and feed are retained.

Physical scale-only and scale-plus-frame comparison linking is planned for 2.1.4
and is not included in this release.

Validation covers calibrated bar geometry across export scales, exact embedded
PNG preservation, channel/LUT/frame selection, HTTP filenames and image types,
and the installed native viewer and scientific export smoke checks.
