"""Native OME-TIFF, TIFF, ImageJ hyperstack, and QPTIFF input.

Serves 2D tiled pyramidal TIFFs, exported ROI TIFFs, and untiled 2D TIFFs
directly with zero disk cache, and opens bounded multi-plane (Z/T) stacks
interactively with Z-slider, autofocus, and timeline support.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from .direct_common import _TILE_CACHE, _Lifecycle, _Root
from .ometiff_levels import (  # noqa: F401
    _ASSOCIATED_NAMES,
    _LAZY_STREAM_BAND_ROWS,
    _MAX_CACHED_STRIP_BYTES,
    _MAX_SEGMENT_BYTES,
    _MAX_STACK_FRAME_BYTES,
    _MEMO_BAND_ROWS,
    UNTILED_EAGER_MAX_BYTES,
    UNTILED_MAX_PIXELS,
    _box_mean_axes,
    _build_direct_plane_attrs,
    _build_tiled_pyramid_levels,
    _can_lazy_stream_untiled,
    _checked_read_segment,
    _CoarseMemoLevel,
    _compute_default_windows,
    _contig_tile_reader,
    _decode_page_segment,
    _discover_embedded_tiled_levels,
    _expand_multipoint_ome_pages,
    _extract_raw_bps,
    _group_logical_series,
    _is_associated_series,
    _LazyUntiledLevel,
    _MemoryPyramidLevel,
    _OmeTiledLevel,
    _page_array_to_cyx,
    _page_kf,
    _planar_tile_reader,
    _read_untiled_page_rows,
    _series_2d_to_cyx,
    _SyntheticPageSeries,
    _tiled_level_reader_for,
    _untiled_window_reader_for,
)
from .ometiff_levels import (
    _resolve_untiled_eager_max_bytes as _levels_resolve_untiled_eager_max_bytes,
)
from .ometiff_metadata import (  # noqa: F401
    _DTD_TEXT_RE,
    _FLUOROPHORE_PALETTE,
    _MAX_XML_BYTES,
    OMETIFF_SUFFIXES,
    _apply_col_override,
    _build_channels,
    _clean_imagej_label,
    _extract_imagej_calibration,
    _extract_imagej_channels,
    _extract_tiff_tag_calibration,
    _local_tag,
    _normalize_dtype,
    _objective_mag,
    _parse_ome_color,
    _parse_ome_xml,
    _parse_qpi_page_meta,
    _parse_series_axes,
    _ParsedSeriesLayout,
    _pixels_uses_rgb24_colors,
    _resolve_metadata_for_series,
    _tag_rational_float,
    _unit_to_um,
    is_ometiff,
)
from .ometiff_stack import (  # noqa: F401
    _STACK_FRAME_CACHE,
    _STACK_REDUCED_CACHE,
    HISTOGRAM_LRU,
    PLATE_K,
    PREFETCH_PENDING_MAX,
    STACK_MAX_DIM,
    STACK_MAX_FRAME_PX,
    STACK_MIN_DIM,
    THUMB_K,
    Z_PREFETCH_PENDING_MAX,
    ContainerSceneStackSource,
    _BaseInteractiveStackSource,
    _sharpness_score,
    _StackArrayLevel,
    _StackReducedLevel,
)
from .platform_io import BINARY, total_physical_memory_bytes
from .reader import level_shapes

__all__ = [
    "HISTOGRAM_LRU",
    "OMETIFF_SUFFIXES",
    "PLATE_K",
    "PREFETCH_PENDING_MAX",
    "STACK_MAX_DIM",
    "STACK_MAX_FRAME_PX",
    "STACK_MIN_DIM",
    "THUMB_K",
    "UNTILED_EAGER_MAX_BYTES",
    "UNTILED_MAX_PIXELS",
    "Z_PREFETCH_PENDING_MAX",
    "ContainerSceneStackSource",
    "OmeTiffStackSource",
    "collect_ometiff_info",
    "inspect_ometiff_series",
    "is_ometiff",
    "is_ometiff_stack",
    "open_direct",
]


def _resolve_untiled_eager_max_bytes(eager_max_bytes: int | None = None) -> int:
    return _levels_resolve_untiled_eager_max_bytes(
        eager_max_bytes, default_max_bytes=UNTILED_EAGER_MAX_BYTES
    )


def inspect_ometiff_series(path: str | Path) -> list[dict[str, Any]]:
    """Inspect series/scenes in a TIFF/OME-TIFF container without decoding pixels."""
    import tifffile

    path = Path(path)
    is_qp = path.suffix.lower() == ".qptiff"
    with tifffile.TiffFile(str(path)) as tf:
        ome_images = _parse_ome_xml(getattr(tf, "ome_metadata", None))
        grouped = _group_logical_series(tf, ome_images, is_qptiff=is_qp)
        out: list[dict[str, Any]] = []
        for i, (s, _lvs, ome_rec) in enumerate(grouped):
            try:
                layout = _parse_series_axes(s, ome_rec, is_qptiff_container=is_qp)
            except NotImplementedError:
                continue
            name = (
                (ome_rec or {}).get("name")
                or str(getattr(s, "name", "") or "").strip()
                or f"Series {i + 1}"
            )
            out.append(
                {
                    "index": i,
                    "name": name,
                    "width": int(layout.width),
                    "height": int(layout.height),
                    "channels": int(layout.n_c),
                    "zCount": int(layout.n_z),
                    "tCount": int(layout.n_t),
                }
            )
        return out


def is_ometiff_stack(path: str | Path, *, series: int = 0) -> bool:
    """True when the selected TIFF/OME-TIFF series is an interactive Z/T stack."""
    import tifffile

    try:
        path = Path(path)
        if not is_ometiff(path) or not path.is_file():
            return False
        is_qp = path.suffix.lower() == ".qptiff"
        with tifffile.TiffFile(str(path)) as tf:
            ome_images = _parse_ome_xml(getattr(tf, "ome_metadata", None))
            grouped = _group_logical_series(tf, ome_images, is_qptiff=is_qp)
            if not (0 <= series < len(grouped)):
                return False
            s, _lvs, ome_rec = grouped[series]
            layout = _parse_series_axes(s, ome_rec, is_qptiff_container=is_qp)
            if layout.n_z <= 1 and layout.n_t <= 1:
                return False
            if not (
                STACK_MIN_DIM <= layout.width <= STACK_MAX_DIM
                and STACK_MIN_DIM <= layout.height <= STACK_MAX_DIM
            ):
                return False
            if layout.width * layout.height > STACK_MAX_FRAME_PX:
                return False
            frame_bytes = layout.width * layout.height * max(1, layout.n_c) * layout.dtype.itemsize
            return 0 < frame_bytes <= _MAX_STACK_FRAME_BYTES
    except Exception:
        return False


def open_direct(
    path: str | Path,
    *,
    series: int = 0,
    tile: int = 512,
    eager_max_bytes: int | None = None,
) -> tuple[_Root, dict[str, Any]]:
    """Open a 2D TIFF / OME-TIFF / QPTIFF directly with zero on-disk cache."""
    import tifffile

    from .convert import available_memory_bytes

    path = Path(path)
    is_qp = path.suffix.lower() == ".qptiff"
    tf = tifffile.TiffFile(str(path))
    tf_closed = False
    fd = -1
    try:
        ome_images = _parse_ome_xml(getattr(tf, "ome_metadata", None))
        grouped = _group_logical_series(tf, ome_images, is_qptiff=is_qp)
        if not (0 <= series < len(grouped)):
            raise ValueError(f"series {series} out of range (0..{len(grouped) - 1})")
        base_series, level_objs, ome_rec = grouped[series]
        layout = _parse_series_axes(base_series, ome_rec, is_qptiff_container=is_qp)

        z_sel = layout.n_z // 2 if layout.n_z > 1 else 0
        t_sel = 0
        pixel_size_um, _z_step_um, cal_source, magnification, channels = _resolve_metadata_for_series(
            tf, base_series, layout, ome_rec, series_index=series
        )

        H, W = layout.height, layout.width
        shapes = level_shapes(H, W, tile)
        life = _Lifecycle()
        page0_kf = _page_kf(base_series.pages[0])
        raw_bps = _extract_raw_bps(page0_kf)

        can_tile_direct = (
            layout.n_z == 1
            and layout.n_t == 1
            and getattr(page0_kf, "is_tiled", False)
        )
        embedded: dict[int, tuple[Any, int, int]] = {}
        owner = f"ometiff:{path.resolve()}:s{series}:{id(life)}"
        if can_tile_direct:
            fd = os.open(str(path), os.O_RDONLY | BINARY)
            ident = path.stat().st_mtime_ns
            embedded = _discover_embedded_tiled_levels(
                level_objs,
                fd,
                _TILE_CACHE,
                owner,
                ident,
                H,
                shapes,
                layout,
                reader_builder=_tiled_level_reader_for,
            )

        if 0 in embedded:
            levels, memo_levels, shapes = _build_tiled_pyramid_levels(
                embedded,
                shapes,
                layout,
                life,
                tiled_level_cls=_OmeTiledLevel,
                coarse_level_cls=_CoarseMemoLevel,
            )
            coarsest = np.asarray(levels[str(len(shapes) - 1)][:, :, :])
            windows = _compute_default_windows(
                coarsest, layout.dtype, layout.rgb, raw_bps=raw_bps
            )
            notes = [
                f"served directly from tiled TIFF ({len(embedded)} embedded level(s), {W} x {H})"
            ]
            if not pixel_size_um:
                notes.append("no physical pixel calibration in TIFF metadata; measurements are in pixels")

            selection_meta: dict[str, Any] = {"p": series} if series > 0 or len(grouped) > 1 else {}
            attrs = _build_direct_plane_attrs(
                layout=layout,
                channels=channels,
                pixel_size_um=pixel_size_um,
                source_name=str(path),
                selection=selection_meta,
                notes=notes,
                magnification=magnification,
                cal_source=cal_source,
                shapes=shapes,
                tile=tile,
                windows=windows,
            )

            def _teardown_tiled() -> None:
                if not life.close():
                    return
                for ml in memo_levels:
                    ml.release()
                tf.close()
                if fd >= 0:
                    os.close(fd)
                _TILE_CACHE.purge(owner)

            return _Root(levels, _teardown_tiled), attrs

        # Untiled 2D TIFF (or mid-plane fallback of an oversized >8192 stack):
        if H * W > UNTILED_MAX_PIXELS:
            raise ValueError(
                f"untiled TIFF ({W} x {H}) exceeds {UNTILED_MAX_PIXELS // 1_000_000} MPx direct memory limit; "
                "save as a tiled TIFF or OME-TIFF pyramid"
            )
        plane_bytes = layout.n_c * H * W * layout.dtype.itemsize
        eager_ceiling = _resolve_untiled_eager_max_bytes(eager_max_bytes)

        selection_meta = {}
        if series > 0 or len(grouped) > 1:
            selection_meta["p"] = series
        if layout.n_z > 1:
            selection_meta["z"] = z_sel
        if layout.n_t > 1:
            selection_meta["t"] = t_sel

        # Large untiled 2D TIFFs (> eager_ceiling, default 64 MiB): stream strips/rows
        # lazily on demand via pread rather than materializing hundreds of MB in RAM,
        # provided compressed strips fit within the strip cache (avoiding repeated
        # full-image decompression on single-strip compressed TIFFs).
        can_lazy_stream = _can_lazy_stream_untiled(
            page0_kf,
            H,
            W,
            layout,
            max_cached_strip_bytes=_MAX_CACHED_STRIP_BYTES,
        )
        if plane_bytes > eager_ceiling and can_lazy_stream:
            if fd < 0:
                fd = os.open(str(path), os.O_RDONLY | BINARY)
            ident = path.stat().st_mtime_ns
            lazy_reader = _untiled_window_reader_for(
                base_series,
                fd,
                _TILE_CACHE,
                (owner, ident, "untiled"),
                layout,
                z_idx=z_sel,
                t_idx=t_sel,
            )
            if lazy_reader is not None:
                lazy_levels: dict[str, _LazyUntiledLevel] = {
                    str(k): _LazyUntiledLevel(
                        lazy_reader, H, W, layout.n_c, sh, sw, 2**k, life, layout.dtype
                    )
                    for k, (sh, sw) in enumerate(shapes)
                }
                coarsest = np.asarray(lazy_levels[str(len(shapes) - 1)][:, :, :])
                windows = _compute_default_windows(
                    coarsest,
                    layout.dtype,
                    layout.rgb,
                    raw_bps=raw_bps,
                )
                notes = [f"served lazily on demand from untiled TIFF ({W} x {H})"]
                if layout.n_z > 1:
                    notes.append(f"series has {layout.n_z} Z planes; showing plane z={z_sel}")
                if not pixel_size_um:
                    notes.append(
                        "no physical pixel calibration in TIFF metadata; measurements are in pixels"
                    )
                attrs = _build_direct_plane_attrs(
                    layout=layout,
                    channels=channels,
                    pixel_size_um=pixel_size_um,
                    source_name=str(path),
                    selection=selection_meta,
                    notes=notes,
                    magnification=magnification,
                    cal_source=cal_source,
                    shapes=shapes,
                    tile=tile,
                    windows=windows,
                )

                def _teardown_lazy() -> None:
                    if not life.close():
                        return
                    for ll in lazy_levels.values():
                        ll.release()
                    tf.close()
                    if fd >= 0:
                        os.close(fd)
                    _TILE_CACHE.purge(owner)

                return _Root(lazy_levels, _teardown_lazy), attrs

        avail = available_memory_bytes()
        budget = (avail if avail is not None else total_physical_memory_bytes()) // 2
        if plane_bytes * 2 > budget:
            raise ValueError(
                "Not enough memory to open this untiled TIFF at full resolution; close other slides and retry"
            )

        base_cyx = _series_2d_to_cyx(base_series, layout, z_idx=z_sel, t_idx=t_sel)
        tf.close()
        tf_closed = True
        if fd >= 0:
            os.close(fd)
            fd = -1

        mem_levels: dict[str, _MemoryPyramidLevel] = {
            str(k): _MemoryPyramidLevel(base_cyx, sh, sw, 2**k, life)
            for k, (sh, sw) in enumerate(shapes)
        }
        del base_cyx  # _MemoryPyramidLevel owns the sole reference; release() frees it
        coarsest = np.asarray(mem_levels[str(len(shapes) - 1)][:, :, :])
        windows = _compute_default_windows(
            coarsest, layout.dtype, layout.rgb, raw_bps=raw_bps
        )
        notes = [f"served directly in memory from TIFF ({W} x {H})"]
        if layout.n_z > 1:
            notes.append(f"series has {layout.n_z} Z planes; showing plane z={z_sel}")
        if not pixel_size_um:
            notes.append("no physical pixel calibration in TIFF metadata; measurements are in pixels")

        attrs = _build_direct_plane_attrs(
            layout=layout,
            channels=channels,
            pixel_size_um=pixel_size_um,
            source_name=str(path),
            selection=selection_meta,
            notes=notes,
            magnification=magnification,
            cal_source=cal_source,
            shapes=shapes,
            tile=tile,
            windows=windows,
        )

        def _teardown_mem() -> None:
            if life.close():
                for ml in mem_levels.values():
                    ml.release()

        return _Root(mem_levels, _teardown_mem), attrs
    except BaseException:
        if not tf_closed:
            tf.close()
        if fd >= 0:
            os.close(fd)
        raise


class OmeTiffStackSource(_BaseInteractiveStackSource):
    """Interactive multi-plane Z/T stack source for OME-TIFF and ImageJ hyperstacks."""

    def __init__(self, path: str | Path, *, series: int = 0, tile: int = 512):
        import tifffile

        self._init_common(path, series, tile)
        is_qp = self.path.suffix.lower() == ".qptiff"
        self._tf = tifffile.TiffFile(str(self.path))
        try:
            ome_images = _parse_ome_xml(getattr(self._tf, "ome_metadata", None))
            grouped = _group_logical_series(self._tf, ome_images, is_qptiff=is_qp)
            if not (0 <= self.series_index < len(grouped)):
                raise ValueError(f"series {self.series_index} out of range (0..{len(grouped) - 1})")
            self._series, _lvs, ome_rec = grouped[self.series_index]
            self._layout = _parse_series_axes(self._series, ome_rec, is_qptiff_container=is_qp)
            frame_bytes = (
                self._layout.width
                * self._layout.height
                * max(1, self._layout.n_c)
                * self._layout.dtype.itemsize
            )
            if frame_bytes <= 0 or frame_bytes > _MAX_STACK_FRAME_BYTES:
                raise ValueError(
                    f"TIFF stack plane size ({frame_bytes} B) exceeds safety limit ({_MAX_STACK_FRAME_BYTES} B)"
                )

            self.T = max(1, int(self._layout.n_t))
            self.P = 1
            self.Z = max(1, int(self._layout.n_z))
            self.frame_shape = (self._layout.n_c, self._layout.height, self._layout.width)
            self.dtype = self._layout.dtype
            self.rgb = self._layout.rgb
            (
                self.pixel_size_um,
                self.z_step_um,
                self.calibration_source,
                self.magnification,
                self.channels,
            ) = _resolve_metadata_for_series(
                self._tf, self._series, self._layout, ome_rec, series_index=self.series_index
            )
            site_name = (
                (ome_rec or {}).get("name")
                or str(getattr(self._series, "name", "") or "").strip()
                or f"Series {self.series_index + 1}"
            )
            self.sites = [{"i": 0, "name": site_name, "row": 0, "col": 0, "stageUm": None}]
            self.rows = 1
            self.cols = 1
            self.z_home = self.Z // 2
            self.bottom_to_top = True if self.z_step_um is not None else None
            self.period_ms: float | None = None
            self.times_ms: list[float] = []
            self.exposure_ms: float | None = None

            self._finalize_stack_attrs(
                source_label="TIFF",
                include_selection_p=self.series_index > 0 or len(grouped) > 1,
            )
        except BaseException:
            self._tf.close()
            raise

    def _read_plane_cyx(self, t: int, _p: int, z: int) -> np.ndarray:
        with self._io_lock:
            return _series_2d_to_cyx(self._series, self._layout, z_idx=z, t_idx=t)

    def _close_backend_handles(self) -> None:
        with self._io_lock:
            self._tf.close()


def collect_ometiff_info(path: str | Path) -> dict[str, Any]:
    """`nd2wsi info` summary payload for a TIFF / OME-TIFF / QPTIFF file."""
    import tifffile

    path = Path(path)
    is_qp = path.suffix.lower() == ".qptiff"
    with tifffile.TiffFile(str(path)) as tf:
        ome_images = _parse_ome_xml(getattr(tf, "ome_metadata", None))
        grouped = _group_logical_series(tf, ome_images, is_qptiff=is_qp)
        s0, level_objs, ome0 = grouped[0]
        layout = _parse_series_axes(s0, ome0, is_qptiff_container=is_qp)
        px_um, z_step_um, cal_src, mag, channels = _resolve_metadata_for_series(
            tf, s0, layout, ome0, series_index=0
        )
        p0 = s0.pages[0]
        fmt_label = (
            "OME-TIFF"
            if ome_images or getattr(tf, "is_ome", False)
            else "PerkinElmer QPTIFF"
            if is_qp
            else "ImageJ TIFF"
            if getattr(tf, "is_imagej", False)
            else "TIFF"
        )
        return {
            "file": str(path),
            "size_bytes": path.stat().st_size,
            "format": fmt_label,
            "series_count": len(grouped),
            "width": layout.width,
            "height": layout.height,
            "channels": layout.n_c,
            "z_planes": layout.n_z,
            "timepoints": layout.n_t,
            "dtype": str(layout.dtype),
            "rgb": layout.rgb,
            "tiled": bool(getattr(p0, "is_tiled", False)),
            "pyramid_levels": len(level_objs),
            "pixel_size_um": list(px_um) if px_um else None,
            "z_step_um": z_step_um,
            "calibration_source": cal_src,
            "magnification": mag,
            "channel_info": [
                {"name": ch.name, "color": "#{:02X}{:02X}{:02X}".format(*ch.color)}
                for ch in channels
            ],
        }
