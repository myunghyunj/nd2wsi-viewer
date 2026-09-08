nd2wsi-viewer 2.0.0 for Windows

Installation
Run nd2wsi-viewer-2.0.0-Setup.exe. Setup installs the app and its _internal
folder together in %LOCALAPPDATA%\Programs\nd2wsi-viewer for your Windows
account. Start the app from the Start Menu or its Desktop shortcut.
Python does not need to be installed separately.

Supported target systems
- Windows 10 x64, version 1709/build 16299 or later, including Education.
  Windows 10 Education 22H2 is the recommended Windows 10 target.
- Windows 11 x64 on Intel/AMD.
- Windows 11 ARM64 using Windows x64 emulation.
Windows 10 ARM64 and 32-bit Windows are not supported by this x64 app.

Prerequisites
.NET Framework 4.6.2 or newer is required; current Windows 10 Education
normally includes a newer version. Setup checks this before installing.
Setup reuses Microsoft Edge WebView2 Runtime, or offers its official
Microsoft bootstrapper if missing. That prerequisite download needs Internet.

Removal and reinstall
Use Windows Settings > Apps, or Uninstall nd2wsi-viewer in the Start Menu.
Removal keeps user-added images, annotations, caches, and other user files.
Run this same Setup again to reinstall the same version in its registered
folder. To change from 1.x to 2.0.0, close the viewer and uninstall the previous
app first, then run the new Setup. If retained user files leave the old app
folder nonempty, choose a new empty installation folder; do not delete research
files to satisfy Setup. Different registered versions are not silently replaced.
Do not move only nd2wsi-viewer.exe: it needs the adjacent _internal directory.

Data and validation
Your input scan is read-only. Image caches and annotations are stored beside
the source under nd2wsi/caches and nd2wsi/annotations. Diagnostic logs are in
%LOCALAPPDATA%\nd2wsi-viewer\Logs.
New v2 image caches are single .nd2svs files containing the existing compressed
Zarr data and metadata. This is not an Aperio .svs file. Source-backed overview
caches still need the original acquisition, and annotations remain separate.
Opening a matching legacy managed cache can create a verified .nd2svs copy;
automatic opening preserves the old cache directory. Version 1.x cannot read
the new container. Close the viewer and any migration writer before moving or
deleting caches. Do not discard a SQLite journal file while a write is active.

The installer manifest records the exact portable executable and documentation
hashes for this build. The v1.2.8 Windows 11 ARM64-emulation verification is
historical evidence, not a v2.0.0 Windows or hardware verification result.
The supplied Windows check tool records the actual tested OS and results;
support targets do not imply that every OS/hardware combination was tested.
The build requires smoke checks for ND2, JPEG/JPEG2000 SVS, and TIFF ROI paths.
Unused optional imagecodecs modules may need Microsoft's Visual C++ runtime.

This distribution is not Authenticode-signed. Check the release page and the
adjacent checksum for publication status and the exact installer being used.
