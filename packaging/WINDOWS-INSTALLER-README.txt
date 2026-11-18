nd2wsi-viewer for Windows (Setup Installer)

Download published Windows packages and their matching .sha256 files from:
https://github.com/myunghyunj/nd2wsi-viewer/releases

Installation
Run nd2wsi-viewer-<version>-windows-x64-setup.exe. Setup installs the app and
its _internal folder together in %LOCALAPPDATA%\Programs\nd2wsi-viewer for your
Windows account (HKCU) without requesting an administrator UAC prompt.
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
- Windows 10 x64, version 1709 / build 16299 or later (including Education).
- Windows 11 x64 on Intel/AMD.
- Windows 11 ARM64 using Windows x64 emulation (not a native ARM64 binary).
- Windows Server 2019/2022/2025 x64 with Desktop Experience.
Windows 10 ARM64, headless Server Core, and 32-bit Windows are not supported.

Prerequisites
.NET Framework 4.6.2 or newer is required; Setup checks this before installing.
Setup reuses Microsoft Edge WebView2 Evergreen Runtime, or offers its official
Microsoft-signed bootstrapper if missing (downloading the runtime requires an
Internet connection).

Removal and reinstall
Use Windows Settings > Apps, or Uninstall nd2wsi-viewer in the Start Menu.
Uninstall removes only manifest-listed payload files, empty payload folders,
installer shortcuts, and its own HKCU uninstall and OpenWithProgids entries.
User-added images, annotations, caches, and other user files are kept.
Run the same Setup again to reinstall the same version in its registered
folder. Before switching between different installed versions, close the viewer
and uninstall the previous version first. If retained user files leave the old
app folder nonempty, choose a new empty installation folder; never delete
research data to satisfy Setup. Keep nd2wsi-viewer.exe beside _internal.

Data, provenance, and licensing
Your input scan is read-only. Viewing caches and annotations are stored beside
the source under nd2wsi\caches (.nd2svs SQLite Zarr containers) and
nd2wsi\annotations. Close all viewers before moving or deleting caches.
Diagnostic logs are written to %LOCALAPPDATA%\nd2wsi-viewer\Logs.
Build provenance, dependency licenses, and third-party notices ship in
_internal\nd2wsi-viewer-about\ (build-info.json, LICENSE.txt,
THIRD_PARTY_NOTICES.md, licenses\, SDPC-CODEC-NOTICE.txt, sdpc-codec-licenses\,
and sdpc-codec-source.json for the LGPL-2.1-or-later FFmpeg/PyAV SDPC decoder).

Signing and verification
Check _internal\nd2wsi-viewer-about\build-info.json and the release's .sha256
checksum sidecar for exact artifact hashes and signing status. Unless built
with Authenticode signing credentials, the executable and Setup installer are
unsigned and Windows SmartScreen may show an unknown-publisher prompt.
