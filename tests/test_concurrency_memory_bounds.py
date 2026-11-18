"""Coordinated concurrency, memory budget, cancellation, and clean shutdown tests.

Verifies all five tightened subsystems:
1. Bounded SQLite reader pool (max_readers=4, cache_size=-8192, zero leaked permits on
   query/connection errors, deterministic close of idle + in-flight handles).
2. Epoch-cancelled 1-worker Z-stack prefetch (<5 planes decoded across a 30-step scrub,
   cache-thrashing guardrails, and zero lingering threads after close()).
3. Process-wide _RAW_CHUNK_CACHE byte ceiling (32-96 MiB, <=25% per-entry skip,
   pre-insert LRU eviction under multi-threaded contention).
4. Untiled 2D TIFF >64 MiB lazy row/strip streaming without eager full-image RAM
   materialization, plus deterministic file-handle teardown on close().
5. Bounded 16-worker HTTP pool, early _shutting_down abort, quiet BrokenPipeError /
   ConnectionResetError handling, and zero leaked threads after server_close().
"""

from __future__ import annotations

import concurrent.futures
import errno
import socket
import sqlite3
import struct
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from nd2wsi import ometiff, plate, render, server
from nd2wsi.server import SlideRegistry, create_server, server_url
from nd2wsi.storage.single_file import (
    _CACHE_SIZE_KIB,
    _READ_POOL_SIZE,
    SingleFileStore,
    SQLiteStore,
    open_zarr_group,
)


