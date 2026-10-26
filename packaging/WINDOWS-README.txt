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

Microsoft Edge WebView2 Runtime is included in _internal\webview2-runtime, so
WebView2 does not have to be installed separately and no internet access is
needed. If a WebView2 Runtime is already installed (registered) on the PC, the
app uses that one, because it updates itself; otherwise it uses the included
copy. To always use the included copy, set the environment variable
ND2WSI_WEBVIEW2_RUNTIME=bundled. The included copy is not updated
automatically: newer app releases bring its security updates.
If you run the app on Windows 10 (not yet verified), it gives Windows'
app-sandbox groups read access to that folder the first time it uses the
included copy. That needs no administrator rights for a folder you extracted
yourself. If the app reports that this folder permission step failed, an
administrator can run once:
icacls "<app folder>\_internal\webview2-runtime" /grant *S-1-15-2-1:(OI)(CI)(RX) *S-1-15-2-2:(OI)(CI)(RX)
Run the app from a drive on this PC, not from a network share.
About the included runtime: WEBVIEW2-RUNTIME-NOTICE.txt and Microsoft's license
files inside _internal\webview2-runtime.

Updates: obtain a newer Windows ZIP from GitHub Releases and extract it into a
new folder after closing the current app. Windows updates are installed
manually.

This portable build does not register file associations or need administrator
access. Windows may display an unknown-publisher SmartScreen prompt because
the executable is not Authenticode signed. Compare the ZIP with the matching
SHA-256 checksum supplied alongside it.

Diagnostic logs: %LOCALAPPDATA%\nd2wsi-viewer\logs
Build version, source provenance and dependencies: build-info.json
Licenses: LICENSE.txt and licenses\
