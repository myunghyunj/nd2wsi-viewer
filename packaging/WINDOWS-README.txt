nd2wsi-viewer for Windows

Download published Windows packages and their matching .sha256 files from:
https://github.com/myunghyunj/nd2wsi-viewer/releases
Keep the matching checksum supplied beside this ZIP.

1. Extract the entire ZIP to a folder on your computer.
2. Open nd2wsi-viewer.exe inside that folder.
3. Use Open to select an ND2 or SVS image. You can also open a file with
   nd2wsi-viewer.exe from Windows' Open with menu.

Keep the _internal folder beside the EXE. Do not run the EXE inside the ZIP.
Python, Nikon NIS-Elements, CUDA, and a dedicated graphics card are not needed.
Image reading and export run on the CPU.

Supported target: current Windows 11, Intel/AMD x64 or ARM64.
On ARM64 devices this x64 package uses Windows 11's built-in x64 emulation;
it is not a native ARM64 binary. Windows 10 on ARM is not supported.

The app uses Microsoft Edge WebView2 Evergreen Runtime, normally included
with Windows 11. If your organization removed it, install the runtime from:
https://developer.microsoft.com/microsoft-edge/webview2/
Select the runtime for your operating system (ARM64 on an ARM64 PC).

Updates: obtain a newer Windows ZIP from GitHub Releases and extract it into a
new folder after closing the current app. Windows updates are installed
manually. The update button opens the releases page for downloads.

This portable build does not register file associations or need administrator
access. Windows may display an unknown-publisher SmartScreen prompt because
the executable is not Authenticode signed. Compare the ZIP with the matching
SHA-256 checksum supplied alongside it.

Diagnostic logs: %LOCALAPPDATA%\nd2wsi-viewer\logs
Build version, source provenance and dependencies: build-info.json
Licenses: LICENSE.txt and licenses\
