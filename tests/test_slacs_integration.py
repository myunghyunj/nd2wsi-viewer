"""The detection monitor is window-owned and closed with its viewer."""
import json
from unittest.mock import Mock

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
