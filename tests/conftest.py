"""Platform capabilities required by specific filesystem tests."""

import functools
import os
import weakref
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def forbid_quarantine_of_open_sqlite_store(request, monkeypatch):
    """Fail on POSIX if quarantine() renames a SQLite container while a SQLiteStore handle is open.

    On Windows, SQLite opens database files without FILE_SHARE_DELETE, so NTFS
    rejects MoveFileExW natively (raising PermissionError asserted by lock tests).
    `test_rebuild_evicts_the_registered_state` intentionally tests POSIX live eviction
    across an open SlideRegistry handle (with its own `os.name == 'nt'` branch).
    """
    if os.name == "nt" or request.node.name == "test_rebuild_evicts_the_registered_state":
        return
    import nd2wsi.cache as cache_mod
    import nd2wsi.plate as plate_mod
    from nd2wsi.storage import single_file as sf_mod

    live_stores: list[weakref.ref[sf_mod.SQLiteStore]] = []
    orig_init = sf_mod.SQLiteStore.__init__
    orig_quarantine = cache_mod.quarantine

    @functools.wraps(orig_init)
    def tracked_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        live_stores[:] = [ref for ref in live_stores if ref() is not None]
        live_stores.append(weakref.ref(self))

    @functools.wraps(orig_quarantine)
    def guarded_quarantine(path, *args, **kwargs):
        target = Path(os.path.abspath(os.fspath(path)))
        for ref in list(live_stores):
            store = ref()
            if store is None:
                continue
            if (
                not getattr(store, "_closed", True)
                and getattr(store, "_connection", None) is not None
                and getattr(store, "path", None) == target
            ):
                raise AssertionError(
                    f"quarantine({path}) called while SQLiteStore connection is still open (fails on Windows)"
                )
        return orig_quarantine(path, *args, **kwargs)

    monkeypatch.setattr(sf_mod.SQLiteStore, "__init__", tracked_init)
    monkeypatch.setattr(cache_mod, "quarantine", guarded_quarantine)
    monkeypatch.setattr(plate_mod, "quarantine", guarded_quarantine)


@pytest.fixture
def symlink_support(tmp_path):
    """Skip only symlink tests when Windows has not granted that privilege."""
    if os.name != "nt":
        return
    target = tmp_path / ".symlink-capability-target"
    link = tmp_path / ".symlink-capability-link"
    target.write_bytes(b"probe")
    try:
        try:
            link.symlink_to(target)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege is unavailable; junction and lock tests still run")
            raise
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
