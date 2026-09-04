"""Cache identity, atomicity and recovery — the 0.8 acceptance criteria.

Before 0.8 a cache was its slide's stem: any T/P/Z landed in one path, a
changed source was served stale, and a crashed conversion wedged the
slide permanently (the store existed, so nothing rebuilt it, and it
would not open). These tests pin the container contract that replaced
all of that.
"""

import json
import os
import threading
import time

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")

from nd2wsi import convert as convert_mod  # noqa: E402
from nd2wsi.cache import (  # noqa: E402
    CacheLock,
    cache_container,
    container_store,
    read_manifest,
)
from nd2wsi.convert import ensure_cache, open_store  # noqa: E402
from nd2wsi.reader import PlaneSelection  # noqa: E402


@pytest.fixture()
def slide(tmp_path):
    """SVS stands in for any slide; ensure_cache treats them alike."""
    rng = np.random.default_rng(3)
    img = rng.integers(0, 255, (900, 1200, 3), dtype=np.uint8)
    p = tmp_path / "s.svs"
    tifffile.imwrite(p, img, tile=(240, 240), photometric="rgb",
                     compression="jpeg2000", compressionargs={"reversible": True},
                     description="Aperio Image|MPP = 0.5|AppMag = 20")
    return p


def test_cache_identity_includes_source_suffix(tmp_path):
    nd2 = cache_container(tmp_path / "sample.nd2")
    svs = cache_container(tmp_path / "sample.svs")
    assert nd2 != svs
    assert "sample.nd2" in nd2.name
    assert "sample.svs" in svs.name


def test_cache_lands_in_a_manifested_container(slide):
    store = ensure_cache(slide)
    container = cache_container(slide)
    assert store == container_store(container)
    m = read_manifest(container)
    assert m["complete"] and m["kind"] == "full"
    assert m["source"]["name"] == "s.svs"
    assert m["selection"] == {"t": 0, "p": 0, "z": "mid", "z_resolved": None} or \
        m["selection"]["t"] == 0  # z_resolved differs by source kind
    open_store(store)  # opens clean


def test_selections_get_their_own_containers(slide):
    a = cache_container(slide, PlaneSelection())
    b = cache_container(slide, PlaneSelection(z=3))
    c = cache_container(slide, PlaneSelection(t=1, p=2, z="max"))
    assert len({a, b, c}) == 3
    assert a.name.endswith("--t0-p0-zmid.nd2wsi-cache")
    assert c.name.endswith("--t1-p2-zmax.nd2wsi-cache")


def test_changed_source_is_never_served_stale(slide):
    first = ensure_cache(slide)
    marker = container_store(cache_container(slide)) / ".zattrs"
    stamp0 = marker.stat().st_mtime_ns

    # rewrite the slide with different pixels (and a bumped mtime)
    rng = np.random.default_rng(9)
    img = rng.integers(0, 255, (900, 1200, 3), dtype=np.uint8)
    tifffile.imwrite(slide, img, tile=(240, 240), photometric="rgb",
                     compression="jpeg2000", compressionargs={"reversible": True},
                     description="Aperio Image|MPP = 0.5|AppMag = 20")
    os.utime(slide, ns=(stamp0 + 10**9, stamp0 + 10**9))

    second = ensure_cache(slide)
    assert second == first  # same path…
    root, _ = open_store(second)
    got = np.moveaxis(np.asarray(root["0"][:, :4, :4]), 0, -1)
    assert (got == img[:4, :4]).all()  # …fresh pixels
    # the stale container went to quarantine, not into the void
    quarantined = list(cache_container(slide).parent.glob("*.corrupt-*"))
    assert quarantined


def test_interrupted_build_never_exposes_a_final_path(slide, monkeypatch):
    calls = {"n": 0}
    real = convert_mod._downsample_into

    def bomb(*a, **k):
        calls["n"] += 1
        raise RuntimeError("power cut")

    monkeypatch.setattr(convert_mod, "_downsample_into", bomb)
    with pytest.raises(RuntimeError, match="power cut"):
        ensure_cache(slide)
    container = cache_container(slide)
    assert not container.exists()
    assert not list(container.parent.glob("*.building-*"))
    assert calls["n"] == 1

    # and the slide recovers on the next open, no manual surgery
    monkeypatch.setattr(convert_mod, "_downsample_into", real)
    store = ensure_cache(slide)
    open_store(store)


