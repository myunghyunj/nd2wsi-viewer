"""Launcher lifecycle tests without opening any native windows or source data."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nd2wsi.metal_viewport import launcher, source
from nd2wsi.window_sessions import create_window_session


@pytest.fixture
def native_platform(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.platform, "machine", lambda: "arm64")


@pytest.fixture
def rig(tmp_path, monkeypatch, native_platform):
    sessions = []
    calls = []
    server = SimpleNamespace(
        source=SimpleNamespace(
            metadata=lambda: {"source": "specimen.nd2", "dtype": "uint16"},
            metrics=lambda: {"raw_tile_reads": 3, "cpu_jpeg_operations": 0},
            preservation_report=lambda: {"unchanged": True},
        )
    )

    def close():
        calls.append("close")

    server.close = close

    def create(role, base_path=None):
        session = create_window_session(role, base_path or tmp_path / "sessions")
        sessions.append(session)
        return session

    def open_source(path, session, *, allow_user=False):
        calls.append(("open", path, session.id, session.role, allow_user))
        return server, "http://127.0.0.1:12345/private-capability/"

    def run(base, context, report):
        calls.append(("run", base, json.loads(context), report))
        Path(report.decode()).write_text('{"native":true}')
        return 0

    library = SimpleNamespace(nd2wsi_viewport_run=run)
    monkeypatch.setattr(launcher, "create_window_session", create)
    monkeypatch.setattr(launcher, "load_native", lambda: library)
    monkeypatch.setattr(source, "create_source_server", open_source)
    monkeypatch.setattr(
        launcher, "_choose_source", lambda: pytest.fail("must not touch a user file panel")
    )
    return SimpleNamespace(
        sessions=sessions,
        calls=calls,
        server=server,
        library=library,
        path=tmp_path / "specimen.nd2",
    )


def _record(session):
    return json.loads((session.root / "session.json").read_text())


@pytest.mark.parametrize(
    "system,machine", [("win32", "AMD64"), ("linux", "aarch64"), ("darwin", "x86_64")]
)
def test_wrong_platform_rejected_before_native_or_session(tmp_path, monkeypatch, system, machine):
    monkeypatch.setattr(launcher.sys, "platform", system)
    monkeypatch.setattr(launcher.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        launcher, "load_native", lambda: pytest.fail("must not load native on fallback platform")
    )
    monkeypatch.setattr(
        launcher,
        "create_window_session",
        lambda *args, **kwargs: pytest.fail("must not create session"),
    )
    with pytest.raises(SystemExit) as exc:
        launcher.main([str(tmp_path / "specimen.nd2")])
    assert exc.value.code == 2


def test_native_launch_creates_fresh_agent_and_preserves_evidence(rig):
    assert launcher.main([str(rig.path)]) == 0
    assert launcher.main([str(rig.path), "--agent-window"]) == 0
    first, second = rig.sessions
    assert first.role == second.role == "agent" and first.id != second.id
    assert rig.calls.count("close") == 2
    for session in rig.sessions:
        assert _record(session)["state"] == "closed"
        report = session.exports_root / "viewport-report.json"
        companion = report.with_suffix(".source.json")
        assert json.loads(report.read_text()) == {"native": True}
        evidence = json.loads(companion.read_text())
        assert evidence["preservation"]["unchanged"] is True
        assert evidence["session"]["id"] == session.id
        assert evidence["source"]["cpu_jpeg_operations"] == 0
    run = next(call for call in rig.calls if isinstance(call, tuple) and call[0] == "run")
    assert run[2]["role"] == "agent"
    assert "NEW Agent window" in run[2]["agent_directive"]


def test_explicit_user_launch_keeps_identity_and_read_only_source_policy(rig):
    commands = {"agent": ["/app/viewer", "--agent-window"]}
    assert launcher.main([str(rig.path)], role="user", window_commands=commands) == 0
    session, = rig.sessions
    assert session.role == "user" and _record(session)["state"] == "closed"
    opened = next(call for call in rig.calls if isinstance(call, tuple) and call[0] == "open")
    assert opened[3:] == ("user", True)
    run = next(call for call in rig.calls if isinstance(call, tuple) and call[0] == "run")
    assert run[2]["role"] == "user"
    assert run[2]["annotation_mode"] == "read-only-snapshot"
    assert run[2]["app_name"] == "nd2wsi-viewer"
    assert run[2]["bundle_id"] == "com.nd2wsi.viewer"
    assert run[2]["window_commands"] == commands


def test_source_info_does_not_load_native_or_touch_gui(rig, monkeypatch, capsys):
    monkeypatch.setattr(
        launcher, "load_native", lambda: pytest.fail("source-info must be headless")
    )
    assert launcher.main([str(rig.path), "--source-info"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "specimen.nd2"
    assert rig.calls[-1] == "close"
    assert _record(rig.sessions[0])["state"] == "closed"


def test_cancel_own_panel_opens_no_source_or_session(rig, monkeypatch):
    monkeypatch.setattr(launcher, "_choose_source", lambda: None)
    assert launcher.main([]) == 0
    assert not rig.sessions and not rig.calls


def test_existing_report_is_not_overwritten_or_opened(rig, tmp_path):
    report = tmp_path / "existing.json"
    report.write_bytes(b"preserve measurement")
    with pytest.raises(SystemExit):
        launcher.main([str(rig.path), "--report", str(report)])
    assert report.read_bytes() == b"preserve measurement"
    assert not rig.calls
    assert all(_record(session)["state"] == "closed" for session in rig.sessions)


def test_existing_companion_is_rejected_before_source_or_native(rig, tmp_path):
    report = tmp_path / "new.json"
    companion = report.with_suffix(".source.json")
    companion.write_bytes(b"preserve old raw-source evidence")
    with pytest.raises(SystemExit):
        launcher.main([str(rig.path), "--report", str(report)])
    assert companion.read_bytes() == b"preserve old raw-source evidence"
    assert not report.exists() and not rig.calls
    assert all(_record(session)["state"] == "closed" for session in rig.sessions)


def test_source_open_error_still_closes_session(rig, monkeypatch):
    def fail(*args):
        raise ValueError("unsupported test source")

    monkeypatch.setattr(source, "create_source_server", fail)
    with pytest.raises(ValueError, match="unsupported test source"):
        launcher.main([str(rig.path)])
    assert _record(rig.sessions[0])["state"] == "closed"


def test_native_exception_still_drains_source_and_saves_preservation(rig):
    def fail(*args):
        raise RuntimeError("simulated native failure")

    rig.library.nd2wsi_viewport_run = fail
    with pytest.raises(RuntimeError, match="simulated native failure"):
        launcher.main([str(rig.path)])
    assert rig.calls[-1] == "close"
    session = rig.sessions[0]
    assert _record(session)["state"] == "closed"
    evidence = session.exports_root / "viewport-report.source.json"
    assert json.loads(evidence.read_text())["preservation"]["unchanged"] is True


def test_bad_report_parent_still_closes_created_session(rig, tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.write_bytes(b"original file")
    with pytest.raises(OSError):
        launcher.main([str(rig.path), "--report", str(parent / "report.json")])
    assert parent.read_bytes() == b"original file"
    assert not rig.calls
    assert all(_record(session)["state"] == "closed" for session in rig.sessions)


def test_source_close_failure_cannot_leave_session_starting(rig):
    def fail_close():
        raise RuntimeError("simulated close failure")

    rig.server.close = fail_close
    with pytest.raises(RuntimeError, match="simulated close failure"):
        launcher.main([str(rig.path)])
    assert _record(rig.sessions[0])["state"] == "closed"
