"""RC2 defaults and fallback matrix: no GUI, research data or GPU allocation."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app, desktop, metal
from nd2wsi.metal_viewport import launcher
from nd2wsi.renderer_policy import FailureLedger, source_fingerprint


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop, "metal_available", Mock(return_value=True))
    monkeypatch.setattr(desktop, "command_prefix", lambda: ["/App/viewer"])
    monkeypatch.setenv("ND2WSI_RENDERER_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("ND2WSI_GPU_PYRAMID", "0")
    monkeypatch.setattr(desktop, "__version__", "2.1.0rc2")
    browser = Mock(return_value=17)
    native = Mock(return_value=launcher.NativeLaunchResult(0))
    spawn = Mock(return_value=SimpleNamespace(pid=123))
    choose = Mock(side_effect=AssertionError("must not touch another window's picker"))
    monkeypatch.setattr(app, "main", browser)
    monkeypatch.setattr(desktop, "spawn_browser", spawn)
    monkeypatch.setattr(launcher, "main", native)
    monkeypatch.setattr(launcher, "_choose_source", choose)
    path = tmp_path / "fluorescence.nd2"
    path.write_bytes(b"isolated fake slide")
    return SimpleNamespace(browser=browser, native=native, choose=choose, spawn=spawn, path=path)


def option(arguments, name):
    return arguments[arguments.index(name) + 1]


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_other_operating_systems_retain_original_entry(rig, monkeypatch, system):
    monkeypatch.setattr(desktop.sys, "platform", system)
    args = [str(rig.path), "--agent-window", "--smoke"]
    assert desktop.main(args) == 17
    rig.browser.assert_called_once_with(args)
    rig.native.assert_not_called()


@pytest.mark.parametrize("smoke", ["--smoke", "--gui-smoke"])
def test_existing_package_smoke_entry_is_unchanged(rig, smoke):
    args = ["--agent-window", smoke, str(rig.path)]
    assert desktop.main(args) == 17
    rig.browser.assert_called_once_with(args)
    rig.native.assert_not_called()


@pytest.mark.parametrize("flag", ["--new-window", "--agent-window"])
def test_unmeasured_gate_defaults_to_browser_without_gpu_probe(rig, flag):
    assert desktop.main([flag, str(rig.path)]) == 17
    rig.native.assert_not_called()
    desktop.metal_available.assert_not_called()
    args = rig.browser.call_args.args[0]
    assert args[0] == flag and args[-1] == str(rig.path)
    assert option(args, "--renderer") == "browser"
    assert len(option(args, "--open-attempt-id")) == 32


@pytest.mark.parametrize("flag,role", [("--new-window", "user"), ("--agent-window", "agent")])
def test_product_opt_in_opens_fresh_role_scoped_native_window(rig, flag, role):
    assert desktop.main([flag, "--prefer-metal", str(rig.path)]) == 0
    assert rig.native.call_args.args == ([str(rig.path)],)
    context = rig.native.call_args.kwargs
    assert context["role"] == role and context["prefer_metal"]
    assert context["return_result"] and not context["fallback_consumed"]
    assert context["window_commands"]["user"] == ["/App/viewer", "--new-window"]
    assert context["window_commands"]["agent"] == ["/App/viewer", "--agent-window"]
    assert option(context["window_commands"]["standard"], "--renderer") == "browser"
    rig.browser.assert_not_called()
    rig.spawn.assert_not_called()


@pytest.mark.parametrize("flag", ["--new-window", "--agent-window"])
def test_missing_hardware_keeps_role_without_constructing_native(rig, monkeypatch, flag):
    monkeypatch.setattr(desktop, "metal_available", Mock(return_value=False))
    assert desktop.main([flag, "--prefer-metal", str(rig.path)]) == 17
    assert rig.browser.call_args.args[0][0] == flag
    rig.native.assert_not_called()


def test_browser_override_and_consumed_open_never_reenter_metal(rig):
    for flags in (["--renderer", "browser", "--prefer-metal"],
                  ["--fallback-consumed", "--prefer-metal"]):
        assert desktop.main([*flags, str(rig.path)]) == 17
    desktop.metal_available.assert_not_called()
    rig.native.assert_not_called()


@pytest.mark.parametrize("failure", ["metadata_timeout", "metadata_invalid", "gpu_execution_failure",
                                     "tile_unavailable", "memory_pressure"])
@pytest.mark.parametrize("flag", ["--new-window", "--agent-window"])
def test_fatal_opens_one_same_role_process_after_native_returns(rig, flag, failure):
    calls = []

    def finished(*args, **kwargs):
        calls.append("native-cleaned")
        return launcher.NativeLaunchResult(5, "fatal", failure)

    def spawned(arguments):
        assert calls == ["native-cleaned"]

    rig.native.side_effect = finished
    rig.spawn.side_effect = spawned
    assert desktop.main([flag, "--prefer-metal", str(rig.path)]) == 0
    args = rig.spawn.call_args.args[0]
    assert args[0] == flag and args[-1] == str(rig.path)
    assert "--fallback-consumed" in args and option(args, "--renderer") == "browser"
    assert option(args, "--open-attempt-id") == rig.native.call_args.kwargs["open_attempt_id"]
    rig.browser.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("unsupported"), OSError("I/O unavailable"),
                                    PermissionError("Agent cache missing")])
def test_startup_source_failure_is_same_role_and_never_persistent(rig, error):
    rig.native.side_effect = error
    assert desktop.main(["--agent-window", "--prefer-metal", str(rig.path)]) == 0
    assert rig.spawn.call_args.args[0][0] == "--agent-window"
    assert not FailureLedger().kinds(source_fingerprint(rig.path), "2.1.0rc2")


def test_cleanup_failure_never_starts_another_reader(rig):
    rig.native.side_effect = launcher.NativeCleanupError("could not close")
    assert desktop.main(["--prefer-metal", str(rig.path)]) == 5
    rig.spawn.assert_not_called()


def test_user_close_is_not_a_fallback(rig):
    assert desktop.main(["--prefer-metal", str(rig.path)]) == 0
    rig.spawn.assert_not_called()


def test_same_logical_attempt_cannot_spawn_twice(rig):
    rig.native.return_value = launcher.NativeLaunchResult(5, "fatal", "metadata_timeout")
    args = ["--prefer-metal", "--open-attempt-id", "a" * 32, str(rig.path)]
    assert desktop.main(args) == 0
    assert desktop.main(args) == 5
    assert rig.spawn.call_count == 1


def test_persistent_failure_suppression_cannot_bypass_same_attempt_guard(rig):
    rig.native.return_value = launcher.NativeLaunchResult(5, "fatal", "gpu_execution_failure")
    args = ["--prefer-metal", "--open-attempt-id", "b" * 32, str(rig.path)]
    assert desktop.main(args) == 0
    assert desktop.main(args) == 5
    assert rig.spawn.call_count == 1 and rig.native.call_count == 1
    rig.browser.assert_not_called()
    # A genuinely new request may open the standard viewer using the ledger.
    assert desktop.main(["--prefer-metal", str(rig.path)]) == 17
    assert rig.browser.call_count == 1


def test_failed_browser_spawn_does_not_retry(rig):
    rig.native.return_value = launcher.NativeLaunchResult(5, "fatal", "metadata_timeout")
    rig.spawn.side_effect = OSError("failed executable")
    assert desktop.main(["--prefer-metal", str(rig.path)]) == 5
    assert rig.spawn.call_count == 1
    rig.browser.assert_not_called()


def test_failure_skips_next_opt_in_but_explicit_retry_success_clears(rig):
    ledger, key = FailureLedger(), source_fingerprint(rig.path)
    ledger.record(key, "2.1.0rc2", "gpu_execution_failure")
    assert desktop.main(["--prefer-metal", str(rig.path)]) == 17
    rig.native.assert_not_called()
    rig.native.return_value = launcher.NativeLaunchResult(0, "closed", first_presented=True)
    assert desktop.main(["--retry-metal", str(rig.path)]) == 0
    assert not ledger.kinds(key, "2.1.0rc2")


@pytest.mark.parametrize("change", ["version", "file"])
def test_new_version_or_source_does_not_inherit_failure(rig, monkeypatch, change):
    FailureLedger().record(source_fingerprint(rig.path), "2.1.0rc2", "gpu_execution_failure")
    if change == "version":
        monkeypatch.setattr(desktop, "__version__", "2.1.0rc3")
    else:
        rig.path.write_bytes(b"changed isolated input")
    assert desktop.main(["--prefer-metal", str(rig.path)]) == 0
    rig.native.assert_called_once()


def test_strict_diagnostics_does_not_fallback(rig):
    rig.native.return_value = launcher.NativeLaunchResult(5, "fatal", "gpu_execution_failure")
    assert desktop.main(["--renderer=metal", str(rig.path)]) == 5
    rig.spawn.assert_not_called()
    rig.native.side_effect = ValueError("unsupported")
    with pytest.raises(ValueError, match="unsupported"):
        desktop.main(["--renderer=metal", str(rig.path)])


def test_strict_hardware_failure_is_not_silent_browser(rig, monkeypatch):
    monkeypatch.setattr(desktop, "metal_available", lambda: False)
    with pytest.raises(RuntimeError, match="supported Apple silicon"):
        desktop.main(["--renderer=metal", str(rig.path)])
    rig.browser.assert_not_called()


def test_fault_injection_is_agent_only_and_not_persisted(rig):
    with pytest.raises(SystemExit):
        desktop.main(["--prefer-metal", "--diagnostic-fault", "gpu_fatal", str(rig.path)])
    rig.native.return_value = launcher.NativeLaunchResult(5, "fatal", "gpu_execution_failure")
    assert desktop.main(["--agent-window", "--prefer-metal", "--diagnostic-fault", "gpu_fatal",
                         str(rig.path)]) == 0
    assert rig.native.call_args.kwargs["diagnostics"]["fault"] == "gpu_fatal"
    assert not FailureLedger().kinds(source_fingerprint(rig.path), "2.1.0rc2")


def test_cancel_own_picker_creates_no_source_or_session(rig):
    rig.choose.side_effect, rig.choose.return_value = None, None
    assert desktop.main(["--agent-window", "--prefer-metal"]) == 0
    rig.native.assert_not_called()
    rig.browser.assert_not_called()


def test_source_info_is_headless_and_forwards_its_diagnostics(rig, tmp_path):
    report = tmp_path / "report.json"
    rig.native.return_value = 0
    assert desktop.main([str(rig.path), "--agent-window", "--source-info", "--report", str(report)]) == 0
    args = rig.native.call_args.args[0]
    assert option(args, "--report") == str(report) and "--source-info" in args
    rig.spawn.assert_not_called()


def test_browser_benchmark_arguments_reach_default_and_fallback(rig, tmp_path):
    diagnostics = ["--browser-replay-report", str(tmp_path / "replay.json"),
                   "--benchmark-context", str(tmp_path / "context.json"),
                   "--benchmark-test-root", str(tmp_path)]
    assert desktop.main(["--agent-window", *diagnostics, str(rig.path)]) == 17
    for flag in diagnostics[::2]:
        assert option(rig.browser.call_args.args[0], flag) == option(diagnostics, flag)
    rig.native.return_value = launcher.NativeLaunchResult(5, "fatal", "metadata_timeout")
    assert desktop.main(["--agent-window", "--prefer-metal", *diagnostics, str(rig.path)]) == 0
    for flag in diagnostics[::2]:
        assert option(rig.spawn.call_args.args[0], flag) == option(diagnostics, flag)


@pytest.mark.skipif(os.name != "posix", reason="macOS handoff uses POSIX ownership/modes")
def test_manual_native_handoff_preserves_role_attempt_and_display_only(rig, tmp_path):
    from nd2wsi.view_state import read_handoff

    state = {"version": 1, "source_dimensions": [1000, 800], "center": [234, 456], "zoom": .25,
             "channels": [{"window": [321, 54321], "gamma": 1.5,
                           "color": [10, 23, 250], "visible": False}]}
    report = tmp_path / "session" / "exports" / "report.json"
    rig.native.return_value = launcher.NativeLaunchResult(0, "handoff", first_presented=True,
                                                         view_state=state, report_path=report)
    assert desktop.main(["--agent-window", "--prefer-metal", str(rig.path)]) == 0
    args = rig.spawn.call_args.args[0]
    assert args[0] == "--agent-window"
    assert read_handoff(option(args, "--handoff-state"), source=rig.path, role="agent") == state
    assert option(args, "--open-attempt-id") == rig.native.call_args.kwargs["open_attempt_id"]
    with pytest.raises(ValueError, match="role"):
        read_handoff(option(args, "--handoff-state"), source=rig.path, role="user")


@pytest.mark.skipif(os.name != "posix", reason="macOS handoff uses POSIX ownership/modes")
def test_incoming_handoff_state_is_validated_before_native_and_retained_on_startup_failure(rig, tmp_path):
    from nd2wsi.view_state import read_handoff, write_handoff

    state = {"version": 1, "source_dimensions": [100, 80], "center": [23, 45], "zoom": 2,
             "channels": [{"window": [100, 60000], "gamma": 2, "color": [255, 0, 0], "visible": True}]}
    handoff = write_handoff(state, source=rig.path, role="agent", root=tmp_path)
    rig.native.return_value = launcher.NativeLaunchResult(5, "fatal", "metadata_timeout")
    assert desktop.main(["--agent-window", "--prefer-metal", "--handoff-state", str(handoff),
                         str(rig.path)]) == 0
    assert rig.native.call_args.kwargs["initial_view_state"] == state
    args = rig.spawn.call_args.args[0]
    assert read_handoff(option(args, "--handoff-state"), source=rig.path, role="agent") == state


def test_browser_child_environment_does_not_inherit_native_replay_faults(monkeypatch):
    monkeypatch.setattr(desktop, "command_prefix", lambda: ["/App/viewer"])
    for key in ("ND2WSI_VIEWPORT_REPLAY", "ND2WSI_VIEWPORT_AUTOQUIT",
                "ND2WSI_VIEWPORT_CAPTURE_AFTER_REPLAY", "MTL_CAPTURE_ENABLED"):
        monkeypatch.setenv(key, "1")
    popen = Mock()
    monkeypatch.setattr(desktop.subprocess, "Popen", popen)
    desktop.spawn_browser(["--agent-window", "--renderer", "browser"])
    args, = popen.call_args.args
    assert args == ["/App/viewer", "--agent-window", "--renderer", "browser"]
    environment = popen.call_args.kwargs["env"]
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert environment["ND2WSI_WINDOW_CHILD"] == "1"
    assert "ND2WSI_VIEWPORT_REPLAY" not in environment and "MTL_CAPTURE_ENABLED" not in environment


@pytest.mark.parametrize("system,machine", [("darwin", "x86_64"), ("win32", "arm64"), ("linux", "aarch64")])
def test_metal_probe_requires_apple_silicon(monkeypatch, system, machine):
    monkeypatch.setattr(desktop.sys, "platform", system)
    monkeypatch.setattr(desktop.platform, "machine", lambda: machine)
    monkeypatch.setattr(metal, "device_info", Mock(side_effect=AssertionError("unsupported")))
    assert desktop.metal_available() is False


@pytest.mark.parametrize("supported", [True, False])
def test_apple_silicon_uses_actual_runtime_support(monkeypatch, supported):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(metal, "device_info", lambda: {"supported": supported})
    assert desktop.metal_available() is supported


def test_command_prefix_source_and_frozen(monkeypatch):
    monkeypatch.setattr(desktop.sys, "executable", "/App/viewer")
    monkeypatch.delattr(desktop.sys, "frozen", raising=False)
    assert desktop.command_prefix() == ["/App/viewer", "-m", "nd2wsi.desktop"]
    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    assert desktop.command_prefix() == ["/App/viewer"]