def test_wedged_legacy_store_is_quarantined_and_rebuilt(slide, tmp_path):
    """The pre-0.8 failure mode: a pyramids/ dir that exists but does not
    open. It used to block the slide forever."""
    wedge = tmp_path / "pyramids" / "s.ome.zarr"
    wedge.mkdir(parents=True)
    (wedge / "0").mkdir()  # levels but no .zattrs: a crashed conversion

    store = ensure_cache(slide)
    open_store(store)
    assert not wedge.exists()
    assert list((tmp_path / "pyramids").glob("s.ome.zarr.corrupt-*"))


def test_valid_legacy_store_is_honored_untouched(slide, tmp_path):
    legacy = tmp_path / "pyramids" / "s.ome.zarr"
    convert_mod.convert(slide, legacy, progress=False)
    stamp = (legacy / ".zattrs").stat().st_mtime_ns

    assert ensure_cache(slide) == legacy
    assert (legacy / ".zattrs").stat().st_mtime_ns == stamp
    assert not cache_container(slide).exists()  # no shadow container


def test_build_lock_serializes_concurrent_builders(slide):
    container = cache_container(slide)
    lock = CacheLock(container)
    lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            CacheLock(container).acquire(timeout=0.4, poll=0.1)
    finally:
        lock.release()
    # a lock whose holder is dead is reclaimed
    container.parent.mkdir(parents=True, exist_ok=True)
    stale = CacheLock(container)
    stale.path.write_text(json.dumps({"pid": 99999999, "time": 0}))
    reclaimed = CacheLock(container)
    reclaimed.acquire(timeout=2)
    reclaimed.release()


def test_build_lock_with_live_local_pid_never_ages_out(slide):
    container = cache_container(slide)
    held = CacheLock(container)
    held.acquire()
    try:
        info = json.loads(held.path.read_text())
        info["time"] = time.time() - 5 * 3600
        held.path.write_text(json.dumps(info))

        with pytest.raises(TimeoutError):
            CacheLock(container).acquire(timeout=0.1, poll=0.01)
    finally:
        held.release()


def test_lock_refresh_renews_only_its_own_inode(slide, monkeypatch):
    container = cache_container(slide)
    held = CacheLock(container)
    held.acquire()

    monkeypatch.setattr(time, "time", lambda: 1234.5)
    assert held.refresh()
    refreshed = json.loads(held.path.read_text())
    assert refreshed["time"] == 1234.5

    replacement = {
        "pid": os.getpid(),
        "time": 5678.0,
        "gen": "replacement",
        "machine": "replacement-machine",
    }

    def replace_then_report_time():
        successor = held.path.with_name(held.path.name + ".successor")
        successor.write_text(json.dumps(replacement))
        successor.replace(held.path)
        return 9999.0

    monkeypatch.setattr(time, "time", replace_then_report_time)
    assert not held.refresh()
    held.release()

    assert json.loads(held.path.read_text()) == replacement


def test_claim_timestamp_is_created_after_waiting_for_arbiter(slide, monkeypatch):
    container = cache_container(slide)
    held = CacheLock(container)
    now = {"value": 10.0}
    real_acquire = held._arbiter.acquire

    monkeypatch.setattr(time, "time", lambda: now["value"])

    def acquire_after_wait(*args, **kwargs):
        now["value"] = 90.0
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(held._arbiter, "acquire", acquire_after_wait)
    held.acquire()
    try:
        assert json.loads(held.path.read_text())["time"] == 90.0
    finally:
        held.release()


def test_failed_claim_write_does_not_leave_a_live_orphan(
    slide, monkeypatch
):
    container = cache_container(slide)
    failed = CacheLock(container)
    real_write = CacheLock._write_fd
    fail_once = {"value": True}

    def partial_write_then_fail(fd, payload):
        info = json.loads(payload)
        if fail_once["value"] and not info.get("released"):
            fail_once["value"] = False
            os.pwrite(fd, payload[:12], 0)
            os.ftruncate(fd, 12)
            raise OSError("injected incomplete claim")
        return real_write(fd, payload)

    monkeypatch.setattr(
        CacheLock, "_write_fd", staticmethod(partial_write_then_fail)
    )
    with pytest.raises(OSError, match="injected incomplete claim"):
        failed.acquire(timeout=0.1)

    info = json.loads(failed.path.read_text())
    assert info["released"] is True
    assert info["pid"] is None

    successor = CacheLock(container)
    successor.acquire(timeout=0.1, poll=0.01)
    successor.release()


