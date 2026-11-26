"""Unit and failure-injection tests for the internal direct-to-SQLite staging writer."""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dask
import dask.array as da
import numpy as np
import pytest

from nd2wsi.cache import (
    MANIFEST_NAME,
    SINGLE_FILE_FORMAT,
    CacheLock,
    build_manifest,
    commit_container,
    quick_fingerprint,
    read_manifest,
    remove_staging_artifact,
    sweep_stale_builds,
)
from nd2wsi.convert import (
    NGFF_VERSION,
    _downsample_into,
    _hooks,
    _percentile_windows,
    _write_level0,
    _write_level1_from_source,
    build_group_attrs,
    open_store,
)
from nd2wsi.migration import pack_directory
from nd2wsi.platform_io import retry_windows_staging_op
from nd2wsi.reader import ChannelInfo, PlaneSelection, PlaneSource, level_shapes
from nd2wsi.storage import DEFAULT_STORAGE
from nd2wsi.storage.single_file import (
    APPLICATION_ID,
    SQLiteStore,
    is_owned_rollback_sqlite_header,
)
from nd2wsi.storage.single_file_writer import (
    SingleFileWriteGroup,
    _SingleFileStagingBackend,
    verify_and_finalize_single_file,
)
from nd2wsi.storage.zarr_v2 import UnsafeZarrArrayError


def _make_synthetic_source(
    arr: np.ndarray,
    slide_path: Path,
    *,
    tile: int,
    overview: bool,
) -> PlaneSource:
    source_tile = tile * 2 if overview else tile
    darr = da.from_array(arr, chunks=(1, source_tile, source_tile))
    channels = [
        ChannelInfo(name=f"ch{i}", color=(60 * (i + 1) % 256, 120, 200))
        for i in range(arr.shape[0])
    ]
    return PlaneSource(
        data=darr,
        dtype=arr.dtype,
        shape=tuple(arr.shape),
        rgb=False,
        channels=channels,
        pixel_size_um=(0.5, 0.5),
        source_name=slide_path.name,
        selection={"t": 0, "p": 0, "z": 0},
        notes=[],
    )


