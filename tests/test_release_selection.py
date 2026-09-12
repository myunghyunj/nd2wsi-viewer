"""OS/architecture/channel selection is tested without network or Cocoa."""
from __future__ import annotations

import io
import json
from unittest.mock import Mock

import pytest

from nd2wsi import release_selection as updates


def release(tag, *names, prerelease=False, draft=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft,
            "assets": [{"name": name, "state": "uploaded",
                        "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/{name}"}
                       for name in names]}


@pytest.fixture
def index():
    return [
        release("v2.1.0rc1", "nd2wsi-viewer-2.1.0rc1-macos-arm64.dmg", prerelease=True),
        release("v2.0.0", "nd2wsi-viewer.dmg", "nd2wsi-viewer-2.0.0-windows-x64.zip"),
        release("v1.2.8", "nd2wsi-viewer.dmg", "nd2wsi-viewer-1.2.8-Setup.exe"),
    ]


@pytest.mark.parametrize("machine", ["AMD64", "x86_64", "ARM64", "aarch64"])
@pytest.mark.parametrize("channel", ["stable", "rc"])
def test_windows_never_receives_mac_only_rc(index, machine, channel):
    selected = updates.select_release_asset(index, platform_name="win32", machine=machine,
                                             current_version="1.2.8", channel=channel)
    assert selected["asset_name"] == "nd2wsi-viewer-2.0.0-windows-x64.zip"
    assert selected["platform"] == "win32"
    assert selected["channel"] == "stable"
    assert selected["emulated"] == (machine.lower() in ("arm64", "aarch64"))


def test_stable_mac_does_not_receive_rc(index):
    assert updates.select_release_asset(index, platform_name="darwin", machine="arm64",
                                        current_version="2.0.0", channel="stable") is None


def test_rc_mac_receives_matching_arm64_rc(index):
    selected = updates.select_release_asset(index, platform_name="darwin", machine="arm64",
                                             current_version="2.0.0", channel="rc")
    assert selected["asset_name"] == "nd2wsi-viewer-2.1.0rc1-macos-arm64.dmg"
    assert selected["emulated"] is False


def test_no_verified_intel_package_is_not_fake_browser_fallback(index):
    assert updates.select_release_asset(index, platform_name="darwin", machine="x86_64",
                                        current_version="1.2.8", channel="rc") is None


@pytest.mark.parametrize("platform_name,machine", [("linux", "arm64"), ("win32", "i386"),
                                                   ("darwin", "unknown"), ("win32", "")])
def test_unsupported_os_or_architecture_fails_closed(index, platform_name, machine):
    assert updates.select_release_asset(index, platform_name=platform_name, machine=machine,
                                        current_version="1.0.0") is None


def test_future_mac_only_stable_does_not_hide_latest_windows(index):
    index.insert(0, release("v3.0.0", "nd2wsi-viewer-3.0.0-macos-arm64.dmg"))
    selected = updates.select_release_asset(index, platform_name="win32", machine="AMD64",
                                             current_version="1.2.8")
    assert selected["version"] == "2.0.0"


def test_rc_can_advance_to_final_of_same_version(index):
    index.insert(0, release("v2.1.0", "nd2wsi-viewer-2.1.0-macos-arm64.dmg"))
    selected = updates.select_release_asset(index, platform_name="darwin", machine="arm64",
                                             current_version="2.1.0rc1", channel="rc")
    assert selected["version"] == "2.1.0"
    assert selected["channel"] == "stable"


@pytest.mark.parametrize("tag,prerelease", [("v2.2.0rc1", False), ("v2.2.0", True),
                                           ("v2.2.0b1", True), ("testdata", False)])
def test_inconsistent_or_unrequested_prerelease_is_ignored(tag, prerelease):
    item = release(tag, f"nd2wsi-viewer-{tag[1:]}-macos-arm64.dmg", prerelease=prerelease)
    assert updates.select_release_asset([item], platform_name="darwin", machine="arm64",
                                        current_version="2.0.0", channel="rc") is None


def test_drafts_are_never_updates(index):
    index[0]["draft"] = True
    assert updates.select_release_asset(index, platform_name="darwin", machine="arm64",
                                        current_version="2.0.0", channel="rc") is None


@pytest.mark.parametrize("url", ["http://github.com/malware.exe", "https://evil.invalid/update.dmg",
                                  "https://github.com/other/repo/releases/download/v2.1.0rc1/update.dmg",
                                  "https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.1.0rc1/not-the-asset.dmg"])
def test_unexpected_asset_url_is_rejected(index, url):
    index[0]["assets"][0]["browser_download_url"] = url
    assert updates.select_release_asset(index, platform_name="darwin", machine="arm64",
                                        current_version="2.0.0", channel="rc") is None


@pytest.mark.parametrize("name", ["nd2wsi-viewer.dmg", "nd2wsi-viewer-2.0.0-macos-arm64.dmg",
                                   "nd2wsi-viewer-2.1.0rc1-windows-arm64.dmg",
                                   "nd2wsi-viewer-2.1.0rc1-macos-arm64.zip",
                                   "nd2wsi-viewer-2.1.0rc1-macos-arm64.dmg.sha256"])