def test_release_closes_without_writing_when_arbiter_is_unavailable(
    slide, monkeypatch
):
    container = cache_container(slide)
    held = CacheLock(container)
    held.acquire()
    owned_fd = held._fd
    original = held.path.read_bytes()
    writes = []

    def timeout(*args, **kwargs):
        raise TimeoutError("injected wedged arbiter")

    real_write = CacheLock._write_fd

    def record_write(fd, payload):
        writes.append((fd, payload))
        return real_write(fd, payload)

    monkeypatch.setattr(CacheLock, "_write_fd", staticmethod(record_write))
    monkeypatch.setattr(held._arbiter, "acquire", timeout)
    held.release()

    assert writes == []
    assert held.path.read_bytes() == original
    assert held._fd is None and held._gen is None
    with pytest.raises(OSError):
        os.fstat(owned_fd)
    info = json.loads(held.path.read_text())
    assert info.get("released") is not True
    assert info["pid"] == os.getpid()

    # Safety wins over liveness in this exceptional path: no unsynchronized
    # writer may corrupt a successor, so the compatibility lease remains live
    # until this process exits (simulated here with a dead owner).
    successor = CacheLock(container)
    with pytest.raises(TimeoutError):
        successor.acquire(timeout=0.05, poll=0.01)
    info["pid"] = 99999999
    held.path.write_text(json.dumps(info))
    successor.acquire(timeout=0.1, poll=0.01)
    successor.release()


def test_release_write_failure_is_invalidated_under_the_arbiter(
    slide, monkeypatch
):
    container = cache_container(slide)
    held = CacheLock(container)
    held.acquire()
    owned_fd = held._fd
    real_write = CacheLock._write_fd

    def fail_tombstone(fd, payload):
        if json.loads(payload).get("released"):
            raise OSError("injected tombstone failure")
        return real_write(fd, payload)

    monkeypatch.setattr(CacheLock, "_write_fd", staticmethod(fail_tombstone))
    held.release()

    assert held._fd is None and held._gen is None
    with pytest.raises(OSError):
        os.fstat(owned_fd)
    assert held.path.read_bytes() == b""

    successor = CacheLock(container)
    successor.acquire(timeout=0.1, poll=0.01)
    successor.release()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_lock_never_overwrites_a_linked_file(slide, tmp_path, link_kind):
    container = cache_container(slide)
    lock = CacheLock(container)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    unrelated = tmp_path / f"unrelated-{link_kind}.json"
    original = '{"do_not_touch": true}'
    unrelated.write_text(original)
    if link_kind == "symlink":
        lock.path.symlink_to(unrelated)
    else:
        os.link(unrelated, lock.path)

    with pytest.raises(TimeoutError):
        lock.acquire(timeout=0.05, poll=0.01)
    assert unrelated.read_text() == original


def test_explicit_convert_is_never_quarantined_mid_build(slide, tmp_path):
    """`nd2wsi convert` stages atomically now, so a concurrent default
    open finds nothing at the final path while the build runs."""
    from nd2wsi.convert import _legacy_store

    staging = tmp_path / "pyramids" / ".s.ome.zarr.building-12345"
    (staging / "0").mkdir(parents=True)  # someone else's build in flight
    assert _legacy_store(slide.resolve()) is None
    assert staging.exists()  # untouched


def test_legacy_store_for_another_timepoint_is_not_the_default_view(slide, tmp_path):
    from nd2wsi.convert import convert, ensure_cache, open_store

    legacy = tmp_path / "pyramids" / "s.ome.zarr"
    convert(slide, legacy, progress=False)
    # forge a recorded non-default selection, as `nd2wsi convert --t 3` writes
    import zarr

    g = zarr.open_group(str(legacy), mode="r+")
    meta = dict(g.attrs["nd2wsi"])
    meta["selection"] = {"t": 3, "p": 0, "z": 0}
    g.attrs["nd2wsi"] = meta

    store = ensure_cache(slide)
    assert store != legacy  # a fresh default cache, not the t=3 pyramid
    assert legacy.exists()  # and the explicit store is untouched
    _, attrs = open_store(store)
    assert not attrs["nd2wsi"]["selection"].get("t")


