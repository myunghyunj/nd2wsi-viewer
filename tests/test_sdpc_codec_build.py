"""Fail-closed provenance checks, independent of native build tools."""
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

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
