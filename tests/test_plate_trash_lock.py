from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from nd2wsi.cache import CACHE_SUFFIX
from nd2wsi.server import SlideRegistry, ViewerState


def _hold_plate_writer(container: str, ready: Any, release: Any) -> None:
    from nd2wsi.plate import PlateWriterLock

    writer = PlateWriterLock(container)
    try:
        writer.acquire(timeout=2.0)
    except BaseException as exc:  # pragma: no cover - reported to the parent
        ready.put(("error", f"{type(exc).__name__}: {exc}"))
        return
    try:
        ready.put(("held", os.getpid()))
        release.wait(20)
    finally:
        writer.release()


class _ReadOnlyPlateStore:
    def __init__(self, container: Path, probe: Path):
        self.container = container
        self.probe = probe
        self.closed = False

    @property
    def writable(self) -> bool:
        return False

    def read_probe(self) -> bytes:
        if self.closed:
            raise ValueError("store is closed")
        return self.probe.read_bytes()

    def close(self, *, release_writer: bool = True) -> None:
        del release_writer
        self.closed = True


class _LocalWriterPlateStore(_ReadOnlyPlateStore):
    def __init__(self, container: Path, probe: Path, writer: Any):
        super().__init__(container, probe)
        self._writer = writer

    @property
    def writable(self) -> bool:
        return self._writer is not None and self._writer.acquired

    def close(self, *, release_writer: bool = True) -> Any:
        self.closed = True
        writer, self._writer = self._writer, None
        if writer is not None and release_writer:
            writer.release()
            return None
        return writer


class _PlateSource:
    def __init__(self, store: _ReadOnlyPlateStore):
        self.store = store

    def close_for_trash(self) -> Any:
        return self.store.close(release_writer=False)


class _LostWriterPlateSource(_PlateSource):
    def __init__(self, store: _LocalWriterPlateStore):
        super().__init__(store)
        self._closed = False
        self._teardown_complete = False

    def close_for_trash(self) -> None:
        writer = self.store.close(release_writer=False)
        if writer is not None:
            writer.release()
        self._closed = True
        self._teardown_complete = True
        return None


class _TimedOutBusy:
    """Lifecycle double for the narrow check-to-close race."""

    def __init__(self):
        self.closed = False
        self.reopened = False

    @property
    def active(self) -> int:
        return 0

    def close(self, timeout: float = 30.0) -> bool:
        del timeout
        self.closed = True
        return False

    def reopen(self) -> None:
        self.closed = False
        self.reopened = True


def test_cache_trash_refuses_another_process_plate_writer(tmp_path: Path):
    container = tmp_path / f"synthetic{CACHE_SUFFIX}"
    container.mkdir()
    probe = container / "probe.bin"
    probe.write_bytes(b"still-readable")
    source = tmp_path / "synthetic.nd2"
    source.write_bytes(b"source-is-not-cache")

    store = _ReadOnlyPlateStore(container, probe)
    state = ViewerState(
        {"0": object()},
        {},
        trash_path=container,
        source_path=source,
        plate=_PlateSource(store),
    )
    registry = SlideRegistry()
    sid = "synthetic"
    registry.slides[sid] = state

    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    release = ctx.Event()
    child = ctx.Process(
        target=_hold_plate_writer,
        args=(str(container), ready, release),
    )
    child.start()
    try:
        try:
            status, detail = ready.get(timeout=10)
        except queue.Empty:
            pytest.fail("spawned plate writer did not report readiness")
        assert status == "held", detail
        assert detail != os.getpid()

        with pytest.raises(ValueError, match="another viewer is writing"):
            registry.trash_cache(sid)

        assert registry.get(sid) is state
        assert not state.busy.closed
        assert not store.closed
        assert store.read_probe() == b"still-readable"
        assert container.is_dir()
        assert source.read_bytes() == b"source-is-not-cache"
    finally:
        release.set()
        child.join(timeout=10)
        if child.is_alive():  # pragma: no cover - last-resort cleanup
            child.terminate()
            child.join(timeout=5)
        ready.close()
        ready.join_thread()

    assert child.exitcode == 0
    assert registry.trash_cache(sid) >= len(b"still-readable")
    assert registry.get(sid) is None
    assert store.closed
    assert not container.exists()
    assert source.read_bytes() == b"source-is-not-cache"


