# nd2wsi-viewer 2.1.8

A maintenance release for macOS and Windows, built from the same source revision.

## Code cleanup

- Reuse the shared child-process environment setup when opening the standard viewer, preserving independent windows and stripping inherited replay/capture settings.
- Remove an unused cache-lock wrapper; active lock acquisition and stale-lock safety checks are unchanged.
- Remove an unused rectangle helper from the viewer; region selection, rotation, mirroring and export coordinates retain their existing behavior.
- Fix simultaneous window launches being mistaken for an update in progress on slower storage. Durable session writes no longer hold the launch gate; lifetime locks still prevent installation while a window is starting or open. Kernel-lock diagnostic metadata no longer requests an unnecessary disk flush.

## Platform alignment

- Both packages retain the 2.1.7 brightfield ND2 controls: white-background viewing with independently adjustable red, green and blue ranges, gamma and visibility.
- Existing LUT, annotation, measurement, calibrated SVG export and Arial-default label behavior are retained. No storage format or research-data migration is introduced.
- Apple silicon retains the macOS Metal option and standard viewer fallback. Windows uses the standard viewer and updates via its portable ZIP; Metal and Sparkle remain macOS-only.

The macOS package targets Apple silicon on macOS 12 or later. It is ad-hoc signed, not Developer ID notarized; Sparkle uses the existing update signing identity. The Windows portable ZIP is x64, with Windows 11 ARM64 support through x64 emulation, not a native ARM build. The executable is not Authenticode-signed; no Setup installer is included.

The [GitHub release](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.8) records the exact source commit, completed validation and artifact checksums.
