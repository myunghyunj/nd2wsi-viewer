"""Level-1 Shading Correction tiles prepared on threads: same bytes, same lifetimes.

Private synthetic ND2/cache fixtures only. These pin that the thread pool is an
implementation detail: staged pixels and progress match the serial build bit for
bit, reads still enter ``state.busy``, and cancellation or a failing tile never
leaves a worker running past ``prepare_apply``.
"""

import threading
import time

import numpy as np
import pytest
from test_h_persistence import _original_state

from nd2wsi import h_persistence as hp
from nd2wsi.cache import CacheLock
from nd2wsi.convert import open_store
from nd2wsi.h_model import Model

WORKERS = 4


@pytest.fixture
def original(tmp_path):
    # 65 x 75 at level 1 with 16-pixel tiles: 25 tiles per channel, 100 in all.
    native = np.random.default_rng(7).integers(0, 8000, (4, 131, 151), dtype=np.uint16)
    return _original_state(tmp_path, native)


@pytest.fixture
def model(tmp_path):
    rng = np.random.default_rng(11)
    path = tmp_path / "local-model.npz"
    np.savez_compressed(path, basis=np.array([[0.62, 0.05], [-0.03, 0.71]]), oldbasis=np.eye(2),
                        templates=rng.normal(0, 0.05, (2, 8, 8)), oldtemplates=rng.normal(0, 0.05, (2, 8, 8)),
                        delta=rng.normal(0, 0.02, (6, 7)), local=np.array([True, False]),
                        old=np.array([False, False]), channel_indices=np.array([1, 2]),
                        enabled=np.array([True, True]))
    result = Model(path)
    yield result
    result.data.close()


@pytest.fixture
def workers(monkeypatch):
    def use(count):
        monkeypatch.setattr(hp, "_APPLY_MAX_WORKERS", count)
        assert hp._apply_workers(16) == count
    return use


def _apply_threads():
    return [t for t in threading.enumerate() if t.name.startswith("nd2wsi-h-apply")]


def _prepare(original, model, event=None, progress=lambda _: None):
    return hp.prepare_apply(original, model, {"selected": []}, event or threading.Event(), progress)


def _staged(replacement):
    stored, _ = open_store(replacement.staging)
    try:
        return {key: np.asarray(stored[key][:]) for key in ("1", "2", "3")}
    finally:
        stored.close()


def _assert_released(original, before):
    assert not _apply_threads()
    assert original.busy.active == 0
    assert original.container_path.read_bytes() == before
    assert not list(original.container_path.parent.glob("*.building-h-*"))
    with CacheLock(original.container_path):
        pass


def test_parallel_level1_matches_serial_bytes_and_progress(original, model, workers, monkeypatch):
    runs = {}
    for count in (1, WORKERS):
        workers(count)
        names = set()
        correct = model.correct

        def recording(*args, correct=correct, names=names):
            names.add(threading.current_thread().name)
            return correct(*args)

        monkeypatch.setattr(model, "correct", recording)
        progress = []
        replacement = _prepare(original, model, progress=progress.append)
        try:
            runs[count] = (_staged(replacement), progress, names)
        finally:
            replacement.close()
        monkeypatch.undo()
    (serial, serial_progress, serial_names), (parallel, parallel_progress, parallel_names) = runs.values()
    assert serial_names == {threading.current_thread().name}
    assert len(parallel_names) > 1 and all(name.startswith("nd2wsi-h-apply") for name in parallel_names)
    for key in serial:
        assert parallel[key].dtype == serial[key].dtype == np.dtype("float32")
        assert np.array_equal(parallel[key].view(np.uint32), serial[key].view(np.uint32)), key
    # Level-1 progress is still reported tile by tile, in tile order.
    assert parallel_progress == serial_progress
    assert parallel_progress == sorted(parallel_progress) and parallel_progress[-1] == 1.0
    # And the tiles agree with correcting the whole native plane at once.
    whole = model.correct(1, original.root["0"][1], 0, 0)
    expected = hp._reduce2(whole[:130, :150], 65, 75)
    assert np.array_equal(parallel["1"][1].view(np.uint32), expected.view(np.uint32))
    for ci in (0, 3):
        np.testing.assert_array_equal(parallel["1"][ci], original.root["1"][ci].astype(np.float32))
    assert not _apply_threads()


