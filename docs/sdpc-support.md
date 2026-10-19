# SDPC support — v2.3 development

Version 2.3.0 adds direct viewing of the validated SQ/SDPC 24-bit HEVC
whole-slide format. This is a source/development implementation, not a
published native release. The decoder packaging gate is described in
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
- Reject unsupported variants, invalid dimensions/offsets, truncated tiles,
  source replacement, and decode failures instead of returning blank tissue.

Source use requires the optional decoder:

```sh
pip install -e '.[app,sdpc]'
nd2wsi info /path/to/slide.sdpc
nd2wsi view /path/to/slide.sdpc
```

SDPC is direct-only: portable whole-slide conversion, `--store`, and ND2
plane-selection options are rejected. JPEG SDPC and other container variants,
associated macro/label images, and vendor colour-correction emulation are not
supported in this version. Existing ND2/SVS behavior is retained.

## Container structure

The reader follows little-endian records:

| Record | Contents used |
| --- | --- |
| `SQ` | Declared file/header sizes, source dimensions, tile width/height, hierarchy count, scale, ruler (µm/pixel), objective, codec |
| `EI` | Next-record link and colour-gamma metadata; no patient fields |
| `MI` | 123-byte associated-image header, payload length at +34, next-record link at +43; payload skipped |
| `IF`, layer 0 | 122-byte thumbnail header and payload; skipped |
| `IF`, layers 1…N | 122-byte level header, `uint32` tile-length table, independent row-major HEVC payloads |

Each level's grid, edge padding, scale, calibration, payload sum, and next
offset must agree. All reads are range-checked. For layouts without the
associated-image chain, baseline discovery is bounded to the first 16 MiB
and requires validation of the entire pyramid. Index and tile sizes are
bounded before allocation.

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

Development checks used PyAV 16.0.1 on the current macOS machine. Public
macOS/Windows binaries, the existing macOS 12 baseline, and bundled-codec
redistribution have not been verified for this feature. Native builders exclude
the codec by default; the explicit development opt-in is not a publishable
release configuration. Existing installations are not replaced by this work.
