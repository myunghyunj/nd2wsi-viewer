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
    return api


def test_mirror_defaults_to_opposite_surface_without_changing_stage_calibration():
    store = SimpleNamespace(attrs={'nd2wsi': {'levels': [{'width': 2048, 'height': 2048}],
                                             'pixel_size_um': [.660165814209375] * 2},
                                   'omero': {'channels': [{}]}}, generation='pixels', plate=None)
    payload = {'version': 1, 'center': [1000, 1000], 'zoom': 1, 'flip': False, 'query': 'g=pixels'}
    context = validate_context(store, payload, slide_url='http://local/s/ito/')
    assert context['mirror_scan'] is True
    assert context['flip'] is False
    assert context['pixel_size_um'] == store.attrs['nd2wsi']['pixel_size_um']
    assert validate_context(store, {**payload, 'mirror_scan': False}, slide_url='http://local/s/ito/')['mirror_scan'] is False
    with pytest.raises(ValueError, match='Mirror image'):
        validate_context(store, {**payload, 'mirror_scan': 'true'}, slide_url='http://local/s/ito/')


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
