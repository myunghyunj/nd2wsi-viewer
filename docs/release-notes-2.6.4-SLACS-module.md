# 2.6.4_SLACS-module

This lab release includes the [2.6.4 Astromap import fixes](release-notes-2.6.4.md).

- Import valid regions from a TXT even when other rows are malformed. Review the skipped rows without editing the source file.
- Optionally recover zero-width or zero-height legacy regions as one-pixel regions. Recovery requires an explicit choice and reports both original and corrected coordinates.
- The tracing window retains skipped-row and repair details while navigating, and preserves the current regions when a file has no usable rows.
- SLACS stage follow, laser controls, session logging and the bundled Windows WebView2 runtime remain available. This edition keeps automatic public-viewer updates disabled.
- The Apple silicon DMG installs `nd2wsi-viewer SLACS.app` alongside the public viewer. Its separate bundle identity and disabled update feed keep it independent of public-viewer updates. Hardware control remains available on supported Windows SLACS setups.

The macOS app is ad-hoc signed, not Developer ID signed or notarized. Windows builds are not Authenticode-signed. Corresponding LGPL SDPC decoder sources and checksums accompany the release.
