# nd2wsi-viewer 2.1.14

The gold **Type LUT** button is replaced by a compact, borderless pencil icon from the same Fluent set as the adjacent copy and paste icons. All three actions use matching 16 px icons on macOS and Windows. The pencil retains the **Type LUT** accessible label.

Hover over edit, copy or paste for about one second to see a short explanation. The descriptions are also available with keyboard focus. A successful copy still shows the green check and **Copied!** immediately.

This is a presentation-only change. The numeric LUT dialog, input validation, copy/paste behavior and LUT values are unchanged. Raw image values, channel colors and visibility, annotations and cache formats are unchanged.

The macOS download targets Apple silicon and uses an ad-hoc code signature plus signed Sparkle updates. It is not Developer ID notarized. The Windows download is an x64 portable build; Windows 11 ARM64 runs it through x64 emulation, not a native ARM64 binary. The Windows executable is not Authenticode-signed, and Windows updates remain manual.
