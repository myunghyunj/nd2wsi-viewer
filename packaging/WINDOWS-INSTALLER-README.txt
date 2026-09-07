nd2wsi-viewer 1.2.8 for Windows

Installation
Run nd2wsi-viewer-1.2.8-Setup.exe. Setup installs the app and its _internal
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
folder. A different future version requires its documented upgrade procedure.
Do not move only nd2wsi-viewer.exe: it needs the adjacent _internal directory.

Data and validation
Your input scan is read-only. Image caches and annotations are stored beside
the source under nd2wsi/caches and nd2wsi/annotations. Diagnostic logs are in
%LOCALAPPDATA%\nd2wsi-viewer\Logs.
The app executable is the previously verified portable x64 build. The
installer manifest records its exact hash and the small documentation change.
The supplied Windows check tool records the actual tested OS and results;
support targets do not imply that every OS/hardware combination was tested.
The core ND2, JPEG/JPEG2000 SVS, and TIFF ROI paths are covered by smoke tests.
Unused optional imagecodecs modules may need Microsoft's Visual C++ runtime.

This is an unsigned local distribution. It has not been uploaded to GitHub.
