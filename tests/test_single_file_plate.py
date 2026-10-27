import hashlib
import json
import os
import shutil
from types import SimpleNamespace

import numpy as np
import pytest
from test_plate import H, P, T, W, Z, value
from test_plate import plate_nd2 as _plate_nd2_fixture
from test_single_file_recovery import _crash_write

from nd2wsi.cache import quick_fingerprint, read_manifest
from nd2wsi.plate import (
    THUMB_K,
    WINDOWS_KEY,
    WINDOWS_METHOD,
    PlateSource,
    PlateStore,
    _stored_windows,
    _windows_record,
    plate_container,
)
from nd2wsi.storage.single_file import write_entry

plate_nd2 = _plate_nd2_fixture


@pytest.fixture(autouse=True)
def no_background(monkeypatch):
    monkeypatch.setattr(PlateStore, 'start', lambda self: None)


def test_plate_is_one_file_with_source_authenticated_reductions(plate_nd2):
    source = PlateSource(plate_nd2)
    path = plate_container(plate_nd2)
    try:
        assert source.store is not None and source.store.writable
        assert path.is_file() and path.suffix == '.nd2svs'
        for t in range(T):
            for p in range(P):
                for z in range(Z):
                    pixels = source.reduced(t, p, z, THUMB_K)
                    assert np.all(pixels == value(t, p, z))
        assert source.store.count() == T * P * Z
    finally:
        source.close()
    manifest = read_manifest(path)
    assert manifest['format'] == 'nd2wsi-plate/2'
    assert (path.parent / manifest['source']['relative_path']).resolve() == plate_nd2.resolve()
    assert not list(path.parent.glob(path.name + '-*'))
    assert not list(path.parent.glob(path.name + '.building-*'))
    assert not (path.parent / 'thumbs.zarr').exists()
    again = PlateSource(plate_nd2, cache_path=path)
    try:
        assert again.store.container == path
        assert again.store.count() == T * P * Z
        assert np.all(again.store.get(1, 2, 1) == value(1, 2, 1))
    finally:
        again.close()


def test_explicit_plate_cache_mismatch_is_not_rebuilt(plate_nd2):
    source = PlateSource(plate_nd2)
    source.close()
    path = plate_container(plate_nd2)
    manifest = read_manifest(path)
    manifest['source']['quick_sha256'] = '0' * 64
    write_entry(path, 'manifest.json', json.dumps(manifest).encode())
    before = path.read_bytes()
    with pytest.raises(ValueError, match='does not match'):
        PlateSource(plate_nd2, cache_path=path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(path.name + '.corrupt-*'))


@pytest.mark.parametrize("explicit", [False, True])
def test_plate_reopens_interrupted_cache_without_rebuilding(plate_nd2, explicit):
    source = PlateSource(plate_nd2)
    source.reduced(0, 0, 0, THUMB_K)
    source.close()
    path = plate_container(plate_nd2)
    committed_manifest = read_manifest(path)
    journal = _crash_write(path)
    assert read_manifest(path) is None
    reopened = PlateSource(plate_nd2, **({"cache_path": path} if explicit else {}))
    try:
        assert reopened.store is not None
        assert reopened.store.manifest == committed_manifest
        assert np.all(reopened.store.get(0, 0, 0) == value(0, 0, 0))
        assert not journal.exists()
        assert not list(path.parent.glob(path.name + ".corrupt-*"))
    finally:
        reopened.close()


def test_explicit_plate_cache_relocates_without_default_cache(tmp_path, plate_nd2):
    source = PlateSource(plate_nd2)
    source.reduced(0, 0, 0, THUMB_K)
    source.close()
    moved = tmp_path / 'moved'
    moved.mkdir()
    moved_source = moved / plate_nd2.name
    shutil.copy2(plate_nd2, moved_source)
    package = moved / 'arbitrary-name.nd2svs'
    shutil.copy2(plate_container(plate_nd2), package)
    manifest = read_manifest(package)
    manifest['source']['relative_path'] = moved_source.name
    write_entry(package, 'manifest.json', json.dumps(manifest).encode())
    reopened = PlateSource(moved_source, cache_path=package)
    try:
        assert reopened.store.container == package
        assert np.all(reopened.store.get(0, 0, 0) == value(0, 0, 0))
        assert not plate_container(moved_source).exists()
    finally:
        reopened.close()


def test_legacy_plate_directory_is_read_only_and_preserved(plate_nd2):
    source = PlateSource(plate_nd2, store=False)
    path = plate_container(plate_nd2).with_suffix('.nd2wsi-cache')
    shape = (T, P, Z, 1, H // THUMB_K, W // THUMB_K)
    try:
        PlateStore._create(source, path, quick_fingerprint(plate_nd2), shape)
    finally:
        source.close()
    before = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in path.rglob('*') if p.is_file()}
    reopened = PlateSource(plate_nd2, cache_path=path)
    try:
        assert reopened.store is not None and not reopened.store.writable
        assert np.all(reopened.reduced(0, 0, 0, THUMB_K) == value(0, 0, 0))
    finally:
        reopened.close()
    after = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in path.rglob('*') if p.is_file()}
    assert after == before
    assert not plate_container(plate_nd2).exists()


