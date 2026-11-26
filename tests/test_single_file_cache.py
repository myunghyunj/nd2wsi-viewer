"""Single-file cache publication and cleanup use synthetic temporary data only."""

import os
import sqlite3
from pathlib import Path

import pytest

from nd2wsi.cache import (
    CacheFromNewerApp,
    CacheLock,
    cache_container,
    commit_container,
    is_file_container,
    newer_cache_format,
    quarantine,
    sweep_stale_builds,
)
from nd2wsi.convert import ensure_cache
from nd2wsi.storage.single_file import MAX_READABLE_VERSION, SQLiteStore, read_entry


def _write_cache(path, value=b"retained bytes"):
    with SQLiteStore(path) as store:
        store.write_bytes("marker", value)


def _snapshot(folder):
    return {
        path.relative_to(folder).as_posix(): (
            ("symlink", os.readlink(path)) if path.is_symlink() else ("file", path.read_bytes())
        )
        for path in folder.rglob("*") if path.is_symlink() or path.is_file()
    }


def _make_companion(path, suffix, symlink):
    companion = path.with_name(path.name + suffix)
    if symlink:
        try:
            companion.symlink_to(path.parent / "missing-companion-target")
        except OSError:
            pytest.skip("symlink creation is unavailable")
    else:
        companion.write_bytes(b"preserve interrupted transaction")
    return companion


def test_quarantine_reports_windows_busy_cache_without_removing_it(tmp_path, monkeypatch):
    cache = tmp_path / 'busy.nd2svs'
    _write_cache(cache)
    before = cache.read_bytes()

    def busy(*args):
        error = PermissionError('simulated sharing violation')
        error.winerror = 32
        raise error

    monkeypatch.setattr(Path, 'rename', busy)
    with pytest.raises(PermissionError, match='close all windows'):
        quarantine(cache)
    assert cache.read_bytes() == before
    assert not list(tmp_path.glob('*.corrupt-*'))


def test_ensure_cache_refuses_future_storage_header_without_opening_sqlite_or_changing_files(tmp_path, monkeypatch):
    source = tmp_path / "source.nd2"
    source.write_bytes(b"synthetic source must not be parsed or modified")
    container = cache_container(source)
    _write_cache(container)
    with sqlite3.connect(container) as connection:
        connection.execute(f"PRAGMA user_version={MAX_READABLE_VERSION + 1}")
    before = _snapshot(tmp_path)

    def must_not_connect(*args, **kwargs):
        raise AssertionError("future format must be rejected from the header alone")

    monkeypatch.setattr(sqlite3, "connect", must_not_connect)
    with pytest.raises(CacheFromNewerApp, match="newer nd2wsi-viewer"):
        ensure_cache(source)
    assert _snapshot(tmp_path) == before
    assert not list(container.parent.glob("*.corrupt-*"))
    assert not list(container.parent.glob("*.building-*"))
    assert not list(container.parent.glob("*.lock*"))


@pytest.mark.parametrize("suffix", [".nd2svs", ".ND2SVS"])
def test_future_single_file_suffix_recognition_is_case_insensitive(tmp_path, suffix):
    path = tmp_path / ("source" + suffix)
    _write_cache(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={MAX_READABLE_VERSION + 1}")
    before = path.read_bytes()
    assert is_file_container(path)
    assert newer_cache_format(path) == f"nd2svs-sqlite/{MAX_READABLE_VERSION + 1}"
    assert path.read_bytes() == before


@pytest.mark.parametrize("family", [
    "slide.nd2--t0-p0-zmid.nd2svs",
    "slide.building-round2.nd2--t0-p0-zmid.nd2svs",
    "slide.replaced-earlier.nd2--t0-p0-zmid.nd2svs",
    ".hidden.nd2--t0-p0-zmid.nd2svs",
    "UPPER.ND2--t0-p0-zmid.ND2SVS",
])
@pytest.mark.parametrize("suffix", ["building", "replaced"])
def test_live_uuid_artifact_uses_the_exact_family_even_with_separator_text(tmp_path, family, suffix):
    container = tmp_path / family
    artifact = tmp_path / f"{family}.{suffix}-0123456789abcdef0123456789abcdef"
    _write_cache(artifact)
    before = artifact.read_bytes()
    with CacheLock(container):
        assert sweep_stale_builds(tmp_path) == 0
        assert artifact.read_bytes() == before


def test_dead_recognized_uuid_file_is_swept_but_unknown_artifacts_are_preserved(tmp_path):
    dead = tmp_path / ("dead.nd2--t0-p0-zmid.nd2svs.building-" + "a" * 32)
    _write_cache(dead)
    unknown_file = tmp_path / ("notes.nd2svs.building-" + "b" * 32)
    unknown_file.write_bytes(b"user notes, not a database")
    unknown_dir = tmp_path / "research-notes.building-current"
    unknown_dir.mkdir()
    (unknown_dir / "annotations.json").write_bytes(b"important annotations")
    unrelated_database = tmp_path / "research.sqlite.building-current"
    _write_cache(unrelated_database)
    future = tmp_path / ("future.nd2svs.building-" + "c" * 32)
    _write_cache(future)
    with sqlite3.connect(future) as connection:
        connection.execute(f"PRAGMA user_version={MAX_READABLE_VERSION + 1}")
    expected = _snapshot(tmp_path)
    expected.pop(dead.name)
    assert sweep_stale_builds(tmp_path) == 1
    assert not dead.exists()
    assert _snapshot(tmp_path) == expected


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("symlink", [False, True])
def test_quarantine_refuses_companions_and_preserves_the_complete_family(tmp_path, suffix, symlink):
    cache = tmp_path / "original.nd2svs"
    _write_cache(cache)
    _make_companion(cache, suffix, symlink)
    before = _snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="journal|companion"):
        quarantine(cache)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("location", ["staging", "existing-final", "missing-final"])
