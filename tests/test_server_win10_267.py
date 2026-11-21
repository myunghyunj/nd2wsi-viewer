"""Unit tests for Wave 1 M6 remediations (Windows 10 SMB/reparse + server modularization)."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nd2wsi import server, server_registry, server_series, ui_settings, windows_fs

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_CLOUD = 0x9000001A
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class _FakeWinStat:
    """Delegate os.stat_result fields while attaching Windows reparse attributes."""

    def __init__(
        self,
        real: os.stat_result,
        *,
        st_file_attributes: int = 0,
        st_reparse_tag: int = 0,
    ) -> None:
        self._real = real
        self.st_file_attributes = st_file_attributes
        self.st_reparse_tag = st_reparse_tag

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def _install_fake_kernel32_drive_types(
    monkeypatch: pytest.MonkeyPatch,
    drive_types: dict[str, int],
    calls: list[str] | None = None,
) -> None:
    """Install a fake ctypes.WinDLL('kernel32').GetDriveTypeW for Win32 path tests."""

    def _get_drive_type(root: str) -> int:
        if calls is not None:
            calls.append(root)
        return drive_types.get(root.upper(), 3)  # default DRIVE_FIXED (3)

    _get_drive_type.argtypes = None  # type: ignore[attr-defined]
    _get_drive_type.restype = None  # type: ignore[attr-defined]
    fake_kernel32 = SimpleNamespace(GetDriveTypeW=_get_drive_type)

    def _fake_win_dll(name: str, *, use_last_error: bool = False):
        assert name.lower() == "kernel32"
        return fake_kernel32

    monkeypatch.setattr(ctypes, "WinDLL", _fake_win_dll, raising=False)


# ---------------------------------------------------------------------------
# 1. Windows 10 SMB timeout & silent-pruning protection in ui_settings
# ---------------------------------------------------------------------------


def test_is_windows_network_path_handles_unc_mapped_drives_and_non_windows(
    monkeypatch: pytest.MonkeyPatch,
):
    drive_calls: list[str] = []
    _install_fake_kernel32_drive_types(
        monkeypatch,
        {"Z:\\": ui_settings.DRIVE_REMOTE, "Y:\\": ui_settings.DRIVE_REMOTE, "C:\\": 3},
        calls=drive_calls,
    )

    monkeypatch.setattr(sys, "platform", "win32")
    assert ui_settings._is_windows_network_path(r"\\lab-nas\slides\brain.nd2") is True
    assert ui_settings._is_windows_network_path("//lab-nas/slides/brain.nd2") is True
    assert ui_settings._is_windows_network_path(r"\\?\UNC\lab-nas\slides\brain.nd2") is True
    assert ui_settings._is_windows_network_path("//?/UNC/lab-nas/slides/brain.nd2") is True
    assert drive_calls == []  # UNC prefixes do not even need GetDriveTypeW

    assert ui_settings._is_windows_network_path(r"Z:\project\sample.nd2") is True
    assert ui_settings._is_windows_network_path("z:/project/sample.nd2") is True
    assert ui_settings._is_windows_network_path(r"\\?\Y:\deep\sample.nd2") is True
    assert ui_settings._is_windows_network_path(r"C:\Users\alice\sample.nd2") is False
    assert ui_settings._is_windows_network_path(r"\\?\C:\Users\alice\sample.nd2") is False
    assert ui_settings._is_windows_network_path(r"\\.\PhysicalDrive0") is False
    assert drive_calls == ["Z:\\", "Z:\\", "Y:\\", "C:\\", "C:\\"]

    # Non-Windows platforms preserve existing behavior and never invoke Win32 APIs.
    drive_calls.clear()
    for plat in ("linux", "darwin"):
        monkeypatch.setattr(sys, "platform", plat)
        assert ui_settings._is_windows_network_path(r"\\lab-nas\slides\brain.nd2") is False
        assert ui_settings._is_windows_network_path(r"Z:\project\sample.nd2") is False
    assert drive_calls == []


def test_list_recent_files_does_not_stat_or_prune_windows_network_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ND2WSI_UI_SETTINGS_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    _install_fake_kernel32_drive_types(
        monkeypatch,
        {"Z:\\": ui_settings.DRIVE_REMOTE, "C:\\": 3},
    )

    local_ok = tmp_path / "existing_local.nd2"
    local_ok.write_bytes(b"ok")
    local_missing = tmp_path / "deleted_local.nd2"

    unc_backslash = r"\\offline-nas\microscopy\brain_01.nd2"
    unc_slash = "//offline-nas/microscopy/brain_02.nd2"
    mapped_drive = r"Z:\offline-lab\organoid.czi"
    network_paths = {unc_backslash, unc_slash, mapped_drive}

    now = time.time()
    doc = {
        "format": ui_settings.FORMAT,
        "theme": "auto",
        "panels": {},
        "shellToolsExpanded": None,
        "recentFiles": [
            {"path": str(local_ok), "name": "existing_local.nd2", "openedAt": now},
            {"path": unc_backslash, "name": "brain_01.nd2", "openedAt": now - 1},
            {"path": unc_slash, "name": "brain_02.nd2", "openedAt": now - 2},
            {"path": mapped_drive, "name": "organoid.czi", "openedAt": now - 3},
            {"path": str(local_missing), "name": "deleted_local.nd2", "openedAt": now - 4},
        ],
    }
    settings_path = ui_settings.ui_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(doc), encoding="utf-8")

    orig_exists = Path.exists
    probed_paths: list[str] = []

    def _guarded_exists(self: Path) -> bool:
        s = str(self)
        probed_paths.append(s)
        if s in network_paths:
            raise AssertionError(f"blocking Path.exists() was called on Windows network path: {s}")
        return orig_exists(self)

    monkeypatch.setattr(Path, "exists", _guarded_exists)

    items = ui_settings.list_recent_files(limit=20)
    by_path = {item["path"]: item for item in items}

    assert str(local_ok) in by_path
    assert by_path[str(local_ok)]["available"] is True
    assert str(local_missing) not in by_path  # missing local path was pruned

    for net_path in network_paths:
        assert net_path in by_path
        assert by_path[net_path]["available"] is True
        assert by_path[net_path]["network"] is True
        assert net_path not in probed_paths

    # Verify ui-settings.json persisted the pruned local removal while keeping all network entries.
    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    persisted_paths = [entry["path"] for entry in persisted["recentFiles"]]
    assert persisted_paths == [str(local_ok), unc_backslash, unc_slash, mapped_drive]
    for entry in persisted["recentFiles"]:
        assert "network" not in entry and "available" not in entry

    # A second read keeps all network entries intact without touching Path.exists() for them.
    probed_paths.clear()
    items_again = ui_settings.list_recent_files(limit=20)
    assert [it["path"] for it in items_again] == persisted_paths
    assert not (set(probed_paths) & network_paths)


def test_list_recent_files_non_windows_keeps_existing_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ND2WSI_UI_SETTINGS_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")

    local_ok = tmp_path / "slide.nd2"
    local_ok.write_bytes(b"ok")
    missing_local = tmp_path / "gone.nd2"
    unmounted_vol = f"/Volumes/OfflineDrive_{os.getpid()}_win10test/slide.nd2"

    now = time.time()
    doc = {
        "format": ui_settings.FORMAT,
        "theme": "auto",
        "panels": {},
        "shellToolsExpanded": None,
        "recentFiles": [
            {"path": str(local_ok), "name": "slide.nd2", "openedAt": now},
            {"path": unmounted_vol, "name": "slide.nd2", "openedAt": now - 1},
            {"path": str(missing_local), "name": "gone.nd2", "openedAt": now - 2},
            {"path": r"\\server\share\not-win.nd2", "name": "not-win.nd2", "openedAt": now - 3},
        ],
    }
    settings_path = ui_settings.ui_settings_path()
    settings_path.write_text(json.dumps(doc), encoding="utf-8")

    items = ui_settings.list_recent_files(limit=20)
    assert [it["path"] for it in items] == [str(local_ok), unmounted_vol]
    assert items[0]["available"] is True and "network" not in items[0]
    assert items[1]["available"] is False


# ---------------------------------------------------------------------------
# 2. Server trash_cache surrogate reparse checks (OneDrive vs symlink/junction)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cache_kind", ["file", "dir"])
@pytest.mark.parametrize(
    ("reparse_tag", "should_accept"),
    [
        (IO_REPARSE_TAG_CLOUD, True),
        (IO_REPARSE_TAG_MOUNT_POINT, False),
        (IO_REPARSE_TAG_SYMLINK, False),
    ],
)
def test_trash_cache_accepts_onedrive_cloud_placeholder_and_rejects_surrogates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache_kind: str,
    reparse_tag: int,
    should_accept: bool,
):
    if cache_kind == "file":
        cache_target = tmp_path / "slide.nd2svs"
        cache_target.write_bytes(b"placeholder-cache-payload")
        monkeypatch.setattr(server, "_validate_file_cache_for_trash", lambda path, manifest: None)
    else:
        cache_target = tmp_path / "pyramid.ome.zarr"
        (cache_target / "0").mkdir(parents=True)
        (cache_target / "0" / "0.0.0").write_bytes(b"chunk-bytes")
        monkeypatch.setattr(server, "rescue_annotations", lambda folder, home: [])

    registry = server.SlideRegistry()
    registry.slides["sid00001"] = server.ViewerState(
        {"0": object()},
        {},
        trash_path=cache_target,
        container_path=cache_target,
        manifest={"generation": "g1"},
    )

    orig_stat = Path.stat

    def _patched_stat(self: Path, *, follow_symlinks: bool = True):
        real = orig_stat(self, follow_symlinks=follow_symlinks)
        if not follow_symlinks and (
            self == cache_target or self.name.startswith(f".{cache_target.name}.trashing-")
        ):
            return _FakeWinStat(
                real,
                st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
                st_reparse_tag=reparse_tag,
            )
        return real

    monkeypatch.setattr(Path, "stat", _patched_stat)

    if should_accept:
        freed = registry.trash_cache("sid00001")
        assert freed > 0
        assert not cache_target.exists()
        assert "sid00001" not in registry.slides
    else:
        with pytest.raises(ValueError, match="not a managed cache"):
            registry.trash_cache("sid00001")
        assert cache_target.exists()
        assert "sid00001" in registry.slides


@pytest.mark.parametrize(
    ("queried_tag", "should_accept"),
    [
        (IO_REPARSE_TAG_CLOUD, True),
        (IO_REPARSE_TAG_MOUNT_POINT, False),
        (IO_REPARSE_TAG_SYMLINK, False),
    ],
)
def test_trash_cache_fstat_passes_fd_to_reparse_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    queried_tag: int,
    should_accept: bool,
):
    cache_file = tmp_path / "cloud_slide.nd2svs"
    cache_file.write_bytes(b"single-file-cloud-payload")
    target_ino = cache_file.stat().st_ino
    monkeypatch.setattr(server, "_validate_file_cache_for_trash", lambda path, manifest: None)

    registry = server.SlideRegistry()
    registry.slides["sid00002"] = server.ViewerState(
        {"0": object()},
        {},
        trash_path=cache_file,
        container_path=cache_file,
        manifest={"generation": "g2"},
    )

    orig_stat = Path.stat
    orig_fstat = os.fstat

    def _patched_stat(self: Path, *, follow_symlinks: bool = True):
        real = orig_stat(self, follow_symlinks=follow_symlinks)
        if not follow_symlinks and (
            self == cache_file or self.name.startswith(f".{cache_file.name}.trashing-")
        ):
            return _FakeWinStat(
                real,
                st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
                st_reparse_tag=IO_REPARSE_TAG_CLOUD,
            )
        return real

    def _patched_fstat(fd: int):
        real = orig_fstat(fd)
        if real.st_ino == target_ino:
            # Windows CPython os.fstat() populates st_file_attributes but leaves st_reparse_tag=0.
            return _FakeWinStat(
                real,
                st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
                st_reparse_tag=0,
            )
        return real

    queried_fds: list[int] = []

    def _fake_query_fd(fd: int) -> tuple[int, int]:
        queried_fds.append(fd)
        return FILE_ATTRIBUTE_REPARSE_POINT, queried_tag

    monkeypatch.setattr(Path, "stat", _patched_stat)
    monkeypatch.setattr(os, "fstat", _patched_fstat)
    monkeypatch.setattr(windows_fs, "_query_fd_reparse_info", _fake_query_fd)

    if should_accept:
        freed = registry.trash_cache("sid00002")
        assert freed == len(b"single-file-cloud-payload")
        assert not cache_file.exists()
        assert queried_fds and all(isinstance(fd, int) for fd in queried_fds)
    else:
        with pytest.raises(ValueError, match="cache root changed during validation"):
            registry.trash_cache("sid00002")
        assert cache_file.exists()
        assert queried_fds and all(isinstance(fd, int) for fd in queried_fds)


# ---------------------------------------------------------------------------
# 3. Facade re-exports & ui_settings delegation on nd2wsi.server
# ---------------------------------------------------------------------------


def test_server_facade_reexports_and_removes_duplicate_ui_settings_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Registry & lifecycle symbols re-exported from nd2wsi.server_registry
    assert server.SlideRegistry is server_registry.SlideRegistry
    assert server.ViewerState is server_registry.ViewerState
    assert server.store_generation is server_registry.store_generation
    assert server._sidecar_items is server_registry._sidecar_items
    assert server._auto_record_allowed is server_registry._auto_record_allowed
    assert server._is_isolated_settings_env is server_registry._is_isolated_settings_env

    # Series & per-series view/tile helpers re-exported from nd2wsi.server_series
    for name in (
        "_HEX6_RE",
        "_SERIES_META_CACHE",
        "_SERIES_META_LOCK",
        "_SERIES_META_MAX",
        "_accepts_kwarg",
        "_add_allocation",
        "_allocated_bytes",
        "_associated_backend",
        "_associated_names",
        "_associated_source_path",
        "_attrs_with_colors",
        "_frame_args",
        "_inspect_path_series",
        "_inspect_slide_series",
        "_inspection_storage_details",
        "_path_usage",
        "_pixel_payload",
        "_render_plate_frame_compat",
        "_render_tile_compat",
        "_series_item_is_stack",
        "_storage_mode",
    ):
        assert getattr(server, name) is getattr(server_series, name)

    # Direct ui_settings and windows_fs re-exports
    assert server.load_ui_settings is ui_settings.load_ui_settings
    assert server.save_ui_settings is ui_settings.save_ui_settings
    assert server.list_recent_files is ui_settings.list_recent_files
    assert server.record_recent_file is ui_settings.record_recent_file
    assert server.is_windows_name_surrogate_reparse is windows_fs.is_windows_name_surrogate_reparse

    # Duplicate ui_settings fallback implementation has been removed
    for removed in (
        "_UI_FALLBACK_LOCK",
        "_PYTEST_FALLBACK_DIR",
        "_fallback_ui_settings_file",
        "_fallback_read_doc",
        "_fallback_write_doc",
        "_normalize_ui_settings_dict",
        "_is_unmounted_macos_volume",
    ):
        assert not hasattr(server, removed)

    # _*_safe wrappers delegate directly to ui_settings and honor ui_settings monkeypatches
    monkeypatch.setattr(ui_settings, "ui_settings_root", lambda: tmp_path)
    slide = tmp_path / "demo.nd2"
    slide.write_bytes(b"nd2")

    assert server._load_ui_settings_safe()["theme"] == "auto"
    saved = server._save_ui_settings_safe({"ok": True, "theme": "dark"})
    assert saved["theme"] == "dark"
    assert ui_settings.load_ui_settings()["theme"] == "dark"

    assert server._record_recent_file_safe(slide, window_role="agent") == []
    recents = server._record_recent_file_safe(slide, window_role="user")
    assert len(recents) == 1 and Path(recents[0]["path"]) == slide.resolve()
    assert server._list_recent_files_safe(limit=5) == ui_settings.list_recent_files(limit=5)
