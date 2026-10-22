# nd2wsi-viewer 2.4.0

Shading Correction now defaults to **Rank1 (BF-free)**, using the adopted
`consensus_rank1` method to estimate a repeating illumination pattern from a
supported stitched ND2 slide's fluorescence. FITC and CY3 are assessed
separately. BF and DAPI remain unchanged; DAPI may help propose a pattern but
is never corrected. A BF channel is not required.

The confirmation dialog retains **BF-guided** as a manual choice with its
existing H method and channel rules. Opening a slide does not start correction,
and an unsupported Rank1 fit does not automatically switch to BF-guided.
Channels without sufficient evidence remain unchanged. Plate/time-series
acquisitions and Z projections remain unsupported for Shading Correction.

## Saved correction and original data

An accepted correction replaces the `.nd2svs` viewing pyramid and remains
available after reopening. **Revoke** rebuilds the original pyramid from the
source ND2. Cancellation or failure before publication preserves the current
cache. Existing H corrections and earlier BF-free preview caches remain
readable with their saved models; updating the application does not refit them.
New Rank1 caches require version 2.4.0 or later.

PNG, JPEG and SVG exports use the corrected display and include
`shading-corrected` in their filenames. Native ND2/TIFF exports, including
reduced-resolution exports, and pixel readouts retain original values.
Source slides, annotations and saved LUTs retain their preservation rules.

The [method and validation limits](shading-correction-validation.md) distinguish
Rank1 from the retained BF-guided method. The README before/after images show a
historical H result, not Rank1. Periodic fitting checks do not establish
biological preservation, uniform improvement or quantitative assay validity.

## Panel shortcut routing

The tab toolbar and active slide use a shared panel shortcut route. It ignores
held-key repeats and protects editable fields, text composition and native
copy/select-all when meaningful text is selected. Existing combinations are:

| Panel | macOS | Windows |
|:--|:--|:--|
| Annotations | `⌘A` | `Ctrl+A` |
| Channels & LUTs | `⌘C` | `Ctrl+C` |
| Export | `E` | `E` |
| Slide Info | `⌘I` | `Ctrl+I` |

See the [keyboard reference](../README.md#keyboard) for the complete list.
The macOS source GUI smoke passed all 12 panel, repeat and editable-field
checks through AppKit and WKWebView. It used process-local synthetic Latin
key events; physical keyboard input was not assessed. Packaged platform checks are recorded with the release artifacts. This smoke
does not assess native Windows keyboard input.

## Platform limits

The macOS download is for Apple silicon, ad-hoc signed and not Developer ID
notarized. Signed Sparkle updates retain their existing signing identity.
Windows uses the portable x64 application, including x64 emulation on
Windows 11 ARM64; it is not a native ARM64 binary, requires WebView2 and is not
Authenticode-signed. Windows updates remain manual.

SDPC support and its pinned LGPL decoder retain the
[existing format limits](sdpc-support.md). Corresponding codec sources
accompany both platform downloads. Artifact checksums and completed platform
verification belong to the published release record.
