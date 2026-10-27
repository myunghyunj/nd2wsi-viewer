"""Motion receivers belong to one overlay and never outlive it."""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi.translucent_window import TranslucentApi, validate_context


def make_overlay():
    api = TranslucentApi({'pixel_size_um': [.660165814209375] * 2,
                          'dimensions': [2048, 2048], 'image_name': 'ITO.nd2'},
                         logger=Mock(), on_closed=Mock())
    api._window = Mock()
    api._dispatch = lambda operation: None
    api._on_slacs_change({'available': True, 'session_id': 'test-process', 'status': 'running'})
    return api


def test_detection_never_blocks_follow_and_ends_only_a_follow_bound_to_a_lost_process():
    api = make_overlay()
    api._discovery = Mock()  # Drive lifecycle deterministically without enumerating this machine.
    adapter = {'source': 'adapter'}  # no real keyboard observer in this test
    api._on_slacs_change({'available': False, 'session_id': None, 'status': 'not_running'})
    assert api.slacs_discovery_state()['available'] is False
    unbound = api.start_stage_follow(adapter)
    assert unbound['ok']  # detection never blocks Follow
    receiver = api._stage_follow
    try:
        api._on_slacs_change({'available': True, 'session_id': 'process-one', 'status': 'running'})
        api._on_slacs_change({'available': False, 'session_id': None, 'status': 'error'})
        assert api._stage_follow is receiver and receiver.snapshot()['enabled']  # never bound
        api.stop_stage_follow()
        api._on_slacs_change({'available': True, 'session_id': 'process-one', 'status': 'running'})
        first = api.start_stage_follow(adapter)
        assert first['ok']
        old_receiver = api._stage_follow
        follower = Mock()
        api._stage_follower = follower  # a key follower bound to the same Follow must stop too
        api._on_slacs_change({'available': False, 'session_id': None, 'status': 'error'})
        assert not old_receiver.snapshot()['enabled']
        assert api._stage_follow is None and api._stage_follower is None
        follower.stop.assert_called_once_with('slacs_lost')
        assert api.stage_follow_state()['status'] == 'paused'
        api._on_slacs_change({'available': True, 'session_id': 'process-two', 'status': 'running'})
        assert api._stage_follow is None  # No automatic resume, even for a new process.
        second = api.start_stage_follow(adapter)
        assert second['ok'] and second['session_id'] != first['session_id']
        current = api._stage_follow
        api._on_slacs_change({'available': True, 'session_id': 'process-three', 'status': 'running'})
        assert api._stage_follow is None and not current.snapshot()['enabled']
        assert api.stage_follow_state()['status'] == 'paused'
    finally:
        api.close_translucent()


@pytest.mark.parametrize('flip', [False, True])
def test_context_carries_the_main_viewer_orientation_and_ignores_an_old_mirror_setting(flip):
    store = SimpleNamespace(attrs={'nd2wsi': {'levels': [{'width': 2048, 'height': 2048}],
                                             'pixel_size_um': [.660165814209375] * 2},
                                   'omero': {'channels': [{}]}}, generation='pixels', plate=None)
    payload = {'version': 1, 'center': [1000, 1000], 'zoom': 1, 'rotation': 450, 'flip': flip, 'query': 'g=pixels'}
    context = validate_context(store, payload, slide_url='http://local/s/ito/')
    assert context['flip'] is flip and context['rotation'] == 90
    assert 'mirror_scan' not in context
    assert context['pixel_size_um'] == store.attrs['nd2wsi']['pixel_size_um']
    # Older code may still send its own mirror setting; it never changes the orientation.
    for mirror in (True, False, 'true', None):
        older = validate_context(store, {**payload, 'mirror_scan': mirror}, slide_url='http://local/s/ito/')
        assert older == context


def test_receivers_are_owned_isolated_and_lock_does_not_stop_observation():
    first, second = make_overlay(), make_overlay()
    try:
        a, b = first.start_stage_follow(), second.start_stage_follow()
        assert a['ok'] and b['ok']
        assert a['session_id'] != b['session_id'] and a['url'] != b['url']
        assert first.set_translucent_locked(True)['locked']
        assert first.stage_follow_state()['enabled']
        assert first.start_stage_follow()['session_id'] == a['session_id']
        first.close_translucent()
        assert not first.stage_follow_state()['enabled']
        assert not first.start_stage_follow()['ok']
        assert second.stage_follow_state()['enabled']
        assert 'token' not in second.stage_follow_state()
        second._did_close()
        assert not second.stage_follow_state()['enabled']
    finally:
        first.stop_stage_follow()
        second.stop_stage_follow()


