<p align="center">
  <img src="docs/icon.png" alt="nd2wsi-viewer icon" width="112">
</p>

<h1 align="center">nd2wsi-viewer</h1>

<p align="center">
  <strong>Whole-slide viewing for stitched Nikon ND2 and Aperio SVS.</strong><br>
  Pan from an overview to native pixels. Mark a region. Export its raw values.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-13%2B-1d1d1f?style=flat-square&logo=apple&logoColor=white" alt="macOS 13+">
  <img src="https://img.shields.io/badge/Python-3.11%2B-0A84FF?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-8E8E93?style=flat-square" alt="MIT">
</p>

![Two slides open in nd2wsi-viewer](docs/hero.png)

## The problem

An SVS is made for whole-slide viewing. It stores tiled images at several resolutions. A viewer reads only the tiles on screen.

A stitched ND2 is often different. In the NIS-Elements files tested here, one selected frame is one large raster. It contains the native pixels but no useful whole-slide pyramid. General ND2 viewers can open it, yet a large scan remains slow to survey and awkward to crop.

`nd2wsi-viewer` adds the missing whole-slide layer. It keeps the ND2 as the source of truth, stores a compact set of reduced levels, and serves both through one local tile interface.

Nothing is uploaded.

## What it does

- Opens stitched Nikon `.nd2` and Aperio `.svs` slides.
- Pans and zooms from a full-slide overview to native pixels.
- Controls fluorescence channels, colors, windows, gamma, and histograms.
- Reads native pixel values under the cursor and inspects slide/cache metadata.
- Compares two slides with linked calibrated navigation and a manual alignment offset.
- Measures calibrated distances.
- Stores rulers, pins, boxes, and notes in a JSON sidecar.
- Exports annotations as QuPath-compatible GeoJSON.
- Exports a selected region as ND2, TIFF, PNG, or JPEG.
- Preserves raw values in ND2 and TIFF exports.
- Opens several slides in tabs.
- Runs as a macOS app, a browser UI, or a CLI.

Unknown calibration stays unknown. If a file has no valid pixel size, the viewer reports pixels, hides the scale bar, and omits physical calibration from exports.

## Quick start

### macOS app

Download the DMG from the latest release. Drag the app to Applications. Drop an `.nd2` or `.svs` file onto the window.

The current binary is ad-hoc signed, not notarized. macOS may ask you to confirm its first launch.

### Python

Python 3.11 or newer is required.

```bash
python -m pip install .
```

Optional extras add the rest.

```bash
python -m pip install ".[app]"       # native macOS window
python -m pip install ".[svs]"       # JPEG and JPEG 2000 SVS decoding
python -m pip install ".[legacy]"    # older JPEG 2000 ND2 files
```

ND2 export uses Laboratory Imaging's package index.

```bash
python -m pip install ".[nd2export]" \
  --extra-index-url https://pypi.laboratory-imaging.com/simple
```

Open one or more slides.

```bash
nd2wsi view slide.nd2 another.svs
```

Inspect a file first.

```bash
nd2wsi info slide.nd2
nd2wsi info slide.nd2 --json
```

## How storage works

### Compact ND2 cache

An eligible modern uncompressed ND2 stores this way.

```text
level 0      original ND2, read through its memory map
levels 1..n  compact overview cache
```

The cache does not copy native resolution. It stores only the reduced levels.

If the native image contains `N` pixels, a full pyramid contains about `4N/3` pixels.

```text
N + N/4 + N/16 + ... = 4N/3
```

The compact cache contains about `N/3` pixels.

```text
N/4 + N/16 + ... = N/3
```

Thus its pixel volume is about one quarter of a full pyramid. Actual disk use also depends on compression, blank regions, chunk size, and filesystem allocation blocks.

Compact mode requires four things.

- a modern ND2 container
- uncompressed frame data
- one stored T/P/Z plane
- a memory-mapped frame view

Compressed or legacy ND2 files, maximum-intensity projections, and unsupported layouts use a full cache instead.

### Portable OME-Zarr

Use `convert` when the result must stand alone.

```bash
nd2wsi convert slide.nd2 slide.ome.zarr
```

This writes native resolution and every reduced level. The result uses OME-NGFF 0.4 metadata on Zarr v2 for broad reader compatibility.

A compact cache is not a portable OME-Zarr dataset. It depends on its source ND2.

### SVS

A tiled SVS already contains a pyramid. The viewer reads its embedded tiles directly and fills small gaps in the level ladder on demand. It writes no cache unless you run `convert` yourself.

## Files on disk

Managed files live beside the slide.

```text
experiment/
├── slide.nd2
└── nd2wsi/
    ├── annotations/
    │   └── annotations_slide.nd2--t0-p0-z0.json
    └── caches/
        └── slide.nd2--t0-p0-zmid.nd2wsi-cache/
            ├── manifest.json
            └── store.ome.zarr/
```

The manifest records the source fingerprint, selected plane, image shape, pyramid algorithm, cache generation, and storage format. A build occurs in a temporary sibling. The final path appears only after validation.

