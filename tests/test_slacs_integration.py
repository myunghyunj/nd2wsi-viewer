"""The detection monitor is window-owned and closed with its viewer."""
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
