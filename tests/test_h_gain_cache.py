"""Shared H gains on a corrected level 0: identical pixels, bounded, dropped on close.

Private synthetic ND2/cache fixtures only.
"""

import threading
from types import SimpleNamespace

import numpy as np
import pytest
from test_h_persistence import _original_state

from nd2wsi import h_persistence as hp
from nd2wsi.direct import _TILE_CACHE, _TileCache, open_nd2_backed
from nd2wsi.h_model import Model

TILE = 16


@pytest.fixture
def model(tmp_path):
    rng = np.random.default_rng(5)
    path = tmp_path / "adaptive-local.npz"
    np.savez_compressed(path, basis=np.array([[0.61, 0.04], [-0.02, 0.69]]), oldbasis=np.eye(2),
                        templates=rng.normal(0, 0.05, (2, 8, 8)), oldtemplates=rng.normal(0, 0.05, (2, 8, 8)),
                        delta=rng.normal(0, 0.02, (6, 7)), local=np.array([True, False]),
                        old=np.array([False, False]), channel_indices=np.array([1, 2]),
                        enabled=np.array([True, True]), amplitude_nodes=rng.uniform(0.5, 1.0, (2, 12, 12)),
                        envelope_cell_origin=np.array([0.0, 0.0]), model_schema=np.asarray(Model.ADAPTIVE_SCHEMA))
    result = Model(path)
    yield result
    result.data.close()


def _counting(model, monkeypatch):
    calls = []
    compute = model._gain

    def counted(c, shape, origin):
        calls.append((c, tuple(shape), tuple(origin)))
        return compute(c, shape, origin)

    monkeypatch.setattr(model, "_gain", counted)
    return calls


def _bits(values):
    return np.asarray(values, np.float32).view(np.uint32)


def test_tile_gains_are_reused_read_only_and_bit_identical(model, monkeypatch):
    fresh = {(c, y, x, h, w): model.gain(c, (h, w), (y, x)).copy()
             for c in (1, 2) for y, x, h, w in ((32, 48, 16, 16), (64, 80, 7, 11))}
    cache = _TileCache(budget=1 << 20)
    model.share_gains(cache, "test-tag", TILE)
    calls = _counting(model, monkeypatch)
    for (c, y, x, h, w), expected in fresh.items():
        first = model.gain(c, (h, w), (y, x))
        again = model.gain(c, (h, w), (y, x))
        assert again is first and not first.flags.writeable
        assert np.array_equal(_bits(first), _bits(expected))
    assert len(calls) == len(fresh)  # one computation per tile and channel
    raw = np.random.default_rng(1).integers(0, 8000, (16, 16), dtype=np.uint16)
    corrected = model.correct(1, raw, 32, 48)
    assert np.array_equal(_bits(corrected), _bits(np.asarray(raw, np.float32) / fresh[(1, 32, 48, 16, 16)]))
    assert corrected.flags.writeable  # the quotient is the caller's own array
    assert len(calls) == len(fresh)


@pytest.mark.parametrize("shape, origin", [((16, 16), (3, 48)), ((16, 16), (32, 40)),
                                           ((17, 16), (32, 48)), ((32, 32), (0, 0)), ((0, 16), (0, 0))])
def test_strips_and_unaligned_windows_bypass_the_cache(model, monkeypatch, shape, origin):
    expected = model.gain(1, shape, origin).copy()
    cache = _TileCache(budget=1 << 20)
    model.share_gains(cache, "test-tag", TILE)
    calls = _counting(model, monkeypatch)
    for _ in range(2):
        result = model.gain(1, shape, origin)
        assert np.array_equal(_bits(result), _bits(expected))
    assert len(calls) == 2 and cache.used == 0


def test_shared_gains_respect_the_cache_budget(model):
    cache = _TileCache(budget=3 * TILE * TILE * 4)
    model.share_gains(cache, "test-tag", TILE)
    for y in range(0, 6 * TILE, TILE):
        model.gain(1, (TILE, TILE), (y, 0))
    assert cache.used <= cache.budget and len(cache._d) == 3


def test_reopened_corrected_level0_shares_gains_until_close(tmp_path, model, monkeypatch):
    native = np.random.default_rng(3).integers(0, 8000, (4, 67, 73), dtype=np.uint16)
    original = _original_state(tmp_path, native, tile=TILE)
    prepared = hp.prepare_apply(original, model, {"selected": []}, threading.Event(), lambda _: None)
    prepared.publish()
    prepared.close()
    monkeypatch.setattr("nd2.ND2File", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("nd2wsi.reader.is_source_backable", lambda *_: (True, ""))
    monkeypatch.setattr("nd2wsi.reader.plane_view", lambda *_: (original.root["0"], None))
    reopened, attrs = open_nd2_backed(original.source_path, original.container_path)
    try:
        assert attrs["nd2wsi"]["tile"] == TILE
        loaded = reopened["0"].model
        cache, tag, tile = loaded._shared_gains
        assert cache is _TILE_CACHE and tile == TILE

        def entries():
            with _TILE_CACHE._lock:
                return [key for key in _TILE_CACHE._d if key[0] == tag]

        calls = _counting(loaded, monkeypatch)
        windows = [(16, 32, 32, 48), (64, 67, 64, 73)]  # an interior and an edge tile
        for _ in range(3):  # three contrast steps re-read the same tiles
            for y0, y1, x0, x1 in windows:
                tile_values = reopened["0"][:, y0:y1, x0:x1]
                expected = native[:, y0:y1, x0:x1].astype(np.float32)
                expected[1] = model.correct(1, native[1, y0:y1, x0:x1], y0, x0)
                expected[2] = model.correct(2, native[2, y0:y1, x0:x1], y0, x0)
                assert np.array_equal(_bits(tile_values), _bits(expected))
        assert len(calls) == 2 * len(windows)
        assert sorted(entries()) == sorted((tag, c, y0, x0, y1 - y0, x1 - x0)
                                           for c in (1, 2) for y0, y1, x0, x1 in windows)
        # A rendered strip is not kept.
        reopened["0"][:, 3:9, 0:73]
        assert len(entries()) == 2 * len(windows)
    finally:
        reopened.close(delay=0)
    assert not entries()