def _build_direct_and_packed_pair(
    tmp_path: Path,
    data: np.ndarray,
    *,
    tile: int,
    overview: bool,
    max_inflight_bytes: int = 32 * 1024 * 1024,
    flush_threshold_bytes: int = 16 * 1024 * 1024,
) -> tuple[Path, Path]:
    slide = tmp_path / "synthetic.nd2"
    slide.write_bytes(b"synthetic-slide-header" * 128)
    fp = quick_fingerprint(slide)
    selection = PlaneSelection()
    _, h, w = data.shape
    shapes = level_shapes(h, w, tile)
    first_level = 1 if overview else 0
    src = _make_synthetic_source(data, slide, tile=tile, overview=overview)
    hooks = _hooks()

    # 1. Reference: loose directory + pack_directory
    dir_stage = tmp_path / "ref.nd2wsi-cache"
    dir_stage.mkdir()
    with dask.config.set(scheduler="threads", num_workers=4):
        ref_root = DEFAULT_STORAGE.create_group(dir_stage / "store.ome.zarr")
        ref_arrays = {
            lv: DEFAULT_STORAGE.create_array(
                ref_root, str(lv), (data.shape[0], shapes[lv][0], shapes[lv][1]), data.dtype, tile
            )
            for lv in range(first_level, len(shapes))
        }
        if overview:
            _write_level1_from_source(src, ref_arrays[1], DEFAULT_STORAGE)
        else:
            _write_level0(src, ref_arrays[0], DEFAULT_STORAGE)
        for lv in range(first_level + 1, len(shapes)):
            _downsample_into(ref_arrays[lv - 1], ref_arrays[lv], tile, data.dtype, DEFAULT_STORAGE)
        ref_level_paths = [str(lv) for lv in range(first_level, len(shapes))]
        ref_windows = _percentile_windows(ref_root, ref_level_paths, src)
        ref_attrs = build_group_attrs(
            src, shapes[first_level:], tile, ref_windows, start_level=first_level
        )
        ref_attrs["nd2wsi"]["storage"] = DEFAULT_STORAGE.descriptor(ngff_version=NGFF_VERSION)
        ref_attrs["nd2wsi"]["source_fingerprint"] = fp
        if overview:
            ref_attrs["nd2wsi"]["kind"] = "overview"
            ref_attrs["nd2wsi"]["overview_of"] = {
                "channels": data.shape[0],
                "height": h,
                "width": w,
            }
        ref_root.attrs.update(ref_attrs)
    ref_manifest = build_manifest(
        dir_stage,
        slide,
        fp,
        selection.describe(),
        0,
        tile,
        tuple(data.shape),
        str(data.dtype),
        kind="overview" if overview else "full",
        storage=ref_attrs["nd2wsi"]["storage"],
        single_file=False,
        complete=True,
    )
    (dir_stage / MANIFEST_NAME).write_text(json.dumps(ref_manifest, indent=1), encoding="utf-8")
    packed_out = tmp_path / "packed.nd2svs"
    pack_directory(dir_stage, packed_out, source=slide)

    # 2. Candidate: _SingleFileStagingBackend + verify_and_finalize_single_file
    direct_out = tmp_path / "direct.nd2svs"
    staging_db = tmp_path / "direct.nd2svs.building-0123456789abcdef"
    backend = _SingleFileStagingBackend(
        max_inflight_bytes=max_inflight_bytes,
        flush_threshold_bytes=flush_threshold_bytes,
    )
    with dask.config.set(scheduler="threads", num_workers=4):
        group = backend.create_group(staging_db)
        arrays = {
            lv: backend.create_array(
                group, str(lv), (data.shape[0], shapes[lv][0], shapes[lv][1]), data.dtype, tile
            )
            for lv in range(first_level, len(shapes))
        }
        if overview:
            _write_level1_from_source(src, arrays[1], backend)
        else:
            _write_level0(src, arrays[0], backend)
        group.flush()
        for lv in range(first_level + 1, len(shapes)):
            _downsample_into(arrays[lv - 1], arrays[lv], tile, data.dtype, backend)
            group.flush()
        level_paths = [str(lv) for lv in range(first_level, len(shapes))]
        windows = _percentile_windows(group, level_paths, src)
        attrs = build_group_attrs(
            src, shapes[first_level:], tile, windows, start_level=first_level
        )
        attrs["nd2wsi"]["storage"] = backend.descriptor(ngff_version=NGFF_VERSION)
        attrs["nd2wsi"]["source_fingerprint"] = fp
        if overview:
            attrs["nd2wsi"]["kind"] = "overview"
            attrs["nd2wsi"]["overview_of"] = {
                "channels": data.shape[0],
                "height": h,
                "width": w,
            }
        group.attrs.update(attrs)
        manifest = build_manifest(
            direct_out,
            slide,
            fp,
            selection.describe(),
            0,
            tile,
            tuple(data.shape),
            str(data.dtype),
            kind="overview" if overview else "full",
            storage=attrs["nd2wsi"]["storage"],
            single_file=True,
            complete=False,
        )

    def _validate(candidate: Path) -> None:
        r, _ = hooks.open_store(candidate)
        try:
            assert r is not None
        finally:
            close = getattr(r, "close", None)
            if close:
                close()

    verify_and_finalize_single_file(
        group,
        staging_path=staging_db,
        source=slide,
        before_fp=fp,
        manifest=manifest,
        validate_fn=_validate,
    )
    commit_container(staging_db, direct_out)
    return packed_out, direct_out


