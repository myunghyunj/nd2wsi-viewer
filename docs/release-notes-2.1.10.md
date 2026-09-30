# nd2wsi-viewer 2.1.10

Makes primary action buttons readable in light appearance.

## Light-mode buttons

- Give the annotation editor's Done button a blue background with white text, including when viewing SVS slides in light mode.
- Apply the same correction to the ND2 export button. Both blue gradient colors provide at least 4.5:1 contrast against white labels.
- Keep dark appearance, neutral Delete buttons and annotation saving behavior unchanged.

## Platform alignment

The macOS DMG and Windows ZIP share this source revision and version. This appearance fix applies to both platforms; image rendering, scientific measurements and exported data are unchanged.

The macOS package targets Apple silicon on macOS 12 or later. It is ad-hoc signed, not Developer ID notarized; Sparkle retains the existing update-signing identity. The Windows portable ZIP is x64, with Windows 11 ARM64 support through x64 emulation, not a native ARM build. The executable is not Authenticode-signed; no Setup installer is included.

The [GitHub release](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.10) records the exact source commit, completed validation and artifact checksums.