A stale or damaged cache is quarantined, not deleted. A store opened explicitly is never treated as disposable cache.

The trash button removes one viewing cache. It leaves the source slide and annotations intact.

Migrate older layouts with `tidy`.

```bash
nd2wsi tidy /path/to/slides --dry-run
nd2wsi tidy /path/to/slides --move-annotations
```

## Viewer

### Display

![Channel histogram and LUT controls](docs/channels-luts.png)

Each fluorescence channel has its own color, display window, gamma, histogram, and visibility switch. `Auto` places the low threshold at the dominant background peak and the high threshold at the 99.9th percentile. A bright-background guard keeps BF-like channels on the percentile fallback instead of collapsing the range around white. Shift-drag moves all windows together.

Display defaults already stored in an existing cache stay unchanged until you press `Auto` or rebuild that cache.

Display changes affect viewer tiles and rendered PNG or JPEG files. They do not alter raw ND2 or TIFF values.

RGB slides use their native color channels and hide the fluorescence panel.

The chrome follows the slide. A brightfield color slide such as an SVS opens in light appearance and a fluorescence ND2 opens in dark. The appearance button overrides either, and the override lasts while that tab stays in front.

### Measure and annotate

![Measurements, pins, and boxes](docs/annotations.png)

| Key | Action |
|---|---|
| `M` | measure |
| `P` | place a pin |
| `B` | draw a box |
| `R` | select a region |
| `V` | move the selected region |
| `0` | fit the slide |
| `Esc` | cancel the active tool |
| `I` | show or hide the Pixel Inspector HUD |
| `⌘1` | show or hide Channels and LUTs |
| `⌘2` | show or hide Region |
| `⌘3` | show or hide Annotate |
| `⌘I` | show or hide Slide Info |
| `⌘⇧E` | export annotations as GeoJSON |
| `⌘\` | start or stop side-by-side Compare |
| `L` | link or unlink compared views |

Measurements use the calibration stored in the source. Without calibration, the viewer reports pixels.

Annotations use level-0 pixel coordinates. Their sidecar records the source name, dimensions, selected plane, and calibration state. A mismatched sidecar is not applied silently.

Pins, rulers, and boxes can also be exported as a GeoJSON FeatureCollection for import into QuPath. The coordinates remain level-0 image pixels.

### Inspect a slide

Move over the image to see raw channel values in the Pixel status cell. Press `I` for a cursor-following HUD. The probe reports native values for a complete slide; if only a reduced cached overview survives, it says that the value is an overview mean.

Open Slide Info with `⌘I` or the info button. It shows provenance, calibration source, objective, selected plane, pyramid levels, and both logical and allocated cache size. For Aperio SVS files it also shows whichever thumbnail, label, and macro images actually exist. Copy and Finder reveal actions operate only on the currently registered slide paths.

### Compare serial sections

Open at least two slides, then press `⌘\` or click the Personal Hotspot-style link button. The active slide stays as the reference. With two slides, Compare links the only available partner immediately; with three or more, it asks which other open slide to link. Neither slide is reloaded.

When both slides are calibrated, linked navigation maps the center and field of view in micrometers. Otherwise it falls back to relative image coordinates. Press `L` or click the link control to unlink, align either view manually, then relink; the viewer captures that translation as the approximate alignment. Reset removes the manual offset.

In this acquisition workflow, SVS and ND2 coordinates run in opposite horizontal and vertical directions. A mixed SVS–ND2 pair therefore starts with a 180° orientation automatically: the ND2 pane is visually rotated while linked navigation maps both reversed axes.

The **180°** and **Mirror** controls are independent and preserve the current center. Mirror reverses the moving slide left-to-right; using it together with 180° gives the corresponding top-to-bottom reflection. Reset restores the format-based orientation, removes Mirror, and clears the manual offset.

The rotation and reflection are display-only. Pixel readouts, regions, annotations, GeoJSON, and all exported pixels remain in the source slide's native coordinate system.

This is intentionally a coarse navigation aid for serial sections. It does not claim cell-level registration, infer orientation from tissue appearance, compensate arbitrary rotation or deformation, or imply that cells in sections separated in depth are the same cells.

### Export a region

![A selected export region](docs/region-export.png)

Draw a box or enter its size in pixels or micrometers.

| Format | Output |
|---|---|
| ND2 | raw selected channels, `uint8` or `uint16`, tiled write |
| TIFF | raw values and source dtype; tiled BigTIFF |
| PNG | current rendered display |
| JPEG | current rendered display |

ND2 keeps channel names, colors, objective magnification when known, and isotropic calibration when the writer can represent it safely. TIFF stores X and Y resolution separately.

PNG and JPEG are limited by the render-memory cap. Large requests use a coarser pyramid level rather than claiming an unsafe native-size render.

A native ND2 crop does not need a pyramid.

```bash
nd2wsi crop slide.nd2 roi.nd2 \
  --x 12000 --y 8000 --w 4096 --h 4096 --c 0,2
