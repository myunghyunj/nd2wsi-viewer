"""The detection monitor is window-owned and closed with its viewer."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import stage_keys
from nd2wsi import translucent_window as tw
from nd2wsi.app import Api
from nd2wsi.translucent_window import TranslucentApi


def test_main_detector_starts_lazily_and_stops_with_window(monkeypatch):
    from nd2wsi import slacs_discovery

    detector = Mock()
    detector.snapshot.return_value = {'available': True, 'session_id': 'process-one', 'status': 'running'}
    factory = Mock(return_value=detector)
    monkeypatch.setattr(slacs_discovery, 'SlacsDiscovery', factory)
    api = Api(None)
    factory.assert_not_called()
    assert api.slacs_discovery_state()['session_id'] == 'process-one'
    assert api.slacs_discovery_state()['available'] is True
    factory.assert_called_once()
    # The monitor's own records (liveness checks, exits) name the window that owns it.
    assert factory.call_args.kwargs == {'name': 'main'}
    api._close_translucent_windows()
    detector.stop.assert_called_once()
    assert api.slacs_discovery_state()['available'] is False
    factory.assert_called_once()


def test_main_window_logs_each_detection_change_once(monkeypatch):
    from nd2wsi import app as app_module
    from nd2wsi import slacs_discovery

    events = []
    monkeypatch.setattr(app_module.session_log, 'event', lambda kind, **fields: events.append((kind, fields)))
    detector = Mock()
    monkeypatch.setattr(slacs_discovery, 'SlacsDiscovery', Mock(return_value=detector))
    api = Api(None)
    states = [{'available': False, 'session_id': None, 'status': 'checking'},
              {'available': False, 'session_id': None, 'status': 'checking'},
              {'available': False, 'session_id': None, 'status': 'error'},
              {'available': True, 'session_id': 'process-one', 'status': 'running'},
              {'available': True, 'session_id': 'process-one', 'status': 'running'},
              {'available': True, 'session_id': 'process-two', 'status': 'running'}]
    for state in states:
        detector.snapshot.return_value = dict(state)
        api.slacs_discovery_state()
    detections = [fields for kind, fields in events if kind == 'slacs_detection']
    assert detections == [{'window': 'main', **state} for state in (states[0], states[2], states[3], states[5])]
    api._close_translucent_windows()


def test_overlay_monitor_reports_session_loss_independent_of_frontend_polling(monkeypatch):
    from nd2wsi import slacs_discovery

    detector = Mock()
    detector.snapshot.return_value = {'available': False, 'session_id': None, 'status': 'checking'}
    factory = Mock(return_value=detector)
    monkeypatch.setattr(slacs_discovery, 'SlacsDiscovery', factory)
    api = TranslucentApi({'pixel_size_um': [0.5, 0.5]}, logger=Mock(), on_closed=Mock())
    api._window = Mock()
    assert api.slacs_discovery_state()['available'] is False
    callback = factory.call_args.kwargs['on_change']
    assert factory.call_args.kwargs['name'] == 'translucent'
    callback({'available': True, 'session_id': 'process-one', 'status': 'running'})
    receiver = Mock()
    api._stage_follow = receiver
    api._stage_follow_session = 'process-one'
    # No bridge/UI poll occurs between detection and loss.
    callback({'available': False, 'session_id': None, 'status': 'error'})
    receiver.close.assert_called_once()
    assert api.stage_follow_state()['status'] == 'paused'
    assert api._stage_follow is None
    api.close_translucent()
    detector.stop.assert_called_once()


def _overlay_with_detector(monkeypatch, snapshot):
    from nd2wsi import slacs_discovery

    detector = Mock()
    detector.snapshot.return_value = snapshot
    factory = Mock(return_value=detector)
    monkeypatch.setattr(slacs_discovery, 'SlacsDiscovery', factory)
    api = TranslucentApi({'pixel_size_um': [0.5, 0.5]}, logger=Mock(), on_closed=Mock())
    api._window = Mock()
    api.slacs_discovery_state()
    return api, factory.call_args.kwargs['on_change']


def _join_revocations():
    import threading

    for thread in threading.enumerate():
        if thread.name == 'nd2wsi-stage-follow-revoke':
            thread.join(5)


def test_loss_revokes_follow_in_the_page_and_closes_the_receiver(monkeypatch):
    running = {'available': True, 'session_id': 'process-one', 'status': 'running'}
    api, callback = _overlay_with_detector(monkeypatch, running)
    started = api.start_stage_follow()
    try:
        assert started['ok'] is True and api._stage_follow_receiver_id == started['session_id']
        order = []
        receiver = api._stage_follow
        real_close = receiver.close
        receiver.close = lambda: (order.append('close'), real_close())
        api._window.evaluate_js.side_effect = lambda script: order.append(('page', script))
        callback({'available': False, 'session_id': None, 'status': 'not_running'})
        _join_revocations()
    finally:
        api.close_translucent()
    scripts = [item[1] for item in order if item != 'close']
    assert len(scripts) == 1 and scripts[0].startswith('window.nd2TranslucentRevokeFollow?.(')
    revocation = json.loads(scripts[0][len('window.nd2TranslucentRevokeFollow?.('):-len('); true')])
    assert revocation == {'session_id': started['session_id'],
                          'message': 'SLACS program stopped or changed. Follow stopped; reconnect manually.'}
    assert order.count('close') == 1
    assert api._stage_follow is None and api._stage_follow_receiver_id is None


def test_unchanged_session_or_idle_follow_never_pushes_a_revocation(monkeypatch):
    running = {'available': True, 'session_id': 'process-one', 'status': 'running'}
    api, callback = _overlay_with_detector(monkeypatch, running)
    callback({'available': False, 'session_id': None, 'status': 'error'})  # Follow is off
    callback(running)
    receiver = Mock()
    api._stage_follow, api._stage_follow_session, api._stage_follow_receiver_id = receiver, 'process-one', 'r-1'
    callback(dict(running))  # same process: keep following
    _join_revocations()
    api._window.evaluate_js.assert_not_called()
    receiver.close.assert_not_called()
    api.close_translucent()


def test_revocation_is_queued_before_the_receiver_closes(monkeypatch):
    running = {'available': True, 'session_id': 'process-one', 'status': 'running'}
    api, callback = _overlay_with_detector(monkeypatch, running)
    order, receiver = [], Mock()
    receiver.close.side_effect = lambda: order.append('close')
    api._stage_follow, api._stage_follow_session, api._stage_follow_receiver_id = receiver, 'process-one', 'r-1'
    monkeypatch.setattr(api, '_revoke_follow_in_page', lambda receiver_id, message: order.append(('revoke', receiver_id)))
    callback({'available': True, 'session_id': 'process-two', 'status': 'running'})  # replaced
    assert order == [('revoke', 'r-1'), 'close']
    api.close_translucent()


def test_revocation_that_runs_after_the_overlay_closed_is_skipped(monkeypatch):
    running = {'available': True, 'session_id': 'process-one', 'status': 'running'}
    api, _ = _overlay_with_detector(monkeypatch, running)
    window = api._window
    api.close_translucent()
    api._push_revocation(window, 'window.nd2TranslucentRevokeFollow?.({}); true')
    window.evaluate_js.assert_not_called()


class _ControlsMonitor:
    """SlacsControlsMonitor's interface, reporting the pid it is given at each read."""

    instances = []

    def __init__(self, pid_provider):
        self.pid_provider = pid_provider
        _ControlsMonitor.instances.append(self)

    def start(self):
        return True

    def stop(self):
        pass

    def latest(self):
        return {'ok': False, 'roughness_um': None, 'mirror_h': None, 'mirror_v': None, 'age_ms': None,
                'busy': False, 'error': None, 'reads': 0}