def test_publication_refuses_companions_on_either_name_before_any_rename(tmp_path, suffix, symlink, location):
    staging = tmp_path / ("source.nd2svs.building-" + "d" * 32)
    final = tmp_path / "source.nd2svs"
    _write_cache(staging, b"new cache")
    if location != "missing-final":
        _write_cache(final, b"old cache")
    _make_companion(staging if location == "staging" else final, suffix, symlink)
    before = _snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="journal|companion"):
        commit_container(staging, final)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_stale_sweep_does_not_detach_a_cache_from_its_companion(tmp_path, suffix):
    staging = tmp_path / ("source.nd2svs.building-" + "e" * 32)
    _write_cache(staging)
    _make_companion(staging, suffix, False)
    before = _snapshot(tmp_path)
    assert sweep_stale_builds(tmp_path) == 0
    assert _snapshot(tmp_path) == before


def test_stale_sweep_reclaims_dead_building_journal_pair_and_preserves_replaced_and_symlinks(
    tmp_path,
):
    # Dead .nd2svs.building-* + regular -journal pair is reclaimed (returns 1).
    dead = tmp_path / ("source.nd2svs.building-" + "e" * 32)
    _write_cache(dead)
    _make_companion(dead, "-journal", False)
    assert sweep_stale_builds(tmp_path) == 1
    assert not dead.exists()
    assert not dead.with_name(dead.name + "-journal").exists()

    # Live-locked .nd2svs.building-* + -journal stays preserved.
    live_final = tmp_path / "live.nd2svs"
    live_staging = tmp_path / ("live.nd2svs.building-" + "1" * 32)
    _write_cache(live_staging)
    live_journal = _make_companion(live_staging, "-journal", False)
    live_bytes = live_staging.read_bytes()
    live_j_bytes = live_journal.read_bytes()
    with CacheLock(live_final):
        assert sweep_stale_builds(tmp_path) == 0
        assert live_staging.read_bytes() == live_bytes
        assert live_journal.read_bytes() == live_j_bytes

    # .replaced-* backups with any companion (-journal/-wal/-shm) remain preserved.
    replaced_final = tmp_path / "replaced.nd2svs"
    _write_cache(replaced_final)
    replaced_files: list[tuple[Path, bytes]] = []
    for idx, suffix in enumerate(("-journal", "-wal", "-shm")):
        rep = tmp_path / (f"replaced.nd2svs.replaced-{idx:08x}")
        _write_cache(rep)
        comp = _make_companion(rep, suffix, False)
        replaced_files.extend([(rep, rep.read_bytes()), (comp, comp.read_bytes())])
    with CacheLock(live_final):
        assert sweep_stale_builds(tmp_path) == 0
        for p, expected_bytes in replaced_files:
            assert p.read_bytes() == expected_bytes

    # Symlinked -journal beside dead .building-* is preserved (fail closed).
    sym_staging = tmp_path / ("sym.nd2svs.building-" + "2" * 32)
    _write_cache(sym_staging)
    sym_journal = _make_companion(sym_staging, "-journal", True)
    sym_bytes = sym_staging.read_bytes()
    with CacheLock(live_final):
        assert sweep_stale_builds(tmp_path) == 0
        assert sym_staging.read_bytes() == sym_bytes
        assert sym_journal.is_symlink()


