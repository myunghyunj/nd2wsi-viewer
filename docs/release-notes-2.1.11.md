# nd2wsi-viewer 2.1.11

This maintenance release separates plate frame controls from the main viewer code. Time and Z navigation, playback, autofocus and cache-status requests now share a dedicated controller, with direct module tests for physical Z direction and request cancellation.

Existing image rendering, annotation ownership and site-switch behavior are preserved. This refactor makes future plate changes easier to test; it does not claim a performance or scientific-method change.

The macOS download targets Apple silicon and uses an ad-hoc code signature plus signed Sparkle updates. It is not Developer ID notarized. The Windows download is an x64 portable build; Windows 11 ARM64 runs it through x64 emulation.