def test_a_key_follow_reads_the_controls_of_the_slacs_process_the_overlay_detected(monkeypatch):
    _ControlsMonitor.instances.clear()
    monkeypatch.setitem(sys.modules, 'nd2wsi.slacs_controls', SimpleNamespace(SlacsControlsMonitor=_ControlsMonitor))
    followers = []

    def follower(receiver, session_id, **options):
        made = Mock(**{'start.return_value': {'running': True}, 'snapshot.return_value': {'running': True}})
        followers.append((session_id, options, made))
        return made

    monkeypatch.setattr(stage_keys, 'SlacsKeyFollower', follower)
    running = {'available': True, 'session_id': 'process-one', 'status': 'running'}
    api, callback = _overlay_with_detector(monkeypatch, running)
    detector = api._discovery
    detector.slacs_pid.return_value = 5150
    monkeypatch.setattr(tw.sys, 'platform', 'win32')
    try:
        started = api.start_stage_follow({'source': 'keys', 'step_um': 10, 'step_source': 'slacs'})
        assert started['ok'] is True
        [(session_id, options, made)] = followers
        [monitor] = _ControlsMonitor.instances
        assert session_id == started['session_id']
        assert options == {'step_um': 10.0, 'step_source': 'slacs', 'controls': monitor, 'pixel_size_um': (0.5, 0.5),
                           'flip_x': False, 'flip_y': False, 'mark_context': api._mark_context, 'on_mark': api._on_mark_record}
        assert monitor.pid_provider() == 5150
        detector.slacs_pid.return_value = None  # the detector no longer reports that process
        assert monitor.pid_provider() is None
        callback({'available': False, 'session_id': None, 'status': 'not_running'})
        _join_revocations()
        made.stop.assert_called_once_with('slacs_lost')  # and with it, its controls monitor
    finally:
        api.close_translucent()
    assert monitor.pid_provider() is None


