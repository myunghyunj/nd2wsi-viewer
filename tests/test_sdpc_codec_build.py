"""Fail-closed provenance checks, independent of native build tools."""
import hashlib
import importlib.util
import json
import shlex
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", ["../av/lib.so", "/av/lib.so", "av/../../lib.so", "av\\lib.dll"])
def test_manifest_paths_cannot_escape_build_root(path):
    verifier = load_script("verify_sdpc_codec")
    with pytest.raises(RuntimeError, match="Unsafe path"):
        verifier.safe_relative(path)


def minimal_manifest(verifier):
    return {"schema_version": 1, "ffmpeg_version": "8.1.3", "pyav_version": "16.0.1",
            "license": "LGPL-2.1-or-later", "configure_args": list(verifier.REQUIRED_FLAGS),
            "sources": [{"filename": name, "sha256": value}
                        for name, value in verifier.EXPECTED_SOURCES.items()]}


@pytest.mark.parametrize("flag", ["--enable-gpl", "--enable-nonfree", "--enable-version3", "--enable-libx265"])
def test_manifest_rejects_non_lgpl_decoder_configuration(tmp_path, flag):
    verifier = load_script("verify_sdpc_codec")
    manifest = minimal_manifest(verifier)
    manifest["configure_args"].append(flag)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="Non-release codec configuration"):
        verifier.verify_manifest(path)


def test_manifest_requires_the_reviewed_source_hashes(tmp_path):
    verifier = load_script("verify_sdpc_codec")
    manifest = minimal_manifest(verifier)
    manifest["sources"][0]["sha256"] = "0" * 64
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="reviewed pinned releases"):
        verifier.verify_manifest(path)


def test_cached_sources_are_verified_before_extraction(tmp_path):
    builder = load_script("build_sdpc_codec")
    path = tmp_path / "source.tar.xz"
    path.write_bytes(b"source fixture")
    source = {"filename": path.name, "url": "https://invalid.example/unused",
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert builder.download(source, tmp_path) == path
    path.write_bytes(b"changed source")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        builder.download(source, tmp_path)


def test_native_manifest_hashes_actual_wheel_binaries_only(tmp_path):
    builder = load_script("build_sdpc_codec")
    path = tmp_path / "av.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("av/core.so", b"extension")
        archive.writestr("av/.dylibs/libavcodec.dylib", b"decoder")
        archive.writestr("av/__init__.py", b"python")
    assert builder.native_hashes(path) == {
        "av/core.so": hashlib.sha256(b"extension").hexdigest(),
        "av/.dylibs/libavcodec.dylib": hashlib.sha256(b"decoder").hexdigest(),
    }


def test_windows_ffmpeg_uses_selected_msys_make_and_preserves_quoting(tmp_path, monkeypatch):
    builder = load_script("build_sdpc_codec")
    bash = tmp_path / "action-managed MSYS2" / "usr" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    (bash.parent / "make.exe").touch()
    compiler = tmp_path / "Visual Studio" / "bin" / "cl.exe"
    source = tmp_path / "FFmpeg's source"
    arguments = ["--prefix=D:/build output/FFmpeg's install", "--toolchain=msvc"]
    calls = []
    monkeypatch.setattr(builder, "os", SimpleNamespace(name="nt", pathsep=";"))
    monkeypatch.setattr(builder.shutil, "which", lambda name: str(compiler) if name == "cl.exe" else None)
    monkeypatch.setattr(builder, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    builder.make_ffmpeg(source, arguments, 6, {"PATH": "native-make-directory"}, str(bash))

    expected_commands = [["./configure", *arguments],
                         ["/usr/bin/make", "SHELL=/usr/bin/bash", "-j6"],
                         ["/usr/bin/make", "SHELL=/usr/bin/bash", "install"]]
    assert len(calls) == len(expected_commands)
    for (command, kwargs), expected in zip(calls, expected_commands):
        assert command[:4] == [str(bash), "--noprofile", "--norc", "-c"]
        assert shlex.split(command[4]) == ["cd", source.resolve().as_posix(), "&&", *expected]
        assert kwargs["env"]["PATH"] == f"{compiler.parent};{bash.parent};native-make-directory"
        assert kwargs["env"]["SHELL"] == "/usr/bin/bash"
        assert kwargs["env"]["MSYSTEM"] == "MSYS"


def test_windows_ffmpeg_rejects_msys_without_make_before_build(tmp_path, monkeypatch):
    builder = load_script("build_sdpc_codec")
    bash = tmp_path / "bash.exe"
    bash.touch()
    monkeypatch.setattr(builder, "os", SimpleNamespace(name="nt"))
    with pytest.raises(RuntimeError, match="Install make in the selected MSYS2"):
        builder.make_ffmpeg(tmp_path, [], 1, {"PATH": "native-make-directory"}, str(bash))


def test_non_windows_ffmpeg_retains_native_make_commands(tmp_path, monkeypatch):
    builder = load_script("build_sdpc_codec")
    calls = []
    env = {"PATH": "native-build-tools"}
    monkeypatch.setattr(builder, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(builder, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    builder.make_ffmpeg(tmp_path, ["--enable-shared"], 6, env, None)
    assert calls == [
        ([str(tmp_path / "configure"), "--enable-shared"], {"cwd": tmp_path, "env": env}),
        (["make", "-j6"], {"cwd": tmp_path, "env": env}),
        (["make", "install"], {"cwd": tmp_path, "env": env}),
    ]


def test_windows_workflow_passes_action_managed_msys_bash():
    workflow = (ROOT / ".github/workflows/windows.yml").read_text(encoding="utf-8")
    assert "- uses: msys2/setup-msys2@v2\n        id: msys2" in workflow
    assert "MSYS2_LOCATION: ${{ steps.msys2.outputs.msys2-location }}" in workflow
    assert "$msysBash = Join-Path $env:MSYS2_LOCATION 'usr\\bin\\bash.exe'" in workflow
    assert "--jobs 6 --msys-bash $msysBash" in workflow


@pytest.mark.parametrize("windows", [False, True])
def test_pyav_build_adds_msvc_import_library_directory_only_on_windows(tmp_path, monkeypatch, windows):
    builder = load_script("build_sdpc_codec")
    source = tmp_path / "PyAV source"
    prefix = tmp_path / "FFmpeg install"
    wheel_dir = tmp_path / "wheels"
    env = {"PATH": "native-build-tools"}
    calls = []
    monkeypatch.setattr(builder, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    builder.build_pyav(source, prefix, "python", wheel_dir, 6, env, windows=windows)
    build_options = ["build_ext", "-j6"]
    if windows:
        build_options.append(f"--library-dirs={prefix / 'bin'}")
    assert calls == [(["python", "setup.py", f"--ffmpeg-dir={prefix}", *build_options,
                      "bdist_wheel", "--dist-dir", str(wheel_dir)], {"cwd": source, "env": env})]
