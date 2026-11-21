"""Pyramid level discovery, SubIFD/multi-page indexing, and tiled/untiled TIFF readers."""

from __future__ import annotations

import math
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from .contrast import auto_window
from .direct_common import (
    MAX_EXTRA,
    LeaseClosed,
    _Lifecycle,
    _parse_cyx_index,
    _TileCache,
    box_mean,
)
from .ometiff_metadata import _parse_series_axes, _ParsedSeriesLayout
from .platform_io import read_at
from .reader import ChannelInfo, PlaneSource

UNTILED_MAX_PIXELS = 256_000_000
# Eager in-memory pyramid ceiling for untiled 2D TIFFs (64 MiB raw level-0 bytes).
# Larger untiled 2D TIFFs stream strips/rows lazily on demand from disk.
UNTILED_EAGER_MAX_BYTES = 64 * 1024 * 1024
_LAZY_STREAM_BAND_ROWS = 256
_MAX_CACHED_STRIP_BYTES = 16 * 1024 * 1024
_MEMO_BAND_ROWS = 64

_ASSOCIATED_NAMES = frozenset({"thumbnail", "label", "macro", "overview", "mask"})

_MAX_SEGMENT_BYTES = 256 * 1024 * 1024
_MAX_STACK_FRAME_BYTES = 512 * 1024 * 1024


def _checked_read_segment(fd: int, file_size: int, offset: int, count: int, seg: int) -> bytes:
    off = int(offset)
    cnt = int(count)
    if off < 0 or cnt < 0 or cnt > _MAX_SEGMENT_BYTES or off + cnt > file_size:
        raise ValueError(f"TIFF segment {seg} ({cnt} B @ {off}) exceeds file bounds")
    return read_at(fd, cnt, off)


def _is_associated_series(series: Any) -> bool:
    name = str(getattr(series, "name", "") or "").strip().lower()
    if name in _ASSOCIATED_NAMES:
        return True
    page0 = series.pages[0] if getattr(series, "pages", None) else None
    if page0 is None:
        return True
    desc = (getattr(page0, "description", "") or "").strip().lower()
    for assoc in _ASSOCIATED_NAMES:
        if re.match(rf"^{assoc}\b", desc):
            return True
    subfile = int(getattr(page0, "subfiletype", 0) or 0)
    # FILETYPE.REDUCEDIMAGE = 1, MACRO/MASK bits > 1 when small
    if subfile not in (0, 1) and ("macro" in desc or "label" in desc):
        return True
    return False


@dataclass(frozen=True)
class _SyntheticPageSeries:
    """Lightweight view over a slice of homogeneous TIFF IFDs (e.g. NIS-Elements multipoint OME-TIFF)."""

    pages: tuple[Any, ...]
    shape: tuple[int, ...]
    axes: str
    dtype: np.dtype
    name: str
    parent: Any

    @property
    def levels(self) -> list[_SyntheticPageSeries]:
        return [self]

    def asarray(self) -> np.ndarray:
        arrs = [np.asarray(p.asarray()) for p in self.pages]
        if len(arrs) == 1 and tuple(arrs[0].shape) == self.shape:
            return arrs[0]
        return np.stack(arrs, axis=0).reshape(self.shape)