def test_cache_trash_transfers_local_writer_through_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from nd2wsi.plate import PlateWriterLock

    container = tmp_path / f"local-writer{CACHE_SUFFIX}"
    container.mkdir()
    probe = container / "probe.bin"
    probe.write_bytes(b"owned-locally")

    local_writer = PlateWriterLock(container)
    local_writer.acquire()
    store = _LocalWriterPlateStore(container, probe, local_writer)
    state = ViewerState(
        {"0": object()},
        {},
        trash_path=container,
        plate=_PlateSource(store),
    )
    registry = SlideRegistry()
    sid = "local-writer"
    registry.slides[sid] = state

    real_rename = Path.rename
    held_during_rename = []

    def checked_rename(path: Path, target: Path) -> Path:
        if path == container:
            contender = PlateWriterLock(container)
            try:
                with pytest.raises(TimeoutError):
                    contender.acquire(timeout=0.0)
                held_during_rename.append(local_writer.acquired)
            finally:
                contender.release()
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", checked_rename)
    try:
        assert registry.trash_cache(sid) >= len(b"owned-locally")
    finally:
        local_writer.release()

    assert held_during_rename == [True]
    assert not local_writer.acquired
    assert store.closed
    assert not container.exists()

    successor = PlateWriterLock(container)
    successor.acquire(timeout=0.1)
    successor.release()


def test_plate_writer_start_failure_releases_both_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from nd2wsi import plate as plate_mod
    from nd2wsi.plate import PlateWriterLock

    container = tmp_path / f"start-failure{CACHE_SUFFIX}"
    real_start = threading.Thread.start

    def fail_heartbeat_start(thread):
        if thread.name == "plate-writer-lock":
            raise RuntimeError("injected heartbeat start failure")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_heartbeat_start)
    failed = PlateWriterLock(container)
    with pytest.raises(RuntimeError, match="heartbeat start failure"):
        failed.acquire(timeout=0.1)
    assert not failed.acquired

    monkeypatch.setattr(plate_mod.threading.Thread, "start", real_start)
    successor = PlateWriterLock(container)
    successor.acquire(timeout=0.1)
    successor.release()


def test_cache_trash_rescues_annotation_created_at_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    container = tmp_path / f"late-annotation{CACHE_SUFFIX}"
    container.mkdir()
    (container / "chunk").write_bytes(b"cache")
    state = ViewerState({"0": object()}, {}, trash_path=container)
    registry = SlideRegistry()
    sid = "late-annotation"
    registry.slides[sid] = state
    real_rename = Path.rename

    def add_annotation_after_rename(path: Path, target: Path) -> Path:
        result = real_rename(path, target)
        if path == container and ".trashing-" in target.name:
            (target / "annotations_late.json").write_text('{"items":["late"]}')
        return result

    monkeypatch.setattr(Path, "rename", add_annotation_after_rename)
    registry.trash_cache(sid)

    rescued = tmp_path / "nd2wsi" / "annotations" / "annotations_late.json"
    assert rescued.read_text() == '{"items":["late"]}'
    assert not container.exists()


