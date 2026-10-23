# Development

[Back to the README](../README.md) · [Application architecture](architecture.md) · [Release procedure](releasing.md)

## Install from source

Python 3.11 or newer.

```bash
python -m pip install ".[app,svs]"
```

ND2 export needs Laboratory Imaging's package index.

```bash
python -m pip install ".[nd2export]" \
  --extra-index-url https://pypi.laboratory-imaging.com/simple
```

## Command line

```text
nd2wsi info FILE
nd2wsi view FILE [FILE ...]
nd2wsi convert FILE [OUTPUT.ome.zarr]
nd2wsi crop INPUT.nd2 OUTPUT.nd2 --x X --y Y --w W --h H [--c 0,2]
nd2wsi serve STORE [STORE ...]
nd2wsi tidy FOLDER [FOLDER ...]
```

`view` opens the browser UI on a local port. `convert` writes a portable OME-Zarr pyramid with every level. `crop` cuts a native ND2 region with no pyramid at all.

## How it works

An eligible ND2, meaning a modern uncompressed file with one stored T/P/Z plane, gets a compact cache that holds only the reduced levels, while level 0 is the ND2 itself read through its memory map. Compressed and legacy files fall back to a full pyramid. A tiled SVS is served from its own embedded pyramid. An ND2 of camera fields over time, sites, or z planes opens in plate mode, where each frame is read from the file with one sequential read and its reduced copies live in a small zarr store beside the file, one chunk per time point and plane, warmed into memory once it is full. A small server on loopback, behind a random capability URL, hands tiles to OpenSeadragon. Regions are read from the raw tiles for TIFF and ND2 export and rendered for PNG and JPEG.

## Development

See [application boundaries and regression coverage](architecture.md) for
the desktop lifecycle, HTTP/export services and viewer controllers.

```bash
uv sync --all-extras
uv run ruff check nd2wsi tests
uv run pytest -q -m "not realdata"

python scripts/fetch_testdata.py
uv run pytest -q -m realdata

ND2WSI_SMOKE_FILE=docs/example_cell.nd2 packaging/build_mac_app.sh dist
```

CI tests Python 3.11 through 3.13, runs Ruff, builds and installs the wheel, and runs the real-data suite on every tag.

Alignment/Compare now keeps landmark drafts separate from committed geometry, requires four valid corresponding points before Done, and scopes spatial commands and remembered fits to the current pane/source/site. Clear Points, Reset Orientation, and Remove Fit have separate meanings. Physical links that cannot be represented by the renderer are blocked; Relative mapping is an explicit display-only choice. ROI previews show the final source-aligned export rectangle, and the scalebar accounts for display rotation and X/Y calibration. See [v1.2.6 validation](v1.2.6-validation.md) for coverage and limitations.

## Prior work

- [`tlambert03/nd2`](https://github.com/tlambert03/nd2) for ND2 reading and metadata
- [`Laboratory-Imaging/limnd2`](https://github.com/Laboratory-Imaging/limnd2) for ND2 writing
- [`girder/large_image`](https://github.com/girder/large_image) as the direct ND2 tile-source reference
- [OME-NGFF](https://ngff.openmicroscopy.org/) for multiscale metadata
- [OpenSeadragon](https://openseadragon.github.io/) for deep zoom
- [tifffile](https://github.com/cgohlke/tifffile) for TIFF and SVS access

