# Stage follow: viewer contract and ACS adapter boundary

The translucent viewer can follow normalized sample-stage command observations.
Follow is opt-in, requires calibrated image pixels, appears only while the SLACS
program is detected, and moves the image only while the overlay is locked. The
implementation consists
of `nd2wsi/stage_follow.py`, the owned receiver in `translucent_window.py`, and the
camera updates in `static/translucent-v1.js`. It does not connect to SLACS or decode
ACS packets. Legacy SLACS files are unchanged.

## Key source: observing SLACS W/A/S/D (Windows)

On Windows, Follow's default source observes the W/A/S/D keys the user presses in
the legacy SLACS program to jog the ACS stage, and moves the overlay to match. It
is built from `nd2wsi/stage_keys.py` (`SlacsKeyFollower`) and
`nd2wsi/slacs_identity.py`, and feeds the same commanded-displacement path through
`StageFollowReceiver.submit` that the HTTP routes use. SLACS jogs the stage by
its Roughness: `SLACS.py` `onKeyPress` → `moveL/R/U/D` → `acs.move(axis, ±Ro)`
with `Ro = 0.001 * Roughness` mm, so Roughness is micrometres per press. By
default (**Read Roughness from SLACS**, on) Follow reads Roughness and the two
Mirror checkboxes from SLACS's window while it runs; the settings' step field is
then **Fallback step if Roughness can't be read**. With reading turned off the
field is **Step per key press**, and every press moves by it. The choice and the
step are kept in the overlay page's storage (`nd2wsi.stageFollow.stepAuto`,
`nd2wsi.stageFollow.stepUm`) for the current app run only: the page runs in a
private web view whose origin changes with every launch. Like the step, the
choice is fixed while Follow runs. On non-Windows the default source stays the
in-process/HTTP adapter.

**How it works.** A hidden message-only window registers a Raw Input keyboard
*sink* (`RIDEV_INPUTSINK`) on a daemon thread. Each W/A/S/D key-down (autorepeat
included) becomes one commanded `delta` of one step (SLACS's Roughness, or the
manual step) in a screen
direction — W moves the overlay's view up, S down, A left and D right, as
displayed — submitted to the receiver, plus a heartbeat about once a second so
the receiver does not go stale. W/A/S/D key-ups are observed too, only to tell
an autorepeating held key from separate presses. Only these key presses are followed: the source
registers the keyboard alone, so mouse clicks, mouse jogs and other input are
never seen or counted. Presses are counted only while SLACS owns the
foreground window, identified from the process command line's Python script
operand (`SniperCellSorterGUIv3.1.py`), cached per process and failing closed.
A jog key pressed with Ctrl, Alt or a Windows key held is a shortcut and is not
counted; that held state is followed from the same raw keystroke stream. Windows
allows one Raw Input keyboard target per process, so every overlay following in a
window process shares one registration, removed when the last one stops.