@pytest.mark.parametrize('pixels', [None, [], [1], [0, 1], [True, 1], [float('nan'), 1]])
def test_uncalibrated_images_never_open_a_receiver(pixels):
    api = make_overlay()
    api._context['pixel_size_um'] = pixels
    assert not api.start_stage_follow()['ok']
    assert api._stage_follow is None


def test_stop_restart_changes_session_and_clears_displacement():
    api = make_overlay()
    try:
        first = api.start_stage_follow()
        api.stop_stage_follow()
        second = api.start_stage_follow()
        assert first['session_id'] != second['session_id']
        assert api.stage_follow_state()['displacement_um'] == [0, 0]
    finally:
        api.stop_stage_follow()


def test_export_is_source_bound_and_cancel_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace(SAVE_DIALOG='save'))
    api = make_overlay()
    try:
        descriptor = api.start_stage_follow()
        path = tmp_path / 'connection.json'
        api._window.create_file_dialog.return_value = [str(path)]
        assert api.save_stage_follow_connection()['ok']
        exported = json.loads(path.read_text())
        assert exported['session_id'] == descriptor['session_id']
        assert exported['token'] == descriptor['token']
        assert exported['image_name'] == 'ITO.nd2'
        assert exported['motion_source'] == 'commanded'
        assert exported['pixel_size_um_yx'] == api._context['pixel_size_um']
        if os.name != "nt":  # POSIX permission bits; Windows uses ACLs
            assert path.stat().st_mode & 0o077 == 0
        before = path.read_bytes()
        api._window.create_file_dialog.return_value = None
        assert api.save_stage_follow_connection()['cancelled']
        assert path.read_bytes() == before
    finally:
        api.stop_stage_follow()


# Key-follow source: options, lifecycle merge and client logging --------------

from nd2wsi import stage_follow as stage_follow_module  # noqa: E402
from nd2wsi import stage_keys as stage_keys_module  # noqa: E402
from nd2wsi import translucent_window as tw  # noqa: E402


class FakeReceiver:
    def __init__(self, order):
        self._order = order
        self._session = "fake-session"
        self.closed = False

    def start(self):
        return {"url": "http://127.0.0.1:9/", "token": "secret-token",
                "session_id": self._session, "protocol": stage_follow_module.PROTOCOL}

    def snapshot(self):
        return {"enabled": True, "session_id": self._session, "seq": 0,
                "displacement_um": [0, 0], "status": "waiting", "message": ""}

    def close(self):
        self.closed = True
        self._order.append("receiver.close")


class FakeFollower:
    instances = []

    def __init__(self, receiver, session_id, *, step_um, start_ok=True, order=None):
        self.receiver = receiver
        self.session_id = session_id
        self.step_um = step_um
        self._start_ok = start_ok
        self._order = order if order is not None else []
        self.stopped = None
        FakeFollower.instances.append(self)

    def start(self):
        return self._snapshot(running=self._start_ok)

    def _snapshot(self, running):
        return {"source": "keys", "running": running, "supported": True, "step_um": self.step_um,
                "counted": 3, "ignored_not_slacs": 1, "last_key": "D",
                "slacs_in_front": True, "error": None if running else "boom"}

    def snapshot(self):
        return self._snapshot(running=True)

    def stop(self, reason="stopped"):
        self.stopped = reason
        self._order.append("follower.stop")


@pytest.fixture
def keys_overlay(monkeypatch):
    FakeFollower.instances.clear()
    order = []
    monkeypatch.setattr(stage_follow_module, "StageFollowReceiver", lambda: FakeReceiver(order))

    def make(*, start_ok=True, platform="win32"):
        monkeypatch.setattr(tw.sys, "platform", platform)
        monkeypatch.setattr(stage_keys_module, "SlacsKeyFollower",
                            lambda receiver, session_id, *, step_um: FakeFollower(
                                receiver, session_id, step_um=step_um, start_ok=start_ok, order=order))
        return make_overlay(), order

    return make


