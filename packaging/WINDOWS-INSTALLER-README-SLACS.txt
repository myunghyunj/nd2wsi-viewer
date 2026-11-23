nd2wsi-viewer SLACS for Windows (Setup Installer)

Download published Windows packages and their matching .sha256 files from:
https://github.com/myunghyunj/nd2wsi-viewer/releases

Installation
Run nd2wsi-viewer-<version>_SLACS-module-windows-x64-setup.exe. Setup installs
the lab edition and its _internal folder together in
%LOCALAPPDATA%\Programs\nd2wsi-viewer SLACS for your Windows account (HKCU)
without requesting an administrator UAC prompt. This installation uses a
separate folder, Start Menu group, Desktop shortcut, and uninstall registration
so it can live beside the public %LOCALAPPDATA%\Programs\nd2wsi-viewer install.
Start the app from the Start Menu or its optional Desktop shortcut, or open a
supported slide from Windows Explorer's Open with menu. Python, vendor
microscope software (NIS-Elements, ZEN, LAS X), and CUDA are not required.

Supported formats
Open or drop Nikon ND2 (.nd2), Zeiss CZI (.czi), Leica LIF (.lif), OME-TIFF
and TIFF (.ome.tif, .ome.tiff, .tif, .tiff), QPTIFF (.qptiff), Aperio SVS
(.svs), SQRAY SDPC (.sdpc), nd2wsi single-file caches (.nd2svs), and standard
raster images (.jpg, .jpeg, .png). Setup registers per-user Open with and
Default Apps capabilities under HKCU\Software\Classes for .nd2, .czi, .lif,
.tif, .tiff, .qptiff, .svs, .sdpc, and .nd2svs without overwriting your
existing default image handlers or changing system PATH.

Supported target systems
- Windows 11 x64 on Intel/AMD.
- Windows 11 ARM64 using Windows x64 emulation (not a native ARM64 binary).
- Windows Server 2025 x64 with Desktop Experience (CI verification environment).
Setup's preflight also permits 64-bit Windows 10 (build 10240+, including
Enterprise LTSC with cumulative OS UCRT updates) and Windows Server 2016+ with
Desktop Experience when .NET Framework 4.6.2+ is present (using the bundled
offline WebView2 runtime when needed); Windows 10 is not tested in CI. Windows
8.1 and earlier, Windows 10 ARM64, headless Server Core, and 32-bit Windows are
not supported.

Prerequisites and offline WebView2 runtime
.NET Framework 4.6.2 or newer is required; Setup checks this before installing.
This SLACS edition bundles the Microsoft Edge WebView2 Fixed Version Runtime
under _internal\webview2-runtime\ for offline installation on air-gapped
microscope workstations; no Internet connection is required during Setup or
first launch. Automatic public updates are disabled in SLACS builds.

Removal and reinstall
Use Windows Settings > Apps, or Uninstall nd2wsi-viewer SLACS in the Start
Menu. Uninstall removes only manifest-listed payload files, empty payload
folders, installer shortcuts, and its own HKCU uninstall and OpenWithProgids
entries. User-added images, annotations, caches, and other user files are kept.
Run the same Setup again to reinstall the same version in its registered
folder, or run a newer SLACS Setup to upgrade in place. If retained user files
leave an old app folder nonempty, choose a new empty installation folder; never
delete research data to satisfy Setup. Keep nd2wsi-viewer.exe beside _internal.

Data, provenance, and licensing
Your input scan is read-only. Viewing caches and annotations are stored beside
the source under nd2wsi\caches (.nd2svs SQLite Zarr containers) and
nd2wsi\annotations. Close all viewers before moving or deleting caches.
Diagnostic logs are written to %LOCALAPPDATA%\nd2wsi-viewer\Logs.
Build provenance, dependency licenses, and third-party notices ship in
_internal\nd2wsi-viewer-about\ (build-info.json, LICENSE.txt,
THIRD_PARTY_NOTICES.md, licenses\, SDPC-CODEC-NOTICE.txt, sdpc-codec-licenses\,
sdpc-codec-source.json for the LGPL-2.1-or-later FFmpeg/PyAV SDPC decoder, and
WEBVIEW2-RUNTIME-NOTICE.txt for the bundled WebView2 Fixed Version Runtime).

Signing and verification
Check _internal\nd2wsi-viewer-about\build-info.json and the release's .sha256
checksum sidecar for exact artifact hashes and signing status. Unless built
with Authenticode signing credentials, the executable and Setup installer are
unsigned and Windows SmartScreen may show an unknown-publisher prompt.
