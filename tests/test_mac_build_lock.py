"""The macOS bundle installs the locked dependency set, never a fresh resolve.

2.5.4 shipped limnd2 0.4.3 and PyInstaller 6.22.3 on macOS while every test
and the Windows build used the locked 0.4.0 and 6.22.2. These checks read the
build inputs only; no native build runs.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def _pins(path):
    return dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", path.read_text(), re.M))


def test_mac_bundle_installs_runtime_from_uv_lock_and_tools_from_a_hashed_lock():
    script = (PACKAGING / "build_mac_app.sh").read_text()
    assert "uv export --project \"$REPO\" --frozen" in script
    for extra in ("app", "svs", "nd2export"):
        assert f"--extra {extra}" in script
    assert ('pip -q install --require-hashes -r "$RUNTIME_LOCK" '
            '-r "$HERE/macos-build-requirements.txt"') in script
    # every other install is the project itself or the verified codec wheel,
    # neither of which may pull dependencies from an index
    for line in re.findall(r"pip -q install [^\n]*", script):
        assert ("--require-hashes" in line or "--no-deps" in line
                or line.endswith("--upgrade pip")), line


def test_mac_build_tools_match_the_windows_pins_and_carry_hashes():
    mac_in, windows_in = (_pins(PACKAGING / f"{os}-build-requirements.in") for os in ("macos", "windows"))
    assert mac_in == windows_in
    locked = (PACKAGING / "macos-build-requirements.txt").read_text()
    assert "--python-platform aarch64-apple-darwin" in locked
    assert all(_pins(PACKAGING / "macos-build-requirements.txt")[name] == version
               for name, version in mac_in.items())
    # every pinned requirement is followed by its hashes
    assert len(re.findall(r"^[A-Za-z0-9_.-]+==", locked, re.M)) == len(
        re.findall(r"^[A-Za-z0-9_.-]+==\S+ \\\n    --hash=sha256:", locked, re.M))


def test_release_workflow_fails_when_packaged_versions_drift_from_the_lock():
    workflow = (ROOT / ".github" / "workflows" / "macos-release.yml").read_text()
    assert "packaged dependencies differ from uv.lock" in workflow