@pytest.fixture(autouse=True)
def _isolate_app_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch real ~/Library/Application Support/nd2wsi-viewer during tests."""
    app_dir = tmp_path / "isolated-app-support"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ND2WSI_APP_SUPPORT_DIR", str(app_dir))
    monkeypatch.setenv("ND2WSI_UI_SETTINGS_ROOT", str(app_dir))
    monkeypatch.setenv("ND2WSI_LUT_SETTINGS_ROOT", str(app_dir / "lut-settings"))
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(app_dir / "window-sessions"))
    return app_dir


def _build_synthetic_lif(path: Path, series_specs: list[dict[str, Any]]) -> Path:
    elem_xmls: list[str] = []
    mem_payloads: list[tuple[str, bytes]] = []
    for idx, spec in enumerate(series_specs):
        name = spec.get("name", f"Series{idx:03d}")
        mem_id = f"MemBlock_{idx + 1}"
        payload: bytes = spec["payload"]
        dims_xml = "".join(spec["dims_xml"])
        chans_xml = "".join(spec["chans_xml"])
        elem_xmls.append(
            f'<Element Name="{name}">'
            f"<Data><Image><ImageDescription>"
            f"<Channels>{chans_xml}</Channels>"
            f"<Dimensions>{dims_xml}</Dimensions>"
            f"</ImageDescription></Image></Data>"
            f'<Memory Size="{len(payload)}" MemoryBlockID="{mem_id}"/>'
            f"</Element>"
        )
        mem_payloads.append((mem_id, payload))

    xml_str = (
        '<?xml version="1.0" encoding="utf-16"?>'
        f'<LMSDataContainerHeader Version="2">{"".join(elem_xmls)}</LMSDataContainerHeader>'
    )
    xml_utf16 = xml_str.encode("utf-16-le")
    buf = bytearray()
    buf.extend(struct.pack("<iIBI", 0x70, 5 + len(xml_utf16), 0x2A, len(xml_utf16) // 2))
    buf.extend(xml_utf16)
    for mem_id, payload in mem_payloads:
        id_utf16 = mem_id.encode("utf-16-le")
        chunk_len = 1 + 8 + 1 + 4 + len(id_utf16)
        buf.extend(
            struct.pack("<iIBQBI", 0x70, chunk_len, 0x2A, len(payload), 0x2A, len(id_utf16) // 2)
        )
        buf.extend(id_utf16)
        buf.extend(payload)
    path.write_bytes(bytes(buf))
    return path


# ---------------------------------------------------------------------------
# 1. Bounded SQLite Reader Pool, Pragmas, Permit Safety & Deterministic Close
# ---------------------------------------------------------------------------


def test_sqlite_pool_caps_at_4_under_20_thread_contention_and_enforces_8mib_pragma(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    assert _READ_POOL_SIZE == 4
    assert _CACHE_SIZE_KIB == -8192

    db_path = tmp_path / "contended.nd2svs"
    payloads = {f"tiles/{i:03d}": (f"tile-{i:03d}-".encode() * 128) for i in range(64)}
    with SingleFileStore(db_path) as writer:
        with writer.batch():
            for k, v in payloads.items():
                writer.write_bytes(k, v)

    real_connect = sqlite3.connect
    created_conns: list[sqlite3.Connection] = []
    conn_lock = threading.Lock()

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        with conn_lock:
            created_conns.append(conn)
        return conn

    monkeypatch.setattr("nd2wsi.storage.single_file.sqlite3.connect", tracking_connect)

    store = SQLiteStore(db_path, read_only=True)
    four_barrier = threading.Barrier(4)
    peak_active = 0
    active_now = 0
    state_lock = threading.Lock()

    def hold_four(idx: int) -> None:
        nonlocal peak_active, active_now
        with store._read_conn() as conn:
            with state_lock:
                active_now += 1
                peak_active = max(peak_active, active_now)
            try:
                four_barrier.wait(timeout=5.0)
                assert conn.execute("PRAGMA cache_size").fetchone()[0] == -8192
                assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 0
                assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
                assert conn.execute("PRAGMA temp_store").fetchone()[0] == 1
                time.sleep(0.02)
            finally:
                with state_lock:
                    active_now -= 1
        assert store.read_bytes(f"tiles/{idx:03d}") == payloads[f"tiles/{idx:03d}"]

    def contend_worker(worker_id: int) -> None:
        nonlocal peak_active, active_now
        for step in range(12):
            key = f"tiles/{(worker_id * 7 + step) % 64:03d}"
            with store._read_conn() as conn:
                with state_lock:
                    active_now += 1
                    peak_active = max(peak_active, active_now)
                try:
                    row = conn.execute(
                        "SELECT value FROM entries WHERE key=?", (key,)
                    ).fetchone()
                    assert row is not None and row[0] == payloads[key]
                finally:
                    with state_lock:
                        active_now -= 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(hold_four, i) for i in range(4)]
        futs.extend(pool.submit(contend_worker, w) for w in range(20))
        for fut in futs:
            fut.result(timeout=15.0)

    assert peak_active == 4
    assert len(created_conns) == 4
    assert len(store._read_connections) == 4
    assert store._active_reads == 0

    store.close()
    assert store._closed is True
    assert store._read_connections == []
    assert store._connection is None
    for conn in created_conns:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_sqlite_pool_never_leaks_permits_on_query_errors_or_broken_connections_and_closes_inflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "errors_and_close.nd2svs"
    with SQLiteStore(db_path) as writer:
        for i in range(16):
            writer.write_bytes(f"k/{i}", f"val-{i}".encode())

    # Verify env/arg configuration for max_readers & cache_size
    monkeypatch.setenv("ND2WSI_SQLITE_MAX_READERS", "3")
    monkeypatch.setenv("ND2WSI_SQLITE_CACHE_KIB", "4096")
    env_store = SQLiteStore(db_path, read_only=True)
    try:
        assert env_store._max_readers == 3
        child_store = env_store.child("k")
        try:
            assert child_store._max_readers == 3
            with child_store._read_conn() as c:
                assert c.execute("PRAGMA cache_size").fetchone()[0] == -4096
        finally:
            child_store.close()
    finally:
        env_store.close()
    monkeypatch.delenv("ND2WSI_SQLITE_MAX_READERS")
    monkeypatch.delenv("ND2WSI_SQLITE_CACHE_KIB")

    store = SQLiteStore(db_path, read_only=True, max_readers=4)

    def error_and_break_worker(idx: int) -> None:
        # 1. SQL syntax / missing table error -> must NOT leak semaphore permit
        with pytest.raises(sqlite3.OperationalError):
            with store._read_conn() as conn:
                conn.execute("SELECT * FROM nonexistent_table")

        # 2. Explicitly broken/closed connection inside _read_conn -> discarded cleanly!
        if idx % 2 == 0:
            with pytest.raises(sqlite3.ProgrammingError):
                with store._read_conn() as conn:
                    conn.close()
                    conn.execute("SELECT 1")

        # 3. Subsequent normal reads succeed and never exceed max_readers=4
        assert store.read_bytes(f"k/{idx % 16}") == f"val-{idx % 16}".encode()

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(error_and_break_worker, i) for i in range(32)]
        for f in futs:
            f.result(timeout=15.0)

    assert store._active_reads == 0
    assert 1 <= len(store._read_connections) <= 4
    # Prove all 4 permits are available by acquiring all 4 simultaneously
    b4 = threading.Barrier(4)

    def verify_four_permits(_i: int) -> None:
        with store._read_conn() as c:
            b4.wait(timeout=5.0)
            assert c.execute("SELECT 1").fetchone()[0] == 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for f in [pool.submit(verify_four_permits, i) for i in range(4)]:
            f.result(timeout=5.0)

    # Deterministic close while 2 connections are idle and 2 are in-flight
    inflight_entered = threading.Barrier(3)
    release_inflight = threading.Event()
    inflight_conns: list[sqlite3.Connection] = []
    all_known_conns = list(store._read_connections)

    def slow_reader() -> None:
        with store._read_conn() as conn:
            inflight_conns.append(conn)
            inflight_entered.wait(timeout=5.0)
            release_inflight.wait(timeout=5.0)
            assert conn.execute("SELECT value FROM entries WHERE key='k/0'").fetchone()[0] == b"val-0"

    t1 = threading.Thread(target=slow_reader)
    t2 = threading.Thread(target=slow_reader)
    t1.start()
    t2.start()
    inflight_entered.wait(timeout=5.0)

    close_done = threading.Event()
    closer = threading.Thread(target=lambda: (store.close(), close_done.set()))
    closer.start()

    # Once close() begins, new callers immediately receive ValueError("SQLiteStore is closed")
    deadline = time.monotonic() + 2.0
    while not store._closed and time.monotonic() < deadline:
        time.sleep(0.005)
    with pytest.raises(ValueError, match="closed"):
        store.read_bytes("k/1")

    # Release in-flight readers -> close() finishes and closes 100% of idle + in-flight handles
    release_inflight.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert close_done.wait(timeout=5.0)
    closer.join(timeout=5.0)

    for conn in {*all_known_conns, *inflight_conns}:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


# ---------------------------------------------------------------------------
# 2. Epoch-Cancelled, Single-Worker Z-Stack Prefetch & Cache Guardrails
# ---------------------------------------------------------------------------


def test_rapid_30_step_z_scrub_cancels_stale_epochs_and_closes_zero_lingering_threads(
    tmp_path: Path,
):
    n_z, n_c, h, w = 32, 2, 64, 64
    stack = np.zeros((n_z, n_c, h, w), dtype=np.uint16)
    for z in range(n_z):
        stack[z, 0] = (z + 1) * 100
        stack[z, 1] = (z + 1) * 200

    lif_path = _build_synthetic_lif(
        tmp_path / "scrub_32z.lif",
        [
            {
                "name": "ScrubStack",
                "payload": stack.tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="6.4e-5" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="6.4e-5" Unit="m"/>',
                    f'<DimensionDescription DimID="3" NumberOfElements="{n_z}" BytesInc="{h * w * n_c * 2}" Length="3.2e-5" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Cyan" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Magenta" BytesInc="{h * w * 2}"/>',
                ],
            }
        ],
    )

    src = plate.PlateSource(lif_path, tile=64, store=False)
    try:
        plate._FRAME_CACHE.purge(src._owner)
        decoded_planes: list[int] = []
        dec_lock = threading.Lock()
        orig_read = src._read_lif_frame

        def slow_tracked_read(t: int, p: int, z: int) -> np.ndarray:
            time.sleep(0.012)  # realistic disk/decompress cost per Z plane
            with dec_lock:
                decoded_planes.append(z)
            return orig_read(t, p, z)

        src._read_lif_frame = slow_tracked_read  # type: ignore[method-assign]

        # Rapid 30-step Z scrub (z = 0..29): naive FIFO queue would decode ~58 planes!
        for z in range(30):
            src.prefetch_neighbors(0, 0, z)
            assert len(src._zpf_pending_targets) <= 2
            time.sleep(0.0005)

        assert src._prefetch_epoch == 30

        # Wait for the latest epoch's target planes (z=30, z=28) to finish warming
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            hit_30 = plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 30))
            hit_28 = plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 28))
            if hit_30 is not None and hit_28 is not None:
                break
            time.sleep(0.01)

        assert plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 30)) is not None
        assert plate._FRAME_CACHE.get((src._owner, "frame", 0, 0, 28)) is not None
        # Stale intermediate epochs were cancelled before touching disk/decoders!
        assert 2 <= len(decoded_planes) < 5, f"expected <5 planes decoded, got {decoded_planes}"
        assert 30 in decoded_planes and 28 in decoded_planes
        worker_thread = src._zpf_thread
        assert worker_thread is not None
    finally:
        src.close(timeout=5.0)

    assert src._closed is True
    assert not worker_thread.is_alive()
    lingering = [
        t.name
        for t in threading.enumerate()
        if t.name.startswith(("plate-z-prefetch", "plate-prefetch"))
    ]
    assert lingering == []


def test_z_prefetch_cache_thrashing_guardrails_and_ometiff_stack_scrub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    n_z, n_c, h, w = 32, 2, 64, 64
    stack = np.arange(n_z * n_c * h * w, dtype=np.uint16).reshape(n_z, n_c, h, w)
    frame_bytes = n_c * h * w * 2  # 16,384 bytes

    ome_stack = tmp_path / "z_scrub.ome.tif"
    tifffile.imwrite(
        ome_stack,
        stack,
        ome=True,
        photometric="minisblack",
        metadata={"axes": "ZCYX", "PhysicalSizeZ": 1.0, "PhysicalSizeZUnit": "µm"},
    )

    # 1. OME-TIFF 30-step rapid Z-scrub also cancels stale epochs (<5 planes decoded)
    ome_src = ometiff.OmeTiffStackSource(ome_stack)
    try:
        plate._FRAME_CACHE.purge(ome_src._owner)
        ome_decoded: list[int] = []
        orig_plane_read = ome_src._read_plane_cyx

        def tracked_plane_read(t: int, p: int, z: int) -> np.ndarray:
            time.sleep(0.012)
            ome_decoded.append(z)
            return orig_plane_read(t, p, z)

        ome_src._read_plane_cyx = tracked_plane_read  # type: ignore[method-assign]
        for z in range(30):
            ome_src.prefetch_neighbors(0, 0, z)
            time.sleep(0.0005)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (
                plate._FRAME_CACHE.get((ome_src._owner, "frame", 0, 0, 30)) is not None
                and plate._FRAME_CACHE.get((ome_src._owner, "frame", 0, 0, 28)) is not None
            ):
                break
            time.sleep(0.01)
        assert 2 <= len(ome_decoded) < 5
        ome_worker = ome_src._zpf_thread
        assert ome_worker is not None
    finally:
        ome_src.close(timeout=5.0)
    assert not ome_worker.is_alive()

    # 2. Cache-thrashing guardrails on PlateSource:
    lif_path = _build_synthetic_lif(
        tmp_path / "guardrail.lif",
        [
            {
                "name": "Guard",
                "payload": stack[:6].tobytes(),
                "dims_xml": [
                    f'<DimensionDescription DimID="1" NumberOfElements="{w}" BytesInc="2" Length="6.4e-5" Unit="m"/>',
                    f'<DimensionDescription DimID="2" NumberOfElements="{h}" BytesInc="{w * 2}" Length="6.4e-5" Unit="m"/>',
                    f'<DimensionDescription DimID="3" NumberOfElements="6" BytesInc="{frame_bytes}" Length="6.0e-6" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Cyan" BytesInc="0"/>',
                    f'<ChannelDescription DataType="0" ChannelTag="0" Resolution="16" LUTName="Magenta" BytesInc="{h * w * 2}"/>',
                ],
            }
        ],
    )
    src = plate.PlateSource(lif_path, tile=64, store=False)
    try:
        plate._FRAME_CACHE.purge(src._owner)
        decoded: list[int] = []
        orig_lif = src._read_lif_frame

        def spy_lif(t: int, p: int, z: int) -> np.ndarray:
            decoded.append(z)
            return orig_lif(t, p, z)

        src._read_lif_frame = spy_lif  # type: ignore[method-assign]

        # (a) estimated_frame_bytes * 1.5 > budget -> skip completely (0 targets queued)
        monkeypatch.setattr(plate._FRAME_CACHE, "budget", int(frame_bytes * 1.2))
        src.prefetch_neighbors(0, 0, 2)
        time.sleep(0.05)
        assert src._zpf_pending_targets == []
        assert decoded == []

        # (b) estimated_frame_bytes * 2 > budget (<= 1.5x limit) -> at most 1 neighbor in travel dir!
        monkeypatch.setattr(plate._FRAME_CACHE, "budget", int(frame_bytes * 1.8))
        # Step downward 4 -> 3 -> direction is -1, so ONLY z=2 is prefetched (not z=4)!
        src._last_prefetch_tpz = (0, 0, 4)
        src.prefetch_neighbors(0, 0, 3)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not decoded:
            time.sleep(0.01)
        time.sleep(0.03)
        assert decoded == [2]
    finally:
        src.close(timeout=5.0)


# ---------------------------------------------------------------------------
# 3. _RAW_CHUNK_CACHE Byte Budget, >25% Entry Skip & Multi-Threaded Eviction
# ---------------------------------------------------------------------------


def test_raw_chunk_cache_enforces_exact_budget_skips_oversized_and_evicts_lru():
    render.clear_raw_chunk_cache()
    base_stats = render.raw_chunk_cache_stats()
    assert base_stats["used"] == 0 and base_stats["entries"] == 0
    assert 32 * 1024 * 1024 <= base_stats["budget"] <= 96 * 1024 * 1024

    budget = 1024 * 1024  # 1 MiB -> max single entry = 256 KiB
    cache = render._RawChunkCache(budget=budget)

    # 1. Single entry > 25% of budget (320 KiB > 256 KiB) is skipped without evicting residents
    hot = np.ones((1, 128, 512), dtype=np.uint16)  # 128 KiB
    cache.put(("hot", 0), hot)
    assert cache.stats()["used"] == 128 * 1024

    oversized = np.ones((1, 320, 512), dtype=np.uint16)  # 320 KiB (> 256 KiB)
    returned = cache.put(("too-big", 0), oversized)
    assert returned.shape == oversized.shape
    st_after_skip = cache.stats()
    assert st_after_skip["skipped"] == 1
    assert st_after_skip["used"] == 128 * 1024
    assert cache.get(("hot", 0)) is not None

    # 2. Multi-threaded contention: 16 threads writing 128 KiB chunks concurrently
    peak_observed_used = 0
    obs_lock = threading.Lock()

    def writer_worker(tid: int) -> None:
        nonlocal peak_observed_used
        for step in range(10):
            # Keep ("hot", 0) hot on even steps
            if step % 2 == 0:
                _ = cache.get(("hot", 0))
            arr = np.full((1, 128, 512), tid * 100 + step, dtype=np.uint16)  # 128 KiB
            cache.put(("worker", tid, step), arr)
            cur_used = cache.used
            assert 0 <= cur_used <= budget
            with obs_lock:
                peak_observed_used = max(peak_observed_used, cur_used)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for fut in [pool.submit(writer_worker, t) for t in range(16)]:
            fut.result(timeout=10.0)

    final_stats = cache.stats()
    assert peak_observed_used <= budget
    assert 0 < final_stats["used"] <= budget
    assert final_stats["entries"] <= 8  # 8 * 128 KiB == 1 MiB
    assert final_stats["evictions"] >= 100
    # Hot entry touched throughout remained cached while cold entries were evicted
    assert cache.get(("hot", 0)) is not None
    actual_bytes = sum(int(arr.nbytes) for _ref, arr in cache._d.values())
    assert final_stats["used"] == actual_bytes


# ---------------------------------------------------------------------------
# 4. Large Untiled 2D TIFF (>64 MiB) Lazy Streaming & File Handle Teardown
# ---------------------------------------------------------------------------


def test_large_untiled_2d_tiff_exceeding_64mib_streams_lazily_and_closes_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # (A) Real >64 MiB untiled 2D OME-TIFF: 3 x 3600 x 3200 uint16 = 69,120,000 B (~65.9 MiB)
    c, h, w = 3, 3600, 3200
    raw_nbytes = c * h * w * 2
    assert raw_nbytes > ometiff.UNTILED_EAGER_MAX_BYTES

    big_arr = np.zeros((c, h, w), dtype=np.uint16)
    big_arr[0, 10:80, 20:90] = 12345
    big_arr[1, 1800:1850, 1600:1660] = 43210
    big_arr[2, -40:, -50:] = 55555

    big_tif = tmp_path / "large_untiled_66mb.ome.tif"
    tifffile.imwrite(
        big_tif,
        big_arr,
        ome=True,
        photometric="minisblack",
        metadata={
            "axes": "CYX",
            "PhysicalSizeX": 0.5,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.5,
            "PhysicalSizeYUnit": "µm",
            "Channel": {"Name": ["DAPI", "GFP", "CY5"]},
        },
    )
    del big_arr  # free writer buffer before opening

    opened_fds: list[int] = []
    real_os_open = ometiff.os.open

    def tracking_os_open(p, flags, *args, **kwargs):
        fd = real_os_open(p, flags, *args, **kwargs)
        if Path(p).resolve() == big_tif.resolve():
            opened_fds.append(fd)
        return fd

    monkeypatch.setattr(ometiff.os, "open", tracking_os_open)

    # Guard that _series_2d_to_cyx (full-image RAM materialization) is NEVER called!
    def forbid_eager_full_decode(*_args, **_kwargs):
        raise AssertionError("full-image _series_2d_to_cyx should not run for >64 MiB untiled TIFF")

    monkeypatch.setattr(ometiff, "_series_2d_to_cyx", forbid_eager_full_decode)

    root, attrs = ometiff.open_direct(big_tif)
    try:
        assert isinstance(root["0"], ometiff._LazyUntiledLevel)
        assert "lazily" in " ".join(attrs["nd2wsi"]["notes"])

        # Retained memo memory across all pyramid levels is tiny (<8 MiB vs 65.9 MiB raw!)
        retained_bytes = sum(
            int(lv._memo.nbytes)
            for lv in root.values()
            if getattr(lv, "_memo", None) is not None
        )
        assert retained_bytes < 8 * 1024 * 1024

        # Verify exact pixel values at corners & interior without full-plane read
        top_patch = root["0"][:, 0:100, 0:100]
        assert int(top_patch[0, 20, 30]) == 12345
        assert int(top_patch[0, 0, 0]) == 0
        mid_patch = root["0"][1, 1800:1850, 1600:1660]
        assert np.all(mid_patch == 43210)
        bot_patch = root["0"][2, h - 40 : h, w - 50 : w]
        assert np.all(bot_patch == 55555)

        # Tile render works end-to-end
        jpg = render.render_tile(root, attrs, 0, 0, 0, [0, 1, 2], fmt="jpg")
        assert len(jpg) > 200
        assert len(opened_fds) == 1
    finally:
        root.close(delay=0)

    # Verify raw fd is deterministically closed after close(delay=0)
    for fd in opened_fds:
        with pytest.raises(OSError) as exc_info:
            ometiff.os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF

    with pytest.raises(ValueError, match="closed"):
        _ = root["0"][:, 0:16, 0:16]


def test_compressed_untiled_tiff_lazy_matches_eager_pyramid_bit_for_bit(tmp_path: Path):
    rng = np.random.default_rng(909)
    arr = rng.integers(0, 50000, size=(2, 320, 384), dtype=np.uint16)
    comp_tif = tmp_path / "compressed_strips.ome.tif"
    tifffile.imwrite(
        comp_tif,
        arr,
        ome=True,
        compression="zlib",
        rowsperstrip=32,
        photometric="minisblack",
        metadata={"axes": "CYX"},
    )

    eager_root, eager_attrs = ometiff.open_direct(comp_tif, tile=64, eager_max_bytes=64 * 1024 * 1024)
    lazy_root, lazy_attrs = ometiff.open_direct(comp_tif, tile=64, eager_max_bytes=64 * 1024)
    try:
        assert isinstance(eager_root["0"], ometiff._MemoryPyramidLevel)
        assert isinstance(lazy_root["0"], ometiff._LazyUntiledLevel)
        assert [lv["path"] for lv in eager_attrs["nd2wsi"]["levels"]] == [
            lv["path"] for lv in lazy_attrs["nd2wsi"]["levels"]
        ]
        for lv in eager_attrs["nd2wsi"]["levels"]:
            k = lv["path"]
            np.testing.assert_array_equal(lazy_root[k][:, :, :], eager_root[k][:, :, :])
    finally:
        eager_root.close(delay=0)
        lazy_root.close(delay=0)


# ---------------------------------------------------------------------------
# 5. Bounded 16-Worker HTTP Pool, Quiet Client Disconnects & Zero Thread Leaks
# ---------------------------------------------------------------------------


def test_http_server_16_worker_bound_broken_pipe_silence_and_clean_shutdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    # Create both a .nd2svs SQLite-backed pyramid and a Z-stack .ome.tif to exercise
    # SlideRegistry.close_all() across SQLiteStore + Stack prefetcher + OME-TIFF handles!
    nd2svs_path = tmp_path / "cached_slide.nd2svs"
    w_grp = open_zarr_group(nd2svs_path, prefix="store.ome.zarr", mode="w")
    try:
        w_grp.attrs.update(
            {
                "multiscales": [{"datasets": [{"path": "0"}], "version": "0.4"}],
                "nd2wsi": {
                    "source": "cached_slide.nd2",
                    "levels": [{"path": "0", "width": 512, "height": 512, "downsample": 1}],
                    "tile": 256,
                    "dtype": "uint16",
                    "rgb": False,
                    "pixel_size_um": [0.5, 0.5],
                },
                "omero": {
                    "channels": [
                        {
                            "label": "DAPI",
                            "color": "00FFFF",
                            "window": {"start": 0.0, "end": 40000.0, "min": 0.0, "max": 65535.0},
                        }
                    ]
                },
            }
        )
        w_grp.create_array(
            "0",
            data=np.full((1, 512, 512), 20000, dtype=np.uint16),
            chunks=(1, 256, 256),
        )
    finally:
        w_grp.store.close()
    # Add manifest.json so SlideRegistry accepts the .nd2svs container
    with SQLiteStore(nd2svs_path) as m_store:
        m_store.write_bytes(
            "manifest.json",
            b'{"complete":true,"format":"nd2wsi-cache/4","kind":"full","generation":"gen-test-1","origin":"export"}',
        )

    z_tif = tmp_path / "stack_slide.ome.tif"
    tifffile.imwrite(
        z_tif,
        np.full((6, 1, 256, 256), 15000, dtype=np.uint16),
        ome=True,
        photometric="minisblack",
        metadata={"axes": "ZCYX"},
    )

    httpd = create_server([nd2svs_path, z_tif], port=0)
    assert httpd.max_workers == server.HTTP_MAX_WORKERS == 16

    serve_thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    serve_thread.start()

    base = server_url(httpd).rstrip("/")
    host, port = httpd.server_address[0], httpd.server_address[1]
    token = httpd.token
    slides = httpd.registry.listing()
    assert len(slides) == 2
    sid_sqlite = slides[0]["sid"]
    sid_stack = slides[1]["sid"]
    sqlite_store_ref = httpd.registry.get(sid_sqlite).root.store
    stack_plate_ref = httpd.registry.get(sid_stack).plate

    peak_workers = 0
    pw_lock = threading.Lock()
    keepalive_socks: list[socket.socket] = []

    try:
        def valid_tile_client(i: int) -> int:
            nonlocal peak_workers
            target_sid = sid_sqlite if i % 2 == 0 else sid_stack
            q = f"?z={i % 6}" if target_sid == sid_stack else ""
            url = f"{base}/s/{target_sid}/api/tile/0/0/0.jpg{q}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                body = resp.read()
                cur = sum(
                    1 for t in threading.enumerate() if t.name.startswith("nd2wsi-http")
                )
                with pw_lock:
                    peak_workers = max(peak_workers, cur)
                assert resp.status == 200
                return len(body)

        def abrupt_disconnect_client(i: int) -> None:
            """Simulate OpenSeadragon aborting off-screen tile sockets mid-flight (RST)."""
            s = socket.create_connection((host, port), timeout=5.0)
            # SO_LINGER with timeout=0 sends an immediate TCP RST on close(),
            # triggering BrokenPipeError / ConnectionResetError in the handler!
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            req = (
                f"GET /{token}/s/{sid_sqlite}/api/tile/0/{i % 2}/{ (i // 2) % 2 }.png HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n\r\n"
            ).encode("ascii")
            try:
                s.sendall(req)
                if i % 2 == 0:
                    _ = s.recv(16)  # read partial header then slam RST mid-body!
            except OSError:
                pass
            finally:
                try:
                    s.close()
                except OSError:
                    pass

        # Also open 2 HTTP/1.1 keep-alive connections and leave them idle
        for _ in range(2):
            ks = socket.create_connection((host, port), timeout=5.0)
            ks.sendall(
                f"GET /{token}/api/slides HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: keep-alive\r\n\r\n".encode(
                    "ascii"
                )
            )
            _ = ks.recv(4096)
            keepalive_socks.append(ks)

        with concurrent.futures.ThreadPoolExecutor(max_workers=36) as client_pool:
            valid_futs = [client_pool.submit(valid_tile_client, i) for i in range(40)]
            rst_futs = [client_pool.submit(abrupt_disconnect_client, j) for j in range(24)]
            for f in rst_futs:
                f.result(timeout=15.0)
            sizes = [f.result(timeout=15.0) for f in valid_futs]

        assert all(sz > 50 for sz in sizes)
        assert 1 <= peak_workers <= 16

        # Verify queued requests abort immediately when _shutting_down is set
        s1, s2 = socket.socketpair()
        try:
            key = object()
            httpd._pending_sockets[key] = s1
            httpd._shutting_down = True
            httpd._run_pooled_request(key, s1, ("127.0.0.1", 0))
            assert key not in httpd._pending_sockets
        finally:
            for sp in (s1, s2):
                try:
                    sp.close()
                except OSError:
                    pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve_thread.join(timeout=5.0)
        for ks in keepalive_socks:
            try:
                ks.close()
            except OSError:
                pass

    # Zero stacktrace spam on stderr despite 24 abrupt TCP RST / BrokenPipe disconnects!
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "BrokenPipeError" not in captured.err
    assert "ConnectionResetError" not in captured.err

    # Deterministic backend & thread cleanup
    assert sqlite_store_ref._closed is True
    assert stack_plate_ref._closed is True
    assert render.raw_chunk_cache_stats()["used"] == 0

    leaked_threads = [
        t.name
        for t in threading.enumerate()
        if t.is_alive()
        and t.name.startswith(
            (
                "nd2wsi-http",
                "plate-z-prefetch",
                "plate-prefetch",
                "stack-z-prefetch",
                "stack-prefetch",
            )
        )
    ]
    assert leaked_threads == []


def test_slide_registry_close_all_closes_sqlite_plate_and_ometiff_deterministically(
    tmp_path: Path,
):
    untiled_tif = tmp_path / "small_untiled.ome.tif"
    tifffile.imwrite(untiled_tif, np.ones((2, 64, 64), dtype=np.uint16), ome=True, metadata={"axes": "CYX"})

    lif_path = _build_synthetic_lif(
        tmp_path / "reg_plate.lif",
        [
            {
                "name": "RegZ",
                "payload": np.zeros((4, 1, 64, 64), dtype=np.uint8).tobytes(),
                "dims_xml": [
                    '<DimensionDescription DimID="1" NumberOfElements="64" BytesInc="1" Length="6.4e-5" Unit="m"/>',
                    '<DimensionDescription DimID="2" NumberOfElements="64" BytesInc="64" Length="6.4e-5" Unit="m"/>',
                    '<DimensionDescription DimID="3" NumberOfElements="4" BytesInc="4096" Length="4.0e-6" Unit="m"/>',
                ],
                "chans_xml": [
                    '<ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green" BytesInc="0"/>',
                ],
            }
        ],
    )

    reg = SlideRegistry()
    sid_tif = reg.open_path(untiled_tif)
    sid_lif = reg.open_path(lif_path)
    plate_src = reg.get(sid_lif).plate
    plate_src.prefetch_neighbors(0, 0, 1)
    tif_root = reg.get(sid_tif).root

    # Even default close_all() (once busy drains) closes everything deterministically
    reg.close_all()
    assert plate_src._closed is True
    assert tif_root._closed is True
    with pytest.raises(ValueError, match="closed"):
        _ = tif_root["0"][:, 0:8, 0:8]


def test_http_server_bind_bypasses_getfqdn_dns_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_getfqdn(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("socket.getfqdn must not be called by local viewer server_bind()")

    monkeypatch.setattr(socket, "getfqdn", _forbid_getfqdn)
    httpd = create_server([], port=0)
    try:
        assert httpd.server_name == "127.0.0.1"
        assert httpd.server_port > 0
    finally:
        httpd.server_close()