def test_lock_release_never_removes_a_reclaimed_lock(slide):
    container = cache_container(slide)
    a = CacheLock(container)
    a.acquire()
    # someone reclaims a's lock (judged stale) and takes their own
    b = CacheLock(container)
    a.path.unlink()
    b.acquire(timeout=2)
    a.release()  # must not unlink b's live lock
    assert b.path.exists()
    with pytest.raises(TimeoutError):
        CacheLock(container).acquire(timeout=0.3, poll=0.1)
    b.release()


def test_stale_reclaim_is_serialized_before_a_successor_can_be_claimed(
    slide, monkeypatch
):
    """A delayed stale observer must not take the first claimant's lock."""
    container = cache_container(slide)
    stale = CacheLock(container)
    stale.path.parent.mkdir(parents=True, exist_ok=True)
    stale.path.write_text(json.dumps({"pid": 99999999, "time": 0}))

    entered_write = threading.Event()
    allow_write = threading.Event()
    first_acquired = threading.Event()
    second_acquired = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    errors = []
    gate = threading.Lock()
    paused = {"value": False}
    real_write = CacheLock._write_fd

    def gated_write(fd, payload):
        info = json.loads(payload)
        with gate:
            should_pause = (
                info.get("pid") == os.getpid()
                and not info.get("released")
                and not paused["value"]
            )
            if should_pause:
                paused["value"] = True
        if should_pause:
            entered_write.set()
            if not allow_write.wait(timeout=5):
                raise TimeoutError("test did not release the stale claimant")
        return real_write(fd, payload)

    monkeypatch.setattr(CacheLock, "_write_fd", staticmethod(gated_write))
    first = CacheLock(container)
    second = CacheLock(container)

    def hold(lock, acquired, release):
        try:
            lock.acquire(timeout=5, poll=0.01)
            acquired.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release acquired cache lock")
        except BaseException as exc:  # surfaced in the parent test thread
            errors.append(exc)
        finally:
            lock.release()

    a = threading.Thread(
        target=hold, args=(first, first_acquired, release_first), daemon=True
    )
    b = threading.Thread(
        target=hold, args=(second, second_acquired, release_second), daemon=True
    )
    try:
        a.start()
        assert entered_write.wait(timeout=5)
        b.start()
        assert not second_acquired.wait(timeout=0.1)
        allow_write.set()
        assert first_acquired.wait(timeout=5)
        assert not second_acquired.wait(timeout=0.1)
        release_first.set()
        assert second_acquired.wait(timeout=5)
    finally:
        allow_write.set()
        release_first.set()
        release_second.set()
        a.join(timeout=5)
        b.join(timeout=5)

    assert not a.is_alive() and not b.is_alive()
    assert not errors


def test_release_writes_only_its_fd_when_successor_arrives(slide, monkeypatch):
    """Replacement at the old read/unlink race point must remain untouched."""
    container = cache_container(slide)
    held = CacheLock(container)
    held.acquire()
    replacement = {
        "pid": os.getpid(),
        "time": 6789.0,
        "gen": "legacy-successor",
        "machine": "legacy-machine",
    }
    real_write = CacheLock._write_fd

    def replace_during_release(fd, payload):
        info = json.loads(payload)
        if info.get("released"):
            successor = held.path.with_name(held.path.name + ".successor")
            successor.write_text(json.dumps(replacement))
            successor.replace(held.path)
        return real_write(fd, payload)

    monkeypatch.setattr(
        CacheLock, "_write_fd", staticmethod(replace_during_release)
    )
    held.release()

    assert json.loads(held.path.read_text()) == replacement


def test_sweep_clears_stranded_backups_and_stagings(tmp_path):
    from nd2wsi.cache import sweep_stale_builds

    caches = tmp_path / "caches"
    dead1 = caches / "a--t0-p0-zmid.nd2wsi-cache.building-99999999"
    dead2 = caches / "a--t0-p0-zmid.nd2wsi-cache.replaced-deadbeef"
    weird = caches / "b[1]--t0-p0-zmid.nd2wsi-cache.building-99999999"
    for d in (dead1, dead2, weird):
        d.mkdir(parents=True)
    assert sweep_stale_builds(caches) == 3
    assert not dead1.exists() and not dead2.exists() and not weird.exists()


