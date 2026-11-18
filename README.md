<p align="center">
  <img src="docs/icon.png" alt="" width="88">
</p>

<h1 align="center">nd2wsi-viewer</h1>

<p align="center">
  <strong>Browse whole slides and confocal stacks. Inspect channels. Export native regions.</strong><br>
  A local microscopy viewer for macOS and Windows.<br>
  <strong>Supported formats:</strong> ND2 · CZI · LIF · OME-TIFF / TIFF · SVS · <a href="docs/sdpc-support.md">SDPC</a> · ND2SVS · JPG · PNG
</p>

<p align="center">
  <a href="#download">Download</a> ·
  <a href="#features">Features</a> ·
  <a href="docs/user-guide.md">User guide</a> ·
  <a href="https://github.com/myunghyunj/nd2wsi-viewer/releases">Release notes</a> ·
  <a href="docs/user-guide.md#numbered-regions-with-astromapper">Astromap</a>
</p>

## Recent updates

- **OME-TIFF / TIFF, multi-series scenes & workflow upgrades:** open pyramidal `.ome.tif` / `.tif` slides directly and multi-plane stacks interactively, switch heterogeneous `.lif` / `.czi` series from the toolbar, customize channel colours or `Option/Alt`-click to solo a channel, undo/redo annotation edits (`⌘Z` / `⌘⇧Z`), reopen recent slides from the start screen, and export OME-XML `.tif` regions.
- **Astromap TXT import recovery (`2.6.4`):** review skipped rows, import valid regions, and optionally restore zero-area legacy rectangles to one pixel without changing the source TXT. [2.6.4 release notes](docs/release-notes-2.6.4.md).
- **Path safety, nested LIF mosaics & 16-bit SVS (`2.6.3`):** fix `#` / `?` path truncation during cache creation and conversion, stitch hierarchical Leica `.lif` tile scans, preserve `uint16` precision across 16-bit RGB `.svs` viewing and TIFF export, scope CZI `--t` to the selected scene, and write single-file `.nd2svs` pyramids from `nd2wsi convert`. [2.6.3 release notes](docs/release-notes-2.6.3.md).
- **Interactive CZI & LIF Z-stacks (`2.6.1–2.6.2`):** scrub physical `µm` Z planes, run sharpness autofocus (`F`), play time series, browse LambdaScan and uniform multi-series `.lif` site grids, decode mixed-depth channels accurately, and isolate annotations per `(T, P, Z)` plane. [2.6.1](docs/release-notes-2.6.1.md) · [2.6.2](docs/release-notes-2.6.2.md).
- **Zeiss CZI & Leica LIF support (`2.6.0`):** open 2D scans, stitched mosaics, confocal Z-stacks, and LIGHTNING deconvolved `.czi` and `.lif` acquisitions with `.nd2svs` pyramid caching, physical calibration, fluorophore colours, and native-value ROI export. [2.6.0 release notes](docs/release-notes-2.6.0.md).

<p align="center">
  <img src="docs/overview-sdpc.png" alt="Brightfield SVS slide with Slide Info in the light appearance" width="50%"><img src="docs/overview-nd2.png" alt="Fluorescence ND2 image with Channels &amp; LUTs in the dark appearance" width="50%">
</p>

## Download

| Platform | Download · 2.6.4 | Requirements |
| --- | --- | --- |
| macOS | [DMG](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.4/nd2wsi-viewer-2.6.4-macos-arm64.dmg) | Apple silicon · macOS 14 or later |
| Windows | [Portable ZIP](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.6.4/nd2wsi-viewer-2.6.4-windows-x64.zip) | Windows 11 (x64, or ARM64 via x64 emulation) · WebView2 |

