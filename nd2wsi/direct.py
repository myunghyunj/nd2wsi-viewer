"""Serve an SVS with no pyramid store at all.

An Aperio file already carries a pyramid (typically 1x, 4x, 16x, 32x), and
the parallel tile reader in :mod:`nd2wsi.svs` decodes it fast enough to
feed the viewer straight from the file. This module wraps those embedded
levels in the halving ladder the viewer expects. Ladder levels the file
holds are decoded directly, and the gaps between them (2x, 8x, and the
tail below the deepest embedded level) are box-means of the next finer
embedded level, computed per request. A bytes-bounded cache of decoded
native tiles keeps revisited fields instant.

Nothing is written to disk. Opening is immediate, and the trash button has
nothing to do.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from .direct_common import (
    _TILE_CACHE as _TILE_CACHE,
)

# Preserve existing imports while format-neutral consumers use direct_common.
from .direct_common import (
    MAX_EXTRA as MAX_EXTRA,
)
from .direct_common import (
    TILE_CACHE_BYTES as TILE_CACHE_BYTES,
)
from .direct_common import (
    _Lifecycle as _Lifecycle,
)
from .direct_common import (
    _parse_cyx_index as _parse_cyx_index,
)
from .direct_common import (
    _Root as _Root,
)
from .direct_common import (
    _SvsLevel as _SvsLevel,
)
from .direct_common import (
    _TileCache as _TileCache,
)
from .reader import ChannelInfo, PlaneSource, level_shapes
from .svs import _aperio_meta, _require_imagecodecs, _window_reader


def _cached_reader(page: Any, fd: int, cache: _TileCache, tag: Any):
    """A :func:`_window_reader` whose native-tile decodes go through
    ``cache``. Same window semantics, shared across server threads."""
    raw = _window_reader(page, fd)
    th, tw = page.tilelength, page.tilewidth

    def read(y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        # aligned single-tile windows are the cacheable unit
        key = (tag, y0 // th, x0 // tw)
        if (y1 - y0, x1 - x0) == (th, tw) and y0 % th == 0 and x0 % tw == 0:
            hit = cache.get(key)
            if hit is not None:
                return hit
            block = raw(y0, y1, x0, x1)
            cache.put(key, block)
            return block
        # larger windows assemble from cached tiles
        out = None
        for ty in range(y0 // th, -(-y1 // th)):
            for tx in range(x0 // tw, -(-x1 // tw)):
                t = read(ty * th, (ty + 1) * th, tx * tw, (tx + 1) * tw)
                if out is None:
                    out = np.zeros((y1 - y0, x1 - x0, t.shape[2]), t.dtype)
                oy0, oy1 = max(y0, ty * th), min(y1, (ty + 1) * th)
                ox0, ox1 = max(x0, tx * tw), min(x1, (tx + 1) * tw)
                out[oy0 - y0 : oy1 - y0, ox0 - x0 : ox1 - x0] = t[
                    oy0 - ty * th : oy1 - ty * th, ox0 - tx * tw : ox1 - tx * tw
                ]
        return out if out is not None else raw(y0, y1, x0, x1)

    return read


class _Nd2Level0:
    """The slide's own pixels as level 0, sliced like a (C, Y, X) array.

    Wraps the zero-copy strided view :func:`nd2wsi.reader.plane_view`
    returns. Every read copies its window out of the memory map inside
    the lifecycle guard — ``.copy()``, never ``ascontiguousarray``, which
    hands the mapped view itself back for contiguous slices — so nothing
    mapped survives past ``close``. Each read also re-stats the source:
    a file changed or unreachable raises a clean error instead of
    serving garbage, and a vanished volume fails before the map is
    touched in almost every case.
    """

    def __init__(self, view: np.ndarray, life: _Lifecycle, path: str | Path):
        self._view = view
        self._life = life
        self._path = str(path)
        st = os.stat(path)
        self._ident = (st.st_size, st.st_mtime_ns)
        self.shape = tuple(view.shape)
        self.dtype = view.dtype

    def __getitem__(self, idx: Any) -> np.ndarray:
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        with self._life:
            try:
                st = os.stat(self._path)
            except OSError as e:
                raise ValueError(
                    f"the source file is unreachable ({e.strerror}); "
                    "close and reopen the slide"
                ) from e
            if (st.st_size, st.st_mtime_ns) != self._ident:
                raise ValueError(
                    "the source file changed while it was open; "
                    "close and reopen the slide"
                )
            return self._view[cs, y0:y1, x0:x1].copy()


class _PersistedHLevel0:
    """Correct native display pixels while retaining a separate RAW adapter."""

    def __init__(self, raw, model, life):
        self.raw, self.model, self._life = raw, model, life
        self.shape, self.dtype = raw.shape, np.dtype("float32")
        self._enabled = {c for c, enabled in zip(model.indices, model.enabled) if enabled}

    def __getitem__(self, idx):
        cs, y0, y1, x0, x1 = _parse_cyx_index(idx, self.shape)
        with self._life:
            out = np.asarray(self.raw[cs, y0:y1, x0:x1], dtype=np.float32)
            selected = np.arange(self.shape[0])[cs]
            scalar = np.ndim(selected) == 0
            result = out[None, ...] if scalar else out
            for slot, channel in enumerate(np.atleast_1d(selected)):
                if int(channel) in self._enabled and y1 > y0 and x1 > x0:
                    result[slot] = self.model.correct(int(channel), result[slot], y0, x0)
            return out


def open_direct(path: str | Path) -> tuple[Any, dict[str, Any]]:
    """(virtual root, attrs) for an SVS, shaped like ``open_store``'s.

    Raises ``NotImplementedError`` for files this cannot serve well -- an
    untiled baseline, an embedded pyramid that is missing or off the
    power-of-two ladder, or one so sparse that a gap level would have to
    decode a huge span per request. Callers fall back to a full
    conversion for those, which is what every SVS got before 0.7.0.
    """
    import tifffile

    from .convert import build_group_attrs

    _require_imagecodecs()
    path = Path(path)
    tf = tifffile.TiffFile(str(path))
    fd = -1
    try:
        series = tf.series[0]
        if series.ndim != 3 or series.shape[-1] not in (3, 4):
            raise NotImplementedError(f"unexpected SVS layout {series.shape}")
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))

        H, W = int(series.shape[0]), int(series.shape[1])
        shapes = level_shapes(H, W, 512)
        cache = _TILE_CACHE
        owner = str(path)
        # a replaced file must never alias tiles a previous open decoded:
        # sweep anything the deferred close has not purged yet, and fold
        # the file's identity into every new key
        cache.purge(owner)
        ident = path.stat().st_mtime_ns
        life = _Lifecycle()

        # every embedded level that is tiled and sits on the halving ladder
        embedded: dict[int, Any] = {}
        for lv in series.levels:
            page = lv.pages[0]
            if not getattr(page, "is_tiled", False):
                continue
            d = max(1, round(H / lv.shape[0]))
            k = d.bit_length() - 1
            on_ladder = (
                2**k == d
                and k < len(shapes)
                and abs(shapes[k][0] - lv.shape[0]) <= 2
                and abs(shapes[k][1] - lv.shape[1]) <= 2
            )
            if on_ladder and k not in embedded:
                embedded[k] = (page, int(lv.shape[0]), int(lv.shape[1]))

        if 0 not in embedded:
            raise NotImplementedError("SVS baseline level is not tiled")
        if len(shapes) > 1 and len(embedded) == 1:
            raise NotImplementedError("SVS carries no usable embedded pyramid")

        readers = {
            k: _cached_reader(page, fd, cache, (owner, ident, k))
            for k, (page, _, _) in embedded.items()
        }
        root: dict[str, _SvsLevel] = {}
        served: list[tuple[int, int]] = []
        for k in range(len(shapes)):
            src = max(j for j in embedded if j <= k)
            e = 2 ** (k - src)
            if e > MAX_EXTRA:
                raise NotImplementedError(
                    f"embedded pyramid too sparse (level {k} would need "
                    f"a {e}x downsample per request)"
                )
            sh, sw = embedded[src][1], embedded[src][2]
            # serve exactly what the source holds, so a read scaled by
            # ``e`` can never cross the source edge or its tile grid
            h, w = sh // e, sw // e
            root[str(k)] = _SvsLevel(readers[src], h, w, e, life)
            served.append((h, w))
        shapes = served

        meta = _aperio_meta(tf.pages[0].description or "")
    except Exception:
        tf.close()
        if fd >= 0:
            os.close(fd)
        raise
    mpp = meta.get("mpp")
    calibrated = mpp is not None and mpp > 0
    src_info = PlaneSource(
        data=None,
        dtype=np.dtype(np.uint8),
        shape=(3, H, W),
        rgb=True,
        channels=[
            ChannelInfo("Red", (255, 0, 0)),
            ChannelInfo("Green", (0, 255, 0)),
            ChannelInfo("Blue", (0, 0, 255)),
        ],
        pixel_size_um=(float(mpp), float(mpp)) if calibrated else None,
        source_name=str(path),
        selection={},
        notes=[f"served straight from the SVS ({len(embedded)} embedded levels)"],
        magnification=meta.get("magnification"),
        calibration_source="aperio-mpp" if calibrated else "unknown",
    )
    windows = [{"start": 0.0, "end": 255.0, "min": 0.0, "max": 255.0}] * 3
    attrs = build_group_attrs(src_info, shapes, 512, windows)
    attrs["nd2wsi"]["direct"] = True

    def _teardown() -> None:
        if not life.close():
            return  # a read still holds the fd; leaking beats EBADF under it
        tf.close()
        os.close(fd)
        _TILE_CACHE.purge(owner)

    return _Root(root, _teardown), attrs


def open_nd2_backed(
    slide: str | Path, store: str | Path
) -> tuple[Any, dict[str, Any]]:
    """(virtual root, attrs) serving level 0 from the ND2 file itself.

    ``store`` is an overview pyramid holding levels 1..n; the file's own
    memory map plays level 0. Raises ``NotImplementedError`` when the
    file cannot back a level at runtime — the caller falls back to a full
    store or a degraded overview-only view.
    """
    import copy

    import nd2

    from .cache import manifest_container, read_manifest
    from .convert import open_store
    from .reader import PlaneSelection, is_source_backable, plane_view

    slide, store = Path(slide), Path(store)
    container = manifest_container(store)
    manifest = read_manifest(container) if container is not None else {}
    manifest = manifest or {}
    sel = manifest.get("selection", {})
    selection = PlaneSelection(
        t=int(sel.get("t", 0)),
        p=int(sel.get("p", 0)),
        z=sel.get("z_resolved", sel.get("z", "mid")),
    )

    f = nd2.ND2File(str(slide))
    zroot = model = None
    try:
        ok, why = is_source_backable(f, selection)
        if not ok:
            raise NotImplementedError(f"{slide.name}: {why}")
        view, _ = plane_view(f, selection)

        zroot, stored_attrs = open_store(store)
        attrs = copy.deepcopy(stored_attrs)
        meta = attrs.get("nd2wsi") or {}
        ov = meta.get("overview_of")
        if meta.get("kind") != "overview" or not ov:
            raise NotImplementedError(f"{store} is not an overview store")
        c, h, w = view.shape
        if [c, h, w] != [ov["channels"], ov["height"], ov["width"]]:
            raise NotImplementedError(
                f"{slide.name} no longer matches its overview "
                f"({w} x {h} vs {ov['width']} x {ov['height']})"
            )

        life = _Lifecycle()
        raw_level0 = _Nd2Level0(view, life, slide)
        levels: dict[str, Any] = {"0": raw_level0}
        if (manifest.get("format") == "nd2wsi-cache/5"
                or manifest.get("h_correction") is not None or meta.get("h_correction") is not None):
            from .cache import fingerprints_match, quick_fingerprint
            from .h_persistence import load_model

            if meta.get("h_correction") != manifest.get("h_correction"):
                raise ValueError("Stored and native correction provenance disagree")
            if not fingerprints_match(manifest.get("source", {}), quick_fingerprint(slide)):
                raise ValueError("The original ND2 no longer matches its corrected cache")
            model = load_model(store, manifest)
            levels["0"] = _PersistedHLevel0(raw_level0, model, life)
            meta["h_correction"] = manifest["h_correction"]
            meta["display_dtype"] = "float32"
        for lv in meta["levels"]:
            levels[lv["path"]] = zroot[lv["path"]]

        meta["levels"] = [
            {"path": "0", "width": w, "height": h, "downsample": 1}
        ] + meta["levels"]
        meta["kind"] = "source-backed"
        for ms in attrs.get("multiscales", []):
            first = ms["datasets"][0]["coordinateTransformations"][0]["scale"]
            ms["datasets"].insert(
                0,
                {
                    "path": "0",
                    "coordinateTransformations": [
                        {"type": "scale", "scale": [1.0, first[1] / 2, first[2] / 2]}
                    ],
                },
            )
        attrs["nd2wsi"] = meta
    except BaseException:
        try:
            f.close()
        finally:
            if model is not None:
                model.data.close()
            close = getattr(zroot, "close", None)
            if close is not None:
                close()
        raise

    def _teardown() -> None:
        if not life.close():
            # a reader still holds the map after the drain timeout.
            # nd2's frames are ndarray(buffer=mmap) views, which raise no
            # BufferError on close — unmapping here would be a segfault in
            # that reader, so the handle leaks until process exit instead
            return
        try:
            f.close()
        except BufferError:  # pragma: no cover - defensive
            pass
        finally:
            if model is not None:
                model.data.close()
            close = getattr(zroot, "close", None)
            if close is not None:
                close()

    root = _Root(levels, _teardown)
    # Pixel probes and native ND2 exports use this adapter, never H(display).
    root.raw_level0 = raw_level0
    return root, attrs
