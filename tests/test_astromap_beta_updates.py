"""Local beta cannot open the stable updater or release download flow."""
from unittest.mock import Mock

import pytest

from nd2wsi import app


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_local_beta_disables_automatic_and_manual_updates(monkeypatch, platform):
    monkeypatch.setattr(app.sys, "platform", platform)
    monkeypatch.setenv("ND2WSI_DISABLE_UPDATES", "1")
    release = Mock(side_effect=AssertionError("beta opened stable downloads"))
    monkeypatch.setattr("nd2wsi.release_selection.check_for_updates", release)
    api = app.Api(None)
    assert api._updates_disabled
    assert api.update_status()["available"] is False
    assert api.update_status()["mode"] == "disabled"
    assert api.check_for_updates()["ok"] is False
    release.assert_not_called()


def test_ordinary_user_window_retains_update_policy(monkeypatch):
    monkeypatch.setattr(app.sys, "platform", "darwin")
    monkeypatch.delenv("ND2WSI_DISABLE_UPDATES", raising=False)
    api = app.Api(None)
    assert not api._updates_disabled
    assert api.update_status()["mode"] == "sparkle"