def test_parallel_native_reads_overlap_inside_busy(original, model, workers):
    workers(WORKERS)
    native = original.root["0"]
    barrier = threading.Barrier(2, timeout=10)
    seen = []

    class Native:
        shape, dtype = native.shape, native.dtype

        def __getitem__(self, index):
            seen.append(original.busy.active)
            if len(seen) <= 2:
                barrier.wait()  # two reads in flight at once, or BrokenBarrierError
            return native[index]

    original.root = dict(original.root, **{"0": Native()})
    replacement = _prepare(original, model)
    replacement.close()
    assert len(seen) == 2 * 25  # every corrected level-1 tile read once
    assert min(seen) >= 1 and max(seen) >= 2
    assert original.busy.active == 0 and not _apply_threads()


def test_cancel_during_parallel_apply_joins_workers_before_releasing(original, model, workers, monkeypatch):
    workers(WORKERS)
    before = original.container_path.read_bytes()
    spans = []
    correct = model.correct
    event = threading.Event()

    def slow(*args):
        start = time.perf_counter()
        event.set()  # the user cancels while corrected tiles are running
        time.sleep(0.05)
        try:
            return correct(*args)
        finally:
            spans.append((start, time.perf_counter()))

    monkeypatch.setattr(model, "correct", slow)
    with pytest.raises(hp.PreparationCancelled):
        _prepare(original, model, event)
    returned = time.perf_counter()
    assert spans and all(end <= returned for _, end in spans)
    # Only tiles already past their checks finish; queued ones never start.
    assert len(spans) <= WORKERS
    _assert_released(original, before)


def test_failing_tile_propagates_after_running_tiles_finish(original, model, workers, monkeypatch):
    workers(WORKERS)
    before = original.container_path.read_bytes()
    spans = []
    calls = []
    lock = threading.Lock()
    correct = model.correct

    def failing(*args):
        with lock:
            number = len(calls)
            calls.append(args[0])
        start = time.perf_counter()
        try:
            time.sleep(0.03)
            if number == 2:
                raise ValueError("synthetic tile failure")
            return correct(*args)
        finally:
            spans.append((start, time.perf_counter()))

    monkeypatch.setattr(model, "correct", failing)
    with pytest.raises(ValueError, match="synthetic tile failure"):
        _prepare(original, model)
    returned = time.perf_counter()
    assert all(end <= returned for _, end in spans)
    assert len(calls) < 50
    _assert_released(original, before)


def test_nonfinite_parallel_tile_is_refused(original, model, workers, monkeypatch):
    workers(WORKERS)
    before = original.container_path.read_bytes()
    correct = model.correct

    def poisoned(c, raw, y, x):
        out = correct(c, raw, y, x)
        if (y, x) == (32, 64):
            out[0, 0] = np.nan
        return out

    monkeypatch.setattr(model, "correct", poisoned)
    with pytest.raises(ValueError, match="Nonfinite values in the corrected pyramid"):
        _prepare(original, model)
    _assert_released(original, before)


def test_closed_slide_stops_parallel_apply_cleanly(original, model, workers):
    workers(WORKERS)
    before = original.container_path.read_bytes()
    calls = []

    def close_after_first(_value):
        calls.append(_value)
        if len(calls) == 1:
            assert original.busy.close(timeout=10)  # the slide closes mid-apply

    with pytest.raises(ValueError, match="slide is closed"):
        _prepare(original, model, progress=close_after_first)
    _assert_released(original, before)


def test_workers_are_bounded_by_cores_and_task_memory(monkeypatch):
    monkeypatch.setattr(hp, "_APPLY_MAX_WORKERS", 8)
    assert hp._apply_workers(16) == 8
    assert hp._apply_workers(512) == 8  # 32 MiB per 1024^2 task window
    assert hp._apply_workers(1024) == 2  # 128 MiB per 2048^2 task window
    assert hp._apply_workers(4096) == 1
    monkeypatch.setattr(hp, "_APPLY_MAX_WORKERS", 1)
    assert hp._apply_workers(16) == 1
    assert 1 <= hp._APPLY_MAX_WORKERS
