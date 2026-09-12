import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from nd2wsi.window_sessions import AGENT_DIRECTIVE, create_window_session


def test_new_windows_never_share_identity_or_storage(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(lambda role: create_window_session(role, tmp_path), ["user", "agent"] * 6))
    assert len({s.id for s in sessions}) == 12
    assert len({s.root for s in sessions}) == 12
    for session in sessions:
        assert session.annotation_root.is_dir()
        assert session.exports_root.is_dir()
        assert session.drafts_root.is_dir()
        record = json.loads((session.root / "session.json").read_text())
        assert record["pid"] == os.getpid()
        assert record["role"] == session.role
        assert record["agent_directive"] == AGENT_DIRECTIVE
        assert list(session.annotation_root.iterdir()) == []


def test_context_makes_agent_boundary_explicit(tmp_path):
    session = create_window_session("agent", tmp_path)
    context = session.as_dict()
    assert context["annotation_mode"] == "isolated-snapshot"
    assert "NEW Agent window" in context["agent_directive"]
    assert "Never reuse" in context["agent_directive"]
    assert "OS mouse and keyboard focus is shared" in context["agent_directive"]
    assert context["new_window_supported"] is True


def test_close_preserves_results_and_drafts(tmp_path):
    session = create_window_session("agent", tmp_path)
    draft = session.drafts_root / "conflict.json"
    draft.write_text('{"items": []}')
    snapshot = session.annotation_root / "slide.json"
    snapshot.write_text('{"items": [{"id": "manual-original"}]}')
    session.record_endpoint("http://127.0.0.1:43123/")
    session.mark_closed()
    assert draft.read_text() == '{"items": []}'
    assert "manual-original" in snapshot.read_text()
    record = json.loads((session.root / "session.json").read_text())
    assert record["state"] == "closed"
    assert record["endpoint"] == "http://127.0.0.1:43123/"
    assert "closed_at" in record
    assert not list(session.root.glob("*.tmp"))


@pytest.mark.parametrize("role", ["Agent", "", "../../", None])
def test_invalid_role_writes_nothing(tmp_path, role):
    with pytest.raises(ValueError):
        create_window_session(role, tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_explicit_session_root_for_isolated_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path))
    session = create_window_session("user")
    assert session.root.parent == tmp_path
    assert session.as_dict()["annotation_mode"] == "shared-with-conflict-check"
    if os.name != "nt":
        assert session.root.stat().st_mode & 0o777 == 0o700
        assert (session.root / "session.json").stat().st_mode & 0o777 == 0o600
