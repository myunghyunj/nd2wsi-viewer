# nd2wsi-viewer 2.1.9

Fixes hidden slide tabs when entering macOS full-screen with the green window button.

## Full-screen tabs

- Keep the tab strip visible and usable in native macOS full-screen. The windowed title-bar spacer no longer leaves WebKit clipping the top of the page.
- Restore inline traffic lights and the original window size when leaving full-screen, including repeated entry/exit cycles.
- Keep open slides and tab state intact; no slide reload, cache rebuild or data migration is needed for the transition.

## Platform alignment

The macOS DMG and Windows ZIP share this source revision and version. The fix is macOS-only; Windows retains its native title bar and existing viewer behavior. RGB ND2 controls, annotations, measurements, calibrated SVG export and the macOS Metal option are unchanged.

The macOS package targets Apple silicon on macOS 12 or later. It is ad-hoc signed, not Developer ID notarized; Sparkle retains the existing update-signing identity. The Windows portable ZIP is x64, with Windows 11 ARM64 support through x64 emulation, not a native ARM build. The executable is not Authenticode-signed; no Setup installer is included.

The [GitHub release](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.9) records the exact source commit, completed validation and artifact checksums.