@pytest.mark.parametrize(
    "shape,tile,overview",
    [
        ((2, 130, 195), 32, False),  # odd dimensions + multi-level + all-zero channel
        ((2, 130, 195), 32, True),  # overview build
        ((2, 20, 24), 32, False),  # single-level pyramid (len(shapes) == 1)
        ((2, 131, 5), 16, False),  # 1-pixel collapsed X axis across deep levels
    ],
)
def test_direct_writer_matches_pack_directory_bit_for_bit(tmp_path, shape, tile, overview):
    rng = np.random.default_rng(12345)
    data = rng.integers(0, 4096, size=shape, dtype=np.uint16)
    data[1] = 0  # exercise intentional all-zero omission on every level
    packed_out, direct_out = _build_direct_and_packed_pair(
        tmp_path,
        data,
        tile=tile,
        overview=overview,
        max_inflight_bytes=16 * 1024,
        flush_threshold_bytes=8 * 1024,
    )

    ref_root, ref_attrs = open_store(packed_out)
    dir_root, dir_attrs = open_store(direct_out)
    try:
        assert dir_attrs == ref_attrs
        first = 1 if overview else 0
        shapes = level_shapes(shape[1], shape[2], tile)
        for lv in range(first, len(shapes)):
            np.testing.assert_array_equal(
                np.asarray(dir_root[str(lv)][:]), np.asarray(ref_root[str(lv)][:])
            )
    finally:
        ref_root.close()
        dir_root.close()

    with SQLiteStore(packed_out, read_only=True) as ref_st, SQLiteStore(
        direct_out, read_only=True
    ) as dir_st:
        assert set(dir_st.list_keys()) == set(ref_st.list_keys())
        for key in dir_st.list_keys():
            if key.endswith((".zarray", ".zgroup", ".zattrs")):
                assert json.loads(dir_st.read_bytes(key)) == json.loads(ref_st.read_bytes(key))

    ref_m = read_manifest(packed_out)
    dir_m = read_manifest(direct_out)
    assert ref_m is not None and dir_m is not None
    ref_norm = {k: v for k, v in ref_m.items() if k not in ("generation", "migration")}
    dir_norm = {k: v for k, v in dir_m.items() if k != "generation"}
    assert dir_norm == ref_norm


def test_build_manifest_single_file_flag_handles_existing_directory_target(tmp_path):
    slide = tmp_path / "slide.nd2"
    slide.write_bytes(b"slide")
    existing_dir = tmp_path / "legacy.nd2svs"
    existing_dir.mkdir()
    storage = {"format": "zarr", "zarr_version": 2, "ngff_version": "0.4", "backend": "zarr-v2-direct"}
    storage_copy = dict(storage)
    manifest = build_manifest(
        existing_dir,
        slide,
        quick_fingerprint(slide),
        {"t": 0, "p": 0, "z": "mid"},
        0,
        512,
        (1, 64, 64),
        "uint16",
        storage=storage,
        single_file=True,
        complete=False,
    )
    assert manifest["format"] == SINGLE_FILE_FORMAT
    assert manifest["complete"] is False
    assert manifest["source"]["relative_path"] == "slide.nd2"
    assert manifest["storage"]["container"] == "nd2svs-sqlite/2"
    assert storage == storage_copy  # caller storage dict was not mutated


def test_coordinate_accounting_duplicate_missing_and_oversized_zarray(tmp_path):
    staging = tmp_path / "acct.nd2svs.building-1111"
    backend = _SingleFileStagingBackend()
    group = backend.create_group(staging)
    try:
        with pytest.raises(UnsafeZarrArrayError):
            # 1 * 8192 * 8192 * 2 = 128 MiB > 64 MiB MAX_CHUNK_BYTES
            backend.create_array(group, "0", (1, 8192, 8192), np.dtype("uint16"), 8192)
        arr = backend.create_array(group, "0", (1, 32, 64), np.dtype("uint16"), 32)
        chunked = backend.chunk_array(arr)
        chunked.write((0, 0, 0), np.ones((1, 32, 32), dtype=np.uint16))
        with pytest.raises(RuntimeError, match="duplicate chunk coordinate"):
            chunked.write((0, 0, 0), np.ones((1, 32, 32), dtype=np.uint16))
        with pytest.raises(RuntimeError, match="1 planned chunk coordinate"):
            group.assert_all_coords_accounted()
        chunked.record_zero((0, 0, 1))
        group.assert_all_coords_accounted()
        group.flush()
    finally:
        group.close_handles(suppress_errors=True)