def test_publication_preserves_previous_file_if_atomic_replace_fails(tmp_path, monkeypatch):
    from nd2wsi.platform_io import filesystem_path

    staging = tmp_path / ("source.nd2svs.building-" + "f" * 32)
    final = tmp_path / "source.nd2svs"
    _write_cache(staging, b"new cache")
    _write_cache(final, b"old cache")
    before = _snapshot(tmp_path)

    def fail_publish(source, destination):
        assert source == filesystem_path(staging) and destination == filesystem_path(final)
        raise OSError("injected publication failure")

    monkeypatch.setattr(os, "replace", fail_publish)
    with pytest.raises(OSError, match="injected publication"):
        commit_container(staging, final)
    assert _snapshot(tmp_path) == before
    assert read_entry(final, "marker") == b"old cache"


def test_closed_single_file_publication_keeps_only_the_new_final_file(tmp_path):
    staging = tmp_path / ("source.nd2svs.building-" + "f" * 32)
    final = tmp_path / "source.nd2svs"
    _write_cache(staging, b"new cache")
    _write_cache(final, b"old cache")
    commit_container(staging, final)
    assert list(tmp_path.iterdir()) == [final]
    assert read_entry(final, "marker") == b"new cache"


def test_commit_container_and_remove_artifact_refuse_symlinks_and_junctions(
    tmp_path, monkeypatch, symlink_support
):
    import shutil

    from nd2wsi import cache as cache_mod
    from nd2wsi.cache import _remove_artifact

    # Directory staging -> symlinked final directory is refused before branching.
    staging_dir = tmp_path / "staged.ome.zarr"
    staging_dir.mkdir()
    (staging_dir / ".zgroup").write_text('{"zarr_format": 2}', encoding="utf-8")
    real_user_dir = tmp_path / "real_user_dir"
    real_user_dir.mkdir()
    (real_user_dir / "keep.txt").write_bytes(b"user-data")
    sym_final = tmp_path / "sym_final.ome.zarr"
    sym_final.symlink_to(real_user_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic links"):
        commit_container(staging_dir, sym_final)
    assert (real_user_dir / "keep.txt").read_bytes() == b"user-data"
    assert staging_dir.is_dir()

    # Dangling symlink at final is also refused.
    dangling = tmp_path / "dangling.nd2svs"
    dangling.symlink_to(tmp_path / "nonexistent-target")
    staging_file = tmp_path / "staged.nd2svs"
    _write_cache(staging_file, b"new")
    with pytest.raises(ValueError, match="symbolic links"):
        commit_container(staging_file, dangling)
    assert dangling.is_symlink()

    # Symlinked staging is refused.
    sym_staging = tmp_path / "sym_staging.nd2svs"
    sym_staging.symlink_to(staging_file)
    with pytest.raises(ValueError, match="symbolic links"):
        commit_container(sym_staging, tmp_path / "dest.nd2svs")

    # Simulated Windows junction at final is refused for directory and file commits.
    junc_dir = tmp_path / "junc_dir"
    junc_dir.mkdir()
    (junc_dir / "keep.txt").write_bytes(b"junction-target")
    junc_ino = os.lstat(junc_dir).st_ino
    monkeypatch.setattr(
        cache_mod,
        "is_windows_name_surrogate_reparse",
        lambda st, fd=None: getattr(st, "st_ino", None) == junc_ino,
    )
    with pytest.raises(ValueError, match="junctions"):
        commit_container(staging_dir, junc_dir)
    with pytest.raises(ValueError, match="junctions"):
        commit_container(staging_file, junc_dir)

    # _remove_artifact refuses a Windows junction before calling shutil.rmtree.
    rmtree_calls = []
    real_rmtree = shutil.rmtree

    def spy_rmtree(p, *a, **kw):
        rmtree_calls.append(p)
        return real_rmtree(p, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", spy_rmtree)
    with pytest.raises(ValueError, match="junction"):
        _remove_artifact(junc_dir)
    assert rmtree_calls == []
    assert (junc_dir / "keep.txt").read_bytes() == b"junction-target"


def _make_synthetic_svs(
    tmp_path: Path,
    name: str = "slide.svs",
    shape: tuple[int, int, int] = (900, 1200, 3),
    seed: int = 3,
):
    import numpy as np

    tifffile = pytest.importorskip("tifffile")
    pytest.importorskip("imagecodecs")
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, shape, dtype=np.uint8)
    path = tmp_path / name
    tifffile.imwrite(
        path,
        img,
        tile=(240, 240),
        photometric="rgb",
        compression="jpeg2000",
        compressionargs={"reversible": True},
        description="Aperio Image|MPP = 0.5|AppMag = 20",
    )
    return path, img


def _make_synthetic_nd2_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "sample.nd2",
    shape: tuple[int, int, int] = (4, 600, 620),
    seed: int = 11,
):
    """Create a real uncompressed interleaved file on disk + ND2File/open_plane shim."""
    import mmap
    from types import SimpleNamespace

    import dask.array as da
    import nd2
    import numpy as np

    from nd2wsi import convert_cache as cc
    from nd2wsi.reader import ChannelInfo, PlaneSource

    rng = np.random.default_rng(seed)
    c, h, w = shape
    data = rng.integers(0, 4096, size=shape, dtype=np.uint16)
    # Leave one quadrant zero to exercise all-zero chunk omission too.
    data[:, :256, :256] = 0

    header = b"FAKE_ND2_HEADER_" * 8
    interleaved = np.ascontiguousarray(data.transpose(1, 2, 0))
    path = tmp_path / name
    path.write_bytes(header + interleaved.tobytes())

    class _FakeND2:
        def __init__(self, target: str) -> None:
            self.path = Path(target)
            self.is_legacy = False
            self.attributes = SimpleNamespace(compressionType="none")
            self.dtype = data.dtype
            self.sizes = {"Y": h, "X": w, "C": c}
            self._fh = open(self.path, "rb")
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._mm.close()
            self._fh.close()

        def read_frame(self, idx: int) -> np.ndarray:
            assert idx == 0
            itemsize = data.dtype.itemsize
            x_stride = c * itemsize
            y_stride = w * x_stride
            return np.ndarray(
                shape=(c, h, w),
                dtype=data.dtype,
                buffer=self._mm,
                offset=len(header),
                strides=(itemsize, y_stride, x_stride),
            )

    def _fake_open_plane(handle, selection, tile=512):
        arr = da.from_array(data, chunks=(1, tile, tile))
        channels = [
            ChannelInfo(name=f"CH{i}", color=((i * 60) % 256, 200, 120))
            for i in range(c)
        ]
        return PlaneSource(
            data=arr,
            shape=(c, h, w),
            dtype=data.dtype,
            rgb=False,
            channels=channels,
            pixel_size_um=(0.5, 0.5),
            source_name=path.name,
            selection={**selection.describe(), "z": 0},
            notes=[],
            calibration_source="synthetic",
        )

    monkeypatch.setattr(nd2, "ND2File", _FakeND2)
    monkeypatch.setattr(cc, "open_plane", _fake_open_plane)
    return path, data


