# nd2wsi-viewer 2.1.1 — macOS Apple silicon

- Removed the top-right cache-delete button and its confirmation popup, including
  the unused click handling and styles. Annotation Delete controls are unchanged.
- Existing image caches, source slides and annotations are not deleted or migrated
  by this update. Cache creation and viewing behavior are unchanged.
- Retains the familiar standard viewer by default and independent windows. The
  separate Metal preview remains opt-in, with the same support limits and recovery
  behavior as RC2. Metal integration into the standard GUI is not part of 2.1.1.
- Original app name and bundle identity retained. Update downloads are selected
  for the operating system and CPU. This release includes only a macOS arm64 DMG;
  Windows stays on its existing v2.0.0 release:
  [Windows x64 download](https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.0.0/nd2wsi-viewer-2.0.0-windows-x64.zip)
  ([checksums and release details](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.0.0)).

No new rendering or performance claim is made. The previous paired measurement
and its limitations remain documented in [the RC2 notes](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.0rc2).
The app is ad-hoc signed, not Developer ID notarized.

Validation: 139 relevant existing tests passed, JavaScript syntax and lint checks
passed, and the packaged ND2/SVS viewing and pixel-exact export smoke checks passed.
The installed app's Python modules and static assets match the source checkout.
Installed WKWebView smoke and visual inspection confirmed version 2.1.1, the
removed toolbar button, and the retained annotation panel in isolated Agent windows.
