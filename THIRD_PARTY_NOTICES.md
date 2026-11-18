# Third-Party Notices

`nd2wsi-viewer` is licensed under the [MIT License](LICENSE). This file records
third-party software bundled in the source repository or distributed alongside
platform binary releases.

## Bundled in Source Repository

### OpenSeadragon 5.0.1

- **Location:** `nd2wsi/static/vendor/openseadragon/openseadragon.min.js`
- **License:** BSD-3-Clause (`nd2wsi/static/vendor/openseadragon/LICENSE.txt`)
- **Copyright:** Copyright (C) 2009 CodePlex Foundation; Copyright (C) 2010–2024 OpenSeadragon contributors
- **Project:** <https://openseadragon.github.io/>

### Icons8 Windows 10 Icons

- **Location:** `nd2wsi/static/vendor/icons8-win10/`
- **License:** MIT (`nd2wsi/static/vendor/icons8-win10/LICENSE.md`)
- **Copyright:** Copyright (c) 2025 Icons8
- **Project:** <https://github.com/icons8/windows-10-icons>

## Included in Platform Binary Releases

### SDPC HEVC Decoder (FFmpeg & PyAV)

Binary releases on macOS and Windows include a source-built HEVC decoder for
SQRAY `.sdpc` whole-slide images:

- **FFmpeg:** GNU Lesser General Public License v2.1 or later (`LGPL-2.1-or-later`), dynamically linked with GPL, nonfree, and version-3-only components disabled.
- **PyAV (`av==16.0.1`):** BSD-3-Clause.
- **Notices & Corresponding Source:** See [`packaging/SDPC-CODEC-NOTICE.txt`](packaging/SDPC-CODEC-NOTICE.txt) and [`docs/sdpc-support.md`](docs/sdpc-support.md). Exact corresponding source archives (`sdpc-codec-sources-macos-arm64.zip` and `sdpc-codec-sources-windows-x64.zip`), build/verification scripts, and license texts accompany every GitHub release and packaged binary (`nd2wsi-viewer-about/`).

### Sparkle (macOS App Bundle)

- **License:** MIT
- **Project:** <https://sparkle-project.org/>
- **Scope:** Signed in-app update framework bundled in the macOS DMG application.

### Python Runtime & Locked Dependencies

Standalone macOS and Windows bundles ship CPython (`PSF-2.0`) and locked Python
packages declared in [`pyproject.toml`](pyproject.toml) and `uv.lock`. Full
per-package license texts and build provenance manifests are included inside each
packaged distribution under `nd2wsi-viewer-about/licenses/`.
