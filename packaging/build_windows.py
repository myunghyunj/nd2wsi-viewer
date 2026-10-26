#!/usr/bin/env python3
"""Build and smoke-test the portable Windows x64 executable.

Run with the locked Windows environment; see .github/workflows/windows.yml.
An x64 build also runs under Windows 11 ARM64's built-in x64 emulation.
This script deliberately has no option to skip the packaged release checks.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
APP = "nd2wsi-viewer"
# The one checked-in pin shared with the CI download step in windows.yml.
WEBVIEW2_PIN = REPO / "packaging" / "webview2-fixed-runtime.json"
# nd2wsi.webview2_runtime looks for BROWSER_EXE directly inside
# sys._MEIPASS/BUNDLED_DIRNAME, and sys._MEIPASS is the onedir _internal folder.
WEBVIEW2_BROWSER = "msedgewebview2.exe"
WEBVIEW2_BUNDLE_PATH = "_internal/webview2-runtime"
PE_MACHINE_AMD64 = 0x8664
# READ-ME.txt describes the bundled runtime; a build without it (no
# --webview2-fixed-runtime) ships the earlier system-prerequisite paragraph.
BUNDLED_README_START = "Microsoft Edge WebView2 Runtime is included"
SYSTEM_WEBVIEW2_README = """\
The app uses Microsoft Edge WebView2 Evergreen Runtime, normally included
with Windows 11. If your organization removed it, install the runtime from:
https://developer.microsoft.com/microsoft-edge/webview2/
Select the runtime for your operating system (ARM64 on an ARM64 PC).
"""


def readme_text(*, bundled: bool) -> str:
    text = (REPO / "packaging" / "WINDOWS-README.txt").read_text(encoding="utf-8")
    if bundled:
        return text
    paragraphs = text.split("\n\n")
    matches = [index for index, paragraph in enumerate(paragraphs) if paragraph.startswith(BUNDLED_README_START)]
    if len(matches) != 1:
        raise RuntimeError("WINDOWS-README.txt has no single bundled WebView2 paragraph to replace")
    paragraphs[matches[0]] = SYSTEM_WEBVIEW2_README.rstrip("\n")
    return "\n\n".join(paragraphs)


WEBVIEW2_NOTICE = """\
Microsoft Edge WebView2 Runtime (Fixed Version) {version} ({architecture})
(c) Microsoft Corporation. All rights reserved.

nd2wsi-viewer includes an unmodified copy of the Microsoft Edge WebView2
Runtime (Fixed Version) in _internal\\webview2-runtime. It is distributed under
Microsoft's WebView2 Runtime license terms, and Microsoft's own notices inside
that folder are left in place.

This copy is not updated automatically. Security and other updates to it
arrive only with new nd2wsi-viewer releases. If a working WebView2 Runtime is
already installed on the PC, nd2wsi-viewer uses that one instead, because it
updates itself.

On Windows 10, the first time nd2wsi-viewer uses this copy it gives Windows'
app-sandbox groups (ALL APPLICATION PACKAGES and ALL RESTRICTED APPLICATION
PACKAGES) read access to the webview2-runtime folder, as Microsoft requires
for the Fixed Version. This needs no administrator rights for a folder you
extracted yourself; otherwise an administrator can grant it once with icacls
(see READ-ME.txt).

The WebView2 Runtime (Fixed Version) must not be run from a network share or
UNC path. Extract nd2wsi-viewer to a local drive.

