"""nd2wsi: Nikon ND2 -> OME-Zarr pyramid -> browser WSI-style viewer."""

import importlib.metadata as _md

try:
    __version__ = _md.version("nd2wsi-viewer")
except _md.PackageNotFoundError:  # running from a checkout without install
    __version__ = "0.0.0+source"
