"""ND2 plane access.

Turns one (T, P, Z) selection of an ND2 file into a lazily readable
``dask.array`` shaped ``(C, Y, X)`` with small spatial chunks.

Why this module exists
----------------------
A stitched "large image" produced by NIS-Elements is stored inside the ND2
container as a *single* ``ImageDataSeq|N!`` chunk: one flattened raster per
frame, however large.  ``nd2.ND2File.to_dask()`` therefore produces one dask
chunk per frame -- for a slide scan that chunk is the whole slide, which
defeats lazy loading.

For *uncompressed* modern ND2 files, however, ``nd2`` returns frames as
zero-copy strided views onto a memory map.  Slicing such a view only touches
the pages that intersect the slice, so wrapping the view in a dask array with
small ``(1, tile, tile)`` chunks gives genuinely memory-bounded random access
into the flattened raster.  That is what :func:`open_plane` does.

For zlib-compressed ND2 files random access inside a frame is impossible
(the whole frame must be inflated), so we fall back to a single in-memory
read with a loud warning.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np

FRAME_AXES = {"C", "Y", "X", "S"}


@dataclass
class ChannelInfo:
    name: str
    color: tuple[int, int, int]  # 0-255 RGB


@dataclass
class PlaneSelection:
    """Which single 2D (multi-channel) plane of the ND2 to use."""

    t: int = 0
    p: int = 0
    z: str | int = "mid"  # "mid", "max" (projection) or an integer index

    def describe(self) -> dict[str, Any]:
        return {"t": self.t, "p": self.p, "z": self.z}


@dataclass
class PlaneSource:
    """A ``(C, Y, X)`` dask array plus everything the pyramid needs to know."""

    data: Any  # dask.array.Array, shape (C, Y, X)
    dtype: np.dtype
    shape: tuple[int, int, int]  # (C, Y, X)
    rgb: bool
    channels: list[ChannelInfo]
    pixel_size_um: tuple[float, float] | None  # (y, x); None = uncalibrated
    source_name: str
    selection: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    magnification: float | None = None  # objective magnification, if recorded
    calibration_source: str = "unknown"  # nd2-voxel-size | aperio-mpp | unknown


def _mid(n: int) -> int:
    return n // 2


def _nd2_pixel_size(f: Any) -> tuple[tuple[float, float] | None, str]:
    """The file's own (y, x) pixel size in um, or None when it has none.

    ``voxel_size()`` alone cannot say: the nd2 reader substitutes 1.0 for a
    missing calibration, exactly the fabrication this tool refuses to pass
    on. The per-axis ``axesCalibrated`` flags carry the truth, so when they
    are present they decide; when the metadata lacks them, the voxel size
    is taken at its word.
    """
    try:
        vol = f.metadata.channels[0].volume
        flags = vol.axesCalibrated
        if not (bool(flags[0]) and bool(flags[1])):
            return None, "unknown"
    except (AttributeError, IndexError, TypeError):
        pass  # metadata variant without the flags: fall through
    vs = f.voxel_size()
    if vs is not None and _finite_positive(vs.x) and _finite_positive(vs.y):
        return (float(vs.y), float(vs.x)), "nd2-voxel-size"
    return None, "unknown"


def _finite_positive(v: Any) -> bool:
    try:
        return v is not None and math.isfinite(float(v)) and float(v) > 0
    except (TypeError, ValueError):
        return False


def _channel_infos(f: Any, n_channels: int, rgb: bool) -> list[ChannelInfo]:
    if rgb:
        return [
            ChannelInfo("Red", (255, 0, 0)),
            ChannelInfo("Green", (0, 255, 0)),
            ChannelInfo("Blue", (0, 0, 255)),
        ][:n_channels]
    infos: list[ChannelInfo] = []
    meta_channels = []
    try:
        meta_channels = list(getattr(f.metadata, "channels", None) or [])
    except Exception:  # pragma: no cover - metadata parsing is best effort
        meta_channels = []
    for i in range(n_channels):
        name = f"Channel {i}"
        color = (255, 255, 255)
        if i < len(meta_channels):
            ch = meta_channels[i].channel
            if getattr(ch, "name", None):
                name = ch.name or name
            c = getattr(ch, "color", None)
            if c is not None:
                color = (int(c.r), int(c.g), int(c.b))
                if color == (0, 0, 0):  # avoid invisible channels
                    color = (255, 255, 255)
        infos.append(ChannelInfo(name, color))
    if n_channels == 1:
        infos[0].color = (255, 255, 255)
    return infos


def _seq_index(sizes: dict[str, int], coord_axes: list[str], coords: dict[str, int]) -> int:
    """Row-major frame index for the given loop coordinates.

    ND2 enumerates frames in row-major order over the loop axes in the order
    they appear in ``ND2File.sizes`` (the same convention ``nd2`` itself uses
    for ``_seq_index_from_coords``).
    """
    if not coord_axes:
        return 0
    dims = [sizes[a] for a in coord_axes]
    idx = [coords.get(a, 0) for a in coord_axes]
    for a, i, n in zip(coord_axes, idx, dims):
        if not 0 <= i < n:
            raise ValueError(f"index {i} out of range for axis {a} (size {n})")
    return int(np.ravel_multi_index(idx, dims))


def _frame_to_cyx(frame: np.ndarray, sizes: dict[str, int]) -> tuple[np.ndarray, bool]:
    """Normalize one frame (as returned by ``ND2File.read_frame``) to (C, Y, X).

    ``read_frame`` returns ``(Y, X)``, ``(C, Y, X)``, ``(Y, X, S)`` or
    ``(C, Y, X, S)`` depending on channel / RGB-component counts.  All
    reshaping below is stride manipulation on the (possibly memory-mapped)
    view -- no copies.
    """
    n_c = sizes.get("C", 1)
    n_s = sizes.get("S", 1)
    if n_c > 1 and n_s > 1:
        raise NotImplementedError(
            "files that combine multiple channels with RGB components "
            f"(C={n_c}, S={n_s}) are not supported by this prototype"
        )
    if frame.ndim == 2:  # (Y, X)
        return frame[None], False
    if frame.ndim == 3 and n_s > 1:  # (Y, X, S) -> (S, Y, X)
        return np.moveaxis(frame, -1, 0), True
    if frame.ndim == 3:  # (C, Y, X)
        return frame, False
    raise NotImplementedError(f"unexpected frame shape {frame.shape}")


def open_plane(
    f: Any,
    selection: PlaneSelection,
    tile: int = 512,
) -> PlaneSource:
    """Build a lazily readable (C, Y, X) source for one plane of an open ND2File.

    The returned dask array holds references into ``f``'s memory map: ``f``
    must stay open while the array is being computed.
    """
    import dask.array as da

    sizes = dict(f.sizes)
    coord_axes = [a for a in sizes if a not in FRAME_AXES]
    notes: list[str] = []

    n_t = sizes.get("T", 1)
    n_p = sizes.get("P", 1)
    n_z = sizes.get("Z", 1)

    if selection.t >= n_t:
        raise ValueError(f"--t {selection.t} out of range (T={n_t})")
    if selection.p >= n_p:
        raise ValueError(f"--position {selection.p} out of range (P={n_p})")
    if n_p > 1:
        notes.append(
            f"file has {n_p} positions (unstitched multipoint); "
            f"converting position {selection.p} only"
        )
    if n_t > 1:
        notes.append(f"file has {n_t} timepoints; converting t={selection.t}")

    compressed = (getattr(f.attributes, "compressionType", None) or "none") != "none"
    if compressed:
        notes.append(
            "ND2 frame data is compressed: random access inside a frame is not "
            "possible, whole frames are inflated into RAM during conversion"
        )
        warnings.warn(notes[-1], stacklevel=2)

    def frame_view(z_index: int) -> np.ndarray:
        seq = _seq_index(
            sizes, coord_axes, {"T": selection.t, "P": selection.p, "Z": z_index}
        )
        return f.read_frame(seq)

    if selection.z == "max" and n_z > 1:
        planes = []
        for zi in range(n_z):
            cyx, rgb = _frame_to_cyx(frame_view(zi), sizes)
            planes.append(da.from_array(cyx, chunks=(1, tile, tile), name=False))
        data = da.stack(planes, axis=0).max(axis=0)
        z_used: str | int = "max"
        notes.append(f"Z: maximum-intensity projection over {n_z} planes")
    else:
        if selection.z == "mid":
            z_index = _mid(n_z)
        elif selection.z == "max":  # single plane anyway
            z_index = 0
        else:
            z_index = int(selection.z)
        if not 0 <= z_index < n_z:
            raise ValueError(f"--z {z_index} out of range (Z={n_z})")
        cyx, rgb = _frame_to_cyx(frame_view(z_index), sizes)
        data = da.from_array(cyx, chunks=(1, tile, tile), name=False)
        z_used = z_index
        if n_z > 1:
            notes.append(f"file has {n_z} Z planes; converting z={z_index}")

    _, rgb = _frame_to_cyx(frame_view(0 if z_used == "max" else int(z_used)), sizes)

    pixel_size, cal_source = _nd2_pixel_size(f)
    if pixel_size is None:
        notes.append("no pixel calibration in the file; measurements are in pixels")

    n_channels = int(data.shape[0])
    channels = _channel_infos(f, n_channels, rgb)

    return PlaneSource(
        data=data,
        dtype=np.dtype(f.dtype),
        shape=(n_channels, int(data.shape[1]), int(data.shape[2])),
        rgb=rgb,
        channels=channels,
        pixel_size_um=pixel_size,
        source_name=str(getattr(f, "path", "nd2")),
        selection={"t": selection.t, "p": selection.p, "z": z_used},
        notes=notes,
        magnification=objective_magnification(f),
        calibration_source=cal_source,
    )


def objective_magnification(f: Any) -> float | None:
    try:
        for ch in f.metadata.channels or []:
            mag = getattr(ch.microscope, "objectiveMagnification", None)
            if mag:
                return float(mag)
    except Exception:  # pragma: no cover - metadata parsing is best effort
        pass
    return None


def level_shapes(height: int, width: int, tile: int) -> list[tuple[int, int]]:
    """Halving pyramid level sizes, level 0 first, until one tile covers it."""
    shapes = [(height, width)]
    h, w = height, width
    while max(h, w) > tile:
        h, w = max(1, h // 2), max(1, w // 2)
        shapes.append((h, w))
    return shapes


def num_levels(height: int, width: int, tile: int) -> int:
    return len(level_shapes(height, width, tile))


def nice_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def estimate_pyramid_bytes(c: int, height: int, width: int, itemsize: int) -> int:
    total = 0
    for h, w in level_shapes(height, width, 512):
        total += c * h * w * itemsize
    return int(total)


def parse_z(value: str) -> str | int:
    if value in ("mid", "max"):
        return value
    try:
        return int(value)
    except ValueError as e:
        raise ValueError("--z must be 'mid', 'max' or an integer") from e


def ceil_div(a: int, b: int) -> int:
    return int(math.ceil(a / b))