On macOS, drag the app into **Applications**; on first launch, choose **System Settings → Privacy & Security → Open Anyway** once (ad-hoc signed; signed Sparkle updates run from **nd2wsi-viewer → Check for Updates…** thereafter). On Windows, extract the entire ZIP, keep `_internal` beside `nd2wsi-viewer.exe`, and allow the SmartScreen unsigned-publisher prompt if shown (verify against the accompanying `.sha256` checksum). See the [installation guide](docs/user-guide.md#get-started).

Click the start screen, drop a slide into the window, use **+**, or pick from **Recent**. Tiled **SVS**, **SDPC**, pyramidal **OME-TIFF / TIFF**, **ND2SVS**, **JPG**, and **PNG** files open directly; stitched **ND2**, **CZI**, and **LIF** scans prepare a reusable `.nd2svs` viewing cache on first open.

Try the public CC0 examples:
[three-channel cells, 28 MB](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/testdata-v1/example_cell.nd2) ·
[two-channel tissue, 47 MB](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/testdata-v1/example_tissue.nd2).

## Features

### Channels & LUTs

![Channels & LUTs with separate FITC, CY3, DAPI and BF histograms](docs/channels-luts.png)

- Set each channel's visibility, intensity range, gamma, and colour (click a colour swatch for presets or **Reset to metadata**; **Option / Alt-click** a channel toggle to solo it).
- Drag the histogram controls (**Shift-drag** moves all channels together), enter exact values, or use **Auto** for contrast.
- **Copy / Paste** transfers minimum, maximum and gamma between matching channel names.
- Press **`I`** for a live cursor readout of raw source pixel values, or **`⌘I`** (`Ctrl+I`) for **Slide Info** (calibration provenance, dimensions, and embedded SVS/SDPC label and macro photos).
- Each file's last-used minimum, maximum, gamma and channel colours are restored automatically when you reopen it, including after restarting the app.

**Auto-fit** fits the histogram axis to the signal; it does not change contrast.
These display controls leave source pixels unchanged.

### Z-stacks, time series and plates

Scrub **Z planes** in micrometres, jump to the sharpest plane with **Autofocus (`F`)**, and play or swipe through **time series** across **ND2**, **CZI**, **LIF**, and multi-plane **OME-TIFF** stacks (including **LambdaScan** spectral series). Multi-well **ND2** plates and uniform 2D multi-series **LIF** files open in a site grid; heterogeneous multi-series **LIF** and multi-scene **CZI** containers provide a toolbar **Series** picker (`Series 1 / N`) to switch scenes directly (or convert an individual scene via `nd2wsi convert --position N`). [Navigation guide](docs/user-guide.md#time-series-of-a-plate).

### Annotations & Astromap

Add calibrated rulers (`M`), pins (`P`) and boxes (`B`) with notes, saved automatically beside the slide with 50-step undo/redo (**`⌘Z` / `⌘⇧Z`**, or **`Ctrl+Z` / `Ctrl+Y`**; press **`?`** for shortcuts). Export marks with **Export GeoJSON** (`⌘⇧E`), or use **Import…** (**Open…**) to add QuPath-compatible `.geojson` features in full-resolution pixel coordinates or load native `.annotations.json` files. Open **Astromap** (`⌘⇧A` / `Ctrl+Shift+A`) to manage numbered ROI boxes, step through regions with `←` / `→` at constant zoom, import AstroMapper `.txt` coordinates with skipped-row review and optional `1 px` recovery, and export **JPG + TXT** or landscape **PDF** ROI reports. [Annotation guide](docs/user-guide.md#measuring-and-marking) · [Astromap guide](docs/user-guide.md#numbered-regions-with-astromapper).

### Export

Select a region (or **Whole Slide** / **Full Field**) from the active 2D plane and export visible source channels as **OME-TIFF** (`.tif`, all bit depths and scales) or native-scale **ND2** (`uint8` / `uint16` non-raster slides), or export the current colours and contrast as **PNG / JPEG**. Add a calibrated **Scale bar + JPEG** or an editable vector **Scale bar + SVG** on calibrated slides. Native-resolution data exports preserve source channel values (or decoded RGB for colour slides); reduced-scale exports resample them. [Export options](docs/user-guide.md#cutting-out-a-region).

### Link and compare

![Linked immunofluorescence, Masson's trichrome and H&E serial sections](docs/compare-serial-sections-linked.png)

Compare up to four images at the same physical scale (`⌘\`). Reflect opposite-side glass mounts with **Flip ↔**, **Flip ↕**, or **Transpose**, and match recognizable tissue features with **Align** to calibrate orientation and manually align serial sections. With **Link scale + frame**, hold **Shift** (or **Option / Alt**) while dragging, or press the arrow keys, to adjust one view; release to resume linked movement with the new offset. This supports visual comparison of serial sections; it does not establish cell-to-cell correspondence.
[Comparison controls](docs/user-guide.md#comparing-scans-and-serial-sections).

### Translucent viewer

![Floating fluorescence reference with opacity and navigation controls](docs/translucent-viewer.png)

Click the **cloud** in the top toolbar to float the current fluorescence slide or **JPG / PNG** image above other apps. Adjust its opacity, navigate Astromap or imported **JPG + TXT** ROIs, or lock the view while you work underneath it.

### Shading correction

![Two fluorescence scans before and after shading correction](docs/shading-correction-current.png)

*FITC, CY3 and DAPI composites. Each pair uses the same display settings before
and after correction; DAPI is unchanged.*

Reduce grid-like shadows from uneven illumination on single-frame stitched **ND2** scans by learning the repeating tile pattern and adapting its strength across the image. Supported **FITC** and **CY3** channels are corrected from the slide's fluorescence while **BF** and **DAPI** remain unchanged. The corrected viewing cache can be reverted anytime with **Revoke**, and raw **ND2 / TIFF** exports and pixel readouts always retain original source values.
[Method and validation limits](docs/shading-correction-validation.md).

## Your files stay local

No upload or account is required, and original images are not modified. Viewing caches and annotation sidecars live in an `nd2wsi` folder beside the image (**move that folder with the source image** to keep them together; a compact `.nd2svs` viewing cache still needs its source file for native level-0 pixels and editing, whereas `nd2wsi convert` pyramids are self-contained). Saved per-file LUTs, recent-file history, and UI preferences stay in local application data.

[Storage and portability](docs/user-guide.md#what-the-viewer-writes-on-your-disk) ·
[Plate, time and Z navigation](docs/user-guide.md#time-series-of-a-plate) ·
[Source installation and development](docs/development.md)

Built by Myunghyun Jeong for day-to-day microscopy work.
[Report an issue](https://github.com/myunghyunj/nd2wsi-viewer/issues) with the app
version, platform and file type.

## License

[MIT](LICENSE). The example acquisitions are CC0; see [their license](TESTDATA_LICENSE.md).
Bundled third-party software notices (including OpenSeadragon under BSD-3-Clause and the LGPL SDPC HEVC decoder accompanying binary releases) are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
