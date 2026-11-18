"""Regression tests for macOS Developer ID signing, notarization, stapling, and CI proof."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SIGN_SCRIPT = ROOT / "packaging" / "sign_release.sh"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "macos-release.yml"
RELEASING_DOC = ROOT / "docs" / "releasing.md"

DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS bash/file/xattr/ditto/codesign signing harness",
)


def _make_synthetic_app(base_dir: Path, *, include_sparkle: bool = True) -> Path:
    """Build a minimal Mach-O .app bundle inside base_dir without compiling anything."""
    app = base_dir / "nd2wsi-viewer.app"
    macos_dir = app / "Contents" / "MacOS"
    frameworks_dir = app / "Contents" / "Frameworks"
    resources_dir = app / "Contents" / "Resources"
    macos_dir.mkdir(parents=True)
    frameworks_dir.mkdir(parents=True)
    resources_dir.mkdir(parents=True)

    macho_template = Path("/usr/bin/true")
    shutil.copyfile(macho_template, macos_dir / "nd2wsi-viewer")
    (macos_dir / "nd2wsi-viewer").chmod(0o755)
    shutil.copyfile(macho_template, frameworks_dir / "libhelper.dylib")
    (frameworks_dir / "libhelper.dylib").chmod(0o755)
    (resources_dir / "notes.txt").write_text("non-executable resource\n", encoding="utf-8")

    info = {
        "CFBundleExecutable": "nd2wsi-viewer",
        "CFBundleIdentifier": "com.nd2wsi.viewer.test",
        "CFBundleName": "nd2wsi-viewer",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
    }
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)

    if include_sparkle:
        sparkle = frameworks_dir / "Sparkle.framework"
        ver_b = sparkle / "Versions" / "B"
        res_b = ver_b / "Resources"
        xpc_macos = ver_b / "XPCServices" / "Downloader.xpc" / "Contents" / "MacOS"
        res_b.mkdir(parents=True)
        xpc_macos.mkdir(parents=True)
        shutil.copyfile(macho_template, ver_b / "Sparkle")
        (ver_b / "Sparkle").chmod(0o755)
        shutil.copyfile(macho_template, xpc_macos / "Downloader")
        (xpc_macos / "Downloader").chmod(0o755)

        with (res_b / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleExecutable": "Sparkle",
                    "CFBundleIdentifier": "org.sparkle-project.Sparkle.TestPreserved",
                    "CFBundleName": "Sparkle",
                    "CFBundlePackageType": "FMWK",
                    "CFBundleShortVersionString": "2.9.6",
                    "CFBundleVersion": "2.9.6",
                },
                handle,
            )
        with (ver_b / "XPCServices" / "Downloader.xpc" / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleExecutable": "Downloader",
                    "CFBundleIdentifier": "org.sparkle-project.Downloader.TestPreserved",
                    "CFBundleName": "Downloader",
                    "CFBundlePackageType": "XPC!",
                    "CFBundleShortVersionString": "2.9.6",
                    "CFBundleVersion": "2.9.6",
                },
                handle,
            )
        (sparkle / "Versions" / "Current").symlink_to("B")
        (sparkle / "Sparkle").symlink_to("Versions/Current/Sparkle")
        (sparkle / "Resources").symlink_to("Versions/Current/Resources")
        (sparkle / "XPCServices").symlink_to("Versions/Current/XPCServices")

    return app


def _install_mock_apple_tools(
    mock_bin: Path,
    log_file: Path,
    *,
    python_exe: str | None = None,
) -> None:
    """Create mock codesign, xcrun, and spctl executables recording every invocation."""
    mock_bin.mkdir(parents=True, exist_ok=True)
    py_exe = python_exe or sys.executable
    recorder_py = mock_bin / "_apple_tool_recorder.py"
    recorder_py.write_text(
        f"""import json
import os
import sys
import zipfile
from pathlib import Path

tool = sys.argv[1]
args = sys.argv[2:]
log_path = Path({str(log_file)!r})
entry = {{"tool": tool, "args": args}}

if tool == "xcrun" and len(args) >= 3 and args[0] == "notarytool" and args[1] == "submit":
    submitted = Path(args[2])
    entry["submitted_exists"] = submitted.is_file()
    entry["submitted_suffix"] = submitted.suffix
    if submitted.is_file() and submitted.suffix == ".zip":
        with zipfile.ZipFile(submitted) as zf:
            entry["zip_names"] = sorted(zf.namelist())
    mode = os.environ.get("MOCK_NOTARY_MODE", "accept")
    sub_id = os.environ.get("MOCK_NOTARY_ID", "2efe2717-52ef-43a5-96dc-0797e4ca1041")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\\n")
    if mode == "accept":
        print(json.dumps({{"id": sub_id, "message": "Processing complete", "status": "Accepted"}}))
        raise SystemExit(0)
    if mode == "invalid_exit0":
        print(json.dumps({{"id": sub_id, "message": "Processing complete", "status": "Invalid"}}))
        raise SystemExit(0)
    if mode == "reject_nonzero":
        print(json.dumps({{"id": sub_id, "message": "Package Invalid", "status": "Invalid"}}))
        raise SystemExit(1)

