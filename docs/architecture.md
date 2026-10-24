# Application boundaries

The viewer has three entry points: `app.py` owns the native application,
`server.py` owns HTTP and registered slides, and `static/app.js` owns a single
viewer pane. Small modules receive the dependencies they need; they do not
import their entry point or create a second copy of its state.

## Desktop application

| Module | Responsibility | Boundary |
| --- | --- | --- |
| `app.py` | Launch options, native bridge, window roles, server startup and platform selection | Creates one API and native window per session; retains compatibility entry points |
| `window_lifecycle.py` | Save-before-close and save-before-update state machines | Receives the window, lifecycle API and logger; owns request identities, cancellation and worker/main-thread ordering |
| `window_chrome.py` | macOS chrome/menu adapters and native file-drop delivery | Receives the owning window/API; imports native frameworks at the point of use |

Browser state must acknowledge saved annotations before shutdown can release
its server resources. Cancellation belongs to the original request until its
worker has finished; a late callback cannot authorize a later close or update.
Native actions retain their owning window/API rather than selecting whichever
window happens to be first or frontmost. Windows keeps its WebView2 renderer
and standard native frame.

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

Run `python -m pytest tests -q` in the locked test environment, with Node.js
available. `scripts/fetch_testdata.py` supplies the checksummed public ND2
fixtures for real-data tests. The repository CI also checks Python 3.11–3.13,
wheel installation and the same frozen Windows ZIP on x64 and Windows 11 ARM64
via x64 emulation. Native platform checks supplement these module tests.

The original desktop, export and browser-controller extraction followed the
2.1.4 release; the shared direct-reader extraction is part of
[2.3.1](release-notes-2.3.1.md). Previously published release artifacts and tags
retain their original source.