The WebView2 Runtime includes Microsoft Defender SmartScreen, which may send
information to Microsoft. See the Microsoft privacy statement:
https://go.microsoft.com/fwlink/?LinkId=521839
"""


def sdpc_bundle_options() -> tuple[Path, list[str]]:
    """A release must include the exact verified source-built LGPL decoder."""
    from bundle_sdpc_codec import manifest_path, verified_manifest

    manifest = manifest_path()
    verified_manifest(manifest)
    return manifest, ["--collect-all", "av"]


def run(*args: str | Path) -> None:
    subprocess.run([str(arg) for arg in args], cwd=REPO, check=True)


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_source_files(provenance: dict) -> None:
    """Require the exact source bytes named by a local snapshot manifest."""
    files = provenance.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Source manifest must contain a nonempty files SHA-256 mapping")
    for relative, expected in files.items():
        path = PurePosixPath(relative)
        if (path.is_absolute() or ".." in path.parts or "\\" in relative
                or ":" in relative or str(path) != relative):
            raise ValueError(f"Invalid relative source path: {relative}")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"Invalid source SHA-256: {relative}")
        actual = REPO / relative
        if not actual.is_file() or sha256(actual) != expected:
            raise ValueError(f"Source snapshot checksum mismatch: {relative}")
    required = {
        "pyproject.toml", "uv.lock", "LICENSE", "packaging/build_windows.py",
        "packaging/launch.py", "packaging/AppIcon.icns", "packaging/WINDOWS-README.txt",
        "packaging/windows-build-requirements.txt",
        "packaging/SDPC-CODEC-NOTICE.txt",
        "packaging/build_sdpc_codec.py", "packaging/verify_sdpc_codec.py",
        "packaging/bundle_sdpc_codec.py", "packaging/webview2-fixed-runtime.json",
    }
    # Generated bytecode can be created by imports/tests. Every actual module
    # and static resource shipped by collect-all must belong to the snapshot.
    required.update(str(path.relative_to(REPO).as_posix())
                    for path in (REPO / "nd2wsi").rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts
                    and path.suffix != ".pyc")
    missing = required - set(files)
    if missing:
        raise ValueError(f"Source manifest omits build inputs: {sorted(missing)}")


def source_provenance(source_manifest: Path | None) -> dict:
    if source_manifest is not None:
        result = json.loads(source_manifest.read_text(encoding="utf-8-sig"))
        if result.get("schema_version") != 1:
            raise ValueError("Unsupported source manifest schema_version")
        if not re.fullmatch(r"[0-9a-f]{40}", result.get("base_commit", "")):
            raise ValueError("Source manifest requires the full base Git commit SHA")
        if type(result.get("working_tree_dirty")) is not bool:
            raise ValueError("Source manifest requires a boolean working_tree_dirty")
        result["source_kind"] = "local-working-tree-snapshot"
        result["manifest_sha256"] = sha256(source_manifest)
    else:
        # Git is only needed for a checkout build, never in a snapshot guest.
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO)
            names = subprocess.check_output(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO
            ).decode("utf-8").split("\0")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("No Git checkout available; pass --source-manifest SOURCE-MANIFEST.json") from exc
        result = {
            "schema_version": 1,
            "source_kind": "git-working-tree",
            "base_commit": commit,
            "working_tree_dirty": bool(status.strip()),
            "files": {name: sha256(REPO / name) for name in sorted(set(names))
                      if name and (REPO / name).is_file()},
        }
    verify_source_files(result)
    content = json.dumps(result["files"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    result["snapshot_sha256"] = hashlib.sha256(content).hexdigest()
    return result


def load_webview2_pin(path: Path = WEBVIEW2_PIN) -> dict:
    """Read and sanity-check the pinned Microsoft WebView2 Fixed Version cab."""
    pin = json.loads(path.read_text(encoding="utf-8"))
    version = pin.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        raise ValueError("WebView2 pin requires a four-part runtime version")
    if pin.get("architecture") != "x64":
        raise ValueError("WebView2 pin must name the x64 Fixed Version runtime")
    if pin.get("folder") != f"Microsoft.WebView2.FixedVersionRuntime.{version}.x64":
        raise ValueError("WebView2 pin folder does not match its version and architecture")
    url = pin.get("url")
    if (not isinstance(url, str) or not url.startswith("https://msedge.sf.dl.delivery.mp.microsoft.com/")
            or not url.endswith(f"/{pin['folder']}.cab")):
        raise ValueError("WebView2 pin must download the matching cab directly from Microsoft")
    if type(pin.get("bytes")) is not int or pin["bytes"] <= 0:
        raise ValueError("WebView2 pin requires the exact cab size in bytes")
    if not isinstance(pin.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]):
        raise ValueError("WebView2 pin requires the cab's lowercase SHA-256")
    return pin


def pe_machine(path: Path) -> int:
    """IMAGE_FILE_HEADER.Machine of a Windows PE image."""
    with path.open("rb") as image:
        dos = image.read(0x40)
        if len(dos) < 0x40 or dos[:2] != b"MZ":
            raise ValueError(f"Not a Windows PE image: {path}")
        image.seek(struct.unpack_from("<I", dos, 0x3C)[0])
        header = image.read(6)
    if len(header) < 6 or header[:4] != b"PE\0\0":
        raise ValueError(f"Not a Windows PE image: {path}")
    return struct.unpack_from("<H", header, 4)[0]


def check_webview2_runtime(source: Path, pin: dict) -> Path:
    """Reject a wrong runtime folder before the multi-minute PyInstaller build."""
    browser = source / WEBVIEW2_BROWSER
    if not browser.is_file():
        raise ValueError(f"The WebView2 Fixed Version folder must directly contain {WEBVIEW2_BROWSER}: {source}")
    machine = pe_machine(browser)
    if machine != PE_MACHINE_AMD64:
        raise ValueError(f"{browser} is not an x64 (AMD64) image: PE machine 0x{machine:04x}")
    if source.name != pin["folder"] and pin["version"] not in source.name:
        raise ValueError(f"WebView2 runtime folder {source.name!r} is not the pinned {pin['folder']}")
    return source


def webview2_target(bundle: Path) -> Path:
    return bundle.joinpath(*WEBVIEW2_BUNDLE_PATH.split("/"))


def runtime_files(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): sha256(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def stage_webview2_runtime(source: Path, bundle: Path, pin: dict) -> dict:
    """Copy the Fixed Version runtime, unmodified, to where the app looks for it."""
    check_webview2_runtime(source, pin)
    if not (bundle / "_internal").is_dir():
        raise RuntimeError(f"The PyInstaller bundle has no _internal folder: {bundle}")
    expected = runtime_files(source)
    target = webview2_target(bundle)
    shutil.copytree(source, target, dirs_exist_ok=False)
    files = runtime_files(target)
    if files != expected:
        raise RuntimeError("The staged WebView2 runtime differs from its source folder")
    return {
        "distribution": "fixed-version-bundled",
        "version": pin["version"],
        "architecture": pin["architecture"],
        "path": WEBVIEW2_BUNDLE_PATH,
        "file_count": len(files),
        "total_bytes": sum((target / name).stat().st_size for name in files),
        "files": files,
        "browser_executable_sha256": sha256(target / WEBVIEW2_BROWSER),
        "cab": {"url": pin["url"], "bytes": pin["bytes"], "sha256": pin["sha256"]},
        "default_mode": "auto",
    }


def verify_staged_webview2_runtime(bundle: Path, info: dict) -> None:
    """Running the packaged app must not have changed the runtime that ships."""
    actual, expected = runtime_files(webview2_target(bundle)), info["files"]
    if actual != expected:
        changed = sorted(name for name in set(actual) | set(expected)
                         if actual.get(name) != expected.get(name))
        raise RuntimeError(f"The bundled WebView2 runtime changed after staging: {changed[:10]}")


# (label, key path) of the per-window evidence the GUI smoke report must carry.
WEBVIEW2_EVIDENCE = (
    ("main window", ("webview2_runtime", "main")),
    ("translucent viewer", ("webview2_runtime", "translucent")),
    ("independent window", ("independent_window", "child_gui", "webview2_runtime", "main")),
)


def check_bundled_webview2_report(report: dict, runtime: Path, version: str) -> None:
    """Require proof, per window, that the bundled browser process rendered it.

    The version alone proves nothing (the system Evergreen runtime can be the
    same version), so the browser process image must be the bundled file.
    """
    browser = runtime / WEBVIEW2_BROWSER
    for label, keys in WEBVIEW2_EVIDENCE:
        evidence = report
        for key in keys:
            evidence = evidence.get(key) if isinstance(evidence, dict) else None
        where = ".".join(keys)
        if not isinstance(evidence, dict):
            raise RuntimeError(f"Packaged GUI smoke has no WebView2 runtime evidence for the {label} ({where})")
        executable = evidence.get("browser_executable")
        if evidence.get("bundled") is not True:
            raise RuntimeError(f"The {label} was not rendered by the bundled WebView2 runtime "
                               f"({where}: {executable!r}, error {evidence.get('error')!r})")
        try:
            same = isinstance(executable, str) and os.path.samefile(executable, browser)
        except OSError:
            same = False
        # Independent of samefile: the reported path itself must name the staged copy.
        inside = isinstance(executable, str) and (
            os.path.normcase(os.path.realpath(executable)).startswith(
                os.path.normcase(os.path.realpath(runtime)) + os.sep)
            and os.path.basename(executable).lower() == WEBVIEW2_BROWSER.lower())
        if not (same and inside):
            raise RuntimeError(f"The {label}'s WebView2 browser process {executable!r} is not {browser}")
        if evidence.get("browser_version") != version:
            raise RuntimeError(f"The {label} reported WebView2 {evidence.get('browser_version')!r}, "
                               f"not the pinned {version}")
        print(f"WebView2 proof: {label} rendered by {executable} {version} "
              f"(pid {evidence.get('browser_process_id')}, system {evidence.get('system_version')})")


def smoke(exe: Path, source: Path, evidence: Path, *, bundled_webview2: Path | None = None) -> None:
    """Exercise the actual windowed EXE, including the WebView2 renderer.

    With bundled_webview2 (the staged runtime folder), both runs force
    ND2WSI_WEBVIEW2_RUNTIME=bundled, and the GUI report must prove that the
    main window, the translucent overlay and the independent child window
    were all rendered by that folder's msedgewebview2.exe.
    """
    evidence.mkdir(parents=True, exist_ok=True)
    # Bundled: 180 s readiness + overlay + a 240 s cold child + close stay inside it.
    gui_limit = 180 if bundled_webview2 is None else 600
    for flag, limit in (("--smoke", 600), ("--gui-smoke", gui_limit)):
        name = flag.removeprefix("--")
        log = evidence / f"{name}.log"
        log.unlink(missing_ok=True)
        env = dict(os.environ, ND2WSI_LOG_FILE=str(log))
        if bundled_webview2 is not None:
            env["ND2WSI_WEBVIEW2_RUNTIME"] = "bundled"
        command = [str(exe), flag, str(source)]
        report = evidence / "gui-smoke.json"
        if flag == "--gui-smoke":
            report.unlink(missing_ok=True)
            command += ["--smoke-report", str(report)]
        result = subprocess.run(command, env=env, timeout=limit)
        if log.exists():
            print(log.read_text(encoding="utf-8", errors="replace"))
        if result.returncode:
            raise RuntimeError(f"Packaged {flag} failed with exit {result.returncode}")
        if flag == "--gui-smoke":
            gui = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
            if not isinstance(gui, dict) or gui.get("ok") is not True:
                raise RuntimeError("Packaged GUI smoke did not record successful completion")
            if bundled_webview2 is not None:
                check_bundled_webview2_report(gui, bundled_webview2, load_webview2_pin()["version"])
        elif not log.exists() or "smoke ok: SDPC" not in log.read_text(encoding="utf-8"):
            raise RuntimeError(f"Packaged {flag} did not record successful SDPC decoder and ROI checks")
        elif "smoke ok: Astromap JPEG, TXT and PDF" not in log.read_text(encoding="utf-8"):
            raise RuntimeError(f"Packaged {flag} did not record successful Astromap export checks")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=REPO / "dist" / "windows")
    parser.add_argument("--source-manifest", type=Path,
                        help="exact source snapshot manifest; avoids requiring Git on the build machine")
    parser.add_argument("--webview2-fixed-runtime", type=Path, metavar="DIR",
                        help="expanded, pinned WebView2 Fixed Version x64 folder that directly contains "
                             "msedgewebview2.exe; copied unmodified into _internal/webview2-runtime")
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Build on Windows with 64-bit x64 CPython, not by cross-compiling")
    # platform.machine() reports the host under some emulators; the compiler
    # describes the interpreter architecture that determines the frozen binary.
    if "AMD64" not in platform.python_compiler().upper():
        parser.error("Use the x64 CPython interpreter; this package is not native ARM64")
    source = args.smoke_file.resolve(strict=True)
    provenance = source_provenance(args.source_manifest)
    webview2_pin = webview2_source = None
    if args.webview2_fixed_runtime is not None:
        # The pin's bytes were just verified as part of the source snapshot.
        webview2_pin = load_webview2_pin()
        webview2_source = check_webview2_runtime(args.webview2_fixed_runtime.resolve(strict=True), webview2_pin)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    build = REPO / "build" / "windows"
    build.mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    sdpc_manifest, sdpc_options = sdpc_bundle_options()

    # Fail before packaging if either scientific export or image codecs is absent.
    run(sys.executable, "-c", "import _tkinter, limnd2, imagecodecs, clr, webview, reportlab, pypdf; "
        "assert imagecodecs.JPEG.available and imagecodecs.JPEG2K.available")
    from PIL import Image

    icon = build / "AppIcon.ico"
    with Image.open(REPO / "packaging" / "AppIcon.icns") as image:
        image.save(icon, format="ICO", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--onedir", "--name", APP, "--icon", str(icon), "--noupx",
        "--distpath", str(build / "dist"), "--workpath", str(build / "pyinstaller"),
        "--specpath", str(build), "--copy-metadata", "nd2wsi-viewer",
    ]
    for package in (
        "nd2wsi", "nd2", "scipy", "zarr", "numcodecs", "dask", "tifffile", "imagecodecs",
        "limnd2", "ome_types", "xsdata", "xsdata_pydantic_basemodel", "webview",
        "pythonnet", "clr_loader", "reportlab", "pypdf", "charset_normalizer",
    ):
        command += ["--collect-all", package]
    command += sdpc_options
    command += [str(REPO / "packaging" / "launch.py")]
    run(*command)
    bundle = build / "dist" / APP
    exe = bundle / f"{APP}.exe"
    if not exe.is_file():
        raise RuntimeError("PyInstaller did not create the Windows executable")
    webview2_info = None
    web_renderer = "Microsoft Edge WebView2 Evergreen Runtime (system prerequisite)"
    if webview2_source is not None:
        # PyInstaller --noconfirm recreated the bundle, so the copy comes after it.
        webview2_info = stage_webview2_runtime(webview2_source, bundle, webview2_pin)
        web_renderer = (f"Microsoft Edge WebView2 Fixed Version Runtime {webview2_info['version']} x64 "
                        f"(bundled in {WEBVIEW2_BUNDLE_PATH}; the default auto mode prefers an "
                        "installed Evergreen runtime)")
        print(f"Bundled WebView2 Fixed Version {webview2_info['version']}: "
              f"{webview2_info['file_count']} files, {webview2_info['total_bytes']:,} bytes")
    from bundle_sdpc_codec import stage

    sdpc_source = stage(sdpc_manifest, bundle, out, version)
    manifest = {
        "app": APP,
        "version": version,
        "commit": provenance["base_commit"],
        "source": provenance,
        "python": platform.python_version(),
        "binary_architecture": "x64",
        "windows_arm64_mode": "Windows 11 x64 emulation",
        "web_renderer": web_renderer,
        "webview2_runtime": webview2_info or {"distribution": "system-evergreen"},
        "sdpc_codec_bundled": True,
        "distribution_status": "source-built-lgpl-codec",
        "sdpc_codec_manifest_sha256": sha256(sdpc_manifest),
        "sdpc_codec_source": sdpc_source,
        "runtime_lock_sha256": sha256(REPO / "uv.lock"),
        "build_lock_sha256": sha256(REPO / "packaging" / "windows-build-requirements.txt"),
        "executable_sha256": sha256(exe),
        "packages": dict(sorted((d.metadata["Name"], d.version)
                                for d in importlib.metadata.distributions()
                                if d.metadata["Name"])),
    }
    (bundle / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(REPO / "LICENSE", bundle / "LICENSE.txt")
    # Bytes, not text mode: keep the repository file's line endings.
    (bundle / "READ-ME.txt").write_bytes(readme_text(bundled=webview2_info is not None).encode("utf-8"))
    if webview2_info is not None:
        (bundle / "WEBVIEW2-RUNTIME-NOTICE.txt").write_text(
            WEBVIEW2_NOTICE.format(version=webview2_info["version"],
                                   architecture=webview2_info["architecture"]), encoding="utf-8")
    # Include dependency licenses and metadata (useful on machines with no Python).
    licenses = bundle / "licenses"
    for distribution in importlib.metadata.distributions():
        for item in distribution.files or ():
            if ".dist-info/" not in str(item):
                continue
            if "license" in str(item).lower() or "copying" in str(item).lower():
                origin = Path(distribution.locate_file(item))
                if origin.is_file():
                    target = licenses / str(item)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(origin, target)
    smoke(exe, source, out / "build-smoke",
          bundled_webview2=webview2_target(bundle) if webview2_info is not None else None)
    verify_source_files(provenance)
    if webview2_info is not None:
        verify_staged_webview2_runtime(bundle, webview2_info)
    archive = out / f"{APP}-{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                target.write(path, Path(APP) / path.relative_to(bundle))
    (out / f"{archive.name}.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    print(f"Created {archive} ({archive.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
