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
`StageFollowReceiver.submit` that the HTTP routes use. The overlay settings expose
**Step per key press** (µm); set it to SLACS's Roughness, since 1 Roughness ≈ 1 µm.
On non-Windows the default source stays the in-process/HTTP adapter.

**How it works.** A hidden message-only window registers a Raw Input keyboard
*sink* (`RIDEV_INPUTSINK`) on a daemon thread. Each W/A/S/D key-down (autorepeat
included) becomes one commanded `delta` of `step_um` micrometres in a screen
direction — W moves the overlay's view up, S down, A left and D right, as
displayed — submitted to the receiver, plus a heartbeat about once a second so
the receiver does not go stale. Only these key presses are followed: the source
registers the keyboard alone, so mouse clicks, mouse jogs and other input are
never seen or counted. Presses are counted only while SLACS owns the
foreground window, identified from the process command line's Python script
operand (`SniperCellSorterGUIv3.1.py`), cached per process and failing closed.
A jog key pressed with Ctrl, Alt or a Windows key held is a shortcut and is not
counted; that held state is followed from the same raw keystroke stream. Windows
allows one Raw Input keyboard target per process, so every overlay following in a
window process shares one registration, removed when the last one stops.

**No interference (hard rule).** The observer only reads. Raw Input is a sink: it
never consumes, delays, alters, blocks, or fails a keystroke, and SLACS's own
commands to the ACS stage are never touched. The module never calls into SLACS or
the controller.

**Privacy.** The observer receives every keystroke in the system. Anything other
than W/A/S/D is discarded the instant it arrives — never stored, counted
individually, or logged. W/A/S/D pressed while SLACS is not in front are tallied
only as `ignored_not_slacs`, never recorded individually. The session log records
counted presses (`follow_key`), foreground transitions (`slacs_foreground`),
start/stop and errors — never other keys.

**Limits.**

- The overlay follows the *commanded* key, not the stage's response: a jog the
  ACS controller rejects (out of range, at a limit) still moves the overlay, which
  then drifts from the physical stage until Follow is turned off and on.
- Stage moves made without W/A/S/D (mouse jog, scripted moves, absolute goto) are
  not seen.
- The keys carry no orientation of their own; W/A/S/D move the overlay in the
  directions shown on screen. Match the orientation in the main viewer (rotate,
  or ⌘/Ctrl+F to flip) before opening the translucent window; the window has no
  direction settings and no per-key mapping UI. If, on the instrument, the
  overlay moves opposite to SLACS's live image, `KEY_VIEW_SIGN` in
  `nd2wsi/stage_keys.py` is the single value that reverses all four keys; it is
  deliberately not a user setting.
- The step must match SLACS's Roughness, or the overlay moves by the wrong
  distance per press. When SLACS loses the foreground the status shows "SLACS not
  in front" without pausing; following resumes when it returns.

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
at handoff and has no direction settings of its own: the earlier **Mirror
image** control, its default mirroring, and Follow's Rotation, Flip stage X and
Flip stage Y settings are gone, and an older context's `mirror_scan` key is
ignored. OpenSeadragon's own canvas keys (R rotate, F flip, W/A/S/D and arrow
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
orientation, screen-direction moves, presses ignored while unlocked and the
absence of direction settings; it has not been re-run for this behavior, which
the synthetic suite covers. No ACS capture adapter, Windows device validation,
physical precision validation, or installed-app replacement has been completed.

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
time. It does not execute or import the legacy script. Detection is polled, so
appearance and disappearance are not instantaneous; unavailable process
metadata fails closed instead of guessing.

The implementation has not yet been verified on the instrument's Windows setup
or integrated into the separate replacement-renderer branch. It does not decode
ACS traffic, verify ownership of TCP `10.0.0.100:701`, or turn this prototype into
a live hardware-ready release. The main viewer's orientation, key following
only while locked, and unlocked manual alignment are the intended behavior
after discovery.
