import hashlib
import json
import shutil

import numpy as np
import pytest
from test_plate import H, P, T, W, Z, value
from test_plate import plate_nd2 as _plate_nd2_fixture

from nd2wsi.cache import quick_fingerprint, read_manifest
from nd2wsi.plate import THUMB_K, PlateSource, PlateStore, plate_container
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
