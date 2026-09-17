# nd2wsi-viewer 2.1.6 — precise contrast and dependable controls

- Floating-point channels support fractional and very narrow LUT windows.
  Handles, axes, Auto, request parameters and rendering preserve their precision;
  integer channels retain their discrete range behavior.
- Z-stack offsets and navigation respect the recorded acquisition direction.
  Plane numbers remain acquisition indices. Unknown direction metadata does not
  produce a signed micrometre offset.
- A focused Auto-fit checkbox permits application letter shortcuts such as C;
  Space still toggles the checkbox. Pane readiness preserves control focus.
- The comparison L shortcut respects text editing, selection controls,
  composition, modifiers and already-handled events.
- Failed histogram requests retry with bounded backoff. If they still fail,
  Auto becomes an explicit Retry button; manual contrast remains usable.
  Frame changes and closed panes cancel stale work.
- A plate without a viewing cache reports that state instead of a permanent 0%
  progress indicator, and stops unnecessary polling. Cache creation permissions
  and private annotation handling are unchanged.

The opening, update and recovery fixes from 2.1.5 are retained. Users of
2.1.1–2.1.4 should download the latest release manually once to resume automatic
macOS update checks.

The macOS Apple-silicon DMG requires macOS 12 or later. The app is ad-hoc signed,
not Developer ID notarized; Sparkle uses the existing update signing identity.
Windows provides an x64 portable ZIP. Windows 11 ARM64 runs that executable
through x64 emulation, not natively. The EXE is not Authenticode-signed; no 2.1.6
Setup installer is included. Windows 10 Education and local Parallels were not
directly tested for this artifact.

Z direction tests use acquisition metadata with asymmetric home positions and
check unchanged frame indices. Native UI validation also opens a synthetic ND2
reverse stack and checks its labels, navigation and raw pixels.
The public example ND2 files have no Z stack;
physical stage positions from a real reverse acquisition were not measured.

The [GitHub release](https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.1.6)
records the exact source commit, artifact checksums and completed validation.
