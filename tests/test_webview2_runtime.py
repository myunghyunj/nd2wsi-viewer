import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import webview2_runtime as rt

HKLM, HKCU = "HKLM", "HKCU"
MACHINE = "SOFTWARE\\WOW6432Node\\" + rt.CLIENT_KEY
MACHINE_32 = "SOFTWARE\\" + rt.CLIENT_KEY
USER = "Software\\" + rt.CLIENT_KEY


class FakeRegistry:
    """winreg stand-in. Keys match case-insensitively, like the real registry.

    A value is either the key's ``pv`` or a {name: value} mapping.
    """

    HKEY_LOCAL_MACHINE = HKLM
    HKEY_CURRENT_USER = HKCU

    def __init__(self, values):
        self.values = {(hive, subkey.lower()): value for (hive, subkey), value in values.items()}
        self.opened = []

    def OpenKey(self, hive, subkey):  # noqa: N802 - winreg name
        self.opened.append((hive, subkey))
        key = (hive, subkey.lower())
        if key not in self.values:
            raise FileNotFoundError(subkey)

        class Key:
            def __enter__(self):
                return key

            def __exit__(self, *exc):
                return False

        return Key()

    def QueryValueEx(self, key, name):  # noqa: N802 - winreg name
        value = self.values[key]
        if isinstance(value, dict):
            if name not in value:
                raise FileNotFoundError(name)
            value = value[name]
        else:
            assert name == "pv"
        if isinstance(value, Exception):
            raise value
        return value, 1


NET = "SOFTWARE\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full"
NET_48 = {(HKLM, NET): {"Release": 528040}}


def bundled_root(tmp_path):
    folder = tmp_path / rt.BUNDLED_DIRNAME
    folder.mkdir()
    (folder / rt.BROWSER_EXE).write_bytes(b"MZ")
    return tmp_path


@pytest.mark.parametrize("value, expected", [
    ("156.0.4314.42", (156, 0, 4314, 42)),
    (" 120.0.2210.91 ", (120, 0, 2210, 91)),
    ("", None),
    ("0.0.0.0", None),
    (None, None),
    ("abc", None),
    ("156..4314.42", None),
    ("-1.0.0.0", None),
])
def test_parse_version_follows_microsofts_not_installed_states(value, expected):
    assert rt.parse_version(value) == expected


def test_empty_user_value_does_not_hide_a_valid_machine_runtime():
    registry = FakeRegistry({(HKCU, USER): "", (HKLM, MACHINE): "156.0.4314.42"})
    assert rt.system_runtime_version(registry) == "156.0.4314.42"


def test_highest_valid_version_wins_and_zero_counts_as_missing():
    registry = FakeRegistry({
        (HKLM, MACHINE): "0.0.0.0",
        (HKLM, MACHINE_32): "120.0.1.2",
        (HKCU, USER): "121.0.0.1",
    })
    assert rt.system_runtime_version(registry) == "121.0.0.1"


def test_no_registered_runtime_and_unreadable_values_report_none():
    assert rt.system_runtime_version(FakeRegistry({})) is None
    registry = FakeRegistry({(HKLM, MACHINE): PermissionError("denied")})
    assert rt.system_runtime_version(registry) is None


def test_non_windows_without_registry_reports_none(monkeypatch):
    monkeypatch.setattr(rt.sys, "platform", "darwin")
    assert rt.system_runtime_version() is None


@pytest.mark.parametrize("raw, mode", [(None, "auto"), ("", "auto"), (" Bundled ", "bundled"), ("system", "system")])
def test_runtime_mode_reads_the_environment(raw, mode):
    environ = {} if raw is None else {rt.MODE_ENV: raw}
    assert rt.runtime_mode(environ) == mode


def test_runtime_mode_rejects_unknown_values():
    with pytest.raises(ValueError, match=rt.MODE_ENV):
        rt.runtime_mode({rt.MODE_ENV: "fixed"})


def test_bundled_runtime_requires_the_browser_executable(tmp_path):
    assert rt.bundled_runtime_dir(tmp_path) is None
    (tmp_path / rt.BUNDLED_DIRNAME).mkdir()
    assert rt.bundled_runtime_dir(tmp_path) is None
    (tmp_path / rt.BUNDLED_DIRNAME / rt.BROWSER_EXE).write_bytes(b"MZ")
    assert rt.bundled_runtime_dir(tmp_path) == tmp_path / rt.BUNDLED_DIRNAME


