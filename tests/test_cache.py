"""Cache identity, atomicity and recovery — the 0.8 acceptance criteria.

Before 0.8 a cache was its slide's stem: any T/P/Z landed in one path, a
changed source was served stale, and a crashed conversion wedged the
slide permanently (the store existed, so nothing rebuilt it, and it
would not open). These tests pin the container contract that replaced
all of that.
"""

import json
import os

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
    CacheLock(container).acquire(timeout=2)


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
