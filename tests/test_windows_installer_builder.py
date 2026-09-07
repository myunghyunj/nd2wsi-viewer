"""Guard installer file boundaries before invoking the Windows compiler."""

import importlib.util
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "windows_installer_builder",
    Path(__file__).resolve().parents[1] / "packaging/build_windows_installer.py",
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.mark.parametrize("name", [
    "../outside", "/outside", ".", "", "C:/outside", "a\\b",
    "dir/../outside", "dir//file", "dir/NUL.txt", "dir/COM1", "dir/file.",
    "dir/file ", "dir/stream:secret", "dir/line\nbreak",
])
def test_reject_windows_unsafe_paths(name):
    with pytest.raises(ValueError):
        builder.checked_relative(name)


def make_archive(path, extra):
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("nd2wsi-viewer.exe", "build-info.json", "LICENSE.txt", "READ-ME.txt"):
            archive.writestr("nd2wsi-viewer/" + name, b"test")
        for name, content in extra:
            archive.writestr(name, content)


@pytest.mark.parametrize("extra", [
    [("nd2wsi-viewer/../outside", b"bad")],
    [("nd2wsi-viewer/LICENSE.TXT", b"duplicate")],
    [("nd2wsi-viewer/UNINSTALL.EXE", b"conflict")],
    [("nd2wsi-viewer/.nd2wsi-install.ini", b"conflict")],
    [("nd2wsi-viewer/a", b"file"), ("nd2wsi-viewer/a/b", b"child")],
])
def test_rejected_archive_leaves_no_payload(tmp_path, extra):
    archive, payload = tmp_path / "input.zip", tmp_path / "payload"
    make_archive(archive, extra)
    with pytest.raises(ValueError):
        builder.extract_payload(archive, payload)
    assert not payload.exists()
    assert not (tmp_path / "outside").exists()


def test_uninstall_lists_only_verified_files_and_empty_directories(tmp_path):
    archive, payload = tmp_path / "input.zip", tmp_path / "payload"
    make_archive(archive, [("nd2wsi-viewer/_internal/space name/$asset.dll", b"runtime")])
    files = builder.extract_payload(archive, payload)
    (payload / "user.nd2").write_bytes(b"user research")
    install, remove, guard = builder.emit_includes(payload, files, tmp_path)
    text = remove.read_text()
    assert 'ND2WSI_DELETE_FILE "_internal\\space name\\$$asset.dll"' in text
    assert "user.nd2" not in text
    assert "/r" not in text
    assert 'ND2WSI_REMOVE_EMPTY_DIR "_internal\\space name"' in text
    assert "$$asset.dll" in install.read_text()
    assert 'ND2WSI_GUARD_PATH "_internal\\space name\\$$asset.dll"' in guard.read_text()
    assert 'ND2WSI_GUARD_PATH "_internal\\space name"' in guard.read_text()
    assert "user.nd2" not in guard.read_text()
    assert (payload / "user.nd2").read_bytes() == b"user research"
