"""Exercise the actual plist writers without building or installing an app."""

import plistlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def write_release_plist(tmp_path, builder, version):
    text = (ROOT / "packaging" / builder).read_text()
    if builder == "build_mac_app.sh":
        script = text.split("<< 'PLISTEOF'\n", 1)[1].split("\nPLISTEOF", 1)[0]
    else:
        script = text.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        # The Metal builder reads installed metadata; isolate that input from
        # whichever package happens to be installed in the test environment.
        script = ("import importlib.metadata\n"
                  f"importlib.metadata.version = lambda name: {version!r}\n" + script)
    target = tmp_path / "Info.plist"
    original = plistlib.dumps({"CFBundleIdentifier": "com.nd2wsi.viewer"})
    target.write_bytes(original)
    project = tmp_path / "pyproject.toml"
    project.write_text(f'[project]\nversion = "{version}"\n')
    completed = subprocess.run([sys.executable, "-", str(target), str(project)],
                               input=script, text=True, capture_output=True, timeout=30)
    return completed, target, original


@pytest.mark.parametrize("builder", ["build_mac_app.sh", "build_metal_rc.sh"])
@pytest.mark.parametrize("version,bundle_version", [
    ("2.5.0", "2.5.0"), ("2.5.0b1", "2.5.0b1"),
    ("2.5.0b255", "2.5.0b255"), ("2.5.0rc2", "2.5.0fc2"),
])
def test_mac_release_versions_preserve_package_and_stage(tmp_path, builder, version, bundle_version):
    completed, target, _ = write_release_plist(tmp_path, builder, version)
    assert completed.returncode == 0, completed.stderr
    info = plistlib.loads(target.read_bytes())
    assert info["CFBundleShortVersionString"] == "2.5.0"
    assert info["CFBundleVersion"] == bundle_version
    assert info["ND2WSIPackageVersion"] == version
    assert info["CFBundleIdentifier"] == "com.nd2wsi.viewer"
    assert info["ND2WSIDevelopmentOnly"] is False


@pytest.mark.parametrize("builder", ["build_mac_app.sh", "build_metal_rc.sh"])
@pytest.mark.parametrize("version", ["2.5.0b0", "2.5.0b256", "2.5.0b"])
def test_invalid_prerelease_version_leaves_plist_unchanged(tmp_path, builder, version):
    completed, target, original = write_release_plist(tmp_path, builder, version)
    assert completed.returncode != 0
    assert target.read_bytes() == original


def test_project_and_locked_package_version_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    locked = next(item for item in packages if item["name"] == project["name"])
    assert project["version"] == locked["version"]
