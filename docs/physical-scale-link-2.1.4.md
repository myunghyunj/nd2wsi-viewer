# 2.1.4: physical scale linking

The comparison link button offers three choices:

- Off: zoom and move independently.
- Link scale: match physical magnification while keeping each view's centre independent.
- Link scale + frame: also link navigation using the existing alignment mapping.

A new comparison starts unlinked with the choice panel open. The scale-only
button has an outlined appearance; full linking uses the filled appearance.
Frame means viewport position, not synchronized acquisition T/P/Z indices.

Physical magnification is measured in micrometres per displayed CSS pixel,
using each image's pixel calibration and displayed orientation. Retina density,
unequal pane widths and pyramid levels do not substitute for calibration.
Registration fit scale affects the alignment coordinates, not the physical zoom.
The shared zoom range is the intersection of the panes' allowed ranges.

The LUT panel omits the Min/Max number-entry row. Displayed endpoint labels,
histogram handles, gamma control, axis zoom/pan, Auto and Reset remain available.

Missing or invalid calibration disables physical linking with an explanation.
Incompatible pixel anisotropy/orientation also pauses linking instead of
claiming equal scale. Scale-only navigation does not change stored alignment
or propagate alignment nudges. Switching modes cancels old pending commands.

## Local review verification

- Production JavaScript tests cover different calibrations and pane widths,
  scale-only panning, fitted alignment, zoom limits, invalid calibration,
  incompatible pixel shapes, cancellation and menu-close layout changes.
- Browser preview used isolated synthetic images calibrated at 0.5 and
  1.0 micrometres per image pixel. Both displayed a 100 micrometre bar at
  88.3125 CSS pixels, then a 50 micrometre bar at 70.6484 CSS pixels after zoom.
- Scale-only dragging moved just the selected view. Full linking moved both.
  Unequal pane widths retained matching 50 micrometre bars at 58.3516 CSS pixels.
- Uncalibrated images disabled both physical modes and showed the reason.
- A menu-close resize could leave magnification unequal; preserving the fresh
  layout synchronization fixes this and has a regression test for both modes.
- Final approved-source local tests: 1,342 passed, 22 skipped and 5 real-data
  tests deselected. Ruff and JavaScript syntax checks also passed.
- Final bundle source comparison, deep/strict signature verification and ND2,
  JPEG-SVS and JPEG2000-SVS package smoke tests passed.
- Installed 2.1.4 passed its native WKWebView slide/bridge smoke in a fresh
  isolated Agent window. The installed app was then opened for user review.

## Approval and publication sequence

Install locally and wait for the user's acceptance. Only after acceptance,
publish 2.1.4 on GitHub, then update main and wait for its CI. If CI fails, fix
the issue and repeat publication and main/CI verification. Clean only reviewed
development artifacts after successful CI, then summarize the completed work.

The user approved the installed 2.1.4 candidate including removal of LUT
number-entry boxes. This source is prepared for publication before main CI,
following that explicit sequence. The GitHub release page records live CI
results and publication status.
