"""Unified RC entry routing, with no GUI, user-data access or GPU allocation."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app, desktop, metal
from nd2wsi.metal_viewport import launcher


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop, "metal_available", lambda: True)
    monkeypatch.setattr(desktop, "command_prefix", lambda: ["/App/viewer"])
    for key in ("ND2WSI_APP_NAME", "ND2WSI_WINDOW_LAUNCHER", "ND2WSI_WINDOW_SESSION_ROOT",
                "ND2WSI_GPU_PYRAMID"):
        monkeypatch.setenv(key, "")
    browser, native = Mock(return_value=17), Mock(return_value=0)
    choose = Mock(side_effect=AssertionError("must not use another window's file picker"))
    monkeypatch.setattr(app, "main", browser)
    monkeypatch.setattr(launcher, "main", native)
    monkeypatch.setattr(launcher, "_choose_source", choose)
    return SimpleNamespace(browser=browser, native=native, choose=choose,
                           path=tmp_path / "fluorescence.nd2")


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_other_operating_systems_retain_original_entry(rig, monkeypatch, system):
    monkeypatch.setattr(desktop.sys, "platform", system)
    arguments = [str(rig.path), "--agent-window", "--smoke"]
    assert desktop.main(arguments) == 17
    rig.browser.assert_called_once_with(arguments)
    rig.native.assert_not_called()
    rig.choose.assert_not_called()


@pytest.mark.parametrize("smoke", ["--smoke", "--gui-smoke"])
def test_existing_package_smoke_entry_is_unchanged(rig, smoke):
    arguments = ["--agent-window", smoke, str(rig.path)]
    assert desktop.main(arguments) == 17
    rig.browser.assert_called_once_with(arguments)
    rig.native.assert_not_called()


@pytest.mark.parametrize("role_flag,role", [("--agent-window", "agent"), ("--new-window", "user")])
def test_compatible_source_uses_metal_and_fresh_role_scoped_commands(rig, role_flag, role):
    assert desktop.main([role_flag, str(rig.path)]) == 0
    arguments, = rig.native.call_args.args
    assert arguments == [str(rig.path)]
    assert rig.native.call_args.kwargs == {
        "role": role,
        "window_commands": {
            "user": ["/App/viewer", "--new-window"],
            "agent": ["/App/viewer", "--agent-window"],
            "standard": ["/App/viewer", role_flag, "--renderer", "browser", str(rig.path)],
            "updates": ["/App/viewer", "--check-updates"],
        },
    }
    rig.browser.assert_not_called()
    rig.choose.assert_not_called()


def test_default_human_launch_is_a_new_user_window(rig):
    assert desktop.main([str(rig.path)]) == 0
    assert rig.native.call_args.kwargs["role"] == "user"


@pytest.mark.parametrize("role_flag", ["--new-window", "--agent-window"])
def test_missing_metal_hardware_keeps_role_in_browser_fallback(rig, monkeypatch, role_flag):
    monkeypatch.setattr(desktop, "metal_available", lambda: False)
    assert desktop.main([role_flag, str(rig.path)]) == 17
    rig.browser.assert_called_once_with([role_flag, str(rig.path)])
    rig.native.assert_not_called()


def test_explicit_browser_path_never_probes_metal(rig, monkeypatch):
    monkeypatch.setattr(desktop, "metal_available", Mock(side_effect=AssertionError("no GPU needed")))
    assert desktop.main(["--renderer", "browser", "--agent-window", str(rig.path)]) == 17
    rig.browser.assert_called_once_with(["--agent-window", str(rig.path)])
    rig.native.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("RGB"), OSError("cache unavailable"),
                                    RuntimeError("Metal unavailable")])
def test_unsupported_source_uses_same_role_new_browser_window(rig, error):
    rig.native.side_effect = error
    assert desktop.main(["--agent-window", str(rig.path)]) == 17
    rig.browser.assert_called_once_with(["--agent-window", str(rig.path)])


def test_native_startup_code_four_falls_back_after_launcher_returns(rig):
    rig.native.return_value = 4
    assert desktop.main(["--agent-window", str(rig.path)]) == 17
    rig.browser.assert_called_once_with(["--agent-window", str(rig.path)])


@pytest.mark.parametrize("code", [1, 2, 3])
def test_invalid_native_launch_is_not_retried_as_a_new_window(rig, code):
    rig.native.return_value = code
    assert desktop.main([str(rig.path)]) == code
    rig.browser.assert_not_called()


@pytest.mark.parametrize("option", ["--source-info", "--renderer=metal"])
def test_explicit_native_diagnostics_do_not_mask_startup_failure(rig, option):
    rig.native.return_value = 4
    assert desktop.main([str(rig.path), option]) == 4
    rig.browser.assert_not_called()
    rig.native.side_effect = ValueError("unsupported source")
    with pytest.raises(ValueError, match="unsupported source"):
        desktop.main([str(rig.path), option])


def test_cancel_new_window_picker_never_creates_source_or_window(rig):
    rig.choose.side_effect = None
    rig.choose.return_value = None
    assert desktop.main(["--agent-window"]) == 0
    rig.native.assert_not_called()
    rig.browser.assert_not_called()


def test_diagnostics_and_session_root_forward_only_to_this_native_launch(rig, tmp_path):
    session_root, report = tmp_path / "sessions", tmp_path / "new-report.json"
    arguments = ["--agent-window", str(rig.path), "--session-root", str(session_root),
                 "--report", str(report), "--capture-enabled", "--source-info"]
    assert desktop.main(arguments) == 0
    assert rig.native.call_args.args == (
        [str(rig.path), "--session-root", str(session_root), "--report", str(report),
         "--capture-enabled", "--source-info"],
    )
    for command in rig.native.call_args.kwargs["window_commands"].values():
        assert "--report" not in command and "--capture-enabled" not in command
    assert desktop.os.environ["ND2WSI_WINDOW_SESSION_ROOT"] == str(session_root)


@pytest.mark.parametrize("system,machine", [("darwin", "x86_64"), ("win32", "arm64"),
                                           ("linux", "aarch64")])
def test_metal_probe_requires_apple_silicon(monkeypatch, system, machine):
    monkeypatch.setattr(desktop.sys, "platform", system)
    monkeypatch.setattr(desktop.platform, "machine", lambda: machine)
    probe = Mock(side_effect=AssertionError("unsupported OS/CPU must not load Metal"))
    monkeypatch.setattr(metal, "device_info", probe)
    assert desktop.metal_available() is False
    probe.assert_not_called()


@pytest.mark.parametrize("supported", [True, False])
def test_apple_silicon_uses_actual_runtime_metal_support(monkeypatch, supported):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(metal, "device_info", lambda: {"supported": supported})
    assert desktop.metal_available() is supported


def test_source_and_frozen_command_prefix_launch_distinct_processes(monkeypatch):
    monkeypatch.setattr(desktop.sys, "executable", "/App/viewer")
    monkeypatch.delattr(desktop.sys, "frozen", raising=False)
    assert desktop.command_prefix() == ["/App/viewer", "-m", "nd2wsi.desktop"]
    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    assert desktop.command_prefix() == ["/App/viewer"]
