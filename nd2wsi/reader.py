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


class _OpaqueView:
    """An ndarray face whose dask token never touches the pixels.

    dask 2026 tokenizes captured arrays by content even under
    ``name=False``, which for a memory-mapped stitched slide means hashing
    the whole multi-gigabyte file through the page cache before a single
    task runs — minutes on a USB disk, observed at 376 s for one 14 GB
    scan. Handing dask this wrapper instead makes the token a constant and
    leaves the pixels untouched until a chunk is actually computed.
    """

    def __init__(self, a: Any):
        self._a = a
        self.shape = a.shape
        self.dtype = a.dtype
        self.ndim = a.ndim

    def __getitem__(self, idx: Any) -> Any:
        return self._a[idx]

    def __dask_tokenize__(self) -> Any:
        return ("nd2wsi-opaque", id(self._a), self.shape, str(self.dtype))


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
    flags = None
    structured_ok = True
    try:
        channels = f.metadata.channels
    except Exception:
        # the structured parser choked on this file (seen: a metadata matrix
        # whose Data field is a string); the global block still carries the
        # same calibration fields, so take them from there under the same rule
        structured_ok = False
        channels = None
    if structured_ok:
        try:
            flags = channels[0].volume.axesCalibrated
        except (AttributeError, IndexError, TypeError):
            flags = None  # metadata variant without the flags: fall through
    else:
        vol = _nd2_global_volume(f)
        if vol is not None:
            flags = vol.get("axesCalibrated")
            calib = vol.get("axesCalibration")
            if flags is not None and not (bool(flags[0]) and bool(flags[1])):
                return None, "unknown"
            if (
                isinstance(calib, (list, tuple)) and len(calib) >= 2
                and _finite_positive(calib[0]) and _finite_positive(calib[1])
            ):
                return (float(calib[1]), float(calib[0])), "nd2-volume-calibration"
        return None, "unknown"
    if flags is not None and not (bool(flags[0]) and bool(flags[1])):
        return None, "unknown"
    try:
        vs = f.voxel_size()
    except Exception:
        return None, "unknown"
    if structured_ok and vs is not None and _finite_positive(vs.x) and _finite_positive(vs.y):
        return (float(vs.y), float(vs.x)), "nd2-voxel-size"
    return None, "unknown"


def _nd2_global_volume(f: Any) -> dict | None:
    """The reader's global ``volume`` block, when the structured path is broken."""
    try:
        rdr = getattr(f, "_rdr", None)
        gm = rdr._cached_global_metadata() if rdr is not None else None
        vol = gm.get("volume") if isinstance(gm, dict) else None
        return vol if isinstance(vol, dict) else None
    except Exception:
        return None


def _nd2_raw_planes(f: Any) -> list[tuple[str | None, int | None]]:
    """(name, uiColor) per picture plane from the raw frame metadata."""
    try:
        rdr = getattr(f, "_rdr", None)
        raw = rdr._cached_raw_metadata() if rdr is not None else None
        planes = raw.get("sPicturePlanes") if isinstance(raw, dict) else None
        table = (planes.get("sPlaneNew") or planes.get("sPlane") or {}) if isinstance(planes, dict) else {}
        out = []
        for key in sorted(table, key=lambda k: str(k)):
            v = table[key]
            if isinstance(v, dict):
                out.append((v.get("sDescription"), v.get("uiColor")))
        return out
    except Exception:
        return []


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
    raw_planes: list[tuple[str | None, int | None]] = []
    try:
        meta_channels = list(getattr(f.metadata, "channels", None) or [])
    except Exception:
        # structured metadata unreadable: the raw picture planes still name
        # and color the channels (uiColor packs 0x00BBGGRR)
        meta_channels = []
        raw_planes = _nd2_raw_planes(f)
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
        elif i < len(raw_planes):
            raw_name, ui_color = raw_planes[i]
            if raw_name:
                name = str(raw_name)
            if isinstance(ui_color, int) and ui_color > 0:
                color = (ui_color & 0xFF, (ui_color >> 8) & 0xFF, (ui_color >> 16) & 0xFF)
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
            planes.append(
                da.from_array(_OpaqueView(cyx), chunks=(1, tile, tile), name=False)
            )
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
        data = da.from_array(_OpaqueView(cyx), chunks=(1, tile, tile), name=False)
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


def resolve_z(selection: PlaneSelection, n_z: int) -> int:
    """The single Z index a selection names (``"max"`` has no single index)."""
    if selection.z == "mid":
        return _mid(n_z)
    if selection.z == "max":
        if n_z > 1:
            raise ValueError("a maximum projection is computed, not stored")
        return 0
    z = int(selection.z)
    if not 0 <= z < n_z:
        raise ValueError(f"--z {z} out of range (Z={n_z})")
    return z


def plane_view(f: Any, selection: PlaneSelection) -> tuple[np.ndarray, bool]:
    """One plane of an open ND2File as a raw ``(C, Y, X)`` numpy view.

    For an uncompressed modern file this is a zero-copy strided view onto
    the file's memory map: slicing it touches only the pages under the
    slice, so it can serve as a pyramid's level 0 with nothing on disk.
    The view is only valid while ``f`` is open. Returns ``(view, rgb)``.
    """
    sizes = dict(f.sizes)
    coord_axes = [a for a in sizes if a not in FRAME_AXES]
    z_index = resolve_z(selection, sizes.get("Z", 1))
    seq = _seq_index(
        sizes, coord_axes, {"T": selection.t, "P": selection.p, "Z": z_index}
    )
    return _frame_to_cyx(f.read_frame(seq), sizes)


def _mmap_backed(a: np.ndarray) -> bool:
    """True when the array is a non-owning view whose buffer is a memory map."""
    import mmap as _mmap

    if a.flags.owndata:
        return False
    base = a
    while getattr(base, "base", None) is not None:
        base = base.base
    return isinstance(base, _mmap.mmap)


def is_source_backable(f: Any, selection: PlaneSelection) -> tuple[bool, str]:
    """Can this plane be served straight from the file at runtime?

    Every condition is checked against the open file itself, not inferred
    from metadata alone: the deciding test is that ``read_frame`` really
    hands back a non-owning view onto the memory map.
    """
    if getattr(f, "is_legacy", False):
        return False, "legacy ND2 format is read through a decoder, not a map"
    if (getattr(f.attributes, "compressionType", None) or "none") != "none":
        return False, "compressed frames must be inflated; no random access"
    sizes = dict(f.sizes)
    if selection.z == "max" and sizes.get("Z", 1) > 1:
        return False, "a maximum projection is computed, not stored in the file"
    try:
        view, _ = plane_view(f, selection)
    except (ValueError, NotImplementedError) as e:
        return False, str(e)
    if min(view.shape[-2:]) < 2:
        return False, "degenerate plane (an axis of size 1)"
    if not _mmap_backed(view):
        return False, "frames come back as copies, not memory-mapped views"
    return True, ""


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


def nice_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def parse_z(value: str) -> str | int:
    if value in ("mid", "max"):
        return value
    try:
        return int(value)
    except ValueError as e:
        raise ValueError("--z must be 'mid', 'max' or an integer") from e