def test_rc_requires_exact_platform_version_and_extension(name):
    item = release("v2.1.0rc1", name, prerelease=True)
    assert updates.select_release_asset([item], platform_name="darwin", machine="arm64",
                                        current_version="2.0.0", channel="rc") is None


def test_native_architecture_wins_within_one_release():
    item = release("v2.1.0", "nd2wsi-viewer-2.1.0-windows-x64.exe",
                   "nd2wsi-viewer-2.1.0-windows-arm64.zip")
    selected = updates.select_release_asset([item], platform_name="win32", machine="arm64",
                                             current_version="2.0.0")
    assert selected["download_architecture"] == "arm64"
    assert selected["emulated"] is False


def test_explicit_universal_mac_matches_intel_and_arm():
    item = release("v2.1.0", "nd2wsi-viewer-2.1.0-macos-universal2.dmg")
    for machine in ("arm64", "x86_64"):
        assert updates.select_release_asset([item], platform_name="darwin", machine=machine,
                                            current_version="2.0.0")["download_architecture"] == "universal2"


@pytest.mark.parametrize("tag", ["v2.2.0", "v1.5.0", "v1.2.1"])
def test_ambiguous_legacy_mac_name_is_only_known_published_versions(tag):
    assert updates.select_release_asset([release(tag, "nd2wsi-viewer.dmg")],
                                        platform_name="darwin", machine="arm64",
                                        current_version="1.0.0") is None


def test_equal_or_older_version_is_not_an_update(index):
    assert updates.select_release_asset(index, platform_name="win32", machine="AMD64",
                                        current_version="2.0.0") is None
    assert updates.select_release_asset(index, platform_name="darwin", machine="arm64",
                                        current_version="2.1.0rc2", channel="rc") is None


@pytest.mark.parametrize("value,channel", [("2.1.0rc1", "rc"), ("v2.1.0rc2", "rc"),
                                            ("2.1.0", "stable"), ("2.1.0b3", "stable"),
                                            ("development", "stable")])
def test_channel_is_explicit_and_version_derived(value, channel):
    assert updates.channel_for_version(value) == channel


def test_check_opens_only_selected_asset_on_user_request(monkeypatch, index):
    fetch = Mock(return_value=index)
    opened = Mock(return_value=True)
    monkeypatch.setattr(updates, "fetch_releases", fetch)
    monkeypatch.setattr(updates.webbrowser, "open", opened)
    result = updates.check_for_updates(platform_name="win32", machine="AMD64",
                                       current_version="1.2.8", channel="rc")
    assert result["ok"] and result["available"] and result["opened"]
    assert result["channel"] == "stable"
    fetch.assert_called_once_with()
    opened.assert_called_once_with(result["download_url"])
    assert result["download_url"].endswith("-windows-x64.zip")


def test_metadata_only_check_never_opens_browser(monkeypatch, index):
    monkeypatch.setattr(updates, "fetch_releases", lambda: index)
    opened = Mock()
    monkeypatch.setattr(updates.webbrowser, "open", opened)
    result = updates.check_for_updates(platform_name="darwin", machine="arm64",
                                       current_version="2.0.0", channel="rc", open_download=False)
    assert result["ok"] and result["available"] and not result["opened"]
    opened.assert_not_called()


def test_fetch_failure_does_not_open_cross_platform_fallback(monkeypatch):
    fetch = Mock(side_effect=RuntimeError("untrusted error https://evil.invalid/"))
    opened = Mock()
    monkeypatch.setattr(updates, "fetch_releases", fetch)
    monkeypatch.setattr(updates.webbrowser, "open", opened)
    result = updates.check_for_updates(platform_name="darwin", machine="arm64", current_version="2.0.0")
    assert not result["ok"] and not result["available"]
    assert "evil" not in result["message"]
    opened.assert_not_called()


@pytest.mark.parametrize("platform_name,machine,current", [("linux", "arm64", "2.0.0"),
                                                           ("darwin", "unknown", "2.0.0"),
                                                           ("win32", "AMD64", "development")])
def test_invalid_local_context_makes_no_network_request(monkeypatch, platform_name, machine, current):
    fetch = Mock()
    monkeypatch.setattr(updates, "fetch_releases", fetch)
    assert not updates.check_for_updates(platform_name=platform_name, machine=machine,
                                         current_version=current)["ok"]
    fetch.assert_not_called()


class Response(io.BytesIO):
    def __init__(self, data, url=updates.RELEASES_API):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


def test_fetch_is_bounded_https_metadata_only(monkeypatch, index):
    open_url = Mock(return_value=Response(json.dumps(index).encode()))
    monkeypatch.setattr(updates.urllib.request, "urlopen", open_url)
    assert updates.fetch_releases() == index
    request = open_url.call_args.args[0]
    assert request.full_url == updates.RELEASES_API
    assert open_url.call_args.kwargs["timeout"] == 8


@pytest.mark.parametrize("data,url", [(b"x" * (updates.MAX_METADATA_BYTES + 1), updates.RELEASES_API),
                                      (b"{}", updates.RELEASES_API),
                                      (b"[]", "https://evil.invalid/releases")])
def test_fetch_rejects_large_invalid_or_redirected_index(monkeypatch, data, url):
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **kw: Response(data, url))
    with pytest.raises(ValueError):
        updates.fetch_releases()