def test_auto_prefers_a_valid_system_runtime(tmp_path):
    choice = rt.choose_runtime("auto", bundled=tmp_path, system_version="156.0.4314.42")
    assert (choice.kind, choice.path) == ("system", None)


def test_auto_falls_back_to_the_bundled_copy(tmp_path):
    choice = rt.choose_runtime("auto", bundled=tmp_path, system_version=None)
    assert (choice.kind, choice.path) == ("bundled", tmp_path)


def test_auto_without_either_keeps_todays_system_path():
    choice = rt.choose_runtime("auto", bundled=None, system_version=None)
    assert (choice.kind, choice.path) == ("system", None)


def test_bundled_mode_wins_over_a_system_runtime_and_requires_the_copy(tmp_path):
    choice = rt.choose_runtime("bundled", bundled=tmp_path, system_version="156.0.4314.42")
    assert (choice.kind, choice.path, choice.system_version) == ("bundled", tmp_path, "156.0.4314.42")
    with pytest.raises(RuntimeError, match="does not contain the bundled"):
        rt.choose_runtime("bundled", bundled=None, system_version="156.0.4314.42")


def test_system_mode_never_uses_the_bundled_copy(tmp_path):
    choice = rt.choose_runtime("system", bundled=tmp_path, system_version=None)
    assert (choice.kind, choice.path) == ("system", None)


def test_bundled_copy_on_a_network_share_is_refused():
    with pytest.raises(RuntimeError, match="network folder"):
        rt.choose_runtime("bundled", bundled=Path("\\\\server\\share\\app\\_internal\\webview2-runtime"),
                          system_version=None)


def test_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        rt.choose_runtime("fixed", bundled=tmp_path, system_version=None)


def test_select_sets_pywebview_setting_only_for_the_bundled_copy(tmp_path):
    root = bundled_root(tmp_path)
    webview = SimpleNamespace(settings={"WEBVIEW2_RUNTIME_PATH": None})
    choice = rt.select_runtime(webview, environ={rt.MODE_ENV: "bundled"}, app_root=root,
                               registry=FakeRegistry({(HKLM, MACHINE): "156.0.4314.42"}))
    assert choice.kind == "bundled"
    assert webview.settings["WEBVIEW2_RUNTIME_PATH"] == str(root / rt.BUNDLED_DIRNAME)
    assert choice.report() == {
        "kind": "bundled", "mode": "bundled", "path": str(root / rt.BUNDLED_DIRNAME),
        "system_version": "156.0.4314.42", "reason": "bundled runtime requested",
    }

    webview = SimpleNamespace(settings={"WEBVIEW2_RUNTIME_PATH": None})
    choice = rt.select_runtime(webview, environ={}, app_root=root,
                               registry=FakeRegistry({(HKLM, MACHINE): "156.0.4314.42", **NET_48}))
    assert choice.kind == "system"
    assert webview.settings["WEBVIEW2_RUNTIME_PATH"] is None