```

## Support matrix

| Input or operation | Behavior |
|---|---|
| modern uncompressed stitched ND2 | source-backed native level plus compact overview cache |
| modern compressed ND2 | full conversion; whole-frame decode may use substantial RAM |
| legacy JPEG 2000 ND2 | full conversion with the `legacy` extra |
| one selected T/P/Z plane | supported |
| Z maximum projection | supported through full conversion |
| interactive T/Z navigation | not implemented |
| tiled pyramidal SVS | direct viewing |
| untiled or sparse SVS | full-conversion fallback |
| ND2 export | `uint8` and `uint16` |
| anisotropic ND2 calibration | exported uncalibrated unless the writer can represent it safely |

## Measured trade-off

Rebuilding this project's own working set measured the compact design at
scale. Thirty nine stitched ND2 scans totalling 190 GB, the largest a
15.3 GB four channel 68,938 × 27,744 px acquisition, were cached twice on
a USB exFAT SSD, once as full pyramids and once as compact caches.

| Cache mode | Total on disk |
|---|---:|
| full pyramids | 152.1 GB |
| compact source-backed caches | 36.1 GB |

The reduction was 76 percent, close to the 75 percent the pixel
arithmetic predicts. A cold 512 px native window from the largest slide
read through its memory map in 170 ms, and cached overview tiles render
at interactive speed. These numbers describe one machine and one working
set rather than a hardware independent guarantee.

## Architecture

```text
ND2 ── native level 0 ───────────────┐
  └── compact cached levels 1..n ───┤
                                     ├── local tile server ── OpenSeadragon
SVS ── embedded pyramid ─────────────┤
                                     │
portable OME-Zarr ── full pyramid ───┘

selected region ── raw tiles ── TIFF or ND2
```

The main modules have narrow jobs.

- `reader.py` opens one ND2 plane and checks compact-cache eligibility.
- `convert.py` builds full or compact pyramids.
- `storage/` owns the physical Zarr layout.
- `direct.py` adapts ND2 and SVS sources to the same level interface.
- `server.py` serves tiles, metadata, annotations, and exports on localhost.
- `render.py` applies display windows and writes TIFF or rendered images.
- `export_nd2.py` writes tiled ND2 regions through `limnd2`.

The server binds to loopback and places every route behind a random capability URL.

## Commands

```text
nd2wsi info FILE
nd2wsi view FILE [FILE ...]
nd2wsi convert FILE [OUTPUT.ome.zarr]
nd2wsi crop INPUT.nd2 OUTPUT.nd2 --x X --y Y --w W --h H [--c 0,2]
nd2wsi serve STORE [STORE ...]
nd2wsi tidy FOLDER [FOLDER ...]
```

Run `nd2wsi COMMAND --help` for T/P/Z selection, tile size, worker count, host, port, and render limits.

## Example data

Two small Nikon Ti2 acquisitions are published in the immutable `testdata-v1` release.

- `example_cell.nd2` with CY3/FITC/DAPI, 2,185 × 2,247, `uint16`
- `example_tissue.nd2` with CY5/DAPI, 3,650 × 3,406, `uint16`

Download and verify them.

```bash
python scripts/fetch_testdata.py
nd2wsi view docs/example_cell.nd2 docs/example_tissue.nd2
```

See `TESTDATA_LICENSE.md` for provenance and terms.

## Development

```bash
uv sync --all-extras
uv run ruff check nd2wsi tests
uv run pytest -q -m "not realdata"

python scripts/fetch_testdata.py
uv run pytest -q -m realdata
```

CI tests Python 3.11 through 3.13, runs Ruff, and builds and installs the wheel.

Build the macOS app this way.

```bash
ND2WSI_SMOKE_FILE=docs/example_cell.nd2 \
  packaging/build_mac_app.sh dist
```

The script creates an app and a DMG, then opens the example, fetches a tile, exports an ND2 region, and reopens that export.

A frictionless public binary also needs Developer ID signing, hardened runtime, notarization, and stapling.

## Prior work

This project builds on the following.

- [`tlambert03/nd2`](https://github.com/tlambert03/nd2) for ND2 reading and metadata
- [`Laboratory-Imaging/limnd2`](https://github.com/Laboratory-Imaging/limnd2) for ND2 writing
- [`girder/large_image`](https://github.com/girder/large_image) as the direct ND2 tile-source reference
- [OME-NGFF](https://ngff.openmicroscopy.org/) for multiscale metadata
- [OpenSeadragon](https://openseadragon.github.io/) for deep zoom
- [tifffile](https://github.com/cgohlke/tifffile) for TIFF and SVS access

`large-image-source-nd2` shows that ND2 can act as an on-demand tile source. `nd2wsi-viewer` makes a different trade. It reads native pixels from the ND2 but persists reduced box-mean levels for stable whole-slide navigation.

## License

MIT. The example acquisitions are CC0, described in `TESTDATA_LICENSE.md`. OpenSeadragon is included under its BSD-3-Clause license.
