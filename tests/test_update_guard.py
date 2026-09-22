"""Update admission is independent of annotation/cache storage and window role."""
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nd2wsi import app
from nd2wsi.update_guard import UpdateInstallGuard
from nd2wsi.window_sessions import create_window_session


def test_existing_windows_keep_work_until_the_user_closes_them(tmp_path):
    owner = create_window_session('user', tmp_path)
    peer = create_window_session('agent', tmp_path)
    draft = peer.drafts_root / 'work.json'
    draft.write_text('{"unmerged":true}')
    guard = UpdateInstallGuard(owner, bundle_peers=lambda: [])
    assert 'other viewer windows' in guard.acquire()
    assert not guard.gate.acquired
    assert json.loads((peer.root/'session.json').read_text())['state'] == 'starting'
    peer.mark_closed()
    assert guard.acquire() is None
    try:
        with pytest.raises(RuntimeError, match='update is being installed'):
            create_window_session('user', tmp_path)
        assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 2
    finally:
        guard.release()
    assert draft.read_text() == '{"unmerged":true}'
    assert create_window_session('user', tmp_path).id != owner.id


def test_process_death_releases_liveness_without_rewriting_recovery_record(tmp_path):
    owner = create_window_session('user', tmp_path)
    process = subprocess.Popen(
        [sys.executable, '-c',
         'import sys; from nd2wsi.window_sessions import create_window_session; '
         'session = create_window_session("user", sys.argv[1]); '
         'print(session.id, flush=True); sys.stdin.read()', str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    dead_id = process.stdout.readline().strip()
    assert len(dead_id) == 32
    record = tmp_path / dead_id / 'session.json'
    guard = UpdateInstallGuard(owner, bundle_peers=lambda: [])
    try:
        assert 'other viewer windows' in guard.acquire()
        process.terminate()
        process.wait(timeout=10)
        assert guard.acquire() is None
        assert record.exists()
        assert json.loads(record.read_text())['state'] == 'starting'
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        guard.release()


@pytest.mark.parametrize('mode', ['older-bundle', 'bad-live-record'])
def test_untracked_bundle_and_live_session_with_bad_record_fail_closed(tmp_path, mode):
    owner = create_window_session('user', tmp_path)
    if mode == 'bad-live-record':
        peer = create_window_session('user', tmp_path)
        (peer.root/'session.json').write_text('broken record')
    guard = UpdateInstallGuard(owner, bundle_peers=lambda: [123] if mode == 'older-bundle' else [])
    assert guard.acquire() is not None
    assert not guard.gate.acquired


def test_reused_pid_in_legacy_record_does_not_invent_a_live_window(tmp_path):
    owner = create_window_session('user', tmp_path)
    legacy = tmp_path / ('a' * 32)
    legacy.mkdir()
    (legacy / 'session.json').write_text(json.dumps({
        'state': 'ready', 'pid': os.getpid(),
    }))
    guard = UpdateInstallGuard(owner, bundle_peers=lambda: [])
    try:
        assert guard.acquire() is None
        assert json.loads((legacy / 'session.json').read_text())['state'] == 'ready'
    finally:
        guard.release()


def test_custom_roots_share_liveness_and_startup_admission(tmp_path, monkeypatch):
    coordination = tmp_path / 'coordination'
    monkeypatch.setattr('nd2wsi.update_guard._coordination_root', lambda _: coordination)
    owner = create_window_session('user', tmp_path / 'normal')
    peer = create_window_session('agent', tmp_path / 'diagnostics')
    guard = UpdateInstallGuard(owner, bundle_peers=lambda: [])
    assert 'other viewer windows' in guard.acquire()
    peer.mark_closed()
    assert guard.acquire() is None
    try:
        with pytest.raises(RuntimeError, match='update is being installed'):
            create_window_session('agent', tmp_path / 'new-diagnostics')
    finally:
        guard.release()
    resumed = create_window_session('agent', tmp_path / 'new-diagnostics')
    resumed.mark_closed()


def test_packaged_gate_location_is_independent_of_private_session_root(tmp_path, monkeypatch):
    from nd2wsi.update_guard import _coordination_root

    monkeypatch.setattr('sys.platform', 'darwin')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    expected = tmp_path / 'Library/Application Support/nd2wsi-viewer/window-sessions'
    assert _coordination_root(tmp_path / 'one') == expected
    assert _coordination_root(tmp_path / 'two') == expected
    assert not expected.exists()  # Resolving the root never writes into the profile.


def test_failed_session_initialization_releases_liveness(tmp_path, monkeypatch):
    owner = create_window_session('user', tmp_path)

    def failed_write(*args):
        raise OSError('simulated full disk')

    monkeypatch.setattr('nd2wsi.window_sessions._atomic_json', failed_write)
    with pytest.raises(OSError, match='simulated full disk'):
        create_window_session('agent', tmp_path)
    guard = UpdateInstallGuard(owner, bundle_peers=lambda: [])
    try:
        assert guard.acquire() is None
    finally:
        guard.release()


def test_slow_session_write_allows_other_launches_but_blocks_install(tmp_path, monkeypatch):
    from nd2wsi import window_sessions

    owner = create_window_session('user', tmp_path)
    writing = threading.Event()
    finish_write = threading.Event()
    real_write = window_sessions._atomic_json

    def delayed_write(path, payload):
        if payload.get('role') == 'agent':
            writing.set()
            assert finish_write.wait(10), 'test did not release the pending write'
        real_write(path, payload)

    monkeypatch.setattr(window_sessions, '_atomic_json', delayed_write)
    guard = UpdateInstallGuard(owner, bundle_peers=lambda: [])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(create_window_session, 'agent', tmp_path)
        try:
            assert writing.wait(5)
            # A real lifetime lock protects the starting peer even before its
            # JSON exists; neither a long sleep nor a larger timeout is needed.
            assert 'other viewer windows' in guard.acquire()
            peer = create_window_session('user', tmp_path)
            peer.mark_closed()
            assert 'other viewer windows' in guard.acquire()
        finally:
            finish_write.set()
            guard.release()
        pending.result(timeout=5).mark_closed()
    try:
        assert guard.acquire() is None
    finally:
        guard.release()
        owner.mark_closed()


@pytest.mark.parametrize('child', [False, True])
def test_user_windows_enable_sparkle_including_child_processes(tmp_path, monkeypatch, child):
    monkeypatch.setattr('sys.platform', 'darwin')
    monkeypatch.setenv('ND2WSI_WINDOW_CHILD', '1' if child else '0')
    user = create_window_session('user', tmp_path)
    agent = create_window_session('agent', tmp_path)
    assert app.Api(None, window_session=user)._updates_disabled is False
    assert app.Api(None, window_session=agent)._updates_disabled is True


def test_packaged_metal_rc_enables_scheduled_checks():
    builder = Path(__file__).resolve().parents[1]/'packaging/build_metal_rc.sh'
    assert 'SUEnableAutomaticChecks=True' in builder.read_text()