def test_keys_is_the_default_source_on_windows(keys_overlay):
    api, _order = keys_overlay(platform="win32")
    result = api.start_stage_follow()
    assert result["ok"] and result["source"] == "keys"
    follower = FakeFollower.instances[-1]
    assert follower.step_um == tw.DEFAULT_STEP_UM
    state = api.stage_follow_state()
    assert state["keys"]["source"] == "keys" and state["keys"]["counted"] == 3
    assert "token" not in state
    api.stop_stage_follow()


def test_adapter_is_the_default_source_off_windows(keys_overlay):
    api, _order = keys_overlay(platform="darwin")
    result = api.start_stage_follow()
    assert result["ok"] and result["source"] == "adapter"
    assert FakeFollower.instances == []
    assert "keys" not in api.stage_follow_state()
    api.stop_stage_follow()


def test_explicit_keys_options_set_the_step(keys_overlay):
    api, _order = keys_overlay(platform="darwin")
    result = api.start_stage_follow({"source": "keys", "step_um": 2.5})
    assert result["ok"] and result["source"] == "keys"
    assert FakeFollower.instances[-1].step_um == 2.5
    api.stop_stage_follow()


@pytest.mark.parametrize("options, fragment", [
    ({"source": "sideways"}, "Unknown"),
    ({"source": "keys", "step_um": 0}, "between"),
    ({"source": "keys", "step_um": 20000}, "between"),
    ({"source": "keys", "step_um": float("nan")}, "between"),
    ({"source": "keys", "step_um": "10"}, "between"),
    ("not-a-dict", "Invalid"),
])
def test_invalid_options_are_rejected_without_a_receiver(keys_overlay, options, fragment):
    api, _order = keys_overlay(platform="win32")
    result = api.start_stage_follow(options)
    assert not result["ok"] and fragment in result["message"]
    assert api._stage_follow is None and FakeFollower.instances == []


def test_follower_that_cannot_start_closes_the_receiver(keys_overlay):
    api, order = keys_overlay(start_ok=False, platform="win32")
    result = api.start_stage_follow()
    assert not result["ok"] and result["message"] == "boom"
    assert order == ["follower.stop", "receiver.close"]
    assert api._stage_follow is None and api._stage_follower is None


def test_stop_stops_the_follower_before_the_receiver(keys_overlay):
    api, order = keys_overlay(platform="win32")
    api.start_stage_follow()
    order.clear()
    api.stop_stage_follow()
    assert order == ["follower.stop", "receiver.close"]
    assert api._stage_follow is None and api._stage_follower is None


def test_close_stops_the_follower_before_the_receiver(keys_overlay):
    api, order = keys_overlay(platform="win32")
    api.start_stage_follow()
    order.clear()
    api.close_translucent()
    assert order == ["follower.stop", "receiver.close"]


def test_restart_is_idempotent_and_reuses_the_running_follower(keys_overlay):
    api, _order = keys_overlay(platform="win32")
    first = api.start_stage_follow()
    second = api.start_stage_follow({"source": "keys", "step_um": 99})
    assert second["session_id"] == first["session_id"]
    # A second start never builds a new follower while one is running.
    assert len(FakeFollower.instances) == 1
    api.stop_stage_follow()


def test_log_client_event_caps_and_rate_limits(monkeypatch):
    events = []
    monkeypatch.setattr(tw.session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    api = make_overlay()
    result = api.log_client_event({"source": "x" * 5000, "kind": "error",
                                   "message": "boom", "where": "a.js:1", "stack": "y" * 9000,
                                   "ignored": "drop me"})
    assert result["ok"] and events[0][0] == "client_error"
    fields = events[0][1]
    assert set(fields) == {"source", "client_kind", "message", "where", "stack"}
    assert len(fields["source"]) == tw.CLIENT_LOG_FIELD_LIMIT
    assert len(fields["stack"]) == tw.CLIENT_LOG_STACK_LIMIT
    assert fields["message"] == "boom" and fields["client_kind"] == "error"
    assert "ignored" not in fields
    for _ in range(tw.CLIENT_LOG_MAX * 2):
        last = api.log_client_event({"kind": "error", "message": "again"})
    assert last.get("rate_limited") is True
    assert len(events) == tw.CLIENT_LOG_MAX


def test_log_client_event_rejects_non_dict_and_closed(monkeypatch):
    events = []
    monkeypatch.setattr(tw.session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    api = make_overlay()
    assert api.log_client_event("oops") == {"ok": False}
    assert events == []
    api.close_translucent()
    assert api.log_client_event({"kind": "error"}) == {"ok": False}