if tool == "xcrun" and len(args) >= 3 and args[0] == "notarytool" and args[1] == "log":
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\\n")
    print(json.dumps({{"jobId": args[2], "status": "Invalid", "issues": [{{"message": "The binary is not signed with a valid Developer ID certificate."}}]}}))
    raise SystemExit(0)

with log_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\\n")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    for tool_name in ("codesign", "xcrun", "spctl"):
        script_path = mock_bin / tool_name
        launcher = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(py_exe)} {shlex.quote(str(recorder_py))} {shlex.quote(tool_name)} \"$@\"\n"
        )
        script_path.write_text(launcher, encoding="utf-8")
        script_path.chmod(0o755)


def _read_invocations(log_file: Path) -> list[dict]:
    if not log_file.is_file():
        return []
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


@DARWIN_ONLY
def test_sign_release_ad_hoc_mode_signs_app_and_dmg_and_preserves_sparkle(tmp_path: Path) -> None:
    app = _make_synthetic_app(tmp_path, include_sparkle=True)
    sparkle = app / "Contents" / "Frameworks" / "Sparkle.framework"
    xpc = sparkle / "Versions" / "B" / "XPCServices" / "Downloader.xpc"

    # Pre-sign Sparkle helpers as official Sparkle distribution archives arrive.
    subprocess.run(["codesign", "--force", "--sign", "-", str(xpc)], check=True)
    subprocess.run(
        [
            "codesign",
            "--force",
            "--sign",
            "-",
            "--identifier",
            "org.sparkle-project.Sparkle.CustomMarker",
            str(sparkle),
        ],
        check=True,
    )

    dmg_src = tmp_path / "dmg_src"
    dmg_src.mkdir()
    (dmg_src / "README.txt").write_text("dmg test\n", encoding="utf-8")
    dmg = tmp_path / "nd2wsi-viewer.dmg"
    subprocess.run(
        ["hdiutil", "create", "-volname", "nd2wsi-test", "-srcfolder", str(dmg_src), "-ov", "-format", "UDZO", str(dmg)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    isolated_tmp = tmp_path / "isolated_tmp"
    isolated_tmp.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CODESIGN_", "APPLE_NOTARY_", "NOTARIZE_"))
    }
    env["TMPDIR"] = str(isolated_tmp)
    # Even if a developer has APPLE_NOTARY_PROFILE in their shell profile, unset/ad-hoc
    # CODESIGN_IDENTITY without NOTARIZE_RELEASE=1 must stay 100% ad-hoc compatible.
    env["APPLE_NOTARY_PROFILE"] = "developer-shell-profile"

    completed = subprocess.run(
        ["/bin/bash", str(SIGN_SCRIPT), str(app), str(dmg)],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

    verify_app = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        text=True,
        capture_output=True,
    )
    assert verify_app.returncode == 0, verify_app.stderr

    sparkle_info = subprocess.run(
        ["codesign", "-dv", str(sparkle)],
        text=True,
        capture_output=True,
    )
    assert "Identifier=org.sparkle-project.Sparkle.CustomMarker" in sparkle_info.stderr

    verify_dmg = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(dmg)],
        text=True,
        capture_output=True,
    )
    assert verify_dmg.returncode == 0, verify_dmg.stderr
    assert list(isolated_tmp.iterdir()) == []


