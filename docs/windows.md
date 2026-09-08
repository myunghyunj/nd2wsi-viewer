# Windows distribution

The current source targets viewer 2.0.0, with a native Windows frame
and Microsoft Edge WebView2. `nd2wsi-viewer-2.0.0-Setup.exe` is the expected installer
name for that version. Setup installs the app
for the current Windows account in `%LOCALAPPDATA%\Programs\nd2wsi-viewer`.
It creates Start Menu shortcuts, an optional Desktop shortcut, and an entry in
Windows Settings > Apps for removal. The executable and `_internal` directory
are installed together; no separate Python installation is needed.

A portable ZIP can also be built locally. It includes x64 CPython, the viewer,
ND2 reading and writing, TIFF/SVS codecs, and the application assets. Keep the
whole extracted folder together; the EXE depends on its `_internal` directory.
Download an available Setup and its adjacent SHA-256 checksum from the
[release page](https://github.com/myunghyunj/nd2wsi-viewer/releases).
Check that release's artifact version and Windows test evidence: the historical
v1.2.8 results below do not establish v2.0.0 Windows hardware verification.
The portable ZIP is a local build output; the public Windows download is Setup.

Target execution modes (exact v2.0.0 build verification is still required):

| Computer | Execution |
| --- | --- |
| Windows 10 x64, version 1709/build 16299 or later, including Education | Native x64; 22H2 recommended |
| Windows 11 on Intel/AMD x64 | Native x64 |
| Windows 11 on ARM64 | Same x64 EXE through Windows x64 emulation |

Windows 10 ARM64 and 32-bit Windows are not supported. The laboratory target
is Windows 10 Education x64; its actual edition and build are recorded by the
supplied verification script. This is a desktop Windows edition even when the
computer is used as a laboratory server.

There is no GPU compute requirement. ARM64-native packaging is not claimed:
the locked Python/.NET bridge and some scientific dependencies do not provide
a complete native Windows ARM64 stack. Microsoft's
[x64 emulation documentation](https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation)
describes the Windows 11 execution path.

## Historical v1.2.8 installer verification

The [v1.2.8 Setup](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v1.2.8) had SHA-256
`d080538b51b6786a6c04d28a0b0c1fae1d26736aa8f859cf741b262403f265d1`.
It passed all 23 installation, execution, reinstallation, and removal steps on
Windows 11 ARM64 build 26200.9168 using x64 emulation. This includes all 2,559
payload hashes, Korean installation/source paths, Unicode shortcuts, scientific
and GUI smoke checks, directory-junction refusal, and five locked-file or
interrupted-removal recovery cases. Scientific-data and app-data sentinels
survived removal; the temporary test installations were cleaned up.
Windows 10 Education and separate native x64 hardware were not tested in that run.
This record applies only to that v1.2.8 artifact, not to the v2.0.0 source,
single-file cache changes, or a newly built installer.

The Windows source was added after the original macOS v1.2.8 tag. The release
notes link the Windows source revision; the automatic source archives attached
to the original v1.2.8 tag continue to describe that original macOS revision.

## Local build and verification

The installer wraps an existing checksum-verified portable ZIP without
rebuilding its executable. The installed `READ-ME.txt` is updated for Setup and
Windows 10 support, with both old and new hashes recorded explicitly.
`packaging/build_windows_installer.py` uses the
NSIS compiler, which can run on macOS, and records the exact portable payload,
installer source, compiler, and Microsoft WebView2 bootstrapper hashes.
See [the installer build contract](../packaging/windows-installer.md).

`packaging/verify_windows_installer.ps1` verifies installation in a Korean path,
all installed file hashes, shortcuts and uninstall registration, both app smoke
modes, same-version reinstallation, and removal. Its scientific-data sentinels
must survive removal; replaced directory junctions must cause installation and
removal to stop before changing files. `-KeepInstalled` finishes with a tested
installation in the default user directory. Verification results describe only
the Windows machines on which that script was actually run.

The installer checks the .NET Framework 4.6.2 minimum and checks for WebView2.
Windows 10 Education 22H2 already includes .NET Framework 4.8. It uses Microsoft's bundled, signed
bootstrapper if the runtime is missing. That prerequisite step needs Internet
access; existing WebView2 installations are reused and are not uninstalled with
the viewer. App removal preserves added images, annotations, caches, and other
user files. Reinstalling the same version into its registered directory is
supported. To change from v1.x to v2.0.0, close the app, uninstall the previous
version, then install the new version. Setup will not silently replace a different
registered version. If retained user files leave the old install folder nonempty,
choose a new empty installation folder; do not delete research data to make room.

The local build uses `uv.lock` and the hash-pinned
`packaging/windows-build-requirements.txt`. PyInstaller runs with x64 CPython
inside Windows, including a Windows 11 ARM virtual machine running x64 CPython
through emulation. It does not cross-compile the EXE on macOS. The build
manifest records source provenance, Python and package versions, lock hashes,
and the EXE hash.

Run the synthetic suite and the checksum-verified real ND2 suite before
packaging. The packaging script requires the frozen executable to pass:

- Opening a real ND2 acquisition, building/opening its overview and serving a tile.
- ND2 ROI export with exact source pixels, calibration, and channel names.
- JPEG and lossless JPEG 2000 SVS pyramid reads, tile serving, and exact TIFF ROI export.
- A native window using WebView2, a JavaScript/Python bridge roundtrip, and a loaded viewer canvas.

To reproduce in Windows with x64 Python 3.13.14, uv 0.12.10, Node.js 22, and
Microsoft Edge WebView2 Runtime:

```powershell
uv sync --frozen --all-extras --python 3.13.14
uv pip install --python .venv/Scripts/python.exe --require-hashes --no-config --default-index https://pypi.org/simple -r packaging/windows-build-requirements.txt
.venv/Scripts/python.exe scripts/fetch_testdata.py
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe packaging/build_windows.py --smoke-file docs/example_cell.nd2
```

For the current source, the expected output is
`dist/windows/nd2wsi-viewer-2.0.0-windows-x64.zip` and its `.sha256`.
The actual version comes from the built package and its build manifest.
There is no packaging switch to skip the mandatory executable smoke checks.

Verify the extracted delivery archive on a Windows target with Windows PowerShell 5.1 or later:

```powershell
./packaging/verify_windows.ps1 -Archive dist/windows/nd2wsi-viewer-2.0.0-windows-x64.zip -SmokeFile docs/example_cell.nd2
```

This verification uses the shipped EXE without installing Python, checks
archive and EXE hashes, and tests Korean names and spaces in both installation
and source paths. Reports record the tested Windows version and OS architecture.
The build and verification reports describe the machines actually tested;
a passing ARM virtual-machine run does not establish a separate x64-hardware
test result.

The local snapshot runner (`packaging/build_windows_local.ps1`) performs this
extracted-archive check automatically before reporting completion. Its
`output/verification` evidence includes both native GUI and scientific smoke
results from the extracted EXE, including Korean and space-containing paths.

## CI workflow

`.github/workflows/windows.yml` is configured for main-branch pushes, version
tags, pull requests, and manual runs pinned to a full commit SHA. It builds on
x64 Windows and tests the same final archive on x64 Windows and Windows 11 ARM
runners, saving the archive and smoke evidence as workflow artifacts. A local
build does not itself require publication. The workflow file alone does not
establish a successful run for v2.0.0; report the exact tested commit and artifact
hashes from completed jobs.

## Local behavior

The source scan remains read-only. Annotations and caches use the same layout
as macOS, beside the source: `nd2wsi/annotations` and `nd2wsi/caches`.
New v2 image caches use one `.nd2svs` file containing the same Zarr v2 encoded
chunks and metadata in SQLite. This is an application cache container, not an
Aperio `.svs` file or a replacement for an original scan. Source-backed overview
caches still require the acquisition; annotations remain separate JSON files.
Opening a matching legacy managed cache can make a verified single-file copy;
automatic opening preserves the legacy directory. Version 1.x cannot read the
new container, so keep original data and required legacy caches when testing a
rollback.

Close all viewers and migration writers before moving or deleting a cache,
especially on Windows where open handles can prevent those operations. A write
can temporarily create a SQLite journal beside the container; do not remove
journal/companion files to force a one-file appearance. The v2 Windows acceptance
run must cover read-only reopen, legacy import, Unicode and long paths, and
cache-handle closure before rename/removal. The historical installer result above
does not establish that these new Windows checks have passed.

Windows locks use kernel byte-range locking and safe process-liveness checks.
Cache removal captures file identities and deletes through Windows handles;
directory junctions and symlinks are never traversed during deletion.

Logs are stored in `%LOCALAPPDATA%\nd2wsi-viewer\logs`. WebView2 runs in
private mode with temporary browser data. Windows uses Ctrl/Alt shortcuts
and Explorer's reveal command. For a portable copy, install updates by
extracting a newer supplied ZIP into a fresh folder. The Windows update button
opens the GitHub Releases page for manual downloads. Sparkle remains specific
to macOS. For a newer installed version, follow that release's upgrade instructions.

The viewer executable and Setup are not Authenticode-signed. They include a project license and available
dependency license files. A Windows code-signing certificate is optional for
running the app but would reduce unknown-publisher warnings.
