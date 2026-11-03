# nd2wsi-viewer 2.5.7

The Windows viewer now starts from a ZIP extracted with Windows Explorer, the Windows download is
tidier, and the update card is easier to read. Displayed and exported pixel values, and stored
annotation, correction and LUT formats, are unchanged.

- **Windows startup fix.** The viewer starts from a ZIP extracted with Windows Explorer. It no longer stops with "Failed to resolve Python.Runtime.Loader.Initialize".
- **Tidier Windows download.** The extracted folder shows only `nd2wsi-viewer.exe`, `_internal` and `READ-ME.txt`. The ZIP holds about 40% fewer files.
- **Compact update card.** Updates show a few summary lines. Select **Details** to read the full notes.
- **macOS updates.** Installing an update no longer waits indefinitely for a stuck slide read. A cancelled update leaves the window in a clear state, and the app declares macOS 14 as its minimum.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
