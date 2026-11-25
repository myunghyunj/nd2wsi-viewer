"""Regression tests for SLACS-module audit remediations (Domains 5 & 6)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app, session_log, stage_follow
from nd2wsi.translucent_window import TranslucentApi

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _minimal_pe(machine: int, body: bytes) -> bytes:
    header = bytearray(0x80)
    header[0:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x80)
    pe = b"PE\0\0" + struct.pack("<H", machine) + b"\0" * 18
    return bytes(header) + pe + body


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# D6-F1: Laser delivery catch-up in same lock epoch vs unlock/relock rejection
# ---------------------------------------------------------------------------


def test_laser_catchup_in_same_lock_epoch_succeeds_while_relock_realign_rejects():
    ctx = {"query": "?t=0&p=0&z=0", "pixel_size_um": [0.5, 0.5], "slide_url": "http://127.0.0.1:9/s/1/"}
    overlay = TranslucentApi(ctx, logger=Mock(), on_closed=Mock())
    overlay._stage_follow = Mock()
    overlay._stage_follow_receiver_id = "receiver-1"
    overlay._locked = True
    overlay._epoch = 3

    base_view = {
        "version": 1,
        "center_px": [100, 100],
        "zoom": 1,
        "locked": True,
        "follow_session": "receiver-1",
        "follow_seq": 7,
        "target_px": [100, 100],
        "target_offset_css": [0, 0],
        "target_side_um": 18,
        "target_rotation_deg": 0,
        "target_calibrated": True,
        "target_corners_px": [[90, 90], [110, 90], [110, 110], [90, 110]],
    }
    assert overlay.report_overlay_view(base_view)["ok"]

    # Enqueue a laser mark record at seq=8 while overlay is still at seq=7 (same lock epoch).
    enqueued: list[dict] = []
    overlay._laser_delivery = SimpleNamespace(enqueue=enqueued.append, view_changed=lambda: None)
    overlay._on_mark_record({"receiver_id": "receiver-1", "press": 1, "follow_seq": 8, "locked": True})
    assert len(enqueued) == 1
    record = enqueued[0]
    assert record["lock_epoch"] == 3
    assert record["prior_view_seq"] == 7

    # Before the view catches up, _prepare_laser_box reports stale_view.
    assert overlay._prepare_laser_box(record) == (None, "stale_view")

    # View catches up to seq=8 AFTER observed_at in the SAME lock epoch -> succeeds!
    time.sleep(0.005)
    assert overlay.report_overlay_view({**base_view, "follow_seq": 8})["ok"]
    assert overlay._overlay_view["at"] > record["observed_at"]
    payload, reason = overlay._prepare_laser_box(record)
    assert reason is None and payload is not None
    assert payload["press"] == 1

    # Now unlock -> hand-realign -> relock (epoch advances 3 -> 4 -> 5) with same follow_seq=8.
    overlay._epoch = 5
    assert overlay.report_overlay_view({**base_view, "follow_seq": 8, "center_px": [200, 200]})["ok"]
    assert overlay._prepare_laser_box(record) == (None, "view_after_press")


def test_translucent_js_reports_immediately_when_locked_on_seq_change():
    js = (ROOT / "nd2wsi/static/translucent-v1.js").read_text(encoding="utf-8")
    assert "if (seqChanged && report)" in js
    assert "if (state.locked) reportView();" in js
    assert "else scheduleViewReport();" in js


# ---------------------------------------------------------------------------
# D6-F2: Capability URL redaction without /k/, IPv6 [::1], redact-before-truncate
# ---------------------------------------------------------------------------


def test_session_log_redacts_urls_without_k_segment_and_ipv6_loopback():
    raw = (
        "failed http://127.0.0.1:43210/secret-token/s/1/tile "
        "and http://[::1]:54321/k/abc123/s/2/info "
        "and http://localhost:8080/bare"
    )
    redacted = session_log.redact_capability_urls(raw)
    assert "secret-token" not in redacted
    assert "abc123" not in redacted
    assert "127.0.0.1:43210" not in redacted
    assert "[::1]:54321" not in redacted
    assert "localhost:8080" not in redacted
    assert "s/1/tile" in redacted
    assert "s/2/info" in redacted

    # Explicit capability base URL (with or without trailing slash) is stripped cleanly.
    explicit = session_log.redact_capability_urls(
        "origin=http://127.0.0.1:9999/k/tok123/path",
        "http://127.0.0.1:9999/k/tok123/",
    )
    assert "tok123" not in explicit


def test_log_client_event_redacts_before_truncation_and_checks_releasing_httpd(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(session_log, "event", lambda kind, **fields: captured.append((kind, fields)))

    api = app.Api(None)
    api._httpd = None
    api._releasing_httpd = SimpleNamespace(server_address=("127.0.0.1", 51234), token="topsecret")
    limit = app._CLIENT_EVENT_LIMITS["message"]
    # Place capability URL right across the truncation boundary so naive truncate-first would leak a prefix.
    msg = ("x" * (limit - 25)) + "http://127.0.0.1:51234/topsecret/s/1/api/info"
    assert api.log_client_event({"source": "pane", "kind": "error", "message": msg}) == {"ok": True}
    assert len(captured) == 1
    logged_msg = captured[0][1]["message"]
    assert "topsecret" not in logged_msg
    assert "127.0.0.1:51234" not in logged_msg
    assert len(logged_msg) <= limit


# ---------------------------------------------------------------------------
# D6-F3: Lock returns stage_follow baseline and JS absorbs pre-lock jogs
# ---------------------------------------------------------------------------


def test_set_translucent_locked_returns_stage_follow_baseline_when_active():
    overlay = TranslucentApi({}, logger=Mock(), on_closed=Mock())
    overlay._dispatch = lambda fn: fn()
    fake_state = {
        "enabled": True,
        "session_id": "rec-1",
        "status": "following",
        "seq": 4,
        "displacement_um": [10.0, -5.0],
    }
    overlay._stage_follow = Mock()
    overlay.stage_follow_state = Mock(return_value=fake_state)

    locked_res = overlay.set_translucent_locked(True)
    assert locked_res["ok"] is True
    assert locked_res["locked"] is True
    assert locked_res["stage_follow"] == fake_state

    unlocked_res = overlay.set_translucent_locked(False)
    assert unlocked_res == {"ok": True, "locked": False}

    js = (ROOT / "nd2wsi/static/translucent-v1.js").read_text(encoding="utf-8")
    assert "consumeStageState(result.stage_follow, false, { report: false })" in js


# ---------------------------------------------------------------------------
# D6-F4: refresh_context re-checks _closed after stop_stage_follow
# ---------------------------------------------------------------------------


def test_refresh_context_aborts_if_closed_during_stop_stage_follow():
    overlay = TranslucentApi({"query": "?g=1", "slide_url": "http://127.0.0.1:1/s/1/"}, logger=Mock(), on_closed=Mock())
    overlay._configured = True
    overlay._window = Mock()

    def close_mid_stop():
        with overlay._lock:
            overlay._closed = True

    overlay.stop_stage_follow = close_mid_stop
    with pytest.raises(ValueError, match="still opening or closing"):
        overlay._refresh_context({"query": "?g=2", "slide_url": "http://127.0.0.1:1/s/1/"})
    overlay._window.load_url.assert_not_called()
    overlay._window.show.assert_not_called()


# ---------------------------------------------------------------------------
# D6-F5: stage_follow uses selectors.DefaultSelector (not FD_SETSIZE select)
# ---------------------------------------------------------------------------


def test_stage_follow_uses_selectors_instead_of_select():
    source = (ROOT / "nd2wsi/stage_follow.py").read_text(encoding="utf-8")
    assert "import selectors" in source
    assert "selectors.DefaultSelector" in source
    assert "select.select(" not in source
    assert hasattr(stage_follow, "_CancellableReader")


# ---------------------------------------------------------------------------
# D6-F6: open_translucent_viewer releases claim only when overlay didn't take ownership
# ---------------------------------------------------------------------------


def test_open_translucent_viewer_guards_claim_ownership_release():
    source = (ROOT / "nd2wsi/app.py").read_text(encoding="utf-8")
    assert "overlay_owns_claim = False" in source
    assert "if claim is not None and not overlay_owns_claim:" in source


# ---------------------------------------------------------------------------
# D5-F4 & D5-F12: SLACS Windows installer isolation, README, and verifier
# ---------------------------------------------------------------------------


def test_slacs_installer_uses_isolated_defines_and_slacs_readme(tmp_path, monkeypatch):
    for var in (
        "WINDOWS_SIGN_PFX_PATH",
        "WINDOWS_SIGN_PFX_PASSWORD",
        "WINDOWS_SIGN_THUMBPRINT",
        "WINDOWS_SIGNTOOL",
        "WINDOWS_REQUIRE_SIGNING",
    ):
        monkeypatch.delenv(var, raising=False)
    builder = _load_module("build_windows_installer_slacs", ROOT / "packaging/build_windows_installer.py")

    exe_bytes = _minimal_pe(0x8664, b"nd2wsi-slacs-exe")
    wv2_exe = _minimal_pe(0x8664, b"msedgewebview2")
    build_info_in = {
        "app": "nd2wsi-viewer",
        "version": "2.6.7",
        "edition": "SLACS-module",
        "release_version": "2.6.7_SLACS-module",
        "updates_off": True,
        "binary_architecture": "x64",
        "executable_sha256": _sha256_bytes(exe_bytes),
    }
    archive = tmp_path / "nd2wsi-viewer-2.6.7_SLACS-module-windows-x64.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("nd2wsi-viewer/nd2wsi-viewer.exe", exe_bytes)
        zf.writestr("nd2wsi-viewer/READ-ME.txt", b"portable\n")
        zf.writestr("nd2wsi-viewer/_internal/nd2wsi-viewer-about/build-info.json", json.dumps(build_info_in).encode())
        zf.writestr("nd2wsi-viewer/_internal/nd2wsi-viewer-about/LICENSE.txt", b"MIT\n")
        zf.writestr("nd2wsi-viewer/_internal/webview2-runtime/msedgewebview2.exe", wv2_exe)

    bootstrapper = tmp_path / "MicrosoftEdgeWebview2Setup.exe"
    bootstrapper.write_bytes(_minimal_pe(0x014C, b"wv2-boot"))

    defines_out = tmp_path / "defines.json"
    mock_makensis = tmp_path / "makensis.py"
    mock_makensis.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, struct, sys\n"
        "if '-VERSION' in sys.argv:\n"
        "    print('v3.13')\n"
        "    raise SystemExit(0)\n"
        "defs = {}\n"
        "for a in sys.argv[1:]:\n"
        "    if a.startswith('-D') and '=' in a:\n"
        "        k, v = a[2:].split('=', 1)\n"
        "        defs[k] = v\n"
        f"pathlib.Path({str(defines_out)!r}).write_text(json.dumps(defs))\n"
        "out = pathlib.Path(defs['OUTPUT_FILE'])\n"
        "hdr = bytearray(0x80); hdr[0:2] = b'MZ'; struct.pack_into('<I', hdr, 0x3C, 0x80)\n"
        "out.write_bytes(bytes(hdr) + b'PE\\0\\0' + struct.pack('<H', 0x014C) + b'\\0'*24)\n",
        encoding="utf-8",
    )
    mock_makensis.chmod(0o755)
    makensis_bin = mock_makensis
    if sys.platform == "win32":
        makensis_bin = tmp_path / "makensis.cmd"
        makensis_bin.write_text(f'@echo off\r\n"{sys.executable}" "{mock_makensis}" %*\r\n', encoding="ascii")

    out_exe = tmp_path / "out" / "nd2wsi-viewer-2.6.7_SLACS-module-windows-x64-setup.exe"
    rc = builder.main([
        "--archive", str(archive),
        "--expected-sha256", builder.sha256(archive),
        "--makensis", str(makensis_bin),
        "--webview2-bootstrapper", str(bootstrapper),
        "--webview2-sha256", builder.sha256(bootstrapper),
        "--work", str(tmp_path / "work"),
        "--output", str(out_exe),
    ])
    assert rc == 0
    defines = json.loads(defines_out.read_text(encoding="utf-8"))
    assert defines["APP_VERSION"] == "2.6.7"
    assert defines["APP_VERSION_QUAD"] == "2.6.7.0"
    assert defines["APP_RELEASE_VERSION"] == "2.6.7_SLACS-module"
    assert defines["APP_DISPLAY_NAME"] == "nd2wsi-viewer SLACS"
    assert defines["INSTALL_DIR_NAME"] == "nd2wsi-viewer SLACS"
    assert defines["PRODUCT_ID"] == "nd2wsi-viewer.slacs.windows.x64.current-user"
    assert defines["UNINSTALL_KEY"].endswith(r"\nd2wsi-viewer-SLACS")
    assert defines["PROG_ID"] == "nd2wsi-viewer.SLACS.Slide"
    assert defines["SETUP_MUTEX_NAME"] == r"Local\nd2wsi-viewer-SLACS-Setup-current-user"
    assert defines["BUNDLED_WEBVIEW2"] == "1"

    slacs_readme = ROOT / "packaging/WINDOWS-INSTALLER-README-SLACS.txt"
    readme_text = slacs_readme.read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in readme_text)
    assert "Fixed Version Runtime" in readme_text
    assert r"%LOCALAPPDATA%\Programs\nd2wsi-viewer SLACS" in readme_text
    assert "Authenticode" in readme_text
    payload_manifest = json.loads(out_exe.with_suffix(".payload.json").read_text(encoding="utf-8"))
    assert payload_manifest["files"]["READ-ME.txt"] == builder.sha256(slacs_readme)


def test_verify_windows_installer_ps1_supports_slacs_bundled_webview2_and_stage_follow():
    ps1 = (ROOT / "packaging/verify_windows_installer.ps1").read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in ps1)
    assert "$script:bundledWebView2" in ps1
    assert "ND2WSI_WEBVIEW2_RUNTIME" in ps1
    assert "webview2_runtime.main.bundled" in ps1
    assert "webview2_runtime.translucent.bundled" in ps1
    assert "translucent_viewer.stage_follow.ok" in ps1
    assert "nd2wsi-viewer.slacs.windows.x64.current-user" in ps1
    assert "Local\\nd2wsi-viewer-SLACS-Setup-current-user" in ps1


# ---------------------------------------------------------------------------
# D5-F11: build_metal_rc.sh respects edition identity & Sparkle ordering
# ---------------------------------------------------------------------------


def test_build_metal_rc_respects_slacs_edition_and_plist_ordering(tmp_path):
    text = (ROOT / "packaging/build_metal_rc.sh").read_text(encoding="utf-8")
    assert "macos_bundle_identity.py" in text
    assert text.index("Info.plist") < text.index("inject_sparkle.py")

    script = text.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    script = "import importlib.metadata\nimportlib.metadata.version = lambda name: '2.6.7'\n" + script
    target = tmp_path / "Info.plist"
    target.write_bytes(plistlib.dumps({}))
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.6.7"\n', encoding="utf-8")
    edition_dst = tmp_path / "nd2wsi/edition.py"
    edition_dst.parent.mkdir()
    edition_dst.write_text((ROOT / "nd2wsi/edition.py").read_text(encoding="utf-8"), encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k not in ("ND2WSI_SKIP_UPDATER", "ND2WSI_SKIP_SMOKE")}
    subprocess.run(
        [sys.executable, "-", str(target), str(tmp_path / "pyproject.toml"), str(ROOT / "packaging/macos_bundle_identity.py")],
        input=script,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
        timeout=30,
    )
    info = plistlib.loads(target.read_bytes())
    assert info["CFBundleIdentifier"] == "com.nd2wsi.viewer.slacs"
    assert info["CFBundleName"] == "nd2wsi-viewer SLACS"
    assert info["ND2WSIEdition"] == "SLACS-module"
    assert info["ND2WSIUpdatesOff"] is True
    assert info["SUEnableAutomaticChecks"] is False
    assert "SUFeedURL" not in info
    assert info["ND2WSIDevelopmentOnly"] is False


# ---------------------------------------------------------------------------
# D5-F13b: qa/check_native_stage_follow.py has no hardcoded developer paths
# ---------------------------------------------------------------------------


def test_check_native_stage_follow_has_no_machine_specific_paths():
    script = (ROOT / "qa/check_native_stage_follow.py").read_text(encoding="utf-8")
    assert "/Volumes/Extreme SSD" not in script
    assert "/Users/myunghyunjeong" not in script
    assert "ND2WSI_QA_STAGE_FOLLOW_INPUT" in script
    assert "fingerprint_error" in script