def test_default_app_root_matches_pyinstaller_root(monkeypatch, tmp_path):
    monkeypatch.setattr(rt.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert rt.default_app_root() == tmp_path


def test_missing_winreg_on_a_patched_windows_platform_reports_none(monkeypatch):
    # macOS CI patches sys.platform to win32; winreg then cannot be imported.
    monkeypatch.setattr(rt.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert rt.system_runtime_version() is None


@pytest.mark.parametrize("raw, expected", [
    (None, False), ("", False), ("auto", False), ("system", False), ("bundled", True), (" Bundled ", True),
    ("fixed", False),
])
def test_required_only_for_the_bundled_mode(raw, expected, monkeypatch):
    environ = {} if raw is None else {rt.MODE_ENV: raw}
    assert rt.required(environ) is expected
    if raw is None:
        monkeypatch.delenv(rt.MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(rt.MODE_ENV, raw)
    assert rt.required() is expected


# Rendering evidence ----------------------------------------------------------

class FakeCore:
    """CoreWebView2: records the thread every UI-thread-only member is read on."""

    def __init__(self, pid=4242, version="156.0.4314.42", udf="C:\\Temp\\udf"):
        self._pid, self._version, self._udf = pid, version, udf
        self.threads = []

    @property
    def BrowserProcessId(self):  # noqa: N802 - .NET property name
        self.threads.append(threading.current_thread())
        return self._pid

    @property
    def Environment(self):  # noqa: N802 - .NET property name
        self.threads.append(threading.current_thread())
        return SimpleNamespace(BrowserVersionString=self._version, UserDataFolder=self._udf)


def fake_native(core, requested):
    return SimpleNamespace(webview=SimpleNamespace(
        CoreWebView2=core, CreationProperties=SimpleNamespace(BrowserExecutableFolder=requested)))


def direct_call(calls=None):
    def call(window, operation, *, timeout=30):
        if calls is not None:
            calls.append((window, operation, timeout))
        return operation(window.native)
    return call


@pytest.fixture()
def runtime_files(tmp_path):
    (tmp_path / "app").mkdir()
    root = bundled_root(tmp_path / "app")
    other = tmp_path / "EdgeWebView" / "Application" / "156.0.4314.42"
    other.mkdir(parents=True)
    (other / rt.BROWSER_EXE).write_bytes(b"MZ")
    folder = root / rt.BUNDLED_DIRNAME
    return SimpleNamespace(root=root, folder=folder, exe=folder / rt.BROWSER_EXE, other=other / rt.BROWSER_EXE)


def evidence_for(files, *, requested, image, core=None, call=None, registry=None):
    window = SimpleNamespace(native=fake_native(core or FakeCore(), requested))
    return rt.window_evidence(window, call or direct_call(), image_path=lambda pid: image, app_root=files.root,
                              registry=registry or FakeRegistry({(HKLM, MACHINE): "156.0.4314.42"}))


def test_evidence_is_bundled_only_for_the_same_browser_file_and_requested_folder(runtime_files):
    files = runtime_files
    calls = []
    window = SimpleNamespace(native=fake_native(FakeCore(), str(files.folder)))
    # A different spelling of the same file and folder must still match.
    alias = str(files.folder) + os.sep + "."
    evidence = rt.window_evidence(
        window, direct_call(calls), timeout=7, image_path=lambda pid: os.path.join(alias, rt.BROWSER_EXE),
        app_root=files.root, registry=FakeRegistry({(HKLM, MACHINE): "156.0.4314.42"}))
    assert set(evidence) == set(rt.EVIDENCE_KEYS)
    assert evidence == {
        "bundled": True,
        "browser_executable": os.path.realpath(files.exe),
        "browser_version": "156.0.4314.42",
        "browser_process_id": 4242,
        "requested_folder": str(files.folder),
        "expected_folder": os.path.realpath(files.folder),
        "user_data_folder": "C:\\Temp\\udf",
        "system_version": "156.0.4314.42",
        "error": None,
    }
    # The probe goes through the caller's UI-thread dispatcher with its timeout.
    assert calls == [(window, rt.probe_webview2, 7)]
    assert json.loads(json.dumps(evidence)) == evidence


def test_evidence_from_the_system_runtime_with_the_same_version_is_not_bundled(runtime_files):
    files = runtime_files
    evidence = evidence_for(files, requested=str(files.folder), image=str(files.other))
    assert evidence["bundled"] is False
    assert evidence["browser_executable"] == os.path.realpath(files.other)
    assert evidence["browser_version"] == "156.0.4314.42" and evidence["error"] is None


def test_evidence_without_a_requested_folder_is_not_bundled(runtime_files):
    # pywebview silently uses the system runtime when it passed no folder.
    evidence = evidence_for(runtime_files, requested=None, image=str(runtime_files.exe))
    assert evidence["bundled"] is False and evidence["requested_folder"] is None


def test_evidence_without_a_bundled_copy_is_not_bundled(tmp_path):
    files = SimpleNamespace(root=tmp_path / "no-runtime")
    evidence = evidence_for(files, requested=None, image="C:\\EdgeWebView\\msedgewebview2.exe")
    assert evidence["bundled"] is False
    assert evidence["expected_folder"] is None and evidence["error"] is None


def test_evidence_captures_an_uninitialized_webview_without_raising(runtime_files):
    files = runtime_files
    window = SimpleNamespace(native=fake_native(None, str(files.folder)))
    image = Mock()
    evidence = rt.window_evidence(window, direct_call(), image_path=image, app_root=files.root,
                                  registry=FakeRegistry({}))
    assert evidence["bundled"] is False and evidence["browser_process_id"] is None
    assert "CoreWebView2 is None" in evidence["error"]
    assert evidence["expected_folder"] == os.path.realpath(files.folder)
    image.assert_not_called()


@pytest.mark.parametrize("failure", ["call", "image-raises", "image-missing", "registry", "not-a-dict"])
def test_evidence_never_raises(runtime_files, failure):
    files = runtime_files
    window = SimpleNamespace(native=fake_native(FakeCore(), str(files.folder)))
    call = direct_call()
    image_path = Mock(return_value=str(files.exe))
    registry = FakeRegistry({})
    if failure == "call":
        call = Mock(side_effect=AssertionError("native operation timed out"))
    elif failure == "image-raises":
        image_path.side_effect = OSError("access denied")
    elif failure == "image-missing":
        image_path.return_value = None
    elif failure == "registry":
        registry = Mock(HKEY_LOCAL_MACHINE=HKLM, HKEY_CURRENT_USER=HKCU, OpenKey=Mock(side_effect=RuntimeError("boom")))
    else:
        call = Mock(return_value=None)
    evidence = rt.window_evidence(window, call, image_path=image_path, app_root=files.root, registry=registry)
    assert set(evidence) == set(rt.EVIDENCE_KEYS)
    # A failed Evergreen lookup is recorded but does not void a proven bundled image.
    assert evidence["bundled"] is (failure == "registry")
    assert evidence["error"]
    json.dumps(evidence)
    if failure == "call":
        assert "native operation timed out" in evidence["error"]
    if failure == "image-missing":
        assert "pid 4242" in evidence["error"]


def test_evidence_reads_webview2_on_the_winforms_ui_thread(runtime_files, monkeypatch):
    from winforms_fake import FAKE_SYSTEM, FakeWinFormsWindow

    from nd2wsi.translucent_smoke import native_call

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", FAKE_SYSTEM)
    files = runtime_files
    window = FakeWinFormsWindow()
    try:
        core = FakeCore()
        window.native.webview = fake_native(core, str(files.folder)).webview
        evidence = rt.window_evidence(window, native_call, timeout=5, image_path=lambda pid: str(files.exe),
                                      app_root=files.root, registry=FakeRegistry({}))
        assert evidence["bundled"] is True and evidence["error"] is None
        assert core.threads and all(thread is window.native.thread for thread in core.threads)
        assert len(window.native.begin_invokes) == 1
    finally:
        window.native.exit_loop()
    assert window.native.errors == []


def test_process_image_path_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(rt.sys, "platform", "darwin")
    assert rt.process_image_path(os.getpid()) is None


@pytest.mark.skipif(sys.platform != "win32", reason="QueryFullProcessImageNameW is Windows-only")
def test_process_image_path_resolves_the_image_of_a_started_child():
    # A process this test starts, so its image is exactly the launched file even
    # when a venv python.exe is a trampoline for the base interpreter.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert os.path.samefile(rt.process_image_path(child.pid), sys.executable)
    finally:
        child.kill()
        child.wait(timeout=10)
    assert rt.process_image_path(0xFFFFFFF0) is None


# Overlay proof inside the translucent smoke ----------------------------------

@pytest.fixture()
def overlay_smoke(monkeypatch):
    """exercise_translucent_viewer up to its first DOM style read, on fakes."""
    from nd2wsi import translucent_smoke

    api = SimpleNamespace(_translucent_windows={})
    parent = SimpleNamespace(_js_api=api)
    child = SimpleNamespace(uid="overlay", evaluate_js=Mock(side_effect=[True, RuntimeError("stop after setup")]))
    overlay = SimpleNamespace(_window=child, _configured=True, _closed=False, close_translucent=Mock())
    source = {"query": "c=0", "rotation": 0, "flip": False}

    def pane(_parent, expression):
        return source if expression == "captureTranslucentView()" else True

    monkeypatch.setattr(translucent_smoke, "pane_eval", pane)
    monkeypatch.setattr(translucent_smoke, "click_shell_cloud",
                        lambda *_args, **_kwargs: api._translucent_windows.update(one=overlay))
    monkeypatch.setattr(translucent_smoke, "bridge_result", lambda *_args, **_kwargs: {"query": "c=0"})
    monkeypatch.setattr(translucent_smoke, "camera", lambda _child: {"rotation": 0, "flip": False})
    monkeypatch.setattr(translucent_smoke, "check_camera", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(translucent_smoke, "native_state", lambda *_args, **_kwargs: {
        "topmost": True, "has_parent": False, "opacity": .55, "opaque": False, "background_alpha": 0})
    evidence = Mock(return_value={"bundled": False, "error": None})
    monkeypatch.setattr(rt, "window_evidence", evidence)
    monkeypatch.delenv(rt.MODE_ENV, raising=False)
    return SimpleNamespace(run=lambda: translucent_smoke.exercise_translucent_viewer(api, parent, timeout=3),
                           evidence=evidence, child=child, overlay=overlay, native_call=translucent_smoke.native_call)


@pytest.mark.parametrize("required, bundled", [(False, False), (True, True), (True, False)])
def test_overlay_records_its_own_runtime_evidence_on_windows(overlay_smoke, monkeypatch, required, bundled):
    monkeypatch.setattr(sys, "platform", "win32")
    if required:
        monkeypatch.setenv(rt.MODE_ENV, "bundled")
    overlay_smoke.evidence.return_value = {"bundled": bundled, "error": None}
    result = overlay_smoke.run()
    overlay_smoke.evidence.assert_called_once_with(overlay_smoke.child, overlay_smoke.native_call, timeout=3)
    assert result["webview2_runtime"] == {"bundled": bundled, "error": None}
    assert result["ok"] is False
    if required and not bundled:
        assert "Overlay was not rendered by the bundled WebView2 runtime" in result["error"]
    else:
        # The proof passed; the fake stops the check at its next DOM read.
        assert "stop after setup" in result["error"]
    overlay_smoke.overlay.close_translucent.assert_called_once()


def test_overlay_runtime_evidence_is_windows_only(overlay_smoke, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv(rt.MODE_ENV, "bundled")
    result = overlay_smoke.run()
    overlay_smoke.evidence.assert_not_called()
    assert "webview2_runtime" not in result
    assert "stop after setup" in result["error"]



# pywebview 6.2.1 agreement (platforms/winforms.py _is_chromium) -------------

@pytest.mark.parametrize("values, accepted", [
    ({(HKLM, MACHINE): "156.0.4314.42", **NET_48}, True),
    ({(HKCU, USER): "156.0.4314.42", **NET_48}, True),
    ({(HKLM, MACHINE): "156.0.4314.42"}, False),  # no .NET Framework 4.x key
    ({(HKLM, MACHINE): "156.0.4314.42", (HKLM, NET): {"Release": 394801}}, False),  # below 4.6.2
    ({(HKCU, USER): "", (HKLM, MACHINE): "156.0.4314.42", **NET_48}, False),  # pywebview gives up on ''
    ({(HKCU, USER): "0.0.0.0", (HKLM, MACHINE): "156.0.4314.42", **NET_48}, True),
    ({(HKLM, MACHINE): "85.0.564.40", **NET_48}, False),
    (NET_48, False),
])
def test_pywebview_acceptance_mirrors_its_own_registry_check(values, accepted):
    assert rt.pywebview_accepts_system(FakeRegistry(values)) is accepted


def test_pywebview_acceptance_is_false_without_winreg(monkeypatch):
    monkeypatch.setattr(rt.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert rt.pywebview_accepts_system() is False
    monkeypatch.setattr(rt.sys, "platform", "darwin")
    assert rt.pywebview_accepts_system() is False


def test_auto_uses_the_bundled_copy_when_pywebview_would_reject_the_system_runtime(tmp_path):
    choice = rt.choose_runtime("auto", bundled=tmp_path, system_version="156.0.4314.42", system_usable=False)
    assert (choice.kind, choice.path, choice.system_version) == ("bundled", tmp_path, "156.0.4314.42")
    assert "pywebview would not use it" in choice.reason
    choice = rt.choose_runtime("auto", bundled=None, system_version="156.0.4314.42", system_usable=False)
    assert (choice.kind, choice.path) == ("system", None)
    choice = rt.choose_runtime("auto", bundled=tmp_path, system_version="156.0.4314.42", system_usable=True)
    assert (choice.kind, choice.path) == ("system", None)


def test_select_points_pywebview_at_the_copy_when_an_empty_user_value_hides_the_machine_runtime(tmp_path):
    root = bundled_root(tmp_path)
    webview = SimpleNamespace(settings={"WEBVIEW2_RUNTIME_PATH": None})
    registry = FakeRegistry({(HKCU, USER): "", (HKLM, MACHINE): "156.0.4314.42", **NET_48})
    choice = rt.select_runtime(webview, environ={}, app_root=root, registry=registry)
    assert choice.kind == "bundled"
    assert webview.settings["WEBVIEW2_RUNTIME_PATH"] == str(root / rt.BUNDLED_DIRNAME)


# WebView2 start-up result -----------------------------------------------------

class FakeEvent(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


def watched_window(native=True):
    window = SimpleNamespace(events=SimpleNamespace(before_show=FakeEvent()))
    if native:
        window.native = SimpleNamespace(webview=SimpleNamespace(CoreWebView2InitializationCompleted=FakeEvent()))
    return window


@pytest.mark.parametrize("success, exception, expected", [
    (True, None, (True, None)),
    (False, "0x80070002 runtime not found", (False, "0x80070002 runtime not found")),
])
def test_watch_initialization_reports_the_webview2_result(success, exception, expected):
    window, calls = watched_window(), []
    assert rt.watch_initialization(window, lambda ok, detail: calls.append((ok, detail))) is True
    for handler in window.events.before_show:
        handler()
    [completed] = window.native.webview.CoreWebView2InitializationCompleted
    completed(None, SimpleNamespace(IsSuccess=success, InitializationException=exception))
    assert calls == [expected]


def test_watch_initialization_reports_when_it_cannot_attach():
    window, calls = watched_window(native=False), []
    rt.watch_initialization(window, lambda ok, detail: calls.append((ok, detail)))
    window.events.before_show[0]()
    assert calls and calls[0][0] is False and "could not watch" in calls[0][1]


def test_watch_initialization_needs_a_before_show_event():
    assert rt.watch_initialization(SimpleNamespace(), lambda *_: None) is False
    assert rt.watch_initialization(SimpleNamespace(events=SimpleNamespace()), lambda *_: None) is False


# Windows 10 folder permission for the bundled runtime -----------------------

@pytest.mark.parametrize("build, needed", [(19045, True), (17763, True), (21999, True), (22000, False),
                                           (26100, False)])
def test_only_windows_10_builds_need_the_appcontainer_grant(build, needed):
    assert rt.needs_appcontainer_grant(build) is needed


def test_the_grant_is_not_needed_off_windows_or_without_a_windows_version(monkeypatch):
    monkeypatch.setattr(rt.sys, "platform", "darwin")
    assert rt.needs_appcontainer_grant() is False
    monkeypatch.setattr(rt.sys, "platform", "win32")
    monkeypatch.delattr(rt.sys, "getwindowsversion", raising=False)
    assert rt.needs_appcontainer_grant() is False


def _grant_harness(*, before=False, after=True, returncode=0, stdout="", stderr="", raises=None):
    states = iter([before, after])
    calls = []

    def check(folder):
        state = next(states)
        if isinstance(state, Exception):
            raise state
        return state

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if raises:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return check, run, calls


def test_windows_11_skips_the_grant_without_touching_the_folder(tmp_path):
    check, run, calls = _grant_harness()
    assert rt.ensure_appcontainer_access(tmp_path, build=26100, check=check, run=run) == "not-needed"
    assert calls == []


def test_an_existing_grant_is_left_alone(tmp_path):
    check, run, calls = _grant_harness(before=True)
    assert rt.ensure_appcontainer_access(tmp_path, build=19045, check=check, run=run) == "present"
    assert calls == []


def test_windows_10_grants_inheritable_read_execute_to_both_appcontainer_groups(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    check, run, calls = _grant_harness()
    assert rt.ensure_appcontainer_access(tmp_path, build=19045, check=check, run=run) == "granted"
    [(command, kwargs)] = calls
    # The system icacls, never one found on PATH.
    assert command == [os.path.join("C:\\Windows", "System32", "icacls.exe"), str(tmp_path), "/grant",
                       "*S-1-15-2-1:(OI)(CI)(RX)", "*S-1-15-2-2:(OI)(CI)(RX)", "/Q"]
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["capture_output"] and kwargs["timeout"] == 120


def test_the_ci_diagnostic_forces_the_grant_on_windows_11(tmp_path):
    check, run, calls = _grant_harness()
    assert rt.ensure_appcontainer_access(tmp_path, force=True, build=26100, check=check, run=run) == "granted"
    assert len(calls) == 1


@pytest.mark.parametrize("harness, expected", [
    (dict(returncode=5, stderr="Access is denied.\r\n"), "failed: icacls exit 5: Access is denied."),
    (dict(returncode=2, stdout=""), "failed: icacls exit 2"),
    (dict(raises=FileNotFoundError("icacls.exe")), "failed: icacls.exe"),
    (dict(raises=subprocess.TimeoutExpired("icacls", 120)), "failed: Command 'icacls' timed out after 120 seconds"),
    (dict(after=False), "failed: icacls succeeded but the folder still lacks the grant"),
    (dict(after=OSError("unreadable")), "granted (could not re-check: unreadable)"),
])
def test_grant_failures_are_reported_never_raised(tmp_path, harness, expected):
    check, run, _calls = _grant_harness(**harness)
    assert rt.ensure_appcontainer_access(tmp_path, build=19045, check=check, run=run) == expected


def test_an_unreadable_folder_still_attempts_the_grant(tmp_path):
    check, run, calls = _grant_harness(before=OSError("denied"))
    assert rt.ensure_appcontainer_access(tmp_path, build=19045, check=check, run=run) == "granted"
    assert len(calls) == 1


def test_appcontainer_access_is_unknown_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(rt.sys, "platform", "darwin")
    assert rt.appcontainer_access(tmp_path) is None


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows ACLs and icacls")
def test_real_grant_reaches_the_folder_and_its_children(tmp_path):
    folder = tmp_path / "webview2-runtime"
    (folder / "EBWebView" / "x64").mkdir(parents=True)
    (folder / "msedgewebview2.exe").write_bytes(b"MZ")
    icacls = os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe")
    # Start from no explicit AppContainer grant; the user's temp folder does not give one.
    subprocess.run([icacls, str(folder), "/remove", "*S-1-15-2-1", "*S-1-15-2-2", "/Q"], check=True,
                   capture_output=True)
    assert rt.appcontainer_access(folder) is False
    assert rt.ensure_appcontainer_access(folder, force=True) == "granted"
    assert rt.appcontainer_access(folder) is True
    # Inherited by the folders below, which the sandboxed WebView2 processes read.
    assert rt.appcontainer_access(folder / "EBWebView" / "x64") is True
    assert rt.ensure_appcontainer_access(folder, force=True) == "present"


@pytest.mark.parametrize("mask, readable", [
    (0x1200A9, True), (0x1F01FF, True), (0xA0000000, True),  # RX, full control, GR|GE
    (0x10000000, True), (0x80000000, False), (0x120089, False),  # GA; GR alone; read without execute
])
def test_generic_rights_are_mapped_like_windows_does_for_files(mask, readable):
    assert (rt._file_rights(mask) & rt._READ_EXECUTE == rt._READ_EXECUTE) is readable


@pytest.mark.parametrize("path, drive_type, network", [
    ("\\\\server\\share\\app\\_internal\\webview2-runtime", 3, True),
    ("Z:\\apps\\nd2wsi-viewer\\_internal\\webview2-runtime", 4, True),  # mapped network drive
    ("C:\\Users\\lab\\nd2wsi-viewer\\_internal\\webview2-runtime", 3, False),
    ("relative\\webview2-runtime", 4, False),
])
def test_network_locations_include_mapped_drives(monkeypatch, path, drive_type, network):
    monkeypatch.setattr(rt.sys, "platform", "win32")
    roots = []
    monkeypatch.setattr(rt, "_drive_type", lambda root: roots.append(root) or drive_type)
    assert rt._is_network_path(path) is network
    if path[1:2] == ":":
        assert roots == [path[:2] + "\\"]


def test_a_failed_drive_lookup_is_not_treated_as_a_network_drive(monkeypatch):
    monkeypatch.setattr(rt.sys, "platform", "win32")

    def broken(root):
        raise OSError("no kernel32")

    monkeypatch.setattr(rt, "_drive_type", broken)
    assert rt._is_network_path("C:\\app") is False


def test_the_bundled_copy_on_a_mapped_network_drive_is_refused(monkeypatch):
    monkeypatch.setattr(rt.sys, "platform", "win32")
    monkeypatch.setattr(rt, "_drive_type", lambda root: 4)
    with pytest.raises(RuntimeError, match="network folder"):
        rt.choose_runtime("auto", bundled=Path("Z:\\apps\\_internal\\webview2-runtime"), system_version=None)