**Roughness and Mirror from SLACS.** `nd2wsi/slacs_controls.py`
(`SlacsControlsMonitor`) reads SLACS's controls about every 0.2 s while Follow
runs, in the SLACS process discovery found (`SlacsDiscovery.slacs_pid`). The
Roughness control is a `wx.Slider` with range 1–1000, a native
`msctls_trackbar32` whose position always equals Roughness however it was set
(drag, text box, E/Q, Coarse/Fine/Extra Fine). It is identified by that range;
SLACS's two other sliders have range 1–100, and more than one 1–1000 trackbar is
ambiguous and read as unreadable. **Mirror Horizontal** and **Mirror Vertical**
are `Button` windows with those exact texts. Each W/A/S/D press moves by the last
good Roughness, which is kept while SLACS is busy; until one has been read, or
when the reader is unavailable, the press moves by the fallback step and is
logged as such. Mirror Horizontal (SLACS's `HflipFlag`) reverses W/S, stage axis
1, so the overlay reverses the screen's vertical move for W/S; Mirror Vertical
(`VflipFlag`) reverses A/D, stage axis 0, and the horizontal move for A/D. An
unknown Mirror state is not applied. The settings panel shows what the next
press uses, for example "SLACS Roughness: 10 µm = 15.1 px · Mirror H off, V off"
(image pixels at the overlay's calibration), "SLACS Roughness unreadable — using
10 µm" or "SLACS busy — last Roughness 10 µm". None of this pauses Follow.

**Direction flips.** Two checkboxes in Follow's settings, under "W/A/S/D move
the image in the directions shown on screen", reverse the key source's
movement: **Flip up/down (W/S)** (`flip_y`) reverses the on-screen move of W
and S, and **Flip left/right (A/D)** (`flip_x`) that of A and D. They name the
keys on purpose, because SLACS's own Mirror labels are crossed (Mirror
Horizontal reverses W/S). Each reversal flips the sign once more, so they
compose with SLACS's Mirror checkboxes and with `KEY_VIEW_SIGN`: Mirror
Horizontal plus Flip up/down moves W as if neither were on. A flip applies from
the next press, also while Follow runs and while the overlay is locked; unlike
the step it is not fixed for a run, which is safe because the page applies only
differences of the cumulative displacement. A flip never changes the image's
rotation, flip or camera, and it does not apply to the adapter/HTTP source. The
pair last chosen is remembered per file format of the original image (ND2,
including `.nd2svs` caches and OME-Zarr stores built from an ND2; SVS; SDPC;
JPEG; PNG; other) on this PC, in
`%LOCALAPPDATA%\nd2wsi-viewer\stage-follow\flips.json` (on macOS
`~/Library/Application Support/nd2wsi-viewer/stage-follow/flips.json`;
`ND2WSI_FOLLOW_FLIPS_ROOT` overrides the folder). An overlay opens with the
flips remembered for its format, and the hint names it ("Remembered for ND2
files on this PC."). User windows share the file, last write wins; an Agent
window keeps a private copy under its session root and never writes the
user's file. A file whose content is invalid restores nothing and is left as
it is until the next toggle saves over it. A file that cannot be read at all
(no permission, held open by another program, a link) is never saved over: the
toggle still applies to the window and says it could not be remembered, so the
other formats' flips are kept. SVS and SDPC slides are RGB, which the overlay
does not open today, so their entries have no effect yet.

**Laser shot and next well.** In SLACS, `,` (key 44) fires a laser shot and `.`
(key 46) moves the 96-well collection stage (ACS axes 4/5) to the next well;
neither moves the sample stage. While Follow's key source runs, also while the
overlay is unlocked, each physical `,` or `.` press made with SLACS in front is
logged once as `slacs_key`, with the overlay's view centre in original image
pixels (`image_px`, null until the page has reported its view), the Follow
state and displacement, Roughness, Mirror and the flips. The keys are never
followed, counted or sent to the receiver, so the overlay stays where it is.
Autorepeat writes no record per repeat: a held key writes one `slacs_key_hold`
with its repeat count at the key-up. A record means "pressed while SLACS was in
front", not "fired": focus in a SLACS text field can swallow the key. Presses
are logged only while Follow runs; with Follow off nothing is recorded. Each
overlay that follows writes its own record of the same press, with its own
image centre; the records of one press share a `press` number, so count
distinct `press` values for the number of presses. The page reports its view
to the app after each camera change, lock change, Follow start or move, and
window move or resize; a native window drag, whose end the page cannot see, is
noticed within a second.

**No interference (hard rule).** The observer only reads. Raw Input is a sink: it
never consumes, delays, alters, blocks, or fails a keystroke, and SLACS's own
commands to the ACS stage are never touched. The module never calls into SLACS or
the controller. The controls reader only sends read-only queries to SLACS's
controls (`TBM_GETPOS`, `TBM_GETRANGEMIN`, `TBM_GETRANGEMAX`, `BM_GETCHECK`),
always through `SendMessageTimeoutW` with `SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT`
and a 100 ms timeout, never plain `SendMessage`, and reads window texts with
`InternalGetWindowText`, which sends no message. A busy or hung SLACS makes a
read time out and count as busy; it never waits on SLACS.

**Privacy.** The observer receives every keystroke in the system, and only
while Follow's key source runs. Anything other than W/A/S/D and SLACS's `,` and
`.`, down or up, is discarded the instant it arrives — never stored, counted
individually, or logged. W/A/S/D pressed while SLACS is not in front are
tallied only as `ignored_not_slacs`, never recorded individually; `,` and `.`
pressed there, or with a Windows key held, are not even tallied. The session
log records counted presses (`follow_key`), held keys (`follow_key_hold`),
SLACS's laser shot and next well (`slacs_key`, `slacs_key_hold`), foreground
transitions (`slacs_foreground`), start/stop and errors — never other keys.

**Detection.** Discovery searches for SLACS with the bounded PowerShell/CIM query
described below (10 s timeout). Once a search has found it, each poll checks
that process instead of searching again: the process is opened by pid, its
creation time must match the one the search saw (so a reused pid is not
mistaken for SLACS), and a zero-wait `WaitForSingleObject` tells whether it is
still running. A confirmed exit or reused pid starts a new search at once.
Checks that cannot tell, and failed searches, keep the found session for a 15 s
grace, so a brief failure on a slow instrument PC does not stop Follow; after
that the session is dropped and Follow stops as for a loss.

**Session log.**

- `slacs_detection` (window, available, session_id, status): each detection
  change, from the main window and from each translucent window.
- `slacs_query` (ok, seconds, python_processes or reason): each real PowerShell
  search. The detectors share each search, so it names neither window nor
  monitor.
- `slacs_check_failing` (reason, grace_seconds), `slacs_check_recovered`
  (failed_checks, seconds, last_reason) and `slacs_check_gave_up` (failed_checks,
  seconds, last_reason): checks that cannot tell, their recovery, and the end of
  the grace; `slacs_exit_seen` (session_id) on a confirmed exit;
  `slacs_liveness_summary` (checks, failed, max_ms, seconds) every 60 s. These
  also carry `monitor` (`main` or `translucent`, the window that owns the
  detector).
- `slacs_controls` (roughness_um, mirror_h, mirror_v, ok, error): each change of
  Roughness, Mirror or readability.
- `slacs_ui_busy` (latency_ms, error) when reads start timing out, taking longer
  than 250 ms or finding SLACS's window hung, `slacs_ui_responsive` (busy_seconds, max_latency_ms,
  failed_reads) when a fast read succeeds again, and `slacs_ui_latency` (reads,
  failed, p50_ms, max_ms) every 30 s while Follow runs.
- `follow_start` (source, step_source, step_um, ok, slacs_session,
  source_format, flip_x, flip_y, view_rotation and view_flip — the main
  viewer's rotation and flip at handoff — and controls_error when the reader
  could not be created), `follow_keys_start` and `follow_keys_stop` (with
  step_source).
- `follow_key` per counted press: key, seq, delta_um, step_um, step_from
  (`slacs`, `fallback` or `manual`), roughness_um, mirror_h, mirror_v,
  reading_age_ms, slacs_busy, repeat (an autorepeat press of a held key), gap_ms
  (since the previous counted press), delta_px (the move in image pixels),
  flip_x and flip_y.
- `follow_key_hold` (key, presses, seconds) when a held key with at least one
  counted repeat is released.
- `follow_flip` (source `user` or `remembered`, source_format, flip_x, flip_y,
  image_name, view_rotation, view_flip; a user toggle also has previous,
  following, follow_seq — the presses the overlay had received at the toggle;
  a press already on its way can still arrive with the old flips, and each
  `follow_key`'s flip_x and flip_y are the flips that press used — and saved):
  each toggle, and the remembered flips applied when an overlay opens.
  `follow_flip_save_failed` (source_format, flip_x, flip_y, message) and
  `follow_flip_load_failed` (source_format, message) when the flips file cannot
  be written or read.
- `slacs_key` per physical `,` or `.` press in SLACS while Follow runs: key,
  action (`laser_shot` or `next_well`), press (the press's number in this
  window process, counting only logged presses and shared by the records
  several overlays write for one press; null when unknown), modifiers,
  slacs_session, image_name, source_format, image_px, view (zoom, rotation,
  flip, container, window), view_age_ms, view_follow_seq and
  view_displacement_um (the Follow progress the view already shows; compare
  them with follow_seq and displacement_um for presses not yet drawn), locked, follow_status (`waiting`, `following`,
  `paused`, `lost` or `off`), follow_seq, displacement_um, counted, step_source,
  step_um, roughness_um, mirror_h, mirror_v, reading_age_ms, slacs_busy, flip_x
  and flip_y; or context_error instead of the overlay fields when they could
  not be read. `slacs_key_hold` (key, action, press, repeats, seconds, key_up)
  when a held `,` or `.` autorepeated in SLACS; key_up is false when its
  release was lost and the hold ended at the next press. image_px is null
  until the page reports its view, and again after a report is refused.

**Limits.**

- The overlay follows the *commanded* key, not the stage's response: a jog the
  ACS controller rejects (out of range, at a limit) still moves the overlay, which
  then drifts from the physical stage until Follow is turned off and on.
- Stage moves made without W/A/S/D (mouse jog, scripted moves, absolute goto) are
  not seen.
- The keys carry no orientation of their own; W/A/S/D move the overlay in the
  directions shown on screen. Match the orientation in the main viewer (rotate,
  or ⌘/Ctrl+F to flip) before opening the translucent window; the window has no
  image-orientation settings, and Follow's two flips reverse movement only. If,
  on the instrument, the overlay moves opposite to SLACS's live image,
  `KEY_VIEW_SIGN` in `nd2wsi/stage_keys.py` is the single value that reverses
  all four keys; it is deliberately not a user setting.
- The flips are remembered per file format, but the right flips may depend on
  how SLACS's camera relates to its stage rather than on the format. A
  remembered flip could then double-correct when the same format is opened with
  a different main-viewer orientation; `follow_start` and `follow_flip` record
  view_rotation and view_flip so this can be checked afterwards.
- Roughness is read about every 0.2 s, so a press within that time of a
  Roughness change can still move by the previous value. With reading turned
  off, or while Roughness cannot be read, the manual step must match SLACS's
  Roughness, or the overlay moves by the wrong distance per press. SLACS's other
  handlers can set its step without the Roughness control; those moves are not
  seen. When SLACS loses the foreground the status shows "SLACS not in front"
  without pausing; following resumes when it returns.

Follow moves the image only while the overlay is locked. Unlocked, the user
aligns the overlay by hand and nothing moves it: follow replies are still
consumed, so key presses made meanwhile are discarded and never replayed after
locking, and the status reads "Unlocked: align by hand, lock to follow" without
stopping Follow. The lock state is taken when each follow request is sent, not
when its reply arrives: a reply to a request sent while unlocked or while a lock
change is still pending, or answered after the lock changed, never moves the
image, so presses made before locking (or during a lock that then fails) are
not applied late. After locking, each new displacement is added to the current
image center, so following continues from wherever the view was left. The
native window position, zoom, display orientation, LUT, and ROI source geometry
remain unchanged. ROI outlines are redrawn with the camera; their dimensions
are not recalculated from stage motion.

The translucent window shows exactly the rotation and flip of the main viewer
at handoff and has no orientation settings of its own: the earlier **Mirror
image** control, its default mirroring, and Follow's Rotation, Flip stage X and
Flip stage Y alignment settings are gone, and an older context's `mirror_scan`
key is ignored. Follow's **Flip up/down (W/S)** and **Flip left/right (A/D)**
(see **Direction flips**) reverse its movement only. OpenSeadragon's own canvas keys (R rotate, F flip, W/A/S/D and arrow
pan, +/−/0 zoom) are turned off in this window, so its orientation cannot drift
from the main viewer's; align by dragging and scrolling. Nikon Ti/Aperio views
from above and SLACS from underneath in the supplied context, so flip the image
in the main viewer first when SLACS shows it mirrored.

A displacement `(dx, dy)` in micrometres moves the view centre `dx` to the
right and `dy` down on screen, as displayed (`screenImageDelta` in
`translucent-v1.js`). The screen axes are converted to image-pixel directions
the way OpenSeadragon 5.0.1 converts a mouse drag: its point conversions undo
the display rotation but not the flip, so x is negated on a flipped view first.
Each screen axis then spans its micrometres at the active image's `pixelSizeUm`
(stored as `[Y, X]` micrometres per displayed-base pixel) along that direction,
so non-square pixels are handled, and the camera moves in canonical image
coordinates with `centerOnPreservingZoom`. A step therefore moves the content
the same way on screen at any rotation or flip. This is a display convention,
not a verified SLACS-to-image registration. Use the active image metadata,
including any reduced-base calibration, rather than a hard-coded ND2 scale.

**Observed source and calibration.** In the supplied copy of the instrument's
SLACS Python source:

- `LPC_ACS.py:35` opens Ethernet TCP `10.0.0.100:701`. There is no sample-XY
  serial baud rate or serial framing in this module.
- `LPC_ACS.py:58–74,134–146` identifies sample X/Y as axes 0/1 and passes
  millimetres to `acsc.toPoint`. Default mode `'r'` selects `AMF_RELATIVE`;
  every other mode selects flags zero, used for absolute moves. `move()` does
  not wait for completion and does not return a success value.
- `LPC_ACS.py:161–162` reads axes 0/1 sequentially using `getRPosition`.
  These are reference positions, not measured motor feedback.
- Importing `LPC_ACS.py` prompts for a name and appends a log (`:11–16`).
  Constructing `ACS()` starts buffers, homes and moves axes, resets positions,
  and writes motion parameters (`:27–132`). Never import or instantiate this
  wrapper merely to observe a device.
- `SLACS.py:464–468,1490–1499` normally sets `Ro = 0.001 * Rough` mm. Other
  handlers can override `Ro`; adapters must observe actual commands rather than
  infer every displacement from the Roughness control.

SHA-256 of `SLACS.py`:
`7aa89753b45bf6e6aeddd503abae6a60b7a96e14ff88364a7788ab41082660ae`.
SHA-256 of `LPC_ACS.py`:
`8247c19bf288633f66b8ad7a3a45cf7816344b4de3057e1b804dee78df5b75b4`.
The supplied shortcut points to
`C:\Python27\Scripts\SniperCellSorterGUIv3.1.py`; its target contents were not
provided under that filename.

A supplied ITO calibration record (26 October 2026) lists Roughness 50, 10 twice
in a zigzag, and 100 as corresponding micrometre
steps: approximately 1 Roughness = 1 µm. The merged ITO ND2 has calibrated XY
spacing `0.660165814209375 µm/pixel`, 2048 × 2048 pixels, and Red/Green/BF channels.
This supports the displacement scale; it does not establish stage-axis signs,
registration angle, backlash, or physical following precision.

**Normalized local HTTP protocol.** Starting Follow returns a private descriptor
with `url`, `token`, `session_id`, and `protocol: "nd2wsi-stage-follow/1"`.
The connection export adds image identity, dimensions, calibration and units.
Treat the token as a local credential. Use the returned loopback URL and
`Authorization: Bearer <token>`, `Content-Type: application/json`, and a valid
`Content-Length`. Browser Origin requests are rejected; bodies are limited to
4096 bytes. This is a viewer protocol, not the ACS TCP wire protocol.

Every body contains integer `version: 1` and the descriptor's `session_id`.
`POST /motion` additionally requires consecutive integer `seq` values starting
at 1, one `type`, and the corresponding XY field. X is screen right and Y screen
down as the overlay displays the image, the same meaning as the key source:

| Type | XY field, in µm | Meaning |
| --- | --- | --- |
| `delta` | `delta_um` | Add relative commanded displacement. |
| `reference` | `position_um` | First observation only; establish an absolute baseline without panning. |
| `target` | `position_um` | Absolute commanded position; requires an initial reference. |

`POST /heartbeat` contains only `version` and `session_id`; it consumes no
sequence number. There is no feedback message type. Duplicate or older sequence
numbers return `duplicate_or_stale` without repeating movement. A sequence gap
pauses the receiver. After contact has begun, three seconds without valid contact
also pauses it. Send heartbeats during idle observation, without querying hardware
merely to generate them. Resume by switching Follow off/on, obtaining a new
descriptor, and aligning again; do not replay missed movement.

**Remaining adapter work.** Obtain the actually installed `acspy/acsc.py`, package
version, loaded ACSCL DLL version/hash, and a bidirectional TCP 701 capture from
existing normal use. The public [ACSpy implementation](https://raw.githubusercontent.com/petebachant/ACSpy/master/acspy/acsc.py)
wraps a native DLL; it does not expose a packet serializer, and its current version
is not evidence of the installed version. [ACS documentation](https://www.acsmotioncontrol.com/KC/Content/SW/SW_NETLibrary/Document/Setting_and_Reading_Motion.htm)
distinguishes reference from feedback position. A real decoder must establish
framing, reassemble TCP sequence/retransmissions, correlate command outcomes,
filter axes 0/1, and reject lost streams or controller resets. No additional
hardware queries or driver installation are part of this work.

An earlier native Agent-window check using the ITO image verified the previous
design (default mirroring, following while locked or unlocked, a 1 µm command
moving the image by 1.514770953715 CSS pixels at image zoom 1, retained manual
adjustment, absolute ROI-sized moves, ROI/zoom preservation, duplicates and
gaps) with the original ND2, cache, annotations, context and SLACS module hashes
unchanged. `qa/check_native_stage_follow.py` now checks the main viewer's
orientation, screen-direction moves, presses ignored while unlocked, that
OpenSeadragon's canvas keys leave the overlay unchanged, and the absence of
orientation settings. W/A/S/D observation exists only on Windows, so on macOS the
check commands the same receiver through its loopback adapter, with synthetic
process sessions in the real `slacs-<hex>` identity format. It passed on
2026-10-27 in a private Agent window (installed 2.5.4 runtime with this
checkout's code): every commanded move matched to 0 px and the protected hashes
were unchanged. No ACS capture adapter, Windows device validation, physical
precision validation, or installed-app replacement has been completed.

## Windows delivery and automatic discovery requirements — 2026-10-26

The user's SLACS PC is the deployment target. The current native proof ran on
macOS and does not establish Windows compatibility. The user reports that the
viewer is replacing its strict WebView2 dependency because the instrument PC
cannot use that dependency. Integrate stage follow with that replacement renderer;
do not reintroduce WebView2 as a requirement through this feature. Keep normalized
motion handling independent of the rendering host, and adapt the receiver and
viewport bridge to the chosen Windows host. Do not change the legacy SLACS Python
environment to satisfy viewer dependencies.

Ordinary users must not see SLACS connection controls when the program is absent.
On Windows, discovery checks the running program's actual Python command line:
the script is `SniperCellSorterGUIv3.1.py` (a Python file, not a shortcut or a
filename ending in `.py.`). The supplied shortcut previously identified
`C:\Python27\Scripts\SniperCellSorterGUIv3.1.py` as its target. Detect the script
identity rather than every `python.exe` process or mere file presence. A visible
name-entry prompt is part of this program's startup and is not itself evidence
that controller communication is ready.

When that running program is detected, reveal the SLACS connection interface and
offer it once for that process session. Preserve the opt-in tracking action and
distinguish program detection from a validated motion stream. Confirm the observed
TCP `10.0.0.100:701` flow belongs to the intended SLACS session before following
it. Loss of that process/stream must stop tracking and leave an explicit status
in an already active connection panel; do not silently resume with stale offsets.
The normal interface remains free of SLACS controls when no session is detected.

The main window offers the connection once for each detected process session,
across all of this user's viewer windows: each window is its own process, so the
first window to see a session while the user is looking at it (visible and
focused) claims it with an exclusive file in a `slacs-offers` folder beside the
window sessions, and only that window shows the offer. Background windows
neither claim nor give it up. Agent windows claim separately, so automation
never takes a User window's offer. A viewer opened later does not repeat it; a
restarted SLACS process is a new session and is offered again. Claims older than
seven days are pruned, except the claim of the session currently running. If
the folder cannot be written, each window offers once on its own.

The Follow control appears only while SLACS is detected (a user decision,
2026-10-27, replacing the demo line's earlier choice never to hide it); its
settings panel closes when detection is lost. Follow is opt-in, and the page
and the backend both refuse to start it without a detected session, so every
Follow is bound to the detected process: its loss, replacement, ambiguous
detection or a discovery error stops the receiver and the key observer, hides
the control while leaving the loss notice visible, and a new process does not
automatically resume it. The W/A/S/D source still checks on its own that SLACS
is in front. Each detection change (status, availability and session id) is
recorded in the session log as `slacs_detection`, from the main window and from
each translucent window, so a detection that fails on the instrument PC can be
diagnosed afterwards. When a Follow stops on a loss, the backend closes the
receiver, so every later follow reply reports the loss and carries no
movement, and it pushes a revocation to the overlay page. The page drops every follow reply that arrives after the
revocation, including one computed just before the loss, and treats SLACS as
undetected until a fresh discovery reply arrives. A reply that reaches the page before the
revocation was computed before the loss and can apply that earlier movement once;
the page cannot see a loss before any message reports it. Detection identifies
the program, not controller readiness or a validated motion stream.
Detection uses a hidden, bounded local PowerShell/CIM process-metadata query,
shared between viewer windows for two seconds. It checks the interpreter's
actual script argument and identifies a session by process ID and creation
time. It does not execute or import the legacy script. Once found, the process
is checked by its liveness rather than searched for again, with a 15 s grace
for checks that cannot tell (see **Detection** under the key source). Detection
is polled, so appearance and disappearance are not instantaneous; process
metadata that stays unavailable fails closed instead of guessing.

The implementation has not yet been verified on the instrument's Windows setup
or integrated into the separate replacement-renderer branch. It does not decode
ACS traffic, verify ownership of TCP `10.0.0.100:701`, or turn this prototype into
a live hardware-ready release. The main viewer's orientation, key following
only while locked, and unlocked manual alignment are the intended behavior
after discovery.

## Distribution: the SLACS-module line

This branch, `slacs-module`, is released to the lab as `X.Y.Z_SLACS-module` (for
example `v2.5.4_SLACS-module`) after each public release. A further lab build of the
same version adds a revision (`v2.5.4_SLACS-module.2`). Each one is a GitHub
pre-release, never marked latest and never added to `updates/appcast.xml`. It
keeps the session logging.
`nd2wsi/edition.py` marks the line:

- Update checks are off. The update button is disabled, a manual check returns
  before any network request, and Sparkle is never started. A public release
  lacks the SLACS module and logging, so this build must not offer one.
- The Windows build names its ZIP `nd2wsi-viewer-X.Y.Z_SLACS-module-windows-x64.zip`,
  records `edition`, `release_version` and `updates_off` in `build-info.json`, and
  points the SDPC codec source offer at the `vX.Y.Z_SLACS-module` release.
- Public installations never select this line's releases: the update check accepts
  only plain version tags and public asset names.

To release the next version, merge `main` into this branch after the public
release, rebuild on the `windows` workflow, and attach the portable ZIP, its
`.sha256`, `sdpc-codec-sources-windows-x64.zip` and its `.sha256` to a new release
tagged `vX.Y.Z_SLACS-module`. Publish it as a pre-release that is not marked latest.
For another build of the same version, raise `REVISION` in `nd2wsi/edition.py` and tag
`vX.Y.Z_SLACS-module.N`; set it back to 1 when merging a new public version.
The new tag also runs the `windows` and `realdata` workflows.
