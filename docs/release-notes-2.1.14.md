# nd2wsi-viewer 2.1.14

The standard viewer now remembers each file's last-viewed channel minimum, maximum and gamma. Reopening the same file—even after restarting the app—restores those values before the image is displayed. Saved values take precedence over the initial Auto adjustment; Auto and Reset remain available.

LUT preferences are stored locally in application data, separately for each source file. A moved, replaced or incompatible source starts with its normal defaults. Only display ranges and gamma are remembered, not channel colors, visibility, image pixels or annotations. If saving fails, the viewer reports the problem and does not silently finish a normal close with pending LUT changes.

The opt-in Metal viewer's independent LUT controls are not persisted by this release. Its existing return-to-standard handoff remains available; out-of-range handoff values stay visible without being silently clamped or saved.

The gold **Type LUT** button is replaced by a compact, borderless pencil icon from the same Fluent set as the adjacent copy and paste icons. All three actions use matching 16 px icons on macOS and Windows. The pencil retains the **Type LUT** accessible label.

Hover over edit, copy or paste for about one second to see a short explanation. The descriptions are also available with keyboard focus. A successful copy still shows the green check and **Copied!** immediately.

The numeric LUT dialog, input validation and channel-name copy/paste behavior are unchanged. Raw image values, annotations and cache formats are unchanged.

The macOS download targets Apple silicon and uses an ad-hoc code signature plus signed Sparkle updates. It is not Developer ID notarized. The Windows download is an x64 portable build; Windows 11 ARM64 runs it through x64 emulation, not a native ARM64 binary. The Windows executable is not Authenticode-signed, and Windows updates remain manual.
