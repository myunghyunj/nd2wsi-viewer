"""The Windows startup repair for builds extracted from a downloaded ZIP."""

import os
import sys
from pathlib import Path

import pytest

from nd2wsi import windows_bundle

ROOT = Path(__file__).resolve().parents[1]


def make_internal(tmp_path):
    internal = tmp_path / "_internal"
    for relative in ("pythonnet/runtime/Python.Runtime.dll", "pythonnet/runtime/netstandard.dll",
                     "clr_loader/ffi/dlls/amd64/ClrLoader.dll", "webview/lib/Microsoft.Web.WebView2.Core.dll",
                     "numpy/core.dll", "python313.dll", "pythonnet/runtime/notes.txt"):
        path = internal / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ")
    return internal


def test_only_the_dotnet_assembly_folders_are_cleared(tmp_path, monkeypatch):
    internal = make_internal(tmp_path)
    removed = []

    def remove(path):
        removed.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(windows_bundle.os, "remove", remove)
    assert windows_bundle.clear_download_marks(internal, platform="win32") == []
    names = sorted(Path(path.removesuffix(":Zone.Identifier")).name for path in removed)
    assert names == ["ClrLoader.dll", "Microsoft.Web.WebView2.Core.dll", "Python.Runtime.dll", "netstandard.dll"]
    assert all(path.endswith(":Zone.Identifier") for path in removed)


def test_a_mark_that_cannot_be_removed_is_reported_not_raised(tmp_path, monkeypatch):
    internal = make_internal(tmp_path)

    def remove(path):
        if "Python.Runtime" in path:
            raise PermissionError(13, "Access is denied")
        raise FileNotFoundError(path)

    monkeypatch.setattr(windows_bundle.os, "remove", remove)
    failures = windows_bundle.clear_download_marks(internal, platform="win32")
    assert len(failures) == 1 and "Python.Runtime.dll" in failures[0] and "denied" in failures[0]


@pytest.mark.parametrize("winerror", [1, 50, 87, 123])
def test_filesystems_without_alternate_data_streams_are_ignored(tmp_path, monkeypatch, winerror):
    internal = make_internal(tmp_path)

    def remove(path):
        err = OSError(22, "The parameter is incorrect", path)
        err.winerror = winerror
        raise err

    monkeypatch.setattr(windows_bundle.os, "remove", remove)
    assert windows_bundle.clear_download_marks(internal, platform="win32") == []


def test_a_missing_bundle_folder_is_not_an_error(tmp_path):
    assert windows_bundle.clear_download_marks(tmp_path / "missing", platform="win32") == []


def test_other_platforms_are_left_alone(tmp_path, monkeypatch):
    internal = make_internal(tmp_path)
    monkeypatch.setattr(windows_bundle.os, "remove", lambda path: pytest.fail("touched a file"))
    assert windows_bundle.clear_download_marks(internal, platform="darwin") == []


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data streams")
def test_the_zone_identifier_stream_is_removed_on_ntfs(tmp_path):
    internal = make_internal(tmp_path)
    assembly = internal / "pythonnet/runtime/Python.Runtime.dll"
    native = internal / "python313.dll"
    for path in (assembly, native):
        with open(str(path) + ":Zone.Identifier", "w", encoding="ascii") as stream:
            stream.write("[ZoneTransfer]\nZoneId=3\n")
    assert windows_bundle.clear_download_marks(internal) == []
    assert not os.path.exists(str(assembly) + ":Zone.Identifier")
    assert assembly.read_bytes() == b"MZ"
    # Only .NET assemblies need clearing; everything else keeps its mark.
    assert os.path.exists(str(native) + ":Zone.Identifier")


def test_the_startup_hint_names_the_unblock_step_only_for_blocked_assemblies():
    blocked = ("Could not start nd2wsi-viewer: Failed to resolve Python.Runtime.Loader.Initialize "
               "from C:\\Users\\a\\Downloads\\nd2wsi-viewer\\_internal\\pythonnet\\runtime\\Python.Runtime.dll")
    hint = windows_bundle.startup_hint(blocked, platform="win32")
    assert "Unblock" in hint and "network drive" in hint
    assert windows_bundle.startup_hint("Could not start nd2wsi-viewer: ClrLoader failed", platform="win32") == hint
    assert windows_bundle.startup_hint("Could not start nd2wsi-viewer: port in use", platform="win32") == ""
    assert windows_bundle.startup_hint(blocked, platform="darwin") == ""


def test_the_launcher_clears_marks_before_anything_imports_the_app():
    launcher = (ROOT / "packaging/launch.py").read_text(encoding="utf-8")
    assert launcher.index("clear_download_marks(Path(sys._MEIPASS))") < launcher.index("from nd2wsi.app import run")
    assert 'sys.platform == "win32" and getattr(sys, "frozen", False)' in launcher
    # Support and CI can keep the marks to reproduce the blocked start.
    assert 'os.environ.get("ND2WSI_KEEP_DOWNLOAD_MARKS") != "1"' in launcher


def test_the_startup_error_carries_the_hint():
    source = (ROOT / "nd2wsi/app.py").read_text(encoding="utf-8")
    failure = source.index('message = f"Could not start {APP_NAME}: {exc}"')
    assert source.index("startup_hint(message)", failure) < source.index("_startup_error(message", failure)
    # The traceback reaches both logs, so a support report shows where it failed.
    assert source.rindex("traceback.format_exc()", 0, failure) > source.rindex("except Exception as exc:", 0, failure)
