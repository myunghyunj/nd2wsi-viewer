# Application boundaries

The viewer has three entry points: `app.py` owns the native application,
`server.py` owns HTTP and registered slides, and `static/app.js` owns a single
viewer pane. Small modules receive the dependencies they need; they do not
import their entry point or create a second copy of its state.

## Desktop application

| Module | Responsibility | Boundary |
| --- | --- | --- |
| `app.py` | Launch options, native bridge, window roles, server startup and platform selection | Creates one API and native window per session; retains compatibility entry points |
| `window_lifecycle.py` | Save-before-close (macOS and Windows) and save-before-update (Sparkle) state machines | Receives the window, lifecycle API and logger; owns request identities, cancellation and worker/UI-thread ordering (AppKit main queue or WinForms `BeginInvoke`) |
| `window_chrome.py` | macOS chrome/menu adapters and native file-drop delivery | Receives the owning window/API; imports native frameworks at the point of use |

Browser state must acknowledge saved annotations before shutdown can release
its server resources. Cancellation belongs to the original request until its
worker has finished; a late callback cannot authorize a later close or update.
Native actions retain their owning window/API rather than selecting whichever
window happens to be first or frontmost. Windows keeps its WebView2 renderer
and standard native frame. Native close of each process's main window is
vetoed on both platforms until that handshake and active exports finish; the
packaged GUI smoke exercises this path once and records it as `close` in its
report. Workers bound every browser call, since pywebview's `evaluate_js`
waits without a deadline: a viewer that cannot answer fails the handshake
instead of wedging it. Calls reach the viewer one at a time, so a stuck viewer
is sent nothing more except editing recovery, which waits behind the script it
undoes. A page whose bridge never became ready while no slide
is open closes at once, and a close that fails again after a failed handshake
asks natively whether to close without confirmed saves. pywebview's bridge
threads run as daemons so a reply stranded by the close cannot keep the
process alive, and `run()` ends a finished diagnostic run with `os._exit`.

## HTTP and scientific I/O

| Module | Responsibility | Boundary |
| --- | --- | --- |
| `server.py` | HTTP parsing/serialization, slide registry, annotation concurrency and export progress | Holds the slide lifecycle lease throughout export preparation, writing and transmission |
| `direct_common.py` | Shared read lifetime, virtual roots, bounded tile cache and RGB level adapter | Owns one process-wide decoded-tile cache; format readers retain source validation and decoding |
| `region_export.py` | ROI planning, format policy, rendering and raw temporary-file ownership | Receives slide state and the shared frame resolver; returns a rendered artifact or a context-managed file artifact |
| `annotation_sidecars.py` | Source/plane/site naming and compatible legacy sidecar migration | Preserves source identity, ambiguous-source refusal and non-default-plane copies |
| `cache_removal.py` | Annotation rescue and identity-checked file/tree removal | Called only after registry authorization; caller retains writer/build locks and transfers descriptor ownership explicitly |

ND2/TIFF exports close their temporary handle before a writer opens it, then
stream in 1 MiB chunks. Their file context remains alive until transmission
finishes; success, writer failure and client disconnection all clean it up.
Rendered exports retain the existing pixel cap, calibration and format policy.
Moving these responsibilities does not change raw pixels, HTTP routes, storage
formats, annotation conflicts or cache-removal authorization.

SVS, SDPC, plate viewing and ROI export import their shared read primitives from
`direct_common.py`. Compatibility imports from `direct.py` resolve to the same
objects, including the single decoded-tile cache. Close waits, cache budgets,
index validation and pixel downsampling are unchanged. Auxiliary-image names
and JPEG requests share one lazy backend resolver while retaining source
fingerprint checks before and after reading.

## Viewer pane

| Module | Responsibility | Injected context |
| --- | --- | --- |
| `static/app.js` | Pane initialization, viewer state, OpenSeadragon, physical comparison, annotations and controller wiring | Owns the single pane state |
| `static/lut-controls-v1.js` | Histogram/curve drawing, full/auto/manual axes, pointer/native scrolling and live-update cadence | Channel, DOM, theme, clock and change/flush/shift callbacks |
| `static/frame-data-v1.js` | Histogram and pixel requests, coalescing, cancellation and retry | Request state, frame identity, fetch, clock and render callbacks |
| `static/floating-windows-v1.js` | Panel focus, geometry, drag/resize, collapse and persistence | Stage, document and storage |
| `static/viewer-geometry-v1.js` | Tile source construction, flip-aware image/element point conversion, rect corners and centre-only camera moves shared by the pane and `translucent-v1.js` | Slide info, viewport and the OpenSeadragon namespace, passed per call; no shared state |
| `static/annotation-store-v1.js` | Annotation sidecar saves: debounce, serialized queue, revision/context guards, 409 recovery drafts and plate-site owner switches | Pane state, fetch, clock, lock/frame queries and status/render/notice callbacks |
| `static/save-status-v1.js` | Auto-save indicator: merges the save-state events of the annotation and LUT channels by precedence (conflict, failed, saving, dirty, saved), the "N s ago" and retry-countdown texts and the one-second ticker | Clock, interval timers and a render callback; no DOM |

`index.html` loads each controller before `app.js`; `translucent.html` loads
`viewer-geometry-v1.js` before `translucent-v1.js`. The same controller modules
are loaded directly by the JavaScript regression harness; tests no longer need
to extract the LUT widget implementation from the application file. All
channels share one live-update scheduler so a shift-drag batches the tile
refresh. Graph navigation changes the axis without changing contrast.