def _expand_multipoint_ome_pages(
    tf: Any, s0: Any, ome0: dict[str, Any] | None
) -> list[tuple[Any, list[Any], dict[str, Any] | None]] | None:
    """Recover trailing multipoint scenes when an exporter wrote 1 OME <Image> for N*K homogeneous IFDs."""
    if ome0 is None or len(s0.levels) != 1:
        return None
    k_pages = len(s0.pages)
    if k_pages < 1:
        return None
    total_pages = len(tf.pages)
    if total_pages <= k_pages or total_pages % k_pages != 0:
        return None
    all_pages = [
        pg.aspage() if hasattr(pg, "aspage") else pg
        for pg in tf.pages
    ]
    if len(all_pages) != total_pages:
        return None
    p0 = all_pages[0]
    ref_shape = tuple(p0.shape)
    ref_dtype = np.dtype(p0.dtype)
    ref_tiled = bool(getattr(p0, "is_tiled", False))
    ref_spp = int(getattr(p0, "samplesperpixel", 1) or 1)
    ref_planar = int(getattr(p0, "planarconfig", 1) or 1)
    size_c = ome0.get("size_c")
    size_z = ome0.get("size_z")
    size_t = ome0.get("size_t")
    if size_c is not None and size_z is not None and size_t is not None:
        expected_ifds = (
            max(1, int(size_c) // max(1, ref_spp))
            * max(1, int(size_z))
            * max(1, int(size_t))
        )
        if expected_ifds != k_pages:
            return None
    for pg in all_pages:
        if (
            int(getattr(pg, "subfiletype", 0) or 0) != 0
            or tuple(pg.shape) != ref_shape
            or np.dtype(pg.dtype) != ref_dtype
            or bool(getattr(pg, "is_tiled", False)) != ref_tiled
            or int(getattr(pg, "samplesperpixel", 1) or 1) != ref_spp
            or int(getattr(pg, "planarconfig", 1) or 1) != ref_planar
        ):
            return None

    n_scenes = total_pages // k_pages
    out: list[tuple[Any, list[Any], dict[str, Any] | None]] = []
    for idx in range(n_scenes):
        slc = tuple(all_pages[idx * k_pages : (idx + 1) * k_pages])
        syn = _SyntheticPageSeries(
            pages=slc,
            shape=tuple(s0.shape),
            axes=str(s0.axes),
            dtype=np.dtype(s0.dtype),
            name=f"Position {idx + 1}",
            parent=tf,
        )
        out.append((syn, [syn], {**ome0, "name": f"Position {idx + 1}"}))
    return out


def _group_logical_series(
    tf: Any, ome_images: list[dict[str, Any]], *, is_qptiff: bool = False
) -> list[tuple[Any, list[Any], dict[str, Any] | None]]:
    """Return [(base_series, level_objects, ome_record), ...] for real image scenes.

    Handles both SubIFD pyramids (``series.levels``) and multi-page pyramids where
    decreasing-resolution pages were written as separate top-level series without
    ``subfiletype=1``.
    """
    raw_series = [s for s in tf.series if getattr(s, "pages", None) and not _is_associated_series(s)]
    if not raw_series:
        raw_series = [s for s in tf.series if getattr(s, "pages", None)]
    if not raw_series:
        raise ValueError("TIFF file contains no readable image series")

    # Multi-image OME-TIFF: each series corresponds to an OME <Image> scene
    if len(ome_images) > 1 and len(raw_series) >= len(ome_images):
        grouped = []
        for idx in range(len(ome_images)):
            s = raw_series[idx]
            grouped.append((s, list(s.levels), ome_images[idx]))
        return grouped

    s0 = raw_series[0]
    ome0 = ome_images[0] if ome_images else None
    if len(raw_series) == 1 and len(ome_images) == 1 and len(s0.levels) == 1:
        expanded = _expand_multipoint_ome_pages(tf, s0, ome0)
        if expanded is not None:
            return expanded

    if len(s0.levels) > 1 or len(raw_series) == 1 or len(ome_images) > 1:
        if len(ome_images) <= 1 and len(s0.levels) > 1:
            return [(s0, list(s0.levels), ome0)]
        return [
            (s, list(s.levels), ome_images[i] if i < len(ome_images) else None)
            for i, s in enumerate(raw_series)
        ]

    # Single-level s0 with multiple top-level series and <=1 OME <Image>:
    # Check whether subsequent series form a decreasing-resolution pyramid of s0.
    try:
        base_layout = _parse_series_axes(s0, ome0, is_qptiff_container=is_qptiff)
    except NotImplementedError:
        return [(s, list(s.levels), None) for s in raw_series]

    if base_layout.n_z == 1 and base_layout.n_t == 1 and getattr(s0.pages[0], "is_tiled", False):
        pyr_levels = [s0]
        prev_h, prev_w = base_layout.height, base_layout.width
        base_aspect = base_layout.width / max(1, base_layout.height)
        non_pyramid: list[Any] = []
        for cand in raw_series[1:]:
            try:
                cl = _parse_series_axes(cand, None, is_qptiff_container=is_qptiff)
            except NotImplementedError:
                continue
            cand_aspect = cl.width / max(1, cl.height)
            aspect_ok = abs(cand_aspect - base_aspect) / max(base_aspect, 1e-6) <= 0.06
            if (
                cl.n_z == 1
                and cl.n_t == 1
                and cl.n_c == base_layout.n_c
                and cl.rgb == base_layout.rgb
                and cl.dtype == base_layout.dtype
                and cl.height < prev_h
                and cl.width < prev_w
                and aspect_ok
                and getattr(cand.pages[0], "is_tiled", False)
            ):
                pyr_levels.append(cand)
                prev_h, prev_w = cl.height, cl.width
            else:
                non_pyramid.append(cand)
        if len(pyr_levels) > 1 and not non_pyramid:
            return [(s0, pyr_levels, ome0)]

    return [
        (s, list(s.levels), ome_images[i] if i < len(ome_images) else None)
        for i, s in enumerate(raw_series)
    ]


# ---------------------------------------------------------------------------
# Fast Positional Tile Readers for Direct 2D Pyramidal / Tiled TIFFs
# ---------------------------------------------------------------------------


def _page_kf(page: Any) -> Any:
    """Return the underlying TiffPage keyframe for a TiffPage or lightweight TiffFrame."""
    kf = getattr(page, "keyframe", None)
    if kf is not None:
        return kf
    if hasattr(page, "aspage"):
        return page.aspage()
    return page


def _box_mean_axes(block: np.ndarray, ky: int, kx: int, out_dtype: np.dtype) -> np.ndarray:
    """Box-average the last two axes by ``(ky, kx)``, preserving each axis independently."""
    ky = max(1, int(ky))
    kx = max(1, int(kx))
    if ky == 1 and kx == 1:
        return np.ascontiguousarray(block, dtype=out_dtype)
    h, w = block.shape[-2], block.shape[-1]
    if ky == kx and h >= ky and w >= kx:
        return np.ascontiguousarray(box_mean(block, ky, out_dtype))
    ey = min(ky, h)
    ex = min(kx, w)
    oh = h // ey
    ow = w // ex
    view = block[..., : oh * ey, : ow * ex]
    dt = np.dtype(out_dtype)
    acc_dtype = (
        np.float64
        if block.dtype == np.dtype(np.float64)
        or dt == np.dtype(np.float64)
        or (np.issubdtype(block.dtype, np.integer) and block.dtype.itemsize >= 4)
        or (np.issubdtype(dt, np.integer) and dt.itemsize >= 4)
        else np.float32
    )
    mean = view.reshape((*block.shape[:-2], oh, ey, ow, ex)).mean(axis=(-3, -1), dtype=acc_dtype)
    if np.issubdtype(dt, np.integer):
        info = np.iinfo(dt)
        return np.ascontiguousarray(np.clip(np.rint(mean), info.min, info.max).astype(dt))
    return np.ascontiguousarray(mean.astype(dt))


def _decode_page_segment(page: Any, raw_bytes: bytes, seg: int, tables: dict[str, Any], out_dtype: np.dtype) -> np.ndarray:
    kf = _page_kf(page)
    try:
        decoded = np.asarray(kf.decode(raw_bytes, seg, **tables)[0])
    except ValueError as exc:
        if "imagecodecs" in str(exc).lower():
            raise RuntimeError(
                "reading compressed TIFF tiles requires 'imagecodecs': pip install imagecodecs"
            ) from exc
        raise
    if decoded.dtype == np.bool_:
        return decoded.astype(np.uint8) * 255
    return np.asarray(decoded, dtype=out_dtype)


def _contig_tile_reader(page: Any, fd: int, cache: _TileCache, tag: Any, out_dtype: np.dtype, keep_samples: int):
    """Cached window reader ``(y0, y1, x0, x1) -> (keep_samples, h, w)`` for a CONTIG tiled page."""
    h, w = int(page.imagelength), int(page.imagewidth)
    th, tw = int(page.tilelength), int(page.tilewidth)
    samples = int(page.samplesperpixel)
    if th <= 0 or tw <= 0 or samples <= 0:
        raise ValueError(f"invalid TIFF tile geometry ({tw}x{th}x{samples})")
    raw_item = max(np.dtype(page.dtype).itemsize, np.dtype(out_dtype).itemsize)
    if th * tw * samples * raw_item > _MAX_SEGMENT_BYTES:
        raise ValueError(
            f"TIFF tile decoded size ({th * tw * samples * raw_item} B) exceeds safety limit"
        )
    across = -(-w // tw)
    offsets = page.dataoffsets
    counts = page.databytecounts
    file_size = os.fstat(fd).st_size
    tables = {
        "jpegtables": getattr(page, "jpegtables", None),
        "jpegheader": getattr(page, "jpegheader", None),
    }

    def _tile_cyx(ty: int, tx: int) -> np.ndarray:
        key = (tag, ty, tx)
        hit = cache.get(key)
        if hit is not None:
            return hit
        seg = ty * across + tx
        if seg >= len(counts) or seg >= len(offsets) or counts[seg] == 0:
            block = np.zeros((keep_samples, th, tw), dtype=out_dtype)
        else:
            raw = _checked_read_segment(fd, file_size, offsets[seg], counts[seg], seg)
            dec = _decode_page_segment(page, raw, seg, tables, out_dtype).reshape(-1, tw, samples)[:th]
            block = np.ascontiguousarray(np.moveaxis(dec[..., :keep_samples], -1, 0))
        cache.put(key, block)
        return block

    def read(cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        out: np.ndarray | None = None
        for ty in range(y0 // th, -(-y1 // th)):
            for tx in range(x0 // tw, -(-x1 // tw)):
                t_blk = _tile_cyx(ty, tx)[cs]
                if out is None:
                    lead = t_blk.shape[:-2]
                    out = np.zeros((*lead, y1 - y0, x1 - x0), dtype=out_dtype)
                oy0, oy1 = max(y0, ty * th), min(y1, ty * th + th, h)
                ox0, ox1 = max(x0, tx * tw), min(x1, tx * tw + tw, w)
                out[..., oy0 - y0 : oy1 - y0, ox0 - x0 : ox1 - x0] = t_blk[
                    ..., oy0 - ty * th : oy1 - ty * th, ox0 - tx * tw : ox1 - tx * tw
                ]
        if out is not None:
            return out
        dummy = np.empty((keep_samples, 0, 0), dtype=out_dtype)[cs]
        return np.zeros((*dummy.shape[:-2], y1 - y0, x1 - x0), dtype=out_dtype)

    return read


def _planar_tile_reader(
    pages: list[Any],
    fd: int,
    cache: _TileCache,
    tag: Any,
    out_dtype: np.dtype,
    n_c: int,
):
    """Cached per-channel window reader for multi-page ``CYX`` or single-page ``SEPARATE`` TIFFs."""
    single_separate = len(pages) == 1 and n_c > 1
    page0 = _page_kf(pages[0])
    h, w = int(page0.imagelength), int(page0.imagewidth)
    th, tw = int(page0.tilelength), int(page0.tilewidth)
    if th <= 0 or tw <= 0:
        raise ValueError(f"invalid TIFF tile geometry ({tw}x{th})")
    raw_item = max(np.dtype(page0.dtype).itemsize, np.dtype(out_dtype).itemsize)
    if th * tw * raw_item > _MAX_SEGMENT_BYTES:
        raise ValueError(
            f"TIFF tile decoded size ({th * tw * raw_item} B) exceeds safety limit"
        )
    ny = -(-h // th)
    nx = -(-w // tw)
    tiles_per_plane = ny * nx
    file_size = os.fstat(fd).st_size

    page_tables = [
        {
            "jpegtables": getattr(_page_kf(p), "jpegtables", None),
            "jpegheader": getattr(_page_kf(p), "jpegheader", None),
        }
        for p in pages
    ]

    def _channel_tile(ci: int, ty: int, tx: int) -> np.ndarray:
        key = (tag, ci, ty, tx)
        hit = cache.get(key)
        if hit is not None:
            return hit
        if single_separate:
            page = page0
            tables = page_tables[0]
            seg = ci * tiles_per_plane + ty * nx + tx
        else:
            page = pages[ci]
            tables = page_tables[ci]
            seg = ty * nx + tx
        offsets = page.dataoffsets
        counts = page.databytecounts
        if seg >= len(counts) or seg >= len(offsets) or counts[seg] == 0:
            block = np.zeros((th, tw), dtype=out_dtype)
        else:
            raw = _checked_read_segment(fd, file_size, offsets[seg], counts[seg], seg)
            dec = _decode_page_segment(page, raw, seg, tables, out_dtype).reshape(-1, tw)[:th]
            block = np.ascontiguousarray(dec)
        cache.put(key, block)
        return block

    def read(cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        selected = np.arange(n_c)[cs]
        scalar = np.ndim(selected) == 0
        ch_list = [int(c) for c in np.atleast_1d(selected)]
        out = np.zeros((len(ch_list), y1 - y0, x1 - x0), dtype=out_dtype)
        if y1 <= y0 or x1 <= x0 or not ch_list:
            return out[0] if scalar else out
        for slot, ci in enumerate(ch_list):
            for ty in range(y0 // th, -(-y1 // th)):
                for tx in range(x0 // tw, -(-x1 // tw)):
                    t_yx = _channel_tile(ci, ty, tx)
                    oy0, oy1 = max(y0, ty * th), min(y1, ty * th + th, h)
                    ox0, ox1 = max(x0, tx * tw), min(x1, tx * tw + tw, w)
                    out[slot, oy0 - y0 : oy1 - y0, ox0 - x0 : ox1 - x0] = t_yx[
                        oy0 - ty * th : oy1 - ty * th, ox0 - tx * tw : ox1 - tx * tw
                    ]
        return out[0] if scalar else out

    return read


class _OmeTiledLevel:
    """One halving-ladder level backed by a tiled TIFF page/series."""

    def __init__(
        self,
        reader_fn: Any,
        n_c: int,
        h: int,
        w: int,
        extra: int,
        life: _Lifecycle,
        dtype: np.dtype,
        src_h: int | None = None,
        src_w: int | None = None,
    ):
        self._read = reader_fn
        self.shape = (n_c, h, w)
        self.dtype = np.dtype(dtype)
        self._extra = int(extra)
        self._src_h = int(src_h) if src_h is not None else h * int(extra)
        self._src_w = int(src_w) if src_w is not None else w * int(extra)
        self._life = life

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        e = self._extra
        ky = min(e, self._src_h)
        kx = min(e, self._src_w)
        with self._life:
            block = self._read(
                cs,
                y0 * ky,
                min(self._src_h, y1 * ky),
                x0 * kx,
                min(self._src_w, x1 * kx),
            )
        if (ky > 1 or kx > 1) and y1 > y0 and x1 > x0:
            block = _box_mean_axes(block, ky, kx, self.dtype)
        return block


class _CoarseMemoLevel:
    """Lazily memoized coarse overview level when a TIFF lacks deep pyramid SubIFDs."""

    def __init__(self, finer: Any, h: int, w: int, factor: int, life: _Lifecycle):
        self._finer = finer
        self._factor = int(factor)
        self._life = life
        self.shape = (finer.shape[0], h, w)
        self.dtype = np.dtype(finer.dtype)
        self._memo: np.ndarray | None = None
        self._lock = threading.Lock()

    def _whole(self) -> np.ndarray:
        with self._lock:
            if self._memo is None:
                c, h, w = self.shape
                f = self._factor
                fh, fw = self._finer.shape[1], self._finer.shape[2]
                ky = min(f, fh)
                kx = min(f, fw)
                memo = np.zeros((c, h, w), dtype=self.dtype)
                for r0 in range(0, h, _MEMO_BAND_ROWS):
                    r1 = min(h, r0 + _MEMO_BAND_ROWS)
                    fy0, fy1 = r0 * ky, min(fh, r1 * ky)
                    fx1 = min(fw, w * kx)
                    if fy1 <= fy0 or fx1 <= 0:
                        continue
                    band = np.asarray(self._finer[:, fy0:fy1, 0:fx1])
                    reduced = _box_mean_axes(band, ky, kx, self.dtype)
                    rh, rw = reduced.shape[-2], reduced.shape[-1]
                    memo[:, r0 : r0 + rh, :rw] = reduced
                self._memo = memo
            return self._memo

    def release(self) -> None:
        self._memo = None

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        with self._life:
            if x0 == x1 or y0 == y1:
                return np.empty((self.shape[0], y1 - y0, x1 - x0), dtype=self.dtype)[cs]
            return self._whole()[:, y0:y1, x0:x1][cs].copy()


class _MemoryPyramidLevel:
    """Direct in-memory 2D pyramid level for untiled 2D TIFFs (instant ROI reopen)."""

    def __init__(self, base_cyx: np.ndarray, h: int, w: int, downsample: int, life: _Lifecycle):
        self._base = base_cyx
        self._downsample = int(downsample)
        self._life = life
        self.shape = (base_cyx.shape[0], h, w)
        self.dtype = base_cyx.dtype
        self._memo: np.ndarray | None = None
        self._lock = threading.Lock()

    def _reduce_window(self, cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        d = self._downsample
        if d == 1:
            return self._base[cs, y0:y1, x0:x1].copy()
        _, bh, bw = self._base.shape
        ky = min(d, bh)
        kx = min(d, bw)
        raw = self._base[cs, y0 * ky : min(bh, y1 * ky), x0 * kx : min(bw, x1 * kx)]
        return _box_mean_axes(raw, ky, kx, self.dtype)

    def _whole(self) -> np.ndarray:
        with self._lock:
            if self._memo is None:
                c, h, w = self.shape
                memo = np.empty((c, h, w), dtype=self.dtype)
                for r0 in range(0, h, _MEMO_BAND_ROWS):
                    r1 = min(h, r0 + _MEMO_BAND_ROWS)
                    memo[:, r0:r1, :] = self._reduce_window(slice(None), r0, r1, 0, w)
                self._memo = memo
            return self._memo

    def release(self) -> None:
        self._memo = None
        self._base = None  # type: ignore[assignment]

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        with self._life:
            if x0 == x1 or y0 == y1:
                return np.empty((self.shape[0], y1 - y0, x1 - x0), dtype=self.dtype)[cs]
            if self._downsample >= 8:
                return self._whole()[:, y0:y1, x0:x1][cs].copy()
            return self._reduce_window(cs, y0, y1, x0, x1)


def _resolve_untiled_eager_max_bytes(
    eager_max_bytes: int | None = None,
    *,
    default_max_bytes: int | None = None,
) -> int:
    if eager_max_bytes is not None:
        return max(0, int(eager_max_bytes))
    raw_env = os.environ.get("ND2WSI_UNTILED_EAGER_MAX_BYTES")
    if raw_env is not None and raw_env.strip() != "":
        return max(0, int(raw_env))
    return int(UNTILED_EAGER_MAX_BYTES if default_max_bytes is None else default_max_bytes)


def _read_untiled_page_rows(
    page: Any,
    fd: int,
    cache: _TileCache,
    tag: Any,
    y0: int,
    y1: int,
    x0: int,
    x1: int,
    out_dtype: np.dtype,
    keep_samples: int,
    *,
    strip_base: int = 0,
) -> np.ndarray:
    """Read ``(keep_samples, y1 - y0, x1 - x0)`` from an untiled TIFF page lazily."""
    kf = _page_kf(page)
    h, w = int(kf.imagelength), int(kf.imagewidth)
    out_h = max(0, y1 - y0)
    out_w = max(0, x1 - x0)
    out = np.zeros((keep_samples, out_h, out_w), dtype=out_dtype)
    if out_h == 0 or out_w == 0:
        return out
    cy0, cy1 = max(0, y0), min(h, y1)
    cx0, cx1 = max(0, x0), min(w, x1)
    if cy1 <= cy0 or cx1 <= cx0:
        return out

    rps = int(getattr(kf, "rowsperstrip", h) or h)
    rps = max(1, min(rps, h))
    offsets = page.dataoffsets
    counts = page.databytecounts
    planar = int(getattr(kf, "planarconfig", 1) or 1)
    samples_in_strip = 1 if planar == 2 else int(getattr(kf, "samplesperpixel", 1) or 1)
    byteorder = getattr(getattr(kf, "parent", None), "byteorder", "=") or "="
    raw_dt = np.dtype(kf.dtype).newbyteorder(byteorder)
    bps = getattr(kf, "bitspersample", None)
    bps_tuple = (bps,) if isinstance(bps, int) else tuple(bps or ())
    direct_uncompressed = (
        int(getattr(kf, "compression", 1) or 1) == 1
        and int(getattr(kf, "predictor", 1) or 1) == 1
        and int(getattr(kf, "fillorder", 1) or 1) == 1
        and raw_dt != np.dtype(np.bool_)
        and bool(bps_tuple)
        and all(int(b) == raw_dt.itemsize * 8 for b in bps_tuple)
    )
    row_bytes = w * samples_in_strip * raw_dt.itemsize
    file_size = os.fstat(fd).st_size
    tables = {
        "jpegtables": getattr(kf, "jpegtables", None),
        "jpegheader": getattr(kf, "jpegheader", None),
    }

    for s in range(cy0 // rps, (cy1 - 1) // rps + 1):
        seg = strip_base + s
        sy0 = max(cy0, s * rps)
        sy1 = min(cy1, (s + 1) * rps, h)
        if sy1 <= sy0 or seg >= len(counts) or seg >= len(offsets) or counts[seg] == 0:
            continue
        sh = min(rps, h - s * rps)
        if direct_uncompressed and int(counts[seg]) >= sh * row_bytes:
            n_rows = sy1 - sy0
            byte_off = int(offsets[seg]) + (sy0 - s * rps) * row_bytes
            buf = _checked_read_segment(fd, file_size, byte_off, n_rows * row_bytes, seg)
            if len(buf) == n_rows * row_bytes:
                raw_rows = np.frombuffer(buf, dtype=raw_dt).reshape(n_rows, w, samples_in_strip)
                sub = raw_rows[:, cx0:cx1, :keep_samples]
                out[:, sy0 - y0 : sy1 - y0, cx0 - x0 : cx1 - x0] = np.asarray(
                    np.moveaxis(sub, -1, 0), dtype=out_dtype
                )
                continue

        decoded_strip_bytes = sh * w * samples_in_strip * max(raw_dt.itemsize, np.dtype(out_dtype).itemsize)
        if decoded_strip_bytes > _MAX_SEGMENT_BYTES:
            raise ValueError(
                f"TIFF strip {seg} decoded size ({decoded_strip_bytes} B) exceeds safety limit"
            )
        key = (tag, "strip", seg)
        strip_cyx = cache.get(key)
        if strip_cyx is None:
            raw = _checked_read_segment(fd, file_size, int(offsets[seg]), int(counts[seg]), seg)
            dec = _decode_page_segment(page, raw, seg, tables, out_dtype).reshape(
                -1, w, samples_in_strip
            )[:sh]
            strip_cyx = np.ascontiguousarray(np.moveaxis(dec[..., :keep_samples], -1, 0))
            if strip_cyx.nbytes <= _MAX_CACHED_STRIP_BYTES:
                cache.put(key, strip_cyx)
        out[:, sy0 - y0 : sy1 - y0, cx0 - x0 : cx1 - x0] = strip_cyx[
            :, sy0 - s * rps : sy1 - s * rps, cx0:cx1
        ]
    return out


def _untiled_window_reader_for(
    series: Any,
    fd: int,
    cache: _TileCache,
    tag: Any,
    layout: _ParsedSeriesLayout,
    *,
    z_idx: int = 0,
    t_idx: int = 0,
):
    """Build a lazy ``read(cs, y0, y1, x0, x1)`` callable for an untiled 2D TIFF plane."""
    axes = layout.axes
    shape = layout.shape
    page0 = series.pages[0]
    page_ndim = len(page0.shape)
    lead_axes = axes[: len(axes) - page_ndim]
    lead_shape = shape[: len(shape) - page_ndim]
    n_lead = math.prod(lead_shape) if lead_shape else 1
    h, w = layout.height, layout.width

    # 1. Multi-page where each lead coordinate maps to a TiffPage / TiffFrame
    if len(series.pages) == n_lead and shape[len(lead_shape) :] == tuple(page0.shape):
        if "C" in lead_axes:
            c_pages = []
            for ci in range(layout.n_c):
                coords = [
                    t_idx if ax == "T" else z_idx if ax == "Z" else ci if ax == "C" else 0
                    for ax in lead_axes
                ]
                p_flat = int(np.ravel_multi_index(coords, lead_shape))
                c_pages.append(series.pages[p_flat])
            if all(
                not getattr(_page_kf(p), "is_tiled", False)
                and int(_page_kf(p).imagelength) == h
                and int(_page_kf(p).imagewidth) == w
                and int(getattr(_page_kf(p), "samplesperpixel", 1) or 1) == 1
                for p in c_pages
            ):
                def _read_multipage(cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
                    selected = np.arange(layout.n_c)[cs]
                    scalar = np.ndim(selected) == 0
                    ch_list = [int(c) for c in np.atleast_1d(selected)]
                    out = np.zeros((len(ch_list), max(0, y1 - y0), max(0, x1 - x0)), dtype=layout.dtype)
                    for slot, ci in enumerate(ch_list):
                        out[slot] = _read_untiled_page_rows(
                            c_pages[ci],
                            fd,
                            cache,
                            (*tag, "c", ci),
                            y0,
                            y1,
                            x0,
                            x1,
                            layout.dtype,
                            1,
                        )[0]
                    return out[0] if scalar else out

                return _read_multipage

        else:
            coords = [
                t_idx if ax == "T" else z_idx if ax == "Z" else 0 for ax in lead_axes
            ]
            p_flat = int(np.ravel_multi_index(coords, lead_shape)) if lead_shape else 0
            target_page = series.pages[p_flat]
            tkf = _page_kf(target_page)
            if (
                not getattr(tkf, "is_tiled", False)
                and int(tkf.imagelength) == h
                and int(tkf.imagewidth) == w
            ):
                planar = int(getattr(tkf, "planarconfig", 1) or 1)
                samples = int(getattr(tkf, "samplesperpixel", 1) or 1)
                if planar == 1 and samples >= layout.n_c:
                    def _read_contig(cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
                        blk = _read_untiled_page_rows(
                            target_page,
                            fd,
                            cache,
                            (*tag, "p", p_flat),
                            y0,
                            y1,
                            x0,
                            x1,
                            layout.dtype,
                            layout.n_c,
                        )
                        return blk[cs]

                    return _read_contig

                if planar == 2 and samples >= layout.n_c:
                    rps = max(1, min(h, int(getattr(tkf, "rowsperstrip", h) or h)))
                    strips_per_plane = -(-h // rps)

                    def _read_planar(cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
                        selected = np.arange(layout.n_c)[cs]
                        scalar = np.ndim(selected) == 0
                        ch_list = [int(c) for c in np.atleast_1d(selected)]
                        out = np.zeros(
                            (len(ch_list), max(0, y1 - y0), max(0, x1 - x0)),
                            dtype=layout.dtype,
                        )
                        for slot, ci in enumerate(ch_list):
                            out[slot] = _read_untiled_page_rows(
                                target_page,
                                fd,
                                cache,
                                (*tag, "ps", ci),
                                y0,
                                y1,
                                x0,
                                x1,
                                layout.dtype,
                                1,
                                strip_base=ci * strips_per_plane,
                            )[0]
                        return out[0] if scalar else out

                    return _read_planar

    # 2. Single-IFD contiguous uncompressed hyperstack (e.g., ImageJ raw dataoffset)
    dataoffset = getattr(series, "dataoffset", None)
    bps0 = getattr(page0, "bitspersample", None)
    bps0_tuple = (bps0,) if isinstance(bps0, int) else tuple(bps0 or ())
    if (
        dataoffset is not None
        and lead_axes
        and int(getattr(page0, "compression", 1) or 1) == 1
        and int(getattr(page0, "predictor", 1) or 1) == 1
        and int(getattr(page0, "fillorder", 1) or 1) == 1
        and layout.raw_dtype != np.dtype(np.bool_)
        and bool(bps0_tuple)
        and all(int(b) == layout.raw_dtype.itemsize * 8 for b in bps0_tuple)
    ):
        frame_axes = axes[len(lead_axes) :]
        frame_shape = shape[len(lead_shape) :]
        elem_bytes = int(math.prod(frame_shape)) * layout.raw_dtype.itemsize
        byteorder = getattr(getattr(series, "parent", None), "byteorder", "=") or "="
        disk_dt = layout.raw_dtype.newbyteorder(byteorder)
        file_size = os.fstat(fd).st_size
        if frame_axes == "YX" and "C" in lead_axes and frame_shape == (h, w):
            row_bytes = w * disk_dt.itemsize

            def _read_hyper_c(cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
                selected = np.arange(layout.n_c)[cs]
                scalar = np.ndim(selected) == 0
                ch_list = [int(c) for c in np.atleast_1d(selected)]
                out = np.zeros(
                    (len(ch_list), max(0, y1 - y0), max(0, x1 - x0)), dtype=layout.dtype
                )
                cy0, cy1 = max(0, y0), min(h, y1)
                cx0, cx1 = max(0, x0), min(w, x1)
                if cy1 > cy0 and cx1 > cx0:
                    n_rows = cy1 - cy0
                    for slot, ci in enumerate(ch_list):
                        coords = [
                            t_idx if ax == "T" else z_idx if ax == "Z" else ci if ax == "C" else 0
                            for ax in lead_axes
                        ]
                        idx_flat = int(np.ravel_multi_index(coords, lead_shape))
                        off = int(dataoffset) + idx_flat * elem_bytes + cy0 * row_bytes
                        buf = _checked_read_segment(fd, file_size, off, n_rows * row_bytes, idx_flat)
                        arr_p = np.frombuffer(buf, dtype=disk_dt).reshape(n_rows, w)
                        out[slot, cy0 - y0 : cy1 - y0, cx0 - x0 : cx1 - x0] = np.asarray(
                            arr_p[:, cx0:cx1], dtype=layout.dtype
                        )
                return out[0] if scalar else out

            return _read_hyper_c

    return None


class _LazyUntiledLevel:
    """Band-streamed, on-demand 2D pyramid level for untiled TIFFs exceeding the eager ceiling."""

    def __init__(
        self,
        reader_fn: Any,
        base_h: int,
        base_w: int,
        n_c: int,
        h: int,
        w: int,
        downsample: int,
        life: _Lifecycle,
        dtype: np.dtype,
    ):
        self._read = reader_fn
        self._base_h = int(base_h)
        self._base_w = int(base_w)
        self._downsample = int(downsample)
        self._life = life
        self.shape = (int(n_c), int(h), int(w))
        self.dtype = np.dtype(dtype)
        self._memo: np.ndarray | None = None
        self._lock = threading.Lock()
        self._memo_eligible = (
            self._downsample >= 8
            and int(n_c) * int(h) * int(w) * int(self.dtype.itemsize) <= 8 * 1024 * 1024
        )

    def _reduce_window(self, cs: Any, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        reader = self._read
        if reader is None:
            raise LeaseClosed(self._life)
        d = self._downsample
        if d == 1:
            if (y1 - y0) <= _LAZY_STREAM_BAND_ROWS:
                return reader(cs, y0, y1, x0, x1)
            probe = np.empty((self.shape[0], 0, 0), dtype=self.dtype)[cs]
            out = np.empty((*probe.shape[:-2], y1 - y0, x1 - x0), dtype=self.dtype)
            for ry0 in range(y0, y1, _LAZY_STREAM_BAND_ROWS):
                ry1 = min(y1, ry0 + _LAZY_STREAM_BAND_ROWS)
                out[..., ry0 - y0 : ry1 - y0, :] = reader(cs, ry0, ry1, x0, x1)
            return out

        bh, bw = self._base_h, self._base_w
        ky = min(d, bh)
        kx = min(d, bw)
        band_out = max(1, _LAZY_STREAM_BAND_ROWS // max(1, ky))
        probe = np.empty((self.shape[0], 0, 0), dtype=self.dtype)[cs]
        out = np.empty((*probe.shape[:-2], y1 - y0, x1 - x0), dtype=self.dtype)
        for ry0 in range(y0, y1, band_out):
            ry1 = min(y1, ry0 + band_out)
            raw = reader(cs, ry0 * ky, min(bh, ry1 * ky), x0 * kx, min(bw, x1 * kx))
            part = _box_mean_axes(raw, ky, kx, self.dtype)
            out[..., ry0 - y0 : ry1 - y0, :] = part
        return out

    def _whole(self) -> np.ndarray:
        with self._lock:
            if self._memo is None:
                c, h, w = self.shape
                memo = np.empty((c, h, w), dtype=self.dtype)
                for r0 in range(0, h, _MEMO_BAND_ROWS):
                    r1 = min(h, r0 + _MEMO_BAND_ROWS)
                    memo[:, r0:r1, :] = self._reduce_window(slice(None), r0, r1, 0, w)
                self._memo = memo
            return self._memo

    def release(self) -> None:
        self._memo = None
        self._read = None

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        with self._life:
            if x0 == x1 or y0 == y1:
                return np.empty((self.shape[0], y1 - y0, x1 - x0), dtype=self.dtype)[cs]
            if self._memo_eligible:
                return self._whole()[:, y0:y1, x0:x1][cs].copy()
            return self._reduce_window(cs, y0, y1, x0, x1)


def _series_2d_to_cyx(series: Any, layout: _ParsedSeriesLayout, *, z_idx: int = 0, t_idx: int = 0) -> np.ndarray:
    """Decode one 2D (C, H, W) plane from a TiffPageSeries in native byte order."""
    axes = layout.axes
    shape = layout.shape
    page0 = _page_kf(series.pages[0])
    page_ndim = len(page0.shape)
    lead_axes = axes[: len(axes) - page_ndim]
    lead_shape = shape[: len(shape) - page_ndim]
    n_lead = math.prod(lead_shape) if lead_shape else 1

    if lead_axes and len(series.pages) == n_lead and shape[len(lead_shape) :] == tuple(page0.shape):
        if "C" in lead_axes:
            planes = []
            for ci in range(layout.n_c):
                coords = []
                for ax in lead_axes:
                    if ax == "T":
                        coords.append(t_idx)
                    elif ax == "Z":
                        coords.append(z_idx)
                    elif ax == "C":
                        coords.append(ci)
                    else:
                        coords.append(0)
                p_flat = int(np.ravel_multi_index(coords, lead_shape))
                arr_p = np.asarray(series.pages[p_flat].asarray())
                if arr_p.dtype == np.bool_:
                    arr_p = arr_p.astype(np.uint8) * 255
                planes.append(np.asarray(arr_p, dtype=layout.dtype))
            return np.ascontiguousarray(np.stack(planes, axis=0))

        coords = []
        for ax in lead_axes:
            if ax == "T":
                coords.append(t_idx)
            elif ax == "Z":
                coords.append(z_idx)
            else:
                coords.append(0)
        p_flat = int(np.ravel_multi_index(coords, lead_shape))
        raw = np.asarray(series.pages[p_flat].asarray())
        return _page_array_to_cyx(raw, axes[len(lead_axes) :], layout)

    # Single-IFD contiguous hyperstack (e.g., ImageJ raw dataoffset) or single-page 2D
    dataoffset = getattr(series, "dataoffset", None)
    bps0 = getattr(page0, "bitspersample", None)
    bps0_tuple = (bps0,) if isinstance(bps0, int) else tuple(bps0 or ())
    if (
        dataoffset is not None
        and lead_axes
        and int(getattr(page0, "compression", 1) or 1) == 1
        and int(getattr(page0, "predictor", 1) or 1) == 1
        and int(getattr(page0, "fillorder", 1) or 1) == 1
        and layout.raw_dtype != np.dtype(np.bool_)
        and bool(bps0_tuple)
        and all(int(b) == layout.raw_dtype.itemsize * 8 for b in bps0_tuple)
    ):
        frame_axes = axes[len(lead_axes) :]
        frame_shape = shape[len(lead_shape) :]
        elem_bytes = int(math.prod(frame_shape)) * layout.raw_dtype.itemsize
        byteorder = getattr(getattr(series, "parent", None), "byteorder", "=") or "="
        disk_dt = layout.raw_dtype.newbyteorder(byteorder)
        fh = series.parent.filehandle
        file_size = fh.size
        if "C" in lead_axes:
            planes = []
            for ci in range(layout.n_c):
                coords = [
                    t_idx if ax == "T" else z_idx if ax == "Z" else ci if ax == "C" else 0
                    for ax in lead_axes
                ]
                idx_flat = int(np.ravel_multi_index(coords, lead_shape))
                off = int(dataoffset) + idx_flat * elem_bytes
                if off < 0 or elem_bytes <= 0 or elem_bytes > _MAX_STACK_FRAME_BYTES or off + elem_bytes > file_size:
                    raise ValueError(
                        f"TIFF contiguous plane ({elem_bytes} B @ {off}) exceeds file bounds"
                    )
                fh.seek(off)
                buf = fh.read(elem_bytes)
                if len(buf) != elem_bytes:
                    raise ValueError("truncated TIFF contiguous plane read")
                arr_p = np.frombuffer(buf, dtype=disk_dt).reshape(frame_shape)
                planes.append(np.asarray(arr_p, dtype=layout.dtype))
            return np.ascontiguousarray(np.stack(planes, axis=0))
        coords = [t_idx if ax == "T" else z_idx if ax == "Z" else 0 for ax in lead_axes]
        idx_flat = int(np.ravel_multi_index(coords, lead_shape))
        off = int(dataoffset) + idx_flat * elem_bytes
        if off < 0 or elem_bytes <= 0 or elem_bytes > _MAX_STACK_FRAME_BYTES or off + elem_bytes > file_size:
            raise ValueError(
                f"TIFF contiguous plane ({elem_bytes} B @ {off}) exceeds file bounds"
            )
        fh.seek(off)
        buf = fh.read(elem_bytes)
        if len(buf) != elem_bytes:
            raise ValueError("truncated TIFF contiguous plane read")
        raw = np.frombuffer(buf, dtype=disk_dt).reshape(frame_shape)
        return _page_array_to_cyx(raw, frame_axes, layout)

    # Fallback: decode full series and index (t, z)
    full = np.asarray(series.asarray())
    slc: list[Any] = []
    rem_axes = ""
    for ax in axes:
        if ax == "T":
            slc.append(t_idx)
        elif ax == "Z":
            slc.append(z_idx)
        else:
            slc.append(slice(None))
            rem_axes += ax
    sub = full[tuple(slc)]
    return _page_array_to_cyx(sub, rem_axes, layout)


def _page_array_to_cyx(arr: np.ndarray, axes: str, layout: _ParsedSeriesLayout) -> np.ndarray:
    if arr.dtype == np.bool_:
        arr = arr.astype(np.uint8) * 255
    # Squeeze any unit axes outside C/S/Y/X, plus any singleton C/S when both exist.
    for i in range(len(axes) - 1, -1, -1):
        if axes[i] not in ("C", "S", "Y", "X") and arr.shape[i] == 1:
            arr = np.squeeze(arr, axis=i)
            axes = axes[:i] + axes[i + 1 :]
    if "C" in axes and "S" in axes:
        c_pos = axes.index("C")
        s_pos = axes.index("S")
        drop = s_pos if arr.shape[s_pos] == 1 else (c_pos if arr.shape[c_pos] == 1 else None)
        if drop is not None:
            arr = np.squeeze(arr, axis=drop)
            axes = axes[:drop] + axes[drop + 1 :]
    if axes == "YX":
        cyx = arr[None, :, :]
    elif axes in ("CYX", "SYX"):
        cyx = arr[: layout.n_c]
    elif axes in ("YXS", "YXC"):
        cyx = np.moveaxis(arr[..., : layout.n_c], -1, 0)
    else:
        y_pos = axes.index("Y")
        x_pos = axes.index("X")
        c_pos = next((i for i, a in enumerate(axes) if a in ("C", "S")), None)
        if c_pos is None:
            cyx = np.moveaxis(arr, (y_pos, x_pos), (0, 1))[None, :, :]
        else:
            cyx = np.moveaxis(arr, (c_pos, y_pos, x_pos), (0, 1, 2))[: layout.n_c]
    return np.ascontiguousarray(cyx, dtype=layout.dtype)


def _compute_default_windows(
    sample_cyx: np.ndarray,
    dtype: np.dtype,
    rgb: bool,
    *,
    raw_bps: int | None = None,
) -> list[dict[str, float]]:
    if rgb and np.issubdtype(dtype, np.integer):
        hi = float(np.iinfo(dtype).max)
        end_val = (
            float((1 << raw_bps) - 1)
            if dtype == np.dtype(np.uint16) and isinstance(raw_bps, int) and 0 < raw_bps < 16
            else hi
        )
        return [{"start": 0.0, "end": end_val, "min": 0.0, "max": hi} for _ in range(sample_cyx.shape[0])]

    if np.issubdtype(dtype, np.integer):
        lo_bound, hi_bound = float(np.iinfo(dtype).min), float(np.iinfo(dtype).max)
    else:
        finite = sample_cyx[np.isfinite(sample_cyx)]
        if finite.size:
            lo_bound, hi_bound = float(finite.min()), float(finite.max())
        else:
            lo_bound, hi_bound = 0.0, 1.0
        if hi_bound <= lo_bound:
            hi_bound = lo_bound + 1.0

    if rgb:
        start, end = auto_window(sample_cyx, dtype=dtype, rgb=True)
        return [
            {
                "start": start,
                "end": end,
                "min": min(lo_bound, start),
                "max": max(hi_bound, end),
            }
            for _ in range(sample_cyx.shape[0])
        ]

    windows: list[dict[str, float]] = []
    for ci in range(sample_cyx.shape[0]):
        start, end = auto_window(sample_cyx[ci], dtype=dtype)
        windows.append(
            {
                "start": start,
                "end": end,
                "min": min(lo_bound, start),
                "max": max(hi_bound, end),
            }
        )
    return windows


def _tiled_level_reader_for(
    lv_obj: Any,
    fd: int,
    cache: _TileCache,
    tag: Any,
    layout: _ParsedSeriesLayout,
) -> tuple[Any, int, int] | None:
    """Build a positional tile reader for one pyramid level object if tiled."""
    pages = list(getattr(lv_obj, "pages", None) or [])
    if not pages or not getattr(pages[0], "is_tiled", False):
        return None
    p0 = pages[0]
    lh, lw = int(p0.imagelength), int(p0.imagewidth)
    if np.dtype(p0.dtype).newbyteorder("=") != layout.raw_dtype.newbyteorder("="):
        return None
    planar = int(getattr(p0, "planarconfig", 1))
    samples = int(getattr(p0, "samplesperpixel", 1))

    if len(pages) == 1 and planar == 1 and samples >= layout.n_c:
        reader = _contig_tile_reader(p0, fd, cache, tag, layout.dtype, layout.n_c)
        return reader, lh, lw
    if len(pages) == 1 and planar == 2 and samples >= layout.n_c:
        reader = _planar_tile_reader([p0], fd, cache, tag, layout.dtype, layout.n_c)
        return reader, lh, lw
    if len(pages) >= layout.n_c and all(
        getattr(_page_kf(p), "is_tiled", False)
        and int(_page_kf(p).imagelength) == lh
        and int(_page_kf(p).imagewidth) == lw
        and int(getattr(_page_kf(p), "samplesperpixel", 1)) == 1
        for p in pages[: layout.n_c]
    ):
        reader = _planar_tile_reader(pages[: layout.n_c], fd, cache, tag, layout.dtype, layout.n_c)
        return reader, lh, lw
    return None


def _extract_raw_bps(page_kf: Any) -> int | None:
    """Extract scalar BitsPerSample from a TiffPage keyframe if available."""
    raw_bps = getattr(page_kf, "bitspersample", None)
    if isinstance(raw_bps, (tuple, list)):
        raw_bps = raw_bps[0] if raw_bps else None
    return raw_bps if isinstance(raw_bps, int) else None


def _discover_embedded_tiled_levels(
    level_objs: list[Any],
    fd: int,
    cache: _TileCache,
    owner: str,
    ident: int,
    height: int,
    shapes: list[tuple[int, int]],
    layout: _ParsedSeriesLayout,
    *,
    reader_builder: Any = None,
) -> dict[int, tuple[Any, int, int]]:
    """Index embedded tiled SubIFD / multi-page levels that align with the 2^k halving ladder."""
    build_reader = _tiled_level_reader_for if reader_builder is None else reader_builder
    embedded: dict[int, tuple[Any, int, int]] = {}
    for lv_obj in level_objs:
        p0 = _page_kf(lv_obj.pages[0])
        lh, lw = int(p0.imagelength), int(p0.imagewidth)
        d = max(1, round(height / max(1, lh)))
        k = d.bit_length() - 1
        on_ladder = (
            2**k == d
            and k < len(shapes)
            and abs(shapes[k][0] - lh) <= 2
            and abs(shapes[k][1] - lw) <= 2
        )
        if not on_ladder or k in embedded:
            continue
        built = build_reader(lv_obj, fd, cache, (owner, ident, k), layout)
        if built is not None:
            embedded[k] = built
    return embedded


def _build_tiled_pyramid_levels(
    embedded: dict[int, tuple[Any, int, int]],
    shapes: list[tuple[int, int]],
    layout: _ParsedSeriesLayout,
    life: _Lifecycle,
    *,
    tiled_level_cls: type[_OmeTiledLevel] = _OmeTiledLevel,
    coarse_level_cls: type[_CoarseMemoLevel] = _CoarseMemoLevel,
) -> tuple[dict[str, Any], list[Any], list[tuple[int, int]]]:
    """Construct the halving-ladder pyramid levels from indexed embedded tiled levels."""
    levels: dict[str, Any] = {}
    memo_levels: list[Any] = []
    served: list[tuple[int, int]] = []
    for k in range(len(shapes)):
        src_k = max(j for j in embedded if j <= k)
        e = 2 ** (k - src_k)
        reader_fn, sh, sw = embedded[src_k]
        if e <= MAX_EXTRA:
            h_k, w_k = max(1, sh // e), max(1, sw // e)
            levels[str(k)] = tiled_level_cls(
                reader_fn,
                layout.n_c,
                h_k,
                w_k,
                e,
                life,
                layout.dtype,
                src_h=sh,
                src_w=sw,
            )
            served.append((h_k, w_k))
        else:
            # Chain from the previous level via bounded band memoization
            prev_lv = levels[str(k - 1)]
            h_k, w_k = max(1, prev_lv.shape[1] // 2), max(1, prev_lv.shape[2] // 2)
            coarse = coarse_level_cls(prev_lv, h_k, w_k, 2, life)
            levels[str(k)] = coarse
            memo_levels.append(coarse)
            served.append((h_k, w_k))
    return levels, memo_levels, served


def _can_lazy_stream_untiled(
    page0_kf: Any,
    height: int,
    width: int,
    layout: _ParsedSeriesLayout,
    *,
    max_cached_strip_bytes: int | None = None,
) -> bool:
    """True when an untiled 2D TIFF can safely stream strips/rows lazily without thrashing."""
    limit = _MAX_CACHED_STRIP_BYTES if max_cached_strip_bytes is None else max_cached_strip_bytes
    comp0 = int(getattr(page0_kf, "compression", 1) or 1)
    rps0 = max(1, min(int(getattr(page0_kf, "rowsperstrip", height) or height), height))
    planar0 = int(getattr(page0_kf, "planarconfig", 1) or 1)
    spp0 = 1 if planar0 == 2 else int(getattr(page0_kf, "samplesperpixel", 1) or 1)
    strip_bytes0 = rps0 * width * spp0 * max(layout.raw_dtype.itemsize, layout.dtype.itemsize)
    return (comp0 == 1) or (strip_bytes0 <= limit)


def _build_direct_plane_attrs(
    *,
    layout: _ParsedSeriesLayout,
    channels: list[ChannelInfo],
    pixel_size_um: tuple[float, float] | None,
    source_name: str,
    selection: dict[str, Any],
    notes: list[str],
    magnification: float | None,
    cal_source: str,
    shapes: list[tuple[int, int]],
    tile: int,
    windows: list[dict[str, float]],
) -> dict[str, Any]:
    """Build NGFF/nd2wsi group attrs for a direct-served 2D TIFF pyramid."""
    from .convert import build_group_attrs

    src_info = PlaneSource(
        data=None,
        dtype=layout.dtype,
        shape=(layout.n_c, layout.height, layout.width),
        rgb=layout.rgb,
        channels=channels,
        pixel_size_um=pixel_size_um,
        source_name=source_name,
        selection=selection,
        notes=notes,
        magnification=magnification,
        calibration_source=cal_source if pixel_size_um else "unknown",
    )
    attrs = build_group_attrs(src_info, shapes, tile, windows)
    attrs["nd2wsi"]["direct"] = True
    return attrs
