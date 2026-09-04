from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import time
from pathlib import Path

import pytest

from nd2wsi.session_lock import SessionFileLock


def _child_attempt(path: str, result: object) -> None:
    lock = SessionFileLock(path)
    try:
        lock.acquire(timeout=0.2, poll=0.01)
    except TimeoutError:
        result.put(("blocked", None))
    except BaseException as exc:  # pragma: no cover - reported to the parent
        result.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        result.put(("acquired", os.getpid()))
    finally:
        lock.release()


def _spawn_attempt(path: Path) -> tuple[str, object]:
    ctx = mp.get_context("spawn")
    result = ctx.Queue()
    child = ctx.Process(target=_child_attempt, args=(str(path), result))
    started = False
    try:
        child.start()
        started = True
        child.join(timeout=10)
        if child.is_alive():
            pytest.fail("spawned lock contender did not exit")
        assert child.exitcode == 0
        try:
            return result.get(timeout=2)
        except queue.Empty:
            pytest.fail("spawned lock contender returned no result")
    finally:
        if started and child.is_alive():
            child.terminate()
            child.join(timeout=5)
        if started and child.is_alive():  # pragma: no cover - last-resort cleanup
            child.kill()
            child.join(timeout=5)
        result.close()
        result.join_thread()


def test_same_process_contender_is_blocked(tmp_path: Path):
    path = tmp_path / "plate.writer-session"
    first = SessionFileLock(path)
    second = SessionFileLock(path)
    first.acquire()
    try:
        with pytest.raises(TimeoutError):
            second.acquire(timeout=0.05, poll=0.005)
        assert first.acquired
        assert not second.acquired
    finally:
        second.release()
        first.release()


def test_spawned_process_contends_and_is_cleaned_up(tmp_path: Path):
    path = tmp_path / "plate.writer-session"
    first = SessionFileLock(path)
    first.acquire()
    try:
        assert _spawn_attempt(path) == ("blocked", None)
    finally:
        first.release()

    status, child_pid = _spawn_attempt(path)
    assert status == "acquired"
    assert isinstance(child_pid, int) and child_pid != os.getpid()


def test_five_hour_old_metadata_and_mtime_do_not_expire_live_lock(tmp_path: Path):
    path = tmp_path / "plate.writer-session"
    first = SessionFileLock(path)
    first.acquire()
    try:
        metadata = json.loads(path.read_text())
        metadata["acquired_at"] = time.time() - 5 * 3600
        path.write_text(json.dumps(metadata))
        old = time.time() - 5 * 3600
        os.utime(path, (old, old))

        assert _spawn_attempt(path) == ("blocked", None)
    finally:
        first.release()


def test_release_keeps_coordination_path_and_allows_reacquire(tmp_path: Path):
    path = tmp_path / "plate.writer-session"
    first = SessionFileLock(path)
    first.acquire()
    inode = path.stat().st_ino
    first.release()

    assert path.is_file()
    assert path.stat().st_ino == inode

    second = SessionFileLock(path)
    second.acquire(timeout=0.2)
    try:
        assert second.acquired
    finally:
        second.release()

    assert path.is_file()
    assert path.stat().st_ino == inode


def test_metadata_write_failure_does_not_drop_kernel_lock(tmp_path: Path, monkeypatch):
    path = tmp_path / "plate.writer-session"

    def fail_fsync(_fd: int) -> None:
        raise OSError("diagnostic metadata unavailable")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    first = SessionFileLock(path)
    first.acquire()
    try:
        assert first.acquired
        assert _spawn_attempt(path) == ("blocked", None)
    finally:
        first.release()


def test_symlink_lock_path_cannot_modify_its_target(tmp_path: Path):
    target = tmp_path / "user-data.txt"
    target.write_bytes(b"precious")
    path = tmp_path / "plate.writer-session"
    path.symlink_to(target)

    lock = SessionFileLock(path)
    with pytest.raises(OSError):
        lock.acquire()

    assert target.read_bytes() == b"precious"
    assert path.is_symlink()
    assert not lock.acquired


def test_hardlinked_lock_path_cannot_modify_its_target(tmp_path: Path):
    target = tmp_path / "user-data.txt"
    target.write_bytes(b"precious")
    path = tmp_path / "plate.writer-session"
    os.link(target, path)

    lock = SessionFileLock(path)
    with pytest.raises(OSError):
        lock.acquire()

    assert target.read_bytes() == b"precious"
    assert path.read_bytes() == b"precious"
    assert not lock.acquired


def test_path_replacement_during_acquire_retries_the_current_inode(
    tmp_path: Path, monkeypatch
):
    from nd2wsi import session_lock as lock_mod

    path = tmp_path / "plate.writer-session"
    lock = SessionFileLock(path)
    real_flock = lock_mod.fcntl.flock
    swapped = {"done": False}

    def swap_after_first_lock(fd, operation):
        result = real_flock(fd, operation)
        if (
            not swapped["done"]
            and operation & lock_mod.fcntl.LOCK_EX
            and operation & lock_mod.fcntl.LOCK_NB
        ):
            swapped["done"] = True
            replacement = path.with_name(path.name + ".replacement")
            replacement.write_text("replacement inode")
            replacement.replace(path)
        return result

    monkeypatch.setattr(lock_mod.fcntl, "flock", swap_after_first_lock)
    lock.acquire(timeout=0.2, poll=0.005)
    try:
        held = os.fstat(lock._fd)
        current = path.stat(follow_symlinks=False)
        assert swapped["done"]
        assert (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)
    finally:
        lock.release()