def test_the_overlay_options_fit_the_real_follower_and_controls_monitor(monkeypatch):
    # The Roughness reader and its follower support.
    slacs_controls = pytest.importorskip('nd2wsi.slacs_controls')
    real_monitor, real_follower = slacs_controls.SlacsControlsMonitor, stage_keys.SlacsKeyFollower
    monitors, follower_options = [], []

    # Spies that build the real classes: the overlay hides a monitor that fails
    # to build (Follow then moves by the manual step), so only these can tell.
    def monitor(*args, **kwargs):
        made = real_monitor(*args, **kwargs)
        monitors.append((args, kwargs, made))
        return made

    def follower(*args, **kwargs):
        follower_options.append(kwargs)
        return real_follower(*args, **kwargs)

    monkeypatch.setattr(slacs_controls, 'SlacsControlsMonitor', monitor)
    monkeypatch.setattr(stage_keys, 'SlacsKeyFollower', follower)
    running = {'available': True, 'session_id': 'process-one', 'status': 'running'}
    api, _ = _overlay_with_detector(monkeypatch, running)
    # Off Windows neither reads anything: no Raw Input and no window messages in a test.
    monkeypatch.setattr(tw.sys, 'platform', 'darwin')
    try:
        result = api.start_stage_follow({'source': 'keys', 'step_um': 4, 'step_source': 'slacs'})
    finally:
        api.close_translucent()
    # The real monitor accepts the overlay's pid source and is what the follower gets.
    [(args, kwargs, made)] = monitors
    assert args == (api._slacs_pid,) and kwargs == {}
    [options] = follower_options
    assert options['controls'] is made
    # Every option the overlay passes is accepted; the real follower then refuses off Windows.
    assert result['ok'] is False and 'Windows' in result['message']
    assert api._stage_follow is None and api._stage_follower is None


class _ManualObserver:
    def __init__(self):
        self.on_event = None

    def start(self, on_event):
        self.on_event = on_event
        return True

    def stop(self):
        self.stopped = True  # keeps delivering, so a stopped follower's own guard is what is tested

    def send(self, vk, down=True, modifiers=()):
        self.on_event(vk, down, modifiers)


