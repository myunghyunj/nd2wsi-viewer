# SDPC support — v2.3.0

Version 2.3.0 adds direct viewing of the validated SQ/SDPC 24-bit HEVC
whole-slide format. Native release builds include a source-built decoder.
Its build and redistribution requirements are described in
[`packaging/SDPC-CODEC-NOTICE.txt`](../packaging/SDPC-CODEC-NOTICE.txt).

## Supported behavior

- Open `.sdpc` from the file dialog, drag/drop, Finder, or CLI.
- Read compressed tiles on demand, with a shared, byte-bounded decoded-tile
  cache. No pixel pyramid or converted whole slide is written to disk.
- Preserve rectangular tile geometry, true image bounds, embedded pyramid
  levels, and file-supplied physical calibration. Additional overview levels
  use local means of the deepest embedded level.
- Use the existing RGB viewer, pixel inspection, annotations, and ROI export
  paths. Native TIFF export preserves decoded level-0 RGB samples and their
  calibration. These samples are **not** the pre-compression microscope data:
  the supplied SDPC images use lossy HEVC.
- Keep Agent annotation snapshots separate from shared user annotations.
  Normal user annotation saving retains the existing sidecar behavior.
- Show source-embedded photographs and the thumbnail in Slide Info. Select an
  image to enlarge it within the inspector. SDPC does not reliably identify
  label versus macro photographs in the validated records, so the photographs
  are named **Embedded image 1**, **Embedded image 2**, and so on, in file order.
  They are read from the open source, not reconstructed from the tissue view.
- Reject unsupported variants, invalid dimensions/offsets, truncated tiles,
  source replacement, and decode failures instead of returning blank tissue.

Source use requires the optional decoder:

```sh
pip install -e '.[app,sdpc]'
nd2wsi info /path/to/slide.sdpc
nd2wsi view /path/to/slide.sdpc
```

SDPC is direct-only: portable whole-slide conversion, `--store`, and ND2
plane-selection options are rejected. JPEG whole-slide SDPC and other container
variants, and vendor colour-correction emulation, are not supported in this
version. Embedded JPEG photographs and BMP/JPEG thumbnails are supported.
Existing ND2/SVS behavior is retained.

## Container structure

The reader follows little-endian records:

| Record | Contents used |
| --- | --- |
| `SQ` | Declared file/header sizes, source dimensions, tile width/height, hierarchy count, scale, ruler (µm/pixel), objective, codec |
| `EI` | Next-record link and colour-gamma metadata; no patient fields |
| `MI` | 123-byte associated-image header, payload length at +34, next-record link at +43; bounded JPEG photographs |
| `IF`, layer 0 | 122-byte thumbnail header and bounded BMP/JPEG payload |
| `IF`, layers 1…N | 122-byte level header, `uint32` tile-length table, independent row-major HEVC payloads |

Each level's grid, edge padding, scale, calibration, payload sum, and next
offset must agree. All reads are range-checked. For layouts without the
associated-image chain, baseline discovery is bounded to the first 16 MiB
and requires validation of the entire pyramid. Index and tile sizes are
bounded before allocation.

Auxiliary images have separate compressed-byte and decoded-pixel limits, and
their decoded dimensions must match the records. Their delivery is restricted
to the registered source and rechecks source identity after decoding. Only
images are exposed; unrelated patient metadata records are not surfaced.
The inspector uses bounded 512-pixel previews and up to 2048-pixel enlarged
views. It does not change the original embedded images or their orientation.

The public [opensdpc structure declarations](https://github.com/WonderLandxD/opensdpc/blob/main/opensdpc/Sdpc_struct.py)
and independent [SlideForge parser](https://github.com/drgexy/SlideForge/blob/main/slideforge/converter.py)
were used as format references. The reader is implemented locally and does
not bundle a proprietary scanner SDK.

## Local evidence

Three supplied files were examined without changing the originals:

| Sample | Native width × height (pixels) | Embedded levels | Native tile width × height |
| --- | ---: | ---: | ---: |
| 1 | 249,600 × 173,696 | 8 | 624 × 736 |
| 2 | 219,648 × 179,584 | 8 | 624 × 736 |
| 3 | 237,120 × 182,528 | 8 | 624 × 736 |

All three report 0.190554887 µm/pixel and a 40× objective. Their structure and
tile payloads were checked directly, including multiple levels, seam-crossing
regions, corners and padded edges. The checks verified:

- Agreement with an independent FFmpeg RGB decode for one tile per sample.
- Pixel-exact native TIFF ROI exports across a tile boundary and retained
  physical calibration; matching HTTP pixel readouts and overview rendering.
- Unchanged source identities, sizes, modification times, and directory entries.
- Native macOS WKWebView display in a fresh isolated Agent window.
- Fully synthetic tests for geometry, malformed records, source replacement,
  cache/lifetime behavior, routing, Unicode filenames, and packaging gates.

These are engineering checks, not biological validation. They do not prove
support for every SDPC scanner or format revision. No real slide payloads or
patient/label records are committed as test fixtures.

## Colour and distribution limits

The files contain a gamma field of 1.5. The vendor's
[SDK interface](https://github.com/MrPeterJin/ASlide/blob/main/Aslide/sdpc/include/SlideImage.h)
distinguishes optional colour correction from already-corrected tiles, but
does not establish a complete public transform specification. The viewer
therefore displays decoded RGB without applying a guessed gamma/CCM transform.
Colour equivalence to a particular vendor-viewer preset remains unverified.

YUV-to-RGB conversion explicitly uses FFmpeg's bilinear, accurate-rounding,
bit-exact mode. This fixes the conversion behavior instead of inheriting
version-dependent swscale defaults. It is a colour reconstruction choice, not
a recovery of pre-compression samples, and can differ from earlier development
previews at high-contrast chroma edges.

Native release builders require PyAV 16.0.1 linked to a restricted, shared-library
FFmpeg 8.1.3 build from pinned upstream source archives. GPL, nonfree, external
codec libraries, network access, and unneeded codecs are disabled. The builders
verify the wheel, installed libraries, runtime license/configuration, and a
fully synthetic SDPC decode/export smoke test. A matching source archive,
build recipe, configuration, licenses, and checksums accompany each platform's
release assets. The application also carries the notices and source location.

The macOS deployment target remains 12.0; checking that target in the binaries
is not equivalent to executing on macOS 12. Windows releases retain packaged
x64 and ARM64-emulation checks. Results for a particular release must be taken
from its completed build and verification records, not inferred from this
format-level evidence.
