# nd2wsi-viewer 2.1.13

Channel LUTs can now be entered numerically with the gold **Type LUT** button. Set each channel's minimum, maximum and gamma; invalid entries leave all channels unchanged. The histogram-axis checkbox is shortened to **Auto-fit**, with its behavior unchanged.

Copy and paste channel LUTs between viewer windows through the system clipboard. Paste matches unique channel names exactly, regardless of channel order. Unmatched, duplicate, invalid or incompatible entries are skipped. Channel colors, visibility, raw image values and annotations are not changed.

The shared macOS and Windows toolbar uses compact, borderless Fluent icons. Copy shows a green check and **Copied!** only after success, then returns to its normal state. The tooltip also works with keyboard focus.

macOS title-bar dragging now uses the native Cocoa window-drag operation, including after a display configuration changes. Windows retains its native title bar.

The macOS download targets Apple silicon and uses an ad-hoc code signature plus signed Sparkle updates. It is not Developer ID notarized. The Windows download is an x64 portable build; Windows 11 ARM64 runs it through x64 emulation, not a native ARM64 binary. Windows updates remain manual.