def test_blocked_producers_injected_write_error_terminates_and_preserves_prior_cache(
    tmp_path, monkeypatch
):
    prior = tmp_path / "existing.nd2svs"
    with SQLiteStore(prior) as st:
        st.write_bytes("marker", b"intact-prior")
    prior_bytes = prior.read_bytes()

    staging = tmp_path / "existing.nd2svs.building-deadbeef"
    group = SingleFileWriteGroup(
        staging,
        max_inflight_bytes=16 * 1024,
        flush_threshold_bytes=8 * 1024,
    )
    arr = group.register_level("0", (4, 64, 128), np.dtype("uint16"), 32)
    chunked = _SingleFileStagingBackend().chunk_array(arr)
    rng = np.random.default_rng(7)
    coords = [(c, ty, tx) for c in range(4) for ty in range(2) for tx in range(4)]

    commit_calls = 0
    real_batch = group.store.batch

    def failing_batch():
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 2:
            # Delay briefly so sibling producers contend on _buf_cv before ENOSPC hits.
            time.sleep(0.05)
            raise sqlite3.OperationalError("database or disk is full")
        return real_batch()

    monkeypatch.setattr(group.store, "batch", failing_batch)

    errors: list[BaseException] = []

    def worker(coord: tuple[int, int, int]) -> None:
        block = rng.integers(1, 65000, size=(1, 32, 32), dtype=np.uint16)
        try:
            chunked.write(coord, block)
        except BaseException as exc:
            errors.append(exc)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, coords))
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    assert errors and any(isinstance(e, sqlite3.OperationalError) for e in errors)

    group.close_handles(suppress_errors=True)
    remove_staging_artifact(staging)
    assert not staging.exists()
    assert not staging.with_name(staging.name + "-journal").exists()
    assert prior.read_bytes() == prior_bytes


def test_oversized_chunk_and_keyboard_interrupt_no_deadlock_or_spin(tmp_path, monkeypatch):
    # Tiny 256-byte budget forces oversized-chunk drain path under concurrent writers.
    staging = tmp_path / "oversized.nd2svs.building-0001"
    group = SingleFileWriteGroup(
        staging,
        max_inflight_bytes=256,
        flush_threshold_bytes=128,
    )
    arr = group.register_level("0", (2, 64, 64), np.dtype("uint16"), 32)
    chunked = _SingleFileStagingBackend().chunk_array(arr)
    rng = np.random.default_rng(99)
    coords = [(c, ty, tx) for c in range(2) for ty in range(2) for tx in range(2)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                chunked.write,
                coord,
                rng.integers(1, 65000, size=(1, 32, 32), dtype=np.uint16),
            )
            for coord in coords
        ]
        for f in futures:
            f.result(timeout=5.0)
    group.flush()
    group.assert_all_coords_accounted()
    group.close_handles()

    # Verify BaseException (KeyboardInterrupt) inside store.batch resets _flushing cleanly.
    staging2 = tmp_path / "kbd.nd2svs.building-0002"
    group2 = SingleFileWriteGroup(
        staging2, max_inflight_bytes=2048, flush_threshold_bytes=512
    )
    arr2 = group2.register_level("0", (1, 32, 64), np.dtype("uint16"), 32)
    chunked2 = _SingleFileStagingBackend().chunk_array(arr2)

    def interrupt_batch():
        raise KeyboardInterrupt("simulated ctrl-c")

    monkeypatch.setattr(group2.store, "batch", interrupt_batch)
    with pytest.raises(KeyboardInterrupt):
        chunked2.write((0, 0, 0), rng.integers(1, 65000, size=(1, 32, 32), dtype=np.uint16))
        group2.flush()
    assert not group2._flushing
    assert group2._flushing_bytes == 0
    group2.close_handles(suppress_errors=True)


