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


def test_process_detection_gates_follow_and_loss_stops_receiver():
    api = make_overlay()
    api._discovery = Mock()  # Drive lifecycle deterministically without enumerating this machine.
    adapter = {'source': 'adapter'}  # no real keyboard observer in this test
    api._on_slacs_change({'available': False, 'session_id': None, 'status': 'not_running'})
    assert api.slacs_discovery_state()['available'] is False
    refused = api.start_stage_follow(adapter)
    assert not refused['ok'] and 'SLACS is not detected' in refused['message']
    assert api._stage_follow is None
    for malformed in ({'available': True, 'session_id': '', 'status': 'running'},
                      {'available': True, 'session_id': 'process-one', 'status': 'error'}):
        api._on_slacs_change(malformed)
        assert not api.start_stage_follow(adapter)['ok'] and api._stage_follow is None
    try:
        api._on_slacs_change({'available': True, 'session_id': 'process-one', 'status': 'running'})
        first = api.start_stage_follow(adapter)
        assert first['ok'] and api._stage_follow_session == 'process-one'  # bound to the detected process
        old_receiver = api._stage_follow
        follower = Mock()
        api._stage_follower = follower  # a key follower bound to the same Follow must stop too
        api._on_slacs_change({'available': False, 'session_id': None, 'status': 'error'})
        assert not old_receiver.snapshot()['enabled']
        assert api._stage_follow is None and api._stage_follower is None
        follower.stop.assert_called_once_with('slacs_lost')
        assert api.stage_follow_state()['status'] == 'paused'
        assert not api.start_stage_follow(adapter)['ok']  # lost: nothing to follow until SLACS returns
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


def test_stop_clears_the_binding_and_the_loss_message_for_every_source():
    api = make_overlay()
    api._discovery = Mock()
    try:
        assert api.start_stage_follow({'source': 'adapter'})['ok']
        api._on_slacs_change({'available': False, 'session_id': None, 'status': 'not_running'})
        assert api.stage_follow_state()['status'] == 'paused'
        api.stop_stage_follow()
        assert api._stage_follow_session is None and api._stage_follow_receiver_id is None
        assert api.stage_follow_state()['status'] == 'off'
    finally:
        api.close_translucent()


def test_overlay_logs_each_detection_change_once(monkeypatch):
    events = []
    monkeypatch.setattr(tw.session_log, 'event', lambda kind, **fields: events.append((kind, fields)))
    api = make_overlay()  # injects a running detection
    api._discovery = Mock()
    for snapshot in ({'available': True, 'session_id': 'test-process', 'status': 'running'},  # unchanged
                     {'available': False, 'session_id': None, 'status': 'error'},
                     {'available': False, 'session_id': None, 'status': 'error'},  # unchanged
                     {'available': False, 'session_id': None, 'status': 'not_running'},
                     {'available': True, 'session_id': 'process-two', 'status': 'running'}):
        api._on_slacs_change(snapshot)
    detections = [fields for kind, fields in events if kind == 'slacs_detection']
    assert detections == [
        {'window': 'translucent', 'available': True, 'session_id': 'test-process', 'status': 'running'},
        {'window': 'translucent', 'available': False, 'session_id': None, 'status': 'error'},
        {'window': 'translucent', 'available': False, 'session_id': None, 'status': 'not_running'},
        {'window': 'translucent', 'available': True, 'session_id': 'process-two', 'status': 'running'},
    ]
    api.close_translucent()


def test_overlay_logs_the_detector_state_it_starts_with(monkeypatch):
    from nd2wsi import slacs_discovery

    events = []
    monkeypatch.setattr(tw.session_log, 'event', lambda kind, **fields: events.append((kind, fields)))
    detector = Mock()
    detector.snapshot.return_value = {'available': False, 'session_id': None, 'status': 'checking'}
    factory = Mock(return_value=detector)
    monkeypatch.setattr(slacs_discovery, 'SlacsDiscovery', factory)
    api = TranslucentApi({'pixel_size_um': [.5, .5]}, logger=Mock(), on_closed=Mock())
    api._window = Mock()
    api.slacs_discovery_state()
    api.slacs_discovery_state()
    assert [fields for kind, fields in events if kind == 'slacs_detection'] == [
        {'window': 'translucent', 'available': False, 'session_id': None, 'status': 'checking'}]
    # The monitor's own records (liveness checks, exits) name the window that owns it.
    factory.assert_called_once()
    assert factory.call_args.kwargs['name'] == 'translucent'
    api.close_translucent()


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

    def __init__(self, receiver, session_id, *, step_um, step_source="manual", controls=None,
                 pixel_size_um=None, start_ok=True, order=None):
        self.receiver = receiver
        self.session_id = session_id
        self.step_um = step_um
        self.step_source = step_source
        self.controls = controls
        self.pixel_size_um = pixel_size_um
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


