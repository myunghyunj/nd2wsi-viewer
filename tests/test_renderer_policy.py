"""Local state contains anonymous keys only and no cache/slide mutations."""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from nd2wsi.renderer_policy import (
    PERFORMANCE_GATE,
    PERSISTENT_FAILURE_KINDS,
    TRANSIENT_FAILURE_KINDS,
    FailureLedger,
    open_attempt_id,
    should_try_metal,
    source_fingerprint,
)


@pytest.mark.parametrize("role", ["user", "agent"])
def test_default_gate_is_explicitly_unverified_and_cannot_enable_from_environment(role, monkeypatch):
    monkeypatch.setenv("ND2WSI_METAL_AUTO", "1")
    assert PERFORMANCE_GATE["status"] == "unverified"
    assert PERFORMANCE_GATE["agent_auto_metal"] is False
    assert PERFORMANCE_GATE["validated_hardware"] == []
    assert not should_try_metal(role=role)
    assert should_try_metal(role=role, prefer_metal=True)
    assert should_try_metal(role=role, retry_metal=True)
    assert should_try_metal(role=role, renderer="metal")
    assert not should_try_metal(role=role, renderer="browser", prefer_metal=True)
    assert not should_try_metal(role=role, renderer="metal", fallback_consumed=True)


@pytest.mark.parametrize("kind", sorted(TRANSIENT_FAILURE_KINDS) + ["arbitrary /research/path error", None])
def test_transient_unknown_and_private_messages_never_create_failure_records(tmp_path, kind):
    ledger = FailureLedger(tmp_path / "local-state")
    assert ledger.record("anonymous-key", "2.1.0rc2", kind) is False
    assert not ledger.path.exists()


def test_failure_key_uses_full_package_version_and_canonical_kind(tmp_path):
    ledger = FailureLedger(tmp_path)
    for kind in PERSISTENT_FAILURE_KINDS:
        assert ledger.record("fingerprint", "2.1.0rc2", kind)
    assert ledger.kinds("fingerprint", "2.1.0rc2") == PERSISTENT_FAILURE_KINDS
    assert not ledger.kinds("fingerprint", "2.1.0rc1")
    assert not ledger.kinds("fingerprint", "2.1.0")
    assert not ledger.kinds("changed-fingerprint", "2.1.0rc2")
    ledger.clear("fingerprint", "2.1.0rc2")
    assert not ledger.kinds("fingerprint", "2.1.0rc2")
    if os.name == "posix":
        assert ledger.path.stat().st_mode & 0o777 == 0o600


def test_concurrent_windows_have_atomic_writes_and_one_fallback_claim(tmp_path):
    attempt = open_attempt_id()

    def write(index):
        ledger = FailureLedger(tmp_path)
        ledger.record(f"fingerprint-{index}", "2.1.0rc2", "gpu_execution_failure")
        return ledger.claim_fallback(attempt)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write, range(16)))
    assert sum(results) == 1
    ledger = FailureLedger(tmp_path)
    for index in range(16):
        assert ledger.kinds(f"fingerprint-{index}", "2.1.0rc2") == {"gpu_execution_failure"}
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_sampled_fingerprint_is_path_anonymous_and_changes_with_stat(tmp_path, monkeypatch):
    from nd2wsi import cache

    path = tmp_path / "private-patient-name.nd2"
    path.write_bytes(b"isolated")
    calls = []
    original = cache.quick_fingerprint

    def sampled(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(cache, "quick_fingerprint", sampled)
    key = source_fingerprint(path)
    assert len(key) == 64 and len(calls) == 1
    ledger = FailureLedger(tmp_path / "state")
    ledger.record(key, "2.1.0rc2", "gpu_execution_failure")
    assert path.name.encode() not in ledger.path.read_bytes()
    assert str(path.parent).encode() not in ledger.path.read_bytes()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000))
    assert source_fingerprint(path) != key
    assert source_fingerprint(tmp_path) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow private ledger contract")
def test_ledger_rejects_symlink_and_keeps_target_unchanged(tmp_path):
    target = tmp_path / "research.txt"
    target.write_text("preserve")
    ledger = FailureLedger(tmp_path / "state")
    ledger.root.mkdir()
    ledger.path.symlink_to(target)
    with pytest.raises(OSError, match="symlink"):
        ledger.record("key", "2.1.0rc2", "gpu_execution_failure")
    assert target.read_text() == "preserve"


def test_attempt_identifier_has_no_path_or_role_payload():
    assert len(open_attempt_id()) == 32
    with pytest.raises(ValueError, match="UUID"):
        open_attempt_id("../../user")


def test_read_only_lookup_preserves_broken_legacy_without_quarantine(tmp_path):
    from nd2wsi.convert import existing_cache_store

    source = tmp_path / "isolated.nd2"
    source.write_bytes(b"not read for a missing modern cache")
    legacy = tmp_path / "isolated.ome.zarr"
    legacy.mkdir()
    original = legacy / "keep-manual-note.txt"
    original.write_text("preserve incomplete cache")
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert existing_cache_store(source, read_only=True) is None
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert before == after and original.read_text() == "preserve incomplete cache"


def test_agent_lookup_is_read_only_before_shared_conversion(tmp_path, monkeypatch):
    from nd2wsi import convert, plate
    from nd2wsi.server import SlideRegistry
    from nd2wsi.window_sessions import create_window_session

    source = tmp_path / "isolated.nd2"
    source.write_bytes(b"isolated")
    legacy = tmp_path / "isolated.ome.zarr"
    legacy.mkdir()
    (legacy / ".zattrs").write_text(json.dumps({"malformed": True}))
    session = create_window_session("agent", tmp_path / "sessions")
    registry = SlideRegistry(window_session=session)
    monkeypatch.setattr(plate, "is_plate_file", lambda _: False)
    monkeypatch.setattr(convert, "ensure_cache", lambda *a, **k: pytest.fail("must not build or repair"))
    with pytest.raises(PermissionError, match="cannot build or repair"):
        registry.open_path(source)
    assert legacy.is_dir() and (legacy / ".zattrs").read_text() == '{"malformed": true}'
