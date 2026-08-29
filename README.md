<p align="center">
  <img src="docs/icon.png" alt="nd2wsi-viewer icon — a blue resolution pyramid in a macOS squircle" width="128">
</p>

<h1 align="center">nd2wsi-viewer</h1>

<p align="center">
  <b>Whole-slide viewing for Nikon ND2 and Aperio SVS — a native macOS app and CLI,<br>
  with pixel-exact ROI export back to ND2.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-13%2B-1d1d1f?style=flat-square&logo=apple&logoColor=white" alt="macOS 13+">
  <img src="https://img.shields.io/badge/python-3.10%2B-0A84FF?style=flat-square" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/formats-.nd2%20%C2%B7%20.svs-30D158?style=flat-square" alt="ND2 + SVS">
  <img src="https://img.shields.io/badge/tests-16%20passing-8E8E93?style=flat-square" alt="tests">
  <img src="https://img.shields.io/badge/license-MIT-8E8E93?style=flat-square" alt="MIT">
</p>

---

## Why this exists

Pathology viewers solved whole-slide viewing years ago: open an H&E slide
and it is simply *there* — fly out to the whole section, dive to single
nuclei, never wait, never load the file. Nikon's NIS viewers give stitched
ND2 scans no such treatment, and NIS-Elements does not run on a Mac at all.
I scan slides on a Ti2, read H&E from Aperio scanners, and work on a Mac,
so I built the viewer I wanted: ND2 handled the way pathology viewers
handle their slides, native on macOS.

It adds the one thing pathology viewers lack: crop any region back out as
a **real ND2** that reopens in NIS-Elements with its calibration and
channels intact.

## How it works

The pipeline is `slide → OME-Zarr pyramid → local tile server → deep-zoom
viewer`. The pyramid is built once, next to the slide, with bounded memory;
after that, multi-gigabyte scans pan and zoom fluidly because the viewer
touches only the tiles on screen. SVS input reads the baseline level of the
pyramidal TIFF through `tifffile`, takes its µm calibration from the Aperio
`MPP` field, and flows through the same pipeline.

```
pip install .
nd2wsi view slide.nd2 another.svs
# builds pyramid_<name>.ome.zarr next to each slide (once),
# then opens the tabbed viewer at http://127.0.0.1:8000
```

To skip the terminal, run `packaging/build_mac_app.sh`: it produces a
double-clickable `nd2wsi-viewer.app` and a drag-to-Applications `.dmg`
with everything bundled.

## Install

```bash
pip install .            # nd2, numpy, dask, zarr, numcodecs, pillow, tifffile
pip install ".[svs]"     # + imagecodecs, for Aperio SVS (JPEG tiles)
pip install ".[app]"     # + pywebview, for the native macOS window
pip install ".[legacy]"  # + imagecodecs, for JPEG2000-era (pre-2012) ND2

# ND2 export (the ND2 button, `nd2wsi crop`) uses limnd2, which lives on
# Laboratory Imaging's own package index:
pip install --index-url https://pypi.laboratory-imaging.com/simple limnd2
```

Python ≥ 3.10. The server is stdlib `http.server` and OpenSeadragon is
vendored, so viewing works offline.

## Commands

```text
nd2wsi info    slide.nd2|.svs    # internal layout: chunk census, pyramid,
                                 # compression, calibration, stage positions
nd2wsi convert slide.nd2 [out.ome.zarr] [--tile 512] [--t N] [--z mid|max|N]
                                 [--position N] [--workers N] [--overwrite]
nd2wsi crop    slide.nd2 roi.nd2 --x X --y Y --w W --h H [--c 0,1]
                                 # native-resolution ND2 → ND2 crop, straight
                                 # off the memory map — no pyramid needed
nd2wsi serve   a.ome.zarr [b.ome.zarr …]   # one tab per store
nd2wsi view    one.nd2 [two.svs …]         # convert if needed, then serve
nd2wsi-viewer  [slide.nd2|.svs]            # native macOS window
```

`convert` sizes its thread pool to your CPU; `--workers` overrides it.
Each store holds one 2D plane: pick the timepoint with `--t`, the Z plane
with `--z` (an index, `mid`, or `max` for a maximum-intensity projection),
and the stage position with `--position`. RGB slides are stored as C=3.

## The viewer

The root page is a browser-style tab strip: one tab per slide, a `+`
button, and drag-and-drop. Each tab keeps its own viewer state while open.
`POST /api/open` adds slides at runtime.