def test_reopened_rowid_verification_detects_single_byte_corruption(tmp_path):
    slide = tmp_path / "corrupt.nd2"
    slide.write_bytes(b"slide-bytes" * 64)
    fp = quick_fingerprint(slide)
    final_path = tmp_path / "corrupt.nd2svs"
    staging_db = tmp_path / "corrupt.nd2svs.building-abcd"

    group = SingleFileWriteGroup(staging_db)
    arr = group.register_level("0", (1, 16, 16), np.dtype("uint16"), 16)
    chunked = _SingleFileStagingBackend().chunk_array(arr)
    chunked.write((0, 0, 0), np.arange(256, dtype=np.uint16).reshape(1, 16, 16))
    group.flush()
    manifest = build_manifest(
        final_path,
        slide,
        fp,
        {"t": 0, "p": 0, "z": 0},
        0,
        16,
        (1, 16, 16),
        "uint16",
        single_file=True,
        complete=False,
    )
    orig_close = group.close_handles

    def close_then_corrupt(*, suppress_errors: bool = False) -> None:
        orig_close(suppress_errors=suppress_errors)
        if not suppress_errors:
            with sqlite3.connect(staging_db) as conn:
                raw = conn.execute(
                    "SELECT value FROM entries WHERE key='store.ome.zarr/0/0.0.0'"
                ).fetchone()[0]
                mutated = bytearray(raw)
                mutated[-1] ^= 0xFF
                conn.execute(
                    "UPDATE entries SET value=? WHERE key='store.ome.zarr/0/0.0.0'",
                    (bytes(mutated),),
                )
                conn.commit()

    group.close_handles = close_then_corrupt  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="verification failed"):
        verify_and_finalize_single_file(
            group,
            staging_path=staging_db,
            source=slide,
            before_fp=fp,
            manifest=manifest,
            validate_fn=lambda _p: None,
        )
    assert read_manifest(staging_db) is None  # complete stayed False
    assert not final_path.exists()


def test_iter_entries_by_rowid_pagination_prefix_nonblob_and_format1(tmp_path):
    db = tmp_path / "paged.nd2svs"
    entries = {
        "manifest.json": b'{"complete": true}',
        "store.ome.zarr/.zgroup": b'{"zarr_format": 2}',
        "store.ome.zarr/0/0.0.0": b"A" * 500,
        "store.ome.zarr/0/0.0.1": b"B" * 3000,  # single entry > max_batch_bytes=1024
        "store.ome.zarr/0/0.0.2": b"C" * 400,
    }
    with SQLiteStore(db) as st:
        with st.batch():
            for k, v in entries.items():
                st.write_bytes(k, v)

    with SQLiteStore(db, read_only=True) as ro:
        got = dict(ro.iter_entries_by_rowid(batch_rows=2, max_batch_bytes=1024))
        assert got == entries

    with SQLiteStore(db, read_only=True, prefix="store.ome.zarr") as child:
        got_child = dict(child.iter_entries_by_rowid(batch_rows=2, max_batch_bytes=1024))
        assert got_child == {
            k.removeprefix("store.ome.zarr/"): v
            for k, v in entries.items()
            if k.startswith("store.ome.zarr/")
        }

    # Format-1 WITHOUT ROWID fallback
    fmt1 = tmp_path / "legacy_v1.nd2svs"
    with sqlite3.connect(fmt1) as conn:
        conn.execute(
            "CREATE TABLE entries (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID"
        )
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT INTO entries(key,value) VALUES('a', ?)", (b"hello",))
        conn.commit()
    with SQLiteStore(fmt1, read_only=True) as ro_v1:
        assert list(ro_v1.iter_entries_by_rowid()) == [("a", b"hello")]

    # Tampered non-BLOB value raises ValueError rather than TypeError
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE entries SET value=12345 WHERE key='manifest.json'")
        conn.commit()
    with SQLiteStore(db, read_only=True) as ro_bad:
        with pytest.raises(ValueError, match="not a BLOB"):
            list(ro_bad.iter_entries_by_rowid(batch_rows=2, max_batch_bytes=1024))


