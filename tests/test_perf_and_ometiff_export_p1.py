"""Phase 1 Track 2 tests: SQLite read pool, _RAW_CHUNK_CACHE, jpeg8_encode,
_Nd2Level0 stat coalescing, custom channel colors, LUT color persistence,
UI settings & recent files, OME-XML TIFF ROI export, and Plate Z-prefetch.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
import tifffile
import zarr
from PIL import Image

from nd2wsi import direct, plate, render, ui_settings
from nd2wsi.convert import build_group_attrs, open_store
from nd2wsi.direct_common import _Lifecycle
from nd2wsi.lut_settings import FORMAT as LUT_FORMAT
from nd2wsi.lut_settings import LutSettingsStore, validate_settings
from nd2wsi.reader import ChannelInfo, PlaneSource, level_shapes
from nd2wsi.storage.single_file import (
    _READ_POOL_SIZE,
    SingleFileStore,
    SQLiteStore,
    open_zarr_group,
)
from nd2wsi.window_sessions import create_window_session

OME_NS = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}


@pytest.fixture(autouse=True)
def _isolated_app_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    app_dir = tmp_path / "isolated-app-support"
    monkeypatch.setenv("ND2WSI_APP_SUPPORT_DIR", str(app_dir))
    monkeypatch.setenv("ND2WSI_UI_SETTINGS_ROOT", str(app_dir))
    monkeypatch.setenv("ND2WSI_LUT_SETTINGS_ROOT", str(app_dir / "lut-settings"))
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(app_dir / "window-sessions"))
    return app_dir


def _make_fluor_attrs(
    *,
    source_name: str = "sample.nd2",
    dtype: str = "uint16",
    width: int = 128,
    height: int = 128,
    tile: int = 64,
    channels: list[tuple[str, tuple[int, int, int]]] | None = None,
    pixel_size_um: tuple[float, float] | None = (0.325, 0.325),
    rgb: bool = False,
) -> dict:
    if channels is None:
        channels = [
            ("DAPI", (0, 0, 255)),
            ("FITC", (0, 255, 0)),
            ("TRITC", (255, 0, 0)),
        ]
    shapes = level_shapes(height, width, tile)
    np_dtype = np.dtype(dtype)
    hi = float(np.iinfo(np_dtype).max) if np.issubdtype(np_dtype, np.integer) else 1.0
    src = PlaneSource(
        data=None,
        dtype=np_dtype,
        shape=(len(channels), height, width),
        rgb=rgb,
        channels=[ChannelInfo(name, rgb_col) for name, rgb_col in channels],
        pixel_size_um=pixel_size_um,
        source_name=source_name,
        selection={"t": 0, "p": 0, "z": 0},
    )
    windows = [{"start": 0.0, "end": hi, "min": 0.0, "max": hi} for _ in channels]
    return build_group_attrs(src, shapes, tile, windows)


# ---------------------------------------------------------------------------
# 1. Read-Only SQLite Connection Pool (single_file.py)
# ---------------------------------------------------------------------------


def test_sqlite_read_pool_concurrent_reads_pragmas_and_clean_close(tmp_path: Path):
    db_path = tmp_path / "pooled.nd2svs"
    assert SingleFileStore is SQLiteStore

    payloads = {f"chunks/{i:03d}": (f"payload-{i:03d}-".encode() * 256) for i in range(48)}
    with SingleFileStore(db_path) as writer:
        with writer.batch():
            for k, v in payloads.items():
                writer.write_bytes(k, v)
        # Writable mode keeps conservative -8192 KiB cache_size and mmap_size=0
        assert writer._connection.execute("PRAGMA cache_size").fetchone()[0] == -8192
        assert writer._connection.execute("PRAGMA mmap_size").fetchone()[0] == 0

    reader = SingleFileStore(db_path, read_only=True)
    barrier = threading.Barrier(_READ_POOL_SIZE)
    observed_conns: list[int] = []
    obs_lock = threading.Lock()

    def concurrent_probe(worker_idx: int) -> None:
        with reader._read_conn() as conn:
            with obs_lock:
                observed_conns.append(id(conn))
            barrier.wait(timeout=5.0)
            assert conn.execute("PRAGMA cache_size").fetchone()[0] == -32768
            assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 0
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        for i in range(worker_idx, 48, 3):
            key = f"chunks/{i:03d}"
            assert reader.read_bytes(key) == payloads[key]

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(concurrent_probe, i) for i in range(_READ_POOL_SIZE)]
        futs.extend(
            pool.submit(lambda idx: reader.read_bytes(f"chunks/{idx % 48:03d}"), j)
            for j in range(48)
        )
        for f in futs:
            f.result(timeout=10.0)

    # Exactly 6 distinct read-only connections were opened under concurrency
    pooled_conns = list(reader._read_connections)
    assert len(set(observed_conns)) == _READ_POOL_SIZE == 6
    assert len(pooled_conns) == _READ_POOL_SIZE

    reader.close()
    assert reader._closed is True
    assert reader._connection is None
    assert reader._read_connections == []
    for conn in pooled_conns:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    with pytest.raises(ValueError, match="closed"):
        reader.read_bytes("chunks/000")

    # Closed handles allow clean file rename on all platforms
    renamed = tmp_path / "renamed.nd2svs"
    db_path.rename(renamed)
    assert renamed.is_file()


def test_open_zarr_group_readonly_concurrent_tile_reads(tmp_path: Path):
    nd2svs = tmp_path / "tiles.nd2svs"
    rng = np.random.default_rng(101)
    arr_data = rng.integers(0, 4095, size=(4, 128, 128), dtype=np.uint16)

    w_group = open_zarr_group(nd2svs, prefix="store.ome.zarr", mode="w")
    try:
        w_group.create_array("0", data=arr_data, chunks=(1, 64, 64))
    finally:
        w_group.store.close()

    r_group = open_zarr_group(nd2svs, prefix="store.ome.zarr", mode="r")
    try:
        lvl0 = r_group["0"]

        def fetch_tile(idx: int) -> np.ndarray:
            ty, tx = divmod(idx % 4, 2)
            return np.asarray(lvl0[:, ty * 64 : (ty + 1) * 64, tx * 64 : (tx + 1) * 64])

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(fetch_tile, range(32)))

        for idx, block in enumerate(results):
            ty, tx = divmod(idx % 4, 2)
            np.testing.assert_array_equal(
                block, arr_data[:, ty * 64 : (ty + 1) * 64, tx * 64 : (tx + 1) * 64]
            )
        assert 1 <= len(r_group.store._read_connections) <= _READ_POOL_SIZE
        conns_before_close = list(r_group.store._read_connections)
    finally:
        r_group.store.close()

    for conn in conns_before_close:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


# ---------------------------------------------------------------------------
# 2. _RAW_CHUNK_CACHE Hit & Invalidation + imagecodecs.jpeg8_encode + stat TTL
# ---------------------------------------------------------------------------


class _CountingLevel:
    def __init__(self, data: np.ndarray):
        self._data = data
        self.shape = data.shape
        self.dtype = data.dtype
        self.reads = 0
        self.generation = 1

    def __getitem__(self, idx):
        self.reads += 1
        return self._data[idx].copy()


def test_raw_chunk_cache_hits_on_lut_or_color_drag_and_invalidates_on_mutation(
    tmp_path: Path,
):
    render._RAW_CHUNK_CACHE.clear()
    raw = np.zeros((2, 64, 64), dtype=np.uint16)
    raw[0, :, :] = 40000
    raw[1, :, :] = 20000
    lvl = _CountingLevel(raw)
    attrs = _make_fluor_attrs(
        width=64,
        height=64,
        tile=64,
        channels=[("Ch0", (255, 0, 0)), ("Ch1", (0, 255, 0))],
    )
    attrs["nd2wsi"]["generation"] = "gen-1"
    root = {"0": lvl}

    b1 = render.render_tile(root, attrs, 0, 0, 0, [0, 1], fmt="png", win="0:65535,0:65535")
    assert lvl.reads == 1

    # Changing LUT window, channel visibility, or custom hex color hits _RAW_CHUNK_CACHE!
    b2 = render.render_tile(
        root, attrs, 0, 0, 0, [0], fmt="png", win="0:40000,0:65535", col="00FFFF,FF00FF"
    )
    assert lvl.reads == 1
    assert render._RAW_CHUNK_CACHE.hits >= 1
    img2 = np.asarray(Image.open(io.BytesIO(b2)))
    assert tuple(img2[0, 0]) == (0, 255, 255)
    assert b1 != b2

    # Mutation 1: Shading/pyramid generation change invalidates cached raw chunk
    lvl._data[0, :, :] = 0
    attrs["nd2wsi"]["generation"] = "gen-2"
    b3 = render.render_tile(root, attrs, 0, 0, 0, [0], fmt="png", win="0:40000,0:65535")
    assert lvl.reads == 2
    img3 = np.asarray(Image.open(io.BytesIO(b3)))
    assert tuple(img3[0, 0]) == (0, 0, 0)

    # Mutation 2: h_correction metadata mutation invalidates cached raw chunk
    lvl._data[0, :, :] = 40000
    attrs["nd2wsi"]["h_correction"] = {"applied": True, "method": "bffree"}
    b4 = render.render_tile(root, attrs, 0, 0, 0, [0], fmt="png", win="0:40000,0:65535")
    assert lvl.reads == 3
    img4 = np.asarray(Image.open(io.BytesIO(b4)))
    assert img4[0, 0, 0] == 255

    # Mutation 3: Explicit purge / invalidate drops cached entry
    lvl._data[0, :, :] = 10000
    render._RAW_CHUNK_CACHE.invalidate(lvl)
    _ = render.render_tile(root, attrs, 0, 0, 0, [0], fmt="png", win="0:40000,0:65535")
    assert lvl.reads == 4


def test_raw_chunk_cache_invalidates_on_sqlite_store_and_zarr_mutation(tmp_path: Path):
    render._RAW_CHUNK_CACHE.clear()
    cache_path = tmp_path / "mutable.nd2svs"
    attrs = _make_fluor_attrs(
        width=64, height=64, tile=64, channels=[("DAPI", (0, 0, 255))]
    )

    writer = open_zarr_group(cache_path, prefix="store.ome.zarr", mode="w")
    try:
        writer.attrs.update(attrs)
        writer.create_array("0", data=np.zeros((1, 64, 64), dtype=np.uint16), chunks=(1, 64, 64))
    finally:
        writer.store.close()

    root, loaded_attrs = open_store(cache_path)
    try:
        p1 = np.asarray(
            Image.open(io.BytesIO(render.render_tile(root, loaded_attrs, 0, 0, 0, [0], fmt="png")))
        )
        assert int(p1.max()) == 0

        # Mutate the underlying .nd2svs SQLiteStore through a separate writer handle
        mutator = open_zarr_group(cache_path, prefix="store.ome.zarr", mode="a")
        try:
            mutator["0"][:] = np.full((1, 64, 64), 65535, dtype=np.uint16)
        finally:
            mutator.store.close()

        # Subsequent render_tile must NOT serve stale zero chunk
        p2 = np.asarray(
            Image.open(io.BytesIO(render.render_tile(root, loaded_attrs, 0, 0, 0, [0], fmt="png")))
        )
        assert tuple(p2[0, 0]) == (0, 0, 255)
    finally:
        root.close()

    # Closed store must raise ValueError rather than serving from _RAW_CHUNK_CACHE
    with pytest.raises(ValueError, match="closed"):
        render.render_tile(root, loaded_attrs, 0, 0, 0, [0], fmt="png")

    # Directory Zarr (LocalStore) chunk overwrite also invalidates _RAW_CHUNK_CACHE
    dir_zarr_path = tmp_path / "dir_store.zarr"
    dir_grp = zarr.open_group(dir_zarr_path, mode="w", zarr_format=2)
    dir_grp.create_array("0", data=np.zeros((1, 64, 64), dtype=np.uint16), chunks=(1, 64, 64))
    d1 = np.asarray(
        Image.open(io.BytesIO(render.render_tile(dir_grp, attrs, 0, 0, 0, [0], fmt="png")))
    )
    assert int(d1.max()) == 0
    dir_grp["0"][:] = np.full((1, 64, 64), 65535, dtype=np.uint16)
    d2 = np.asarray(
        Image.open(io.BytesIO(render.render_tile(dir_grp, attrs, 0, 0, 0, [0], fmt="png")))
    )
    assert tuple(d2[0, 0]) == (0, 0, 255)


def test_encode_image_uses_imagecodecs_jpeg8_and_falls_back_to_pil(
    monkeypatch: pytest.MonkeyPatch,
):
    import imagecodecs

    calls: list[int] = []
    real_jpeg8 = imagecodecs.jpeg8_encode

    def spy_jpeg8(arr, *, level=None, **kw):
        calls.append(level)
        return real_jpeg8(arr, level=level, **kw)

    monkeypatch.setattr(imagecodecs, "jpeg8_encode", spy_jpeg8)
    rgb = np.zeros((48, 64, 3), dtype=np.uint8)
    rgb[:, :32, 1] = 220
    # Pass a non-contiguous slice to ensure contiguity handling
    sliced = rgb[:, ::2, :]
    assert not sliced.flags.c_contiguous
    jpg_bytes = render.encode_image(sliced, "jpg", quality=91)
    assert calls == [91]
    decoded = Image.open(io.BytesIO(jpg_bytes))
    assert decoded.size == (32, 48) and decoded.mode == "RGB"

    # Simulate imagecodecs unavailable -> PIL fallback still works cleanly
    monkeypatch.setattr(imagecodecs, "jpeg8_encode", None)
    fallback_bytes = render.encode_image(sliced, "jpeg", quality=85)
    decoded_fb = Image.open(io.BytesIO(fallback_bytes))
    assert decoded_fb.size == (32, 48) and decoded_fb.mode == "RGB"


def test_nd2_level0_coalesces_os_stat_within_ttl_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    slide = tmp_path / "coalesced.nd2"
    slide.write_bytes(b"synthetic-nd2-bytes")
    raw = np.arange(2 * 64 * 64, dtype=np.uint16).reshape(2, 64, 64)
    life = _Lifecycle()

    fake_now = [100.0]
    stat_calls: list[str] = []
    real_stat = os.stat

    def fake_monotonic() -> float:
        return fake_now[0]

    def counting_stat(p, *args, **kwargs):
        if str(p) == str(slide):
            stat_calls.append(str(p))
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(direct.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(direct.os, "stat", counting_stat)

    lvl0 = direct._Nd2Level0(raw, life, slide)
    stat_calls.clear()

    # 20 tile reads within 0.05s (< 0.1s TTL) hit os.stat only ONCE
    for i in range(20):
        fake_now[0] = 100.0 + i * 0.002
        out = lvl0[:, 0:16, 0:16]
        assert out.shape == (2, 16, 16)
    assert len(stat_calls) == 1

    # Advance past 0.1s TTL -> os.stat runs again and detects file mutation
    slide.write_bytes(b"modified-synthetic-nd2-bytes-longer")
    fake_now[0] = 100.15
    with pytest.raises(ValueError, match="changed"):
        _ = lvl0[:, 0:16, 0:16]
    assert len(stat_calls) == 2


# ---------------------------------------------------------------------------
# 3. Custom Channel Colors (col) & LUT Settings Color Persistence
# ---------------------------------------------------------------------------


def test_parse_colors_and_custom_hex_compositing():
    defaults = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
    assert render.parse_colors(None, defaults) == defaults
    assert render.parse_colors("", defaults) == defaults
    assert render.parse_colors("00FFFF,ff00ff,,BADHEX", defaults) == [
        (0, 255, 255),
        (255, 0, 255),
        (0, 0, 255),
        (255, 255, 255),
    ]
    assert render.parse_colors(["FFFF00", None, "#123456", "3B82F6"], defaults) == [
        (255, 255, 0),
        (0, 255, 0),
        (0, 0, 255),
        (59, 130, 246),
    ]

    region = np.zeros((2, 32, 32), dtype=np.uint16)
    region[0, :, :16] = 65535
    region[1, :, 16:] = 65535
    attrs = _make_fluor_attrs(
        width=32,
        height=32,
        tile=32,
        channels=[("C0", (255, 0, 0)), ("C1", (0, 255, 0))],
    )
    root = {"0": region}

    # Default colors (Red left, Green right)
    default_png = render.render_tile(root, attrs, 0, 0, 0, [0, 1], fmt="png")
    arr_def = np.asarray(Image.open(io.BytesIO(default_png)))
    assert tuple(arr_def[10, 4]) == (255, 0, 0)
    assert tuple(arr_def[10, 24]) == (0, 255, 0)

    # Custom colors via col="00FFFF,FF00FF" (Cyan left, Magenta right)
    custom_png = render.render_tile(
        root, attrs, 0, 0, 0, [0, 1], fmt="png", col="00FFFF,FF00FF"
    )
    arr_cus = np.asarray(Image.open(io.BytesIO(custom_png)))
    assert tuple(arr_cus[10, 4]) == (0, 255, 255)
    assert tuple(arr_cus[10, 24]) == (255, 0, 255)

    # Also verify render_frame_bytes and export_roi_rendered honor col
    frame_png = render.render_frame_bytes(
        region, attrs, [0, 1], fmt="png", col="FFFF00,"
    )
    arr_frame = np.asarray(Image.open(io.BytesIO(frame_png)))
    assert tuple(arr_frame[10, 4]) == (255, 255, 0)
    assert tuple(arr_frame[10, 24]) == (0, 255, 0)

    roi_png = render.export_roi_rendered(
        root, attrs, 0, 0, 0, 32, 32, [0, 1], "png", col=",3B82F6"
    )
    arr_roi = np.asarray(Image.open(io.BytesIO(roi_png)))
    assert tuple(arr_roi[10, 4]) == (255, 0, 0)
    assert tuple(arr_roi[10, 24]) == (59, 130, 246)


def test_lut_settings_color_roundtrip_and_backward_compatibility(tmp_path: Path):
    slide = tmp_path / "color_lut_slide.nd2"
    slide.write_bytes(b"source-slide-bytes-for-lut")
    attrs = _make_fluor_attrs(
        source_name=slide.name,
        channels=[("DAPI", (0, 0, 255)), ("FITC", (0, 255, 0)), ("CY5", (255, 0, 0))],
    )

    store = LutSettingsStore(slide, attrs)
    payload = {
        "format": LUT_FORMAT,
        "luts": [
            {"lo": 100, "hi": 4000, "gamma": 1.0, "color": "00ffff"},
            {"lo": 50, "hi": 3000, "gamma": 1.2, "color": ""},
            {"lo": 0, "hi": 65535, "gamma": 1.0, "color": None},
        ],
    }
    assert store.write(payload) == {"ok": True}

    reopened = LutSettingsStore(slide, attrs)
    restored = reopened.read()
    assert restored == {
        "ok": True,
        "available": True,
        "settings": {
            "format": LUT_FORMAT,
            "luts": [
                {"lo": 100, "hi": 4000, "gamma": 1.0, "color": "00FFFF"},
                {"lo": 50, "hi": 3000, "gamma": 1.2},
                {"lo": 0, "hi": 65535, "gamma": 1.0},
            ],
        },
    }

    # Legacy saved .json without "color" keys loads cleanly
    raw_record = json.loads(store.path.read_text(encoding="utf-8"))
    raw_record["settings"]["luts"] = [
        {"lo": 10, "hi": 1000, "gamma": 1.0},
        {"lo": 20, "hi": 2000, "gamma": 1.0},
        {"lo": 30, "hi": 3000, "gamma": 1.0},
    ]
    store.path.write_text(json.dumps(raw_record), encoding="utf-8")
    legacy_loaded = LutSettingsStore(slide, attrs).read()
    assert legacy_loaded["settings"]["luts"] == raw_record["settings"]["luts"]

    # Invalid color strings / types are rejected without overwriting disk
    for bad_color in ["#00FFFF", "FFF", "GGGGGG", 123456, True, ["00FFFF"]]:
        with pytest.raises(ValueError):
            validate_settings(
                {
                    "format": LUT_FORMAT,
                    "luts": [
                        {"lo": 0, "hi": 1000, "gamma": 1.0, "color": bad_color},
                        {"lo": 0, "hi": 1000, "gamma": 1.0},
                        {"lo": 0, "hi": 1000, "gamma": 1.0},
                    ],
                },
                attrs["omero"]["channels"],
                "uint16",
            )

    # Agent session isolates custom color edits away from user LUT settings
    user_sess = create_window_session("user", tmp_path / "sessions")
    agent_sess = create_window_session("agent", tmp_path / "sessions")
    try:
        user_lut = LutSettingsStore(slide, attrs, user_sess)
        user_lut.write(
            {
                "format": LUT_FORMAT,
                "luts": [
                    {"lo": 0, "hi": 5000, "gamma": 1.0, "color": "00FF00"},
                    {"lo": 0, "hi": 5000, "gamma": 1.0},
                    {"lo": 0, "hi": 5000, "gamma": 1.0},
                ],
            }
        )
        agent_lut = LutSettingsStore(slide, attrs, agent_sess)
        assert agent_lut.read()["settings"]["luts"][0]["color"] == "00FF00"
        agent_lut.write(
            {
                "format": LUT_FORMAT,
                "luts": [
                    {"lo": 0, "hi": 5000, "gamma": 1.0, "color": "FF00FF"},
                    {"lo": 0, "hi": 5000, "gamma": 1.0},
                    {"lo": 0, "hi": 5000, "gamma": 1.0},
                ],
            }
        )
        assert user_lut.read()["settings"]["luts"][0]["color"] == "00FF00"
        assert agent_lut.read()["settings"]["luts"][0]["color"] == "FF00FF"
    finally:
        agent_sess.mark_closed()
        user_sess.mark_closed()


# ---------------------------------------------------------------------------
# 4. Backend UI Settings & Recent Files (ui_settings.py)
# ---------------------------------------------------------------------------


def test_ui_settings_atomic_merge_validation_and_thread_safety(tmp_path: Path):
    # Initial load creates no files and returns defaults
    assert not ui_settings.ui_settings_path().exists()
    assert ui_settings.load_ui_settings() == {
        "theme": "auto",
        "panels": {},
        "shellToolsExpanded": None,
    }

    # Invalid patches raise ValueError without creating ui-settings.json
    for bad_patch in [
        {"theme": "neon"},
        {"shellToolsExpanded": "yes"},
        {"panels": "not-a-dict"},
        {"panels": {"channels": {"x": float("nan")}}},
    ]:
        with pytest.raises(ValueError):
            ui_settings.save_ui_settings(bad_patch)
    assert not ui_settings.ui_settings_path().exists()

    # Agent window_role does not write to user ui-settings.json
    agent_view = ui_settings.save_ui_settings({"theme": "dark"}, window_role="agent")
    assert agent_view["theme"] == "dark"
    assert not ui_settings.ui_settings_path().exists()

    # User updates merge atomically across keys and panel entries
    saved1 = ui_settings.save_ui_settings(
        {
            "theme": "dark",
            "panels": {"channels": {"rect": {"x": 12, "y": 48, "w": 280, "h": 360}, "collapsed": False}},
        }
    )
    assert saved1["theme"] == "dark"
    assert saved1["shellToolsExpanded"] is None
    assert "channels" in saved1["panels"]

    saved2 = ui_settings.save_ui_settings(
        {
            "shellToolsExpanded": False,
            "panels": {"roi": {"rect": {"x": 320, "y": 48, "w": 260, "h": 220}, "collapsed": True}},
        }
    )
    assert saved2 == {
        "theme": "dark",
        "shellToolsExpanded": False,
        "panels": {
            "channels": {"rect": {"x": 12, "y": 48, "w": 280, "h": 360}, "collapsed": False},
            "roi": {"rect": {"x": 320, "y": 48, "w": 260, "h": 220}, "collapsed": True},
        },
    }

    # Concurrent panel writes preserve valid JSON and all panels
    def write_panel(i: int) -> None:
        ui_settings.save_ui_settings(
            {"panels": {f"p{i}": {"rect": {"x": i * 10, "y": 20, "w": 200, "h": 150}}}}
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_panel, range(16)))

    final = ui_settings.load_ui_settings()
    for i in range(16):
        assert final["panels"][f"p{i}"]["rect"]["x"] == i * 10


def test_recent_files_agent_exclusion_dedup_cap_and_unmounted_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    slides_dir = tmp_path / "slides"
    slides_dir.mkdir()
    local_a = slides_dir / "a.nd2"
    local_b = slides_dir / "b.czi"
    local_deleted = slides_dir / "deleted.lif"
    for p in (local_a, local_b, local_deleted):
        p.write_bytes(b"slide")

    # Agent window NEVER records recent files
    assert ui_settings.record_recent_file(local_a, window_role="agent") == []
    assert ui_settings.record_recent_file(local_a, role="agent") == []
    assert not ui_settings.ui_settings_path().exists()

    # User records deduplicate, move to front, and cap at 20 stored
    for idx in range(25):
        extra = slides_dir / f"extra_{idx:02d}.nd2"
        extra.write_bytes(b"x")
        ui_settings.record_recent_file(extra)

    assert len(ui_settings.list_recent_files(limit=50)) == 20
    assert len(ui_settings.list_recent_files()) == 12  # default limit=12

    ui_settings.record_recent_file(local_a)
    ui_settings.record_recent_file(local_b, name="Custom B")
    ui_settings.record_recent_file(local_deleted)
    # Re-opening local_a deduplicates and moves it to index 0
    ui_settings.record_recent_file(local_a)

    # Record an unmounted external volume path (/Volumes/<nonexistent_vol>/...)
    unmounted_vol_name = f"UnmountedLabDrive_{os.getpid()}_p1"
    assert not (Path("/Volumes") / unmounted_vol_name).exists()
    unmounted_slide = f"/Volumes/{unmounted_vol_name}/project/brain_01.nd2"
    # Inject unmounted entry into ui-settings.json (simulating a previously mounted drive)
    raw_doc = json.loads(ui_settings.ui_settings_path().read_text(encoding="utf-8"))
    raw_doc["recentFiles"].insert(
        1,
        {
            "path": unmounted_slide,
            "name": "brain_01.nd2",
            "openedAt": time.time(),
        },
    )
    ui_settings.ui_settings_path().write_text(json.dumps(raw_doc), encoding="utf-8")

    # Delete local_deleted on mounted local filesystem -> must be pruned!
    local_deleted.unlink()

    items = ui_settings.list_recent_files(limit=10)
    paths = [it["path"] for it in items]
    assert str(local_a.resolve()) == paths[0]
    assert items[0]["available"] is True
    assert str(local_deleted.resolve()) not in paths  # pruned!

    # Unmounted /Volumes/<vol>/... entry is preserved with available=False
    vol_items = [it for it in items if it["path"] == unmounted_slide]
    assert len(vol_items) == 1
    assert vol_items[0]["available"] is False
    assert vol_items[0]["name"] == "brain_01.nd2"

    # Second call still preserves the unmounted /Volumes entry in ui-settings.json
    items_again = ui_settings.list_recent_files(limit=10)
    assert any(it["path"] == unmounted_slide and it["available"] is False for it in items_again)

    b_items = [it for it in items_again if it["path"] == str(local_b.resolve())]
    assert b_items and b_items[0]["name"] == "Custom B" and b_items[0]["available"] is True


# ---------------------------------------------------------------------------
# 5. OME-XML TIFF Export (render.export_roi_tiff)
# ---------------------------------------------------------------------------


def test_export_roi_tiff_emits_compliant_ome_xml_and_preserves_pixels(tmp_path: Path):
    rng = np.random.default_rng(42)
    raw = rng.integers(0, 60000, size=(3, 96, 140), dtype=np.uint16)
    attrs = _make_fluor_attrs(
        source_name="원본_실험.nd2",
        dtype="uint16",
        width=140,
        height=96,
        tile=64,
        channels=[
            ("DAPI", (0, 255, 255)),     # 00FFFF
            ("α-SMA", (255, 0, 255)),    # FF00FF
            ("CY5", (255, 255, 0)),      # FFFF00
        ],
        pixel_size_um=(0.5, 0.25),  # (py, px)
    )
    root = {
        "0": raw,
        "1": raw[:, ::2, ::2],
    }

    # Multi-channel export at level 0
    out_l0 = tmp_path / "roi_l0.ome.tif"
    render.export_roi_tiff(root, attrs, out_l0, 0, 10, 8, 100, 70, [0, 2, 1])

    with tifffile.TiffFile(out_l0) as tf:
        assert tf.is_ome is True
        ome_xml = tf.ome_metadata
        assert ome_xml is not None
        arr = tf.asarray()
        np.testing.assert_array_equal(arr, raw[[0, 2, 1], 8:78, 10:110])

    xml_root = ET.fromstring(ome_xml)
    img_el = xml_root.find("ome:Image", OME_NS)
    assert img_el is not None and img_el.attrib["Name"] == "원본_실험.nd2"
    px_el = img_el.find("ome:Pixels", OME_NS)
    assert px_el is not None
    assert px_el.attrib["Type"] == "uint16"
    assert px_el.attrib["SizeX"] == "100"
    assert px_el.attrib["SizeY"] == "70"
    assert px_el.attrib["SizeC"] == "3"
    assert float(px_el.attrib["PhysicalSizeX"]) == pytest.approx(0.25)
    assert float(px_el.attrib["PhysicalSizeY"]) == pytest.approx(0.5)
    assert px_el.attrib["PhysicalSizeXUnit"] == "µm"
    assert px_el.attrib["PhysicalSizeYUnit"] == "µm"

    ch_els = px_el.findall("ome:Channel", OME_NS)
    assert [c.attrib["Name"] for c in ch_els] == ["DAPI", "CY5", "α-SMA"]
    decoded_hex = [f"{(int(c.attrib['Color']) >> 8) & 0xFFFFFF:06X}" for c in ch_els]
    assert decoded_hex == ["00FFFF", "FFFF00", "FF00FF"]

    # Downsampled level 1 scales PhysicalSizeX / PhysicalSizeY by factor 2
    out_l1 = tmp_path / "roi_l1.ome.tif"
    render.export_roi_tiff(root, attrs, out_l1, 1, 0, 0, 50, 40, [1])
    with tifffile.TiffFile(out_l1) as tf1:
        assert tf1.is_ome is True
        px1 = ET.fromstring(tf1.ome_metadata).find(".//ome:Pixels", OME_NS)
        assert px1 is not None
        assert px1.attrib["SizeC"] == "1"
        assert float(px1.attrib["PhysicalSizeX"]) == pytest.approx(0.5)
        assert float(px1.attrib["PhysicalSizeY"]) == pytest.approx(1.0)
        np.testing.assert_array_equal(tf1.asarray(), raw[1, ::2, ::2][:40, :50])

    # Uncalibrated slide omits PhysicalSizeX / PhysicalSizeY and TIFF resolution
    uncal_attrs = _make_fluor_attrs(pixel_size_um=None, width=140, height=96, tile=64)
    out_uncal = tmp_path / "uncal.ome.tif"
    render.export_roi_tiff(root, uncal_attrs, out_uncal, 0, 0, 0, 64, 64, [0, 1])
    with tifffile.TiffFile(out_uncal) as tf_u:
        assert tf_u.is_ome is True
        px_u = ET.fromstring(tf_u.ome_metadata).find(".//ome:Pixels", OME_NS)
        assert px_u is not None
        assert "PhysicalSizeX" not in px_u.attrib
        assert "PhysicalSizeY" not in px_u.attrib
        assert tf_u.pages[0].tags["ResolutionUnit"].value == 1


# ---------------------------------------------------------------------------
# 6. Dynamic FRAME_CACHE_BYTES & PlateSource.prefetch_neighbors (plate.py)
# ---------------------------------------------------------------------------


def test_dynamic_frame_cache_bytes_scaling():
    mib = 1024 * 1024
    gib = 1024 * mib
    assert plate._default_frame_cache_bytes(2 * gib) == 320 * mib
    assert plate._default_frame_cache_bytes(8 * gib) == int(8 * gib * 0.08)
    assert plate._default_frame_cache_bytes(16 * gib) == int(16 * gib * 0.08)
    assert plate._default_frame_cache_bytes(64 * gib) == 1536 * mib
    assert 320 * mib <= plate.FRAME_CACHE_BYTES <= 1536 * mib
    assert plate._FRAME_CACHE.budget == plate.FRAME_CACHE_BYTES


def test_plate_source_prefetch_neighbors_warms_adjacent_z_and_cleans_up(
    tmp_path: Path,
):
    from test_lif import build_synthetic_lif

    rng = np.random.default_rng(77)
    h, w, n_z, n_c = 64, 64, 5, 2
    stack = rng.integers(5, 250, size=(n_z, n_c, h, w), dtype=np.uint8)
    lif_path = build_synthetic_lif(
        tmp_path / "zstack_prefetch.lif",
        [
            {
                "name": "ZStack",
                "payload": stack.tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="1" Length="6.4e-5" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w}" Length="6.4e-5" Unit="m"/>',
                    f'<DimensionDescription DimID="3" NumberOfElements="{n_z}" BytesInc="{h * w * n_c}" Length="1.0e-5" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Red" BytesInc="{h * w}"/>',
                ],
            }
        ],
    )

    src = plate.PlateSource(lif_path, tile=64, store=False)
    try:
        plate._FRAME_CACHE.purge(src._owner)
        read_zs: list[int] = []
        read_lock = threading.Lock()
        orig_read_lif = src._read_lif_frame

        def tracked_read_lif(t: int, p: int, z: int) -> np.ndarray:
            with read_lock:
                read_zs.append(z)
            return orig_read_lif(t, p, z)

        src._read_lif_frame = tracked_read_lif  # type: ignore[method-assign]

        # Spamming prefetch_neighbors(0, 0, 2) across a tile burst deduplicates
        for _ in range(12):
            src.prefetch_neighbors(0, 0, 2)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            hit_prev = plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 1))
            hit_next = plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 3))
            if hit_prev is not None and hit_next is not None:
                break
            time.sleep(0.02)

        hit_prev = plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 1))
        hit_next = plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 3))
        assert hit_prev is not None and np.array_equal(hit_prev, stack[1])
        assert hit_next is not None and np.array_equal(hit_next, stack[3])
        # Each neighbor plane was decoded strictly once despite 12 burst calls
        assert sorted(read_zs) == [1, 3]
        assert src._fg == 0

        # Subsequent foreground frame() calls for z=1 and z=3 are instant cache hits
        np.testing.assert_array_equal(src.frame(0, 0, 1), stack[1])
        np.testing.assert_array_equal(src.frame(0, 0, 3), stack[3])
        assert sorted(read_zs) == [1, 3]

        # Also verify PlateSource.render_frame supports custom col
        custom_frame = src.render_frame(0, 0, 2, 2, [0, 1], fmt="png", col="00FFFF,FF00FF")
        assert Image.open(io.BytesIO(custom_frame)).size == (32, 32)

        zpf_thread = src._zpf_thread
        assert zpf_thread is not None
    finally:
        src.close(timeout=5.0)

    assert src._closed is True
    assert not zpf_thread.is_alive()
    assert plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 1)) is None
    assert plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 3)) is None