The files created next to a slide name themselves:
`pyramid_<slide>.ome.zarr` holds the viewing cache and
`annotations_<slide>.json` holds the annotations. Older unprefixed names
still load and migrate.

The design follows the macOS design language, with values measured from
Apple's macOS 27 UI kit: SF Pro and SF Mono type, translucent panels over
the slide, capsule glass controls with specular edges, and kit-geometry
switches. Each control panel is a small mac window — drag it by the title
bar, resize it from any edge (the LUT histograms grow with it), and use
the traffic lights to close, collapse, or zoom it. The viewer remembers
the geometry.

* **Navigation** — deep-zoom pan and scroll with a minimap. The viewer is
  Retina-aware: the zoom and level readouts count device pixels, and
  overzoom stops at about 3 device pixels per image pixel.
* **Channels & LUTs** — the NIS-style LUT panel, for any number of
  channels: each channel shows its intensity histogram in the channel
  color, black/white triangles set the display window in raw counts, and a
  knob on the mapping curve sets gamma (0.25–4; γ > 1 brightens midtones).
  `Auto` stretches the window to the 0.1–99.9 percentile. Shift-drag moves
  all channels together; every channel has a reset and an on/off switch.
* **Measure & annotate** — `M` draws a ruler and reports µm
  (anisotropy-correct); `P` drops a pin; `B` draws a box; each takes a
  note. The colored dot beside each list item cycles it through the Apple
  system palette; clicking an item flies to it. Everything saves itself to
  the annotation sidecar — never into the ND2 — reloads on the next open,
  and imports from or exports to plain JSON.
* **Region export** — press `R`, drag a rectangle, and refine it by typing:
  the Pixels and Physical (µm) fields edit the region directly and stay in
  sync, using the file's own calibration (ND2 `voxel_size`, Aperio `MPP`).
  Exports are always native resolution:
  * **ND2** (default) — a real, uncompressed ND2 carrying the source's
    calibration, channel names, colors, and objective magnification; it
    reopens in NIS-Elements and round-trips through this tool.
  * **TIFF** — raw pixel values, original dtype, tiled, with the pixel
    size in the resolution tags.
  * **PNG / JPEG** — rendered exactly as displayed, current LUTs included
    (capped by `--max-render-mpx`, default 100 MPx).
  ND2 and TIFF stream, so any size works — the whole slide included.
* **Status bar** — stage position in µm, pixel coordinates, zoom, active
  pyramid level, and a scale bar.

Everything the UI does is plain HTTP:

```
GET  /api/slides                 open slides and their ids
POST /api/open                   {"path": …} → convert if needed, add a tab
GET  /s/<sid>/api/info
GET  /s/<sid>/api/histogram
GET  /s/<sid>/api/tile/{level}/{x}/{y}.jpg?c=0,2&win=399:1057,220:2366:2.0
GET  /s/<sid>/api/roi?level=0&x=…&y=…&w=…&h=…&format=nd2|tiff|png|jpg
GET  /s/<sid>/api/annotations    sidecar contents
POST /s/<sid>/api/annotations    {"items": [...]} → atomic sidecar write
```

Bare `/api/…` addresses the first slide, so single-slide scripts stay
simple. `win` takes one `lo:hi[:gamma]` slot per channel; an empty slot
keeps the stored default. The UI always exports level 0; the `level`
parameter serves scripted downsampled reads.

## The macOS app

`nd2wsi-viewer` wraps the same server in a native WKWebView window. It
opens to a black drop target; drop an `.nd2` or `.svs` (or click to
browse), the first open builds the pyramid, and the viewer loads from an
ephemeral localhost port.

`packaging/build_mac_app.sh` bundles Python and every dependency with
PyInstaller, signs the bundle ad-hoc, self-tests it headlessly, and wraps
it in a `.dmg`. Ad-hoc signing suits direct hand-offs; public distribution
also needs a Developer ID and notarization.

## Inside a stitched ND2

The question that started this project: does a stitched ND2 keep its
acquisition tiles, or is it one flattened raster? Run `nd2wsi info` on your
own files for the answer; on ours it was unambiguous.

* A modern ND2 stores pixel data as one `ImageDataSeq` blob per frame,
  never split spatially. A stitched scan is a single frame, so it is one
  contiguous raster; the acquisition tiles do not survive stitching.
* An uncompressed ND2 is efficiently seekable: the reader hands out
  zero-copy views onto a memory map, so cropping a small ROI from a
  multi-GB slide touches only the pages it needs. zlib-compressed files
  lose this property.
* Multipoint files saved *unstitched* keep per-tile frames and µm stage
  positions — addressable tiles, but stitching becomes your job.