class FakeControlsMonitor:
    """SlacsControlsMonitor's interface; the overlay only constructs it."""

    instances = []

    def __init__(self, pid_provider):
        self.pid_provider = pid_provider
        self.calls = []
        FakeControlsMonitor.instances.append(self)

    def start(self):
        self.calls.append("start")
        return True

    def stop(self):
        self.calls.append("stop")

    def latest(self):
        return {"ok": True, "roughness_um": 10.0, "mirror_h": False, "mirror_v": False, "age_ms": 0.0,
                "busy": False, "error": None, "reads": 1}


@pytest.fixture
def fake_controls(monkeypatch):
    """Stand in for nd2wsi.slacs_controls, whose Win32 reader this module never needs."""
    FakeControlsMonitor.instances.clear()
    module = SimpleNamespace(SlacsControlsMonitor=FakeControlsMonitor)
    monkeypatch.setitem(sys.modules, "nd2wsi.slacs_controls", module)
    return module


@pytest.fixture
def keys_overlay(monkeypatch, fake_controls):
    FakeFollower.instances.clear()
    order = []
    monkeypatch.setattr(stage_follow_module, "StageFollowReceiver", lambda: FakeReceiver(order))

    def make(*, start_ok=True, platform="win32"):
        monkeypatch.setattr(tw.sys, "platform", platform)
        monkeypatch.setattr(stage_keys_module, "SlacsKeyFollower",
                            lambda receiver, session_id, **options: FakeFollower(
                                receiver, session_id, start_ok=start_ok, order=order, **options))
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
    ({"source": "keys", "step_source": "auto"}, "step source"),
    ({"source": "keys", "step_source": None}, "step source"),
    ({"source": "keys", "step_source": True}, "step source"),
    ({"source": "keys", "step_source": ["slacs"]}, "step source"),
    ({"step_source": "SLACS"}, "step source"),
    ({"source": "adapter", "step_source": ""}, "step source"),
    ({"source": "keys", "step_source": "slacs", "step_um": -1}, "between"),
])
def test_invalid_options_are_rejected_without_a_receiver(keys_overlay, options, fragment):
    api, _order = keys_overlay(platform="win32")
    result = api.start_stage_follow(options)
    assert not result["ok"] and fragment in result["message"]
    assert api._stage_follow is None and FakeFollower.instances == []
    assert FakeControlsMonitor.instances == []


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
    assert len(FakeControlsMonitor.instances) == 1
    api.stop_stage_follow()


# Roughness and Mirror read from SLACS ----------------------------------------

def test_windows_keys_read_roughness_from_slacs_by_default(keys_overlay):
    api, _order = keys_overlay(platform="win32")
    api._context["pixel_size_um"] = [0.5, 0.8]  # [Y, X], anisotropic so a swap cannot pass
    result = api.start_stage_follow()
    assert result["ok"] and result["source"] == "keys"
    follower = FakeFollower.instances[-1]
    [monitor] = FakeControlsMonitor.instances
    assert follower.step_source == "slacs" and follower.controls is monitor
    assert follower.step_um == tw.DEFAULT_STEP_UM  # the fallback while Roughness cannot be read
    assert follower.pixel_size_um == (0.8, 0.5)  # (X, Y) for the follower
    # The monitor asks the overlay's detector for the pid at each read, not once at start.
    assert monitor.pid_provider() is None
    api._discovery = Mock()
    api._discovery.slacs_pid.return_value = 4242
    assert monitor.pid_provider() == 4242
    api._on_slacs_change({"available": False, "session_id": None, "status": "not_running"})
    assert follower.stopped == "slacs_lost"
    # The follower owns the monitor's start and stop; the overlay never calls them.
    assert monitor.calls == []
    api.close_translucent()


@pytest.mark.parametrize("platform, options, expected", [
    ("win32", {"source": "keys"}, "slacs"),
    ("win32", {"source": "keys", "step_um": 2.5, "step_source": "slacs"}, "slacs"),
    ("win32", {"source": "keys", "step_source": "manual"}, "manual"),
    ("darwin", {"source": "keys"}, "manual"),
    ("darwin", {"source": "keys", "step_source": "slacs"}, "slacs"),  # as the page asks by default
])
def test_step_source_defaults_and_explicit_choices(keys_overlay, platform, options, expected):
    api, _order = keys_overlay(platform=platform)
    assert api.start_stage_follow(options)["ok"]
    follower = FakeFollower.instances[-1]
    assert follower.step_source == expected
    assert follower.step_um == options.get("step_um", tw.DEFAULT_STEP_UM)
    assert len(FakeControlsMonitor.instances) == (expected == "slacs")
    assert (follower.controls is not None) is (expected == "slacs")
    api.stop_stage_follow()


