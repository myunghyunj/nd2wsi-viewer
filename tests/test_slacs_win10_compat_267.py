"""Comprehensive unit tests for Wave 2 SLACS Windows 10 P0/P1 compatibility fixes (2.6.7).

Covers:
1. WebView2 runtime floor (>= 109), unified registry channels, disk binary verification,
   in-process Win32 advapi32 AppContainer ACL grant (folder + msedgewebview2.exe, zero icacls),
   watch_initialization sync-failure hook & _dismiss_failed_webview2_window no-shown fallback,
   and 1-shot auto-mode bundled <-> system fallback relaunch.
2. Zero-subprocess ctypes Toolhelp32 SLACS process discovery + elevated SLACS UIPI
   ERROR_ACCESS_DENIED (5) detection & hint (and slacs_controls UIPI hint).
3. Windows NSIS installer SLACS legacy 2.6.5/2.6.6 migration (strictly gated on
   "edition": "SLACS-module" & non-symlink checks), Win10 bundled WebView2 ACL pre-grant,
   FindRunningApp QueryFullProcessImageNameW scoping, de-hardcoded dialogs, and balance.
4. packaging/build_windows.py VS 2022 MSVC CRT DLL verification.
5. nd2wsi.app facade re-exports and delegation to nd2wsi.app_slacs.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import re
import struct
import sys
import threading
import zipfile
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app, app_slacs, slacs_controls, slacs_discovery
from nd2wsi import webview2_runtime as rt

ROOT = Path(__file__).resolve().parents[1]

HKLM, HKCU = "HKLM", "HKCU"
NET_KEY = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
NET_48 = {(HKLM, NET_KEY): {"Release": 528040}}
MACHINE_RUNTIME = "SOFTWARE\\WOW6432Node\\" + rt.CLIENT_KEY
MACHINE_RUNTIME_32 = "SOFTWARE\\" + rt.CLIENT_KEY
USER_RUNTIME = "SOFTWARE\\" + rt.CLIENT_KEY
BETA_CHANNEL = "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}"
MACHINE_BETA = f"SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{BETA_CHANNEL}"


class _FakeRegistry:
    HKEY_LOCAL_MACHINE = HKLM
    HKEY_CURRENT_USER = HKCU

    def __init__(self, values: dict):
        self.values = {(hive, subkey.lower()): value for (hive, subkey), value in values.items()}

    def OpenKey(self, hive, subkey):  # noqa: N802
        key = (hive, subkey.lower())
        if key not in self.values:
            raise FileNotFoundError(subkey)

        class _Key:
            def __enter__(self):
                return key

            def __exit__(self, *exc):
                return False

        return _Key()

    def QueryValueEx(self, key, name):  # noqa: N802
        value = self.values[key]
        if isinstance(value, dict):
            if name not in value:
                raise FileNotFoundError(name)
            value = value[name]
        elif name != "pv":
            raise FileNotFoundError(name)
        if isinstance(value, Exception):
            raise value
        return value, 1


class _FakeEvent(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


def _make_bundled_root(tmp_path: Path) -> Path:
    folder = tmp_path / rt.BUNDLED_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    (folder / rt.BROWSER_EXE).write_bytes(b"MZ")
    return tmp_path


def _minimal_pe(machine: int = 0x8664, payload: bytes = b"") -> bytes:
    header = bytearray(0x80)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    header[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", header, 0x44, machine)
    return bytes(header) + payload


# ---------------------------------------------------------------------------
# 1. WebView2 floor >= 109, unified channels & disk binary verification
# ---------------------------------------------------------------------------


def test_webview2_system_floor_is_109_and_selects_bundled_when_below(tmp_path: Path) -> None:
    assert rt.MIN_SYSTEM_MAJOR == 109
    root = _make_bundled_root(tmp_path)

    # 108.0.x is below the 109 floor: rejected by pywebview_accepts_system and auto chooses bundled.
    reg_108 = _FakeRegistry({(HKLM, MACHINE_RUNTIME): "108.0.1462.54", **NET_48})
    assert rt.system_runtime_version(reg_108) == "108.0.1462.54"
    assert rt.system_version_meets_floor("108.0.1462.54") is False
    assert rt.pywebview_accepts_system(reg_108) is False

    wv_108 = SimpleNamespace(settings={})
    choice_108 = rt.select_runtime(wv_108, environ={}, app_root=root, registry=reg_108)
    assert choice_108.kind == "bundled"
    assert "109" in choice_108.reason
    assert wv_108.settings["WEBVIEW2_RUNTIME_PATH"] == str(root / rt.BUNDLED_DIRNAME)

    # Direct choose_runtime without system_usable also enforces floor >= 109.
    direct = rt.choose_runtime("auto", bundled=root / rt.BUNDLED_DIRNAME, system_version="108.0.1462.54")
    assert direct.kind == "bundled"

    # 109.0.x meets the floor and is accepted in auto mode.
    reg_109 = _FakeRegistry({(HKLM, MACHINE_RUNTIME): "109.0.1518.52", **NET_48})
    assert rt.system_version_meets_floor("109.0.1518.52") is True
    assert rt.pywebview_accepts_system(reg_109) is True

    wv_109 = SimpleNamespace(settings={"WEBVIEW2_RUNTIME_PATH": None})
    choice_109 = rt.select_runtime(wv_109, environ={}, app_root=root, registry=reg_109)
    assert choice_109.kind == "system"
    assert choice_109.system_version == "109.0.1518.52"
    assert wv_109.settings["WEBVIEW2_RUNTIME_PATH"] is None


def test_webview2_unified_registry_channels_and_disk_binary_verification(tmp_path: Path) -> None:
    root = _make_bundled_root(tmp_path)
    # Beta channel only: BOTH system_runtime_version and pywebview_accepts_system see it.
    reg_beta = _FakeRegistry({(HKLM, MACHINE_BETA): "125.0.2535.67", **NET_48})
    assert rt.system_runtime_version(reg_beta) == "125.0.2535.67"
    assert rt.pywebview_accepts_system(reg_beta) is True

    # Stale Win10 registry key (pv set, msedgewebview2.exe missing on disk) is rejected.
    reg_stale = _FakeRegistry({(HKLM, MACHINE_RUNTIME): "125.0.2535.67", **NET_48})
    assert rt.system_runtime_version(reg_stale, verify_binary=True, binary_exists=lambda *_a, **_k: False) is None
    assert rt.pywebview_accepts_system(reg_stale, verify_binary=True, binary_exists=lambda *_a, **_k: False) is False

    wv = SimpleNamespace(settings={})
    choice = rt.select_runtime(
        wv,
        environ={},
        app_root=root,
        registry=reg_stale,
        verify_binary=True,
        binary_exists=lambda *_a, **_k: False,
    )
    assert choice.kind == "bundled"
    assert choice.system_version is None

    # Real filesystem check via system_runtime_binary_exists + custom location / ProgramFiles.
    pf = tmp_path / "pf86"
    exe = pf / "Microsoft" / "EdgeWebView" / "Application" / "125.0.2535.67" / rt.BROWSER_EXE
    assert rt.system_runtime_binary_exists(
        "125.0.2535.67", hive=HKLM, environ={"ProgramFiles(x86)": str(pf)},
    ) is False
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert rt.system_runtime_binary_exists(
        "125.0.2535.67", hive=HKLM, environ={"ProgramFiles(x86)": str(pf)},
    ) is True


# ---------------------------------------------------------------------------
# 2. In-process Win32 advapi32 AppContainer ACL grant (zero icacls subprocess)
# ---------------------------------------------------------------------------


def test_webview2_advapi32_acl_grant_in_process_and_checks_browser_exe(tmp_path: Path) -> None:
    assert not hasattr(rt, "subprocess")

    # Exercise _grant_appcontainer_acl_win32 directly against a mocked advapi32 + kernel32.
    recorded: dict = {"sids": [], "entries": [], "set_calls": [], "freed": []}

    class _FakeAdvapi:
        def ConvertStringSidToSidW(self, sid_text, out_ptr):  # noqa: N802
            recorded["sids"].append(sid_text)
            ptr = ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_void_p)).contents
            ptr.value = 0x1000 + len(recorded["sids"])
            return 1

        def GetNamedSecurityInfoW(self, path, obj_type, info, _o, _g, out_dacl, _s, out_sd):  # noqa: N802
            assert obj_type == 1 and info == 4
            ctypes.cast(out_dacl, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0x2000
            ctypes.cast(out_sd, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0x3000
            return 0

        def SetEntriesInAclW(self, count, entries, old_dacl, out_new_dacl):  # noqa: N802
            assert count == len(rt.APPCONTAINER_SIDS)
            assert old_dacl.value == 0x2000
            for idx in range(count):
                e = entries[idx]
                recorded["entries"].append((
                    int(e.grfAccessPermissions),
                    int(e.grfAccessMode),
                    int(e.grfInheritance),
                    int(e.Trustee.TrusteeForm),
                    int(e.Trustee.ptstrName),
                ))
            ctypes.cast(out_new_dacl, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0x4000
            return 0

        def SetNamedSecurityInfoW(self, path, obj_type, info, _o, _g, new_dacl, _s):  # noqa: N802
            recorded["set_calls"].append((path, obj_type, info, new_dacl.value))
            return 0

    class _FakeKernel:
        def LocalFree(self, ptr):  # noqa: N802
            recorded["freed"].append(ptr.value)
            return 0

    folder = tmp_path / "webview2-runtime"
    folder.mkdir()
    rt._grant_appcontainer_acl_win32(folder, inherit=True, advapi=_FakeAdvapi(), kernel=_FakeKernel())

    assert recorded["sids"] == ["S-1-15-2-1", "S-1-15-2-2"]
    assert recorded["entries"] == [
        (0x001200A9, 1, 0x3, 0, 0x1001),
        (0x001200A9, 1, 0x3, 0, 0x1002),
    ]
    assert recorded["set_calls"] == [(str(folder), 1, 4, 0x4000)]
    # All allocated handles (new_dacl, security descriptor, both SIDs) freed.
    assert set(recorded["freed"]) == {0x4000, 0x3000, 0x1001, 0x1002}

    # Verify ensure_appcontainer_access also checks and grants ACL on msedgewebview2.exe
    # when the folder is allowed but msedgewebview2.exe has broken inheritance.
    exe = folder / rt.BROWSER_EXE
    exe.write_bytes(b"MZ")
    states = {folder: "allowed", exe: "missing"}
    grant_calls: list[tuple[Path, bool]] = []

    def fake_check(path):
        return states[Path(path)]

    def fake_grant(path, *, inherit=True):
        grant_calls.append((Path(path), inherit))
        states[Path(path)] = "allowed"

    assert rt.ensure_appcontainer_access(folder, build=19045, check=fake_check, grant=fake_grant) == "granted"
    assert grant_calls == [(exe, False)]

    # File-level appcontainer_decision accepts non-inheritable effective ACEs (require_inherit=False).
    file_aces = [(0, 0x10, 0x1200A9, 0), (0, 0x10, 0x1200A9, 1)]
    assert rt.appcontainer_decision(file_aces, require_inherit=True) == "missing"
    assert rt.appcontainer_decision(file_aces, require_inherit=False) == "allowed"


# ---------------------------------------------------------------------------
# 3. watch_initialization sync failure & _dismiss_failed_webview2_window fallback
# ---------------------------------------------------------------------------


def test_watch_initialization_catches_sync_failure_before_before_show(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeEdgeChrome:
        def __init__(self, window):
            self.pywebview_window = window

        def on_webview_ready(self, sender, args):
            return None

    fake_mod = SimpleNamespace(EdgeChrome=_FakeEdgeChrome)
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", fake_mod)

    window = SimpleNamespace(
        events=SimpleNamespace(initialized=_FakeEvent(), before_show=_FakeEvent(), closed=_FakeEvent()),
    )
    results: list[tuple[bool, str | None]] = []
    assert rt.watch_initialization(window, lambda ok, detail: results.append((ok, detail))) is True

    # initialized handler must never return False (would veto pywebview startup).
    for handler in window.events.initialized:
        assert handler("edgechromium") is not False

    # Synchronous failure inside EdgeChrome.__init__ BEFORE before_show fires!
    chrome = _FakeEdgeChrome(window)
    chrome.on_webview_ready(None, SimpleNamespace(IsSuccess=False, InitializationException="0x80070005 denied"))
    assert results == [(False, "0x80070005 denied")]

    # Subsequent before_show firing does not double-report even if native control is absent.
    for handler in window.events.before_show:
        handler()
    assert len(results) == 1


def test_dismiss_failed_webview2_window_falls_back_when_shown_never_fires() -> None:
    shown = threading.Event()  # intentionally never set
    gui_destroyed: list[str] = []
    destroy_calls: list[int] = []

    window = SimpleNamespace(
        uid="master",
        events=SimpleNamespace(shown=shown),
        gui=SimpleNamespace(destroy_window=gui_destroyed.append),
        destroy=lambda: destroy_calls.append(1),
    )
    approved: list[str] = []
    api = SimpleNamespace(_close_coordinator=SimpleNamespace(approve_close=approved.append))

    class _SyncSpawn:
        def __init__(self, *, target, name, daemon):
            assert name == "webview2-failed-close" and daemon is True
            self._target = target

        def start(self):
            self._target()

    app._dismiss_failed_webview2_window(api, window, spawn=_SyncSpawn, shown_timeout=0.01)
    assert approved and "WebView2" in approved[0]
    assert gui_destroyed == ["master"]
    assert destroy_calls == []  # skipped @_shown_call wait!

    # Also falls back to native.Close() when gui.destroy_window is absent.
    native_closed: list[int] = []
    window_native = SimpleNamespace(
        uid="master",
        events=SimpleNamespace(shown=threading.Event()),
        native=SimpleNamespace(InvokeRequired=False, Close=lambda: native_closed.append(1)),
        destroy=Mock(side_effect=RuntimeError("shown timed out")),
    )
    app._dismiss_failed_webview2_window(api, window_native, spawn=_SyncSpawn, shown_timeout=0.01)
    assert native_closed == [1]


# ---------------------------------------------------------------------------
# 4. 1-shot auto-mode runtime fallback (bundled <-> system)
# ---------------------------------------------------------------------------


def test_auto_mode_one_shot_fallback_system_to_bundled_and_bundled_to_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_bundled_root(tmp_path)
    reg_ok = _FakeRegistry({(HKLM, MACHINE_RUNTIME): "125.0.2535.67", **NET_48})

    # system -> bundled when bundled copy exists and fallback not yet tried.
    sel_sys = {"kind": "system", "mode": "auto", "path": None, "system_version": "125.0.2535.67"}
    assert rt.fallback_runtime_mode(sel_sys, environ={}, app_root=root) == "bundled"
    # Already tried -> None (strictly 1-shot, no infinite loop).
    assert rt.fallback_runtime_mode(sel_sys, environ={rt.FALLBACK_TRIED_ENV: "1"}, app_root=root) is None
    # Explicit mode="system" -> never auto-falls back.
    assert rt.fallback_runtime_mode({**sel_sys, "mode": "system"}, environ={}, app_root=root) is None

    # bundled -> system when valid system Evergreen >= 109 exists.
    sel_bun = {"kind": "bundled", "mode": "auto", "path": str(root / rt.BUNDLED_DIRNAME),
               "system_version": "125.0.2535.67"}
    assert rt.fallback_runtime_mode(sel_bun, environ={}, app_root=root, registry=reg_ok) == "system"
    assert rt.fallback_runtime_mode(sel_bun, environ={rt.FALLBACK_TRIED_ENV: "1"}, app_root=root, registry=reg_ok) is None
    # When system is below 109, bundled -> system is refused.
    reg_old = _FakeRegistry({(HKLM, MACHINE_RUNTIME): "108.0.0.1", **NET_48})
    assert rt.fallback_runtime_mode(sel_bun, environ={}, app_root=root, registry=reg_old) is None

    # End-to-end relaunch via app_slacs.maybe_relaunch_webview2_fallback.
    monkeypatch.setattr(sys, "platform", "win32")
    spawned: list[tuple[list[str], dict]] = []

    def fake_popen(cmd, **kwargs):
        spawned.append((cmd, kwargs))
        return SimpleNamespace(pid=777)

    api = SimpleNamespace(
        _webview2_runtime=sel_sys,
        _webview2_init={"ok": False, "error": "0x80070002"},
    )
    args = SimpleNamespace(gui_smoke=False, browser_replay_report=None, nd2="slide.nd2",
                           agent_window=False, new_window=False)
    assert app.maybe_relaunch_webview2_fallback(
        api, args, raw_argv=["slide.nd2"], environ={}, app_root=root, popen=fake_popen, logger=lambda _m: None,
    ) is True
    assert len(spawned) == 1
    cmd, kwargs = spawned[0]
    assert cmd[-1] == "slide.nd2"
    assert kwargs["env"][rt.MODE_ENV] == "bundled"
    assert kwargs["env"][rt.FALLBACK_TRIED_ENV] == "1"
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    # Once ND2WSI_WEBVIEW2_FALLBACK_TRIED=1 is present, second failure does NOT relaunch.
    assert app.maybe_relaunch_webview2_fallback(
        api, args, raw_argv=["slide.nd2"], environ={rt.FALLBACK_TRIED_ENV: "1"},
        app_root=root, popen=fake_popen, logger=lambda _m: None,
    ) is False
    assert len(spawned) == 1


def test_app_main_performs_one_shot_fallback_without_error_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_bundled_root(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv(rt.MODE_ENV, raising=False)
    monkeypatch.delenv(rt.FALLBACK_TRIED_ENV, raising=False)
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr(rt, "default_app_root", lambda: root)

    selection = {"kind": "system", "mode": "auto", "path": None, "system_version": "125.0.2535.67", "reason": "ok"}
    monkeypatch.setattr(rt, "select_runtime", Mock(return_value=SimpleNamespace(report=lambda: dict(selection))))

    watched: dict = {}
    monkeypatch.setattr(rt, "watch_initialization", lambda win, rec: watched.update(rec=rec) or True)
    monkeypatch.setattr(app, "_dismiss_failed_webview2_window", Mock())

    dialog = Mock()
    monkeypatch.setattr(app, "_startup_error", dialog)

    spawned: list[dict] = []
    monkeypatch.setattr(
        app_slacs.subprocess,
        "Popen",
        lambda cmd, **kw: spawned.append({"cmd": cmd, "env": kw["env"]}) or SimpleNamespace(pid=9001),
    )

    api_box = SimpleNamespace(_startup_error=None, _webview2_runtime=None, _webview2_init=None,
                              stop_server_for_update=Mock())
    monkeypatch.setattr(app, "create_app_window", Mock(return_value=(api_box, SimpleNamespace())))
    fake_wv = SimpleNamespace(start=Mock(side_effect=lambda **_kw: watched["rec"](False, "0x80070005")), settings={})
    monkeypatch.setitem(sys.modules, "webview", fake_wv)

    assert app.main([]) == 0
    dialog.assert_not_called()
    assert len(spawned) == 1
    assert spawned[0]["env"][rt.MODE_ENV] == "bundled"
    assert spawned[0]["env"][rt.FALLBACK_TRIED_ENV] == "1"


# ---------------------------------------------------------------------------
# 5. slacs_discovery zero-subprocess Win32 Toolhelp32 + UIPI elevated detection
# ---------------------------------------------------------------------------


def _fake_win32_process_api(entries: list[dict]):
    """Build a fake Win32 kernel32+ntdll Toolhelp32 API for hardware-free testing."""

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_int32),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_uint16),
            ("MaximumLength", ctypes.c_uint16),
            ("Buffer", ctypes.c_void_p),
        ]

    state = {"err": 0, "idx": -1, "closed": [], "keepalive": []}
    by_pid = {item["pid"]: item for item in entries}
    handle_to_pid: dict[int, int] = {}

    class _Kernel32:
        def CreateToolhelp32Snapshot(self, flags, pid):  # noqa: N802
            assert flags == 0x2 and pid == 0
            return 0xCAFE

        def Process32FirstW(self, snap, ptr):  # noqa: N802
            assert snap == 0xCAFE
            if not entries:
                state["err"] = 18
                return 0
            state["idx"] = 0
            e = ctypes.cast(ptr, ctypes.POINTER(PROCESSENTRY32W)).contents
            e.th32ProcessID = entries[0]["pid"]
            e.szExeFile = entries[0]["name"]
            return 1

        def Process32NextW(self, snap, ptr):  # noqa: N802
            state["idx"] += 1
            if state["idx"] >= len(entries):
                state["err"] = 18
                return 0
            e = ctypes.cast(ptr, ctypes.POINTER(PROCESSENTRY32W)).contents
            e.th32ProcessID = entries[state["idx"]]["pid"]
            e.szExeFile = entries[state["idx"]]["name"]
            return 1

        def OpenProcess(self, access, inherit, pid):  # noqa: N802
            assert access == 0x1000 and inherit is False
            item = by_pid[pid]
            if item.get("open_error"):
                state["err"] = item["open_error"]
                return 0
            handle = 0x5000 + pid
            handle_to_pid[handle] = pid
            return handle

        def QueryFullProcessImageNameW(self, handle, flags, buf, size_ptr):  # noqa: N802
            item = by_pid[handle_to_pid[handle]]
            image = item.get("image", rf"C:\Python27\{item['name']}")
            buf.value = image
            ctypes.cast(size_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = len(image)
            return 1

        def GetProcessTimes(self, handle, c_ptr, _e, _k, _u):  # noqa: N802
            item = by_pid[handle_to_pid[handle]]
            ticks = item.get("ticks", 134_060_904_000_000_000)
            ft = ctypes.cast(c_ptr, ctypes.POINTER(wintypes.FILETIME)).contents
            ft.dwHighDateTime = (ticks >> 32) & 0xFFFFFFFF
            ft.dwLowDateTime = ticks & 0xFFFFFFFF
            return 1

        def CloseHandle(self, handle):  # noqa: N802
            state["closed"].append(handle)
            return 1

    class _Ntdll:
        def NtQueryInformationProcess(self, handle, info_class, buf, buf_len, ret_len):  # noqa: N802
            assert info_class == 60
            item = by_pid[handle_to_pid[handle]]
            if item.get("cmd_denied"):
                return -1073741790  # STATUS_ACCESS_DENIED (0xC0000022)
            cmd = item.get("command_line", "")
            us_size = ctypes.sizeof(UNICODE_STRING)
            encoded = ctypes.create_unicode_buffer(cmd)
            byte_len = len(cmd) * ctypes.sizeof(ctypes.c_wchar)
            total = us_size + byte_len + ctypes.sizeof(ctypes.c_wchar)
            ctypes.cast(ret_len, ctypes.POINTER(wintypes.ULONG)).contents.value = total
            if buf is None or buf_len < total:
                return -1073741820  # STATUS_INFO_LENGTH_MISMATCH
            state["keepalive"].append(encoded)
            us = ctypes.cast(buf, ctypes.POINTER(UNICODE_STRING)).contents
            us.Length = byte_len
            us.MaximumLength = byte_len + ctypes.sizeof(ctypes.c_wchar)
            us.Buffer = ctypes.addressof(encoded)
            return 0

    api = SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        kernel32=_Kernel32(),
        ntdll=_Ntdll(),
        PROCESSENTRY32W=PROCESSENTRY32W,
        UNICODE_STRING=UNICODE_STRING,
        get_last_error=lambda: state["err"],
        set_last_error=lambda v: state.__setitem__("err", v),
    )
    return api, state


def test_slacs_discovery_toolhelp32_enumerates_slacs_without_powershell() -> None:
    assert not hasattr(slacs_discovery, "subprocess")
    script = r"C:\Python27\Scripts\SniperCellSorterGUIv3.1.py"
    api, state = _fake_win32_process_api([
        {"pid": 100, "name": "explorer.exe"},
        {"pid": 200, "name": "python.exe", "command_line": r'"C:\Python312\python.exe" other.py'},
        {"pid": 300, "name": "pythonw.exe", "command_line": f'"C:\\Python27\\pythonw.exe" "{script}"'},
    ])
    rows = slacs_discovery._query_windows_processes(api=api)
    assert [r["pid"] for r in rows] == [200, 300]
    assert 0xCAFE in state["closed"]
    assert 0x5000 + 200 in state["closed"] and 0x5000 + 300 in state["closed"]

    snap = slacs_discovery._identify_processes(rows)
    assert snap["available"] is True
    assert snap["status"] == "running"
    assert snap["session_id"].startswith("slacs-")
    assert "hint" not in snap


def test_slacs_discovery_detects_elevated_uipi_access_denied() -> None:
    # 1. OpenProcess returns ERROR_ACCESS_DENIED (5) -> status="elevated" + Administrator hint.
    api_open, _ = _fake_win32_process_api([
        {"pid": 404, "name": "python.exe", "open_error": 5},
    ])
    rows_open = slacs_discovery._query_windows_processes(api=api_open)
    assert rows_open[0]["access_denied"] is True and rows_open[0]["winerror"] == 5
    snap_open = slacs_discovery._identify_processes(rows_open)
    assert snap_open["available"] is False
    assert snap_open["status"] == "elevated"
    assert "Administrator" in snap_open["hint"] and "UIPI" in snap_open["hint"]

    # 2. NtQueryInformationProcess returns STATUS_ACCESS_DENIED -> also reports "elevated".
    api_cmd, _ = _fake_win32_process_api([
        {"pid": 405, "name": "python.exe", "cmd_denied": True},
    ])
    rows_cmd = slacs_discovery._query_windows_processes(api=api_cmd)
    snap_cmd = slacs_discovery._identify_processes(rows_cmd)
    assert snap_cmd["status"] == "elevated" and "Administrator" in snap_cmd["hint"]

    # 3. Monitor surfaces "elevated" snapshot cleanly.
    monitor = slacs_discovery.SlacsDiscovery(process_query=lambda: rows_open, autostart=False)
    polled = monitor.poll_once()
    assert polled["status"] == "elevated" and polled["available"] is False
    assert "Administrator" in polled["hint"]


def test_slacs_controls_reports_clear_uipi_hint_on_error_5() -> None:
    user32 = SimpleNamespace(
        SendMessageTimeoutW=lambda *a: 0,
        IsWindow=lambda _h: 1,
    )
    api = SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        user32=user32,
        get_window_long=lambda *_a: 0,
        WNDENUMPROC=lambda fn: fn,
        get_last_error=lambda: 5,
        set_last_error=lambda _v: 0,
    )
    with pytest.raises(slacs_controls._Unreadable, match="Administrator|UIPI"):
        slacs_controls._Win32Windows(api).send(0x42, slacs_controls._BM_GETCHECK, 80)


# ---------------------------------------------------------------------------
# 6. packaging/windows-installer.nsi & build_windows_installer.py Win10 contracts
# ---------------------------------------------------------------------------


def test_nsis_slacs_legacy_migration_acl_and_find_running_app_scoping() -> None:
    nsi = (ROOT / "packaging" / "windows-installer.nsi").read_text(encoding="utf-8")

    # 1. Legacy 2.6.5/2.6.6 SLACS migration is strictly gated under !ifdef SLACS_EDITION
    #    and checks non-symlink build-info.json for "edition": "SLACS-module".
    assert "!ifdef SLACS_EDITION" in nsi
    mig_start = nsi.index("Function MigrateLegacySlacsInstall")
    mig_end = nsi.index("FunctionEnd", mig_start)
    mig_body = nsi[mig_start:mig_end]
    assert 'Call FileContainsSlacsEdition' in mig_body
    assert r'$R0\_internal\nd2wsi-viewer-about\build-info.json' in mig_body
    assert r'$R0\build-info.json' in mig_body
    assert 'Call CheckPathChain' in mig_body
    assert 'legacy-slacs-uninstall.exe" /S _?=$R0' in mig_body
    assert 'DeleteRegKey HKCU "${LEGACY_SLACS_UNINSTALL_KEY}"' in mig_body

    ed_start = nsi.index("Function FileContainsSlacsEdition")
    ed_end = nsi.index("FunctionEnd", ed_start)
    ed_body = nsi[ed_start:ed_end]
    assert '"edition": "SLACS-module"' in ed_body
    assert "Call IsNameSurrogateReparse" in ed_body
    assert "Call CheckPathChain" in ed_body

    # 2. Win10 (< Win11) bundled WebView2 ACL pre-grant uses in-process advapi32 (no icacls).
    acl_start = nsi.index("Function GrantBundledWebView2Acl")
    acl_end = nsi.index("FunctionEnd", acl_start)
    acl_body = nsi[acl_start:acl_end]
    assert "${AtLeastWin11}" in acl_body
    assert "S-1-15-2-1" in acl_body
    assert "S-1-15-2-2" in acl_body
    assert "msedgewebview2.exe" in acl_body

    sid_start = nsi.index("Function GrantSidRxOnPath")
    sid_end = nsi.index("FunctionEnd", sid_start)
    sid_body = nsi[sid_start:sid_end]
    for win32_call in (
        "advapi32::ConvertStringSidToSidW",
        "advapi32::GetNamedSecurityInfoW",
        "advapi32::SetEntriesInAclW",
        "advapi32::SetNamedSecurityInfoW",
    ):
        assert win32_call in sid_body
    non_comment_nsi = "\n".join(
        line for line in nsi.splitlines() if not line.lstrip().startswith(";")
    )
    assert "icacls" not in non_comment_nsi.lower()

    # 3. SecApplication calls GrantBundledWebView2Acl and MigrateLegacySlacsInstall
    #    after payload extraction and BEFORE WriteFileAssociations.
    sec_start = nsi.index('Section "${APP_DISPLAY_NAME} (required)" SecApplication')
    sec_end = nsi.index("SectionEnd", sec_start)
    sec_body = nsi[sec_start:sec_end]
    assert sec_body.index("Call GrantBundledWebView2Acl") < sec_body.index("Call WriteFileAssociations")
    assert sec_body.index("Call MigrateLegacySlacsInstall") < sec_body.index("Call WriteFileAssociations")

    # 4. FindRunningApp scopes to $INSTDIR\${APP_EXE} via OpenProcess(0x1000) + QueryFullProcessImageNameW.
    find_start = nsi.index("Function ${PREFIX}FindRunningApp")
    find_end = nsi.index("FunctionEnd", find_start)
    find_body = nsi[find_start:find_end]
    assert "kernel32::OpenProcess(i 0x1000" in find_body
    assert "kernel32::QueryFullProcessImageNameW" in find_body
    assert 'Push "$INSTDIR\\${APP_EXE}"' in find_body
    assert "FindRunningApp.other_install" in find_body

    # 5. Zero hardcoded "nd2wsi-viewer" in any MessageBox or GuardError dialog string.
    for line in nsi.splitlines():
        stripped = line.strip()
        if stripped.startswith(("MessageBox ", "StrCpy $GuardError ")):
            assert "nd2wsi-viewer" not in stripped, f"Hardcoded dialog string: {stripped}"

    # 6. Full NSIS structural balance.
    assert len(re.findall(r"(?m)^\s*Function\s+", nsi)) == len(re.findall(r"(?m)^\s*FunctionEnd\s*$", nsi))
    assert len(re.findall(r"(?m)^\s*Section\s+", nsi)) == len(re.findall(r"(?m)^\s*SectionEnd\s*$", nsi))
    assert len(re.findall(r"(?m)^\s*!macro\s+", nsi)) == len(re.findall(r"(?m)^\s*!macroend\s*$", nsi))
    assert len(re.findall(r"(?m)^\s*!if(?:def|ndef)?\b", nsi)) == len(re.findall(r"(?m)^\s*!endif\b", nsi))
    assert len(re.findall(r"\$\{(?:If|IfNot)\}", nsi)) == len(re.findall(r"\$\{EndIf\}", nsi))
    assert len(re.findall(r"\$\{(?:Do|DoWhile)\}", nsi)) == len(re.findall(r"\$\{Loop\}", nsi))


def test_build_windows_installer_defines_slacs_edition_only_for_slacs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "WINDOWS_SIGN_PFX_PATH", "WINDOWS_SIGN_PFX_PASSWORD", "WINDOWS_SIGN_THUMBPRINT",
        "WINDOWS_SIGNTOOL", "WINDOWS_REQUIRE_SIGNING",
    ):
        monkeypatch.delenv(var, raising=False)
    spec = importlib.util.spec_from_file_location(
        "build_win_inst_267", ROOT / "packaging/build_windows_installer.py",
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    def _run_build(name: str, edition: str | None) -> dict:
        exe = _minimal_pe(0x8664, name.encode())
        import hashlib
        info = {"app": "nd2wsi-viewer", "version": "2.6.7", "binary_architecture": "x64",
                "executable_sha256": hashlib.sha256(exe).hexdigest()}
        if edition is not None:
            info["edition"] = edition
            info["release_version"] = "2.6.7_SLACS-module"
        archive = tmp_path / f"{name}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("nd2wsi-viewer/nd2wsi-viewer.exe", exe)
            zf.writestr("nd2wsi-viewer/READ-ME.txt", b"readme\n")
            zf.writestr("nd2wsi-viewer/_internal/nd2wsi-viewer-about/build-info.json", json.dumps(info).encode())
            zf.writestr("nd2wsi-viewer/_internal/nd2wsi-viewer-about/LICENSE.txt", b"MIT\n")
        boot = tmp_path / f"{name}-boot.exe"
        boot.write_bytes(_minimal_pe(0x014C, b"boot"))
        defs_file = tmp_path / f"{name}-defs.json"
        makensis = tmp_path / f"{name}-makensis.py"
        makensis.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, struct, sys\n"
            "if '-VERSION' in sys.argv:\n"
            "    print('v3.13'); raise SystemExit(0)\n"
            "defs = dict(a[2:].split('=', 1) for a in sys.argv[1:] if a.startswith('-D') and '=' in a)\n"
            f"pathlib.Path({str(defs_file)!r}).write_text(json.dumps(defs))\n"
            "out = pathlib.Path(defs['OUTPUT_FILE'])\n"
            "hdr = bytearray(0x80); hdr[:2] = b'MZ'; struct.pack_into('<I', hdr, 0x3C, 0x40)\n"
            "out.write_bytes(bytes(hdr[:0x40]) + b'PE\\0\\0' + struct.pack('<H', 0x014C) + b'\\0'*24)\n",
            encoding="utf-8",
        )
        makensis.chmod(0o755)
        makensis_bin = makensis
        if sys.platform == "win32":
            makensis_bin = tmp_path / f"{name}-makensis.cmd"
            makensis_bin.write_text(f'@echo off\r\n"{sys.executable}" "{makensis}" %*\r\n', encoding="ascii")
        out_exe = tmp_path / f"{name}-setup.exe"
        assert builder.main([
            "--archive", str(archive),
            "--expected-sha256", builder.sha256(archive),
            "--makensis", str(makensis_bin),
            "--webview2-bootstrapper", str(boot),
            "--webview2-sha256", builder.sha256(boot),
            "--work", str(tmp_path / f"{name}-work"),
            "--output", str(out_exe),
        ]) == 0
        return json.loads(defs_file.read_text(encoding="utf-8"))

    slacs_defs = _run_build("slacs", "SLACS-module")
    public_defs = _run_build("public", None)
    assert slacs_defs.get("SLACS_EDITION") == "1"
    assert "SLACS_EDITION" not in public_defs


# ---------------------------------------------------------------------------
# 7. packaging/build_windows.py MSVC CRT DLL verification
# ---------------------------------------------------------------------------


def test_build_windows_verifies_msvc_crt_dlls(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("build_win_crt_267", ROOT / "packaging/build_windows.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    assert set(builder.REQUIRED_MSVC_CRT_DLLS) == {"vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"}

    bundle = tmp_path / "dist"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)

    # Missing all three -> fails clearly listing all three DLLs.
    with pytest.raises(RuntimeError, match="vcruntime140.dll.*vcruntime140_1.dll.*msvcp140.dll"):
        builder.verify_msvc_crt_dlls(bundle)

    # Present with wrong architecture (32-bit x86) -> fails clearly.
    for name in builder.REQUIRED_MSVC_CRT_DLLS:
        (internal / name).write_bytes(_minimal_pe(0x8664, name.encode()))
    (internal / "msvcp140.dll").write_bytes(_minimal_pe(0x014C, b"x86"))
    with pytest.raises(RuntimeError, match="not an x64"):
        builder.verify_msvc_crt_dlls(bundle)

    # All three valid x64 DLLs (including mixed-case Windows filename) -> passes.
    (internal / "msvcp140.dll").unlink()
    (internal / "MSVCP140.dll").write_bytes(_minimal_pe(0x8664, b"x64"))
    verified = builder.verify_msvc_crt_dlls(bundle)
    assert set(verified) == set(builder.REQUIRED_MSVC_CRT_DLLS)
    assert verified["msvcp140.dll"] == "_internal/MSVCP140.dll"

    # Wired into main() in build_windows.py.
    source = (ROOT / "packaging/build_windows.py").read_text(encoding="utf-8")
    assert "verify_msvc_crt_dlls(bundle)" in source


# ---------------------------------------------------------------------------
# 8. nd2wsi.app facade re-exports from nd2wsi.app_slacs
# ---------------------------------------------------------------------------


def test_app_facade_reexports_and_delegates_to_app_slacs() -> None:
    for symbol in (
        "build_relaunch_command",
        "configure_win32_webview2",
        "maybe_relaunch_webview2_fallback",
    ):
        assert getattr(app, symbol) is getattr(app_slacs, symbol)

    for method in (
        "slacs_discovery_state",
        "_log_slacs_detection",
        "_claim_slacs_offer",
        "open_translucent_viewer",
        "report_slacs_laser_box",
        "_follow_flip_store",
        "close_translucent_viewer",
        "_close_translucent_windows",
    ):
        assert callable(getattr(app.Api, method))

    assert callable(app._dismiss_failed_webview2_window)
    assert callable(app._startup_error)
