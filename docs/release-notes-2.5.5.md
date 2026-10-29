# nd2wsi-viewer 2.5.5

This release protects annotations and caches in several cases where 2.5.4 could
lose or misplace them, keeps the stored values of 16-bit grayscale PNG files, and
speeds up plates, cached tiles and annotation drawing. Displayed and exported
pixel values of every format 2.5.4 read correctly are unchanged, and stored
annotation, correction and LUT formats are unchanged.

- **Annotation files are checked before they replace your marks.** Opening an annotation file, or loading a sidecar, that contains an item the viewer cannot draw (an unknown type or a missing coordinate) is refused with the reason. Your existing marks stay as they were and are never saved over. A colour that is not `#rrggbb` is dropped, labels become text, and items without an id get one, so files from converters and other tools open instead of freezing the pane.
- **Typed labels are kept.** Clicking another mark, or placing a new pin or box, while the label editor is open now saves what you typed first.
- **Cache files from elsewhere stay in their folder.** A `.nd2svs` cache file opened directly can no longer place annotation files outside the slide's folder; a cache whose manifest names an invalid plane is refused.
- **Sleep no longer disturbs cache builds on macOS.** A window opened after the Mac slept could treat another window's cache or Shading Correction build as abandoned and remove it while it was being written. Builds now recognise each other across sleep, and on Windows a build left over from before a restart is now recognised as abandoned.
- **16-bit grayscale PNG keeps its values.** These files, such as 12-bit camera images saved as 16-bit PNG, used to be clipped at 255 and look almost white. They now open as one 16-bit channel, and pixel values, histograms and TIFF data exports show the stored values.
- **Updates can wait out an annotation conflict.** While an update is waiting on an annotation conflict, the viewer stays editable so you can choose **Keep recovery drafts**; the update continues on its next attempt.
- **`nd2wsi tidy --remove-corrupt-caches` keeps annotations.** Annotation files inside quarantined caches are copied to the annotations folder before the folder is removed. If that copy fails, the folder is kept.
- **The tracing window keeps the main viewer's orientation.** Once its image had been clicked, the image's built-in keys could rotate (R), flip (F), re-fit (0), zoom (+/-) or pan (W/A/S/D, arrows) the tracing window, so it could drift from the main viewer's orientation. Those keys now do nothing there; Escape, L, Cmd/Ctrl+O and the Left/Right ROI keys work as before.
- **Faster plates and tiles.** Plate thumbnails fill about 5 times faster (192 sites: 8.8 to 1.7 s), cached slides serve 12–24% more tiles per second, zooming and rotating no longer rebuild every annotation, plate playback follows frame arrival and pauses in hidden panes, and Shading Correction prepares its first reduced level in parallel.
- **Reliability.** Files opened from Finder while an earlier batch is still being handed over are no longer dropped; on macOS, closing a window while an update is being prepared is refused with a notice; quitting is no longer held open by a stalled slide read; exporting with every channel hidden is refused with a notice; note text being typed is kept when the browser closes; a tab can be closed during tile reads and is held only by real exports; and Windows sizes rendered exports from installed memory instead of assuming 8 GB.
- **Release builds.** The macOS app is built from the same locked dependency versions as the tests and the Windows build.

### Downloads

- macOS: Apple silicon, macOS 14 or later. The app is ad-hoc signed; Sparkle update metadata is signed. It is not Developer ID signed or notarized.
- Windows: x64 portable ZIP; also supports Windows 11 ARM64 through x64 emulation. Extract the whole ZIP and keep `_internal` beside the executable. WebView2 is required; the executable is not Authenticode-signed.
- Corresponding LGPL SDPC decoder sources and checksums are included for both platforms.
