"""Require the verified source-built decoder and its distribution evidence."""

import hashlib
import importlib.util
import json
import plistlib
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_sdpc_codec_is_optional_and_pinned():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["optional-dependencies"]["sdpc"] == ["av==16.0.1"]
    assert not any(item.startswith("av") for item in project["dependencies"])
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    av = next(item for item in lock["package"] if item["name"] == "av")
    assert av["version"] == "16.0.1"
    assert av["source"]["registry"] == "https://pypi.org/simple"


def windows_builder():
    spec = importlib.util.spec_from_file_location("sdpc_build_windows", ROOT / "packaging/build_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_release_requires_a_verified_codec_even_if_old_flag_disables_it(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    import bundle_sdpc_codec

    module = windows_builder()
    monkeypatch.delenv("ND2WSI_SDPC_CODEC_MANIFEST", raising=False)
    with pytest.raises(ValueError, match="source-built LGPL"):
        module.sdpc_bundle_options()
    manifest = tmp_path / "sdpc-codec-manifest.json"
    manifest.write_text("{}")
    verify = Mock(return_value={})
    monkeypatch.setattr(bundle_sdpc_codec, "verified_manifest", verify)
    monkeypatch.setenv("ND2WSI_SDPC_CODEC_MANIFEST", str(manifest))
    monkeypatch.setenv("ND2WSI_BUNDLE_SDPC", "0")
    path, args = module.sdpc_bundle_options()
    assert path == manifest and args == ["--collect-all", "av"]
    verify.assert_called_once_with(manifest)
    verify.side_effect = ValueError("unexpected GPL runtime")
    with pytest.raises(ValueError, match="unexpected GPL runtime"):
        module.sdpc_bundle_options()


@pytest.mark.parametrize("builder", ["build_mac_app.sh", "build_metal_rc.sh"])
def test_mac_sdpc_association_and_release_marker(tmp_path, builder):
    """Execute only the pure plist writer, never the native builder."""
    text = (ROOT / "packaging" / builder).read_text()
    if builder == "build_mac_app.sh":
        source = text.split("<< 'PLISTEOF'\n", 1)[1].split("\nPLISTEOF", 1)[0]
    else:
        source = text.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    target = tmp_path / "Info.plist"
    target.write_bytes(plistlib.dumps({}))
    subprocess.run([sys.executable, "-", str(target), str(ROOT / "pyproject.toml")],
                   input=source, text=True, check=True)
    info = plistlib.loads(target.read_bytes())
    imported = {item["UTTypeIdentifier"]: item for item in info["UTImportedTypeDeclarations"]}
    assert imported["com.sqray.sdpc"]["UTTypeTagSpecification"]["public.filename-extension"] == ["sdpc"]
    assert [item["UTTypeIdentifier"] for item in info["UTExportedTypeDeclarations"]] == ["com.nd2wsi.nd2svs"]
    assert any("com.sqray.sdpc" in item["LSItemContentTypes"] for item in info["CFBundleDocumentTypes"])
    assert info["ND2WSISdpcBundled"] is True
    assert info["ND2WSIDevelopmentOnly"] is False
    assert "--collect-all av" in text and "--exclude-module av" not in text
    assert 'verify_sdpc_codec.py" --manifest "$ND2WSI_SDPC_CODEC_MANIFEST"' in text
    assert "smoke ok: SDPC" in text
    if builder == "build_metal_rc.sh":
        assert info["LSMinimumSystemVersion"] == "12.0"


def test_codec_resources_bind_source_asset_checksum_and_release_url(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    import bundle_sdpc_codec

    built = tmp_path / "codec"
    built.mkdir()
    source = built / "sdpc-codec-sources-windows-x64.zip"
    source.write_bytes(b"verified source archive fixture")
    licenses = built / "licenses"
    licenses.mkdir()
    (licenses / "LGPL.txt").write_text("license fixture")
    metadata = {"source_archive_file": source.name,
                "source_archive_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "licenses_directory": "licenses"}
    manifest = built / "sdpc-codec-manifest.json"
    manifest.write_text(json.dumps(metadata))
    verify = Mock(return_value=metadata)
    monkeypatch.setattr(bundle_sdpc_codec, "verified_manifest", verify)
    resources, output = tmp_path / "app-resources", tmp_path / "release-assets"
    result = bundle_sdpc_codec.stage(manifest, resources, output, "2.3.0")
    verify.assert_called_once_with(manifest)
    assert (output / source.name).read_bytes() == source.read_bytes()
    assert not (resources / source.name).exists()
    assert (resources / "sdpc-codec-manifest.json").read_bytes() == manifest.read_bytes()
    assert (resources / "sdpc-codec-licenses/LGPL.txt").read_text() == "license fixture"
    assert result["source_url"].endswith("/v2.3.0/sdpc-codec-sources-windows-x64.zip")
    assert json.loads((resources / "sdpc-codec-source.json").read_text()) == result
    source.write_bytes(b"changed source archive")
    with pytest.raises(ValueError, match="checksum changed"):
        bundle_sdpc_codec.stage(manifest, resources, output, "2.3.0")


@pytest.mark.parametrize("sdpc_success", [False, True])
def test_windows_packaged_smoke_requires_actual_sdpc_success(monkeypatch, tmp_path, sdpc_success):
    module = windows_builder()

    def run(command, *, env, timeout):
        marker = "smoke ok: SDPC" if sdpc_success else "smoke ok: ND2"
        Path(env["ND2WSI_LOG_FILE"]).write_text(marker)
        if "--gui-smoke" in command:
            Path(command[-1]).write_text('{"ok":true}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    if sdpc_success:
        module.smoke(tmp_path / "viewer.exe", tmp_path / "input.nd2", tmp_path / "evidence")
    else:
        with pytest.raises(RuntimeError, match="SDPC decoder and ROI"):
            module.smoke(tmp_path / "viewer.exe", tmp_path / "input.nd2", tmp_path / "evidence")


def test_windows_verifies_same_archive_on_x64_and_arm64_after_codec_build():
    workflow = (ROOT / ".github/workflows/windows.yml").read_text()
    assert workflow.index("Windows unit and integration tests") < workflow.index("Build and install the source-verified LGPL SDPC decoder")
    assert workflow.index("Windows real-data tests") < workflow.index("Build and install the source-verified LGPL SDPC decoder")
    assert "platform: x64" in workflow and "platform: arm64-emulation" in workflow
    assert "name: sdpc-codec-sources-windows-x64" in workflow
    verifier = (ROOT / "packaging/verify_windows.ps1").read_text()
    assert "-notmatch 'smoke ok: SDPC'" in verifier
    assert "sdpc_codec_manifest_sha256" in verifier