def test_same_stem_sources_get_distinct_new_cache_names(tmp_path):
    from nd2wsi.cache import legacy_cache_container

    nd2_path = tmp_path / "slide.nd2"
    svs_path = tmp_path / "slide.svs"
    assert cache_container(nd2_path) != cache_container(svs_path)
    assert cache_container(nd2_path).name.startswith("slide.nd2--")
    assert cache_container(svs_path).name.startswith("slide.svs--")
    # The old stem-only layout did collide; it remains a read-only migration path.
    assert legacy_cache_container(nd2_path) == legacy_cache_container(svs_path)


def test_legacy_portable_store_is_not_reused_for_same_stem_other_format(tmp_path):
    import zarr

    from nd2wsi.convert import default_store_path, existing_cache_store

    nd2_path = tmp_path / "slide.nd2"
    svs_path = tmp_path / "slide.svs"
    nd2_path.write_bytes(b"nd2")
    svs_path.write_bytes(b"svs")

    legacy = tmp_path / "pyramids" / "slide.ome.zarr"
    root = zarr.open_group(str(legacy), mode="w", zarr_format=2)
    root.attrs.update(
        {
            "multiscales": [],
            "nd2wsi": {"source": "slide.nd2", "selection": {}},
        }
    )

    assert default_store_path(nd2_path) == legacy
    assert default_store_path(svs_path) == tmp_path / "pyramids" / "slide.svs.ome.zarr"
    assert existing_cache_store(svs_path) is None


def test_cache_from_a_newer_app_is_left_alone_and_names_the_update(slide):
    """An older app must not destroy a cache it merely cannot read."""
    container = cache_container(slide)
    container.mkdir(parents=True)
    (container / "manifest.json").write_text(json.dumps({
        "format": "nd2wsi-cache/99", "complete": True, "kind": "full",
        "source": {"name": "s.svs"},
    }))
    (container / "store.ome.zarr").mkdir()

    with pytest.raises(RuntimeError, match="newer nd2wsi-viewer"):
        ensure_cache(slide)
    assert (container / "manifest.json").exists()
    assert not list(container.parent.glob("*.corrupt-*"))


def test_an_unknown_older_format_is_still_quarantined_and_rebuilt(slide):
    container = cache_container(slide)
    container.mkdir(parents=True)
    (container / "manifest.json").write_text(json.dumps({
        "format": "nd2wsi-cache/1", "complete": True, "kind": "full",
        "source": {"name": "s.svs"},
    }))
    (container / "store.ome.zarr").mkdir()

    store = ensure_cache(slide)
    open_store(store)
    assert list(container.parent.glob("*.corrupt-*"))


def test_lock_from_another_machine_is_reclaimed_even_with_a_live_pid(slide):
    import time

    container = cache_container(slide)
    container.parent.mkdir(parents=True, exist_ok=True)
    foreign = CacheLock(container)
    # this process is alive, so a pid probe alone would wait for hours
    foreign.path.write_text(json.dumps({
        "pid": os.getpid(), "time": time.time() - 120, "gen": "x", "machine": "elsewhere",
    }))
    reclaimed = CacheLock(container)
    reclaimed.acquire(timeout=2)
    reclaimed.release()
    # a foreign lock younger than the grace period still holds
    foreign.path.write_text(json.dumps({
        "pid": os.getpid(), "time": time.time(), "gen": "y", "machine": "elsewhere",
    }))
    with pytest.raises(TimeoutError):
        CacheLock(container).acquire(timeout=0.4, poll=0.1)


def test_sweep_removes_builds_left_by_another_machine(tmp_path):
    import time

    from nd2wsi.cache import sweep_stale_builds

    caches = tmp_path / "caches"
    family = "a--t0-p0-zmid.nd2wsi-cache"
    staging = caches / f"{family}.building-{os.getpid()}"
    staging.mkdir(parents=True)
    CacheLock(caches / family).path.write_text(json.dumps({
        "pid": os.getpid(), "time": time.time(), "machine": "elsewhere",
    }))
    assert sweep_stale_builds(caches) == 1
    assert not staging.exists()
