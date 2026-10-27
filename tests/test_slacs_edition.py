"""The SLACS-module line never checks for, offers or opens public updates."""

import fnmatch
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app, edition, release_selection, session_log

pytestmark = pytest.mark.slacs_edition
ROOT = Path(__file__).resolve().parents[1]


def windows_builder():
    spec = importlib.util.spec_from_file_location("edition_build_windows", ROOT / "packaging/build_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_release(tag, asset):
    url = f"https://github.com/{release_selection.REPOSITORY}/releases/download/{tag}/{asset}"
    return {"tag_name": tag, "draft": False, "prerelease": False,
            "assets": [{"name": asset, "state": "uploaded", "browser_download_url": url}]}


def no_network(monkeypatch):
    fetch = Mock(side_effect=AssertionError("the SLACS-module build fetched public releases"))
    opened = Mock(side_effect=AssertionError("the SLACS-module build opened a download"))
    monkeypatch.setattr(release_selection, "fetch_releases", fetch)
    monkeypatch.setattr(release_selection.webbrowser, "open", opened)
    return fetch, opened


def test_this_line_ships_with_update_checks_off():
    assert edition.UPDATES_OFF is True
    assert edition.NAME == "SLACS-module"
    assert edition.release_version("2.5.4") == "2.5.4_SLACS-module"


@pytest.mark.parametrize(("platform_name", "machine"),
                         [("win32", "AMD64"), ("win32", "ARM64"), ("darwin", "arm64")])
def test_manual_check_returns_before_any_request(monkeypatch, platform_name, machine):
    fetch, opened = no_network(monkeypatch)
    result = release_selection.check_for_updates(platform_name=platform_name, machine=machine,
                                                 current_version="2.5.4")
    assert result["ok"] is False and result["available"] is False
    assert result["mode"] == "disabled" and result["edition"] == "SLACS-module"
    assert result["message"] == edition.UPDATES_OFF_MESSAGE
    fetch.assert_not_called()
    opened.assert_not_called()


def test_without_the_switch_the_next_public_release_would_be_offered(monkeypatch):
    # Shows that the switch, not a missing release, is what keeps public 2.5.5 away.
    monkeypatch.setattr(edition, "UPDATES_OFF", False)
    monkeypatch.setattr(release_selection, "fetch_releases",
                        lambda: [public_release("v2.5.5", "nd2wsi-viewer-2.5.5-windows-x64.zip")])
    result = release_selection.check_for_updates(platform_name="win32", machine="AMD64",
                                                 current_version="2.5.4", open_download=False)
    assert result["available"] is True and result["version"] == "2.5.5"


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_viewer_window_disables_the_update_button_and_check(monkeypatch, platform):
    monkeypatch.setattr(app.sys, "platform", platform)
    monkeypatch.delenv("ND2WSI_DISABLE_UPDATES", raising=False)
    fetch, opened = no_network(monkeypatch)
    api = app.Api(None)
    assert api._updates_disabled
    status = api.update_status()
    assert status["available"] is False and status["mode"] == "disabled"
    assert status["edition"] == "SLACS-module"
    assert status["message"] == edition.UPDATES_OFF_MESSAGE
    # The packaged GUI smoke accepts the page bridge only once this field is set.
    assert status["version"]
    assert api.check_for_updates() == {"ok": False, "message": edition.UPDATES_OFF_MESSAGE}
    fetch.assert_not_called()
    opened.assert_not_called()


def test_sparkle_is_never_installed(monkeypatch):
    # Sparkle is installed by a block queued on the AppKit main queue; record the queue.
    queued = []
    queue = SimpleNamespace(addOperationWithBlock_=queued.append)
    monkeypatch.setitem(sys.modules, "Foundation",
                        SimpleNamespace(NSOperationQueue=SimpleNamespace(mainQueue=lambda: queue)))
    monkeypatch.setattr(app.sys, "platform", "darwin")
    monkeypatch.setattr(app, "_UPDATER_COORDINATOR", None)
    app._install_app_updater(app.Api(None), Mock())
    assert queued == []
    # Without the switch the same call queues the Sparkle installer.
    monkeypatch.setattr(edition, "UPDATES_OFF", False)
    app._install_app_updater(app.Api(None), Mock())
    assert len(queued) == 1


def test_portable_zip_is_named_apart_from_the_public_zip():
    name = windows_builder().archive_name("2.5.4")
    assert name == "nd2wsi-viewer-2.5.4_SLACS-module-windows-x64.zip"
    # The windows workflow still uploads and verifies it ...
    workflow = (ROOT / ".github/workflows/windows.yml").read_text(encoding="utf-8")
    assert "dist/windows/nd2wsi-viewer-*-windows-x64.zip\n" in workflow.replace("\r\n", "\n")
    assert fnmatch.fnmatchcase(name, "nd2wsi-viewer-*-windows-x64.zip")
    # ... and a public installation's update check never matches it.
    assert release_selection._ASSET.fullmatch(name) is None


def test_public_installs_never_select_a_slacs_module_release(monkeypatch):
    monkeypatch.setattr(edition, "UPDATES_OFF", False)
    tag = "v" + edition.release_version("2.5.4")
    item = public_release(tag, windows_builder().archive_name("2.5.4"))
    for platform_name, machine in (("win32", "AMD64"), ("win32", "ARM64")):
        assert release_selection.select_release_asset(
            [item], platform_name=platform_name, machine=machine, current_version="2.5.3") is None


def test_session_log_records_the_edition():
    assert app._start_session_log("test") is not None
    records = [json.loads(line) for line in session_log.path().read_text(encoding="utf-8").splitlines()]
    start = next(record for record in records if record.get("kind") == "app_start")
    assert start["edition"] == "SLACS-module"
    assert start["updates_off"] is True
