#!/usr/bin/env python3
"""Wrap a checksum-verified Windows portable ZIP in a per-user NSIS installer.

This does not rebuild the app or publish anything. Run the resulting installer
through verify_windows_installer.ps1 before delivering it to another computer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
APP = "nd2wsi-viewer"
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or not path.parts or path.is_absolute() or str(path) != name or ".." in path.parts:
        raise ValueError(f"Noncanonical payload path: {name!r}")
    for part in path.parts:
        if (part.endswith((".", " ")) or part.split(".")[0].upper() in RESERVED
                or any(ord(c) < 32 or c in '<>:"\\|?*' for c in part)):
            raise ValueError(f"Invalid Windows payload path: {name!r}")
    return path


def nsis_quote(value: str | Path) -> str:
    text = str(value)
    if any(ord(c) < 32 for c in text):
        raise ValueError("Control characters are not allowed in installer paths")
    return '"' + text.replace("$", "$$").replace('"', '$\\"') + '"'


def extract_payload(archive: Path, destination: Path) -> dict[str, str]:
    """Extract only canonical, unique regular payload files into a new folder."""
    if destination.exists():
        raise ValueError(f"Payload staging directory already exists: {destination}")
    names: set[str] = set()
    entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            raw = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
            path = checked_relative(raw)
            if path.parts[0] != APP:
                raise ValueError(f"Unexpected archive root: {raw}")
            if len(path.parts) == 1:
                if not entry.is_dir():
                    raise ValueError("Archive root must be a directory")
                continue
            relative = PurePosixPath(*path.parts[1:])
            key = str(relative).casefold()
            if key in {"uninstall.exe", ".nd2wsi-install.ini"}:
                raise ValueError(f"Payload conflicts with installer ownership files: {relative}")
            if key in names:
                raise ValueError(f"Case-insensitive duplicate archive path: {relative}")
            names.add(key)
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"Nonregular archive entry: {raw}")
            if not entry.is_dir():
                entries.append((entry, relative))
        # Validate file/directory collisions before writing anything.
        file_names = {str(relative).casefold() for _, relative in entries}
        for _, relative in entries:
            if any(str(parent).casefold() in file_names
                   for parent in relative.parents if str(parent) != "."):
                raise ValueError(f"File/directory collision: {relative}")
        required = {f"{APP}.exe", "build-info.json", "LICENSE.txt", "READ-ME.txt"}
        if not required.issubset({str(relative) for _, relative in entries}):
            raise ValueError("Archive is missing required executable/provenance files")
        destination.mkdir(parents=True)
        files = {}
        for entry, relative in entries:
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as stream, target.open("xb") as output:
                while block := stream.read(1024 * 1024):
                    output.write(block)
            files[str(relative)] = sha256(target)
    return dict(sorted(files.items()))


def emit_includes(payload: Path, files: dict[str, str], work: Path) -> tuple[Path, Path, Path]:
    install, remove = [], []
    directories: set[PurePosixPath] = set()
    previous_parent = None
    for relative, expected in sorted(files.items()):
        path = checked_relative(relative)
        origin = payload.joinpath(*path.parts)
        if origin.is_symlink() or sha256(origin) != expected:
            raise ValueError(f"Payload changed before compilation: {relative}")
        if path.parent != previous_parent:
            suffix = "" if str(path.parent) == "." else "\\" + str(path.parent).replace("/", "\\")
            install.append('SetOutPath "$INSTDIR' + suffix.replace("$", "$$") + '"')
            previous_parent = path.parent
        install.append(f"File {nsis_quote('/oname=' + path.name)} {nsis_quote(origin)}")
        remove.append("!insertmacro ND2WSI_DELETE_FILE " + nsis_quote(relative.replace("/", "\\")))
        directories.update(parent for parent in path.parents if str(parent) != ".")
    for directory in sorted(directories, key=lambda p: (-len(p.parts), str(p))):
        remove.append("!insertmacro ND2WSI_REMOVE_EMPTY_DIR "
                      + nsis_quote(str(directory).replace("/", "\\")))
    install_path = work / "payload-install.nsh"
    remove_path = work / "payload-uninstall.nsh"
    guard_path = work / "payload-guard.nsh"
    guarded = sorted(set(files) | {str(directory) for directory in directories})
    guards = ["!insertmacro ND2WSI_GUARD_PATH " + nsis_quote(path.replace("/", "\\"))
              for path in guarded]
    install_path.write_text("\n".join(install) + "\n", encoding="utf-8")
    remove_path.write_text("\n".join(remove) + "\n", encoding="utf-8")
    guard_path.write_text("\n".join(guards) + "\n", encoding="utf-8")
    return install_path, remove_path, guard_path


def pe_machine(path: Path) -> int:
    with path.open("rb") as source:
        if source.read(2) != b"MZ":
            raise ValueError(f"Not a Windows executable: {path}")
        source.seek(0x3C)
        source.seek(struct.unpack("<I", source.read(4))[0])
        if source.read(4) != b"PE\0\0":
            raise ValueError(f"Invalid PE header: {path}")
        return struct.unpack("<H", source.read(2))[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--makensis", type=Path, required=True)
    parser.add_argument("--nsisdir", type=Path)
    parser.add_argument("--webview2-bootstrapper", type=Path, required=True)
    parser.add_argument("--webview2-sha256", required=True)
    parser.add_argument("--icon", type=Path)
    parser.add_argument("--readme", type=Path,
                        default=REPO / "packaging/WINDOWS-INSTALLER-README.txt")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    archive = args.archive.resolve(strict=True)
    bootstrapper = args.webview2_bootstrapper.resolve(strict=True)
    for path, expected in ((archive, args.expected_sha256), (bootstrapper, args.webview2_sha256)):
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256(path) != expected:
            parser.error(f"SHA-256 does not match: {path}")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    payload = work / APP
    files = extract_payload(archive, payload)
    original_readme_hash = files["READ-ME.txt"]
    readme = args.readme.resolve(strict=True)
    if not readme.is_relative_to(REPO):
        parser.error("--readme must be a repository source file for provenance")
    readme_bytes = readme.read_bytes()
    (payload / "READ-ME.txt").write_bytes(readme_bytes)
    files["READ-ME.txt"] = hashlib.sha256(readme_bytes).hexdigest()
    info = json.loads((payload / "build-info.json").read_text(encoding="utf-8"))
    version = info["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Installer requires a three-part numeric app version")
    if pe_machine(payload / f"{APP}.exe") != 0x8664:
        raise ValueError("Portable executable must be AMD64/x64")
    if files[f"{APP}.exe"] != info["executable_sha256"]:
        raise ValueError("Executable does not match its build provenance")
    pe_machine(bootstrapper)
    install, uninstall, guard = emit_includes(payload, files, work)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError(f"Refusing to overwrite an existing installer: {output}")
    manifest = {
        "version": version,
        "portable_archive_sha256": args.expected_sha256,
        "files": files,
        "documentation_override": {
            "path": "READ-ME.txt", "portable_sha256": original_readme_hash,
            "installed_sha256": files["READ-ME.txt"],
        },
    }
    manifest_path = output.with_suffix(".payload.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    defines = {
        "APP_VERSION": version,
        "APP_VERSION_QUAD": version + ".0",
        "OUTPUT_FILE": str(output),
        "PAYLOAD_INSTALL_INCLUDE": str(install),
        "PAYLOAD_UNINSTALL_INCLUDE": str(uninstall),
        "PAYLOAD_GUARD_INCLUDE": str(guard),
        "PAYLOAD_SIZE_KIB": str((sum((payload / n).stat().st_size for n in files) + 1023) // 1024),
        "WEBVIEW2_BOOTSTRAPPER": str(bootstrapper),
    }
    if args.icon:
        defines["APP_ICON"] = str(args.icon.resolve(strict=True))
    env = os.environ.copy()
    if args.nsisdir:
        env["NSISDIR"] = str(args.nsisdir.resolve(strict=True))
    compiler = str(args.makensis.resolve(strict=True))
    compiler_version = subprocess.check_output([compiler, "-VERSION"], env=env, text=True).strip()
    script = REPO / "packaging/windows-installer.nsi"
    source_paths = (Path(__file__).resolve(), script, readme)
    source_hashes = {str(path.relative_to(REPO)): sha256(path) for path in source_paths}
    if sha256(readme) != files["READ-ME.txt"]:
        raise RuntimeError("Installer documentation changed while preparing the payload")
    include_hashes = {path.name: sha256(path) for path in (install, uninstall, guard)}
    command = [compiler, "-NOCD", "-INPUTCHARSET", "UTF8", "-WX", "-V3"]
    command += [f"-D{name}={value}" for name, value in defines.items()]
    command.append(str(script))
    result = subprocess.run(command, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    output.with_suffix(".compile.log").write_text(result.stdout, encoding="utf-8")
    print(result.stdout)
    result.check_returncode()
    if any(sha256(path) != source_hashes[str(path.relative_to(REPO))] for path in source_paths):
        raise RuntimeError("Installer source changed during compilation; rebuild from a stable snapshot")
    if any(sha256(path) != include_hashes[path.name] for path in (install, uninstall, guard)):
        raise RuntimeError("Generated payload instructions changed during compilation")
    if sha256(bootstrapper) != args.webview2_sha256:
        raise RuntimeError("WebView2 bootstrapper changed during compilation")
    # Compilation must not change the verified input payload.
    for name, expected in files.items():
        if sha256(payload / name) != expected:
            raise RuntimeError(f"Payload changed during compilation: {name}")
    report = {
        "schema_version": 1,
        "app_version": version,
        "installer_sha256": sha256(output),
        "installer_bytes": output.stat().st_size,
        "installer_pe_machine": hex(pe_machine(output)),
        "portable_archive_sha256": args.expected_sha256,
        "documentation_override": manifest["documentation_override"],
        "app_executable_sha256": files[f"{APP}.exe"],
        "payload_file_count": len(files),
        "payload_manifest_sha256": sha256(manifest_path),
        "compiler_version": compiler_version,
        "compiler_sha256": sha256(Path(compiler)),
        "webview2_bootstrapper_sha256": args.webview2_sha256,
        "installer_source_sha256": source_hashes,
        "generated_include_sha256": include_hashes,
        "windows_verification": "pending; run verify_windows_installer.ps1",
        "published": False,
    }
    output.with_suffix(".build.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{report['installer_sha256']}  {output.name}\n", encoding="ascii")
    print(f"Created {output} ({output.stat().st_size:,} bytes); Windows verification is pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