def test_slacs_marks_carry_the_overlay_view_while_follow_runs(monkeypatch):
    """A real overlay and key follower: "," and "." are logged with the image centre, never followed."""
    from nd2wsi import session_log

    events = []
    monkeypatch.setattr(session_log, 'event', lambda kind, **fields: events.append((kind, fields)))
    observer, real_follower = _ManualObserver(), stage_keys.SlacsKeyFollower

    def follower(receiver, session_id, **options):
        return real_follower(receiver, session_id, observer=observer, foreground_provider=lambda: {'pid': 7, 'created': 1},
                             identifier=lambda pid, created: True, heartbeat_interval=None, **options)

    monkeypatch.setattr(stage_keys, 'SlacsKeyFollower', follower)
    running = {'available': True, 'session_id': 'process-one', 'status': 'running'}
    api, _ = _overlay_with_detector(monkeypatch, running)
    api._context.update(dimensions=[2048, 2048], image_name='ITO.nd2', source_format='nd2')
    api._source_format = 'nd2'
    marks = lambda: [fields for kind, fields in events if kind == 'slacs_key']  # noqa: E731
    try:
        started = api.start_stage_follow({'source': 'keys', 'step_um': 10, 'step_source': 'manual'})
        assert started['ok'] is True
        receiver = api._stage_follow
        observer.send(0xBC)
        observer.send(0xBC, down=False)
        [before] = marks()
        assert before['image_px'] is None and before['view'] is None  # no view reported yet
        assert before['slacs_session'] == 'process-one' and before['follow_status'] == 'waiting'
        assert before['source_format'] == 'nd2' and before['image_name'] == 'ITO.nd2'
        # Unlocked: the overlay is aligned by hand, and the press is still logged.
        assert before['locked'] is False and before['counted'] == 0 and before['step_source'] == 'manual'
        assert api.report_overlay_view({'version': 1, 'center_px': [100, 200], 'zoom': 2, 'rotation': 0,
                                        'flip': False, 'locked': False, 'follow_session': started['session_id'],
                                        'follow_seq': 0, 'follow_displacement_um': [0, 0]})['ok']
        observer.send(0xBE)
        observer.send(0xBE, down=False)
        after = marks()[-1]
        assert (after['key'], after['action']) == ('.', 'next_well')
        assert after['image_px'] == [100.0, 200.0] and after['view_follow_seq'] == 0
        assert after['flip_x'] is False and after['flip_y'] is False
        # Neither key moved or counted anything; W/A/S/D still do.
        assert receiver.snapshot()['seq'] == 0 and receiver.snapshot()['displacement_um'] == [0, 0]
        api.set_stage_follow_flips({'flip_x': False, 'flip_y': True})
        observer.send(0x57)
        assert receiver.snapshot()['displacement_um'] == [0.0, 10.0]  # W, reversed by Flip up/down
        observer.send(0xBC)
        last = marks()[-1]
        assert last['key'] == ',' and last['image_px'] == [100.0, 200.0]
        assert last['follow_seq'] == 1 and last['counted'] == 1 and last['flip_y'] is True
        assert last['view_follow_seq'] == 0  # the view does not show that press yet
        # A refused report forgets the view: the next record shows no centre, not the old one.
        assert api.report_overlay_view({'version': 1, 'center_px': [float('nan'), 0], 'zoom': 1})['ok'] is False
        observer.send(0xBC, down=False)
        observer.send(0xBE)
        assert marks()[-1]['image_px'] is None and marks()[-1]['view'] is None
        api.stop_stage_follow()
        count = len(marks())
        observer.send(0xBC)  # a stopped follower logs nothing
        assert len(marks()) == count == 4
        assert [kind for kind, _ in events].count('follow_key') == 1
    finally:
        api.close_translucent()


def test_follower_option_contract_is_also_checked_without_a_loopback_bind(monkeypatch):
    from nd2wsi import stage_follow

    receiver = Mock()
    receiver.start.return_value = {"session_id": "receiver-test"}
    receiver.snapshot.return_value = {"status": "waiting", "seq": 0}
    monkeypatch.setattr(stage_follow, "StageFollowReceiver", lambda: receiver)
    test_a_key_follow_reads_the_controls_of_the_slacs_process_the_overlay_detected(monkeypatch)
