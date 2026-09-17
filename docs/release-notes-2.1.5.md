# nd2wsi-viewer 2.1.5 — reliable opening and recovery

- Keyboard shortcuts work when the first slide becomes ready, without first
  clicking the image. Switching tabs and leaving comparison restore focus
  without taking it from a text field.
- macOS user windows start Sparkle again, and the packaged app enables its
  daily checks. Installation waits for other viewer windows to close normally.
  **Users of 2.1.1–2.1.4 should download 2.1.5 manually once:** those versions
  did not start scheduled checks. Successful manual checks also show their result.
- Annotation conflicts offer an explicit choice to keep recovery drafts and
  allow closing. The source annotations are never overwritten or merged.
  Newer edits, failed draft writes and unpreserved plate sites still block close.
- Missing wells no longer collapse separate sites into one grid cell. Well
  identities disambiguate sparse scans while stage coordinates preserve axis
  direction and transposition. Existing within-column spread remains supported.
- Opening a recognized cache can recover an interrupted SQLite transaction
  through its intact rollback journal. Unknown formats and read-only access
  remain protected; recovery does not rebuild the cache or change the source slide.

This release also includes the service and UI module refactoring previously
integrated on main. Scientific rendering and export formats are unchanged.

The macOS Apple-silicon DMG requires macOS 12 or later. The app is ad-hoc signed,
not Developer ID notarized; the Sparkle feed uses the existing signing identity.
Windows ships as an x64 portable ZIP, with Windows 11 ARM64 using x64 emulation.
The EXE is not Authenticode-signed. No 2.1.5 Setup installer is included.
Windows 10 Education and local Parallels were not directly tested.

The [GitHub release](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.5)
records the exact source commit, checksums and completed validation for these
artifacts. Keep the previous release available as the rollback source.