The slide is flat but seekable. Interactive viewing needs a pyramid and a
tile protocol, which OME-Zarr and OpenSeadragon supply; this tool is the
bridge between them.

## Measured on real Ti2 scans

Five 20× "Scan Large Image" acquisitions from an ECLIPSE Ti2 with
NIS-Elements AR 2022 (two-channel CY5 + DAPI uint16, 0.66 µm/px, 1.4–5.5 GB
per file) all showed the same layout: exactly one uncompressed
`ImageDataSeq` blob — no surviving tiles, no embedded pyramid. Conversion
on an Apple-silicon laptop (14 cores):

| slide (px)      | ND2    | convert | pyramid on disk |
|-----------------|--------|---------|-----------------|
| 19,029 × 19,171 | 1.4 GB |   7.3 s | 1.0 GB (7 levels) |
| 20,064 × 34,271 | 2.6 GB |  15.1 s | 1.9 GB (8 levels) |
| 53,144 × 27,799 | 5.5 GB |  49.2 s | 3.8 GB (8 levels) |

On the 1.48-gigapixel slide, one zoom gesture fetched tiles from level 6
down to level 0 in sequence, ending with only the visible native tiles. A
11,957 × 7,972 px ROI (364 MB raw) exported as TIFF in 2.4 s and as a new
ND2 via `nd2wsi crop` in 14 s. Both exports matched the source memory map
pixel for pixel, with calibration, channel names, display colors, and
objective magnification carried over.

## Tests

`pytest` runs 16 tests. They verify that ND2 exports round-trip pixel-exact
through the independent `nd2` reader with calibration and channel metadata
preserved; that three-channel files convert, render histograms, and export;
that the annotation sidecar survives a GET/POST round-trip on disk; that
multi-slide routing serves each slide and closes cleanly; and that the LUT
window/gamma parsing and compositing math behave. The stores also open in
the official `ome-zarr-py` reader, so napari and vizarr read them directly.

`scripts/make_synthetic_nd2.py` generates fixtures with the same internal
shape as real stitched scans, using Laboratory Imaging's own `limnd2`
writer; the export tests skip when `limnd2` is absent.

## Architecture

```
 slide.nd2 ──(nd2 mmap, zero-copy frame view)──┐
                                               │  dask: (1, tile, tile) chunks
                                               ▼
                     pyramid_<name>.ome.zarr   0/ 1/ 2/ … (NGFF 0.4, zstd)
                                               │
                 stdlib ThreadingHTTPServer ───┤
                   /api/tile → JPEG/PNG        │   /api/roi → ND2 / tiled TIFF
                   (windowed composite)        ▼   (streamed, raw dtype)
                          OpenSeadragon custom tile source (vendored)
```

* **convert.py** writes level 0 straight from memory-map slices, then
  computes each further level as a 2×2 mean of the one before it, so
  memory never depends on slide size.
* **render.py** composites tiles (RGB passthrough or additive windowed
  channels) and streams ROI exports.
* **export_nd2.py** writes ROIs back to ND2 through `limnd2`, one 512-row
  band at a time.
* **server.py + static/** serve the tabs, the tiles, and the viewer.

## Limitations

* Each store holds one 2D plane; choose T/Z/position at convert time, and
  convert again for another plane.
* ND2 export needs `limnd2` (see Install) and covers uint8/uint16 sources;
  without it, the ND2 button explains itself and TIFF/PNG/JPEG still work.
* zlib-compressed ND2 frames cannot be memory-map-sliced; converting a
  compressed stitched slide loads the whole frame. Re-save uncompressed,
  or use `bioformats2raw`.
* Files that combine multiple channels with RGB components are rejected.
* Legacy JPEG2000-era ND2: `info` works; `convert` needs `[legacy]` and
  loads whole frames.
* The server is a single-user local viewer, not a hardened web service.

## Related work

* **limnd2** — Laboratory Imaging's own Python ND2 SDK: reader, writer, and
  OME-Zarr exporter. Its exporter loads whole frames, which is what breaks
  on giant stitched scans — the gap this tool fills.
* **nis2pyr** — ND2 → pyramidal OME-TIFF, viewed in QuPath.
* **QuPath + Bio-Formats** — a full WSI analysis suite that opens ND2.
* **bioformats2raw** — Java, robust ND2 → OME-Zarr.
* **large-image-source-nd2** — Kitware's ND2 tile source for Girder.

MIT licensed. OpenSeadragon (BSD-3) is bundled under
`nd2wsi/static/vendor/openseadragon/`.