def test_cache_trash_reports_unlink_failure_and_keeps_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    container = tmp_path / f"unlink-failure{CACHE_SUFFIX}"
    container.mkdir()
    stubborn = container / "stubborn.chunk"
    stubborn.write_bytes(b"preserve-on-failure")
    state = ViewerState({"0": object()}, {}, trash_path=container)
    registry = SlideRegistry()
    sid = "unlink-failure"
    registry.slides[sid] = state
    progress = []
    real_unlink = os.unlink

    def fail_stubborn(path, *args, **kwargs):
        if Path(path).name == stubborn.name:
            raise PermissionError("injected unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_stubborn)
    with pytest.raises(OSError, match="remaining data was kept"):
        registry.trash_cache(sid, on_progress=progress.append)

    leftovers = list(tmp_path.glob(f".{container.name}.trashing-*"))
    assert len(leftovers) == 1
    assert (leftovers[0] / stubborn.name).read_bytes() == b"preserve-on-failure"
    assert registry.get(sid) is None
    assert 1.0 not in progress


def test_cache_trash_unregisters_a_plate_closed_before_writer_transfer(
    tmp_path: Path,
):
    from nd2wsi.plate import PlateWriterLock

    container = tmp_path / f"lost-writer{CACHE_SUFFIX}"
    container.mkdir()
    probe = container / "probe.bin"
    probe.write_bytes(b"intact")
    writer = PlateWriterLock(container)
    writer.acquire()
    store = _LocalWriterPlateStore(container, probe, writer)
    source = _LostWriterPlateSource(store)
    state = ViewerState({"0": object()}, {}, trash_path=container, plate=source)
    registry = SlideRegistry()
    sid = "lost-writer"
    registry.slides[sid] = state

    with pytest.raises(RuntimeError, match="retain the plate cache writer"):
        registry.trash_cache(sid)

    assert source._teardown_complete and source._closed
    assert registry.get(sid) is None
    assert probe.read_bytes() == b"intact"
    successor = PlateWriterLock(container)
    successor.acquire(timeout=0.1)
    successor.release()


def test_cache_trash_unregisters_nonplate_after_close_failure(tmp_path: Path):
    container = tmp_path / "broken-close.ome.zarr"
    container.mkdir()
    (container / "probe.bin").write_bytes(b"intact")

    class CloseThenFail(dict):
        def __init__(self):
            super().__init__({"0": object()})
            self._closed = False

        def close(self, delay=0):
            del delay
            self._closed = True
            raise OSError("injected backend close failure")

    root = CloseThenFail()
    state = ViewerState(root, {}, trash_path=container)
    registry = SlideRegistry()
    sid = "broken-close"
    registry.slides[sid] = state

    with pytest.raises(OSError, match="backend close failure"):
        registry.trash_cache(sid)

    assert root._closed
    assert registry.get(sid) is None
    assert (container / "probe.bin").read_bytes() == b"intact"


def test_cache_trash_refuses_a_symlink_root_without_touching_its_target(
    tmp_path: Path,
):
    target = tmp_path / "unrelated-user-folder"
    target.mkdir()
    precious = target / "precious.txt"
    precious.write_bytes(b"never cache data")
    container = tmp_path / f"substituted{CACHE_SUFFIX}"
    container.symlink_to(target, target_is_directory=True)
    state = ViewerState({"0": object()}, {}, trash_path=container)
    registry = SlideRegistry()
    sid = "substituted"
    registry.slides[sid] = state

    with pytest.raises(ValueError, match="not a managed cache"):
        registry.trash_cache(sid)

    assert registry.get(sid) is state
    assert container.is_symlink()
    assert precious.read_bytes() == b"never cache data"


def test_cache_trash_detects_root_substitution_at_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    container = tmp_path / f"rename-substitution{CACHE_SUFFIX}"
    container.mkdir()
    (container / "cache.chunk").write_bytes(b"cache")
    unrelated = tmp_path / "unrelated-at-rename"
    unrelated.mkdir()
    precious = unrelated / "precious.txt"
    precious.write_bytes(b"never cache data")
    state = ViewerState({"0": object()}, {}, trash_path=container)
    registry = SlideRegistry()
    sid = "rename-substitution"
    registry.slides[sid] = state
    original_cache = container.with_name(container.name + ".original")
    real_rename = Path.rename

    def substitute_before_final_rename(path: Path, target: Path) -> Path:
        if path == container and ".trashing-" in target.name:
            real_rename(path, original_cache)
            path.symlink_to(unrelated, target_is_directory=True)
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", substitute_before_final_rename)
    with pytest.raises(OSError, match="changed before deletion"):
        registry.trash_cache(sid)

    assert registry.get(sid) is None
    assert precious.read_bytes() == b"never cache data"
    assert (original_cache / "cache.chunk").read_bytes() == b"cache"
    retained_links = list(tmp_path.glob(f".{container.name}.trashing-*"))
    assert len(retained_links) == 1 and retained_links[0].is_symlink()


def test_cache_trash_hides_live_path_before_current_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    container = tmp_path / f"reopen-race{CACHE_SUFFIX}"
    container.mkdir()
    (container / "cache.chunk").write_bytes(b"cache")
    state = ViewerState({"0": object()}, {}, trash_path=container)
    registry = SlideRegistry()
    sid = "reopen-race"
    registry.slides[sid] = state

    entered = threading.Event()
    finished = threading.Event()

    def current_reopen() -> None:
        entered.set()
        # This is the registry-commit portion shared by current open paths. It
        # must not observe a live pathname after trash has unregistered it.
        with registry._lock:
            if container.exists():
                registry.slides[sid] = ViewerState(
                    {"0": object()}, {}, trash_path=container
                )
        finished.set()

    contender = threading.Thread(target=current_reopen)
    real_rename = Path.rename

    def pause_at_final_rename(path: Path, target: Path) -> Path:
        if path == container and ".trashing-" in target.name:
            contender.start()
            assert entered.wait(timeout=5)
            assert not finished.wait(timeout=0.05)
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", pause_at_final_rename)
    registry.trash_cache(sid)
    contender.join(timeout=5)

    assert finished.is_set()
    assert registry.get(sid) is None
    assert not container.exists()


def test_guarded_delete_closes_root_when_parent_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from nd2wsi import server as server_mod

    container = tmp_path / f"parent-open-failure{CACHE_SUFFIX}"
    container.mkdir()
    probe = container / "cache.chunk"
    probe.write_bytes(b"preserved")
    root_fd = os.open(
        container,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    expected = os.fstat(root_fd)
    real_open = os.open

    def fail_parent_open(path, flags, *args, **kwargs):
        if Path(path) == container.parent:
            raise PermissionError("injected parent open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(server_mod.os, "open", fail_parent_open)
    with pytest.raises(OSError, match="cache deletion incomplete"):
        server_mod._delete_verified_cache_tree(
            container,
            expected,
            root_fd=root_fd,
        )

    with pytest.raises(OSError):
        os.fstat(root_fd)
    assert probe.read_bytes() == b"preserved"


def test_cache_trash_unlinks_nested_symlink_without_following_it(tmp_path: Path):
    container = tmp_path / f"nested-link{CACHE_SUFFIX}"
    container.mkdir()
    unrelated = tmp_path / "unrelated-nested"
    unrelated.mkdir()
    precious = unrelated / "precious.txt"
    precious.write_bytes(b"never cache data")
    (container / "external-link").symlink_to(unrelated, target_is_directory=True)
    state = ViewerState({"0": object()}, {}, trash_path=container)
    registry = SlideRegistry()
    sid = "nested-link"
    registry.slides[sid] = state

    registry.trash_cache(sid)

    assert registry.get(sid) is None
    assert not container.exists()
    assert precious.read_bytes() == b"never cache data"


def test_cache_trash_timeout_keeps_the_registered_state_usable(tmp_path: Path):
    container = tmp_path / f"busy{CACHE_SUFFIX}"
    container.mkdir()
    probe = container / "probe.bin"
    probe.write_bytes(b"still-here")
    state = ViewerState({"0": object()}, {}, trash_path=container)
    state.busy = _TimedOutBusy()
    registry = SlideRegistry()
    sid = "busy"
    registry.slides[sid] = state

    with pytest.raises(ValueError, match="did not finish in time"):
        registry.trash_cache(sid)

    assert registry.get(sid) is state
    assert state.busy.reopened
    assert not state.busy.closed
    assert probe.read_bytes() == b"still-here"


def test_cache_trash_rename_failure_closes_then_unregisters_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from nd2wsi.plate import PlateWriterLock

    container = tmp_path / f"rename-failure{CACHE_SUFFIX}"
    container.mkdir()
    probe = container / "probe.bin"
    probe.write_bytes(b"preserved")

    local_writer = PlateWriterLock(container)
    local_writer.acquire()
    store = _LocalWriterPlateStore(container, probe, local_writer)
    state = ViewerState(
        {"0": object()}, {}, trash_path=container, plate=_PlateSource(store)
    )
    registry = SlideRegistry()
    sid = "rename-failure"
    registry.slides[sid] = state

    real_rename = Path.rename

    def fail_final_rename(path: Path, target: Path) -> Path:
        if path == container and ".trashing-" in target.name:
            raise OSError("simulated rename failure")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_final_rename)
    with pytest.raises(OSError, match="simulated rename failure"):
        registry.trash_cache(sid)

    assert registry.get(sid) is None
    assert store.closed
    assert not local_writer.acquired
    assert probe.read_bytes() == b"preserved"


def test_cache_trash_aborts_before_close_when_annotation_rescue_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from nd2wsi import server as server_mod

    container = tmp_path / f"annotation-failure{CACHE_SUFFIX}"
    container.mkdir()
    annotation = container / "annotations_precious.json"
    annotation.write_text('{"items":[1]}')
    state = ViewerState({"0": object()}, {}, trash_path=container)
    registry = SlideRegistry()
    sid = "annotation-failure"
    registry.slides[sid] = state

    def fail_rescue(*_args, **_kwargs):
        raise OSError("safe destination is full")

    monkeypatch.setattr(server_mod, "rescue_annotations", fail_rescue)
    with pytest.raises(OSError, match="safe destination is full"):
        registry.trash_cache(sid)

    assert registry.get(sid) is state
    assert not state.busy.closed
    assert annotation.read_text() == '{"items":[1]}'