def test_retry_windows_staging_op_and_file_exists_and_header_checks(tmp_path, symlink_support):
    # 1. FileExistsError must never delete a pre-existing file
    preexisting = tmp_path / "pre.nd2svs.building-9999"
    preexisting.write_bytes(b"keep-me")
    with pytest.raises(FileExistsError):
        SingleFileWriteGroup(preexisting)
    assert preexisting.read_bytes() == b"keep-me"

    # 2. Retry transient WinError 32 and wrapped sqlite3.OperationalError (SQLITE_BUSY=5)
    calls = 0

    def flaky_perm() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            err = PermissionError("sharing violation")
            err.winerror = 32
            raise err
        return "ok"

    assert retry_windows_staging_op(flaky_perm, base_delay=0.001, platform="nt") == "ok"
    assert calls == 3

    calls_sql = 0

    def flaky_sql() -> str:
        nonlocal calls_sql
        calls_sql += 1
        if calls_sql < 3:
            op = sqlite3.OperationalError("database is locked")
            op.sqlite_errorcode = 261  # SQLITE_BUSY_RECOVERY (261 & 0xFF == 5)
            raise ValueError("wrapped") from op
        return "ok-sql"

    assert retry_windows_staging_op(flaky_sql, base_delay=0.001, platform="nt") == "ok-sql"
    assert calls_sql == 3

    # Non-transient SQLITE_CANTOPEN_ISDIR (526) is NOT retried
    isdir_calls = 0

    def non_transient_isdir() -> None:
        nonlocal isdir_calls
        isdir_calls += 1
        op = sqlite3.OperationalError("unable to open database file")
        op.sqlite_errorcode = 526
        raise op

    with pytest.raises(sqlite3.OperationalError):
        retry_windows_staging_op(non_transient_isdir, base_delay=0.001, platform="nt")
    assert isdir_calls == 1

    # 3. Symlinked .nd2svs remains readable via SQLiteStore while rejected by destructive header probe
    real_db = tmp_path / "real.nd2svs"
    with SQLiteStore(real_db) as st:
        st.write_bytes("k", b"v")
    sym_db = tmp_path / "link.nd2svs"
    sym_db.symlink_to(real_db)
    with SQLiteStore(sym_db, read_only=True) as ro:
        assert ro.read_bytes("k") == b"v"
    assert is_owned_rollback_sqlite_header(real_db) is True
    assert is_owned_rollback_sqlite_header(sym_db) is False


def test_sweep_stale_builds_owned_family_and_orphan_journal(tmp_path):
    final = tmp_path / "slide.nd2--t0-p0-zmid.nd2svs"
    crashed = tmp_path / f"{final.name}.building-aaaa"
    with SQLiteStore(crashed) as st:
        st.write_bytes("k", b"v")
    crashed.with_name(crashed.name + "-journal").write_bytes(
        b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 16
    )

    # Orphan rollback journal with valid magic vs. non-journal file
    orphan_valid = tmp_path / f"{final.name}.building-bbbb-journal"
    orphan_valid.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 16)
    orphan_zero = tmp_path / f"{final.name}.building-cccc-journal"
    orphan_zero.write_bytes(b"")
    orphan_bogus = tmp_path / f"{final.name}.building-dddd-journal"
    orphan_bogus.write_bytes(b"not-a-sqlite-journal-at-all")

    with CacheLock(final):
        # Without owned_families, our own live CacheLock protects the family.
        assert sweep_stale_builds(tmp_path) == 0
        assert crashed.exists()
        # With owned_families=(final.name,), crashed DB+journal + valid orphans are swept.
        assert sweep_stale_builds(tmp_path, owned_families=(final.name,)) == 3
        assert not crashed.exists()
        assert not crashed.with_name(crashed.name + "-journal").exists()
        assert not orphan_valid.exists()
        assert not orphan_zero.exists()
        assert orphan_bogus.exists()
