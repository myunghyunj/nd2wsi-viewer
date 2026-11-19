"""Tests for Windows NSIS installer CI wiring, format sync, and optional Authenticode signing."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import struct
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

MILESTONE_1_EXTENSIONS = (
    ".nd2",
    ".czi",
    ".lif",
    ".tif",
    ".tiff",
    ".qptiff",
    ".svs",
    ".sdpc",
    ".nd2svs",
)

FORBIDDEN_AUTOMATION_TOKENS = (
    "--agent-window",
    'new_window("agent")',
    "new_window('agent')",
    "window_context",
    "New Agent Window",
    "exports_root",
)


def _load_installer_builder():
    spec = importlib.util.spec_from_file_location(
        "windows_installer_builder_ci",
        ROOT / "packaging" / "build_windows_installer.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _minimal_pe(machine: int = 0x8664, payload: bytes = b"") -> bytes:
    header = bytearray(256)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x80)
    header[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", header, 0x84, machine)
    return bytes(header) + payload


def _make_executable_script(path: Path, body: str) -> Path:
    py_file = path.with_name(path.stem + "_impl.py")
    py_file.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    if os.name == "nt":
        cmd_path = path.with_suffix(".cmd")
        cmd_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_file}" %*\r\n', encoding="utf-8"
        )
        return cmd_path
    wrapper = f'#!/bin/sh\nexec "{sys.executable}" "{py_file}" "$@"\n'
    path.write_text(wrapper, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
    return path


def _build_portable_fixture(
    tmp_path: Path,
    *,
    version: str = "2.6.4",
) -> tuple[Path, str, Path, str, bytes]:
    exe_bytes = _minimal_pe(0x8664, b"nd2wsi-viewer-x64-payload")
    exe_sha = _sha256_bytes(exe_bytes)
    build_info = {
        "app": "nd2wsi-viewer",
        "version": version,
        "binary_architecture": "x64",
        "executable_sha256": exe_sha,
    }
    archive = tmp_path / f"nd2wsi-viewer-{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("nd2wsi-viewer/nd2wsi-viewer.exe", exe_bytes)
        zf.writestr("nd2wsi-viewer/READ-ME.txt", b"portable readme\n")
        zf.writestr(
            "nd2wsi-viewer/_internal/nd2wsi-viewer-about/build-info.json",
            (json.dumps(build_info, indent=2) + "\n").encode("utf-8"),
        )
        zf.writestr(
            "nd2wsi-viewer/_internal/nd2wsi-viewer-about/LICENSE.txt",
            b"MIT License\n",
        )
        zf.writestr(
            "nd2wsi-viewer/_internal/nd2wsi-viewer-about/THIRD_PARTY_NOTICES.md",
            b"Notices\n",
        )
        zf.writestr("nd2wsi-viewer/_internal/python313.dll", _minimal_pe(0x8664, b"py313"))
    archive_bytes = archive.read_bytes()
    archive_sha = _sha256_bytes(archive_bytes)

    bootstrapper = tmp_path / "MicrosoftEdgeWebview2Setup.exe"
    bootstrapper.write_bytes(_minimal_pe(0x014C, b"webview2-bootstrapper"))
    bootstrapper_sha = _sha256_file(bootstrapper)
    return archive, archive_sha, bootstrapper, bootstrapper_sha, exe_bytes


def _make_mock_makensis(tmp_path: Path, event_log: Path) -> Path:
    return _make_executable_script(
        tmp_path / "mock_makensis.py",
        f"""
        import json, pathlib, struct, sys

        log_path = pathlib.Path({str(event_log)!r})
        args = sys.argv[1:]
        if args == ["-VERSION"]:
            print("v3.12")
            raise SystemExit(0)

        defines = {{}}
        for item in args:
            if item.startswith("-D") and "=" in item:
                k, v = item[2:].split("=", 1)
                defines[k] = v

        install_inc = pathlib.Path(defines["PAYLOAD_INSTALL_INCLUDE"]).read_text(encoding="utf-8")
        exe_hash = None
        for line in install_inc.splitlines():
            if '"/oname=nd2wsi-viewer.exe"' in line:
                parts = line.split('"')
                staged_exe = pathlib.Path(parts[3])
                import hashlib
                exe_hash = hashlib.sha256(staged_exe.read_bytes()).hexdigest()

        out = pathlib.Path(defines["OUTPUT_FILE"])
        header = bytearray(256)
        header[:2] = b"MZ"
        struct.pack_into("<I", header, 0x3C, 0x80)
        header[0x80:0x84] = b"PE\\0\\0"
        struct.pack_into("<H", header, 0x84, 0x014C)
        out.write_bytes(bytes(header) + b"nsis-setup-stub:" + (exe_hash or "").encode("ascii"))

        events = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
        events.append({{"tool": "makensis", "output": out.name, "staged_exe_sha256": exe_hash, "defines": defines}})
        log_path.write_text(json.dumps(events), encoding="utf-8")
        print("Output: " + str(out))
        """,
    )


def _make_mock_signtool(tmp_path: Path, event_log: Path, *, fail: bool = False) -> Path:
    return _make_executable_script(
        tmp_path / ("mock_signtool_fail.py" if fail else "mock_signtool.py"),
        f"""
        import json, pathlib, sys

        log_path = pathlib.Path({str(event_log)!r})
        args = sys.argv[1:]
        target = pathlib.Path(args[-1])
        events = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
        events.append({{"tool": "signtool", "args": args, "target": target.name}})
        log_path.write_text(json.dumps(events), encoding="utf-8")

        if {fail!r}:
            pw = args[args.index("/p") + 1] if "/p" in args else ""
            print(f"SignTool Error: Invalid PFX password {{pw}}")
            raise SystemExit(1)

        target.write_bytes(target.read_bytes() + b"\\nAUTHENTICODE-SHA256:" + target.name.encode("utf-8"))
        print("Successfully signed: " + target.name)
        """,
    )


def _clean_signing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "WINDOWS_SIGN_PFX_PATH",
        "WINDOWS_SIGN_PFX_PASSWORD",
        "WINDOWS_SIGN_THUMBPRINT",
        "WINDOWS_SIGNTOOL",
        "WINDOWS_TIMESTAMP_URL",
        "WINDOWS_REQUIRE_SIGNING",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 1. build_windows_installer.py: Unsigned Default & Mocked Authenticode Flows
# ---------------------------------------------------------------------------


def test_build_windows_installer_defaults_cleanly_to_unsigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_signing_env(monkeypatch)
    builder = _load_installer_builder()
    archive, archive_sha, bootstrapper, bootstrapper_sha, exe_bytes = _build_portable_fixture(tmp_path)
    original_zip_bytes = archive.read_bytes()
    event_log = tmp_path / "events.json"
    makensis = _make_mock_makensis(tmp_path, event_log)
    output = tmp_path / "out" / "nd2wsi-viewer-2.6.4-windows-x64-setup.exe"
    work = tmp_path / "work"

    rc = builder.main(
        [
            "--archive",
            str(archive),
            "--expected-sha256",
            archive_sha,
            "--makensis",
            str(makensis),
            "--webview2-bootstrapper",
            str(bootstrapper),
            "--webview2-sha256",
            bootstrapper_sha,
            "--work",
            str(work),
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert output.is_file()
    assert archive.read_bytes() == original_zip_bytes

    report = json.loads(output.with_suffix(".build.json").read_text(encoding="utf-8"))
    payload_manifest = json.loads(output.with_suffix(".payload.json").read_text(encoding="utf-8"))
    sidecar = Path(str(output) + ".sha256").read_text(encoding="ascii").strip()

    assert report["app_version"] == "2.6.4"
    assert report["installer_sha256"] == _sha256_file(output)
    assert sidecar == f"{report['installer_sha256']}  {output.name}"
    assert report["signing"]["signed"] is False
    assert report["signing"]["status"] == "unsigned"
    assert report["app_executable_sha256"] == _sha256_bytes(exe_bytes)
    assert payload_manifest["signing"]["signed"] is False
    assert "build_info_override" not in payload_manifest

    events = json.loads(event_log.read_text(encoding="utf-8"))
    assert [e["tool"] for e in events] == ["makensis"]


def test_build_windows_installer_signs_exe_before_makensis_and_setup_after_makensis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clean_signing_env(monkeypatch)
    builder = _load_installer_builder()
    archive, archive_sha, bootstrapper, bootstrapper_sha, exe_bytes = _build_portable_fixture(tmp_path)
    original_zip_bytes = archive.read_bytes()
    unsigned_exe_sha = _sha256_bytes(exe_bytes)

    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"fake-pfx-bundle")
    secret_pw = "TopSecretPfxPass!2026"
    event_log = tmp_path / "events.json"
    makensis = _make_mock_makensis(tmp_path, event_log)
    signtool = _make_mock_signtool(tmp_path, event_log)

    monkeypatch.setenv("WINDOWS_SIGN_PFX_PATH", str(pfx))
    monkeypatch.setenv("WINDOWS_SIGN_PFX_PASSWORD", secret_pw)
    monkeypatch.setenv("WINDOWS_SIGNTOOL", str(signtool))
    monkeypatch.setenv("WINDOWS_TIMESTAMP_URL", "http://timestamp.digicert.com")
    monkeypatch.setenv("WINDOWS_REQUIRE_SIGNING", "1")

    output = tmp_path / "out" / "nd2wsi-viewer-2.6.4-windows-x64-setup.exe"
    work = tmp_path / "work"
    rc = builder.main(
        [
            "--archive",
            str(archive),
            "--expected-sha256",
            archive_sha,
            "--makensis",
            str(makensis),
            "--webview2-bootstrapper",
            str(bootstrapper),
            "--webview2-sha256",
            bootstrapper_sha,
            "--work",
            str(work),
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert archive.read_bytes() == original_zip_bytes

    events = json.loads(event_log.read_text(encoding="utf-8"))
    assert [(e["tool"], e.get("target") or e.get("output")) for e in events] == [
        ("signtool", "nd2wsi-viewer.exe"),
        ("makensis", "nd2wsi-viewer-2.6.4-windows-x64-setup.exe"),
        ("signtool", "nd2wsi-viewer-2.6.4-windows-x64-setup.exe"),
    ]

    for sign_event in (events[0], events[2]):
        args = sign_event["args"]
        assert args[0] == "sign"
        assert args[args.index("/fd") + 1] == "sha256"
        assert args[args.index("/td") + 1] == "sha256"
        assert args[args.index("/tr") + 1] == "http://timestamp.digicert.com"
        assert args[args.index("/f") + 1] == str(pfx.resolve())
        assert args[args.index("/p") + 1] == secret_pw

    report_text = output.with_suffix(".build.json").read_text(encoding="utf-8")
    payload_text = output.with_suffix(".payload.json").read_text(encoding="utf-8")
    compile_log = output.with_suffix(".compile.log").read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for text in (report_text, payload_text, compile_log, captured.out, captured.err):
        assert secret_pw not in text

    report = json.loads(report_text)
    payload_manifest = json.loads(payload_text)
    staged_exe = work / "nd2wsi-viewer" / "nd2wsi-viewer.exe"
    staged_info = json.loads(
        (work / "nd2wsi-viewer" / "_internal" / "nd2wsi-viewer-about" / "build-info.json").read_text(
            encoding="utf-8"
        )
    )

    assert events[1]["staged_exe_sha256"] == _sha256_file(staged_exe)
    assert report["signing"]["signed"] is True
    assert report["signing"]["status"] == "authenticode"
    assert report["signing"]["method"] == "pfx"
    assert report["signing"]["digest_algorithm"] == "sha256"
    assert report["signing"]["timestamp_digest_algorithm"] == "sha256"
    assert report["signing"]["unsigned_app_executable_sha256"] == unsigned_exe_sha
    assert report["app_executable_sha256"] == _sha256_file(staged_exe)
    assert report["app_executable_sha256"] != unsigned_exe_sha
    assert report["installer_sha256"] == _sha256_file(output)
    assert report["installer_sha256"] != report["signing"]["unsigned_installer_sha256"]
    assert staged_info["unsigned_executable_sha256"] == unsigned_exe_sha
    assert staged_info["executable_sha256"] == report["app_executable_sha256"]
    assert payload_manifest["files"]["nd2wsi-viewer.exe"] == report["app_executable_sha256"]
    assert "build_info_override" in payload_manifest


def test_sign_windows_binary_supports_thumbprint_and_sdk_discovery(tmp_path: Path) -> None:
    builder = _load_installer_builder()
    event_log = tmp_path / "events.json"
    kits_root = tmp_path / "Program Files (x86)"
    sdk_old = kits_root / "Windows Kits" / "10" / "bin" / "10.0.19041.0" / "x64"
    sdk_new = kits_root / "Windows Kits" / "10" / "bin" / "10.0.26100.0" / "x64"
    sdk_old.mkdir(parents=True)
    sdk_new.mkdir(parents=True)
    (sdk_old / "signtool.exe").write_bytes(_minimal_pe(0x8664, b"old-sdk"))
    newest_exe = sdk_new / "signtool.exe"
    newest_exe.write_bytes(_minimal_pe(0x8664, b"new-sdk"))

    discovered = builder.find_signtool({"PATH": "", "ProgramFiles(x86)": str(kits_root)})
    assert discovered == newest_exe.resolve()

    mock_signtool = _make_mock_signtool(tmp_path, event_log)
    binary = tmp_path / "sample.exe"
    binary.write_bytes(_minimal_pe(0x8664, b"payload"))
    thumb = "0123456789abcdef0123456789abcdef01234567"
    env = {
        "WINDOWS_SIGNTOOL": str(mock_signtool),
        "WINDOWS_SIGN_THUMBPRINT": thumb,
    }
    res = builder.sign_windows_binary(binary, env=env, description="nd2wsi-viewer")
    assert res["signed"] is True
    assert res["method"] == "thumbprint"
    assert res["signtool_sha256"] == _sha256_file(mock_signtool)

    events = json.loads(event_log.read_text(encoding="utf-8"))
    assert len(events) == 1
    args = events[0]["args"]
    assert "/sha1" in args and args[args.index("/sha1") + 1] == thumb.upper()
    assert args[args.index("/fd") + 1] == "sha256"
    assert args[args.index("/td") + 1] == "sha256"


def test_build_windows_installer_fails_closed_on_invalid_signing_configurations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_signing_env(monkeypatch)
    builder = _load_installer_builder()
    archive, archive_sha, bootstrapper, bootstrapper_sha, _ = _build_portable_fixture(tmp_path)
    event_log = tmp_path / "events.json"
    makensis = _make_mock_makensis(tmp_path, event_log)

    # 1. WINDOWS_REQUIRE_SIGNING=1 without credentials fails closed before creating work/output
    monkeypatch.setenv("WINDOWS_REQUIRE_SIGNING", "1")
    output = tmp_path / "out" / "nd2wsi-viewer-2.6.4-windows-x64-setup.exe"
    work = tmp_path / "work"
    with pytest.raises(RuntimeError, match="WINDOWS_REQUIRE_SIGNING=1"):
        builder.main(
            [
                "--archive",
                str(archive),
                "--expected-sha256",
                archive_sha,
                "--makensis",
                str(makensis),
                "--webview2-bootstrapper",
                str(bootstrapper),
                "--webview2-sha256",
                bootstrapper_sha,
                "--work",
                str(work),
                "--output",
                str(output),
            ]
        )
    assert not output.exists()
    assert not work.exists()
    assert not event_log.exists()

    # 2. WINDOWS_SIGNTOOL set without credentials fails closed
    _clean_signing_env(monkeypatch)
    with pytest.raises(RuntimeError, match="without signing credentials"):
        builder.resolve_signing_config({"WINDOWS_SIGNTOOL": str(makensis)})

    # 3. Both PFX and thumbprint set simultaneously fails closed
    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"pfx")
    with pytest.raises(ValueError, match="not both"):
        builder.resolve_signing_config(
            {
                "WINDOWS_SIGN_PFX_PATH": str(pfx),
                "WINDOWS_SIGN_THUMBPRINT": "a" * 40,
                "WINDOWS_SIGNTOOL": str(makensis),
            }
        )

    # 4. Missing PFX file fails closed
    with pytest.raises(FileNotFoundError, match="WINDOWS_SIGN_PFX_PATH"):
        builder.resolve_signing_config(
            {
                "WINDOWS_SIGN_PFX_PATH": str(tmp_path / "missing.pfx"),
                "WINDOWS_SIGNTOOL": str(makensis),
            }
        )

    # 5. Malformed thumbprint fails closed
    with pytest.raises(ValueError, match="WINDOWS_SIGN_THUMBPRINT"):
        builder.resolve_signing_config(
            {
                "WINDOWS_SIGN_THUMBPRINT": "not-a-hex-thumbprint",
                "WINDOWS_SIGNTOOL": str(makensis),
            }
        )

    # 6. signtool failure fails closed and redacts PFX password from exception message
    failing_signtool = _make_mock_signtool(tmp_path, event_log, fail=True)
    binary = tmp_path / "viewer.exe"
    binary.write_bytes(_minimal_pe(0x8664, b"exe"))
    secret = "NeverLeakThisSecret99!"
    with pytest.raises(RuntimeError, match="Authenticode signtool failed") as exc_info:
        builder.sign_windows_binary(
            binary,
            env={
                "WINDOWS_SIGN_PFX_PATH": str(pfx),
                "WINDOWS_SIGN_PFX_PASSWORD": secret,
                "WINDOWS_SIGNTOOL": str(failing_signtool),
            },
        )
    assert secret not in str(exc_info.value)
    assert "***" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. windows-installer.nsi & verify_windows_installer.ps1 Contracts
# ---------------------------------------------------------------------------


def test_nsis_script_per_user_hygiene_and_milestone_1_formats() -> None:
    nsi = (ROOT / "packaging" / "windows-installer.nsi").read_text(encoding="utf-8")

    assert "Unicode true" in nsi
    assert "RequestExecutionLevel user" in nsi
    assert 'InstallDir "$LOCALAPPDATA\\Programs\\nd2wsi-viewer"' in nsi
    assert "SetShellVarContext current" in nsi
    assert "SetRegView 64" in nsi

    # Zero HKLM writes/deletes (HKLM may only be read for OS, .NET, and WebView2 detection)
    for forbidden_hklm in (
        "WriteRegStr HKLM",
        "WriteRegDWORD HKLM",
        "WriteRegBin HKLM",
        "WriteRegExpandStr HKLM",
        "DeleteRegKey HKLM",
        "DeleteRegKey /ifempty HKLM",
        "DeleteRegValue HKLM",
    ):
        assert forbidden_hklm not in nsi

    # Windows Server with Desktop Experience (e.g., windows-2025 CI) must not be blanket-blocked
    assert "${If} ${IsServerOS}" not in nsi
    assert "Server Core" in nsi

    # All 9 Milestone 1 formats are registered and unregistered cleanly under HKCU
    for ext in MILESTONE_1_EXTENSIONS:
        assert f'!insertmacro ND2WSI_REGISTER_EXTENSION "{ext}"' in nsi
        assert f'!insertmacro ND2WSI_UNREGISTER_EXTENSION "{ext}"' in nsi

    # Shared extension keys (e.g., .tif) use RegQueryInfoKeyW value+subkey emptiness check
    assert "Function un.DeleteKeyIfTrulyEmpty" in nsi
    assert "RegQueryInfoKeyW" in nsi
    assert "Call WriteFileAssociations" in nsi
    uninstall_section = nsi.split('Section "Uninstall"', 1)[1]
    assert "Call un.RemoveFileAssociations" in uninstall_section
    assert uninstall_section.index("Call un.RemoveFileAssociations") < uninstall_section.index(
        "Call un.MarkInProgress"
    )

    # Structural NSIS block balance check
    assert len(re.findall(r"(?m)^\s*Function\s+", nsi)) == len(
        re.findall(r"(?m)^\s*FunctionEnd\s*$", nsi)
    )
    assert len(re.findall(r"(?m)^\s*Section\s+", nsi)) == len(
        re.findall(r"(?m)^\s*SectionEnd\s*$", nsi)
    )
    assert len(re.findall(r"(?m)^\s*!macro\s+", nsi)) == len(
        re.findall(r"(?m)^\s*!macroend\s*$", nsi)
    )
    assert len(re.findall(r"\$\{(?:If|IfNot)\}", nsi)) == len(re.findall(r"\$\{EndIf\}", nsi))


def test_verify_windows_installer_ps1_structure_ascii_and_smoke_snippet(tmp_path: Path) -> None:
    ps1 = (ROOT / "packaging" / "verify_windows_installer.ps1").read_text(encoding="utf-8")

    # Windows PowerShell 5.1 requires pure 7-bit ASCII when no UTF-8 BOM is present
    assert all(ord(ch) < 128 for ch in ps1), "verify_windows_installer.ps1 must remain pure ASCII"

    for ext in MILESTONE_1_EXTENSIONS:
        assert f"'{ext}'" in ps1

    assert "function Assert-FileAssociations" in ps1
    assert "function Assert-FileAssociationsRemoved" in ps1
    assert "function Invoke-CliAndOmetiffSmoke" in ps1
    assert "function Invoke-DirectSilentUninstallAndVerifyCleanRemoval" in ps1
    assert "'-notin @(''Client'', ''Server'')'" in ps1 or "-notin @('Client', 'Server')" in ps1
    assert "'/S /D='" in ps1
    assert "-ArgumentList '/S'" in ps1
    assert "Local\\nd2wsi-viewer-Setup-current-user" in ps1

    # Verify the installer smoke uses --cli-self-check (never arbitrary -c exec)
    assert "--cli-self-check" in ps1
    assert "'-c'" not in ps1 and '"-c"' not in ps1
    report_path = tmp_path / "ometiff-cli-report.json"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "-m", "nd2wsi.app", "--cli-self-check", str(report_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "ok": True,
        "ometiff": True,
        "cli_help": True,
    }


# ---------------------------------------------------------------------------
# 3. .github/workflows/windows.yml YAML Validity, Artifacts & Metadata Step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("signed", [False, True])
def test_windows_workflow_yaml_steps_and_build_info_generation(
    tmp_path: Path, signed: bool
) -> None:
    workflow_path = ROOT / ".github" / "workflows" / "windows.yml"
    doc = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert doc["permissions"] == {"contents": "read"}
    assert set(doc["jobs"]) == {"build", "verify"}

    build_steps = doc["jobs"]["build"]["steps"]
    verify_steps = doc["jobs"]["verify"]["steps"]

    step_names = [s.get("name", "") for s in build_steps]
    assert "Build executable and require packaged headless and GUI smoke" in step_names
    assert "Prepare NSIS compiler and Microsoft WebView2 bootstrapper" in step_names
    assert "Configure optional Authenticode signing" in step_names
    assert "Build the per-user NSIS Windows installer" in step_names
    assert "Smoke-verify the NSIS installer silent install and clean uninstall" in step_names
    assert "Record Windows release build and signing metadata" in step_names
    assert "Remove temporary Authenticode key material" in step_names

    # Portable ZIP upload stays intact alongside NSIS Setup installer upload
    uploads = {
        s["with"]["name"]: s["with"]["path"]
        for s in build_steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    }
    assert "windows-x64-portable" in uploads
    assert "nd2wsi-viewer-*-windows-x64.zip" in uploads["windows-x64-portable"]
    assert "windows-x64-installer" in uploads
    assert "nd2wsi-viewer-*-windows-x64-setup.exe" in uploads["windows-x64-installer"]
    assert "dist/windows/build-info.json" in uploads["windows-x64-installer"]
    assert "windows-x64-release" in uploads
    assert "nd2wsi-viewer-*-windows-x64-setup.exe" in uploads["windows-x64-release"]
    assert "nd2wsi-viewer-*-windows-x64.zip" in uploads["windows-x64-release"]

    # Clean x64 verify job runs verify_windows_installer.ps1
    verify_installer_steps = [
        s for s in verify_steps if s.get("name") == "Verify the shipped NSIS installer on clean Windows x64"
    ]
    assert len(verify_installer_steps) == 1
    assert verify_installer_steps[0]["if"] == "matrix.platform == 'x64'"
    assert "verify_windows_installer.ps1" in verify_installer_steps[0]["run"]

    # Execute the inline Python build-info.json step against synthetic unsigned & signed outputs
    record_step = next(
        s for s in build_steps if s.get("name") == "Record Windows release build and signing metadata"
    )
    assert record_step["shell"] == "python"

    fake_repo = tmp_path / "repo"
    (fake_repo / "dist" / "windows" / "installer-smoke").mkdir(parents=True)
    (fake_repo / "docs").mkdir()
    (fake_repo / "pyproject.toml").write_text('[project]\nversion = "2.6.4"\n', encoding="utf-8")
    (fake_repo / "docs" / "example_cell.nd2").write_bytes(b"nd2-fixture")
    subprocess.run(["git", "init", "-q"], cwd=fake_repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=fake_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=fake_repo, check=True)
    subprocess.run(["git", "add", "pyproject.toml"], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=fake_repo, check=True)

    dist = fake_repo / "dist" / "windows"
    portable = dist / "nd2wsi-viewer-2.6.4-windows-x64.zip"
    setup_exe = dist / "nd2wsi-viewer-2.6.4-windows-x64-setup.exe"
    sdpc_zip = dist / "sdpc-codec-sources-windows-x64.zip"
    for path, content in (
        (portable, b"portable-zip"),
        (setup_exe, b"setup-exe"),
        (sdpc_zip, b"sdpc-sources"),
    ):
        path.write_bytes(content)
        Path(str(path) + ".sha256").write_text(
            f"{_sha256_bytes(content)}  {path.name}\n", encoding="ascii"
        )

    setup_exe.with_suffix(".build.json").write_text(
        json.dumps(
            {
                "installer_sha256": _sha256_file(setup_exe),
                "installer_bytes": setup_exe.stat().st_size,
                "compiler_version": "v3.12",
                "payload_file_count": 42,
                "signing": {
                    "signed": signed,
                    "status": "authenticode" if signed else "unsigned",
                    "method": "pfx" if signed else None,
                },
            }
        ),
        encoding="utf-8",
    )
    (dist / "installer-smoke" / "installer-verification.json").write_text(
        json.dumps({"state": "complete"}), encoding="utf-8"
    )
    sdpc_manifest = tmp_path / "sdpc-codec-manifest.json"
    sdpc_manifest.write_text("{}", encoding="utf-8")

    script_file = tmp_path / "record_metadata.py"
    script_file.write_text(record_step["run"], encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script_file)],
        cwd=fake_repo,
        env={
            **os.environ,
            "SETUP_EXE": str(setup_exe),
            "ND2WSI_SDPC_CODEC_MANIFEST": str(sdpc_manifest),
            "GITHUB_REPOSITORY": "myunghyunj/nd2wsi-viewer",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    build_info = json.loads((dist / "build-info.json").read_text(encoding="utf-8"))
    expected_status = "authenticode" if signed else "unsigned"
    assert build_info["version"] == "2.6.4"
    assert build_info["signing"] == expected_status
    assert build_info["installer_signing"] == expected_status
    assert build_info["portable_signing"] == "unsigned"
    assert build_info["installer"]["filename"] == setup_exe.name
    assert build_info["installer"]["verification"] == "passed"
    assert build_info["installer"]["per_user_install"] is True
    assert build_info["installer"]["uac_elevation"] is False
    assert set(build_info["artifacts"]) == {portable.name, setup_exe.name, sdpc_zip.name}


# ---------------------------------------------------------------------------
# 4. Windows Installer Documentation Sync, Links & AGENTS.md Hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_rel",
    [
        "packaging/WINDOWS-INSTALLER-README.txt",
        "packaging/windows-installer.md",
        "docs/windows.md",
    ],
)
def test_windows_installer_docs_cover_milestone_1_formats_and_never_leak_agent_tokens(
    doc_rel: str,
) -> None:
    doc_path = ROOT / doc_rel
    content = doc_path.read_text(encoding="utf-8")

    for ext in MILESTONE_1_EXTENSIONS:
        assert ext in content, f"{doc_rel} omits Milestone 1 format {ext}"

    assert "%LOCALAPPDATA%\\Programs\\nd2wsi-viewer" in content
    assert "Authenticode" in content

    for forbidden in FORBIDDEN_AUTOMATION_TOKENS:
        assert forbidden not in content, f"{doc_rel} leaked internal token {forbidden!r}"

    if doc_path.suffix == ".md":
        stripped = re.sub(r"```.*?```", "", content, flags=re.S)
        for match in re.finditer(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", stripped):
            raw = match.group(1)
            if raw.startswith(("http://", "https://", "mailto:", "data:")):
                continue
            path_part, _, _ = raw.partition("#")
            target = (doc_path.parent / path_part).resolve() if path_part else doc_path.resolve()
            assert target.exists(), f"{doc_rel}: broken relative link {raw!r} -> {target}"