def test_ensure_cache_direct_sqlite_has_no_loose_staging_and_reclaims_same_slide_crash(
    tmp_path, monkeypatch
):
    import json

    from nd2wsi import convert as convert_mod
    from nd2wsi import migration as migration_mod
    from nd2wsi.cache import SINGLE_FILE_FORMAT, read_manifest
    from nd2wsi.convert import open_store
    from nd2wsi.storage.single_file import APPLICATION_ID, FORMAT_VERSION

    slide, img = _make_synthetic_svs(tmp_path)
    container = cache_container(slide)
    container.parent.mkdir(parents=True, exist_ok=True)

    # Plant a crashed .building-* + -journal pair for THIS exact slide before reopen.
    crashed_db = container.parent / (container.name + ".building-" + "a" * 32)
    _write_cache(crashed_db, b"dead prior build")
    crashed_journal = _make_companion(crashed_db, "-journal", False)

    def must_not_pack(*args, **kwargs):
        raise AssertionError("direct-to-SQLite ensure_cache must not call pack_directory")

    monkeypatch.setattr(migration_mod, "pack_directory", must_not_pack)

    observed_mid_build: list[tuple[list[str], list[str]]] = []
    real_downsample = convert_mod._downsample_into

    def spy_downsample(*args, **kwargs):
        entries = list(container.parent.iterdir())
        dirs = [p.name for p in entries if p.is_dir() and ".building-" in p.name]
        files = [p.name for p in entries if p.is_file() and ".building-" in p.name]
        observed_mid_build.append((dirs, files))
        return real_downsample(*args, **kwargs)

    monkeypatch.setattr(convert_mod, "_downsample_into", spy_downsample)

    store = ensure_cache(slide, tile=256)
    assert store == container
    assert container.is_file()
    assert not crashed_db.exists()
    assert not crashed_journal.exists()
    assert not list(container.parent.glob("*.building-*"))
    assert observed_mid_build, "_downsample_into should have executed"
    for dirs, files in observed_mid_build:
        assert dirs == [], f"unexpected loose staging directories: {dirs}"
        assert len(files) == 1 and files[0].startswith(container.name + ".building-")

    # Verify exact v2.6.10/v2.6.11 SQLite header, manifest, Zarr v2 metadata, and codec.
    with sqlite3.connect(f"file:{container}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert conn.execute("PRAGMA user_version").fetchone()[0] == FORMAT_VERSION

    manifest = read_manifest(container)
    assert manifest is not None
    assert manifest["format"] == SINGLE_FILE_FORMAT == "nd2wsi-cache/4"
    assert manifest["complete"] is True
    assert manifest["kind"] == "full"
    assert manifest["storage"] == {
        "format": "zarr",
        "zarr_version": 2,
        "ngff_version": "0.4",
        "backend": "zarr-v2-direct",
        "container": f"nd2svs-sqlite/{FORMAT_VERSION}",
    }

    with SQLiteStore(container, read_only=True) as ro:
        assert json.loads(ro.read_bytes("store.ome.zarr/.zgroup")) == {"zarr_format": 2}
        zattrs = json.loads(ro.read_bytes("store.ome.zarr/.zattrs"))
        assert "multiscales" in zattrs and "omero" in zattrs and "nd2wsi" in zattrs
        for lv in zattrs["nd2wsi"]["levels"]:
            lv_path = lv["path"]
            zarray = json.loads(ro.read_bytes(f"store.ome.zarr/{lv_path}/.zarray"))
            assert zarray["compressor"] == {
                "id": "blosc",
                "cname": "zstd",
                "clevel": 3,
                "shuffle": 2,
                "blocksize": 0,
            }
            assert json.loads(ro.read_bytes(f"store.ome.zarr/{lv_path}/.zattrs")) == {}

    root, _ = open_store(store)
    try:
        import numpy as np

        got = np.moveaxis(np.asarray(root["0"][:, :16, :16]), 0, -1)
        assert np.array_equal(got, img[:16, :16])
    finally:
        root.close()


def test_fast_fail_source_mutation_after_first_level_preserves_prior_cache(
    tmp_path, monkeypatch
):
    from nd2wsi import convert as convert_mod
    from nd2wsi import convert_cache as cc
    from nd2wsi.cache import read_manifest

    slide, _ = _make_synthetic_svs(tmp_path)
    store = ensure_cache(slide, tile=256)
    container = cache_container(slide)
    assert store == container
    prior_bytes = container.read_bytes()
    prior_gen = read_manifest(container)["generation"]

    # Force a rebuild by rewriting slide with new pixels and bumped mtime,
    # then mutate the source's mtime right as first_level finishes so the
    # post-first_level fast-fail triggers before any later level begins.
    slide, _ = _make_synthetic_svs(tmp_path, seed=99)
    st = slide.stat()
    os.utime(slide, ns=(st.st_atime_ns + 2_000_000_000, st.st_mtime_ns + 2_000_000_000))

    real_write_l0 = cc._write_level0

    def mutate_during_l0(*args, **kwargs):
        result = real_write_l0(*args, **kwargs)
        cur = slide.stat()
        os.utime(slide, ns=(cur.st_atime_ns + 5_000_000_000, cur.st_mtime_ns + 5_000_000_000))
        return result

    downsample_calls = {"n": 0}
    real_ds = convert_mod._downsample_into

    def spy_ds(*args, **kwargs):
        downsample_calls["n"] += 1
        return real_ds(*args, **kwargs)

    monkeypatch.setattr(cc, "_write_level0", mutate_during_l0)
    monkeypatch.setattr(convert_mod, "_downsample_into", spy_ds)

    with pytest.raises(RuntimeError, match="changed while building its cache"):
        ensure_cache(slide, tile=256)

    assert downsample_calls["n"] == 0, "fast-fail must abort before later pyramid levels"
    assert container.read_bytes() == prior_bytes
    assert read_manifest(container)["generation"] == prior_gen
    assert not list(container.parent.glob("*.building-*"))
    assert not list(container.parent.glob("*.corrupt-*"))


def test_cli_and_python_nd2svs_export_overwrite_guards_progress_and_cleanup(
    tmp_path, monkeypatch, symlink_support
):
    import numpy as np
    import zarr

    from nd2wsi import cache as cache_mod
    from nd2wsi import convert as convert_mod
    from nd2wsi import migration as migration_mod
    from nd2wsi.cache import SINGLE_FILE_FORMAT, read_manifest
    from nd2wsi.cli import main as cli_main
    from nd2wsi.convert import open_store
    from nd2wsi.storage import single_file_writer as sfw

    slide, img = _make_synthetic_svs(tmp_path)

    def must_not_pack(*args, **kwargs):
        raise AssertionError("direct-to-SQLite export must not call pack_directory")

    monkeypatch.setattr(migration_mod, "pack_directory", must_not_pack)

    # 1. CLI convert slide -> .nd2svs (overview=False) sets origin="export" and writes directly.
    cli_out = tmp_path / "exported.nd2svs"
    assert cli_main(["convert", str(slide), str(cli_out), "--tile", "256", "--yes"]) == 0
    assert cli_out.is_file()
    manifest = read_manifest(cli_out)
    assert manifest is not None
    assert manifest["origin"] == "export"
    assert manifest["format"] == SINGLE_FILE_FORMAT == "nd2wsi-cache/4"
    assert manifest["complete"] is True
    assert manifest["kind"] == "full"
    root, _ = open_store(cli_out)
    try:
        got = np.moveaxis(np.asarray(root["0"][:, :16, :16]), 0, -1)
        assert np.array_equal(got, img[:16, :16])
    finally:
        root.close()

    # 2. Without overwrite=True, refuses existing .nd2svs; with overwrite=True, replaces it.
    with pytest.raises(FileExistsError):
        convert_mod.convert(slide, cli_out, tile=256, overwrite=False, progress=False)

    events: list[tuple[float, bool]] = []
    prior_mtime = cli_out.stat().st_mtime_ns
    orig_replace = os.replace
    replaced = {"done": False}

    from nd2wsi.platform_io import filesystem_path

    expected_dst = filesystem_path(cli_out)

    def spy_replace(src, dst):
        res = orig_replace(src, dst)
        if filesystem_path(dst) == expected_dst:
            replaced["done"] = True
        return res

    with monkeypatch.context() as mp:
        mp.setattr(os, "replace", spy_replace)
        convert_mod.convert(
            slide,
            cli_out,
            tile=256,
            overwrite=True,
            progress=False,
            on_progress=lambda frac: events.append((frac, replaced["done"])),
        )
    assert prior_mtime >= 0
    fracs = [f for f, _ in events]
    assert fracs == sorted(fracs)
    assert fracs[-1] == 1.0
    assert events[-1][1] is True, "1.0 progress must fire strictly after publication"
    assert all(not done for f, done in events[:-1] if f < 1.0)
    assert not list(tmp_path.glob("*.building-*"))

    # 3. Replacing an existing legacy .nd2svs/ directory writes format="nd2wsi-cache/4".
    legacy_dir = tmp_path / "legacy_export.nd2svs"
    grp = zarr.open_group(str(legacy_dir), mode="w", zarr_format=2)
    grp.attrs.update({"nd2wsi": {"source": "old"}})
    grp.store.close()
    assert legacy_dir.is_dir()
    convert_mod.convert(slide, legacy_dir, tile=256, overwrite=True, progress=False)
    assert legacy_dir.is_file()
    leg_manifest = read_manifest(legacy_dir)
    assert leg_manifest is not None
    assert leg_manifest["format"] == "nd2wsi-cache/4"
    assert leg_manifest["origin"] == "export"
    assert leg_manifest["complete"] is True
    root, _ = open_store(legacy_dir)
    root.close()

    # 4. Refuses non-Zarr directory, symlink, and simulated Windows junction.
    non_zarr = tmp_path / "notes.nd2svs"
    non_zarr.mkdir()
    (non_zarr / "keep.txt").write_bytes(b"important")
    with pytest.raises(IsADirectoryError, match="non-Zarr"):
        convert_mod.convert(slide, non_zarr, tile=256, overwrite=True, progress=False)
    assert (non_zarr / "keep.txt").read_bytes() == b"important"

    sym_out = tmp_path / "sym.nd2svs"
    sym_out.symlink_to(cli_out)
    with pytest.raises(ValueError, match="symbolic links"):
        convert_mod.convert(slide, sym_out, tile=256, overwrite=True, progress=False)

    junc_dir = tmp_path / "junc.nd2svs"
    junc_dir.mkdir()
    (junc_dir / "keep.txt").write_bytes(b"junction-keep")
    junc_ino = os.lstat(junc_dir).st_ino
    with monkeypatch.context() as mp:
        from nd2wsi import convert_cache as cc

        mp.setattr(
            cc,
            "_validate_convert_target",
            cc._validate_convert_target,
        )
        mp.setattr(
            cache_mod,
            "is_windows_name_surrogate_reparse",
            lambda st, fd=None: getattr(st, "st_ino", None) == junc_ino,
        )
        from nd2wsi import windows_fs as wfs

        mp.setattr(
            wfs,
            "is_windows_name_surrogate_reparse",
            lambda st, fd=None: getattr(st, "st_ino", None) == junc_ino,
        )
        with pytest.raises(ValueError, match="junctions"):
            convert_mod.convert(slide, junc_dir, tile=256, overwrite=True, progress=False)
    assert (junc_dir / "keep.txt").read_bytes() == b"junction-keep"

    # 5. Mid-build failure removes staging DB + -journal.
    fail_out = tmp_path / "midfail.nd2svs"
    real_ds = convert_mod._downsample_into

    def fail_ds(*args, **kwargs):
        stagings = list(tmp_path.glob(f".{fail_out.name}.building-*"))
        assert len(stagings) == 1
        _make_companion(stagings[0], "-journal", False)
        raise RuntimeError("injected export mid-build failure")

    with monkeypatch.context() as mp:
        mp.setattr(convert_mod, "_downsample_into", fail_ds)
        with pytest.raises(RuntimeError, match="injected export mid-build failure"):
            convert_mod.convert(slide, fail_out, tile=256, progress=False)
    assert not fail_out.exists()
    assert not list(tmp_path.glob(f".{fail_out.name}.building-*"))
    assert real_ds is not None

    # 6. Post-build TOCTOU FileExistsError preserves raced target and cleans staging DB.
    toctou_out = tmp_path / "toctou.nd2svs"
    real_verify = sfw.verify_and_finalize_single_file

    def race_verify(*args, **kwargs):
        real_verify(*args, **kwargs)
        toctou_out.write_bytes(b"created-during-build")

    with monkeypatch.context() as mp:
        mp.setattr(sfw, "verify_and_finalize_single_file", race_verify)
        with pytest.raises(FileExistsError):
            convert_mod.convert(slide, toctou_out, tile=256, overwrite=False, progress=False)
    assert toctou_out.read_bytes() == b"created-during-build"
    assert not list(tmp_path.glob(f".{toctou_out.name}.building-*"))


def test_python_convert_overview_direct_sqlite_seq_and_dask_fallback_progress(
    tmp_path, monkeypatch
):
    import numpy as np

    from nd2wsi import convert as convert_mod
    from nd2wsi import convert_cache as cc
    from nd2wsi import migration as migration_mod
    from nd2wsi.cache import read_manifest
    from nd2wsi.convert import open_store
    from nd2wsi.storage.zarr_v2 import ZarrV2Storage

    fake_nd2, _ = _make_synthetic_nd2_fixture(tmp_path, monkeypatch)

    # 1. Sequential ND2 Level-1 + direct-to-SQLite overview export.
    seq_calls = {"n": 0}
    real_seq = cc._write_level1_sequential_nd2

    def spy_seq(*args, **kwargs):
        seq_calls["n"] += 1
        return real_seq(*args, **kwargs)

    out_seq = tmp_path / "ov_seq.nd2svs"
    seq_events: list[tuple[float, bool]] = []
    with monkeypatch.context() as mp:
        mp.setattr(cc, "_write_level1_sequential_nd2", spy_seq)
        mp.setattr(
            migration_mod,
            "pack_directory",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected pack_directory")),
        )
        convert_mod.convert(
            fake_nd2,
            out_seq,
            tile=256,
            overview=True,
            progress=False,
            on_progress=lambda f: seq_events.append((f, out_seq.is_file())),
        )
    assert seq_calls["n"] == 1
    assert out_seq.is_file()
    m_seq = read_manifest(out_seq)
    assert m_seq is not None and m_seq["kind"] == "overview" and m_seq["origin"] == "export"
    seq_fracs = [f for f, _ in seq_events]
    assert seq_fracs == sorted(seq_fracs) and seq_fracs[-1] == 1.0
    assert seq_events[-1][1] is True
    assert all(not exists for f, exists in seq_events[:-1] if f < 1.0)

    # 2. Dask fallback when sequential_nd2_layout returns None (verify _StoreProgress fires).
    out_none = tmp_path / "ov_fallback_none.nd2svs"
    none_events: list[tuple[float, bool]] = []
    with monkeypatch.context() as mp:
        mp.setattr(cc, "sequential_nd2_layout", lambda *a, **k: None)
        convert_mod.convert(
            fake_nd2,
            out_none,
            tile=256,
            overview=True,
            progress=False,
            on_progress=lambda f: none_events.append((f, out_none.is_file())),
        )
    none_fracs = [f for f, _ in none_events]
    assert none_fracs == sorted(none_fracs) and none_fracs[-1] == 1.0
    assert any(0.0 < f < 0.90 for f in none_fracs)
    assert none_events[-1][1] is True

    # 3. Dask fallback when _write_level1_sequential_nd2 returns False.
    out_false = tmp_path / "ov_fallback_false.nd2svs"
    false_events: list[tuple[float, bool]] = []
    with monkeypatch.context() as mp:
        mp.setattr(cc, "_write_level1_sequential_nd2", lambda *a, **k: False)
        convert_mod.convert(
            fake_nd2,
            out_false,
            tile=256,
            overview=True,
            progress=False,
            on_progress=lambda f: false_events.append((f, out_false.is_file())),
        )
    false_fracs = [f for f, _ in false_events]
    assert false_fracs == sorted(false_fracs) and false_fracs[-1] == 1.0
    assert any(0.0 < f < 0.90 for f in false_fracs)
    assert false_events[-1][1] is True

    # 4. Compare all 3 direct outputs against directory + pack_directory (non-identity ZarrV2Storage).
    out_packed = tmp_path / "ov_packed.nd2svs"
    convert_mod.convert(
        fake_nd2,
        out_packed,
        tile=256,
        overview=True,
        storage=ZarrV2Storage(),
        progress=False,
    )

    def _read_all(path: Path):
        r, attrs = open_store(path)
        try:
            levels = {
                lv["path"]: np.asarray(r[lv["path"]][:]) for lv in attrs["nd2wsi"]["levels"]
            }
            windows = [ch["window"] for ch in attrs["omero"]["channels"]]
            return levels, windows
        finally:
            r.close()

    ref_levels, ref_windows = _read_all(out_packed)
    for candidate in (out_seq, out_none, out_false):
        got_levels, got_windows = _read_all(candidate)
        assert set(got_levels) == set(ref_levels)
        for lv_key in ref_levels:
            assert np.array_equal(got_levels[lv_key], ref_levels[lv_key])
        assert got_windows == ref_windows


@pytest.mark.realdata
def test_real_nd2_direct_sqlite_overview_matches_directory_pack_bit_for_bit(tmp_path):
    import numpy as np

    from nd2wsi import convert as convert_mod
    from nd2wsi.convert import open_store
    from nd2wsi.storage.zarr_v2 import ZarrV2Storage

    fixture = Path(__file__).resolve().parent.parent / "docs" / "example_tissue.nd2"
    if not fixture.is_file():
        pytest.skip("docs/example_tissue.nd2 fixture unavailable")

    out_direct = tmp_path / "direct.nd2svs"
    out_packed = tmp_path / "packed.nd2svs"
    convert_mod.convert(fixture, out_direct, tile=512, overview=True, progress=False)
    convert_mod.convert(
        fixture, out_packed, tile=512, overview=True, storage=ZarrV2Storage(), progress=False
    )

    r_dir, a_dir = open_store(out_direct)
    r_pack, a_pack = open_store(out_packed)
    try:
        assert [lv["path"] for lv in a_dir["nd2wsi"]["levels"]] == [
            lv["path"] for lv in a_pack["nd2wsi"]["levels"]
        ]
        for lv in a_pack["nd2wsi"]["levels"]:
            p = lv["path"]
            assert np.array_equal(np.asarray(r_dir[p][:]), np.asarray(r_pack[p][:]))
        assert [c["window"] for c in a_dir["omero"]["channels"]] == [
            c["window"] for c in a_pack["omero"]["channels"]
        ]
    finally:
        r_dir.close()
        r_pack.close()
