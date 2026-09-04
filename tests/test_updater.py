from __future__ import annotations

import base64
import importlib.util
import plistlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from nd2wsi.direct import _Lifecycle
from nd2wsi.server import SlideRegistry

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "inject_sparkle.py"
spec = importlib.util.spec_from_file_location("inject_sparkle", SCRIPT)
inject_sparkle = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(inject_sparkle)

PUBLIC_KEY = base64.b64encode(bytes(range(32))).decode()


def _fake_app_and_framework(tmp_path: Path, version: str = "2.9.6"):
    app = tmp_path / "nd2wsi-viewer.app"
    plist = app / "Contents" / "Info.plist"
    plist.parent.mkdir(parents=True)
    with plist.open("wb") as handle:
        plistlib.dump({"CFBundleVersion": "1.1.1"}, handle)

    framework = tmp_path / "Sparkle.framework"
    resources = framework / "Versions" / "B" / "Resources"
    resources.mkdir(parents=True)
    with (resources / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": version}, handle)
    (framework / "Versions" / "B" / "Sparkle").write_bytes(b"fake")
    (framework / "Versions" / "Current").symlink_to("B")
    (framework / "Sparkle").symlink_to("Versions/Current/Sparkle")
    return app, plist, framework


def test_inject_preserves_symlinks_and_requires_user_approval(tmp_path):
    app, plist, framework = _fake_app_and_framework(tmp_path)
    copied = inject_sparkle.inject(
        app=app,
        framework=framework,
        feed_url="https://updates.example.invalid/appcast.xml",
        public_ed_key=PUBLIC_KEY,
    )

    assert (copied / "Sparkle").is_symlink()
    with plist.open("rb") as handle:
        info = plistlib.load(handle)
    assert info["SUEnableAutomaticChecks"] is True
    assert info["SUAutomaticallyUpdate"] is False
    assert info["SUAllowsAutomaticUpdates"] is False
    assert info["SUScheduledCheckInterval"] == 86_400
    assert info["SUPublicEDKey"] == PUBLIC_KEY


@pytest.mark.parametrize("key", ["", "not base64", base64.b64encode(b"short").decode()])
def test_inject_rejects_invalid_public_keys(tmp_path, key):
    app, _, framework = _fake_app_and_framework(tmp_path)
    with pytest.raises(ValueError, match="public Ed25519 key"):
        inject_sparkle.inject(
            app=app,
            framework=framework,
            feed_url="https://updates.example.invalid/appcast.xml",
            public_ed_key=key,
        )


def test_inject_rejects_unpinned_framework(tmp_path):
    app, _, framework = _fake_app_and_framework(tmp_path, version="2.9.5")
    with pytest.raises(ValueError, match="expected Sparkle 2.9.6"):
        inject_sparkle.inject(
            app=app,
            framework=framework,
            feed_url="https://updates.example.invalid/appcast.xml",
            public_ed_key=PUBLIC_KEY,
        )


def test_strict_relaunch_close_waits_and_closes_plate_without_timeout():
    registry = SlideRegistry()
    life = _Lifecycle()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Plate:
        def close(self, timeout=300):
            calls.append(timeout)

    state = SimpleNamespace(busy=life, plate=Plate(), root=None)
    registry.slides["slide"] = state

    def export():
        with life:
            entered.set()
            release.wait(5)

    export_thread = threading.Thread(target=export)
    export_thread.start()
    assert entered.wait(2)
    assert registry.active_export_count() == 1

    closed = threading.Event()

    def close():
        registry.close_all_for_relaunch()
        closed.set()

    close_thread = threading.Thread(target=close)
    close_thread.start()
    assert not closed.wait(0.05)
    release.set()
    assert closed.wait(2)
    export_thread.join(2)
    close_thread.join(2)
    assert calls == [None]
    assert registry.slides == {}


def test_updater_ui_and_annotation_flush_are_wired():
    shell = (ROOT / "nd2wsi" / "static" / "shell.html").read_text()
    shell_js = (ROOT / "nd2wsi" / "static" / "shell-v1.js").read_text()
    pane_js = (ROOT / "nd2wsi" / "static" / "app.js").read_text()
    app_py = (ROOT / "nd2wsi" / "app.py").read_text()

    assert 'id="update-check"' in shell
    assert "window.nd2wsiPrepareForUpdate" in shell_js
    assert 'kind === "quit-ready"' in shell_js
    assert 'event.data.nd2wsi === "prepare-quit"' in pane_js
    assert "await flushAnnotationsForUpdate()" in pane_js
    assert "handle.check_for_updates" in app_py