def test_registry_opens_the_exact_plate_package(plate_nd2):
    from nd2wsi.server import SlideRegistry
    source = PlateSource(plate_nd2)
    source.reduced(0, 0, 0, THUMB_K)
    source.close()
    package = plate_container(plate_nd2)
    registry = SlideRegistry()
    try:
        sid = registry.open_path(package)
        state = registry.get(sid)
        assert state.plate is not None
        assert state.plate.store.container == package
        assert state.manifest['format'] == 'nd2wsi-plate/2'
        assert registry.open_path(package) == sid
        assert np.all(state.plate.store.get(0, 0, 0) == value(0, 0, 0))
    finally:
        registry.close_all(immediate=True)


def test_failed_plate_publication_restores_existing_package(plate_nd2, monkeypatch):
    from pathlib import Path
    source = PlateSource(plate_nd2)
    source.close()
    package = plate_container(plate_nd2)
    before = package.read_bytes()
    source = PlateSource(plate_nd2, store=False)
    original = Path.replace

    def fail_publication(path, target):
        if '.nd2svs.building-' in path.name and Path(target) == package:
            raise OSError('simulated publication failure')
        return original(path, target)

    monkeypatch.setattr(Path, 'replace', fail_publication)
    try:
        shape = (T, P, Z, 1, H // THUMB_K, W // THUMB_K)
        with pytest.raises(OSError, match='publication failure'):
            PlateStore._create(source, package, quick_fingerprint(plate_nd2), shape)
    finally:
        source.close()
    assert package.read_bytes() == before
    assert not list(package.parent.glob(package.name + '.building-*'))
    assert not list(package.parent.glob(package.name + '.corrupt-*'))


def _windows(source):
    return [channel["window"] for channel in source.attrs["omero"]["channels"]]


def _no_measuring(self, *args):
    raise AssertionError("recorded display windows should spare reading frames")


def test_display_windows_are_recorded_and_reused_without_reading_frames(plate_nd2, monkeypatch):
    first = PlateSource(plate_nd2)
    try:
        measured = _windows(first)
        z_home = first.z_home
    finally:
        first.close()
    path = plate_container(plate_nd2)
    manifest = read_manifest(path)
    record = manifest[WINDOWS_KEY]
    assert manifest["format"] == "nd2wsi-plate/2"
    assert record["method"] == WINDOWS_METHOD
    assert record["generation"] == manifest["generation"]
    assert (record["t"], record["z"], record["k"]) == (0, z_home, 4)
    assert record["windows"] == measured

    monkeypatch.setattr(PlateSource, "_measure_windows", _no_measuring)
    monkeypatch.setattr(PlateSource, "frame", _no_measuring)
    again = PlateSource(plate_nd2)
    try:
        assert _windows(again) == measured
        assert again.store.manifest == manifest
    finally:
        again.close()
    explicit = PlateSource(plate_nd2, cache_path=path)
    try:
        assert _windows(explicit) == measured
    finally:
        explicit.close()
    assert read_manifest(path) == manifest

    with pytest.raises(AssertionError, match="spare reading frames"):
        PlateSource(plate_nd2, store=False)  # an open without the store never looks


def test_a_reader_uses_recorded_windows_and_never_writes_them(plate_nd2, monkeypatch):
    path = plate_container(plate_nd2)
    writer = PlateSource(plate_nd2)
    try:
        measured = _windows(writer)
        unrecorded = dict(writer.store.manifest)
        del unrecorded[WINDOWS_KEY]
        write_entry(path, "manifest.json", json.dumps(unrecorded).encode())
        reader = PlateSource(plate_nd2)
        try:
            assert not reader.store.writable
            assert _windows(reader) == measured  # measured again from the source
        finally:
            reader.close()
        assert read_manifest(path) == unrecorded
    finally:
        writer.close()

    recording = PlateSource(plate_nd2)  # the next writer records them again
    try:
        assert recording.store.writable
        monkeypatch.setattr(PlateSource, "_measure_windows", _no_measuring)
        reader = PlateSource(plate_nd2)
        try:
            assert not reader.store.writable
            assert _windows(reader) == measured
        finally:
            reader.close()
    finally:
        recording.close()
    assert read_manifest(path)[WINDOWS_KEY]["windows"] == measured


def test_a_stale_windows_record_is_measured_again_and_replaced(plate_nd2, monkeypatch):
    first = PlateSource(plate_nd2)
    try:
        measured = _windows(first)
    finally:
        first.close()
    path = plate_container(plate_nd2)
    manifest = read_manifest(path)
    manifest[WINDOWS_KEY]["method"] = "plate-low-percentile/0"
    manifest[WINDOWS_KEY]["windows"][0]["start"] = 12345.0
    write_entry(path, "manifest.json", json.dumps(manifest).encode())
    calls = []
    real_measure = PlateSource._measure_windows

    def counting(self):
        calls.append(self)
        return real_measure(self)

    monkeypatch.setattr(PlateSource, "_measure_windows", counting)
    again = PlateSource(plate_nd2)
    try:
        assert _windows(again) == measured
    finally:
        again.close()
    assert len(calls) == 1
    record = read_manifest(path)[WINDOWS_KEY]
    assert record["method"] == WINDOWS_METHOD
    assert record["windows"] == measured


def _listing(folder):
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else None


@pytest.mark.parametrize("existing", ["none", "stale"])
def test_an_open_that_fails_measuring_windows_leaves_the_cache_untouched(
    plate_nd2, monkeypatch, existing
):
    """Looking for recorded windows is read only: an open that fails while
    measuring them creates no cache and quarantines no stale one."""
    path = plate_container(plate_nd2)
    before = None
    if existing == "stale":
        PlateSource(plate_nd2).close()
        st = plate_nd2.stat()
        os.utime(plate_nd2, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        recorded = read_manifest(path)
        assert WINDOWS_KEY in recorded
        assert recorded["source"]["mtime_ns"] != quick_fingerprint(plate_nd2)["mtime_ns"]
        before = path.read_bytes()
    listing = _listing(path.parent)
    real_frame = PlateSource.frame

    def unreadable_last_site(self, t, p, z):
        if p == self.P - 1:
            raise OSError("unreadable site")
        return real_frame(self, t, p, z)

    monkeypatch.setattr(PlateSource, "frame", unreadable_last_site)
    with pytest.raises(OSError, match="unreadable site"):
        PlateSource(plate_nd2)
    assert _listing(path.parent) == listing
    if before is None:
        assert not (plate_nd2.parent / "nd2wsi").exists()
    else:
        assert path.read_bytes() == before


_DROP = object()


@pytest.mark.parametrize("keys, damage", [
    pytest.param(("generation",), "rebuilt", id="cache-rebuilt"),
    pytest.param((WINDOWS_KEY, "generation"), "older", id="record-generation"),
    pytest.param(("source", "quick_sha256"), "0" * 64, id="source-fingerprint"),
    pytest.param((WINDOWS_KEY, "source"), _DROP, id="record-without-fingerprint"),
    pytest.param((WINDOWS_KEY, "method"), "plate-low-percentile/0", id="method"),
    pytest.param((WINDOWS_KEY, "t"), 1, id="time-point"),
    pytest.param((WINDOWS_KEY, "t"), False, id="time-point-not-an-integer"),
    pytest.param((WINDOWS_KEY, "z"), 0, id="plane"),
    pytest.param((WINDOWS_KEY, "k"), 8, id="reduction"),
    pytest.param((WINDOWS_KEY, "dtype"), "uint8", id="dtype"),
    pytest.param((WINDOWS_KEY, "rgb"), True, id="rgb"),
    pytest.param((WINDOWS_KEY, "windows", 1), _DROP, id="channel-count"),
    pytest.param((WINDOWS_KEY, "windows", 0, "end"), float("inf"), id="not-finite"),
    pytest.param((WINDOWS_KEY, "windows", 0, "start"), "1.5", id="not-a-number"),
    pytest.param((WINDOWS_KEY, "windows", 1, "max"), _DROP, id="missing-field"),
    pytest.param((WINDOWS_KEY,), [], id="not-a-record"),
])
def test_a_windows_record_counts_only_for_its_generation_source_plane_and_method(keys, damage):
    source = SimpleNamespace(
        z_home=1, dtype=np.dtype(np.uint16), rgb=False, frame_shape=(2, 12, 16)
    )
    manifest = {
        "generation": "g1",
        "source": {"name": "plate.nd2", "size": 10, "mtime_ns": 5, "quick_sha256": "ab" * 32},
    }
    windows = [
        {"start": 101.5, "end": 3900.25, "min": 0.0, "max": 65535.0},
        {"start": 2.0, "end": 3.0, "min": 0.0, "max": 65535.0},
    ]
    manifest[WINDOWS_KEY] = _windows_record(manifest, source, windows)
    manifest = json.loads(json.dumps(manifest))
    assert _stored_windows(manifest, source) == windows
    target = manifest
    for key in keys[:-1]:
        target = target[key]
    if damage is _DROP:
        del target[keys[-1]]
    else:
        target[keys[-1]] = damage
    assert _stored_windows(manifest, source) is None


def test_measured_windows_are_pinned_to_the_recorded_method():
    """Recorded windows outlive releases. A change to how they are measured
    must also change WINDOWS_METHOD, or older records stay in use."""
    rng = np.random.default_rng(7)
    sites = [rng.integers(200, 4000, (2, 12, 16)).astype(np.uint16) for _ in range(3)]
    source = PlateSource.__new__(PlateSource)
    source.P, source.z_home, source.dtype, source.rgb = 3, 0, np.dtype(np.uint16), False
    source.reduced = lambda t, p, z, k: sites[p] if (t, z, k) == (0, 0, 4) else None
    assert PlateSource._measure_windows(source) == [
        {"start": pytest.approx(211.45), "end": pytest.approx(3964.125),
         "min": 0.0, "max": 65535.0},
        {"start": pytest.approx(216.025), "end": pytest.approx(3982.625),
         "min": 0.0, "max": 65535.0},
    ], "the measurement changed: bump plate.WINDOWS_METHOD and update these values"