Frame changes invalidate requests before debounce starts. Replies must still
match their request and frame identity before they can update a histogram or
pixel readout. An older finalizer cannot clear a newer request. Panel geometry
and collapse state persist; closed state remains specific to the session.

Annotation saves for one pane are serialized in capture order, so a save for
site A issued before switching to B completes before B's or A's next load
reads the sidecar, and an A -> B -> A return never reinstalls older contents.
A 409 conflict is never overwritten or merged: the snapshot stays in the
failed-save map, is shown again when the user returns to that site, and the
pane can only close with it after the user explicitly keeps the recovery
drafts. Older loads and finalizers are rejected by load-sequence, context and
revision guards. The store keeps these fields on the pane state (`state.ann*`)
so the rest of `app.js` reads them unchanged; only the store writes them.

## Verification and distribution

Behavioral coverage lives in the existing export, annotations, cache, updater,
window-role and JavaScript suites, with additional tests in:

- `tests/test_tiff_channel_reads.py`: native TIFF export reads each channel once
  per tile (read counts), byte-identical to the previous iterator.
- `tests/test_region_export_service.py`: streaming lifetime, disconnect/failure
  cleanup, lifecycle lease, scope and HTTP rejection contracts.
- `tests/test_window_chrome.py`: owned-window drop/menu routing, Unicode paths,
  worker dispatch and diagnostic injection.
- `tests/test_lut_module_js.py`: application wiring, native scroll routing,
  shared shift updates and browser module load order.
- `tests/test_frame_data_js.py`: stale completion rejection, coalescing and retry.
- `tests/test_floating_windows_js.py`: geometry, focus, persistence and unavailable
  local storage.
- `tests/test_annotation_site_transitions_js.py`: save ordering across plate-site
  switches, A -> B -> A recovery, 409 drafts and explicit acceptance, flush before
  update or close, and stale load rejection, through the store module's API.
- `tests/test_annotation_render_js.py`: one delegated pointer listener for every
  annotation mark, the translate-only pan path and every drawn input that forces a
  full annotation render.
- `tests/test_window_close_native.py`: the save veto on a fake WinForms UI thread,
  UI-thread approval, hung and never-ready viewers, and the GUI smoke's close.
- `tests/test_process_exit.py`: a stranded pywebview bridge reply cannot keep a
  closed process alive, and diagnostic runs end once their report is written.

Run `python -m pytest tests -q` in the locked test environment, with Node.js
available. `scripts/fetch_testdata.py` supplies the checksummed public ND2
fixtures for real-data tests. The repository CI also checks Python 3.11–3.13,
wheel installation and the same frozen Windows ZIP on x64 and Windows 11 ARM64
via x64 emulation. Native platform checks supplement these module tests.

The original desktop, export and browser-controller extraction followed the
2.1.4 release; the shared direct-reader extraction is part of
[2.3.1](release-notes-2.3.1.md). Previously published release artifacts and tags
retain their original source.

### Update card and Sparkle ownership

`updater.py` installs one composite `SPUUserDriver`, retaining a
`SPUStandardUserDriver` for fallback, and starts `SPUUpdater` directly. Its menu
target implements `checkForUpdates:` and `validateMenuItem:` using
`canCheckForUpdates`. `_install_app_updater` remains the sole installation path.
The lab edition's existing update guard also disables this path after merging.

`update_card.py` owns the update session on AppKit's main queue. At its first
callback it probes the shell's `nd2wsiUpdateCard.ready` for up to two seconds,
then keeps either card or standard UI until `dismissUpdateInstallation`. Landing
pages and unavailable pages use the standard driver. One bounded page transport
serializes evaluation off main and stops after a hung call. Each page instance,
session and action prompt has its own identity; stale buttons cannot answer a
later choice. Reply blocks are consumed once on main. Normal phase transitions
retire optional cancellation blocks without invoking them; reload/close ends
outstanding card choices safely. Explicit PyObjC block metadata preserves the
Sparkle 2.9.6 argument signatures when forwarding to the standard driver.

`static/update-card-v1.js`, its CSS and markup provide the six card states and
plain-text notes. The shell's existing `nd2wsiUpdateNotice` routes export waits
and close vetoes into an active card. Ready-state dismissal is labelled
**Install on quit**, because Sparkle 2.9.6 permits installation after termination.
A manual no-update result stays visible for five seconds before acknowledgment.
Permission requests remain with the standard driver. Downloaded HTML release
notes are ignored; embedded notes are stripped and capped at 600 characters.
The notes action opens the version's GitHub release page.

The landing page embeds the same card for manual Windows checks, with Sparkle
readiness disabled. It requests `check_for_updates(False)` only after a click.
`open_update_download` accepts a one-use token for the exact compatible asset
returned by the existing OS/CPU selector; it does not accept a URL from JavaScript
or perform another network lookup. Failed browser opens may be retried.

Regression suites: `test_update_card`, `test_update_card_js`, `test_updater*`,
`test_shutdown_ownership`, `test_update_cancel_js`, `test_window_close_js`,
`test_release_selection`, `test_update_guard`, `test_window_close_native` and
`test_astromap_beta_updates`. `qa/check_native_update_card.py` drives every modern
user-driver selector using a stand-in protocol in a private Agent window and
captures all states in both themes without loading Sparkle or installing anything.
A signed download/install/relaunch remains a separate release acceptance gate.