@DARWIN_ONLY
def test_sign_release_developer_id_keychain_profile_notarizes_staples_and_verifies(tmp_path: Path) -> None:
    app = _make_synthetic_app(tmp_path, include_sparkle=True)
    dmg = tmp_path / "nd2wsi-viewer.dmg"
    dmg.write_bytes(b"synthetic-udzo-dmg-payload")
    entitlements = tmp_path / "entitlements.plist"
    entitlements.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict></dict></plist>\n',
        encoding="utf-8",
    )

    spaced_py_dir = tmp_path / "nd2 viewer project" / "venv_bin"
    spaced_py_dir.mkdir(parents=True)
    spaced_py = spaced_py_dir / "python"
    spaced_py.symlink_to(sys.executable)

    mock_bin = tmp_path / "mock bin with spaces"
    log_file = tmp_path / "calls.jsonl"
    _install_mock_apple_tools(mock_bin, log_file, python_exe=str(spaced_py))
    isolated_tmp = tmp_path / "isolated_tmp"
    isolated_tmp.mkdir()

    identity = "Developer ID Application: Test Microscopy Lab (TEAM123456)"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CODESIGN_", "APPLE_NOTARY_", "NOTARIZE_"))
    }
    env.update(
        {
            "PATH": f"{mock_bin}:{env.get('PATH', '')}",
            "TMPDIR": str(isolated_tmp),
            "CODESIGN_IDENTITY": identity,
            "CODESIGN_ENTITLEMENTS": str(entitlements),
            "APPLE_NOTARY_PROFILE": "nd2wsi-notary-profile",
            "APPLE_NOTARY_KEYCHAIN": "/tmp/ephemeral-signing.keychain-db",
            "NOTARIZE_RELEASE": "1",
        }
    )

    completed = subprocess.run(
        ["/bin/bash", str(SIGN_SCRIPT), str(app), str(dmg)],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

    calls = _read_invocations(log_file)
    codesign_calls = [c["args"] for c in calls if c["tool"] == "codesign"]

    # Inner non-Sparkle dylib signed with hardened runtime + timestamp
    assert [
        "--force",
        "--sign",
        identity,
        "--options",
        "runtime",
        "--timestamp",
        str(app / "Contents" / "Frameworks" / "libhelper.dylib"),
    ] in codesign_calls

    # Sparkle helpers preserve upstream entitlements under Developer ID signing
    assert [
        "--force",
        "--sign",
        identity,
        "--options",
        "runtime",
        "--timestamp",
        "--preserve-metadata=entitlements",
        str(app / "Contents" / "Frameworks" / "Sparkle.framework"),
    ] in codesign_calls

    # Outer .app receives hardened runtime, timestamp, and passed entitlements
    assert [
        "--force",
        "--sign",
        identity,
        "--options",
        "runtime",
        "--timestamp",
        "--entitlements",
        str(entitlements),
        str(app),
    ] in codesign_calls
    assert ["--verify", "--deep", "--strict", "--verbose=2", str(app)] in codesign_calls

    # .dmg receives Developer ID signature + timestamp and strict verification
    assert ["--force", "--sign", identity, "--timestamp", str(dmg)] in codesign_calls
    assert ["--verify", "--deep", "--strict", "--verbose=2", str(dmg)] in codesign_calls

    submit_calls = [
        c for c in calls if c["tool"] == "xcrun" and c["args"][:2] == ["notarytool", "submit"]
    ]
    assert len(submit_calls) == 2

    app_submit, dmg_submit = submit_calls
    assert app_submit["submitted_exists"] is True
    assert app_submit["submitted_suffix"] == ".zip"
    assert "nd2wsi-viewer.app/Contents/MacOS/nd2wsi-viewer" in app_submit["zip_names"]
    assert app_submit["args"][3:] == [
        "--wait",
        "--output-format",
        "json",
        "--keychain-profile",
        "nd2wsi-notary-profile",
        "--keychain",
        "/tmp/ephemeral-signing.keychain-db",
    ]

    assert dmg_submit["submitted_exists"] is True
    assert dmg_submit["args"][2] == str(dmg)
    assert dmg_submit["args"][3:] == [
        "--wait",
        "--output-format",
        "json",
        "--keychain-profile",
        "nd2wsi-notary-profile",
        "--keychain",
        "/tmp/ephemeral-signing.keychain-db",
    ]

    xcrun_args = [c["args"] for c in calls if c["tool"] == "xcrun"]
    assert ["stapler", "staple", str(app)] in xcrun_args
    assert ["stapler", "validate", str(app)] in xcrun_args
    assert ["stapler", "staple", str(dmg)] in xcrun_args
    assert ["stapler", "validate", str(dmg)] in xcrun_args

    spctl_args = [c["args"] for c in calls if c["tool"] == "spctl"]
    assert spctl_args == [
        ["-a", "-vvv", "-t", "exec", str(app)],
        ["-a", "-vvv", "-t", "open", "--context", "context:primary-signature", str(dmg)],
    ]

    # Temporary submission archive directory is cleaned up completely
    assert list(isolated_tmp.iterdir()) == []


@DARWIN_ONLY
def test_sign_release_developer_id_api_key_auto_enables_and_notarize_zero_skips(tmp_path: Path) -> None:
    app = _make_synthetic_app(tmp_path, include_sparkle=False)
    api_key = tmp_path / "AuthKey_ABC1234567.p8"
    api_key.write_text("-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEH\n-----END PRIVATE KEY-----\n", encoding="utf-8")

    spaced_py_dir = tmp_path / "nd2 viewer project" / "venv_bin"
    spaced_py_dir.mkdir(parents=True)
    spaced_py = spaced_py_dir / "python"
    spaced_py.symlink_to(sys.executable)

    mock_bin = tmp_path / "mock bin with spaces"
    log_file = tmp_path / "calls.jsonl"
    _install_mock_apple_tools(mock_bin, log_file, python_exe=str(spaced_py))
    isolated_tmp = tmp_path / "isolated_tmp"
    isolated_tmp.mkdir()

    identity = "Developer ID Application: Test Microscopy Lab (TEAM123456)"
    base_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CODESIGN_", "APPLE_NOTARY_", "NOTARIZE_"))
    }
    base_env.update(
        {
            "PATH": f"{mock_bin}:{base_env.get('PATH', '')}",
            "TMPDIR": str(isolated_tmp),
            "CODESIGN_IDENTITY": identity,
            "APPLE_NOTARY_KEY_PATH": str(api_key),
            "APPLE_NOTARY_KEY_ID": "ABC1234567",
            "APPLE_NOTARY_ISSUER_ID": "11111111-2222-3333-4444-555555555555",
        }
    )

    # 1. Auto-enabled notarization when NOTARIZE_RELEASE is unset and all 3 API key vars are present
    completed = subprocess.run(
        ["/bin/bash", str(SIGN_SCRIPT), str(app)],
        env=base_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    calls = _read_invocations(log_file)
    submit_calls = [c for c in calls if c["tool"] == "xcrun" and c["args"][:2] == ["notarytool", "submit"]]
    assert len(submit_calls) == 1
    assert submit_calls[0]["args"][3:] == [
        "--wait",
        "--output-format",
        "json",
        "--key",
        str(api_key),
        "--key-id",
        "ABC1234567",
        "--issuer",
        "11111111-2222-3333-4444-555555555555",
    ]
    assert [c["args"] for c in calls if c["tool"] == "spctl"] == [["-a", "-vvv", "-t", "exec", str(app)]]

    # 2. Explicit NOTARIZE_RELEASE=0 signs with Developer ID only, skipping notarytool/stapler/spctl
    log_file.unlink()
    skip_env = dict(base_env, NOTARIZE_RELEASE="0")
    completed_skip = subprocess.run(
        ["/bin/bash", str(SIGN_SCRIPT), str(app)],
        env=skip_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed_skip.returncode == 0, completed_skip.stderr
    skip_calls = _read_invocations(log_file)
    assert any(c["tool"] == "codesign" and "--options" in c["args"] for c in skip_calls)
    assert not any(c["tool"] in {"xcrun", "spctl"} for c in skip_calls)


@DARWIN_ONLY
@pytest.mark.parametrize(
    "extra_env,expected_err",
    [
        ({"NOTARIZE_RELEASE": "1"}, "Developer ID Application CODESIGN_IDENTITY"),
        ({"CODESIGN_IDENTITY": "-", "NOTARIZE_RELEASE": "1"}, "got ad-hoc '-'"),
        (
            {
                "CODESIGN_IDENTITY": "Developer ID Application: Test (TEAM123456)",
                "NOTARIZE_RELEASE": "1",
            },
            "APPLE_NOTARY_PROFILE",
        ),
        (
            {
                "CODESIGN_IDENTITY": "Developer ID Application: Test (TEAM123456)",
                "APPLE_NOTARY_KEY_PATH": "/tmp/missing.p8",
                "APPLE_NOTARY_KEY_ID": "ABC1234567",
                "NOTARIZE_RELEASE": "1",
            },
            "APPLE_NOTARY_ISSUER_ID",
        ),
        (
            {
                "CODESIGN_IDENTITY": "Developer ID Application: Test (TEAM123456)",
                "APPLE_NOTARY_KEY_PATH": "/nonexistent/AuthKey_ABC1234567.p8",
                "APPLE_NOTARY_KEY_ID": "ABC1234567",
                "APPLE_NOTARY_ISSUER_ID": "11111111-2222-3333-4444-555555555555",
                "NOTARIZE_RELEASE": "1",
            },
            "APPLE_NOTARY_KEY_PATH file not found",
        ),
        ({"NOTARIZE_RELEASE": "maybe"}, "invalid NOTARIZE_RELEASE"),
        ({"CODESIGN_ENTITLEMENTS": "/nonexistent/entitlements.plist"}, "CODESIGN_ENTITLEMENTS file not found"),
    ],
)
def test_sign_release_fails_closed_on_invalid_notarization_inputs(
    tmp_path: Path,
    extra_env: dict[str, str],
    expected_err: str,
) -> None:
    app = _make_synthetic_app(tmp_path, include_sparkle=False)
    spaced_py_dir = tmp_path / "nd2 viewer project" / "venv_bin"
    spaced_py_dir.mkdir(parents=True)
    spaced_py = spaced_py_dir / "python"
    spaced_py.symlink_to(sys.executable)

    mock_bin = tmp_path / "mock bin with spaces"
    log_file = tmp_path / "calls.jsonl"
    _install_mock_apple_tools(mock_bin, log_file, python_exe=str(spaced_py))

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CODESIGN_", "APPLE_NOTARY_", "NOTARIZE_"))
    }
    env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
    env.update(extra_env)

    completed = subprocess.run(
        ["/bin/bash", str(SIGN_SCRIPT), str(app)],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode != 0
    assert expected_err in completed.stderr
    calls = _read_invocations(log_file)
    assert not any(c["tool"] in {"xcrun", "spctl"} for c in calls)


@DARWIN_ONLY
def test_sign_release_rejects_missing_or_unsupported_targets(tmp_path: Path) -> None:
    no_args = subprocess.run(["/bin/bash", str(SIGN_SCRIPT)], text=True, capture_output=True, timeout=15)
    assert no_args.returncode == 1
    assert "usage: sign_release.sh" in no_args.stderr

    bad_ext = tmp_path / "installer.pkg"
    bad_ext.write_bytes(b"pkg")
    bad_res = subprocess.run(
        ["/bin/bash", str(SIGN_SCRIPT), str(bad_ext)],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert bad_res.returncode == 1
    assert "expected .app or .dmg" in bad_res.stderr

    missing_dmg = subprocess.run(
        ["/bin/bash", str(SIGN_SCRIPT), str(tmp_path / "missing.dmg")],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert missing_dmg.returncode == 1
    assert "disk image file not found" in missing_dmg.stderr


@DARWIN_ONLY
@pytest.mark.parametrize("failure_mode", ["reject_nonzero", "invalid_exit0"])
def test_sign_release_fetches_notarytool_log_on_rejection_and_cleans_temp(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    app = _make_synthetic_app(tmp_path, include_sparkle=False)
    spaced_py_dir = tmp_path / "nd2 viewer project" / "venv_bin"
    spaced_py_dir.mkdir(parents=True)
    spaced_py = spaced_py_dir / "python"
    spaced_py.symlink_to(sys.executable)

    mock_bin = tmp_path / "mock bin with spaces"
    log_file = tmp_path / "calls.jsonl"
    _install_mock_apple_tools(mock_bin, log_file, python_exe=str(spaced_py))
    isolated_tmp = tmp_path / "isolated_tmp"
    isolated_tmp.mkdir()

    sub_id = "deadbeef-1234-5678-9abc-def012345678"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CODESIGN_", "APPLE_NOTARY_", "NOTARIZE_"))
    }
    env.update(
        {
            "PATH": f"{mock_bin}:{env.get('PATH', '')}",
            "TMPDIR": str(isolated_tmp),
            "CODESIGN_IDENTITY": "Developer ID Application: Test (TEAM123456)",
            "APPLE_NOTARY_PROFILE": "nd2wsi-notary",
            "NOTARIZE_RELEASE": "1",
            "MOCK_NOTARY_MODE": failure_mode,
            "MOCK_NOTARY_ID": sub_id,
        }
    )

    completed = subprocess.run(
        ["/bin/bash", str(SIGN_SCRIPT), str(app)],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 1
    assert "notarization failed" in completed.stderr
    assert "not signed with a valid Developer ID certificate" in completed.stderr

    calls = _read_invocations(log_file)
    xcrun_args = [c["args"] for c in calls if c["tool"] == "xcrun"]
    assert ["notarytool", "log", sub_id, "--keychain-profile", "nd2wsi-notary"] in xcrun_args
    assert not any(args[0] == "stapler" for args in xcrun_args)
    assert not any(c["tool"] == "spctl" for c in calls)
    assert list(isolated_tmp.iterdir()) == []


def test_macos_release_workflow_yaml_structure_and_secret_gates() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert workflow["name"] == "macos-release"
    steps = workflow["jobs"]["build"]["steps"]
    names = [step.get("name", "") for step in steps]

    cred_idx = names.index("Configure macOS signing keychain and notarization credentials")
    build_idx = names.index("Build the production app and require packaged smoke")
    dmg_idx = names.index("Sign, notarize, and staple the release DMG")
    gk_idx = names.index("Verify Gatekeeper acceptance and stapled notarization tickets")
    proof_idx = names.index("Verify the signed artifact and record build proof")
    cleanup_idx = names.index("Clean up ephemeral signing keychain and notary credentials")
    assert cred_idx < build_idx < dmg_idx < gk_idx < proof_idx < cleanup_idx

    cred_env = steps[cred_idx]["env"]
    assert cred_env == {
        "MACOS_CERT_P12_BASE64": "${{ secrets.MACOS_CERT_P12_BASE64 }}",
        "MACOS_CERT_PASSWORD": "${{ secrets.MACOS_CERT_PASSWORD }}",
        "CODESIGN_IDENTITY_SECRET": "${{ secrets.CODESIGN_IDENTITY }}",
        "APPLE_NOTARY_KEY_BASE64": "${{ secrets.APPLE_NOTARY_KEY_BASE64 }}",
        "APPLE_NOTARY_KEY_ID_SECRET": "${{ secrets.APPLE_NOTARY_KEY_ID }}",
        "APPLE_NOTARY_ISSUER_ID_SECRET": "${{ secrets.APPLE_NOTARY_ISSUER_ID }}",
        "APPLE_NOTARY_PROFILE_SECRET": "${{ secrets.APPLE_NOTARY_PROFILE }}",
    }

    assert steps[dmg_idx]["if"] == "env.MACOS_SIGNING_MODE != 'ad-hoc'"
    assert "packaging/sign_release.sh" in steps[dmg_idx]["run"]
    assert steps[gk_idx]["if"] == "env.MACOS_SIGNING_MODE == 'notarized'"
    assert "xcrun stapler validate" in steps[gk_idx]["run"]
    assert "spctl -a -vvv -t exec" in steps[gk_idx]["run"]
    assert "spctl -a -vvv -t open --context context:primary-signature" in steps[gk_idx]["run"]
    assert steps[cleanup_idx]["if"] == "always()"


@DARWIN_ONLY
def test_macos_release_workflow_credential_step_modes_and_fail_closed(tmp_path: Path) -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    cred_script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Configure macOS signing keychain and notarization credentials"
    )

    spaced_py_dir = tmp_path / "nd2 viewer project" / "venv_bin"
    spaced_py_dir.mkdir(parents=True)
    spaced_py = spaced_py_dir / "python"
    spaced_py.symlink_to(sys.executable)

    mock_bin = tmp_path / "mock security bin"
    mock_bin.mkdir()
    identity = "Developer ID Application: Test Microscopy Lab (TEAM123456)"
    security_py = mock_bin / "_security_mock.py"
    security_py.write_text(
        f"""import sys
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["create-keychain"]:
    Path(args[-1]).write_text("mock-keychain\\n", encoding="utf-8")
elif args[:1] == ["list-keychains"] and "-s" not in args:
    print('    "/Users/runner/Library/Keychains/login.keychain-db"')
elif args[:1] == ["find-identity"]:
    print('  1) ABCDEF1234567890 "{identity}"')
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    security_mock = mock_bin / "security"
    security_mock.write_text(
        f"#!/bin/sh\nexec {shlex.quote(str(spaced_py))} {shlex.quote(str(security_py))} \"$@\"\n",
        encoding="utf-8",
    )
    security_mock.chmod(0o755)

    def run_cred_step(secret_env: dict[str, str], case_name: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str], Path]:
        case_dir = tmp_path / case_name
        runner_temp = case_dir / "runner_temp"
        runner_temp.mkdir(parents=True)
        gh_env = case_dir / "github_env.txt"
        gh_env.write_text("", encoding="utf-8")
        env = {
            "PATH": f"{mock_bin}:{os.environ.get('PATH', '')}",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_ENV": str(gh_env),
            "MACOS_CERT_P12_BASE64": "",
            "MACOS_CERT_PASSWORD": "",
            "CODESIGN_IDENTITY_SECRET": "",
            "APPLE_NOTARY_KEY_BASE64": "",
            "APPLE_NOTARY_KEY_ID_SECRET": "",
            "APPLE_NOTARY_ISSUER_ID_SECRET": "",
            "APPLE_NOTARY_PROFILE_SECRET": "",
        }
        env.update(secret_env)
        proc = subprocess.run(
            ["/bin/bash", "-euo", "pipefail", "-c", cred_script],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        parsed: dict[str, str] = {}
        for line in gh_env.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                parsed[k] = v
        return proc, parsed, runner_temp

    # 1. Unconfigured secrets -> clean ad-hoc fallback
    proc_adhoc, env_adhoc, _ = run_cred_step({}, "adhoc")
    assert proc_adhoc.returncode == 0, proc_adhoc.stderr
    assert env_adhoc == {"MACOS_SIGNING_MODE": "ad-hoc"}

    # 2. Partial cert secrets -> fail closed
    p12_b64 = base64.b64encode(b"mock-pkcs12-bundle").decode("ascii")
    proc_partial_cert, _, _ = run_cred_step({"MACOS_CERT_P12_BASE64": p12_b64}, "partial_cert")
    assert proc_partial_cert.returncode != 0
    assert "incomplete macOS Developer ID signing secrets" in proc_partial_cert.stderr

    # 3. Notary secrets without cert secrets -> fail closed
    proc_notary_only, _, _ = run_cred_step({"APPLE_NOTARY_PROFILE_SECRET": "nd2wsi-notary"}, "notary_only")
    assert proc_notary_only.returncode != 0
    assert "without Developer ID signing secrets" in proc_notary_only.stderr

    # 4. Partial API key secrets -> fail closed
    proc_partial_api, _, _ = run_cred_step(
        {
            "MACOS_CERT_P12_BASE64": p12_b64,
            "MACOS_CERT_PASSWORD": "secret-password",
            "CODESIGN_IDENTITY_SECRET": identity,
            "APPLE_NOTARY_KEY_ID_SECRET": "ABC1234567",
        },
        "partial_api",
    )
    assert proc_partial_api.returncode != 0
    assert "incomplete App Store Connect API notarization secrets" in proc_partial_api.stderr

    # 5. Cert secrets without notary secrets -> developer-id mode, .p12 deleted after import
    proc_devid, env_devid, rt_devid = run_cred_step(
        {
            "MACOS_CERT_P12_BASE64": p12_b64,
            "MACOS_CERT_PASSWORD": "secret-password",
            "CODESIGN_IDENTITY_SECRET": identity,
        },
        "devid",
    )
    assert proc_devid.returncode == 0, proc_devid.stderr
    assert env_devid["MACOS_SIGNING_MODE"] == "developer-id"
    assert env_devid["NOTARIZE_RELEASE"] == "0"
    assert env_devid["CODESIGN_IDENTITY"] == identity
    assert not (rt_devid / "signing-cert.p12").exists()

    # 6. Cert secrets + API key secrets -> notarized mode, .p8 written with 0600 permissions
    p8_raw = b"-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEG\n-----END PRIVATE KEY-----\n"
    proc_notarized, env_notarized, _ = run_cred_step(
        {
            "MACOS_CERT_P12_BASE64": p12_b64,
            "MACOS_CERT_PASSWORD": "secret-password",
            "CODESIGN_IDENTITY_SECRET": identity,
            "APPLE_NOTARY_KEY_BASE64": base64.b64encode(p8_raw).decode("ascii"),
            "APPLE_NOTARY_KEY_ID_SECRET": "ABC1234567",
            "APPLE_NOTARY_ISSUER_ID_SECRET": "11111111-2222-3333-4444-555555555555",
        },
        "notarized_api",
    )
    assert proc_notarized.returncode == 0, proc_notarized.stderr
    assert env_notarized["MACOS_SIGNING_MODE"] == "notarized"
    assert env_notarized["NOTARIZE_RELEASE"] == "1"
    assert env_notarized["APPLE_NOTARY_KEY_ID"] == "ABC1234567"
    assert env_notarized["APPLE_NOTARY_ISSUER_ID"] == "11111111-2222-3333-4444-555555555555"
    key_path = Path(env_notarized["APPLE_NOTARY_KEY_PATH"])
    assert key_path.read_bytes() == p8_raw
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    # 7. Cert secrets + keychain profile -> notarized mode
    proc_prof, env_prof, _ = run_cred_step(
        {
            "MACOS_CERT_P12_BASE64": p12_b64,
            "MACOS_CERT_PASSWORD": "secret-password",
            "CODESIGN_IDENTITY_SECRET": identity,
            "APPLE_NOTARY_PROFILE_SECRET": "nd2wsi-ci-profile",
        },
        "notarized_profile",
    )
    assert proc_prof.returncode == 0, proc_prof.stderr
    assert env_prof["MACOS_SIGNING_MODE"] == "notarized"
    assert env_prof["NOTARIZE_RELEASE"] == "1"
    assert env_prof["APPLE_NOTARY_PROFILE"] == "nd2wsi-ci-profile"


def test_macos_release_workflow_build_info_signing_status_reporting(tmp_path: Path) -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    proof_run = next(
        step["run"]
        for step in steps
        if step.get("name") == "Verify the signed artifact and record build proof"
    )
    # Extract the second embedded Python script (the build-info.json proof generator).
    py_blocks = [chunk.split("\nPY", 1)[0] for chunk in proof_run.split("<<'PY'\n")[1:]]
    assert len(py_blocks) == 2
    proof_script = py_blocks[1]

    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    fake_sha = "a" * 40
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    shutil.copyfile(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    subprocess.run(["git", "-C", str(tmp_path), "add", "pyproject.toml"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    head_sha = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    assert len(head_sha) == len(fake_sha)

    output = tmp_path / "dist" / "macos"
    evidence = output / "build-evidence"
    app = output / "nd2wsi-viewer.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir(parents=True)
    sparkle_res = app / "Contents" / "Frameworks" / "Sparkle.framework" / "Versions" / "Current" / "Resources"
    sparkle_res.mkdir(parents=True)
    evidence.mkdir(parents=True)
    (tmp_path / "docs").mkdir(parents=True)

    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "ND2WSIPackageVersion": version,
                "ND2WSIDevelopmentOnly": False,
                "CFBundleIdentifier": "com.nd2wsi.viewer",
                "SUEnableAutomaticChecks": True,
                "SUAutomaticallyUpdate": False,
            },
            handle,
        )
    with (sparkle_res / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": "2.9.6"}, handle)
    (app / "Contents" / "MacOS" / "nd2wsi-viewer").write_bytes(b"arm64-binary")
    (output / "nd2wsi-viewer.dmg").write_bytes(b"dmg-payload")

    source_archive = output / "sdpc-codec-sources-macos-arm64.zip"
    source_archive.write_bytes(b"sdpc-sources")
    manifest = {
        "platform": "macos-arm64",
        "source_archive_file": source_archive.name,
        "source_archive_sha256": hashlib.sha256(b"sdpc-sources").hexdigest(),
    }
    manifest_path = tmp_path / "sdpc-codec-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    shutil.copyfile(manifest_path, app / "Contents" / "Resources" / "sdpc-codec-manifest.json")

    # Replace the hardcoded example_cell.nd2 fixture SHA check in this isolated unit test
    fixture_bytes = b"synthetic-smoke-nd2"
    (tmp_path / "docs" / "example_cell.nd2").write_bytes(fixture_bytes)
    fixture_sha = hashlib.sha256(fixture_bytes).hexdigest()
    adapted_script = proof_script.replace(
        "51d603317947b04c3b43c8e694f3ba97a16478ce30e795857ec6ae0321d41c33",
        fixture_sha,
    )

    (evidence / "mac-build.log").write_text(
        "smoke ok: SDPC\nsmoke ok: Astromap JPEG, TXT and PDF\nsmoke ok: whole-slide reduced TIFF export\n",
        encoding="utf-8",
    )

    def run_proof(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("CODESIGN_IDENTITY", None)
        env.pop("MACOS_SIGNING_MODE", None)
        env.update(
            {
                "ND2WSI_SDPC_CODEC_MANIFEST": str(manifest_path),
                "REQUESTED_REF": head_sha,
                "GITHUB_REPOSITORY": "myunghyunj/nd2wsi-viewer",
                "GITHUB_RUN_ID": "12345",
                "GITHUB_RUN_ATTEMPT": "1",
                "SPARKLE_SHA256": "52bf9e88cdd972fc0c81501377a880e90d47031bd8ca5462488f843e2609e192",
            }
        )
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-"],
            input=adapted_script,
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )

    # 1. Default ad-hoc mode
    res_adhoc = run_proof({"MACOS_SIGNING_MODE": "ad-hoc"})
    assert res_adhoc.returncode == 0, res_adhoc.stderr
    assert json.loads((output / "build-info.json").read_text(encoding="utf-8"))["signing"] == "ad-hoc"

    # 2. developer-id mode requires dmg-signing.log and non-ad-hoc identity
    (evidence / "dmg-signing.log").write_text("signed dmg\n", encoding="utf-8")
    res_devid = run_proof(
        {
            "MACOS_SIGNING_MODE": "developer-id",
            "CODESIGN_IDENTITY": "Developer ID Application: Test (TEAM123456)",
        }
    )
    assert res_devid.returncode == 0, res_devid.stderr
    assert json.loads((output / "build-info.json").read_text(encoding="utf-8"))["signing"] == "developer-id"

    # 3. notarized mode fails closed if stapler/spctl evidence logs are missing
    res_notarized_missing = run_proof(
        {
            "MACOS_SIGNING_MODE": "notarized",
            "CODESIGN_IDENTITY": "Developer ID Application: Test (TEAM123456)",
        }
    )
    assert res_notarized_missing.returncode != 0
    assert "Missing notarization verification log" in res_notarized_missing.stderr

    # 4. notarized mode succeeds when all stapler + spctl logs are present
    for log_name in ("stapler-app.log", "stapler-dmg.log", "spctl-app.log", "spctl-dmg.log"):
        (evidence / log_name).write_text("accepted\n", encoding="utf-8")
    res_notarized = run_proof(
        {
            "MACOS_SIGNING_MODE": "notarized",
            "CODESIGN_IDENTITY": "Developer ID Application: Test (TEAM123456)",
        }
    )
    assert res_notarized.returncode == 0, res_notarized.stderr
    assert json.loads((output / "build-info.json").read_text(encoding="utf-8"))["signing"] == "notarized"


def test_releasing_doc_covers_secrets_commands_and_respects_agents_boundary() -> None:
    doc = RELEASING_DOC.read_text(encoding="utf-8")
    assert "# Release completion contract" in doc
    for required_token in (
        "MACOS_CERT_P12_BASE64",
        "MACOS_CERT_PASSWORD",
        "CODESIGN_IDENTITY",
        "APPLE_NOTARY_KEY_BASE64",
        "APPLE_NOTARY_KEY_ID",
        "APPLE_NOTARY_ISSUER_ID",
        "APPLE_NOTARY_PROFILE",
        "NOTARIZE_RELEASE=1",
        "xcrun notarytool",
        "xcrun stapler",
        "spctl -a -vvv -t exec",
        "spctl -a -vvv -t open --context context:primary-signature",
        '"ad-hoc"',
        '"developer-id"',
        '"notarized"',
    ):
        assert required_token in doc, f"Missing {required_token!r} in docs/releasing.md"

    for forbidden_internal_token in (
        "--agent-window",
        'new_window("agent")',
        "window_context",
        "exports_root",
        "run_native_check.py",
    ):
        assert forbidden_internal_token not in doc
