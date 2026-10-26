# Stage follow: viewer contract and ACS adapter boundary

The translucent viewer can follow normalized sample-stage command observations.
Follow is opt-in and requires calibrated image pixels. The implementation consists
of `nd2wsi/stage_follow.py`, the owned receiver in `translucent_window.py`, and the
camera updates in `static/translucent-v1.js`. It does not connect to SLACS or decode
ACS packets. Legacy SLACS files are unchanged.

With Follow enabled, stage observations move the image even while the manual
controls are locked; the lock label states this behavior. Unlocking permits manual
alignment. Each new displacement is added to the current image center, retaining
that manual adjustment. The native window position, zoom, display orientation,
LUT, and ROI source geometry remain unchanged. ROI outlines are redrawn with the
camera; their dimensions are not recalculated from stage motion.

Nikon Ti/Aperio views from above and SLACS from underneath in the supplied context.
The translucent display defaults to horizontal mirroring
(`mirror_scan: true`). **Mirror image** is editable while unlocked. Its effective
OSD flip is the original handoff flip XOR `mirrorScan`; toggling it mirrors both
image and ROI outlines without changing ROI source geometry. This display default
follows the user's viewing context, not a measured orientation calibration.

Physical stage XY is first mapped through the separate stage alignment controls
(X/Y sign and angle), then divided by the active image's `pixelSizeUm`, stored as
`[Y, X]` in micrometres per displayed-base pixel. Camera movement uses canonical
image coordinates and `centerOnPreservingZoom`. OSD handles display rotation and
mirror; applying them again to the physical displacement would double-transform
it. Mirror image does not also invert a physical stage axis. The independent
default stage angle of zero and unflipped axes are placeholders, not a
verified SLACS-to-image registration. Use the active image metadata, including
any reduced-base calibration, rather than a hard-coded ND2 scale.

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
at 1, one `type`, and the corresponding XY field:

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

Verification passed 35 backend, 81 window/integration and 56 frontend tests
(172 focused tests; unchanged checks were reused), plus a native Agent-window
check using the ITO image. The native check verified default mirroring, a
1 µm command moving the mirrored image by 1.514770953715 CSS pixels at image
zoom 1, locked following, retained manual adjustment, absolute ROI-sized moves,
ROI/zoom preservation, duplicates and gaps. The original ND2, cache, annotations,
context and SLACS module hashes were unchanged. No ACS capture
adapter, Windows device validation, physical precision validation, or installed-app
replacement has been completed.

## Windows delivery and automatic discovery requirements — 2026-10-26

The user's SLACS PC is the deployment target. The current native proof ran on
macOS and does not establish Windows compatibility. The user reports that the
viewer is replacing its strict WebView2 dependency because the instrument PC
cannot use that dependency. Integrate stage follow with that replacement renderer;
do not reintroduce WebView2 as a requirement through this feature. Keep normalized
motion handling independent of the rendering host, and adapt the receiver and
viewport bridge to the chosen Windows host. Do not change the legacy SLACS Python
environment to satisfy viewer dependencies.

Ordinary users must not see SLACS connection controls. The current prototype's
always-visible Follow/settings UI is therefore not the final delivery behavior.
On Windows, discover the running SLACS program by its actual Python command line:
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

Automatic discovery, conditional UI and integration with the replacement Windows
renderer remain to be implemented and tested on the instrument's Windows setup.
These requirements do not turn the current prototype into a live hardware-ready
release. Default mirroring, locked command following and unlocked manual alignment
remain the intended behavior after discovery.