@pytest.mark.parametrize("options", [None, {"source": "adapter"}, {"source": "adapter", "step_source": "slacs"}])
def test_the_adapter_source_never_reads_slacs_controls(keys_overlay, options):
    api, _order = keys_overlay(platform="darwin")
    result = api.start_stage_follow(options)
    assert result["ok"] and result["source"] == "adapter"
    assert FakeFollower.instances == [] and FakeControlsMonitor.instances == []
    api.stop_stage_follow()


def test_follow_start_records_the_step_source(keys_overlay, monkeypatch):
    events = []
    monkeypatch.setattr(tw.session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    api, _order = keys_overlay(platform="win32")
    assert api.start_stage_follow({"source": "keys", "step_um": 2.5, "step_source": "manual"})["ok"]
    api.stop_stage_follow()
    assert api.start_stage_follow()["ok"]
    api.stop_stage_follow()
    assert [fields for kind, fields in events if kind == "follow_start"] == [
        {"source": "keys", "step_source": "manual", "step_um": 2.5, "ok": True, "slacs_session": "test-process"},
        {"source": "keys", "step_source": "slacs", "step_um": tw.DEFAULT_STEP_UM, "ok": True,
         "slacs_session": "test-process"},
    ]


def test_a_follower_that_cannot_start_records_its_step_source_and_leaves_the_monitor_to_it(keys_overlay, monkeypatch):
    events = []
    monkeypatch.setattr(tw.session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    api, order = keys_overlay(start_ok=False, platform="win32")
    result = api.start_stage_follow()
    assert result == {"ok": False, "message": "boom"}
    assert order == ["follower.stop", "receiver.close"]
    [monitor] = FakeControlsMonitor.instances
    assert FakeFollower.instances[-1].controls is monitor and monitor.calls == []
    assert [fields for kind, fields in events if kind == "follow_start"] == [
        {"source": "keys", "step_source": "slacs", "ok": False, "message": "boom"}]


@pytest.mark.parametrize("failure, error", [
    ("missing", "ModuleNotFoundError"),
    ("raises", "OSError: user32 is unavailable"),
])
def test_follow_still_starts_on_the_fallback_step_without_a_controls_monitor(keys_overlay, monkeypatch,
                                                                            failure, error):
    events = []
    monkeypatch.setattr(tw.session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    if failure == "missing":
        monkeypatch.setitem(sys.modules, "nd2wsi.slacs_controls", None)  # the import fails
    else:
        def broken(pid_provider):
            raise OSError("user32 is unavailable")

        monkeypatch.setattr(sys.modules["nd2wsi.slacs_controls"], "SlacsControlsMonitor", broken)
    api, _order = keys_overlay(platform="win32")
    result = api.start_stage_follow({"source": "keys", "step_um": 3})
    assert result["ok"]
    follower = FakeFollower.instances[-1]
    # The follower moves by the manual step and records it as a fallback.
    assert follower.step_source == "slacs" and follower.controls is None and follower.step_um == 3
    [start] = [fields for kind, fields in events if kind == "follow_start"]
    assert start["ok"] is True and start["step_source"] == "slacs"
    assert start["controls_error"].startswith(error)
    api._logger.assert_called()
    api.stop_stage_follow()


def test_slacs_pid_comes_from_the_detector_and_is_none_when_unknown():
    api = make_overlay()
    assert api._slacs_pid() is None  # no detector yet
    api._discovery = Mock()
    api._discovery.slacs_pid.return_value = 4242
    assert api._slacs_pid() == 4242
    for value in (None, 0, -1, True, 12.0, "4242"):
        api._discovery.slacs_pid.return_value = value
        assert api._slacs_pid() is None
    api._discovery.slacs_pid.side_effect = RuntimeError("detector stopped")
    assert api._slacs_pid() is None
    api._discovery.slacs_pid.side_effect = None
    api._discovery.slacs_pid.return_value = 4242
    api.close_translucent()
    assert api._slacs_pid() is None  # never read a closed overlay's SLACS


def test_slacs_pid_never_waits_for_the_overlay_lock():
    # The monitor thread asks for the pid while the overlay may hold its lock,
    # for example while a start or stop of the follower joins that thread.
    import threading

    api = make_overlay()
    api._discovery = Mock()
    api._discovery.slacs_pid.return_value = 7
    held, release, result = threading.Event(), threading.Event(), []

    def hold():
        with api._lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        reader = threading.Thread(target=lambda: result.append(api._slacs_pid()), daemon=True)
        reader.start()
        reader.join(2)
        assert result == [7]
    finally:
        release.set()
        holder.join(5)
    api.close_translucent()


def test_log_client_event_caps_and_rate_limits(monkeypatch):
    events = []
    monkeypatch.setattr(tw.session_log, "event", lambda kind, **fields: events.append((kind, fields)))
    api = make_overlay()
    events.clear()  # the injected detection is logged; only client events matter here
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
    events.clear()  # the injected detection is logged; only client events matter here
    assert api.log_client_event("oops") == {"ok": False}
    assert events == []
    api.close_translucent()
    assert api.log_client_event({"kind": "error"}) == {"ok": False}
