"""The tidy Windows portable folder: layout, pruning, assemblies and licences."""

import importlib.util
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    spec = importlib.util.spec_from_file_location("layout_build_windows", ROOT / "packaging/build_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def pe_image(managed: bool) -> bytes:
    """A minimal PE32+ header; the CLR runtime header is data directory 14."""
    image = bytearray(1024)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x80)
    image[0x80:0x84] = b"PE\0\0"
    optional = 0x80 + 24
    struct.pack_into("<H", image, optional, 0x20B)
    struct.pack_into("<I", image, optional + 108, 16)
    if managed:
        struct.pack_into("<II", image, optional + 112 + 14 * 8, 0x2008, 0x48)
    return bytes(image)


def test_pruning_drops_test_suites_and_caches_but_keeps_testing_packages(builder, tmp_path):
    internal = tmp_path / "_internal"
    write(internal / "scipy/stats/tests/test_a.py")
    write(internal / "scipy/stats/tests/data/b.npz")
    write(internal / "scipy/stats/__pycache__/_bws_test.cpython-313-pytest-9.1.1.pyc.10044")
    keep = [write(internal / "numpy/testing/__init__.py"), write(internal / "zarr/testing/store.py"),
            write(internal / "scipy/stats/_stats.pyd"), write(internal / "nd2wsi/static/test.js")]
    assert builder.prune_bundle(internal) == 3
    assert not (internal / "scipy/stats/tests").exists()
    assert not (internal / "scipy/stats/__pycache__").exists()
    assert all(path.is_file() for path in keep)


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs a privilege on Windows")
def test_pruning_refuses_a_linked_folder(builder, tmp_path):
    internal, outside = tmp_path / "_internal", tmp_path / "outside"
    write(outside / "keep.txt")
    (internal / "pkg").mkdir(parents=True)
    os.symlink(outside, internal / "pkg" / "tests", target_is_directory=True)
    with pytest.raises(RuntimeError, match="linked"):
        builder.prune_bundle(internal)
    assert (outside / "keep.txt").is_file()


def test_dotnet_assemblies_are_recognised_by_their_clr_header(builder, tmp_path):
    assert builder.is_dotnet_assembly(write(tmp_path / "managed.dll", pe_image(True)))
    assert not builder.is_dotnet_assembly(write(tmp_path / "native.dll", pe_image(False)))
    assert not builder.is_dotnet_assembly(write(tmp_path / "text.dll", b"not a PE image"))
    assert not builder.is_dotnet_assembly(write(tmp_path / "short.dll", b"MZ"))


def test_an_assembly_outside_the_cleared_folders_fails_the_build(builder, tmp_path):
    internal = tmp_path / "_internal"
    write(internal / "pythonnet/runtime/Python.Runtime.dll", pe_image(True))
    write(internal / "webview/lib/Microsoft.Web.WebView2.Core.dll", pe_image(True))
    write(internal / "python313.dll", pe_image(False))
    builder.check_assembly_locations(internal)
    write(internal / "someplugin/Managed.dll", pe_image(True))
    with pytest.raises(RuntimeError, match="someplugin/Managed.dll"):
        builder.check_assembly_locations(internal)


def test_licences_cover_the_app_closure_and_the_bootloader_but_not_dev_tools(builder):
    pytest.importorskip("packaging")
    shipped = builder.shipped_distributions()
    assert {"nd2wsi-viewer", "numpy", "pyinstaller", "pyinstaller-hooks-contrib"} <= shipped
    assert not {"pytest", "pytest-xdist", "ruff", "pluggy", "iniconfig", "execnet"} & shipped


def test_a_bundled_dev_tool_fails_the_licence_step(builder, tmp_path):
    pytest.importorskip("packaging")
    internal = tmp_path / "_internal"
    (internal / "numpy-2.5.2.dist-info").mkdir(parents=True)
    (internal / "pytest-9.1.1.dist-info").mkdir()
    with pytest.raises(RuntimeError, match="pytest-9.1.1.dist-info"):
        builder.copy_licenses(internal, tmp_path / "licenses")


def make_bundle(root):
    write(root / "nd2wsi-viewer.exe")
    write(root / "READ-ME.txt")
    write(root / "_internal/nd2wsi-viewer-about/build-info.json")
    return root


def test_the_portable_folder_shows_only_the_exe_runtime_and_readme(builder, tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path / "ok")
    assert builder.check_layout(bundle) == 3
    write(bundle / "build-info.json")
    with pytest.raises(RuntimeError, match="build-info.json"):
        builder.check_layout(bundle)
    leftover = make_bundle(tmp_path / "leftover")
    (leftover / "_internal/scipy/tests").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="scipy/tests"):
        builder.check_layout(leftover)
    monkeypatch.setattr(builder, "MAX_BUNDLE_FILES", 2)
    with pytest.raises(RuntimeError, match="3 files"):
        builder.check_layout(make_bundle(tmp_path / "crowded"))


def test_the_build_excludes_tk_and_pytest_and_stages_notices_inside_the_runtime(builder):
    assert {"tkinter", "_tkinter", "pytest", "_pytest"} <= set(builder.EXCLUDED_MODULES)
    source = (ROOT / "packaging/build_windows.py").read_text(encoding="utf-8")
    assert '"--exclude-module", module' in source
    assert "import _tkinter" not in source
    assert "stage(sdpc_manifest, about, out, release_version)" in source
    # Microsoft's WebView2 runtime ships unmodified, so pruning runs before it is staged.
    assert source.index("prune_bundle(internal)") < source.index("stage_webview2_runtime(webview2_source")
    assert '(about / "WEBVIEW2-RUNTIME-NOTICE.txt")' in source
    assert source.index("prune_bundle(internal)") < source.index("smoke(exe, source")
    assert source.index("check_layout(bundle)") < source.index("archive = out /")


def test_the_verifier_reproduces_and_checks_the_download_mark():
    verifier = (ROOT / "packaging/verify_windows.ps1").read_text(encoding="utf-8")
    assert "-Stream 'Zone.Identifier'" in verifier and "ZoneId=3" in verifier
    loop = verifier.index("foreach ($step in $plan)")
    launch = verifier.index("$process = [System.Diagnostics.Process]::Start($start)", loop)
    assert loop < verifier.index("Set-DownloadMark $appRoot") < launch
    control = verifier.index("$env:ND2WSI_KEEP_DOWNLOAD_MARKS = '1'")
    assert verifier.index("Set-DownloadMark $controlApp") < control
    assert verifier.index("Remove-Item Env:ND2WSI_KEEP_DOWNLOAD_MARKS", control) > control
    assert "-notmatch 'Python\\.Runtime'" in verifier and "-notmatch 'Unblock'" in verifier
    assert "_internal|nd2wsi-viewer.exe|READ-ME.txt" in verifier
