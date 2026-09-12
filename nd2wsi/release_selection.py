"""Explicit, manual update checks that cannot cross OS or CPU boundaries.

No request is made at import or startup. This module reads the public GitHub
release index only when the user checks for updates; it never installs or
replaces an application. Metal capability is an in-app runtime decision, not
permission to give an Intel Mac or Windows machine an Apple-silicon DMG.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import urllib.request
import webbrowser
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import quote

REPOSITORY = "myunghyunj/nd2wsi-viewer"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
MAX_METADATA_BYTES = 2 * 1024 * 1024
_VERSION = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?", re.I)
_ASSET = re.compile(
    r"nd2wsi-viewer-(\d+\.\d+\.\d+(?:rc\d+)?)-(macos|windows)-"
    r"(arm64|x64|x86_64|universal2|universal)(\.dmg|\.zip|\.exe)"
)
_LEGACY_MAC_VERSIONS = {(1, 2, patch, 3, 0) for patch in range(2, 9)} | {(2, 0, 0, 3, 0)}


def _version_key(value: str) -> tuple[int, int, int, int, int] | None:
    if not isinstance(value, str) or not (match := _VERSION.fullmatch(value)):
        return None
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch or 0),
            {"a": 0, "b": 1, "rc": 2, None: 3}[stage.lower() if stage else None],
            int(number or 0))


def installed_version() -> str:
    try:
        return version("nd2wsi-viewer")
    except PackageNotFoundError:
        return "development"


def channel_for_version(value: str) -> str:
    parsed = _version_key(value)
    return "rc" if parsed is not None and parsed[3] == 2 else "stable"


def normalize_machine(value: str) -> str | None:
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "x64": "x86_64",
            "amd64": "x86_64"}.get(value.lower()) if isinstance(value, str) else None


def _candidate_asset(name: str, release_version, platform_name: str, machine: str):
    # These published historical DMGs were Apple-silicon builds. An ambiguous
    # bare DMG from a future release is intentionally NOT assumed compatible.
    if name == "nd2wsi-viewer.dmg":
        if (platform_name == "darwin" and machine == "arm64"
                and release_version in _LEGACY_MAC_VERSIONS):
            return (1, "arm64", False)
        return None
    if not isinstance(name, str) or not (match := _ASSET.fullmatch(name)):
        return None
    asset_version, asset_platform, asset_arch, extension = match.groups()
    if _version_key(asset_version) != release_version:
        return None
    if platform_name == "darwin":
        if asset_platform != "macos" or extension != ".dmg":
            return None
        if asset_arch in ("universal", "universal2"):
            return (2, "universal2", False)
        normalized = normalize_machine(asset_arch)
        return (3, normalized, False) if normalized == machine else None
    if platform_name == "win32":
        if asset_platform != "windows" or extension not in (".zip", ".exe"):
            return None
        normalized = normalize_machine(asset_arch)
        if normalized == machine:
            return (4 if extension == ".exe" else 3, normalized, False)
        # The existing Windows release is x64; its Windows 11 ARM64 emulation
        # path was separately verified. Never label that a native ARM build.
        if machine == "arm64" and normalized == "x86_64":
            return (2 if extension == ".exe" else 1, normalized, True)
    return None


def select_release_asset(releases, *, platform_name: str, machine: str,
                         current_version: str, channel: str = "stable") -> dict | None:
    """Select the newest strictly newer, compatible published asset.

    Stable Windows builds never opt into a macOS-only RC channel. Unknown
    architectures, versions, names, hosts, drafts and invalid metadata fail
    closed. Within one version, a native binary wins over universal/emulation.
    """
    architecture = normalize_machine(machine)
    current = _version_key(current_version)
    if (platform_name not in ("darwin", "win32") or architecture is None
            or current is None or channel not in ("stable", "rc")
            or not isinstance(releases, list)):
        return None
    effective_channel = "stable" if platform_name == "win32" else channel
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is not False:
            continue
        tag = release.get("tag_name")
        parsed = _version_key(tag)
        if parsed is None or parsed <= current:
            continue
        prerelease = release.get("prerelease")
        if not isinstance(prerelease, bool):
            continue
        stable = parsed[3] == 3 and prerelease is False
        rc = parsed[3] == 2 and prerelease is True
        if not stable and not (effective_channel == "rc" and rc):
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("state", "uploaded") != "uploaded":
                continue
            name = asset.get("name")
            compatibility = _candidate_asset(name, parsed, platform_name, architecture)
            if compatibility is None:
                continue
            expected_url = (f"https://github.com/{REPOSITORY}/releases/download/"
                            f"{quote(tag, safe='')}/{quote(name, safe='')}")
            if asset.get("browser_download_url") != expected_url:
                continue
            priority, download_arch, emulated = compatibility
            result = {"version": tag.removeprefix("v"), "tag": tag,
                      "asset_name": name, "download_url": expected_url,
                      "release_url": f"https://github.com/{REPOSITORY}/releases/tag/{quote(tag, safe='')}",
                      "platform": platform_name, "architecture": architecture,
                      "download_architecture": download_arch, "emulated": emulated,
                      "channel": "rc" if rc else "stable"}
            candidates.append((parsed, priority, name, result))
    return max(candidates, key=lambda item: item[:3])[3] if candidates else None


def fetch_releases() -> list:
    """One bounded, unauthenticated HTTPS metadata request; no asset download."""
    request = urllib.request.Request(RELEASES_API, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nd2wsi-viewer-manual-update-check",
    })
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.geturl() != RELEASES_API:
            raise ValueError("unexpected update metadata redirect")
        data = response.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("update metadata exceeded its size limit")
    releases = json.loads(data)
    if not isinstance(releases, list):
        raise ValueError("invalid release metadata")
    return releases


def check_for_updates(*, platform_name: str | None = None, machine: str | None = None,
                      current_version: str | None = None, channel: str | None = None,
                      open_download: bool = True) -> dict:
    """User-triggered manual check; optionally open only the selected asset.

    Browser/application callers can pass ``open_download=False`` to display
    the returned metadata and obtain a separate download confirmation first.
    Nothing is installed and running windows are never closed or replaced.
    """
    platform_name = sys.platform if platform_name is None else platform_name
    machine = platform.machine() if machine is None else machine
    current_version = installed_version() if current_version is None else current_version
    selected_channel = channel_for_version(current_version) if channel is None else channel
    if platform_name == "win32":
        selected_channel = "stable"
    result = {"ok": False, "available": False, "mode": "manual-download",
              "current_version": current_version, "platform": platform_name,
              "architecture": normalize_machine(machine), "channel": selected_channel}
    if platform_name not in ("darwin", "win32") or result["architecture"] is None:
        return {**result, "message": "No verified update package is available for this OS/CPU."}
    if _version_key(current_version) is None or selected_channel not in ("stable", "rc"):
        return {**result, "message": "Cannot determine this installation's update version/channel."}
    try:
        releases = fetch_releases()
        selected = select_release_asset(releases, platform_name=platform_name, machine=machine,
                                        current_version=current_version, channel=selected_channel)
    except Exception:
        # Do not include remote content or local credentials in user-facing
        # error strings. A failed check never opens a generic cross-OS page.
        return {**result, "message": "Could not check updates safely. Please try again later."}
    if selected is None:
        return {**result, "ok": True,
                "message": "No newer compatible package is published for this OS/CPU and channel."}
    result.update(selected, available=True)
    if not open_download:
        return {**result, "ok": True, "opened": False,
                "message": "A compatible update is available. Close every viewer window before installing."}
    try:
        opened = bool(webbrowser.open(selected["download_url"]))
    except Exception:
        opened = False
    return {**result, "ok": opened, "opened": opened,
            "message": ("Opened the compatible download. Close every viewer window before installing."
                        if opened else "A compatible update is available, but the download could not be opened.")}
