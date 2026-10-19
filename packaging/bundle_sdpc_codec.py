#!/usr/bin/env python3
"""Stage verified SDPC codec provenance, licenses, and matching source assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def manifest_path(value: str | Path | None = None) -> Path:
    value = value or os.environ.get("ND2WSI_SDPC_CODEC_MANIFEST")
    if not value:
        raise ValueError("Set ND2WSI_SDPC_CODEC_MANIFEST to the verified source-built LGPL codec manifest")
    return Path(value).expanduser().resolve(strict=True)


def verified_manifest(path: Path, *, wheel_only: bool = False) -> dict:
    command = [sys.executable, str(HERE / "verify_sdpc_codec.py"), "--manifest", str(path)]
    if wheel_only:
        command.append("--wheel-only")
    subprocess.run(command, check=True, stdout=sys.stderr)
    return json.loads(path.read_text(encoding="utf-8"))


def artifact_path(manifest: Path, relative: str) -> Path:
    path = (manifest.parent / relative).resolve(strict=True)
    if not path.is_relative_to(manifest.parent) or path == manifest.parent:
        raise ValueError("Codec artifact must be inside its verified build directory")
    return path


def stage(path: Path, resources: Path, output: Path, version: str) -> dict:
    manifest = verified_manifest(path)
    source = artifact_path(path, manifest["source_archive_file"])
    with source.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != manifest["source_archive_sha256"]:
        raise ValueError("SDPC codec source archive checksum changed before packaging")
    resources.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, resources / "sdpc-codec-manifest.json")
    shutil.copy2(HERE / "SDPC-CODEC-NOTICE.txt", resources / "SDPC-CODEC-NOTICE.txt")
    shutil.copytree(artifact_path(path, manifest["licenses_directory"]),
                    resources / "sdpc-codec-licenses", dirs_exist_ok=True)
    target = output / source.name
    if source != target.resolve():
        shutil.copy2(source, target)
    (output / f"{source.name}.sha256").write_text(f"{actual}  {source.name}\n", encoding="ascii")
    location = {
        "source_archive": source.name,
        "source_archive_sha256": actual,
        "source_url": f"https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v{version}/{source.name}",
        "native_hash_scope": "verified codec wheel before platform signing and packaging",
    }
    (resources / "sdpc-codec-source.json").write_text(
        json.dumps(location, indent=2) + "\n", encoding="utf-8")
    return location


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("wheel", "stage"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--resources", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version")
    args = parser.parse_args()
    path = manifest_path(args.manifest)
    if args.action == "wheel":
        manifest = verified_manifest(path, wheel_only=True)
        print(artifact_path(path, manifest["wheel_file"]))
    else:
        if args.resources is None or args.output is None or not args.version:
            parser.error("stage requires --resources, --output, and --version")
        print(json.dumps(stage(path, args.resources, args.output, args.version), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
