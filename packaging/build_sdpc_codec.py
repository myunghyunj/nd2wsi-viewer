#!/usr/bin/env python3
"""Build the LGPL-only SDPC decoder from pinned, unmodified upstream sources.

Requires matching native Python, Xcode CLT on macOS, or an x64 MSVC developer
environment and MSYS2 (make/diffutils) on Windows. Does not change caller Python.
Publish the resulting corresponding-source ZIP alongside the application.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import urllib.request
import venv
import zipfile
from pathlib import Path

FFMPEG_VERSION = "8.1.3"
PYAV_VERSION = "16.0.1"
MACOS_TARGET = "12.0"
SOURCES = (
    {"filename": "ffmpeg-8.1.3.tar.xz",
     "url": "https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz",
     "sha256": "7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3"},
    {"filename": "av-16.0.1.tar.gz",
     "url": "https://files.pythonhosted.org/packages/15/c3/fd72a0315bc6c943ced1105aaac6e0ec1be57c70d8a616bd05acaa21ffee/av-16.0.1.tar.gz",
     "sha256": "dd2ce779fa0b5f5889a6d9e00fbbbc39f58e247e52d31044272648fe16ff1dbf"},
)
BUILD_REQUIREMENTS = ("setuptools==80.9.0", "wheel==0.45.1", "Cython==3.1.2")
FFMPEG_FLAGS = (
    "--disable-autodetect", "--disable-everything", "--disable-programs",
    "--disable-doc", "--disable-network", "--disable-static", "--enable-shared",
    "--disable-gpl", "--disable-nonfree", "--disable-version3",
    "--enable-avcodec", "--enable-avformat", "--enable-avdevice",
    "--enable-avfilter", "--enable-swresample", "--enable-swscale",
    "--enable-decoder=hevc", "--enable-parser=hevc", "--enable-demuxer=hevc",
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command: list[str], *, cwd: Path | None = None,
        env: dict[str, str] | None = None) -> None:
    print("+ " + shlex.join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def python_at(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def download(source: dict[str, str], directory: Path) -> Path:
    path = directory / source["filename"]
    if not path.exists():
        print(f"Downloading pinned source: {source['filename']}", flush=True)
        with urllib.request.urlopen(source["url"], timeout=120) as response, path.open("xb") as output:
            shutil.copyfileobj(response, output)
    if sha256(path) != source["sha256"]:
        raise RuntimeError(f"Source checksum mismatch: {path}")
    return path


def native_hashes(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        return {name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in sorted(archive.namelist())
                if name.lower().endswith((".dylib", ".so", ".pyd", ".dll"))}


def make_ffmpeg(source: Path, arguments: list[str], jobs: int,
                env: dict[str, str], msys_bash: str | None) -> None:
    if os.name == "nt":
        bash = Path(msys_bash or r"C:\msys64\usr\bin\bash.exe")
        if not bash.is_file():
            raise RuntimeError("MSYS2 bash is required; pass --msys-bash PATH")
        if not (bash.parent / "make.exe").is_file():
            raise RuntimeError("Install make in the selected MSYS2 environment (pacman -S make)")
        compiler = shutil.which("cl.exe")
        if not compiler:
            raise RuntimeError("Run in an x64 MSVC developer environment (cl.exe missing)")
        shell_env = dict(env, MSYSTEM="MSYS", MSYS2_PATH_TYPE="inherit", SHELL="/usr/bin/bash")
        # Keep MSVC's link.exe before MSYS2's unrelated link command.
        shell_env["PATH"] = str(Path(compiler).parent) + os.pathsep + str(bash.parent) + os.pathsep + env["PATH"]
        cd = "cd " + shlex.quote(source.resolve().as_posix()) + " && "
        # Native Windows make can strip the backslashes in FFmpeg's MSVC AWK
        # dependency rules. Use make and its recipe shell from this MSYS2 root,
        # never a similarly named executable inherited from the runner PATH.
        for command in ("./configure " + shlex.join(arguments),
                        f"/usr/bin/make SHELL=/usr/bin/bash -j{jobs}",
                        "/usr/bin/make SHELL=/usr/bin/bash install"):
            run([str(bash), "--noprofile", "--norc", "-c", cd + command], env=shell_env)
    else:
        run([str(source / "configure"), *arguments], cwd=source, env=env)
        run(["make", f"-j{jobs}"], cwd=source, env=env)
        run(["make", "install"], cwd=source, env=env)


def build_pyav(source: Path, prefix: Path, python: str, wheel_dir: Path,
               jobs: int, env: dict[str, str], *, windows: bool) -> None:
    build_options = ["build_ext", f"-j{jobs}"]
    wheel_options = ["bdist_wheel", "--dist-dir", str(wheel_dir)]
    if windows:
        # FFmpeg's MSVC install puts its .lib import libraries beside the DLLs
        # in bin; PyAV's --ffmpeg-dir searches only lib. Keep the upstream layout
        # and add bin to the linker search path without changing either source.
        build_options.append(f"--library-dirs={prefix / 'bin'}")
    else:
        # python.org's universal2 interpreter otherwise labels this wheel as
        # universal2 even though ARCHFLAGS and FFmpeg select native arm64.
        wheel_options += ["--plat-name", f"macosx_{MACOS_TARGET.replace('.', '_')}_arm64"]
    run([python, "setup.py", f"--ffmpeg-dir={prefix}", *build_options,
         *wheel_options], cwd=source, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new or empty output directory")
    parser.add_argument("--source-cache", type=Path, help="optional pinned source archive cache")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 8))
    parser.add_argument("--msys-bash", help="Windows MSYS2 bash.exe (default C:\\msys64\\usr\\bin\\bash.exe)")
    args = parser.parse_args()
    system, machine = platform.system(), platform.machine().lower()
    if (system, machine) not in {("Darwin", "arm64"), ("Windows", "amd64"), ("Windows", "x86_64")}:
        raise SystemExit("Supported release targets are native macOS arm64 and Windows x64")
    if args.jobs < 1:
        raise SystemExit("--jobs must be positive")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"Output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    cache = (args.source_cache or output / "downloads").resolve()
    cache.mkdir(parents=True, exist_ok=True)
    archives = [download(item, cache) for item in SOURCES]
    build = output / "build"
    build.mkdir()
    for archive in archives:
        with tarfile.open(archive) as source:
            source.extractall(build, filter="data")
    ffmpeg, pyav = build / f"ffmpeg-{FFMPEG_VERSION}", build / f"av-{PYAV_VERSION}"
    prefix = build / "ffmpeg-install"
    env = os.environ.copy()
    for variable in ("PYTHONPATH", "PYTHONHOME", "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR",
                     "CFLAGS", "CPPFLAGS", "LDFLAGS", "DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        env.pop(variable, None)
    flags = [f"--prefix={prefix.as_posix()}", *FFMPEG_FLAGS]
    if system == "Darwin":
        env.update(MACOSX_DEPLOYMENT_TARGET=MACOS_TARGET, ARCHFLAGS="-arch arm64",
                   CFLAGS=f"-mmacosx-version-min={MACOS_TARGET}", LDFLAGS=f"-mmacosx-version-min={MACOS_TARGET}")
        flags += ["--arch=arm64", "--enable-pic", f"--extra-cflags=-mmacosx-version-min={MACOS_TARGET}",
                  f"--extra-ldflags=-mmacosx-version-min={MACOS_TARGET}"]
        repair_requirement, asset_platform = "delocate==0.13.0", "macos-arm64"
    else:
        flags += ["--toolchain=msvc", "--arch=x86_64", "--disable-x86asm"]
        repair_requirement, asset_platform = "delvewheel==1.10.0", "windows-x64"
    make_ffmpeg(ffmpeg, flags, args.jobs, env, args.msys_bash)
    build_env = build / "python-build"
    venv.EnvBuilder(with_pip=True).create(build_env)
    python = str(python_at(build_env))
    run([python, "-m", "pip", "install", "--disable-pip-version-check", *BUILD_REQUIREMENTS, repair_requirement], env=env)
    raw = output / "unrepaired-wheel"
    raw.mkdir()
    build_pyav(pyav, prefix, python, raw, args.jobs, env, windows=system == "Windows")
    raw_wheels = list(raw.glob("*.whl"))
    if len(raw_wheels) != 1:
        raise RuntimeError("Expected exactly one source-built PyAV wheel")
    wheelhouse = output / "wheelhouse"
    wheelhouse.mkdir()
    if system == "Darwin":
        run([str(build_env / "bin/delocate-wheel"), "--require-archs", "arm64",
             "--wheel-dir", str(wheelhouse), str(raw_wheels[0])], env=env)
    else:
        run([python, "-m", "delvewheel", "repair", "--add-path", str(prefix / "bin"),
             "--wheel-dir", str(wheelhouse), str(raw_wheels[0])], env=env)
    wheels = list(wheelhouse.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one repaired PyAV wheel")
    wheel = wheels[0]
    licenses = output / "licenses"
    licenses.mkdir()
    shutil.copy2(ffmpeg / "COPYING.LGPLv2.1", licenses / "FFmpeg-LGPL-2.1.txt")
    shutil.copy2(pyav / "LICENSE.txt", licenses / "PyAV-BSD-3-Clause.txt")
    scripts = Path(__file__).resolve().parent
    shutil.copy2(scripts / "SDPC-CODEC-NOTICE.txt", licenses / "SDPC-CODEC-NOTICE.txt")
    manifest = {
        "schema_version": 1, "platform": asset_platform, "ffmpeg_version": FFMPEG_VERSION,
        "pyav_version": PYAV_VERSION, "license": "LGPL-2.1-or-later", "configure_args": flags,
        "sources": list(SOURCES), "build_requirements": [*BUILD_REQUIREMENTS, repair_requirement],
        "python_version": platform.python_version(),
        "macos_deployment_target": MACOS_TARGET if system == "Darwin" else None,
        "wheel_file": wheel.relative_to(output).as_posix(), "wheel_sha256": sha256(wheel),
        "native_files": native_hashes(wheel), "licenses_directory": "licenses",
        "source_archive_file": f"sdpc-codec-sources-{asset_platform}.zip",
    }
    bundle = output / manifest["source_archive_file"]
    with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in archives:
            archive.write(source, "sources/" + source.name)
        for name in ("build_sdpc_codec.py", "verify_sdpc_codec.py", "SDPC-CODEC-NOTICE.txt"):
            archive.write(scripts / name, "packaging/" + name)
        for license_file in licenses.iterdir():
            archive.write(license_file, "licenses/" + license_file.name)
        for name in ("config.h", "config_components.h", "ffbuild/config.mak", "ffbuild/config.log"):
            if (ffmpeg / name).is_file():
                archive.write(ffmpeg / name, "configured-ffmpeg/" + name)
        archive.writestr("build-record.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr("REBUILD.txt", "Use matching native Python and prerequisites in packaging/build_sdpc_codec.py.\n"
                          "Run: python packaging/build_sdpc_codec.py --output NEW_EMPTY_DIR --source-cache sources\n"
                          "On Windows, use an x64 MSVC developer environment, install make and diffutils\n"
                          "in MSYS2, and append --msys-bash C:/path/to/msys64/usr/bin/bash.exe.\n"
                          "The Bash path must belong to the MSYS2 installation containing those packages.\n"
                          "Upstream source archives are unmodified. The build selects LGPL-only shared\n"
                          "HEVC libraries; wheel repair changes library load paths. See the notice.\n")
    manifest["source_archive_sha256"] = sha256(bundle)
    manifest_path = output / "sdpc-codec-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    check_env = build / "python-check"
    venv.EnvBuilder(with_pip=True).create(check_env)
    check_python = str(python_at(check_env))
    run([check_python, "-m", "pip", "install", "--no-deps", str(wheel)], env=env)
    run([check_python, str(scripts / "verify_sdpc_codec.py"), "--manifest", str(manifest_path)], cwd=output, env=env)
    print(f"Verified source-built SDPC codec: {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
