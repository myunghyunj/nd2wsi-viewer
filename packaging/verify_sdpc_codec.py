#!/usr/bin/env python3
"""Fail closed unless the installed SDPC codec matches its source-build record."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

EXPECTED_SOURCES = {
    "ffmpeg-8.1.3.tar.xz": "7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3",
    "av-16.0.1.tar.gz": "dd2ce779fa0b5f5889a6d9e00fbbbc39f58e247e52d31044272648fe16ff1dbf",
}
REQUIRED_FLAGS = {
    "--disable-autodetect", "--disable-everything", "--disable-gpl", "--disable-nonfree",
    "--disable-version3", "--disable-static", "--enable-shared", "--disable-network",
    "--enable-decoder=hevc", "--enable-parser=hevc", "--enable-demuxer=hevc",
}
FORBIDDEN_CONFIGURATION = ("--enable-gpl", "--enable-nonfree", "--enable-version3", "--enable-lib")
NATIVE_SUFFIXES = (".so", ".dylib", ".dll", ".pyd")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and "\\" not in value,
            f"Unsafe path in codec manifest: {value}")
    return path


def verify_manifest(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("schema_version") == 1, "Unknown codec manifest schema")
    require(manifest.get("ffmpeg_version") == "8.1.3", "Unexpected FFmpeg version")
    require(manifest.get("pyav_version") == "16.0.1", "Unexpected PyAV version")
    require(manifest.get("license") == "LGPL-2.1-or-later", "Unexpected codec license")
    sources = {item["filename"]: item["sha256"] for item in manifest.get("sources", [])}
    require(sources == EXPECTED_SOURCES, "Source hashes are not the reviewed pinned releases")
    flags = manifest.get("configure_args", [])
    require(REQUIRED_FLAGS.issubset(flags), "Required restricted decoder build flags are missing")
    require(not any(forbidden in flag for flag in flags for forbidden in FORBIDDEN_CONFIGURATION),
            "Non-release codec configuration in manifest")
    native = manifest.get("native_files", {})
    require(bool(native), "Codec manifest contains no native files")
    for name in native:
        safe_relative(name)
        require(name.lower().endswith(NATIVE_SUFFIXES), f"Unexpected native file: {name}")
        require(not any(word in name.lower() for word in ("x264", "x265", "msys-", "cygwin")),
                f"Unapproved bundled native dependency: {name}")
    root = manifest_path.parent
    wheel = root / safe_relative(manifest["wheel_file"])
    require(wheel.is_file() and digest(wheel) == manifest["wheel_sha256"], "Codec wheel hash mismatch")
    with zipfile.ZipFile(wheel) as archive:
        actual = {name: hashlib.sha256(archive.read(name)).hexdigest()
                  for name in archive.namelist() if name.lower().endswith(NATIVE_SUFFIXES)}
    require(actual == native, "Native wheel content does not match the codec manifest")
    bundle = root / safe_relative(manifest["source_archive_file"])
    require(bundle.is_file() and digest(bundle) == manifest["source_archive_sha256"],
            "Corresponding-source archive is missing or has changed")
    with zipfile.ZipFile(bundle) as archive:
        for name, expected in EXPECTED_SOURCES.items():
            require(hashlib.sha256(archive.read("sources/" + name)).hexdigest() == expected,
                    f"Corresponding source is incorrect: {name}")
    licenses = root / safe_relative(manifest["licenses_directory"])
    for name in ("FFmpeg-LGPL-2.1.txt", "PyAV-BSD-3-Clause.txt", "SDPC-CODEC-NOTICE.txt"):
        require((licenses / name).is_file(), f"Missing codec notice/license: {name}")
    return manifest


def verify_macos_native(path: Path) -> None:
    archs = subprocess.check_output(["lipo", "-archs", str(path)], text=True).split()
    require(archs == ["arm64"], f"Unexpected architecture in {path.name}: {archs}")
    details = subprocess.check_output(["otool", "-l", str(path)], text=True)
    versions = re.findall(r"\bminos\s+(\d+(?:\.\d+){0,2})", details)
    if not versions:
        versions = re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+){0,2})", details)
    require(bool(versions), f"Missing macOS deployment target in {path.name}")
    for value in versions:
        version = tuple(int(part) for part in value.split("."))
        require((version + (0, 0))[:2] <= (12, 0), f"{path.name} requires macOS {value}, above 12.0")


def verify_installed(manifest: dict) -> dict:
    av = importlib.import_module("av")
    require(av.__version__ == manifest["pyav_version"], "Installed PyAV version mismatch")
    site_packages = Path(av.__file__).resolve().parent.parent
    for name, expected in manifest["native_files"].items():
        path = site_packages / safe_relative(name)
        require(path.is_file() and digest(path) == expected, f"Installed codec file mismatch: {name}")
        if platform.system() == "Darwin":
            verify_macos_native(path)
    actual = set()
    for directory in (site_packages / "av", site_packages / "av.libs"):
        if directory.is_dir():
            actual.update(path.relative_to(site_packages).as_posix() for path in directory.rglob("*")
                          if path.is_file() and path.name.lower().endswith(NATIVE_SUFFIXES))
    require(actual == set(manifest["native_files"]), "Unexpected or missing installed codec native files")
    metadata = av._core.library_meta
    require(len(metadata) == 7, "Expected the seven FFmpeg shared core libraries")
    for name, item in metadata.items():
        require(item["license"] == "LGPL version 2.1 or later", f"Unexpected {name} license: {item['license']}")
        configuration = item["configuration"]
        require(not any(flag in configuration for flag in FORBIDDEN_CONFIGURATION), f"Unapproved runtime configuration in {name}")
        require(all(flag in configuration for flag in REQUIRED_FLAGS), f"Missing runtime restrictions in {name}")
    require(av.Codec("hevc", "r").is_decoder, "Native HEVC decoder is unavailable")
    require("libx264" not in av.codecs_available and "libx265" not in av.codecs_available,
            "Unexpected external encoders in decoder-only codec")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wheel-only", action="store_true", help="verify deliverables before wheel installation")
    args = parser.parse_args()
    manifest = verify_manifest(args.manifest.resolve())
    if not args.wheel_only:
        verify_installed(manifest)
    print(json.dumps({"sdpc_codec_verified": True, "platform": manifest["platform"],
                      "ffmpeg": manifest["ffmpeg_version"], "pyav": manifest["pyav_version"],
                      "license": manifest["license"], "native_files": len(manifest["native_files"]),
                      "installed": not args.wheel_only}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, OSError, ValueError, ImportError, zipfile.BadZipFile) as error:
        print(f"SDPC codec verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
