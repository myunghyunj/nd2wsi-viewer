/* nd2wsi viewer ------------------------------------------------------------ */
"use strict";
window.Nd2ShortcutRouter?.localizeLabels(document);

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const fmtInt = (v) => Math.round(v).toLocaleString("en-US");

const state = {
  info: null,
  viewer: null,
  channels: [], // enabled channel indices
  luts: [], // per channel {lo, hi, gamma}; null while at store defaults
  lutPersistence: null, // durable, source-bound display preferences
  lutWidgets: [], // canvas LUT widgets, aligned with luts
  shading: null, // committed, source-bound pyramid correction state
  shadingShellToken: null,
  shadingSeq: 0,
  shadingCommandSeq: -1,
  lutAutoRange: false, // ND2 opens fitted; axis navigation never changes contrast
  lutAutoPending: [], // one initial adjustment; manual edits always take priority
  roi: null, // {x, y, w, h} in level-0 pixels
  roiSite: null, // plate site that owns the ROI; null for WSI or no ROI
  roiOverlayEl: null, // projected SVG polygon in raw image coordinates
  tool: null, // null | "roi" | "measure" | "pin" | "box"
  renderScale: null, // chosen PNG/JPEG downsample; null = finest that fits
  astromap: { exporting: false, rows: new Map(), activeId: null },
  annotations: [], // {id, type: "line"|"pin"|"box", ...image coords, text}
  annPath: null, // sidecar file, reported by the server
  annSite: null, // plate site whose sidecar is displayed; null in the grid
  annReady: false, // sidecar load succeeded for the current annotation owner
  editingId: null,
  tempLine: null, // live ruler while dragging
  windows: null, // floating mac-window controllers
  inspect: null, // extended slide metadata, loaded when Slide Info first opens
  inspectLoading: false,
  plate: null, // {t, z, focus, playing, fps, k, ...} for a time series of sites
  annDirty: false, // an edit is waiting for the debounced save
  annRevision: 0,
  annServerRevision: null, // opaque file revision, separate from local edit counter
  annServerRevisions: new Map(), // URL -> revision from our GET/committed save only
  annContext: 0, // changes when a plate switches to another site's sidecar
  annSaveTail: Promise.resolve(),
  annFailedSaves: new Map(), // URL -> latest captured payload that still needs retry
  quitPreparing: false,
  quitRequestId: null,
  annLoadSeq: 0, // only the newest sidecar load may install its items
  tabCount: 0, // supplied by the same-origin shell for conditional Cmd+digit handling
  pixel: {
    hudVisible: false,
    cursor: null, // latest displayed-base coordinate and element position
    result: null,
    queued: null,
    inFlight: null,
    resultKey: null,
    requests: null,
    timer: null,
    lastStarted: 0,
    retryAfter: 0,
    failed: false,
  },
  histogram: {
    requests: null,
    timer: null,
    ready: false,
  },
  viewportRelay: {
    seq: 0,
    timer: null,
    suppressUntil: 0,
    reopening: false,
    contextChanging: false,
    displayRotation: 0,
    displayFlipped: false,
    commandGate: new window.Nd2SpatialPane.PaneCommandGate(crypto.randomUUID()),
    altHeld: false, // Option held: a drag adjusts the alignment instead of both views
    alignmentDrag: null,
    suppressAlignmentDragEnd: false,
    compare: null, // what the shell says about this pane's part in Compare
  },
  landmark: {
    active: false, // the shell asked for alignment points on this pane
    points: [], // level-0 image coordinates, in placement order
    needed: 4,
    clickToZoom: null, // OpenSeadragon setting restored when the mode ends
    editId: null,
    editRevision: -1,
    pointRevision: 0,
  },
};

const {
  setPlateZ, stepPlateZ, setPlateAuto, setPlateT,
  setPlatePlaying, buildZSlider, renderZSlider, buildTimeLine,
  renderTimeLine, renderPlateAuto, loadPlateFocus, pollPlateStatus,
} = window.Nd2PlateController.createController({
  state, $, clamp, plateZFor,
  plateZText, platePlaneNote, currentFocusSummary, plateGroupKey,
  platePeriodMs, rawValueLabel, fmtTickLabel, fmtHm,
  fmtPeriodWords, fmtInt, paintPlate, plateFrameChanged,
});

// Sidecar persistence: the debounced save, the serialized save queue,
// revision/context guards, failed-save recovery drafts and plate-site owner
// transitions live in annotation-store-v1.js. The store keeps its fields on
// `state` (state.ann*), so the rest of the pane reads them as before; only
// the store writes them. DOM and rendering stay here as callbacks, and the
// wrappers in the annotations section keep the historical function names.
const annotationStore = window.Nd2AnnotationStore.createStore({
  state,
  fetch: (url, init) => fetch(url, init),
  isLocked: () => annLocked(),
  activeFrame: () => activeFrameParams(),
  onStatus: (message) => setAnnStatus(message),
  onRender: () => { renderAnnotations(); rebuildAnnList(); },
  onScope: () => syncFrameScopedControls(),
  onEditorReset: () => { $("ann-editor").hidden = true; },
  onRecoveryNotice: () => updateAnnotationRecoveryNotice(),
  onSaveState: (event) => state.saveStatus?.report(event.channel, event),
});

let frameRequests = null;

window.nd2CaptureViewState = () => window.Nd2ViewState.capture(state);

// Copy display state and validated imported regions; never edit source annotations.
function captureTranslucentView() {
  if (state.quitPreparing) throw new Error("The viewer is closing");
  if (!state.info || (state.info.rgb && !state.info.raster)) throw new Error("Open a fluorescence image first");
  if (state.plate && !activeFrameParams()) throw new Error("Choose a site before opening the translucent viewer");
  if (!spatialImageReady()) throw new Error("Wait for the image to finish opening");
  const viewport = state.viewer.viewport;
  const center = viewport.viewportToImageCoordinates(viewport.getCenter(true));
  const snapshot = {
    version: 1, query: tileQuery(), center: [center.x, center.y],
    zoom: viewport.viewportToImageZoom(viewport.getZoom(true)),
    rotation: viewport.getRotation(), flip: viewport.getFlip(),
  };
  const imported = state.annReady && annotationOwnerReady()
    ? state.annotations.filter(item => item.type === "box" && item.astromap === true &&
      Number.isSafeInteger(item.astromapImportIndex) && item.astromapImportIndex > 0) : [];
  if (imported.length) {
    snapshot.rois = imported.map((item, index) => ({ x: item.x, y: item.y, w: item.w, h: item.h,
      index: index + 1, label: String(item.text || item.astromapImportIndex) }));
    const selected = imported.findIndex(item => item.id === state.astromap.activeId);
    if (selected >= 0) snapshot.roiIndex = selected;
  }
  return snapshot;
}

function openTranslucentViewer() {
  if (state.translucentOpening) return Promise.resolve(false);
  state.translucentOpening = true;
  postViewportState("tools");
  return (async () => {
    try {
      const native = (window.parent !== window ? window.parent : window).pywebview?.api;
      if (typeof native?.open_translucent_viewer !== "function") {
        throw new Error("The translucent viewer is available in the desktop app");
      }
      const snapshot = captureTranslucentView();
      const result = await native.open_translucent_viewer(currentSlideSid(), snapshot);
      if (!result?.ok) throw new Error(result?.message || "Could not open the translucent viewer");
      return true;
    } catch (error) {
      showToast(error.message || "Could not open the translucent viewer");
      return false;
    } finally {
      state.translucentOpening = false;
      postViewportState("tools");
    }
  })();
}

function wireShellToolState() {
  let wasHidden = state.windows.info.isHidden();
  const observer = new MutationObserver(() => {
    const hidden = state.windows.info.isHidden();
    if (wasHidden === hidden) return;
    wasHidden = hidden;
    postViewportState("tools");
  });
  observer.observe($("win-info"), { attributes: true, attributeFilter: ["class"] });
  window.addEventListener("pagehide", () => observer.disconnect(), { once: true });
}

init().catch((e) => {
  $("boot").textContent = "failed to load: " + e.message;
});

async function init() {
  const res = await fetch("api/info");
  if (!res.ok) throw new Error("info endpoint returned " + res.status);
  state.info = await res.json();
  const info = state.info;
  state.channels = info.channels.map((_, i) => i);
  state.luts = info.channels.map(() => null);
  state.lutWidgets = [];
  // before the persistence controllers register their pagehide disposers, so
  // the keepalive flush in wireAutoSave() runs ahead of them
  wireAutoSave();
  await initializeDisplayLuts();
  const LatestRequestGate = window.Nd2LatestRequest && window.Nd2LatestRequest.LatestRequestGate;
  if (!LatestRequestGate) throw new Error("latest-request helper did not load");
  state.pixel.requests = new LatestRequestGate();
  state.histogram.requests = new LatestRequestGate();
  frameRequests = createFrameRequests();

  if (info.kind === "plate" && info.plate) {
    state.plate = {
      t: 0,
      z: clamp(Number(info.plate.zHome) || 0, 0, Math.max(0, info.plate.Z - 1)),
      focus: Number(info.plate.P) === 1 ? 0 : null, // a single-position stack opens focused
      playing: false,
      fps: 8,
      k: 8, // reduction of the grid frames; 4 when the cells grow past 260 px
      timer: null,
      everFocused: false,
      stale: false, // the hidden viewer still shows an older frame
      placed: [], // sites sorted by row then col of the current arrangement
      transposed: true, // conditions as rows; the button flips it, remembered
      auto: false, // every site shows its own sharpest plane
      focusMap: null, // {best: [[z per site] per time point], measured, total}
      loaded: new Map(), // "t/<plane>" -> the site frames that have arrived
      view: {
        siteLabels: null, // decided after the site-name pattern is known
        timeline: Number(info.plate.T) > 1,
        zAxis: Number(info.plate.Z) > 1,
      },
    };
  }

  document.title = info.name + " — nd2wsi-viewer";
  $("file-name").textContent = info.name;
  $("file-dims").textContent =
    fmtInt(info.width) + " × " + fmtInt(info.height) + " px · " + info.dtype +
    (info.rgb ? " · RGB" : " · " + info.channels.length + " ch");
  fillContextBadges(info);
  $("plane-note").textContent = planeNote(info);

  const dpr = window.devicePixelRatio || 1;
  if (dpr > 1) {
    $("hw-cell").hidden = false;
    $("hw-val").textContent = "Retina " + (Math.round(dpr * 10) / 10) + "×";
  }

  buildWindows();
  wireSlideInspector();
  wireTheme();
  wireDragForward();
  buildChannelPanel();
  buildLevelLamps();
  buildViewer();
  wireAnnotationPointer();
  wireAstromapKeys();
  wireShellToolState();
  buildPlate();
  if (state.plate) pollPlateStatus();
  wireCompareRelay();
  initializeShadingCorrection();
  wireTools();
  wireAstromap();
  syncFrameScopedControls();
  wireKeys();
  loadAnnotations();
  if (annLocked()) {
    for (const id of ["tool-measure", "tool-pin", "tool-box", "ann-open"]) {
      const el = $(id);
      if (el) {
        el.disabled = true;
        el.title = "The source file is missing; annotation is locked";
      }
    }
    setAnnStatus("Annotation locked: source file missing");
  }
}

function initializeShadingCorrection() {
  state.shading = window.Nd2ShadingCorrection.createController({
    readContext: activeFrameContext,
    fetch: (...args) => fetch(...args),
    isBlocked: () => state.quitPreparing || !spatialImageReady(),
    onState: () => { postShadingState(); renderPixelInspector(); },
    onError: showToast,
    onNotice: showToast,
    onGeneration: refreshShadingPyramid,
  });
  state.shading.load();
  window.addEventListener("pagehide", () => state.shading.dispose(), { once: true });
}

async function refreshShadingPyramid(generation, captured) {
  const info = state.info;
  if (activeFrameContext()?.key !== captured.key) return;
  const response = await fetch("api/info", { cache: "no-store" });
  if (!response.ok) throw new Error("Could not load the rebuilt pyramid (HTTP " + response.status + ")");
  const next = await response.json();
  if (state.info !== info || activeFrameContext()?.key !== captured.key) return;
  if (next.generation !== generation) throw new Error("The pyramid changed again while loading; retrying");
  if (next.width !== info.width || next.height !== info.height || next.kind !== info.kind ||
      !Array.isArray(next.channels) || next.channels.length !== info.channels.length ||
      next.channels.some((channel, index) => channel.name !== info.channels[index].name)) {
    throw new Error("The rebuilt pyramid does not match this slide's geometry or channels");
  }
  // Preserve effective LUT values even if a channel still uses its defaults.
  // Keep this metadata object: the source-bound persistence controller owns it.
  info.channels.forEach((channel, index) => {
    if (!state.luts[index]) state.luts[index] = window.Nd2LutTransfer.currentLut(channel, null);
  });
  Object.assign(info, next);
  state.lutAutoPending.fill(false);
  frameRequests?.pixels.invalidate(true);
  frameRequests?.histograms.schedule(0);
  state.inspect = null;
  $("level-lamps").replaceChildren();
  buildLevelLamps();
  updateScaleStrip();
  // The coordinate system is unchanged. Keep camera, orientation, ROI and
  // current annotation objects; only invalidate the old pyramid's requests.
  const opened = await reopenPreservingView(true);
  if (!opened) throw new Error("The rebuilt pyramid could not be displayed");
}

function postShadingState() {
  if (window.parent === window || !state.shading || !state.shadingShellToken) return;
  window.parent.postMessage({
    nd2wsi: "shading-state", version: VIEWPORT_PROTOCOL_VERSION,
    ...state.shading.snapshot(), sid: currentSlideSid(),
    paneInstanceId: state.viewportRelay.commandGate.paneInstanceId,
    shellToken: state.shadingShellToken, seq: ++state.shadingSeq,
    blocked: state.quitPreparing || !spatialImageReady(),
  }, location.origin);
}

function receiveShadingCommand(message) {
  if (message.sid !== currentSlideSid()) return;
  if (message.nd2wsi === "shading-status-request") {
    if (typeof message.shellToken !== "string" || !message.shellToken) return;
    state.shadingShellToken = message.shellToken;
    postShadingState();
  } else if (message.nd2wsi === "shading-command") {
    if (message.shellToken !== state.shadingShellToken ||
        message.paneInstanceId !== state.viewportRelay.commandGate.paneInstanceId ||
        !Number.isSafeInteger(message.commandSeq) || message.commandSeq <= state.shadingCommandSeq) return;
    const status = state.shading?.snapshot();
    if (message.action === "dismiss") {
      if (status?.outcome && status.contextKey === message.contextKey) {
        state.shadingCommandSeq = message.commandSeq;
        state.shading.dismissOutcome(message.contextKey);
      } else postShadingState();
      return;
    }
    if (!status || window.Nd2ShadingCorrection.nextAction(status) !== message.action ||
        status.contextKey !== message.contextKey ||
        (message.action !== "cancel" && (message.confirmed !== true ||
          status.generation !== message.generation || status.state !== message.state))) {
      postShadingState();
      return;
    }
    if (message.action === "start") {
      const method = message.method === undefined ? "bffree" : message.method;
      if (!window.Nd2ShadingCorrection.methodSupport(status, method).supported) {
        postShadingState();
        return;
      }
      state.shadingCommandSeq = message.commandSeq;
      state.shading?.activate(message.action, message.contextKey, true, method);
    } else {
      state.shadingCommandSeq = message.commandSeq;
      state.shading?.activate(message.action, message.contextKey, message.confirmed === true);
    }
  }
}

async function initializeDisplayLuts() {
  state.lutPersistence = window.Nd2LutPersistence.createController({
    info: state.info,
    readLuts: () => state.luts,
    fetch: (...args) => fetch(...args),
    reportError: (message) => showToast(String(message)),
    onSaveState: (event) => state.saveStatus?.report(event.channel, event),
  });
  const saved = await state.lutPersistence.load();
  if (saved) state.luts = saved;
  // An explicit renderer handoff represents the view the user just left.
  // It overrides persisted values; both take precedence over initial Auto.
  const handoffApi = window.parent !== window ? window.parent.pywebview?.api : null;
  if (handoffApi?.initial_view_state) {
    const display = await handoffApi.initial_view_state(currentSlideSid());
    if (display) {
      window.Nd2ViewState.applyDisplay(state, display);
      state.initialViewHandoff = display;
      state.lutHandoffOutsideSavedRange = !state.luts.every((lut, i) =>
        window.Nd2LutTransfer.fitsChannel(state.info.channels[i], lut, state.info.dtype));
      if (state.lutHandoffOutsideSavedRange) {
        showToast("This Metal LUT remains visible but is outside the standard viewer's saved range. Adjust the LUTs here to remember them.");
      } else state.lutPersistence.changed();
    }
  }
  initializeLutAuto();
  // Tab close, native quit and updates await flush before the page is removed.
  window.addEventListener("pagehide", () => state.lutPersistence.dispose(), { once: true });
}

function initializeLutAuto() {
  const nd2 = /\.nd2$/i.test(String(state.info.name || ""));
  state.lutAutoRange = nd2;
  state.lutAutoPending = state.info.channels.map((_, i) =>
    nd2 && !state.initialViewHandoff && !state.luts[i]);
}

function acceptLutHistograms(channels) {
  channels.forEach((histogram, i) => {
    const widget = state.lutWidgets[i];
    if (!widget || !widget.setHistogram(histogram)) return;
    if (state.lutAutoPending[i]) {
      state.lutAutoPending[i] = false;
      widget.auto();
    }
  });
}

function createFrameRequests() {
  const shared = {
    readContext: activeFrameContext, matchesResponse: responseMatchesFrameContext,
    appendFrameParams, isAbortError: window.Nd2LatestRequest.isAbortError,
    fetch: (...args) => fetch(...args),
  };
  return {
    histograms: window.Nd2FrameData.createHistogramController({
      ...shared, state: state.histogram, onClear: clearHistograms,
      onStatus(status) { setHistogramReady(status === "ready", false, status); },
      onHistograms(channels) {
        acceptLutHistograms(channels);
      },
    }),
    pixels: window.Nd2FrameData.createPixelProbeController({
      ...shared, state: state.pixel, onRender: renderPixelInspector,
      onClearCursor() {
        $("pos-um").textContent = "–";
        $("pos-px").textContent = "–";
        $("pos-px").title = "";
      },
    }),
  };
}

function kindMark(info) {
  // two words: 2D or 3D from the z axis, SLIDE or PLATE from the scan
  // positions. A plate file reports its loops; a converted scan keeps the
  // counts in its notes ("file has 7 Z planes", "file has 6 positions").
  const notes = (info.notes || []).join("\n");
  const zPlanes = info.plate ? Number(info.plate.Z) : Number((notes.match(/file has (\d+) Z planes/) || [])[1] || 1);
  const positions = info.plate ? Number(info.plate.P) : Number((notes.match(/file has (\d+) positions/) || [])[1] || 1);
  return (zPlanes > 1 ? "3D" : "2D") + " " + (positions > 1 ? "PLATE" : "SLIDE");
}

function fillContextBadges(info) {
  // the app mark names the kind of file; the details live in Slide Info
  $("source-badge").textContent = kindMark(info);
  $("file-dims").hidden = info.kind === "plate";

  const storage = $("storage-badge");
  const storageLabels = {
    compact: "Compact cache",
    full: "Portable pyramid",
    direct: "Direct source",
    "overview-degraded": "Overview only",
  };
  storage.textContent = storageLabels[info.storage] || "";
  storage.hidden = !storage.textContent;
  storage.dataset.state = info.storage === "overview-degraded" ? "warning" : "";
  storage.title = info.storage === "compact"
    ? "Native pixels come from the ND2; reduced levels live in the cache"
    : info.storage === "direct"
      ? "The source already contains a usable pyramid"
      : info.storage === "overview-degraded"
        ? "The source is unavailable; only cached reduced levels can be shown"
        : "A self-contained pyramid is stored on disk";

  const calibration = $("calibration-badge");
  calibration.textContent = info.calibrated ? "Calibrated" : "Pixels only";
  calibration.dataset.state = info.calibrated ? "ok" : "warning";
  calibration.title = info.calibrated
    ? "Physical measurements use calibration stored in the source"
    : "No physical calibration was found";
}

function planeNote(info) {
  if (state.plate) return platePlaneNote();
  const s = info.selection || {};
  const bits = [];
  if (s.t !== undefined && s.t !== 0) bits.push("t=" + s.t);
  if (s.z !== undefined && s.z !== 0) bits.push("z=" + s.z);
  if (s.p !== undefined && s.p !== 0) bits.push("pos=" + s.p);
  if ((info.notes || []).length && bits.length === 0) {
    const zn = (info.notes.find((n) => n.startsWith("Z:")) || "").replace("Z: ", "");
    if (zn) bits.push(zn);
    const z = info.selection && info.selection.z;
    if (z !== undefined && z !== "max") bits.push("z=" + z);
  }
  return bits.join("  ");
}

/* ---- tile source ---------------------------------------------------------- */

function lutParam() {
  // one lo:hi[:gamma] slot per channel; empty slot = store default
  const luts = state.luts;
  if (!luts.some((l) => l)) return "";
  const discrete = /^(?:u?int\d+|bool|[<>=|]?[iu]\d+)$/.test(String(state.info.dtype || ""));
  const limit = (value) => String(discrete ? Math.round(value) : value);
  return state.info.channels
    .map((ch, i) => {
      const l = luts[i];
      if (!l) return "";
      const g = l.gamma === 1 ? "" : ":" + String(l.gamma);
      return limit(l.lo) + ":" + limit(l.hi) + g;
    })
    .join(",");
}

function channelQuery(q) {
  // the visible channel set, the one rule for every request that names it:
  // omitted when every channel shows, "none" when none does. Tiles and
  // previews then draw black, as the screen does; exports refuse that case
  // before they build a URL (downloadRoi)
  if (state.channels.length !== state.info.channels.length)
    q.set("c", state.channels.length ? state.channels.join(",") : "none");
  return q;
}

function renderParams(q) {
  // the channel set, the LUT windows and the cache generation, shared by
  // tiles, plate frames, rendered exports and the export-scale previews
  channelQuery(q);
  const win = lutParam();
  if (win) q.set("win", win);
  // the cache generation makes tile URLs immutable: the browser may keep
  // them, and a rebuilt cache changes the URLs instead of serving stale
  if (state.info.generation) q.set("g", state.info.generation);
  return q;
}

function activeFrameParams() {
  // A multi-site grid has no user-selected frame. User operations must never
  // inherit the hidden viewer's P0 merely because it needs a valid tile source.
  const api = window.Nd2LatestRequest;
  return api.plateFrame(state.plate, plateZFor, false);
}

function backingFrameParams() {
  // Renderer plumbing still needs one explicit frame while OSD is hidden by
  // the grid. This is the only place where P0 is an intentional fallback.
  const api = window.Nd2LatestRequest;
  return api.plateFrame(state.plate, plateZFor, true);
}

function appendFrameParams(q, frame) {
  return window.Nd2LatestRequest.appendFrameParams(q, frame);
}

function activeFrameContext() {
  const frame = state.plate ? activeFrameParams() : null;
  if (state.plate && !frame) return null;
  const sourceId = currentSlideSid();
  const generation = state.info && state.info.generation;
  return {
    sourceId,
    generation,
    frame,
    key: window.Nd2LatestRequest.identityKey(sourceId, generation, frame),
  };
}

function responseMatchesFrameContext(payload, context) {
  const current = activeFrameContext();
  return !!(
    current && context && current.key === context.key &&
    window.Nd2LatestRequest.responseIdentityMatches(payload, context)
  );
}

function plateZFor(p) {
  // the plane a site shows: the one the user set, or with autofocus on the
  // sharpest plane measured for that site at this time point
  const pl = state.plate;
  if (!pl) return 0;
  return window.Nd2PlateUI.displayedZ(pl.focusMap, pl.t, p, pl.z, state.info.plate.Z, pl.auto);
}

function plateGroupKey() {
  // frames of one time point group under the plane they share; with
  // autofocus the sites differ, so they group under the mode instead
  const pl = state.plate;
  return pl.auto ? "a" : String(pl.z);
}

function tileQuery() {
  const q = appendFrameParams(renderParams(new URLSearchParams()), backingFrameParams());
  const s = q.toString();
  return s ? "?" + s : "";
}

function makeTileSource() {
  // tileQuery is read per tile request so a tile refresh (LUT, channel
  // switch, frame change) picks up the current settings without a reopen
  return window.Nd2ViewerGeometry.makeTileSource({
    info: state.info, OSD: OpenSeadragon, query: tileQuery,
  });
}

function flipAwareViewerElementPoint(point) {
  return window.Nd2ViewerGeometry.flipAwareElementPoint(
    state.viewer?.viewport, point, OpenSeadragon);
}

function viewerElementToImagePoint(point) {
  return window.Nd2ViewerGeometry.viewerElementToImagePoint(
    state.viewer.viewport, point, OpenSeadragon);
}

function imageToViewerElementPoint(point) {
  return window.Nd2ViewerGeometry.imageToViewerElementPoint(
    state.viewer.viewport, point, OpenSeadragon);
}

function buildViewer() {
  const viewer = OpenSeadragon({
    id: "stage",
    tileSources: makeTileSource(),
    prefixUrl: "",
    showNavigationControl: false,
    showNavigator: true,
    navigatorPosition: "TOP_RIGHT",
    navigatorSizeRatio: 0.16,
    navigatorAutoFade: false,
    navigatorRotate: true,
    navigatorDisplayRegionColor: "#ffffff",
    animationTime: 0.6,
    springStiffness: 8,
    zoomPerScroll: 1.4,
    minZoomImageRatio: 0.7,
    // hardware-matched overzoom: cap at ~3 DEVICE pixels per image pixel,
    // whatever the display density (OSD works in CSS pixels here)
    // a stitched scan stops at 1.5 screen pixels per image pixel, which is
    // plenty for its tiny pixels; a single camera field of a plate is
    // 2044 px across and wants to be magnified well past that
    maxZoomPixelRatio: state.info.kind === "plate"
      ? 8
      : Math.max(1.25, 3 / (window.devicePixelRatio || 1)),
    imageSmoothingEnabled: true,
    crossOriginPolicy: false,
    drawer: "canvas",
    // bound the work a tile refresh can queue: at most six tile requests in
    // flight and a small decoded-tile cache, so LUT drags stay cheap
    imageLoaderLimit: 6,
    maxImageCacheCount: 120,
  });
  state.viewer = viewer;

  viewer.addHandler("canvas-key", (event) => {
    const original = event.originalEvent;
    // Let the browser compose text, without OSD interpreting a composition
    // key as its built-in rotate, flip, or pan command.
    if (original?.isComposing || original?.keyCode === 229) event.preventDefaultAction = true;
  });
  viewer.addHandler("open", () => {
    applyDesiredDisplayTransform(false);
    if (state.initialViewHandoff) {
      window.Nd2ViewState.applyCamera(viewer, state.initialViewHandoff);
      state.initialViewHandoff = null;
      window.nd2HandoffApplied = true;
    }
    $("boot").style.display = "none";
    updateReadout();
    restoreRoiOverlay();
    if (!state.viewportRelay.reopening) postViewportState("user");
    refreshAstromapPanel();
  });
  viewer.addHandler("animation", () => {
    updateReadout();
    scheduleViewportState();
  });
  viewer.addHandler("animation-finish", () => {
    updateReadout();
    scheduleViewportState();
  });
  viewer.addHandler("update-viewport", () => {
    refreshAnnotationsForViewport();
    moveRoiOverlay();
    updateScalebar(state.plate && state.plate.focus === null ? plateCellZoom() : null);
    scheduleViewportState();
  });
  for (const event of ["rotate", "flip", "resize"]) {
    viewer.addHandler(event, () => updateScalebar(
      state.plate && state.plate.focus === null ? plateCellZoom() : null
    ));
  }
  viewer.addHandler("open", renderAnnotations);
  viewer.addHandler("open-failed", () => showToast("could not open tile source"));
  viewer.addHandler("tile-load-failed", debounceToast("some tiles failed to load"));

  const stage = $("stage");
  stage.addEventListener("mousemove", (ev) => {
    state.viewportRelay.altHeld = ev.altKey;
    const pt = elementPoint(ev, stage);
    const img = viewerElementToImagePoint(pt);
    updateCursor(img, pt);
  });
  stage.addEventListener("mouseleave", () => updateCursor(null, null));
  stage.addEventListener("pointerdown", (ev) => {
    state.viewportRelay.altHeld = ev.altKey;
    beginAlignmentDrag(ev);
  }, true);
  viewer.addHandler("canvas-drag", updateAlignmentDrag);
  viewer.addHandler("canvas-drag-end", (event) => {
    if (!state.viewportRelay.suppressAlignmentDragEnd) return;
    event.preventDefaultAction = true; // no flick or constraint jump after the precise drag
    state.viewportRelay.suppressAlignmentDragEnd = false;
    finishAlignmentDrag();
  });
  window.addEventListener("pointerup", () => finishAlignmentDrag(), true);
  window.addEventListener("pointercancel", () => finishAlignmentDrag(true), true);
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Alt") state.viewportRelay.altHeld = true;
  });
  window.addEventListener("keyup", (ev) => {
    if (ev.key === "Alt") state.viewportRelay.altHeld = false;
  });
  window.addEventListener("blur", () => { state.viewportRelay.altHeld = false; });

  $("zoom-in").onclick = () => viewer.viewport.zoomBy(1.6).applyConstraints();
  $("zoom-out").onclick = () => viewer.viewport.zoomBy(1 / 1.6).applyConstraints();
  $("zoom-fit").onclick = () => viewer.viewport.goHome();
}

function reopenPreservingView(preservePose = false) {
  const viewer = state.viewer;
  const bounds = viewer.viewport.getBounds();
  refreshSpatialContext(preservePose);
  let finish;
  const completed = new Promise(resolve => { finish = resolve; });
  const gate = state.viewportRelay.commandGate;
  const capturedIdentity = JSON.stringify(gate.envelope());
  const capturedCommandSeq = gate.latestCommandSeq;
  clearTimeout(state.viewportRelay.timer);
  state.viewportRelay.timer = null;
  state.viewportRelay.reopening = true;
  state.viewportRelay.suppressUntil = Number.POSITIVE_INFINITY;
  postSpatialReadiness(); // stop shell spatial work while the tile source is closed
  const cleanup = () => {
    viewer.removeHandler("open", restored);
    viewer.removeHandler("open-failed", failed);
  };
  const restored = () => {
    cleanup();
    applyDesiredDisplayTransform(false);
    if (capturedIdentity === JSON.stringify(gate.envelope()) &&
        capturedCommandSeq === gate.latestCommandSeq) viewer.viewport.fitBounds(bounds, true);
    restoreRoiOverlay();
    state.viewportRelay.reopening = false;
    state.viewportRelay.suppressUntil = Date.now() + 180;
    postViewportState("restore");
    finish(true);
  };
  const failed = () => {
    cleanup();
    state.viewportRelay.reopening = false;
    state.viewportRelay.suppressUntil = 0;
    postSpatialReadiness();
    finish(false);
  };
  viewer.addHandler("open", restored);
  viewer.addHandler("open-failed", failed);
  viewer.open(makeTileSource());
  return completed;
}

/* ---- channels ------------------------------------------------------------- */

function macSwitch(on, onToggle) {
  // macOS 27 kit switch: capsule track + wide PILL knob (not a circle)
  const b = document.createElement("button");
  b.className = "switch" + (on ? " on" : "");
  b.type = "button";
  b.setAttribute("role", "switch");
  b.setAttribute("aria-checked", String(on));
  const knob = document.createElement("span");
  knob.className = "knob";
  b.append(knob);
  b.addEventListener("click", () => {
    const next = !b.classList.contains("on");
    if (onToggle(next) !== false) {
      b.classList.toggle("on", next);
      b.setAttribute("aria-checked", String(next));
    }
  });
  return b;
}

function buildChannelPanel() {
  const info = state.info;
  // RGB ND2/SVS expose their Red/Green/Blue components through the same LUT
  // controls. Their default windows preserve the original brightfield colours.
  const list = $("channel-list");
  const rangeToggle = $("lut-auto-range");
  rangeToggle.checked = state.lutAutoRange;
  rangeToggle.addEventListener("change", () => {
    state.lutAutoRange = rangeToggle.checked;
    state.lutWidgets.forEach((widget) => widget.setAutoRange(state.lutAutoRange));
  });
  wireLutToolbar();
  info.channels.forEach((ch, i) => {
    const row = document.createElement("div");
    row.className = "channel-row";
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = "#" + ch.color;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = ch.label || "Channel " + i;
    const win = document.createElement("span");
    win.className = "win";
    win.textContent = fmtInt(ch.window.start) + "–" + fmtInt(ch.window.end);
    const auto = document.createElement("button");
    auto.className = "lut-auto";
    auto.type = "button";
    auto.title = "Auto-adjust contrast with a robust bright limit; keep bright backgrounds and ignore zero padding";
    auto.textContent = "Auto";
    auto.addEventListener("click", (ev) => {
      ev.preventDefault();
      if (state.histogram.failed) { loadHistograms(); return; }
      state.lutWidgets[i].auto();
    });
    const reset = document.createElement("button");
    reset.className = "lut-reset";
    reset.type = "button";
    reset.title = "Reset window and gamma";
    reset.textContent = "↺";
    reset.addEventListener("click", (ev) => {
      ev.preventDefault();
      state.lutWidgets[i].reset();
    });
    const toggle = macSwitch(state.channels.includes(i), (next) => {
      const on = new Set(state.channels);
      next ? on.add(i) : on.delete(i);
      if (!on.size) return false; // keep at least one channel lit
      state.channels = [...on].sort((a, b) => a - b);
      refreshTiles();
    });
    if (info.channels.length < 2) toggle.style.display = "none";
    row.append(sw, name, win, auto, reset, toggle);
    list.append(row);
    list.append(buildLutRow(i, ch, win));
  });
  if (info.channels.length > 1) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "Shift-drag adjusts all channels.";
    list.append(hint);
  }
  loadHistograms();
  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = "Scroll to zoom · Drag to pan · Double-click for full range";
  list.append(hint);
}

function wireLutToolbar() {
  const transfer = window.Nd2LutTransfer;
  const dialog = $("lut-dialog");
  const fields = $("lut-fields");
  const error = $("lut-error");
  let inputs = [];
  $("lut-type").onclick = () => {
    $("lut-type").lutTooltip.dismiss();
    fields.replaceChildren();
    error.textContent = "";
    inputs = state.info.channels.map((channel, i) => {
      const row = document.createElement("fieldset");
      row.className = "lut-input-row";
      const legend = document.createElement("legend");
      legend.textContent = channel.label || "Channel " + i;
      const values = document.createElement("div");
      values.className = "lut-input-values";
      const lut = transfer.currentLut(channel, state.luts[i]);
      const entry = {};
      for (const [key, caption] of [["lo", "Minimum"], ["hi", "Maximum"], ["gamma", "Gamma"]]) {
        const label = document.createElement("label");
        label.textContent = caption;
        const input = document.createElement("input");
        input.type = "number";
        input.step = "any";
        input.required = true;
        input.value = String(lut[key]);
        input.setAttribute("aria-label", `${legend.textContent} ${caption}`);
        if (key === "gamma") { input.min = "0.25"; input.max = "4"; }
        label.append(input);
        values.append(label);
        entry[key] = input;
      }
      row.append(legend, values);
      fields.append(row);
      return entry;
    });
    dialog.showModal();
    inputs[0]?.lo.focus();
  };
  $("lut-cancel").onclick = () => dialog.close();
  // Keyboard shortcuts belong to the editor until it closes.
  dialog.addEventListener("keydown", (event) => event.stopPropagation());
  $("lut-form").onsubmit = (event) => {
    event.preventDefault();
    const luts = inputs.map((entry) => Object.fromEntries(
      Object.entries(entry).map(([key, input]) => [key, input.valueAsNumber])));
    const invalid = luts.findIndex((lut, i) => !transfer.fitsChannel(state.info.channels[i], lut, state.info.dtype));
    if (invalid !== -1) {
      error.textContent = `${state.info.channels[invalid].label || "Channel " + invalid}: enter a finite minimum below maximum and gamma from 0.25 to 4. Integer ranges must span at least 1.`;
      return;
    }
    luts.forEach((lut, i) => state.lutWidgets[i].setLut(lut));
    applyLuts.flush();
    dialog.close();
    showToast("LUT values applied");
  };
  wireLutToolbarTooltips();
  $("lut-copy").onclick = () => runLutClipboard("copy");
  $("lut-paste").onclick = () => {
    $("lut-paste").lutTooltip.dismiss();
    return runLutClipboard("paste");
  };
}

function wireLutToolbarTooltips() {
  if ($("lut-copy").lutCopyFeedback) return;
  const controllers = ["type", "copy", "paste"].map((action) => {
    const button = $("lut-" + action);
    const tooltip = $("lut-" + action + "-tooltip");
    const description = tooltip.textContent;
    let hovered = false, hoverReady = false, focused = false;
    let copied = false, dismissed = false, disposed = false;
    let hoverTimer = null, successTimer = null;
    const cancelHover = () => {
      clearTimeout(hoverTimer);
      hoverTimer = null;
    };
    const render = () => {
      tooltip.textContent = copied ? "Copied!" : description;
      button.classList.toggle("is-copied", copied);
      tooltip.hidden = disposed || dismissed || !(hoverReady || focused || copied);
      if (tooltip.hidden) return;
      const anchor = button.getBoundingClientRect();
      const tip = tooltip.getBoundingClientRect();
      const left = Math.max(8, Math.min(anchor.left + (anchor.width - tip.width) / 2,
        window.innerWidth - tip.width - 8));
      const top = anchor.top - tip.height - 8;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${Math.max(8, Math.min(top >= 8 ? top : anchor.bottom + 8,
        window.innerHeight - tip.height - 8))}px`;
    };
    const reset = () => {
      clearTimeout(successTimer);
      successTimer = null;
      copied = false;
      render();
    };
    const dismiss = () => {
      cancelHover();
      hoverReady = false;
      dismissed = true;
      render();
    };
    const handlers = {
      pointerenter() {
        if (disposed) return;
        cancelHover();
        hovered = true;
        hoverReady = focused || copied;
        dismissed = false;
        if (!focused && !copied) hoverTimer = setTimeout(() => {
          hoverTimer = null;
          hoverReady = hovered;
          render();
        }, 1000);
        render();
      },
      pointerleave() { cancelHover(); hovered = hoverReady = false; render(); },
      focus() {
        if (disposed) return;
        cancelHover();
        focused = true;
        hoverReady = hovered;
        dismissed = false;
        render();
      },
      blur() { focused = false; render(); },
    };
    for (const [event, handler] of Object.entries(handlers)) button.addEventListener(event, handler);
    const controller = {
      render, dismiss, reset,
      succeed() {
        if (disposed) return;
        cancelHover();
        clearTimeout(successTimer);
        copied = true;
        hoverReady = hovered;
        dismissed = false;
        render();
        successTimer = setTimeout(reset, 1800);
      },
      dispose() {
        disposed = true;
        cancelHover();
        reset();
        for (const [event, handler] of Object.entries(handlers)) button.removeEventListener(event, handler);
      },
    };
    button.lutTooltip = controller;
    if (action === "copy") button.lutCopyFeedback = controller;
    return controller;
  });
  const render = () => controllers.forEach((controller) => controller.render());
  const dismiss = (event) => {
    if (event.key === "Escape") controllers.forEach((controller) => controller.dismiss());
  };
  document.addEventListener("keydown", dismiss, true);
  window.addEventListener("resize", render);
  window.addEventListener("scroll", render, true);
  window.addEventListener("pagehide", () => {
    controllers.forEach((controller) => controller.dispose());
    document.removeEventListener("keydown", dismiss, true);
    window.removeEventListener("resize", render);
    window.removeEventListener("scroll", render, true);
  }, { once: true });
}

async function runLutClipboard(action) {
  const button = $(action === "copy" ? "lut-copy" : "lut-paste");
  const transfer = window.Nd2LutTransfer;
  const info = state.info;
  const host = window.parent?.pywebview;
  const nativePlatform = ["cocoa", "edgechromium"].includes(host?.platform);
  const native = nativePlatform ? host.api : null;
  button.disabled = true;
  if (action === "copy") button.lutCopyFeedback.reset();
  try {
    if (action === "copy") {
      const text = transfer.encode(info.channels, state.luts, info.dtype);
      if (nativePlatform) {
        if (typeof native?.write_lut_clipboard !== "function") throw new Error("Native LUT clipboard writing is unavailable");
        const result = await native.write_lut_clipboard(text);
        if (!result?.ok) throw new Error(result?.message || "Could not copy LUT");
      } else {
        await copyPlainText(text);
      }
      button.lutCopyFeedback.succeed();
      showToast("Channel LUTs copied");
    } else {
      let text;
      if (nativePlatform) {
        if (typeof native?.read_lut_clipboard !== "function") throw new Error("Native LUT clipboard reading is unavailable");
        const result = await native.read_lut_clipboard();
        if (!result?.ok) throw new Error(result?.message || "Could not paste LUT");
        text = result.text;
      } else {
        if (!navigator.clipboard?.readText) throw new Error("Clipboard reading is unavailable in this browser");
        text = await navigator.clipboard.readText();
      }
      if (state.info !== info) return; // Do not apply a late read to a replaced slide.
      const plan = transfer.planPaste(text, info.channels, info.dtype);
      plan.updates.forEach(({ index, lut }) => state.lutWidgets[index].setLut(lut));
      if (plan.updates.length) applyLuts.flush();
      showToast(plan.updates.length
        ? `Pasted ${plan.updates.length} channel LUT${plan.updates.length === 1 ? "" : "s"}${plan.skipped ? ` · ${plan.skipped} skipped` : ""}`
        : "No compatible, uniquely named channels to paste");
    }
  } catch (error) {
    showToast(error.message || "LUT clipboard unavailable");
  } finally {
    button.disabled = false;
  }
}

/* ---- per-channel LUT (NIS-style histogram + window/gamma curve) ------------
   Each channel gets a small canvas: the intensity histogram filled in the
   channel color, a black (low) and white (high) triangle to drag the display
   window, and a knob on the mapping curve that drags vertically to set gamma
   -- the layout NIS-Elements' LUTs panel uses.  Shift-drag applies to all
   channels. */

// Coalesce all channels from one input event, then keep showing the newest
// contrast during a continuous drag instead of waiting for it to stop.
const applyLuts = window.Nd2LutControls.liveUpdate(() => refreshTiles(), 100);
window.addEventListener("pagehide", () => {
  applyLuts.cancel();
  frameRequests?.histograms.dispose();
}, { once: true });

/* Swap the tiles of the open image in place. The tile source builds every
   URL through tileQuery() at request time, so dropping the loaded tiles is
   enough for the next draw to fetch them with the current LUT, channel set
   and frame. This replaces the full viewer reopen that ran on every LUT
   change and let the WebKit content process grow past 3 GB during a drag. */
function refreshTiles() {
  const viewer = state.viewer;
  if (state.plate) {
    // the grid frames follow the same LUT, channel set and t/z; while the
    // stage is hidden the viewer keeps its old tiles and catches up on focus
    paintPlate();
    if (state.plate.focus === null) {
      state.plate.stale = true;
      return;
    }
    state.plate.stale = false;
  }
  // a reopen is already in flight and its tile source reads the current
  // query when it opens, so there is nothing extra to do
  if (state.viewportRelay.reopening) return;
  const item = viewer && viewer.world && viewer.world.getItemAt(0);
  if (!item || typeof item.reset !== "function") {
    reopenPreservingView();
    return;
  }
  resetTiledImage(item);
  const navItem = viewer.navigator && viewer.navigator.world
    && viewer.navigator.world.getItemAt(0);
  if (navItem && typeof navItem.reset === "function") resetTiledImage(navItem);
  viewer.forceRedraw();
}

function resetTiledImage(item) {
  // OpenSeadragon 5 TiledImage.reset() clears this image's tiles from the
  // cache and marks it for redraw. The tile records it keeps in tilesMatrix
  // still carry the URL they were built with, and a fully loaded image skips
  // its tile pass until the viewport moves. Emptying the matrix makes the
  // next pass build fresh records through getTileUrl, and setClip with the
  // unchanged clip is the public call that schedules that pass.
  item.reset();
  if (item.tilesMatrix) item.tilesMatrix = {};
  if (typeof item.setClip === "function" && typeof item.getClip === "function") {
    item.setClip(item.getClip());
  }
}

function buildLutRow(i, ch, winLabel) {
  const widget = window.Nd2LutControls.createWidget({
    channel: ch, dtype: state.info?.dtype,
    initialLut: state.luts[i], autoRange: state.lutAutoRange,
    label: winLabel, width: state.windows.channels.bodyWidth(), document, window,
    protocolVersion: VIEWPORT_PROTOCOL_VERSION, inkColor, currentTheme, fmtInt,
    onChange(lut) {
      if (state.lutAutoPending) state.lutAutoPending[i] = false;
      state.luts[i] = lut;
      if (state.lutHandoffOutsideSavedRange) {
        state.lutHandoffOutsideSavedRange = !state.luts.every((value, index) =>
          window.Nd2LutTransfer.fitsChannel(state.info.channels[index],
            window.Nd2LutTransfer.currentLut(state.info.channels[index], value), state.info.dtype));
      }
      if (!state.lutHandoffOutsideSavedRange) state.lutPersistence?.changed();
      applyLuts(); // shared cadence: one tile reload even for shift-drags
    },
    onFlush: () => applyLuts.flush(),
    onManualAxis() {
      if (!state.lutAutoRange) return;
      state.lutAutoRange = false;
      $("lut-auto-range").checked = false;
      state.lutWidgets.forEach((other) => other.freezeAxis());
    },
    onShiftChange: (lut) => state.lutWidgets.forEach((other) => other.setLut(lut)),
  });
  state.lutWidgets[i] = widget;
  return widget.element;
}

function relayoutLuts() {
  const w = state.windows.channels.bodyWidth();
  state.lutWidgets.forEach((wd) => wd && wd.relayout(w));
}

function setHistogramReady(ready, unavailable = false, status = "loading") {
  state.histogram.ready = !!ready;
  state.histogram.failed = status === "failed";
  document.querySelectorAll(".lut-auto").forEach((button) => {
    if (!button.dataset.readyTitle) button.dataset.readyTitle = button.title;
    button.disabled = !ready && !state.histogram.failed;
    button.textContent = state.histogram.failed ? "Retry" : "Auto";
    button.title = ready
      ? button.dataset.readyTitle
      : unavailable
        ? "Choose a site to calculate its histogram"
        : state.histogram.failed
          ? "Histogram could not be loaded. Click to retry. Manual contrast remains available."
          : status === "retrying"
            ? "Histogram request failed; retrying automatically"
            : "Histogram is loading for the current frame";
  });
}

function clearHistograms(unavailable = false) {
  state.lutWidgets.forEach((widget) => widget && widget.clearHistogram());
  setHistogramReady(false, unavailable);
}

function scheduleHistograms(delay) {
  frameRequests.histograms.schedule(delay);
}

function loadHistograms() {
  scheduleHistograms(0);
}

// Invalidate synchronously, before the debounce window can admit an old frame.
function refreshHistograms() {
  scheduleHistograms(300);
}

function debounce(fn, ms) {
  // the arguments are deliberately not forwarded: several of these are
  // wired straight to events, and the event object is not a parameter
  let t = null;
  const run = () => {
    clearTimeout(t);
    t = setTimeout(() => { t = null; fn(); }, ms);
  };
  run.cancel = () => { clearTimeout(t); t = null; };
  return run;
}

/* ---- readout strip -------------------------------------------------------- */

function buildLevelLamps() {
  const lamps = $("level-lamps");
  state.info.levels.forEach((lv) => {
    const el = document.createElement("span");
    el.className = "lamp";
    el.id = "lamp-" + lv.path;
    el.title = lv.width + " × " + lv.height + " (1/" + lv.downsample + ")";
    el.textContent = "L" + lv.path;
    lamps.append(el);
  });
}

function currentImageZoom() {
  const vp = state.viewer.viewport;
  return vp.viewportToImageZoom(vp.getZoom(true)); // CSS px per image px
}

function deviceZoom() {
  // hardware truth: device px per image px (Retina-aware; OSD renders at DPR)
  return currentImageZoom() * (window.devicePixelRatio || 1);
}

function levelForDensity(density) {
  // the coarsest level whose scale still meets the DEVICE sampling density
  const iz = Math.min(density, 1);
  const levels = state.info.levels; // 0 = full res
  for (let k = levels.length - 1; k >= 0; k--) {
    if (1 / levels[k].downsample >= iz) return k;
  }
  return 0;
}

function activeLevel() {
  return levelForDensity(deviceZoom());
}

function updateReadout() {
  if (state.plate && state.plate.focus === null) {
    updateGridReadout();
    return;
  }
  const dz = deviceZoom();
  $("zoom-val").textContent =
    (dz * 100).toFixed(dz >= 0.1 ? 0 : 1) + " %" + (Math.abs(dz - 1) < 0.005 ? " · 1:1" : "");
  const act = activeLevel();
  state.info.levels.forEach((lv, k) => {
    $("lamp-" + lv.path).classList.toggle("active", k === act);
  });
  updateScalebar(currentImageZoom());
}

function updateGridReadout() {
  // in grid mode the readouts describe a site cell, not the hidden viewer
  const iz = plateCellZoom();
  if (!iz) return;
  const dz = iz * (window.devicePixelRatio || 1);
  $("zoom-val").textContent = (dz * 100).toFixed(dz >= 0.1 ? 0 : 1) + " %";
  const act = levelForDensity(dz);
  state.info.levels.forEach((lv, k) => {
    $("lamp-" + lv.path).classList.toggle("active", k === act);
  });
  updateScalebar(iz);
}

function updateCursor(img, elementPt) {
  if (
    !img || img.x < 0 || img.y < 0 ||
    img.x >= state.info.width || img.y >= state.info.height
  ) {
    $("pos-um").textContent = "–";
    $("pos-px").textContent = "–";
    $("pos-px").title = "";
    state.pixel.cursor = null;
    invalidatePixelProbe(false);
    renderPixelInspector();
    return;
  }

  const x = Math.round(clamp(img.x, 0, Math.max(0, state.info.width - 1)));
  const y = Math.round(clamp(img.y, 0, Math.max(0, state.info.height - 1)));
  state.pixel.cursor = { x, y, point: elementPt };
  const ps = pixelSize();
  $("pos-um").textContent = ps
    ? fmtUm(x * ps[1]) + " , " + fmtUm(y * ps[0])
    : "uncalibrated";
  renderPixelInspector();
  queuePixelProbe(x, y);
}

function pixelResultMatchesCursor(result) {
  const c = state.pixel.cursor;
  const context = activeFrameContext();
  return !!(
    c && result && context && state.pixel.resultKey === context.key &&
    Number(result.x) === c.x && Number(result.y) === c.y
  );
}

function rawValueLabel(value) {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "–";
    if (Number.isInteger(value)) return value.toLocaleString("en-US");
    const abs = Math.abs(value);
    return value.toFixed(abs >= 100 ? 1 : abs >= 1 ? 3 : 5).replace(/\.?0+$/, "");
  }
  return value === null || value === undefined ? "–" : String(value);
}

function pixelValueParts(result) {
  if (!result || !Array.isArray(result.values)) return [];
  return result.values.map((item, i) => ({
    name: String((item && item.name) || "C" + i),
    value: rawValueLabel(item && item.value),
  }));
}

function renderPixelInspector() {
  const cursor = state.pixel.cursor;
  const result = pixelResultMatchesCursor(state.pixel.result) ? state.pixel.result : null;
  const hud = $("pixel-hud");
  if (!cursor) {
    hud.hidden = true;
    return;
  }

  const coords = fmtInt(cursor.x) + " , " + fmtInt(cursor.y) + " px";
  const parts = pixelValueParts(result);
  let summary = coords;
  if (parts.length) {
    summary += " · " + parts.map((p) => p.name + " " + p.value).join(" · ");
  } else {
    summary += state.pixel.failed ? " · values –" : " · …";
  }
  $("pos-px").textContent = summary;
  $("pos-px").title = summary;

  hud.hidden = !state.pixel.hudVisible;
  if (!state.pixel.hudVisible) return;

  $("pixel-hud-coord").textContent = coords;
  const um = result && Array.isArray(result.um) ? result.um : null;
  const ps = pixelSize();
  $("pixel-hud-stage").textContent = um
    ? fmtUm(Number(um[0])) + " , " + fmtUm(Number(um[1]))
    : ps
      ? fmtUm(cursor.x * ps[1]) + " , " + fmtUm(cursor.y * ps[0])
      : "Physical position unavailable";

  const values = $("pixel-hud-values");
  values.replaceChildren();
  if (parts.length) {
    for (const part of parts) {
      const row = document.createElement("div");
      const name = document.createElement("span");
      const value = document.createElement("span");
      name.textContent = part.name;
      value.textContent = part.value;
      row.append(name, value);
      values.append(row);
    }
  } else {
    values.textContent = state.pixel.failed ? "Raw values unavailable" : "Reading raw values…";
  }

  const note = $("pixel-hud-note");
  if (result) {
    const level = result.probed_level === undefined ? "" : " · L" + result.probed_level;
    note.textContent = result.sample_kind === "overview-mean"
      ? "Overview mean" + level
      : "Native pixel" + level;
    if (state.shading?.snapshot().applied) note.textContent += " · Original values before Shading Correction";
  } else {
    note.textContent = state.pixel.failed ? "Probe endpoint unavailable; cursor coordinates still work" : "";
  }
  positionPixelHud(cursor.point);
}

function positionPixelHud(point) {
  if (!point || $("pixel-hud").hidden) return;
  const hud = $("pixel-hud");
  const wrap = $("stage-wrap");
  const gap = 16;
  const w = hud.offsetWidth || 220;
  const h = hud.offsetHeight || 96;
  const x = point.x + gap + w <= wrap.clientWidth - 8
    ? point.x + gap
    : point.x - w - gap;
  const y = point.y + gap + h <= wrap.clientHeight - 8
    ? point.y + gap
    : point.y - h - gap;
  hud.style.left = clamp(x, 8, Math.max(8, wrap.clientWidth - w - 8)) + "px";
  hud.style.top = clamp(y, 8, Math.max(8, wrap.clientHeight - h - 8)) + "px";
}

function queuePixelProbe(x, y) {
  frameRequests.pixels.queue(x, y);
}

function invalidatePixelProbe(requeue = true) {
  frameRequests.pixels.invalidate(requeue);
}

function togglePixelInspector() {
  if (state.plate && !activeFrameContext()) {
    state.pixel.hudVisible = false;
    renderPixelInspector();
    showToast("Choose a site before using Pixel Inspector");
    return;
  }
  state.pixel.hudVisible = !state.pixel.hudVisible;
  renderPixelInspector();
  if (state.pixel.hudVisible && !state.pixel.cursor) {
    showToast("Pixel Inspector on — move over the slide");
  }
}

function screenHorizontalUmPerCssPixel() {
  const ps = pixelSize(); // [source Y, source X] in µm per source pixel
  const viewport = state.viewer?.viewport;
  if (!viewport || !ps || !ps.every((value) => Number.isFinite(value) && value > 0)) {
    return null;
  }
  const size = viewport.getContainerSize();
  if (!(size.x > 0 && size.y > 0)) return null;
  const cssLength = 100;
  const center = { x: size.x / 2, y: size.y / 2 };
  const a = viewerElementToImagePoint(center);
  const b = viewerElementToImagePoint({ x: center.x + cssLength, y: center.y });
  const length = Math.hypot((b.x - a.x) * ps[1], (b.y - a.y) * ps[0]);
  // Both OSD element points and CSS widths are CSS pixels. DPR must not be
  // applied here, even though the separate zoom percentage uses device pixels.
  return Number.isFinite(length) && length > 0 ? length / cssLength : null;
}

function updateScalebar(iz) {
  const ps = pixelSize();
  // The plate grid is unrotated <img> cells, not the hidden OSD viewport.
  const umPerScreenPx = state.plate && state.plate.focus === null
    ? ps && ps.every((value) => Number.isFinite(value) && value > 0) && iz > 0
      ? ps[1] / iz : null
    : screenHorizontalUmPerCssPixel();
  if (!(umPerScreenPx > 0)) {
    $("scalebar").style.display = "none";
    $("scalebar-label").textContent = "";
    return;
  }
  const target = 110 * umPerScreenPx; // aim for a ~110 px bar
  const nice = niceLength(target);
  $("scalebar").style.display = "";
  $("scalebar").style.width = nice / umPerScreenPx + "px";
  $("scalebar-label").textContent = nice >= 1000 ? nice / 1000 + " mm" : nice + " µm";
}

function niceLength(um) {
  const pows = [1, 2, 5];
  let best = 1;
  for (let e = -1; e <= 5; e++) {
    for (const p of pows) {
      const v = p * Math.pow(10, e);
      if (v <= um) best = v;
    }
  }
  return best;
}

/* ---- tools: ROI select, ruler, pin, box ------------------------------------ */

const TOOL_BTN = {
  roi: "roi-toggle",
  move: "roi-move",
  measure: "tool-measure",
  pin: "tool-pin",
  box: "tool-box",
  astromap: "astromap-draw",
};

function annLocked() {
  // a degraded overview's base level is level 1: drawing here would save
  // half-scale coordinates into the slide's level-0 sidecar
  return state.info && state.info.storage === "overview-degraded";
}

function frameOwnsRoi(context = activeFrameContext()) {
  if (!state.roi || !context) return false;
  if (!state.plate) return true;
  return window.Nd2LatestRequest.frameOwnsSite(context.frame, state.roiSite);
}

function annotationOwnerReady() {
  if (!state.annReady) return false;
  if (!state.plate) return true;
  const frame = activeFrameParams();
  return window.Nd2LatestRequest.frameOwnsSite(frame, state.annSite);
}

function scopedButton(id, disabled, reason) {
  const el = $(id);
  if (!el) return;
  if (!el.dataset.scopeTitle) el.dataset.scopeTitle = el.title || "";
  el.disabled = !!disabled;
  el.title = disabled && reason ? reason : el.dataset.scopeTitle;
}

function syncFrameScopedControls() {
  refreshAstromapPanel();
  const context = activeFrameContext();
  const hasFrame = !!context;
  const chooseSite = "Choose a site first";
  const hasRoi = frameOwnsRoi(context);

  $("roi-full").textContent = state.plate ? "Full Field" : "Whole Slide";
  $("roi-full").dataset.scopeTitle = state.plate
    ? "Mark the full field of the selected site" : "Mark the whole slide";
  $("roi-hint").textContent = !hasFrame
    ? "Choose a site in the grid to select or export a region. All-site export is not available yet."
    : state.plate
      ? "Source-aligned rectangle · Drag on the selected site, or select its full field."
      : "Source-aligned rectangle · Drag on the slide, or select the whole slide.";

  for (const id of ["roi-toggle", "roi-full"]) {
    scopedButton(id, !hasFrame, chooseSite);
  }
  for (const id of ["roi-dl-tiff", "roi-dl-png", "roi-dl-jpg", "roi-clear"]) {
    scopedButton(id, !hasRoi, hasFrame ? "Mark a region first" : chooseSite);
  }
  syncScaleBarExportControls();
  scopedButton("roi-move", !hasRoi, hasFrame ? "Mark a region first" : chooseSite);
  const nd2Unavailable = state.info && state.info.nd2Export === false;
  scopedButton(
    "roi-dl-nd2",
    !hasRoi || nd2Unavailable,
    nd2Unavailable
      ? "ND2 export needs the limnd2 package on the server"
      : hasFrame ? "Mark a region first" : chooseSite
  );

  const annotationReady = annotationOwnerReady();
  const annotationReason = hasFrame
    ? state.annReady ? "Annotations are unavailable for this site" : "Loading annotations for this site"
    : chooseSite;
  for (const id of ["tool-measure", "tool-pin", "tool-box", "ann-open"]) {
    scopedButton(id, !annotationReady || annLocked(), annLocked()
      ? "The source file is missing; annotation is locked"
      : annotationReason);
  }
  for (const id of ["ann-export", "ann-geojson"]) {
    scopedButton(id, !annotationReady, annotationReason);
  }
}

function setTool(tool) {
  const turningOff = tool === null || state.tool === tool;
  if (!turningOff && state.plate && !activeFrameParams()) {
    showToast("Choose a site before using Export or Annotations");
    return;
  }
  if (
    !turningOff && (tool === "measure" || tool === "pin" || tool === "box" || tool === "astromap") &&
    !annotationOwnerReady()
  ) {
    setAnnStatus("Choose a site and wait for its annotations to load");
    return;
  }
  if (!turningOff && tool === "move" && !frameOwnsRoi()) return;
  if (annLocked() && (tool === "measure" || tool === "pin" || tool === "box" || tool === "astromap")) {
    setAnnStatus("Annotation is locked while the source file is missing");
    return;
  }
  state.cancelToolDrag?.();
  state.tool = state.tool === tool ? null : tool;
  const on = state.tool;
  if ((on === "roi" || on === "move") && state.windows.region.isHidden())
    state.windows.region.open();
  for (const [t, id] of Object.entries(TOOL_BTN)) {
    $(id).classList.toggle("armed", on === t);
  }
  $("roi-toggle").textContent = on === "roi" ? "Drag to Mark…" : "Select Region";
  $("stage").classList.toggle("selecting", !!on && on !== "move");
  $("stage").classList.toggle("moving", on === "move");
  // Astromap owns only Shift + primary drag. Leave the viewer's tracker
  // active for ordinary panning, wheel zoom and trackpad gestures.
  state.viewer.setMouseNavEnabled(!on || on === "astromap");
  if (!on) {
    $("rubber").style.display = "none";
    state.tempLine = null;
    renderAnnotations();
  }
}

function imgPoint(pt) {
  const p = viewerElementToImagePoint(pt);
  return {
    x: clamp(p.x, 0, state.info.width),
    y: clamp(p.y, 0, state.info.height),
  };
}

function imageBoundsOfScreenRect(a, b) {
  // Establish the raw export bounds once: source AABB -> clip -> integer
  // edges. The preview reprojects this same rectangle, not the screen drag.
  const corners = [
    viewerElementToImagePoint({ x: a.x, y: a.y }),
    viewerElementToImagePoint({ x: b.x, y: a.y }),
    viewerElementToImagePoint({ x: b.x, y: b.y }),
    viewerElementToImagePoint({ x: a.x, y: b.y }),
  ];
  if (!corners.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y))) return null;
  const xs = corners.map((c) => c.x), ys = corners.map((c) => c.y);
  // Snap only floating point noise around integer source edges. In particular
  // exact quarter turns must not gain a pixel through sin/cos round-off.
  const snap = (value) => Math.abs(value - Math.round(value)) < 1e-7
    ? Math.round(value) : value;
  const x = Math.floor(snap(clamp(Math.min(...xs), 0, state.info.width)));
  const y = Math.floor(snap(clamp(Math.min(...ys), 0, state.info.height)));
  const right = Math.ceil(snap(clamp(Math.max(...xs), 0, state.info.width)));
  const bottom = Math.ceil(snap(clamp(Math.max(...ys), 0, state.info.height)));
  return { x, y, w: right - x, h: bottom - y };
}

function positionRubber(a, b) {
  const r = imageBoundsOfScreenRect(a, b);
  const rubber = $("rubber");
  let svg = rubber.firstElementChild;
  if (!svg) {
    svg = svgEl("svg", { width: "100%", height: "100%", "aria-hidden": "true" }, rubber);
    svgEl("polygon", {
      fill: "rgba(10, 132, 255, 0.12)", stroke: "var(--accent-soft)",
      "stroke-width": 1.5, "vector-effect": "non-scaling-stroke",
    }, svg);
  }
  const wrap = $("stage-wrap").getBoundingClientRect();
  const stage = $("stage").getBoundingClientRect();
  Object.assign(rubber.style, {
    left: stage.left - wrap.left + "px", top: stage.top - wrap.top + "px",
    width: stage.width + "px", height: stage.height + "px",
    border: "0", borderRadius: "0", background: "none", overflow: "hidden",
  });
  svg.firstElementChild.setAttribute(
    "points", roiOverlayPoints(r).map((p) => p.x + "," + p.y).join(" ")
  );
  return r;
}

function wireTools() {
  const stage = $("stage");
  const rubber = $("rubber");
  let start = null; // element coords
  let startImg = null;
  let dragContext = null;
  let dragPointer = null;
  let current = null;

  $("roi-toggle").onclick = () => setTool("roi");
  $("roi-move").onclick = () => {
    if (state.roi) setTool("move");
  };
  $("tool-measure").onclick = () => setTool("measure");
  $("tool-pin").onclick = () => setTool("pin");
  $("tool-box").title = "Source-aligned rectangle";
  $("tool-box").onclick = () => setTool("box");

  let moveFrom = null; // roi origin at drag start (move mode)
  let landmarkStart = null; // a click, not a drag, places an alignment point
  const drawStart = () => state.tool === "astromap"
    ? imageToViewerElementPoint(startImg) : start;
  const clearDrag = () => {
    const pointer = dragPointer;
    start = startImg = dragContext = dragPointer = current = moveFrom = null;
    rubber.style.display = "none";
    stage.classList.remove("grabbing");
    try { if (pointer !== null) stage.releasePointerCapture(pointer); } catch (_) { /* optional */ }
  };
  state.cancelToolDrag = clearDrag;
  state.viewer.addHandler("update-viewport", () => {
    if (state.tool === "astromap" && start && current)
      positionRubber(drawStart(), current);
  });
  stage.addEventListener("pointerdown", (ev) => {
    if (dragPointer !== null) return;
    const frameLinked = state.viewportRelay?.compare?.enabled && state.viewportRelay.compare.frameLinked;
    // In linked Compare, Shift-drag belongs to alignment even with Astromap
    // open or armed. Do not turn a busy alignment gesture into a new ROI.
    const astromapDrag = ev.button === 0 && ev.pointerType !== "touch" && ev.shiftKey &&
      !ev.metaKey && !ev.ctrlKey && !ev.altKey && !frameLinked;
    if (!state.tool && !state.landmark.active && astromapDrag && !state.windows.astromap.isHidden() &&
        astromapReady()) setTool("astromap");
    if (state.landmark.active && !state.tool && ev.button === 0) {
      landmarkStart = elementPoint(ev, stage);
      return; // OpenSeadragon keeps the drag, so the user can still pan
    }
    if (!state.tool || ev.button !== 0) return;
    if (state.tool === "astromap" && (!astromapDrag || !astromapReady())) return;
    if (state.tool === "move" && !state.roi) return;
    start = current = elementPoint(ev, stage);
    // A camera change during the drag must not move its source-image anchor.
    startImg = state.tool === "astromap" ? viewerElementToImagePoint(start) : imgPoint(start);
    dragContext = activeFrameContext()?.key;
    dragPointer = ev.pointerId;
    if (state.tool === "move") {
      moveFrom = { x: state.roi.x, y: state.roi.y };
      stage.classList.add("grabbing");
    }
    if (state.tool === "roi" || state.tool === "box" || state.tool === "astromap") {
      rubber.style.display = "block";
      positionRubber(start, start);
    }
    try { stage.setPointerCapture(ev.pointerId); } catch (_) { /* optional */ }
    ev.preventDefault();
    ev.stopPropagation();
  }, true);

  stage.addEventListener("pointermove", (ev) => {
    if (!state.tool || !start || ev.pointerId !== dragPointer) return;
    const cur = current = elementPoint(ev, stage);
    if (state.tool === "move" && moveFrom) {
      const p = imgPoint(cur);
      const r = state.roi;
      r.x = Math.round(clamp(moveFrom.x + (p.x - startImg.x), 0, state.info.width - r.w));
      r.y = Math.round(clamp(moveFrom.y + (p.y - startImg.y), 0, state.info.height - r.h));
      moveRoiOverlay();
    } else if (state.tool === "roi" || state.tool === "box" || state.tool === "astromap") {
      positionRubber(drawStart(), cur);
    } else if (state.tool === "measure") {
      const p = imgPoint(cur);
      state.tempLine = { x1: startImg.x, y1: startImg.y, x2: p.x, y2: p.y };
      renderAnnotations();
    }
    ev.preventDefault();
    ev.stopPropagation();
  }, true);

  stage.addEventListener("pointerup", (ev) => {
    if (landmarkStart) {
      const end = elementPoint(ev, stage);
      const moved = Math.hypot(end.x - landmarkStart.x, end.y - landmarkStart.y);
      landmarkStart = null;
      if (moved < 6 && state.landmark.active && !state.tool) placeLandmark(end);
      return;
    }
    if (!state.tool || !start || ev.pointerId !== dragPointer || ev.button !== 0) return;
    if (dragContext !== activeFrameContext()?.key) {
      setTool(null);
      return;
    }
    const end = elementPoint(ev, stage);
    const endImg = imgPoint(end);
    const rectangle = state.tool === "roi" || state.tool === "box" || state.tool === "astromap"
      ? positionRubber(drawStart(), end) : null;
    rubber.style.display = "none";
    const tool = state.tool;
    const moved = Math.hypot(end.x - start.x, end.y - start.y);

    if (tool === "move") {
      moveFrom = null;
      stage.classList.remove("grabbing");
      updateRoiPanel(); // position changed; size and readouts stay
    } else if (tool === "roi") {
      finishSelection(rectangle);
    } else if (tool === "measure") {
      state.tempLine = null;
      if (moved >= 6) {
        addAnnotation({
          type: "line",
          x1: startImg.x, y1: startImg.y, x2: endImg.x, y2: endImg.y,
          text: "",
        });
        setTool(null); // hand the mouse back for panning and zooming
      }
      renderAnnotations();
    } else if (tool === "astromap") {
      if (moved >= 6) addAstromapRoi(rectangle);
      setTool(null);
    } else if (tool === "box") {
      if (moved >= 6 && rectangle && rectangle.w > 0 && rectangle.h > 0) {
        const item = addAnnotation({ type: "box", ...rectangle, text: "" });
        setTool(null);
        openEditor(item.id);
      }
    } else if (tool === "pin") {
      if (moved < 8) {
        const item = addAnnotation({ type: "pin", x: endImg.x, y: endImg.y, text: "" });
        setTool(null);
        openEditor(item.id);
      }
    }
    clearDrag();
    ev.preventDefault();
    ev.stopPropagation();
  }, true);

  const cancelDrag = (ev) => {
    if (ev.pointerId !== dragPointer) return;
    clearDrag();
    if (state.tool === "astromap") setTool(null);
  };
  stage.addEventListener("pointercancel", cancelDrag, true);
  stage.addEventListener("lostpointercapture", cancelDrag, true);

  $("roi-clear").onclick = clearRoi;
  $("roi-full").onclick = () => applyRoi(0, 0, state.info.width, state.info.height);
  $("roi-dl-nd2").onclick = () => downloadRoi("nd2");
  $("roi-dl-tiff").onclick = () => downloadRoi("tiff");
  $("roi-dl-png").onclick = () => downloadRoi("png");
  $("roi-dl-jpg").onclick = () => downloadRoi("jpg");
  $("roi-dl-svg-scale").onclick = () => downloadRoi("svg", true);
  $("roi-dl-jpg-scale").onclick = () => downloadRoi("jpg", true);
  if (state.info.nd2Export === false) {
    const b = $("roi-dl-nd2");
    b.disabled = true;
    b.title =
      "ND2 export needs the limnd2 package on the server — " +
      "pip install --index-url https://pypi.laboratory-imaging.com/simple limnd2";
  }

  wireRoiDims();
  wireAnnotationPanel();
}

/* ---- Astromap: persistent vector regions and bounded whole-slide export --- */

function astromapReady() {
  return !!(state.info && activeFrameContext() && annotationOwnerReady() &&
    !annLocked() && !state.quitPreparing && !state.astromap.exporting && spatialImageReady());
}

function astromapItems() {
  return state.annotations.filter((a) => a.type === "box" && a.astromap === true);
}

function astromapDisplayNote(item) {
  const text = item.text || "";
  // AstroMapper's default NOTE repeats INDEX. Keep it in the annotation and
  // subsequent exports, but show only our numbered button/overlay label.
  return Number.isSafeInteger(item.astromapImportIndex) &&
    text.trim() === String(item.astromapImportIndex) ? "" : text;
}

function selectAstromapRoi(id, locate = true) {
  const item = astromapItems().find((a) => a.id === id);
  if (!item) return false;
  state.astromap.activeId = id;
  renderAnnotations();
  refreshAstromapPanel();
  if (locate) panToAstromapRoi(item);
  return true;
}

function navigateAstromapKey(ev) {
  const step = ev.key === "ArrowLeft" ? -1 : ev.key === "ArrowRight" ? 1 : 0;
  const shortcuts = window.Nd2ShortcutRouter;
  if (!step || ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.altKey || ev.shiftKey ||
      !shortcuts || shortcuts.isTypingEvent(ev)) return false;
  if (state.quitPreparing || state.landmark.active || state.viewportRelay.alignmentDrag ||
      !state.windows?.astromap || state.windows.astromap.isHidden() ||
      !state.astromap?.activeId || !activeFrameContext() ||
      !annotationOwnerReady() || !spatialImageReady()) return false;
  // Match the numbered rows, including regions excluded from export. Neither
  // their IDs nor imported labels necessarily follow the displayed order.
  const items = astromapItems();
  const index = items.findIndex((item) => item.id === state.astromap.activeId);
  if (index < 0) return false;
  ev.preventDefault();
  ev.stopImmediatePropagation(); // no plate/Compare arrow action or OSD pan
  const next = Math.max(0, Math.min(items.length - 1, index + step));
  if (next !== index) {
    const focusRow = !!ev.target.closest?.("#astromap-list");
    selectAstromapRoi(items[next].id);
    const row = state.astromap.rows.get(items[next].id);
    row?.el.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (focusRow) row?.number.focus({ preventScroll: true });
  }
  return true;
}

function wireAstromapKeys() {
  // Register before plate/Compare handlers so one key has one navigation owner.
  window.addEventListener("keydown", navigateAstromapKey, true);
}

function panToAstromapRoi(item) {
  // Retain the displayed scale even if a previous zoom is still animating.
  // This changes only the camera center; ROI size never determines zoom.
  window.Nd2ViewerGeometry.centerOnPreservingZoom(state.viewer.viewport,
    { x: item.x + item.w / 2, y: item.y + item.h / 2 }, OpenSeadragon);
}

function postAstromapState() {
  if (window.parent === window || !state.windows?.astromap) return;
  window.parent.postMessage({ nd2wsi: "astromap-state", version: VIEWPORT_PROTOCOL_VERSION,
    sid: currentSlideSid(), paneInstanceId: state.viewportRelay.commandGate.paneInstanceId,
    open: !state.windows.astromap.isHidden(), ready: !!activeFrameContext() }, location.origin);
}

function toggleAstromap(forceOpen = false) {
  if (state.quitPreparing || !state.windows?.astromap) return false;
  const panel = state.windows.astromap;
  if (forceOpen || panel.isHidden()) panel.open();
  else { panel.close(); if (state.tool === "astromap") setTool(null); }
  refreshAstromapPanel();
  return true;
}

function requestAstromap() {
  if (state.quitPreparing) return false;
  if (window.parent === window) return toggleAstromap();
  refreshSpatialContext();
  window.parent.postMessage({ nd2wsi: "astromap-shortcut", version: VIEWPORT_PROTOCOL_VERSION,
    sid: currentSlideSid(), ...spatialIdentity() }, location.origin);
  return true;
}

function addAstromapRoi(rect) {
  if (!astromapReady()) return null;
  const normalized = window.Nd2Astromap.normalizeRect(rect, state.info.width, state.info.height);
  if (!normalized) { showToast("Draw a region inside the image"); return null; }
  if (astromapItems().some((a) => window.Nd2Astromap.overlaps(normalized, a))) {
    showToast("This region overlaps another Astromap region");
    return null;
  }
  const item = addAnnotation({ type: "box", ...normalized, text: "",
    astromap: true, astromapEnabled: true });
  selectAstromapRoi(item.id, false);
  state.astromap.rows.get(item.id)?.note.focus({ preventScroll: true });
  return item;
}

function clearAstromapRois() {
  if (!astromapReady() || state.astromap.importReading) return false;
  const ids = new Set(astromapItems().map((item) => item.id));
  if (!ids.size) return false;
  if (ids.has(state.editingId)) closeEditor(false);
  if (state.tool === "astromap") state.cancelToolDrag?.();
  state.annotations = state.annotations.filter((item) => item.type !== "box" || item.astromap !== true);
  state.astromap.activeId = null;
  // One revision, render and save for the whole batch; manual annotations stay.
  annotationsChanged();
  $("astromap-status").textContent = `Cleared ${ids.size} Astromap region${ids.size === 1 ? "" : "s"}.`;
  return true;
}

function refreshAstromapPanel() {
  if (!state.astromap || !state.info || !$("astromap-list")) return;
  const items = astromapItems(), ready = astromapReady();
  if (!items.some((a) => a.id === state.astromap.activeId)) state.astromap.activeId = items[0]?.id || null;
  const list = $("astromap-list"), rows = state.astromap.rows;
  const ids = new Set(items.map((a) => a.id));
  for (const [id, row] of rows) if (!ids.has(id)) { row.el.remove(); rows.delete(id); }
  const size = window.Nd2Astromap.exportSize(state.info.width, state.info.height);
  items.forEach((a, index) => {
    let row = rows.get(a.id);
    if (!row) {
      const el = document.createElement("div"); el.className = "astromap-item";
      const include = document.createElement("input"); include.type = "checkbox";
      const number = document.createElement("button"); number.className = "astromap-number"; number.type = "button";
      const note = document.createElement("input"); note.type = "text"; note.maxLength = 4096; note.className = "astromap-note";
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "astromap-remove"; remove.textContent = "×";
      const coords = document.createElement("div"); coords.className = "astromap-coordinates";
      const find = () => state.annotations.find((item) => item.id === a.id && item.astromap === true);
      include.onchange = () => {
        const item = find(); if (!item || !astromapReady()) return;
        item.astromapEnabled = include.checked; annotationsChanged();
      };
      note.oninput = () => {
        const item = find(); if (!item || !astromapReady()) return;
        item.text = note.value; annotationsChanged();
      };
      number.onclick = () => selectAstromapRoi(a.id);
      remove.onclick = () => { if (astromapReady() && find()) removeAnnotation(a.id); };
      el.append(include, number, note, remove, coords);
      row = { el, include, number, note, remove, coords };
      rows.set(a.id, row);
    }
    row.include.checked = a.astromapEnabled !== false;
    row.include.setAttribute("aria-label", `Include region ${index + 1}`);
    row.number.textContent = String(index + 1);
    row.number.title = `Go to region ${index + 1}`;
    row.number.setAttribute("aria-pressed", String(a.id === state.astromap.activeId));
    row.note.setAttribute("aria-label", `Note for region ${index + 1}`);
    row.note.placeholder = "Note";
    if (document.activeElement !== row.note) row.note.value = astromapDisplayNote(a);
    row.remove.setAttribute("aria-label", `Delete region ${index + 1}`);
    row.include.disabled = row.note.disabled = row.remove.disabled = !ready;
    const box = window.Nd2Astromap.scaledCoordinates(a, state.info.width, state.info.height, size.width, size.height);
    row.coords.textContent = `JPG px: ${box.x1}, ${box.y1} → ${box.x2}, ${box.y2}`;
    row.el.classList.toggle("excluded", a.astromapEnabled === false);
    row.el.classList.toggle("selected", a.id === state.astromap.activeId);
    // Reuse the same nodes while typing; moving existing nodes would lose IME focus in WebKit.
    if (list.children[index] !== row.el) list.insertBefore(row.el, list.children[index] || null);
  });
  $("astromap-empty").hidden = items.length > 0;
  $("astromap-size").textContent = `${fmtInt(size.width)} × ${fmtInt(size.height)} px JPG · coordinates match this image`;
  const reason = annLocked() ? "The source file is missing; annotation is locked"
    : !activeFrameContext() ? "Choose a site before adding regions"
    : !annotationOwnerReady() ? "Wait for this site's annotations to load"
    : "Shift-drag to add a rectangle. Pan and zoom as usual.";
  $("astromap-hint").textContent = reason;
  $("astromap-draw").disabled = !ready;
  $("astromap-add-current").disabled = !ready || !frameOwnsRoi();
  const exportDisabled = !ready || !window.Nd2Astromap.selectedRois(state.annotations).length;
  $("astromap-export").disabled = $("astromap-export-pdf").disabled = exportDisabled;
  $("astromap-export").textContent = state.astromap.exporting ? "Preparing export…" : "Export JPG + TXT";
  $("astromap-export-pdf").textContent = state.astromap.exporting ? "Preparing export…" : "Export ZIP with PDF";
  refreshAstromapImport(ready);
  postAstromapState();
}

async function exportAstromap(includePdf = false) {
  if (!astromapReady()) return false;
  closeEditor(true);
  const captured = activeFrameContext();
  const rois = window.Nd2Astromap.selectedRois(state.annotations);
  if (!rois.length) { showToast("Select at least one Astromap region"); return false; }
  const payload = { generation: state.info.generation || "", rois,
    channels: [...state.channels], window: lutParam() || null, frame: captured.frame };
  const withPdf = includePdf === true;
  if (withPdf) payload.include_pdf = true;
  state.astromap.exporting = true;
  $("astromap-status").textContent = withPdf ? "Preparing JPG, coordinates and PDF…" : "Preparing JPG and coordinates…";
  refreshAstromapPanel();
  try {
    const response = await fetch("api/astromap", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Export failed (HTTP ${response.status})`);
    if (activeFrameContext()?.key !== captured.key || state.quitPreparing) {
      throw new Error("The displayed image changed. Export again from the intended image.");
    }
    const url = new URL(result.download_url, location.href);
    if (url.origin !== location.origin || url.pathname !== location.pathname + "api/astromap/download") {
      throw new Error("The export returned an invalid download location");
    }
    const a = document.createElement("a");
    a.href = url.href; a.download = result.filename || "";
    document.body.append(a); a.click(); a.remove();
    $("astromap-status").textContent = `${result.count} regions · ${fmtInt(result.width)} × ${fmtInt(result.height)} px · JPG + TXT${withPdf ? " + PDF" : ""} in ZIP`;
    showToast("Astromap export ready — choose where to save the ZIP");
    return true;
  } catch (error) {
    $("astromap-status").textContent = error.message || "Astromap export failed";
    showToast(error.message || "Astromap export failed");
    return false;
  } finally {
    state.astromap.exporting = false;
    refreshAstromapPanel();
  }
}

function refreshAstromapImport(ready = astromapReady()) {
  const pending = state.astromap.pendingImport;
  const busy = !!state.astromap.importReading;
  $("astromap-import").disabled = !ready || busy;
  $("astromap-clear-all").disabled = !ready || busy || !astromapItems().length;
  $("astromap-import-options").hidden = !pending;
  const size = [Number($("astromap-import-width").value), Number($("astromap-import-height").value)];
  const validSize = size.every((n) => Number.isSafeInteger(n) && n > 0);
  const current = pending && pending.contextKey === activeFrameContext()?.key;
  if (pending && !current)
    $("astromap-import-status").textContent = "The displayed image changed. Choose the TXT again for the intended image.";
  $("astromap-import-confirm").disabled = !ready || busy || !current || !validSize;
  $("astromap-import-jpg").disabled = !ready || busy || !current;
  $("astromap-import-width").disabled = $("astromap-import-height").disabled = busy;
}

async function readAstromapImportFile(file) {
  if (!file || !astromapReady()) return false;
  const contextKey = activeFrameContext()?.key;
  const seq = state.astromap.importSeq = (state.astromap.importSeq || 0) + 1;
  state.astromap.pendingImport = null;
  state.astromap.importReading = true;
  $("astromap-import-status").textContent = "Reading ROI coordinates…";
  refreshAstromapImport();
  try {
    if (file.size > 16 * 1024 * 1024) throw new Error("ROI TXT must be smaller than 16 MiB");
    const text = await file.text();
    if (seq !== state.astromap.importSeq) return false;
    if (contextKey !== activeFrameContext()?.key || !astromapReady())
      throw new Error("The displayed image changed. Choose the TXT again for the intended image.");
    const size = state.info.raster ? { width: state.info.width, height: state.info.height }
      : window.Nd2Astromap.exportSize(state.info.width, state.info.height);
    let parsed;
    try { parsed = window.Nd2AstromapImport.parse(text); }
    catch (error) {
      // Include an independent reference-size mismatch in a malformed-file
      // diagnostic. Valid files still reach the explicit dimension controls.
      window.Nd2AstromapImport.parse(text, size);
      throw error;
    }
    state.astromap.pendingImport = { rows: parsed.rows, contextKey, name: file.name };
    $("astromap-import-width").value = size.width;
    $("astromap-import-height").value = size.height;
    $("astromap-import-name").textContent = `${file.name} · ${parsed.count} regions`;
    $("astromap-import-status").textContent = "Check the JPG dimensions, then select Import regions.";
    return true;
  } catch (error) {
    if (seq === state.astromap.importSeq) $("astromap-import-status").textContent = error.message;
    return false;
  } finally {
    if (seq === state.astromap.importSeq) {
      state.astromap.importReading = false;
      refreshAstromapImport();
    }
  }
}

async function readAstromapImportJpg(file) {
  const pending = state.astromap.pendingImport;
  if (!file || !pending || !astromapReady() || state.astromap.importReading) return false;
  const seq = state.astromap.importSeq = (state.astromap.importSeq || 0) + 1;
  state.astromap.importReading = true;
  refreshAstromapImport();
  try {
    const size = await window.Nd2AstromapImport.readImageSize(file);
    if (seq !== state.astromap.importSeq) return false;
    if (state.astromap.pendingImport !== pending || pending.contextKey !== activeFrameContext()?.key)
      throw new Error("The displayed image changed. Choose the TXT again for the intended image.");
    $("astromap-import-width").value = size.width;
    $("astromap-import-height").value = size.height;
    $("astromap-import-status").textContent = `${file.name}: ${fmtInt(size.width)} × ${fmtInt(size.height)} px. Ready to import.`;
    return true;
  } catch (error) {
    if (seq === state.astromap.importSeq) $("astromap-import-status").textContent = error.message;
    return false;
  } finally {
    if (seq === state.astromap.importSeq) {
      state.astromap.importReading = false;
      refreshAstromapImport();
    }
  }
}

function commitAstromapImport() {
  const pending = state.astromap.pendingImport;
  if (!pending || !astromapReady() || state.astromap.importReading) return false;
  try {
    if (pending.contextKey !== activeFrameContext()?.key)
      throw new Error("The displayed image changed. Choose the TXT again for the intended image.");
    const boxes = window.Nd2AstromapImport.mapToSource(pending.rows,
      { width: Number($("astromap-import-width").value), height: Number($("astromap-import-height").value) },
      { width: state.info.width, height: state.info.height });
    if (astromapItems().length + boxes.length > 2000) throw new Error("Astromap supports up to 2000 regions");
    closeEditor(true);
    // Validate every row first, then append once, keeping imported overlaps
    // and the current frame's existing annotations unchanged.
    const imported = boxes.map((box) => ({ id: "a" + Date.now().toString(36) + annCounter++, ...box }));
    state.annotations.push(...imported);
    state.astromap.activeId = imported[0]?.id || state.astromap.activeId;
    state.astromap.pendingImport = null;
    annotationsChanged();
    $("astromap-import-status").textContent = `Imported ${boxes.length} regions from ${pending.name}.`;
    return true;
  } catch (error) {
    $("astromap-import-status").textContent = error.message;
    return false;
  }
}

function wireAstromapImport() {
  $("astromap-import").onclick = () => { if (astromapReady()) $("astromap-import-file").click(); };
  $("astromap-import-file").onchange = async () => {
    const input = $("astromap-import-file");
    await readAstromapImportFile(input.files[0]);
    input.value = "";
  };
  $("astromap-import-jpg").onclick = () => $("astromap-import-jpg-file").click();
  $("astromap-import-jpg-file").onchange = async () => {
    const input = $("astromap-import-jpg-file");
    await readAstromapImportJpg(input.files[0]);
    input.value = "";
  };
  $("astromap-import-confirm").onclick = commitAstromapImport;
  $("astromap-import-cancel").onclick = () => {
    state.astromap.importSeq = (state.astromap.importSeq || 0) + 1;
    state.astromap.pendingImport = null;
    state.astromap.importReading = false;
    $("astromap-import-status").textContent = "";
    refreshAstromapImport();
  };
  for (const id of ["astromap-import-width", "astromap-import-height"])
    $(id).oninput = () => refreshAstromapImport();
}

function wireAstromap() {
  wireAstromapImport();
  $("astromap-draw").onclick = () => { if (astromapReady()) setTool("astromap"); };
  $("astromap-add-current").onclick = () => {
    if (frameOwnsRoi()) addAstromapRoi({ ...state.roi });
  };
  $("astromap-clear-all").onclick = clearAstromapRois;
  $("astromap-export").onclick = () => exportAstromap();
  $("astromap-export-pdf").onclick = () => exportAstromap(true);
  const observer = new MutationObserver(() => {
    if (state.windows.astromap.isHidden() && state.tool === "astromap") setTool(null);
    postAstromapState();
  });
  observer.observe($("win-astromap"), { attributes: true, attributeFilter: ["class"] });
  window.addEventListener("pagehide", () => observer.disconnect(), { once: true });
  refreshAstromapPanel();
}

/* ---- annotations: model, sidecar persistence, SVG rendering ---------------- */

let annCounter = 0;

function addAnnotation(props) {
  const item = { id: "a" + Date.now().toString(36) + annCounter++, ...props };
  state.annotations.push(item);
  annotationsChanged();
  return item;
}

function removeAnnotation(id) {
  const previous = astromapItems().findIndex((a) => a.id === id);
  state.annotations = state.annotations.filter((a) => a.id !== id);
  if (state.astromap.activeId === id) {
    const remaining = astromapItems();
    state.astromap.activeId = remaining[Math.min(previous, remaining.length - 1)]?.id || null;
  }
  if (state.editingId === id) closeEditor();
  annotationsChanged();
}

function annotationsChanged() {
  if (state.quitPreparing) return;
  annotationStore.markDirty();
  renderAnnotations();
  rebuildAnnList();
  refreshAstromapPanel();
  updateAnnotationRecoveryNotice();
  scheduleAnnSave();
}

function scheduleAnnSave() {
  annotationStore.scheduleSave();
}

// The functions below keep their historical names and delegate to the store
// created next to the plate controller; see annotation-store-v1.js.

function normalizeAnnotationSite(site) {
  return annotationStore.normalizeSite(site);
}

function annotationsUrl(site) {
  return annotationStore.urlFor(site);
}

function annotationSaveEntry(site) {
  return annotationStore.saveEntry(site);
}

function annotationRecoveryDrafts() {
  return annotationStore.recoveryDrafts();
}

function annotationDraftAccepted(entry) {
  return annotationStore.draftAccepted(entry);
}

function acceptAnnotationRecoveryDrafts() {
  if (state.quitPreparing) return false;
  if (!$('ann-editor').hidden) closeEditor(true);
  return annotationStore.acceptRecoveryDrafts();
}

function updateAnnotationRecoveryNotice() {
  const notice = $('ann-recovery');
  if (!notice) return;
  const failed = annotationStore.failedSaves();
  notice.hidden = !failed.some((entry) => entry.conflict);
  if (notice.hidden) return;
  const entries = annotationRecoveryDrafts();
  const accepted = !!entries && entries.every(annotationDraftAccepted);
  $('ann-recovery-message').textContent = accepted
    ? 'Recovery drafts will be kept when you close. These edits have not been applied to the source. Close the tab or window again when ready.'
    : entries
    ? 'Another window changed the annotations. Your edits are preserved in recovery drafts and have not been applied to the source.'
    : 'Another window changed the annotations. Some edits are not yet preserved in a recovery draft. Keep this window open and retry saving or export your annotations.';
  const paths = failed.filter((entry) => entry.draftPath).map((entry) => entry.draftPath);
  $('ann-recovery-paths').textContent = paths.join('\n');
  const button = $('ann-recovery-keep');
  button.disabled = !entries || accepted || state.quitPreparing;
  button.textContent = accepted ? 'Recovery drafts kept for closing' : 'Keep recovery drafts for closing';
  button.onclick = acceptAnnotationRecoveryDrafts;
}

function queueAnnotationSave(entry) {
  return annotationStore.queueSave(entry);
}

function saveAnnotations(site) {
  return annotationStore.save(site);
}

async function flushAnnotationsForUpdate() {
  if (!$('ann-editor').hidden) closeEditor(true);
  state.quitPreparing = true;
  await annotationStore.flushForUpdate();
}

async function acknowledgeUpdatePreparation(requestId) {
  state.quitRequestId = requestId;
  let reply;
  try {
    await flushAnnotationsForUpdate();
    await state.lutPersistence?.flush();
    reply = { ok: true };
  } catch (error) {
    reply = { ok: false, error: String(error.message || error).slice(0, 240) };
  }
  // Cancellation or a later preparation may overtake an in-flight save.
  // The save still owns its captured annotation payload, but its reply must
  // never finish a different installation attempt.
  if (!state.quitPreparing || state.quitRequestId !== requestId) return;
  window.parent.postMessage({
    nd2wsi: "quit-ready",
    version: VIEWPORT_PROTOCOL_VERSION,
    requestId,
    sid: currentSlideSid(),
    ...reply,
  }, location.origin);
}

function cancelUpdatePreparation(requestId) {
  if (!requestId || state.quitRequestId !== requestId) return false;
  state.quitRequestId = null;
  state.quitPreparing = false;
  updateAnnotationRecoveryNotice();
  // Keep in-flight saves and failed snapshots intact. If saving failed while
  // preparation had cancelled the debounce, ordinary editing can retry it.
  if (state.annDirty && !annotationRecoveryDrafts()?.every(annotationDraftAccepted)) scheduleAnnSave();
  return true;
}

function loadAnnotations(site) {
  return annotationStore.load(site);
}

function setAnnStatus(msg) {
  $("ann-status").textContent = msg || "";
}

/* ---- auto-save status: icon, tooltip, periodic retry and pagehide flush ----
   Both persistence controllers already auto-save (annotations 800 ms, LUTs
   250 ms after an edit). They report each transition to one tracker
   (save-status-v1.js) that drives the check icon beside the scale bar. */

const AUTO_SAVE_RETRY_MS = 60000; // the user allowed a once-a-minute safety retry
const SAVE_STATUS_HOVER_MS = 600;
// The icon's accessible name follows the tracker state only; the per-second
// "N s ago" count lives in the tooltip, so assistive technology is not told
// about every tick for a minute after each save.
const SAVE_STATUS_LABELS = {
  saved: "Auto-save: saved",
  saving: "Auto-save: saving",
  dirty: "Auto-save: unsaved changes",
  failed: "Auto-save: not saved",
  conflict: "Auto-save: conflict",
};

function wireAutoSave() {
  const tracker = window.Nd2SaveStatus.createTracker({ onRender: renderSaveStatus });
  state.saveStatus = tracker;
  const tooltip = wireSaveStatusTooltip(tracker);
  tracker.start();
  tracker.scheduleRetry(AUTO_SAVE_RETRY_MS);
  state.autoSaveRetry = setInterval(() => {
    tracker.scheduleRetry(AUTO_SAVE_RETRY_MS);
    retryAutoSave();
  }, AUTO_SAVE_RETRY_MS);
  // One listener: flush first, then stop the retry and the ticker. The
  // controllers' own dispose listeners were registered later and run after.
  window.addEventListener("pagehide", () => {
    flushAutoSaveOnPagehide();
    clearInterval(state.autoSaveRetry);
    state.autoSaveRetry = null;
    tooltip.dispose();
    tracker.dispose();
  }, { once: true });
}

// Once a minute: re-queue transient annotation failures, save a dirty snapshot
// whose debounce is idle and flush unsaved LUT edits. A 409 conflict is left
// to the recovery notice; nothing runs while the native shell is quitting.
function retryAutoSave() {
  if (state.quitPreparing) return Promise.resolve([]);
  const retries = [annotationStore.retryFailed()];
  const lut = state.lutPersistence;
  if (lut && lut.isDirty()) retries.push(lut.flush().catch(() => {}));
  return Promise.all(retries);
}

// Browser mode (nd2wsi view / reload) has no save-before-close handshake, so a
// tab close or reload hands the pending snapshot to the browser with
// keepalive; the store skips a snapshot its 409 recovery draft already holds.
// The native path (flushAnnotationsForUpdate + lutPersistence.flush) stays
// the authority: while the shell is preparing to quit, nothing happens here.
function flushAutoSaveOnPagehide() {
  if (state.quitPreparing) return;
  annotationStore.flushOnPagehide({ keepalive: true });
  const lut = state.lutPersistence;
  if (lut && lut.isDirty()) lut.flush({ keepalive: true }).catch(() => {});
}

function saveStatusTooltipText(status) {
  return status.details ? status.text + "\n" + status.details : status.text;
}

// The tracker calls this when its state or text changes (every second while a
// "N s ago" count is showing). Only the tooltip follows the text; the button's
// attributes are written when the state changes. The #ann-status hint text is
// not touched.
function renderSaveStatus(status) {
  const button = $("save-status");
  if (!button) return;
  const dataState = status.visible ? status.state : null;
  if (button.getAttribute("data-state") !== dataState) {
    button.hidden = !status.visible;
    if (dataState) button.setAttribute("data-state", dataState);
    else button.removeAttribute("data-state");
    button.setAttribute("aria-label", dataState ? SAVE_STATUS_LABELS[dataState] : "Auto-save");
  }
  button.saveStatusTooltip?.render(status);
}

// A sibling of the LUT toolbar tooltips: shown after a short hover or on
// keyboard focus, hidden on leave, blur or Escape, placed above the icon and
// kept inside the window. Its text comes from the tracker on every render.
function wireSaveStatusTooltip(tracker) {
  const button = $("save-status");
  const tooltip = $("save-status-tooltip");
  let hovered = false, hoverReady = false, focused = false, dismissed = false, disposed = false;
  let hoverTimer = null;
  const cancelHover = () => {
    clearTimeout(hoverTimer);
    hoverTimer = null;
  };
  const render = (status = tracker.current()) => {
    tooltip.hidden = disposed || dismissed || !status.visible || !(hoverReady || focused);
    if (tooltip.hidden) return;
    tooltip.textContent = saveStatusTooltipText(status);
    const anchor = button.getBoundingClientRect();
    const tip = tooltip.getBoundingClientRect();
    const left = Math.max(8, Math.min(anchor.left + (anchor.width - tip.width) / 2,
      window.innerWidth - tip.width - 8));
    const top = anchor.top - tip.height - 8;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(8, Math.min(top >= 8 ? top : anchor.bottom + 8,
      window.innerHeight - tip.height - 8))}px`;
  };
  const handlers = {
    pointerenter() {
      if (disposed) return;
      cancelHover();
      hovered = true;
      dismissed = false;
      hoverReady = focused;
      if (!focused) hoverTimer = setTimeout(() => {
        hoverTimer = null;
        hoverReady = hovered;
        render();
      }, SAVE_STATUS_HOVER_MS);
      render();
    },
    pointerleave() { cancelHover(); hovered = hoverReady = false; render(); },
    focus() {
      if (disposed) return;
      cancelHover();
      focused = true;
      dismissed = false;
      render();
    },
    blur() { focused = false; render(); },
  };
  for (const [event, handler] of Object.entries(handlers)) button.addEventListener(event, handler);
  const dismiss = (event) => {
    if (event.key !== "Escape") return;
    cancelHover();
    hoverReady = false;
    dismissed = true;
    render();
  };
  const reposition = () => render();
  document.addEventListener("keydown", dismiss, true);
  window.addEventListener("resize", reposition);
  window.addEventListener("scroll", reposition, true);
  const controller = {
    render,
    dispose() {
      disposed = true;
      cancelHover();
      render();
      for (const [event, handler] of Object.entries(handlers)) button.removeEventListener(event, handler);
      document.removeEventListener("keydown", dismiss, true);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    },
  };
  button.saveStatusTooltip = controller;
  return controller;
}

function pixelSize() {
  // (py, px) in um, or null for an uncalibrated slide — every physical
  // readout must go through here and cope with null
  return state.info && state.info.pixelSizeUm ? state.info.pixelSizeUm : null;
}

function basename(p) {
  return p ? String(p).split("/").pop() : "";
}

function lineLengthUm(a) {
  const ps = pixelSize();
  const dpx = Math.hypot(a.x2 - a.x1, a.y2 - a.y1);
  return ps ? Math.hypot((a.x2 - a.x1) * ps[1], (a.y2 - a.y1) * ps[0]) : null;
}

function lineLengthLabel(a) {
  const um = lineLengthUm(a);
  if (um !== null) return fmtUm(um);
  return fmtInt(Math.hypot(a.x2 - a.x1, a.y2 - a.y1)) + " px";
}

function toEl(x, y) {
  return imageToViewerElementPoint({ x, y });
}

const SVG_NS = "http://www.w3.org/2000/svg";
const ACCENT = "#0a84ff";
// Apple system palette; the list dot cycles an annotation through these
const ANN_COLORS = ["#0a84ff", "#ffd60a", "#30d158", "#ff453a", "#ff9f0a", "#bf5af2"];

function annColor(a) {
  return a.color || ACCENT;
}
function cycleAnnColor(a) {
  const i = ANN_COLORS.indexOf(annColor(a));
  a.color = ANN_COLORS[(i + 1) % ANN_COLORS.length];
  annotationsChanged();
}
function hexAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

function svgEl(tag, attrs, parent) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.append(el);
  return el;
}

function chip(parent, x, y, text, id) {
  // An annotation's own label opens it through wireAnnotationPointer(); the
  // live ruler's label has no id and stays inert.
  const g = svgEl("g", id ? { class: "hit", "data-ann-id": id } : { class: "" }, parent);
  const t = svgEl("text", {
    x: x + 7, y: y + 12.5,
    fill: "rgba(255,255,255,0.9)",
    "font-size": "11",
    "font-family": "-apple-system, BlinkMacSystemFont, sans-serif",
  }, g);
  t.textContent = text;
  const w = Math.max(26, text.length * 6.2 + 14);
  g.insertBefore(
    svgEl("rect", {
      x, y, width: w, height: 18, rx: 9,
      fill: "rgba(28,28,30,0.85)", stroke: "rgba(255,255,255,0.25)",
      "stroke-width": "0.5",
    }),
    t
  );
  return g;
}

/* ---- annotation layer: one pointer listener and translate-only pans --------
   renderAnnotations() draws every mark into one root group of #ann-layer and
   remembers what it drew from. OpenSeadragon raises update-viewport on every
   animation frame. When only the camera centre has moved since that full
   render, the whole picture (screen-fixed labels, end ticks, pins and stroke
   widths included) moves by one screen-space offset under any rotation or
   flip, so refreshAnnotationsForViewport() translates the root group instead
   of rebuilding thousands of nodes. */

let annotationFrame = null; // the last full render: root group, signatures, mapping probes

const ANNOTATION_PROBE_SPAN = 1024; // image px between the mapping probes

// The camera inputs other than its centre.
function annotationViewSignature() {
  const viewport = state.viewer.viewport, size = viewport.getContainerSize();
  return [
    state.viewer, viewport, viewport.getZoom(true), viewport.getRotation(true),
    viewport.getFlip(), size.x, size.y,
  ];
}

// Everything else renderAnnotations() draws from. Annotation edits go through
// annotationsChanged(), which bumps annRevision; the identities catch replaced
// arrays and each new live-ruler position.
function annotationContentSignature() {
  const landmark = state.landmark;
  return [
    state.annotations, state.annotations.length, state.annRevision, state.annContext,
    state.annSite, state.astromap?.activeId, state.editingId, state.tempLine,
    landmark.points, landmark.points.length, landmark.pointRevision,
    state.plate?.focus, state.info, pixelSize(),
  ];
}

// Where image (0, 0) lands, and the element-space image axes. A pure pan moves
// the origin and leaves the axes unchanged up to float rounding.
function annotationMapping() {
  const span = ANNOTATION_PROBE_SPAN;
  const origin = toEl(0, 0), ex = toEl(span, 0), ey = toEl(0, span);
  return {
    origin: { x: origin.x, y: origin.y },
    axes: [ex.x - origin.x, ex.y - origin.y, ey.x - origin.x, ey.y - origin.y],
  };
}

function rememberAnnotationFrame(root) {
  annotationFrame = {
    root, view: annotationViewSignature(), content: annotationContentSignature(),
    ...annotationMapping(), transform: "",
  };
}

function sameAnnotationSignature(a, b) {
  return a.length === b.length && a.every((value, i) => value === b[i]);
}

// For the update-viewport handler only; every other caller changed what is
// drawn and calls renderAnnotations() for a full render.
function refreshAnnotationsForViewport() {
  const frame = annotationFrame;
  if (!frame || !state.viewer || frame.root.parentNode !== $("ann-layer") ||
      !sameAnnotationSignature(frame.view, annotationViewSignature()) ||
      !sameAnnotationSignature(frame.content, annotationContentSignature())) {
    renderAnnotations();
    return;
  }
  const { origin, axes } = annotationMapping();
  const dx = origin.x - frame.origin.x, dy = origin.y - frame.origin.y;
  const scale = Math.max(Math.hypot(frame.axes[0], frame.axes[1]), Math.hypot(frame.axes[2], frame.axes[3]));
  // Rounding-level differences only; anything larger is not a pure pan.
  if (!Number.isFinite(dx) || !Number.isFinite(dy) ||
      axes.some((value, i) => !(Math.abs(value - frame.axes[i]) <= scale * 1e-9))) {
    renderAnnotations();
    return;
  }
  const transform = `translate(${dx} ${dy})`;
  if (transform === frame.transform) return;
  frame.root.setAttribute("transform", transform);
  frame.transform = transform;
}

// One pointerdown listener serves every annotation mark. Box, pin and GeoJSON
// shapes and their labels carry data-ann-id; the layer itself takes no pointer
// events (style.css), so presses on empty space, landmarks, measurement lines
// and the live ruler fall through to OpenSeadragon.
function wireAnnotationPointer() {
  const layer = $("ann-layer");
  layer.addEventListener("pointerdown", (ev) => {
    const hit = ev.target?.closest?.("[data-ann-id]");
    if (!hit || !layer.contains(hit)) return;
    ev.stopPropagation();
    const id = hit.getAttribute("data-ann-id");
    const a = state.annotations.find((item) => String(item.id) === id);
    if (!a) return;
    if (a.type === "box" && a.astromap === true) selectAstromapRoi(a.id, false);
    openEditor(a.id);
  });
}

function renderAnnotations() {
  const layer = $("ann-layer");
  if (!state.viewer) return;
  layer.replaceChildren();
  // A fresh root carries no pan translation (refreshAnnotationsForViewport).
  const root = svgEl("g", { "data-ann-root": "" }, layer);
  if (state.astromap && !state.annotations.some((a) => a.type === "box" && a.astromap === true && a.id === state.astromap.activeId))
    state.astromap.activeId = state.annotations.find((a) => a.type === "box" && a.astromap === true)?.id || null;

  const drawLine = (a, isTemp) => {
    const col = annColor(a);
    const p1 = toEl(a.x1, a.y1);
    const p2 = toEl(a.x2, a.y2);
    const g = svgEl("g", {}, root);
    svgEl("line", {
      x1: p1.x, y1: p1.y, x2: p2.x, y2: p2.y,
      stroke: col, "stroke-width": 1.5,
    }, g);
    // perpendicular end ticks
    const dx = p2.x - p1.x, dy = p2.y - p1.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = (-dy / len) * 5, ny = (dx / len) * 5;
    for (const p of [p1, p2]) {
      svgEl("line", {
        x1: p.x - nx, y1: p.y - ny, x2: p.x + nx, y2: p.y + ny,
        stroke: col, "stroke-width": 1.5,
      }, g);
    }
    const label = lineLengthLabel(a) + (a.text ? " · " + a.text : "");
    const mx = (p1.x + p2.x) / 2 + (-dy / len) * 14 - 20;
    const my = (p1.y + p2.y) / 2 + (dx / len) * 14 - 9;
    chip(root, mx, my, label, isTemp ? null : a.id);
  };

  let astromapNumber = 0;
  for (const a of state.annotations) {
    if (a.astromap === true) astromapNumber += 1;
    if (a.astromap === true && a.astromapEnabled === false) continue;
    if (a.type === "line") drawLine(a, false);
    else if (a.type === "box") {
      const corners = [
        toEl(a.x, a.y), toEl(a.x + a.w, a.y), toEl(a.x + a.w, a.y + a.h), toEl(a.x, a.y + a.h),
      ];
      const left = Math.min(...corners.map((c) => c.x));
      const top = Math.min(...corners.map((c) => c.y));
      const g = svgEl("g", { class: "hit", "data-ann-id": a.id }, root);
      const active = a.astromap === true && a.id === state.astromap.activeId;
      svgEl("polygon", {
        points: corners.map((c) => c.x + "," + c.y).join(" "),
        fill: a.astromap === true ? active ? "rgba(255,217,102,.35)" : "rgba(160,166,174,.15)" : hexAlpha(annColor(a), 0.08),
        stroke: a.astromap === true ? active ? "#ffd966" : "#a0a6ae" : annColor(a), "stroke-width": 1.5,
        "stroke-linejoin": "round",
      }, g);
      const note = a.astromap === true ? astromapDisplayNote(a) : a.text;
      chip(root, left, top - 22, a.astromap === true
        ? `${astromapNumber}${note ? " · " + note : ""}` : a.text || "Box", a.id);
    } else if (a.type === "geojson") {
      renderImportedGeoJSON(a, root);
    } else if (a.type === "pin") {
      const p = toEl(a.x, a.y);
      const g = svgEl("g", { class: "hit", "data-ann-id": a.id }, root);
      svgEl("circle", { cx: p.x, cy: p.y, r: 6, fill: annColor(a), stroke: "#fff", "stroke-width": 1.5 }, g);
      svgEl("circle", { cx: p.x, cy: p.y, r: 1.8, fill: "#fff" }, g);
      if (a.text) chip(root, p.x + 10, p.y - 9, a.text, a.id);
    }
  }
  if (state.tempLine) drawLine({ ...state.tempLine, text: "" }, true);
  renderLandmarks(root);
  rememberAnnotationFrame(root);
}

function renderImportedGeoJSON(item, layer) {
  const api = window.Nd2AnnotationGeoJSON;
  try {
    const parts = api.displayParts(item.geojson.geometry, toEl);
    const group = svgEl("g", { class: "hit", "data-ann-id": item.id }, layer);
    for (const path of parts.paths) svgEl("path", {
      d: path.d, fill: path.closed ? hexAlpha(annColor(item), .08) : "none",
      "fill-rule": "evenodd", stroke: annColor(item), "stroke-width": 1.5,
      "stroke-linejoin": "round",
    }, group);
    for (const point of parts.points) svgEl("circle", {
      cx: point.x, cy: point.y, r: 6, fill: annColor(item), stroke: "#fff", "stroke-width": 1.5,
    }, group);
    const rect = api.bounds(item.geojson.geometry), anchor = toEl(rect.x, rect.y);
    chip(layer, anchor.x, anchor.y - 22, api.label(item), item.id);
  } catch (_) { /* Invalid legacy sidecar geometry must not break other annotations. */ }
}

/* ---- alignment landmarks --------------------------------------------------
   The shell asks each compared pane for numbered points on the same
   structures. The pane only places and shows them; the shell fits the
   transform. */

const LANDMARK_COLOR = "#ff9f0a";

function renderLandmarks(layer) {
  const points = state.landmark.points;
  if (!points.length) return;
  const group = svgEl("g", { class: "landmarks" }, layer);
  points.forEach((pt, i) => {
    const p = toEl(pt.x, pt.y);
    const g = svgEl("g", {}, group);
    svgEl("circle", {
      cx: p.x, cy: p.y, r: 9.5, fill: LANDMARK_COLOR, stroke: "#fff", "stroke-width": 1.5,
    }, g);
    svgEl("line", { x1: p.x - 14, y1: p.y, x2: p.x + 14, y2: p.y, stroke: "#fff", "stroke-width": 1, opacity: 0.7 }, g);
    svgEl("line", { x1: p.x, y1: p.y - 14, x2: p.x, y2: p.y + 14, stroke: "#fff", "stroke-width": 1, opacity: 0.7 }, g);
    const t = svgEl("text", {
      x: p.x, y: p.y + 3.6, fill: "#1c1c1e", "text-anchor": "middle",
      "font-size": "10", "font-weight": "700",
      "font-family": "-apple-system, BlinkMacSystemFont, sans-serif",
    }, g);
    t.textContent = String(i + 1);
  });
}

function landmarkHint() {
  let el = $("landmark-hint");
  if (!el) {
    el = document.createElement("div");
    el.id = "landmark-hint";
    el.hidden = true;
    $("stage-wrap").append(el);
  }
  return el;
}

function updateLandmarkHint() {
  const el = landmarkHint();
  const lm = state.landmark;
  if (!lm.active) {
    el.hidden = true;
    return;
  }
  const n = lm.points.length;
  el.textContent = n < lm.needed
    ? `Align · click point ${n + 1} of ${lm.needed} on a structure you can find on every slide · ⌫ undo · esc cancel`
    : `Align · ${lm.needed} of ${lm.needed} placed · click near a marker to move it · ⌫ undo · ⏎ done`;
  el.hidden = false;
}

function postLandmarkPoints() {
  if (window.parent === window || !spatialPaneReady() || !state.landmark.active) return;
  window.parent.postMessage({
    nd2wsi: "landmark-points",
    version: VIEWPORT_PROTOCOL_VERSION,
    sid: currentSlideSid(),
    ...landmarkEnvelope(),
    points: state.landmark.points.map((p) => ({ ...p })),
  }, location.origin);
}

function setLandmarkMode(message) {
  const lm = state.landmark;
  const viewer = state.viewer;
  const active = Boolean(message.active);
  if (active && (!spatialPaneReady() || !state.viewportRelay.commandGate.current(message) || !message.editId ||
      !Number.isSafeInteger(message.editRevision))) return;
  if (active && message.editId === lm.editId && message.editRevision < lm.editRevision) return;
  lm.editId = active ? message.editId : null;
  lm.editRevision = active ? message.editRevision : -1;
  if (Array.isArray(message.points)) {
    lm.points = message.points
      .map((p) => ({ ...p, id: p.id || crypto.randomUUID(), x: Number(p.x), y: Number(p.y) }))
      .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))
      .slice(0, lm.needed);
  }
  if (message.clear) lm.points = [];
  if (Number.isFinite(Number(message.needed)) && Number(message.needed) > 0) {
    lm.needed = Math.min(8, Math.max(2, Number(message.needed)));
  }
  if (active && !lm.active) {
    if (state.tool) setTool(state.tool);
    if (viewer?.gestureSettingsMouse) {
      lm.clickToZoom = viewer.gestureSettingsMouse.clickToZoom;
      viewer.gestureSettingsMouse.clickToZoom = false;
    }
    $("stage").classList.add("placing");
  } else if (!active && lm.active) {
    if (viewer?.gestureSettingsMouse && lm.clickToZoom !== null) {
      viewer.gestureSettingsMouse.clickToZoom = lm.clickToZoom;
    }
    lm.clickToZoom = null;
    $("stage").classList.remove("placing");
  }
  lm.active = active;
  updateLandmarkHint();
  renderAnnotations();
}

function placeLandmark(screenPoint) {
  const lm = state.landmark;
  if (!lm.active || !spatialPaneReady()) return;
  const img = imgPoint(screenPoint);
  const frame = activeFrameContext()?.frame;
  const acquired = { t: frame?.t ?? null, z: frame?.z ?? null };
  if (lm.points.length < lm.needed) {
    lm.points.push({ id: crypto.randomUUID(), x: img.x, y: img.y, acquired });
  } else {
    let nearest = -1, best = 24;
    lm.points.forEach((pt, i) => {
      const p = toEl(pt.x, pt.y);
      const d = Math.hypot(p.x - screenPoint.x, p.y - screenPoint.y);
      if (d < best) { best = d; nearest = i; }
    });
    if (nearest < 0) {
      showToast(`${lm.needed} points placed · ⌫ removes the last, or click near a marker to move it`);
      return;
    }
    lm.points[nearest] = { ...lm.points[nearest], x: img.x, y: img.y, acquired };
  }
  lm.pointRevision += 1;
  renderAnnotations();
  updateLandmarkHint();
  postLandmarkPoints();
}

function undoLandmark() {
  const lm = state.landmark;
  if (!lm.active || !spatialPaneReady() || !lm.points.length) return;
  lm.points.pop();
  lm.pointRevision += 1;
  renderAnnotations();
  updateLandmarkHint();
  postLandmarkPoints();
}

/* ---- annotation editor + list ---------------------------------------------- */

function openEditor(id) {
  const a = state.annotations.find((x) => x.id === id);
  if (!a) return;
  state.editingId = id;
  const ed = $("ann-editor");
  ed.hidden = false;
  $("ann-text").value = a.text || "";
  const p =
    a.type === "pin" ? toEl(a.x, a.y)
    : a.type === "box" ? toEl(a.x + a.w, a.y)
    : a.type === "geojson" ? (() => {
      const bounds = window.Nd2AnnotationGeoJSON.bounds(a.geojson.geometry);
      return toEl(bounds.x + bounds.w, bounds.y);
    })()
    : toEl((a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2);
  const wrap = $("stage-wrap");
  ed.style.left = clamp(p.x + 14, 8, wrap.clientWidth - 246) + "px";
  ed.style.top = clamp(p.y - 10, 8, wrap.clientHeight - 120) + "px";
  $("ann-text").focus();
}

function closeEditor(save = true) {
  if ($("ann-editor").hidden) return;
  if (save && state.editingId) {
    const a = state.annotations.find((x) => x.id === state.editingId);
    if (a) {
      a.text = $("ann-text").value.trim();
      annotationsChanged();
    }
  }
  state.editingId = null;
  $("ann-editor").hidden = true;
}

function wireAnnotationPanel() {
  $("ann-done").onclick = () => closeEditor(true);
  $("ann-delete").onclick = () => {
    const id = state.editingId;
    closeEditor(false);
    if (id) removeAnnotation(id);
  };
  $("ann-text").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); closeEditor(true); }
    if (ev.key === "Escape") closeEditor(false);
    ev.stopPropagation();
  });

  $("ann-export").onclick = () => {
    if (!annotationOwnerReady()) {
      showToast("Choose a site before exporting annotations");
      return;
    }
    downloadJsonFile(
      annotationDocument(),
      state.info.name.replace(/\.(nd2|svs)$/i, "") + ".annotations.json",
      "application/json"
    );
  };
  $("ann-geojson").onclick = exportAnnotationsGeoJSON;
  $("ann-open").onclick = () => {
    if (annLocked() || state.quitPreparing) showToast("Annotations are currently locked or unavailable");
    else if (!annotationOwnerReady()) showToast("Choose a site and wait for its annotations before opening a file");
    else $("ann-file").click();
  };
  $("ann-file").addEventListener("change", () => openAnnotationFile($("ann-file").files[0]));
}

async function openAnnotationFile(file) {
  if (!file) return false;
  const context = state.annContext, site = state.annSite, revision = state.annRevision;
  const request = state.annImportSeq = (state.annImportSeq || 0) + 1;
  try {
    if (!annotationOwnerReady() || annLocked() || state.quitPreparing) {
      throw new Error("annotations are unavailable or locked for this image");
    }
    if (file.size > window.Nd2AnnotationGeoJSON.MAX_BYTES) throw new Error("annotation files must be smaller than 8 MB");
    const text = await file.text();
    if (request !== state.annImportSeq) return false;
    if (context !== state.annContext || site !== state.annSite || !annotationOwnerReady() ||
        annLocked() || state.quitPreparing) throw new Error("the image or annotation state changed while the file was opening");
    const { items, verified, append = false } = annotationItemsForThisSlide(JSON.parse(text), /\.geojson$/i.test(file.name));
    if (!append && revision !== state.annRevision) throw new Error("annotations changed while the file was opening");
    const usedIds = new Set(state.annotations.map(item => item.id));
    const imported = append ? items.map(item => {
      let id;
      do { id = "a" + Date.now().toString(36) + annCounter++; } while (usedIds.has(id));
      usedIds.add(id);
      return { ...item, id };
    }) : items;
    const combined = append ? state.annotations.concat(imported) : imported;
    if (new Blob([JSON.stringify({ items: combined })]).size > window.Nd2AnnotationGeoJSON.MAX_BYTES) {
      throw new Error("the combined annotations exceed the 8 MB import limit");
    }
    if (!append) closeEditor(false);
    state.annotations = combined;
    annotationsChanged();
    setAnnStatus((append ? "Imported " : "Opened ") + file.name + " (" + imported.length + ")" +
      (append ? " · added to existing annotations" : "") +
      (verified ? "" : " · source not verified"));
    return true;
  } catch (error) {
    showToast("could not open annotations: " + error.message);
    return false;
  } finally { if (request === state.annImportSeq) $("ann-file").value = ""; }
}

function downloadJsonFile(data, filename, mime) {
  const context = state.info?.window;
  if (context?.role === "agent") filename = `agent-${context.id.slice(0, 12)}_${filename}`;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: mime });
  const a = document.createElement("a");
  const url = URL.createObjectURL(blob);
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function annotationFeature(item) {
  if (!item || typeof item !== "object") return null;
  if (item.geojson) return window.Nd2AnnotationGeoJSON.featureForAnnotation(item);
  const finite = (...values) => values.every((value) =>
    value !== null && value !== "" && value !== undefined && Number.isFinite(Number(value))
  );
  let geometry = null;
  if (item.type === "pin" && finite(item.x, item.y)) {
    geometry = { type: "Point", coordinates: [Number(item.x), Number(item.y)] };
  } else if (item.type === "line" && finite(item.x1, item.y1, item.x2, item.y2)) {
    geometry = {
      type: "LineString",
      coordinates: [
        [Number(item.x1), Number(item.y1)],
        [Number(item.x2), Number(item.y2)],
      ],
    };
  } else if (item.type === "box" && finite(item.x, item.y, item.w, item.h)) {
    const x1 = Number(item.x);
    const y1 = Number(item.y);
    const x2 = x1 + Number(item.w);
    const y2 = y1 + Number(item.h);
    geometry = {
      type: "Polygon",
      coordinates: [[
        [x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1],
      ]],
    };
  }
  if (!geometry) return null;
  return {
    type: "Feature",
    geometry,
    properties: {
      objectType: "annotation",
      name: item.text === null || item.text === undefined ? "" : String(item.text),
      ...(/^#[0-9a-f]{6}$/i.test(item.color || "")
        ? { color: [1, 3, 5].map(start => parseInt(item.color.slice(start, start + 2), 16)) } : {}),
    },
  };
}

// Pure serializer: level-0 viewer coordinates are already QuPath image pixels.
function annotationsToGeoJSON(items) {
  return {
    type: "FeatureCollection",
    features: (Array.isArray(items) ? items : [])
      .map(annotationFeature)
      .filter((feature) => feature !== null),
  };
}

function safeFilenamePart(value, fallback) {
  const safe = String(value || "")
    .replace(/[\\/:*?"<>|\x00-\x1f]+/g, "_")
    .replace(/[. ]+$/g, "")
    .trim();
  return safe || fallback;
}

function selectionFilenameTag(selection) {
  const source = selection || {};
  const z = source.z_resolved !== undefined
    ? source.z_resolved
    : source.z === undefined ? "mid" : source.z;
  return [
    "t" + safeFilenamePart(source.t === undefined ? 0 : source.t, "0"),
    "p" + safeFilenamePart(source.p === undefined ? 0 : source.p, "0"),
    "z" + safeFilenamePart(z, "mid"),
  ].join("-");
}

function geojsonFilename(info) {
  const name = basename(info && info.name);
  const stem = name
    .replace(/\.ome\.zarr$/i, "")
    .replace(/\.(nd2|svs)$/i, "");
  const selection = info === state.info ? currentSelection() : info && info.selection;
  return safeFilenamePart(stem, "slide") + "--" +
    selectionFilenameTag(selection) + ".geojson";
}

function exportAnnotationsGeoJSON() {
  if (!annotationOwnerReady()) {
    showToast("Choose a site before exporting annotations");
    return;
  }
  const doc = annotationsToGeoJSON(state.annotations);
  const owner = annotationDocument();
  doc.coordinate_space = owner.coordinate_space;
  doc.source = owner.source;
  doc.selection = owner.selection;
  downloadJsonFile(doc, geojsonFilename(state.info), "application/geo+json");
  showToast("exported " + doc.features.length + " annotation" + (doc.features.length === 1 ? "" : "s") + " as GeoJSON");
}

function annotationDocument() {
  const ps = pixelSize();
  return {
    format: "nd2wsi-annotations/2",
    coordinate_space: "level-0-pixels",
    source: {
      name: state.info.name,
      width: state.info.width,
      height: state.info.height,
    },
    calibration: ps
      ? {
          status: "calibrated",
          source: "viewer-metadata",
          y_um_per_px: ps[0],
          x_um_per_px: ps[1],
        }
      : { status: "unknown", source: "unknown" },
    selection: currentSelection(),
    items: state.annotations,
  };
}

function currentSelection() {
  // the T/P/Z plane the annotations belong to: a plate's sidecar is per
  // site, an ordinary slide's is the plane the cache was built from
  if (state.plate) {
    const frame = activeFrameParams();
    return frame && frame.p === state.annSite ? { p: frame.p } : null;
  }
  return state.info.selection || {};
}

function normalizedSelection(selection) {
  const source = selection || {};
  return {
    t: Number(source.t || 0),
    p: Number(source.p || 0),
    z: String(source.z === undefined ? "mid" : source.z),
  };
}

function annotationItemsForThisSlide(data, geojsonFile = false) {
  if (geojsonFile || window.Nd2AnnotationGeoJSON.isGeoJSON(data)) {
    const items = window.Nd2AnnotationGeoJSON.parse(data, state.info);
    if (data.selection) {
      const current = currentSelection();
      if (!current) throw new Error("choose a plate site first");
      const expected = normalizedSelection(current), actual = normalizedSelection(data.selection);
      if (expected.t !== actual.t || expected.p !== actual.p || expected.z !== actual.z) {
        throw new Error("annotations belong to a different T/P/Z plane");
      }
    }
    const { verified } = annotationItemsForThisSlide({ items: [], source: data.source,
      coordinate_space: data.coordinate_space, selection: data.selection });
    return { items, verified, append: true };
  }
  if (Array.isArray(data)) return { items: data, verified: false };
  if (!data || !Array.isArray(data.items)) throw new Error("no items array");

  const source = data.source;
  if (typeof source === "string") {
    if (source !== state.info.name) {
      throw new Error("this file belongs to " + source);
    }
    return { items: data.items, verified: true };
  }
  if (!source || typeof source !== "object") {
    return { items: data.items, verified: false };
  }
  if (source.name && source.name !== state.info.name) {
    throw new Error("this file belongs to " + source.name);
  }
  if (
    Number(source.width) !== Number(state.info.width) ||
    Number(source.height) !== Number(state.info.height)
  ) {
    throw new Error("source dimensions do not match this slide");
  }
  if (data.coordinate_space && data.coordinate_space !== "level-0-pixels") {
    throw new Error("unsupported annotation coordinate space");
  }
  if (data.selection) {
    const current = currentSelection();
    if (!current) throw new Error("choose a plate site first");
    const expected = normalizedSelection(current);
    const actual = normalizedSelection(data.selection);
    if (expected.t !== actual.t || expected.p !== actual.p || expected.z !== actual.z) {
      throw new Error("annotations belong to a different T/P/Z plane");
    }
  }
  return { items: data.items, verified: true };
}

function rebuildAnnList() {
  const list = $("ann-list");
  list.replaceChildren();
  $("ann-empty").style.display = state.annotations.length ? "none" : "";
  for (const a of state.annotations) {
    const row = document.createElement("div");
    row.className = "ann-item";
    const g = document.createElement("button");
    g.className = "glyph";
    g.type = "button";
    g.textContent = "●";
    g.title = "Change color";
    g.style.color = annColor(a);
    g.onclick = (ev) => { ev.stopPropagation(); cycleAnnColor(a); };
    const txt = document.createElement("span");
    txt.className = "txt";
    txt.textContent =
      a.type === "geojson" ? window.Nd2AnnotationGeoJSON.label(a) :
      a.type === "line" ? (a.text || "Measurement") : (a.text || (a.type === "pin" ? "Pin" : "Box"));
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = a.type === "line" ? lineLengthLabel(a) : "";
    const del = document.createElement("button");
    del.className = "del";
    del.title = "Delete";
    del.textContent = "×";
    del.onclick = (ev) => { ev.stopPropagation(); removeAnnotation(a.id); };
    row.append(g, txt, meta, del);
    row.onclick = () => flyToAnnotation(a);
    list.append(row);
  }
}

function flyToAnnotation(a) {
  const vp = state.viewer.viewport;
  if (a.type === "pin") {
    vp.panTo(vp.imageToViewportCoordinates(new OpenSeadragon.Point(a.x, a.y)));
  } else {
    const r =
      a.type === "box"
        ? new OpenSeadragon.Rect(a.x, a.y, a.w, a.h)
        : a.type === "geojson" ? (() => {
          const bounds = window.Nd2AnnotationGeoJSON.bounds(a.geojson.geometry);
          return new OpenSeadragon.Rect(bounds.x, bounds.y, bounds.w || 1, bounds.h || 1);
        })()
        : new OpenSeadragon.Rect(
            Math.min(a.x1, a.x2), Math.min(a.y1, a.y2),
            Math.abs(a.x2 - a.x1) || 1, Math.abs(a.y2 - a.y1) || 1
          );
    const m = Math.max(r.width, r.height) * 0.6;
    vp.fitBounds(
      vp.imageToViewportRectangle(
        new OpenSeadragon.Rect(r.x - m, r.y - m, r.width + 2 * m, r.height + 2 * m)
      )
    );
  }
}

function finishSelection(box) {
  setTool(null);
  if (!box || box.w < 4 || box.h < 4) return; // treat as an aborted drag
  applyRoi(box.x, box.y, box.w, box.h);
}

function applyRoi(x, y, w, h) {
  const context = activeFrameContext();
  if (!context) {
    showToast("Choose a site before marking a region");
    return false;
  }
  state.roi = { x, y, w, h };
  state.roiSite = context.frame ? context.frame.p : null;
  state.renderScale = null; // a new region starts at the finest fitting scale
  drawRoiOverlay();
  updateRoiPanel();
  $("roi-detail").classList.add("active");
  $("roi-hint").style.display = "none";
  $("roi-move").disabled = false;
  syncFrameScopedControls();
  state.windows.region.fitContent(); // saved heights must not hide the exports
  return true;
}

function roiOverlayPoints(r) {
  if (!r) return [];
  return [
    imageToViewerElementPoint({ x: r.x, y: r.y }),
    imageToViewerElementPoint({ x: r.x + r.w, y: r.y }),
    imageToViewerElementPoint({ x: r.x + r.w, y: r.y + r.h }),
    imageToViewerElementPoint({ x: r.x, y: r.y + r.h }),
  ];
}

function ensureRoiOverlayLayer() {
  let layer = $("roi-layer");
  if (layer) return layer;
  layer = document.createElementNS(SVG_NS, "svg");
  layer.id = "roi-layer";
  layer.setAttribute("aria-hidden", "true");
  Object.assign(layer.style, {
    position: "absolute",
    inset: "0",
    zIndex: "21",
    width: "100%",
    height: "100%",
    overflow: "hidden",
    pointerEvents: "none",
  });
  // This layer is deliberately separate: annotation redraws replace every
  // child of #ann-layer, while a selected ROI must remain on screen.
  $("ann-layer").before(layer);
  return layer;
}

function drawRoiOverlay() {
  if (state.roiOverlayEl) {
    state.roiOverlayEl.remove();
    state.roiOverlayEl = null;
  }
  if (!state.roi || !frameOwnsRoi()) return;
  const polygon = document.createElementNS(SVG_NS, "polygon");
  polygon.classList.add("roi-overlay");
  polygon.setAttribute("fill", "rgba(10, 132, 255, 0.10)");
  polygon.setAttribute("stroke", "var(--accent-soft)");
  polygon.setAttribute("stroke-width", "1.5");
  polygon.setAttribute("vector-effect", "non-scaling-stroke");
  ensureRoiOverlayLayer().append(polygon);
  state.roiOverlayEl = polygon;
  moveRoiOverlay();
}

function moveRoiOverlay() {
  const r = state.roi;
  if (!r || !frameOwnsRoi()) {
    if (state.roiOverlayEl) {
      state.roiOverlayEl.remove();
      state.roiOverlayEl = null;
    }
    return;
  }
  if (!state.roiOverlayEl) return drawRoiOverlay();
  state.roiOverlayEl.setAttribute(
    "points",
    roiOverlayPoints(r).map((p) => p.x + "," + p.y).join(" ")
  );
}

function restoreRoiOverlay() {
  if (frameOwnsRoi()) drawRoiOverlay();
}

function clearRoi() {
  state.roi = null;
  state.roiSite = null;
  state.renderScale = null;
  scaleSeq += 1;
  if (state.roiOverlayEl) {
    state.roiOverlayEl.remove();
    state.roiOverlayEl = null;
  }
  $("roi-detail").classList.remove("active");
  $("roi-hint").style.display = "";
  $("roi-move").disabled = true;
  if (state.tool === "move") setTool("move"); // toggles the mode off
  syncFrameScopedControls();
}

function updateRoiPanel() {
  refreshAstromapPanel();
  const r = state.roi;
  if (!r) return;
  const ps = pixelSize();
  const put = (id, v) => {
    const el = $(id);
    if (document.activeElement !== el) el.value = v; // don't clobber typing
  };
  put("roi-px-w", Math.round(r.w));
  put("roi-px-h", Math.round(r.h));
  if (ps) {
    put("roi-um-w", (r.w * ps[1]).toFixed(1));
    put("roi-um-h", (r.h * ps[0]).toFixed(1));
  } else {
    for (const id of ["roi-um-w", "roi-um-h"]) {
      $(id).value = "";
      $(id).disabled = true;
      $(id).title = "Calibration unavailable — the file carries no pixel size";
    }
  }
  const itemsize = dtypeBytes(state.info.dtype);
  const nCh = state.info.rgb ? 3 : state.channels.length;
  $("roi-bytes").textContent = "≈ " + fmtBytes(r.w * r.h * nCh * itemsize);
  updateScaleStrip();
}

/* ---- export scale: how TIFF, PNG and JPEG leave -------------------------
   Each option is one pyramid level. The strip shows the same field at every
   scale so the pixel cost is visible, the dims line shows what the file
   will hold, and the size is measured from one real 512 px sample at that
   level rather than guessed. TIFF preserves raw channels at the selected
   scale. ND2 always streams native-resolution pixels. */
function scaleOptions() {
  const r = state.roi;
  if (!r) return [];
  const out = [];
  for (const lv of state.info.levels) {
    const d = lv.downsample;
    const w = Math.max(1, Math.floor(r.w / d));
    const h = Math.max(1, Math.floor(r.h / d));
    if ((w * h) / 1e6 <= state.info.maxRenderMpx) out.push({ d, w, h });
    if (out.length >= 4) break;
  }
  return out;
}

function roiScale() {
  const opts = scaleOptions();
  if (!opts.length) return null;
  const pick = opts.find((o) => o.d === state.renderScale);
  return pick || opts[0]; // finest that fits, unless the user chose coarser
}

let scaleSeq = 0;
function updateScaleStrip() {
  syncScaleBarExportControls();
  const strip = $("roi-scales");
  const dims = $("roi-scale-dims");
  const context = activeFrameContext();
  const r = state.roi ? { ...state.roi } : null;
  const opts = scaleOptions();
  const seq = ++scaleSeq;
  strip.innerHTML = "";
  if (!r || !opts.length || !frameOwnsRoi(context)) {
    dims.textContent = "–";
    return;
  }
  const sel = roiScale();
  const extra = renderParams(new URLSearchParams());
  appendFrameParams(extra, context.frame);
  const winQ = extra.toString() ? "&" + extra.toString() : "";
  // one shared field, sized to the coarsest scale so every patch is honest
  const dMax = opts[opts.length - 1].d;
  const field = Math.min(64 * dMax, r.w, r.h); // native px on a side
  const fx = Math.round(r.x + r.w / 2 - field / 2);
  const fy = Math.round(r.y + r.h / 2 - field / 2);

  for (const o of opts) {
    const btn = document.createElement("button");
    btn.className = "scale-opt" + (o.d === sel.d ? " sel" : "");
    const img = document.createElement("img");
    const pw = Math.max(1, Math.round(field / o.d));
    img.src = "api/roi?" + new URLSearchParams({
      level: levelPathFor(o.d), x: Math.max(0, Math.floor(fx / o.d)),
      y: Math.max(0, Math.floor(fy / o.d)), w: pw, h: pw, format: "jpg",
    }) + winQ;
    img.alt = "";
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = o.d === 1 ? "1×" : "1/" + o.d;
    btn.append(img, tag);
    btn.onclick = () => {
      state.renderScale = o.d;
      updateScaleStrip();
    };
    strip.append(btn);
  }

  dims.textContent = fmtInt(sel.w) + " × " + fmtInt(sel.h) + " px";
  const note = $("roi-scale-note");
  note.textContent = "TIFF uses this scale; ND2 stays native.";
  estimateRenderBytes(sel, winQ, r).then((t) => {
    if (seq === scaleSeq && activeFrameContext()?.key === context.key && t) {
      note.textContent = t + " · TIFF keeps raw channels at this scale; ND2 stays native.";
    }
  });
}

function levelPathFor(d) {
  const lv = state.info.levels.find((l) => l.downsample === d);
  return lv ? lv.path : 0;
}

const sizeSamples = new Map(); // sid-stable cache: level+fmt+win -> bytes/px
async function estimateRenderBytes(o, winQ, r = state.roi) {
  if (!r) return "";
  const side = Math.min(512, o.w, o.h);
  const sx = Math.max(0, Math.floor((r.x + r.w / 2) / o.d) - side / 2);
  const sy = Math.max(0, Math.floor((r.y + r.h / 2) / o.d) - side / 2);
  const parts = [];
  for (const fmt of ["jpg", "png"]) {
    const key = o.d + ":" + fmt + ":" + winQ + ":" + Math.round(sx / 512);
    let bpp = sizeSamples.get(key);
    if (bpp === undefined) {
      try {
        const res = await fetch("api/roi?" + new URLSearchParams({
          level: levelPathFor(o.d), x: sx, y: sy, w: side, h: side, format: fmt,
        }) + winQ, { cache: "no-store" });
        const blob = await res.blob();
        bpp = blob.size / (side * side);
        sizeSamples.set(key, bpp);
      } catch (e) { bpp = null; }
    }
    if (bpp) parts.push("≈ " + fmtBytes(bpp * o.w * o.h) + " " + (fmt === "jpg" ? "JPEG" : "PNG"));
  }
  return parts.join(" · ") || "";
}

/* The calibration for the µm fields is the file's own: ND2 voxel_size() /
   Aperio MPP, carried through the store as pixelSizeUm. Typing a size keeps
   the region's top-left corner, clamps to the slide, and syncs both rows. */
function applyRoiDims(unit) {
  const r = state.roi;
  if (!r || !frameOwnsRoi()) return;
  const info = state.info;
  const ps = pixelSize();
  if (unit === "um" && !ps) return; // no physical entry without calibration
  const [py, px] = ps || [1, 1];
  const num = (id) => parseFloat(String($(id).value).replace(/[,\s]/g, ""));
  let w;
  let h;
  if (unit === "um") {
    w = num("roi-um-w") / px;
    h = num("roi-um-h") / py;
  } else {
    w = num("roi-px-w");
    h = num("roi-px-h");
  }
  if (!isFinite(w) || w < 4) w = r.w;
  if (!isFinite(h) || h < 4) h = r.h;
  w = Math.round(Math.min(w, info.width));
  h = Math.round(Math.min(h, info.height));
  // keep the requested size: slide the origin back if the box would overflow
  r.x = Math.min(r.x, info.width - w);
  r.y = Math.min(r.y, info.height - h);
  r.w = w;
  r.h = h;
  drawRoiOverlay();
  updateRoiPanel();
}

function wireRoiDims() {
  for (const [id, unit] of [
    ["roi-px-w", "px"], ["roi-px-h", "px"],
    ["roi-um-w", "um"], ["roi-um-h", "um"],
  ]) {
    const el = $(id);
    el.addEventListener("change", () => applyRoiDims(unit));
    el.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); el.blur(); }
      ev.stopPropagation();
    });
    el.addEventListener("focus", () => el.select());
  }
}

function scaleBarExportIssue() {
  const ps = pixelSize();
  if (!ps || ps.length !== 2 || !ps.every((v) => Number.isFinite(v) && v > 0)) {
    return "Scale bar needs valid pixel calibration";
  }
  const sel = roiScale();
  if (!sel || sel.w < 96 || sel.h < 64) {
    return "Choose a larger export size for the scale bar (at least 96 × 64 px)";
  }
  return "";
}

function syncScaleBarExportControls() {
  const context = activeFrameContext();
  const issue = !frameOwnsRoi(context)
    ? context ? "Mark a region first" : "Choose a site first"
    : scaleBarExportIssue();
  for (const id of ["roi-dl-svg-scale", "roi-dl-jpg-scale"]) scopedButton(id, !!issue, issue);
}

function downloadRoi(fmt, scaleBar = false) {
  const context = activeFrameContext();
  const r = state.roi ? { ...state.roi } : null;
  if (!r || !frameOwnsRoi(context)) {
    showToast("Choose a site and mark a region before exporting");
    return;
  }
  // The screen shows no channel. An omitted channel set would mean all of
  // them, so no format may silently export what is not on screen.
  if (!state.channels.length) {
    showToast("Turn on at least one channel to export");
    return;
  }
  scaleBar = scaleBar || fmt === "svg";
  if (scaleBar) {
    const issue = scaleBarExportIssue();
    if (issue) { showToast(issue); return; }
  }
  // TIFF streams raw channels at the selected scale; ND2 stays native.
  // Rendered images use the same selection, bounded by the raster limit.
  let level = 0;
  let d = 1;
  const rendered = fmt === "png" || fmt === "jpg" || fmt === "svg";
  if (rendered) {
    const sel = roiScale();
    if (!sel) {
      showToast(
        "rendered export capped at " + state.info.maxRenderMpx +
        " MPx — use ND2/TIFF (they stream any size)"
      );
      return;
    }
    d = sel.d;
    level = Number(levelPathFor(d));
  } else if (fmt === "tiff") {
    const sel = roiScale();
    if (sel) {
      d = sel.d;
      level = Number(levelPathFor(d));
    }
  }
  const q = new URLSearchParams({
    level,
    x: Math.floor(r.x / d),
    y: Math.floor(r.y / d),
    w: Math.max(1, Math.floor(r.w / d)),
    h: Math.max(1, Math.floor(r.h / d)),
    format: fmt,
  });
  // Rendered exports match the screen LUTs and name the committed pyramid;
  // ND2 and TIFF carry the channel set alone and keep the source values.
  if (rendered) renderParams(q);
  else channelQuery(q);
  if (scaleBar) q.set("scalebar", "1");
  appendFrameParams(q, context.frame); // explicit active frame; never hidden P0
  let job = null;
  if (fmt === "nd2" || fmt === "tiff") {
    job = Math.random().toString(36).slice(2, 10);
    q.set("job", job);
  }
  const a = document.createElement("a");
  a.href = "api/roi?" + q.toString();
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
  if (job) trackExport(job, fmt.toUpperCase());
  else if (level > 0) {
    showToast("export started at 1/" + d + " scale (" +
      Math.round((r.w / d) * (r.h / d) / 1e6) + " MPx) — check your downloads");
  } else showToast("export started — check your downloads");
}

/* Poll the server's write progress and fill the status-bar export gauge. */
let exportTimer = null;
function trackExport(job, label) {
  const cell = $("export-cell");
  const fill = $("export-fill");
  const pct = $("export-pct");
  clearInterval(exportTimer);
  let shown = false;
  const t0 = Date.now();
  exportTimer = setInterval(() => {
    fetch("api/roi/progress?job=" + job, { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        const p = d.pct || 0;
        if (d.state === "writing" || d.state === "streaming") {
          if (!shown) {
            shown = true;
            cell.hidden = false;
            cell.classList.remove("done");
          }
          fill.style.width = p + "%";
          pct.textContent =
            d.state === "writing" ? label + " " + p + " %" : label + " saving…";
        } else if (d.state === "done") {
          clearInterval(exportTimer);
          if (shown) {
            fill.style.width = "100%";
            cell.classList.add("done");
            pct.textContent = label + " done";
            setTimeout(() => { cell.hidden = true; }, 2000);
          }
        } else if (d.state === "error") {
          clearInterval(exportTimer);
          cell.hidden = true;
          showToast("export failed: " + (d.error || "unknown error"));
        } else if (!shown && Date.now() - t0 > 4000) {
          clearInterval(exportTimer); // job never materialized, stay hidden
        }
        if (Date.now() - t0 > 15 * 60 * 1000) {
          clearInterval(exportTimer);
          cell.hidden = true;
        }
      })
      .catch(() => {});
  }, 350);
}

/* ---- slide inspector ------------------------------------------------------- */

function wireSlideInspector() {
  $("info-retry").onclick = () => loadSlideInspector(true);
  $("info-copy").onclick = () => {
    const data = state.inspect || state.info;
    const text = slideInspectionRows(data)
      .map(([label, value]) => label + ": " + value)
      .join("\n");
    copyPlainText(text)
      .then(() => {
        $("info-status").textContent = "Metadata copied to the clipboard";
      })
      .catch(() => {
        $("info-status").textContent = "Could not copy metadata";
      });
  };
  $("info-reveal-source").onclick = () => revealSlidePath("source");
  $("info-reveal-cache").onclick = () => revealSlidePath("cache");
  $("info-copy").disabled = true;
  $("info-reveal-source").disabled = true;
  $("info-reveal-cache").disabled = true;
}

async function loadSlideInspector(force = false) {
  if (state.inspectLoading || (state.inspect && !force)) return;
  state.inspectLoading = true;
  $("info-loading").hidden = false;
  $("info-loading").textContent = state.inspect ? "Refreshing slide metadata…" : "Loading slide metadata…";
  $("info-error").hidden = true;
  if (!state.inspect) $("info-content").hidden = true;
  try {
    const res = await fetch("api/inspect", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (!data || typeof data !== "object") throw new Error("invalid response");
    state.inspect = { ...state.info, ...data };
    renderSlideInspector(state.inspect);
    $("info-status").textContent = "";
  } catch (error) {
    state.inspect = null;
    renderSlideInspector(state.info, true);
    $("info-error").hidden = false;
    $("info-error").querySelector("span").textContent =
      "Detailed metadata is unavailable (" + error.message + ").";
    $("info-status").textContent = "Basic viewer metadata is shown below";
  } finally {
    state.inspectLoading = false;
    $("info-loading").hidden = true;
  }
}

function formatMetadataValue(value) {
  if (value === null || value === undefined || value === "") return "Not reported";
  if (typeof value !== "object") return String(value);
  if (Array.isArray(value)) return value.map(formatMetadataValue).join(" · ");
  const parts = Object.entries(value)
    .filter(([, item]) => item !== null && item !== undefined && item !== "")
    .map(([key, item]) => key.replace(/_/g, " ") + " " + formatMetadataValue(item));
  return parts.length ? parts.join(" · ") : "Not reported";
}

function calibratedPixelSize(data) {
  const calibration = data && typeof data.calibration === "object" ? data.calibration : {};
  const value = calibration.pixel_size_um || calibration.pixelSizeUm ||
    (data && (data.pixelSizeUm || data.pixel_size_um));
  return Array.isArray(value) && value.length >= 2 ? value.map(Number) : null;
}

function calibrationLabel(data) {
  const ps = calibratedPixelSize(data);
  if (!ps || !ps.every(Number.isFinite)) return "Unknown";
  const calibration = data && typeof data.calibration === "object" ? data.calibration : {};
  const source = calibration.source || data.calibration_source;
  const value = rawValueLabel(ps[1]) + " × " + rawValueLabel(ps[0]) + " µm/px";
  return source ? value + " · " + source : value;
}

function selectionLabel(selection) {
  const source = selection || {};
  const z = source.z_resolved !== undefined
    ? source.z_resolved
    : source.z === undefined ? "mid" : source.z;
  return "T" + (source.t === undefined ? 0 : source.t) +
    " · P" + (source.p === undefined ? 0 : source.p) +
    " · Z" + z;
}

function byteSizeLabel(logical, allocated) {
  if (logical === null || logical === undefined || !Number.isFinite(Number(logical))) {
    return "Unavailable";
  }
  let value = "Data " + fmtBytes(Number(logical));
  if (allocated !== null && allocated !== undefined && Number.isFinite(Number(allocated))) {
    value += " · Disk " + fmtBytes(Number(allocated));
  }
  return value;
}

function storageLabel(storage) {
  const labels = {
    compact: "Compact cache (source-backed)",
    full: "Portable pyramid",
    direct: "Direct source",
    "overview-degraded": "Overview only (source missing)",
  };
  return labels[storage] || formatMetadataValue(storage);
}

function slideInspectionRows(data) {
  data = data || {};
  const channels = Array.isArray(data.channels)
    ? data.channels.map((channel, i) => channel.label || channel.name || "C" + i).join(", ")
    : "Not reported";
  const levels = Array.isArray(data.levels)
    ? data.levels.map((level) =>
        "L" + level.path + " " + fmtInt(level.width) + " × " + fmtInt(level.height) +
        " (1/" + rawValueLabel(level.downsample) + ")"
      ).join(" · ")
    : "Not reported";
  const sourcePath = data.source_path || "Unavailable";
  const cachePath = data.cache_path || (
    data.storage !== "direct" && data.container_path ? data.container_path : null
  );
  const plate = data.kind === "plate" && data.plate && typeof data.plate === "object"
    ? data.plate
    : null;
  const rows = [
    ["File", data.name || "Unnamed slide"],
    ...(plate
      ? [["Kind", "Volumetric temporal · T " + plate.T + " × sites " + plate.P + " × Z " + plate.Z]]
      : []),
    ["Dimensions", fmtInt(data.width || 0) + " × " + fmtInt(data.height || 0) + " px" +
      (plate ? " per frame" : "")],
    ["Pixel type", (data.dtype || "Unknown") + (data.rgb ? " · RGB" : "")],
    ["Channels", data.rgb ? "RGB" : channels],
    ...(plate ? plateInspectionRows(plate) : []),
    ["Calibration", calibrationLabel(data)],
    ["Objective", formatMetadataValue(data.objective)],
    ["Plane", plate ? platePlaneLabel() : selectionLabel(data.selection)],
    ["Pyramid", levels],
    ["Storage", storageLabel(data.storage)],
    ["Source", sourcePath],
    ["Source size", byteSizeLabel(data.source_bytes, data.source_allocated_bytes)],
    ["Cache", cachePath || (data.storage === "direct" ? "None (served from the file)" : "Unavailable")],
    ["Cache size", cachePath
      ? byteSizeLabel(data.cache_bytes, data.cache_allocated_bytes)
      : data.storage === "direct" ? "None" : "Unavailable"],
  ];
  if (data.storage_details && typeof data.storage_details === "object") {
    rows.splice(9, 0, ["Storage details", formatMetadataValue(data.storage_details)]);
  }
  if (Array.isArray(data.associated) && data.associated.length) {
    rows.push(["Associated", data.associated.join(", ")]);
  }
  if (Array.isArray(data.notes) && data.notes.length) {
    rows.push(["Notes", data.notes.join(" · ")]);
  }
  return rows;
}

function renderSlideInspector(data, partial = false) {
  const grid = $("info-grid");
  grid.replaceChildren();
  for (const [label, value] of slideInspectionRows(data)) {
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = value;
    if (label === "Source" || label === "Cache") {
      detail.classList.add("info-path");
      detail.title = value;
    }
    grid.append(term, detail);
  }
  $("info-content").hidden = false;
  $("info-copy").disabled = false;
  $("info-reveal-source").disabled = partial || !data.source_path || data.source_bytes === null;
  $("info-reveal-cache").disabled = partial || !(
    data.cache_path || (data.storage !== "direct" && data.container_path)
  );
  renderAssociatedImages(partial ? [] : data.associated);
  if (state.windows && state.windows.info) state.windows.info.fitContent();
}

function renderAssociatedImages(names) {
  const section = $("info-associated-section");
  const container = $("info-associated");
  container.replaceChildren();
  const available = new Set((Array.isArray(names) ? names : []).map((name) => String(name).toLowerCase()));
  const namesInOrder = ["thumbnail", "label", "macro",
    ...Array.from({ length: 16 }, (_, i) => "associated-" + (i + 1))];
  for (const name of namesInOrder) {
    if (!available.has(name)) continue;
    const figure = document.createElement("figure");
    const button = document.createElement("button");
    const image = document.createElement("img");
    const caption = document.createElement("figcaption");
    const label = name.startsWith("associated-")
      ? "Embedded image " + name.split("-")[1]
      : name[0].toUpperCase() + name.slice(1);
    button.type = "button";
    button.className = "info-image-toggle";
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-label", "Enlarge " + label.toLowerCase() + " image");
    image.alt = label + " associated image";
    image.loading = "lazy";
    const imageUrl = "api/associated/" + name + ".jpg";
    const query = new URLSearchParams();
    if (state.info.generation) query.set("g", state.info.generation);
    image.src = imageUrl + "?" + query.toString();
    caption.textContent = label + " · Enlarge";
    let expanded = false;
    button.addEventListener("click", () => {
      expanded = !expanded;
      figure.classList.toggle("expanded", expanded);
      button.setAttribute("aria-expanded", String(expanded));
      button.setAttribute("aria-label", (expanded ? "Reduce " : "Enlarge ") +
        label.toLowerCase() + " image");
      caption.textContent = label + (expanded ? " · Reduce" : " · Enlarge");
      if (expanded) {
        query.set("detail", "1");
        image.src = imageUrl + "?" + query.toString();
      }
      state.windows?.info?.fitContent();
    });
    image.addEventListener("error", () => {
      figure.remove();
      if (!container.children.length) section.hidden = true;
    });
    button.append(image);
    figure.append(button, caption);
    container.append(figure);
  }
  section.hidden = !container.children.length;
}

async function copyPlainText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (_) { /* fall through to the WebKit-compatible copy path */ }
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.append(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

async function revealSlidePath(which) {
  const button = $(which === "source" ? "info-reveal-source" : "info-reveal-cache");
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = "Revealing…";
  try {
    const res = await fetch("api/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ which }),
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* response may be empty */ }
    if (!res.ok || (data && data.error)) {
      throw new Error((data && data.error) || "HTTP " + res.status);
    }
    const label = which === "source" ? "Source" : "Cache";
    $("info-status").textContent = label + " revealed in the file manager";
  } catch (error) {
    $("info-status").textContent = "Could not reveal " + which + ": " + error.message;
  } finally {
    button.textContent = previous;
    const data = state.inspect || {};
    button.disabled = which === "source"
      ? !data.source_path || data.source_bytes === null
      : !(data.cache_path || (data.storage !== "direct" && data.container_path));
  }
}

/* ---- linked compare viewport relay ---------------------------------------
   The tab shell owns pairing and alignment. Each persistent slide iframe
   reports its current image-space field of view and accepts an immediate
   image-space fit. Physical mapping is possible when both slides report
   calibration; otherwise the shell uses normalized slide coordinates. */

const VIEWPORT_PROTOCOL_VERSION = 2;
const VIEWPORT_EMIT_MS = 50;

function spatialIdentity() {
  const gate = state.viewportRelay.commandGate;
  return { ...gate.envelope(), spatialContext: gate.spatialContext };
}

function spatialImageReady() {
  const viewer = state.viewer;
  return Boolean(viewer?.viewport && viewer.world?.getItemCount() &&
    !state.viewportRelay.reopening && !state.viewportRelay.contextChanging);
}

function spatialPaneReady() {
  refreshSpatialContext();
  const gate = state.viewportRelay.commandGate;
  return gate.enabled && gate.spatialEnabled && Boolean(gate.spatialContext) && spatialImageReady();
}

function clearPaneSpatialWork(resetPose = true) {
  const relay = state.viewportRelay;
  relay.commandGate.pendingViewportCommand = null;
  relay.alignmentDrag = null;
  relay.suppressAlignmentDragEnd = false;
  clearTimeout(relay.timer);
  relay.timer = null;
  if (state.landmark.active || state.landmark.points.length) {
    setLandmarkMode({ active: false, clear: true });
  }
  if (resetPose) {
    relay.displayRotation = 0;
    relay.displayFlipped = false;
    applyDesiredDisplayTransform(false);
  }
}

function refreshSpatialContext(preservePose = false) {
  if (!state.info) return false;
  const context = window.Nd2SpatialPane.spatialContext(activeFrameContext(), Boolean(state.plate));
  const changed = state.viewportRelay.commandGate.setContext(context);
  if (changed) {
    clearPaneSpatialWork(!preservePose);
    postSpatialReadiness();
  }
  return changed;
}

function postSpatialReadiness() {
  state.shading?.syncContext();
  if (window.parent === window) return;
  window.parent.postMessage({
    nd2wsi: "viewport-ready", version: VIEWPORT_PROTOCOL_VERSION,
    sid: currentSlideSid(), seq: ++state.viewportRelay.seq, ...viewportSnapshot(),
  }, location.origin);
}

function landmarkEnvelope() {
  return {
    ...spatialIdentity(),
    editId: state.landmark.editId,
    editRevision: state.landmark.editRevision,
    pointRevision: state.landmark.pointRevision,
  };
}

function receiveCompareLifecycle(message) {
  refreshSpatialContext();
  const relay = state.viewportRelay;
  const oldSession = relay.commandGate.groupSessionId;
  const oldEpoch = relay.commandGate.groupEpoch;
  if (!relay.commandGate.bind(message)) return;
  if (oldSession !== message.groupSessionId || oldEpoch !== message.groupEpoch || !message.enabled) {
    clearPaneSpatialWork(!message.enabled || oldSession !== message.groupSessionId);
  }
  relay.compare = {
    enabled: Boolean(message.enabled), linked: Boolean(message.linked),
    frameLinked: message.frameLinked === undefined ? Boolean(message.linked) : Boolean(message.frameLinked),
    linkMode: message.linkMode || "scale-frame",
    moving: Boolean(message.moving), role: message.role === "anchor" ? "anchor" : "member",
    committedRevision: message.committedRevision,
    nudgeBusy: Boolean(message.nudgeBusy),
  };
}

function receiveSpatialCommand(message) {
  refreshSpatialContext();
  if (!state.viewportRelay.commandGate.receive(message)) return;
  flushPendingSpatialCommand();
}

function flushPendingSpatialCommand() {
  refreshSpatialContext();
  const message = state.viewportRelay.commandGate.takeReady(spatialImageReady());
  if (!message) return;
  // One latest slot, revalidated here even when an old OSD-open callback runs.
  if (message.nd2wsi === "viewport-request") {
    const requestId = String(message.requestId || "");
    if (requestId) postViewportState("request", { requestId });
  } else if (message.nd2wsi === "viewport-apply") applyLinkedViewport(message);
  else if (message.nd2wsi === "viewport-nudge") nudgeView(message.dxPx, message.dyPx, message);
  else if (message.nd2wsi === "display-transform") applyDisplayTransform(message);
  else if (message.nd2wsi === "landmark-mode") setLandmarkMode(message);
}

function normalizedRotation(value) {
  return ((Number(value) % 360) + 360) % 360;
}

function angleDifference(a, b) {
  return ((normalizedRotation(a) - normalizedRotation(b) + 540) % 360) - 180;
}

function applyDesiredDisplayTransform(announce = true) {
  const viewer = state.viewer;
  if (!viewer?.viewport || !viewer.world || !viewer.world.getItemCount()) return false;
  const degrees = state.viewportRelay.displayRotation;
  const flipped = state.viewportRelay.displayFlipped;
  const rotationChanged = Math.abs(angleDifference(viewer.viewport.getRotation(), degrees)) > 0.01;
  const flipChanged = viewer.viewport.getFlip() !== flipped;
  const changed = rotationChanged || flipChanged;
  clearTimeout(state.viewportRelay.timer);
  state.viewportRelay.timer = null;
  state.viewportRelay.suppressUntil = state.viewportRelay.reopening
    ? Number.POSITIVE_INFINITY
    : Date.now() + 180;
  if (changed) {
    if (!$("ann-editor").hidden) closeEditor(true);
    if (rotationChanged) viewer.viewport.setRotation(degrees, true);
    if (flipChanged) viewer.viewport.setFlip(flipped);
    const cursor = state.pixel.cursor;
    if (cursor?.point) {
      state.pixel.result = null;
      state.pixel.failed = false;
      const imagePoint = viewerElementToImagePoint(cursor.point);
      updateCursor(imagePoint, cursor.point);
    }
  }
  renderAnnotations();
  if (state.roiOverlayEl) moveRoiOverlay();
  if (announce) {
    postViewportState("transform", {
      displayRotation: degrees,
      displayFlipped: flipped,
    });
  }
  return true;
}

function applyDisplayTransform(message) {
  if (!state.viewportRelay.commandGate.current(message) || !spatialImageReady()) return;
  const degrees = Number(message.degrees);
  if (!Number.isFinite(degrees) || typeof message.flipped !== "boolean") return;
  state.viewportRelay.displayRotation = normalizedRotation(degrees);
  state.viewportRelay.displayFlipped = message.flipped;
  applyDesiredDisplayTransform();
}

function currentSlideSid() {
  const match = location.pathname.match(/\/s\/([0-9a-f]{8})\//);
  return match ? match[1] : null;
}

function physicalViewportScale() {
  const viewport = state.viewer?.viewport, ps = pixelSize();
  if (!viewport || !ps || ps.length !== 2 ||
      !ps.every((v) => Number.isFinite(v) && v > 0)) return null;
  const size = viewport.getContainerSize();
  if (!(size.x > 0 && size.y > 0)) return null;
  const origin = {x: size.x / 2, y: size.y / 2};
  const a = viewerElementToImagePoint(origin);
  const b = viewerElementToImagePoint({x: origin.x + 100, y: origin.y});
  const c = viewerElementToImagePoint({x: origin.x, y: origin.y + 100});
  const u = {x: (b.x - a.x) * ps[1] / 100, y: (b.y - a.y) * ps[0] / 100};
  const v = {x: (c.x - a.x) * ps[1] / 100, y: (c.y - a.y) * ps[0] / 100};
  const x = Math.hypot(u.x, u.y), y = Math.hypot(v.x, v.y);
  if (![x, y].every((n) => Number.isFinite(n) && n > 0)) return null;
  const result = {x, y, cosine: (u.x * v.x + u.y * v.y) / (x * y)};
  // OSD zoom and screen coordinates use CSS pixels, regardless of Retina DPR.
  const zoom = viewport.viewportToImageZoom(viewport.getZoom(true));
  const minZoom = viewport.viewportToImageZoom(viewport.getMinZoom());
  const maxZoom = viewport.viewportToImageZoom(viewport.getMaxZoom());
  if ([zoom, minZoom, maxZoom].every((n) => Number.isFinite(n) && n > 0)) {
    result.min = x * zoom / maxZoom;
    result.max = x * zoom / minZoom;
  }
  return result;
}

function viewportSnapshot() {
  const viewer = state.viewer;
  const pixelSize = state.info?.pixelSizeUm;
  const calibrated = Array.isArray(pixelSize) && pixelSize.length >= 2 &&
    pixelSize.every((v) => Number.isFinite(v) && v > 0);
  const identity = {
    ...spatialIdentity(), imageReady: spatialImageReady(),
    imagePx: { x: state.info?.width, y: state.info?.height },
    translucentAvailable: !!state.info && (!state.info.rgb || !!state.info.raster),
    translucentOpening: !!state.translucentOpening,
    infoOpen: !!state.windows?.info && !state.windows.info.isHidden(),
    plateGrid: Boolean(state.plate && state.plate.focus === null),
    pixelSizeUm: calibrated ? { x: Number(pixelSize[1]), y: Number(pixelSize[0]) } : null,
  };
  if (!identity.imageReady) return identity;
  // The unrotated bounds describe the field independently of any display
  // rotation or mirror: OSD turns the view about this rectangle's center,
  // and a rotated Rect's corners would fold the angle into the span.
  const bounds = viewer.viewport.getBoundsNoRotate(true);
  const topLeft = viewer.viewport.viewportToImageCoordinates(
    new OpenSeadragon.Point(bounds.x, bounds.y)
  );
  const bottomRight = viewer.viewport.viewportToImageCoordinates(
    new OpenSeadragon.Point(bounds.x + bounds.width, bounds.y + bounds.height)
  );
  const numbers = [topLeft.x, topLeft.y, bottomRight.x, bottomRight.y];
  if (!numbers.every(Number.isFinite)) return { ...identity, imageReady: false };
  return {
    ...identity,
    centerPx: {
      x: (topLeft.x + bottomRight.x) / 2,
      y: (topLeft.y + bottomRight.y) / 2,
    },
    spanPx: {
      x: Math.max(1e-6, Math.abs(bottomRight.x - topLeft.x)),
      y: Math.max(1e-6, Math.abs(bottomRight.y - topLeft.y)),
    },
    imagePx: { x: state.info.width, y: state.info.height },
    plateGrid: Boolean(state.plate && state.plate.focus === null),
    containerPx: (() => {
      const size = viewer.viewport.getContainerSize();
      return { x: Math.max(1, size.x), y: Math.max(1, size.y) };
    })(),
    physicalScale: physicalViewportScale(),
    pixelSizeUm: calibrated
      ? { x: Number(pixelSize[1]), y: Number(pixelSize[0]) }
      : null,
  };
}

function postViewportState(reason, extra = {}) {
  if (window.parent === window) return;
  refreshSpatialContext();
  const snapshot = viewportSnapshot();
  if (!snapshot) return;
  window.parent.postMessage({
    nd2wsi: "viewport-state",
    version: VIEWPORT_PROTOCOL_VERSION,
    sid: currentSlideSid(),
    seq: ++state.viewportRelay.seq,
    reason,
    ...extra,
    ...snapshot,
  }, location.origin);
}

function scheduleViewportState() {
  if (window.parent === window || Date.now() < state.viewportRelay.suppressUntil) return;
  if (state.viewportRelay.alignmentDrag) return;
  if (state.viewportRelay.timer) return;
  state.viewportRelay.timer = setTimeout(() => {
    state.viewportRelay.timer = null;
    if (Date.now() < state.viewportRelay.suppressUntil) return;
    if (state.viewportRelay.alignmentDrag) return;
    postViewportState("user");
  }, VIEWPORT_EMIT_MS);
}

function panByScreenDelta(dxPx, dyPx) {
  const viewer = state.viewer;
  if (!viewer?.viewport || !viewer.world || !viewer.world.getItemCount()) return null;
  const dx = Number(dxPx), dy = Number(dyPx);
  if (!Number.isFinite(dx) || !Number.isFinite(dy) || (!dx && !dy)) return null;
  const viewport = viewer.viewport;
  const size = viewport.getContainerSize();
  const origin = { x: size.x / 2, y: size.y / 2 };
  const a = viewerElementToImagePoint(origin);
  const b = viewerElementToImagePoint({ x: origin.x + dx, y: origin.y + dy });
  const center = viewport.viewportToImageCoordinates(viewport.getCenter(true));
  const target = new OpenSeadragon.Point(center.x - (b.x - a.x), center.y - (b.y - a.y));
  clearTimeout(state.viewportRelay.timer);
  state.viewportRelay.timer = null;
  state.viewportRelay.suppressUntil = Date.now() + 180;
  viewport.panTo(viewport.imageToViewportCoordinates(target), true);
  return {x: target.x - center.x, y: target.y - center.y};
}

function nudgeView(dxPx, dyPx, command = {}) {
  if (!spatialPaneReady() || !state.viewportRelay.commandGate.current(command)) return;
  const delta = panByScreenDelta(dxPx, dyPx);
  if (!delta) return;
  postViewportState("user", {
    nudge: true, nudgeCommandId: command.commandId || null,
    commandSeq: command.commandSeq, nudgeDeltaPx: delta,
    committedRevision: command.committedRevision,
  });
}

function beginAlignmentDrag(event) {
  const relay = state.viewportRelay;
  relay.suppressAlignmentDragEnd = false;
  const shiftPan = event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey &&
    event.pointerType !== "touch";
  if (event.button !== 0 || (!event.altKey && !shiftPan) || !spatialPaneReady() ||
      !relay.compare?.frameLinked || relay.compare.nudgeBusy ||
      (state.tool && !(shiftPan && state.tool === "astromap")) || state.landmark.active) return;
  clearTimeout(relay.timer);
  relay.timer = null;
  relay.suppressAlignmentDragEnd = true;
  // Like Option-drag, Shift-drag keeps ownership until pointerup even if the
  // modifier changes during the gesture. Commit its offset exactly once.
  relay.alignmentDrag = {
    ...spatialIdentity(), dragId: crypto.randomUUID(),
    committedRevision: relay.compare.committedRevision,
    deltaPx: {x: 0, y: 0},
  };
}

function updateAlignmentDrag(event) {
  const drag = state.viewportRelay.alignmentDrag;
  if (!drag || !state.viewportRelay.commandGate.valid(drag)) return;
  event.preventDefaultAction = true;
  const delta = panByScreenDelta(event.delta?.x, event.delta?.y);
  if (!delta) return;
  drag.deltaPx.x += delta.x;
  drag.deltaPx.y += delta.y;
}

function finishAlignmentDrag(cancelled = false) {
  const relay = state.viewportRelay, drag = relay.alignmentDrag;
  relay.alignmentDrag = null;
  if (!drag || !spatialPaneReady() || !relay.commandGate.valid(drag)) return;
  if (!drag.deltaPx.x && !drag.deltaPx.y) return;
  postViewportState("user", {
    nudge: true, nudgeSource: "drag", dragId: drag.dragId,
    nudgeDeltaPx: drag.deltaPx, nudgeCancelled: cancelled,
    committedRevision: drag.committedRevision,
  });
}

function finiteViewportPoint(value, positive = false) {
  if (!value || !Number.isFinite(Number(value.x)) || !Number.isFinite(Number(value.y))) {
    return null;
  }
  const point = { x: Number(value.x), y: Number(value.y) };
  return positive && (point.x <= 0 || point.y <= 0) ? null : point;
}

function applyLinkedViewport(message) {
  if (!state.viewportRelay.commandGate.current(message) || !spatialImageReady()) return;
  if (state.viewportRelay.alignmentDrag) return; // the user's precise drag owns this pane
  const center = finiteViewportPoint(message.centerPx);
  const span = finiteViewportPoint(message.spanPx, true);
  const commandId = String(message.commandId || "");
  if (!center || !span || !commandId) return;

  {
    clearTimeout(state.viewportRelay.timer);
    state.viewportRelay.timer = null;
    // OSD may emit several update events while fitBounds applies constraints.
    // Keep all of those out of the user-event route, then send one explicit
    // acknowledgement carrying the shell command id.
    state.viewportRelay.suppressUntil = Date.now() + 180;
    // fitBounds would fit the bounding box of a rotated field and zoom out
    // under any display rotation, so set the zoom for the field width and
    // the center directly. Both are independent of rotation and mirror.
    const viewport = state.viewer.viewport;
    const independentCenter = message.scaleOnly ? viewport.getCenter(true) : null;
    const screenPerImage = viewport.getContainerSize().x / span.x;
    viewport.zoomTo(viewport.imageToViewportZoom(screenPerImage), null, true);
    if (!independentCenter) viewport.panTo(
      viewport.imageToViewportCoordinates(new OpenSeadragon.Point(center.x, center.y)), true);
    viewport.applyConstraints(true);
    // Keep the live centre, not a possibly stale centre from the shell's snapshot.
    if (independentCenter) viewport.panTo(independentCenter, true);
    postViewportState("apply", { echoOf: commandId });
  }
}

function wireCompareRelay() {
  window.addEventListener("message", (event) => {
    if (
      window.parent === window || event.source !== window.parent ||
      event.origin !== location.origin || !event.data ||
      event.data.version !== VIEWPORT_PROTOCOL_VERSION
    ) return;
    if (event.data.nd2wsi === "astromap-toggle") {
      refreshSpatialContext();
      if (event.data.sid === currentSlideSid() &&
          state.viewportRelay.commandGate.matchesLocal(event.data)) toggleAstromap(event.data.open === true);
    } else if (["shading-status-request", "shading-command"].includes(event.data.nd2wsi)) {
      receiveShadingCommand(event.data);
    } else if (event.data.nd2wsi === "prepare-quit") {
      const requestId = String(event.data.requestId || "");
      if (requestId) acknowledgeUpdatePreparation(requestId);
    } else if (event.data.nd2wsi === "cancel-quit") {
      cancelUpdatePreparation(String(event.data.requestId || ""));
    } else if (["viewport-request", "viewport-apply", "viewport-nudge", "display-transform", "landmark-mode"].includes(event.data.nd2wsi)) {
      receiveSpatialCommand(event.data);
    } else if (event.data.nd2wsi === "compare-state") {
      receiveCompareLifecycle(event.data);
    } else if (event.data.nd2wsi === "tab-shortcut-state") {
      state.tabCount = Math.max(0, Math.floor(Number(event.data.count) || 0));
    } else if (event.data.nd2wsi === "pane-translucent-open") {
      refreshSpatialContext();
      if (event.data.sid === currentSlideSid() &&
          state.viewportRelay.commandGate.matchesLocal(event.data)) openTranslucentViewer();
    } else if (event.data.nd2wsi === "pane-panel-shortcut") {
      refreshSpatialContext();
      // A shell key belongs to the pane/site selected when it was pressed,
      // including a plate grid, not a later reload or site selection.
      if (state.viewportRelay.commandGate.matchesLocal(event.data)) {
        togglePanelForShortcut(event.data.panel);
      }
    } else if (event.data.nd2wsi === "pane-orientation-shortcut") {
      refreshSpatialContext();
      // A shell-focused shortcut captures one pane/site. Never replay it on a
      // new site, a reloaded pane, or a newly established Compare group.
      if (!state.viewportRelay.compare?.enabled &&
          state.viewportRelay.commandGate.matchesLocal(event.data)) {
        applyOrientationShortcut(event.data.action);
      }
    }
  });
  for (const kind of ["pointerdown", "keydown", "beforeinput"]) {
    window.addEventListener(kind, (event) => {
      if (!state.quitPreparing) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);
  }
  // Under the tab strip the toolbar reads as part of the window chrome, so
  // a double-click on its empty space zooms the window too. The shell owns
  // the window and decides; buttons keep their own double-clicks.
  $("toolbar").addEventListener("dblclick", (ev) => {
    if (window.parent === window) return;
    const target = ev.target;
    const empty = target.id === "toolbar" || target.classList.contains("tb-spacer") || target.classList.contains("tb-group");
    if (!empty) return;
    ev.preventDefault();
    window.parent.postMessage({ nd2wsi: "window-zoom", version: VIEWPORT_PROTOCOL_VERSION }, location.origin);
  });
  window.addEventListener("keydown", (ev) => {
    if (!state.landmark.active || window.parent === window || !spatialPaneReady()) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    if (ev.target.closest?.("#tb-plate-view, #plate-view-menu")) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    let kind = null;
    if (ev.key === "Backspace" || ev.key === "Delete") { undoLandmark(); ev.preventDefault(); ev.stopPropagation(); return; }
    if (ev.key === "Escape") kind = "landmark-cancel";
    else if (ev.key === "Enter") kind = "landmark-done";
    if (!kind) return;
    ev.preventDefault();
    ev.stopPropagation();
    window.parent.postMessage({ nd2wsi: kind, version: VIEWPORT_PROTOCOL_VERSION,
      sid: currentSlideSid(), ...landmarkEnvelope() }, location.origin);
  }, true);
  // Arrow keys nudge the alignment while linked. This runs in the capture
  // phase so OpenSeadragon's own arrow-key panning never sees the event.
  window.addEventListener("keydown", (ev) => {
    const compare = state.viewportRelay.compare;
    if (window.parent === window || !compare?.enabled || !compare.frameLinked) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    if (ev.target.closest?.("#tb-plate-view, #plate-view-menu")) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const step = ev.shiftKey ? 10 : 1;
    const delta = {
      ArrowLeft: [-step, 0], ArrowRight: [step, 0],
      ArrowUp: [0, -step], ArrowDown: [0, step],
    }[ev.key];
    if (!delta) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (state.viewportRelay.alignmentDrag) return;
    if (!spatialPaneReady()) return; // grid has no hidden-P0 spatial target
    window.parent.postMessage({
      nd2wsi: "compare-nudge",
      version: VIEWPORT_PROTOCOL_VERSION,
      sid: currentSlideSid(), ...spatialIdentity(),
      dxPx: delta[0],
      dyPx: delta[1],
    }, location.origin);
  }, true);
  // Exactly one listener, never a closure holding an old command or viewport.
  state.viewer.addHandler("open", () => queueMicrotask(() => {
    flushPendingSpatialCommand();
    postSpatialReadiness();
  }));
  refreshSpatialContext();
  postSpatialReadiness();
}

/* ---- appearance ------------------------------------------------------------
   Auto follows the system for fluorescence and opens RGB brightfield in light
   mode. An explicit light or dark choice is never overwritten by the slide. */

const THEME_MODES = ["auto", "light", "dark"];
function currentTheme() {
  return document.documentElement.classList.contains("light") ? "light" : "dark";
}

function resolveTheme(mode) {
  if (mode === "light" || mode === "dark") return mode;
  // Auto follows the slide, not the OS: brightfield color slides (SVS,
  // RGB ND2) read best on light chrome, fluorescence on dark
  return state.info && state.info.rgb ? "light" : "dark";
}

function themeGlyph(mode) {
  if (mode === "light") {
    return '<svg viewBox="0 0 16 16" aria-hidden="true">' +
      '<circle cx="8" cy="8" r="2.7"></circle>' +
      '<path d="M8 1.5v1.4M8 13.1v1.4M1.5 8h1.4M13.1 8h1.4' +
      'M3.4 3.4l1 1M11.6 11.6l1 1M12.6 3.4l-1 1M4.4 11.6l-1 1"></path></svg>';
  }
  if (mode === "dark") {
    return '<svg viewBox="0 0 16 16" aria-hidden="true">' +
      '<path d="M12.9 10.1A5.6 5.6 0 0 1 5.9 3.1 5.6 5.6 0 1 0 12.9 10.1Z"></path></svg>';
  }
  return '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    '<circle cx="8" cy="8" r="5.25"></circle>' +
    '<path d="M8 2.75v10.5a5.25 5.25 0 0 0 0-10.5Z" class="icon-fill"></path></svg>';
}

function applyThemeMode(mode, resolved = null) {
  if (!THEME_MODES.includes(mode)) mode = "auto";
  const theme = resolved || resolveTheme(mode);
  document.documentElement.classList.toggle("light", theme === "light");
  document.documentElement.dataset.themeMode = mode;

  const button = $("tb-theme");
  const icon = button && button.querySelector(".tb-ico");
  if (icon) icon.innerHTML = themeGlyph(mode);
  if (button) {
    const next = THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
    const title = "Appearance: " + mode[0].toUpperCase() + mode.slice(1) +
      " · click for " + next[0].toUpperCase() + next.slice(1);
    button.title = title;
    button.setAttribute("aria-label", title);
    button.dataset.mode = mode;
  }

  if (state.lutWidgets && state.lutWidgets.length) relayoutLuts();
  return theme;
}

function announceTheme(mode, theme) {
  if (window.parent !== window) {
    window.parent.postMessage({ nd2wsi: "theme", mode, theme }, location.origin);
  }
}

function wireTheme() {
  // each tab derives its look from its slide type; nothing persists
  let mode = "auto";
  let theme = applyThemeMode(mode);
  announceTheme(mode, theme);

  $("tb-theme").onclick = () => {
    mode = THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
    theme = applyThemeMode(mode);
    announceTheme(mode, theme);
  };

  window.addEventListener("message", (ev) => {
    if (ev.origin !== location.origin || !ev.data) return;
    if (ev.data.nd2wsi === "theme-request") {
      // coming to the front re-asserts the slide-type default; a manual
      // choice lasts only while the tab stays front
      mode = "auto";
      theme = applyThemeMode(mode);
      announceTheme(mode, theme);
    }
  });
}

function inkColor(a) {
  return currentTheme() === "light"
    ? "rgba(0,0,0," + a + ")"
    : "rgba(255,255,255," + a + ")";
}

/* ---- floating mac windows --------------------------------------------------
   Each panel is a small macOS-style window over the slide: draggable by its
   title bar, resizable from every edge and corner, with working traffic
   lights (close / minimize-to-titlebar / zoom) and remembered geometry. */

let floatingWindows = null;

function buildWindows() {
  const stage = $("stage-wrap");
  floatingWindows = window.Nd2FloatingWindows.createManager({
    stage, document,
    storage: {
      getItem: (key) => localStorage.getItem(key),
      setItem: (key, value) => localStorage.setItem(key, value),
    },
  });
  state.windows = {
    channels: makeMacWindow($("win-channels"), {
      key: "channels",
      def: () => ({ x: 14, y: 14, w: 256, h: null }),
      minW: 232,
      minH: 150,
      maxW: 600,
      zoomW: 480,
      toolbarBtn: $("tb-channels"),
      onResize: debounce(() => relayoutLuts(), 50),
    }),
    region: makeMacWindow($("win-region"), {
      key: "region",
      def: () => ({
        x: stage.clientWidth - 270,
        y: Math.max(14, stage.clientHeight - 300),
        w: 256,
        h: null,
      }),
      minW: 232,
      minH: 120,
      maxW: 420,
      zoomW: 340,
      toolbarBtn: $("tb-region"),
      startClosed: true, // a slide opens with the channels alone
    }),
    annot: makeMacWindow($("win-annot"), {
      key: "annot",
      def: () => ({
        x: stage.clientWidth - 270,
        y: 180,
        w: 256,
        h: null,
      }),
      minW: 232,
      minH: 150,
      maxW: 420,
      zoomW: 340,
      toolbarBtn: $("tb-annot"),
      startClosed: true,
    }),
    astromap: makeMacWindow($("win-astromap"), {
      key: "astromap",
      def: () => ({ x: Math.max(14, stage.clientWidth - 330), y: 68, w: 312, h: null }),
      minW: 280, minH: 180, maxW: 500, zoomW: 400, startClosed: true,
      onOpen: () => refreshAstromapPanel(),
    }),
    info: makeMacWindow($("win-info"), {
      key: "info",
      def: () => ({
        x: Math.max(14, stage.clientWidth - 414),
        y: 14,
        w: 400,
        h: null,
      }),
      minW: 340,
      minH: 220,
      maxW: 720,
      zoomW: 560,
      startClosed: true,
      onOpen: () => loadSlideInspector(),
    }),
  };
  window.addEventListener("resize", () => {
    Object.values(state.windows).forEach((w) => w.clampToStage());
  });
}

function makeMacWindow(el, opts) {
  return floatingWindows.createWindow(el, opts);
}

/* ---- plate mode ------------------------------------------------------------
   A time series of camera fields over sites and z planes (info.kind ===
   "plate"). Grid mode shows every site at the current t and z as a reduced
   frame in the stage arrangement; focus mode hands one site to the deep
   zoom viewer, whose tile URLs carry t, p and z through tileQuery(). A t or
   z change swaps the frames in place, the view never rebuilds. Nothing here
   runs for an ordinary slide. */

function siteLabel(name) {
  // "10(5)_MOI" -> {dil: "10", exp: "5", cond: "MOI"}; other names verbatim
  const m = /^(\d+)\((\d+)\)_(.+)$/.exec(String(name || ""));
  return m ? { dil: m[1], exp: m[2], cond: m[3] } : { dil: String(name || ""), exp: "", cond: "" };
}

function superscript(digits) {
  return String(digits).replace(/\d/g, (c) => "⁰¹²³⁴⁵⁶⁷⁸⁹"[Number(c)]);
}

function siteLabelText(name) {
  const l = siteLabel(name);
  return l.exp ? l.dil + superscript(l.exp) + " " + l.cond : l.dil;
}

function fillSitePill(pill, name) {
  pill.replaceChildren();
  const l = siteLabel(name);
  const dil = document.createElement("span");
  dil.className = "dil";
  dil.textContent = l.dil;
  if (l.exp) {
    const sup = document.createElement("sup");
    sup.textContent = l.exp;
    dil.append(sup);
  }
  pill.append(dil);
  if (l.cond) {
    const sep = document.createElement("span");
    sep.className = "sep";
    sep.textContent = "·";
    const cond = document.createElement("span");
    cond.className = "cond";
    cond.textContent = l.cond;
    pill.append(sep, cond);
  }
}

function fmtClock(ms) {
  // "0 H 30 M": whole minutes, hours first, as the time badge reads
  const total = Math.max(0, Math.round(Number(ms || 0) / 60000));
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h + " H " + String(m).padStart(2, "0") + " M";
}

function fmtPeriod(ms) {
  const total = Math.max(0, Math.round(Number(ms || 0) / 60000));
  if (total < 60) return total + " M";
  return fmtClock(ms);
}

function fmtTickLabel(min) {
  const h = Math.floor(min / 60);
  const m = Math.round(min - h * 60);
  if (h === 0) return m + " m";
  return m ? h + " h " + m + " m" : h + " h";
}

function platePeriodMs() {
  const pl = state.info.plate;
  if (Number(pl.periodMs) > 0) return Number(pl.periodMs);
  const times = pl.timesMs || [];
  if (times.length < 2) return null;
  const diffs = [];
  for (let i = 1; i < times.length; i++) diffs.push(times[i] - times[i - 1]);
  diffs.sort((a, b) => a - b);
  const mid = diffs[Math.floor(diffs.length / 2)];
  return mid > 0 ? mid : null;
}

function plateInspectionRows(plate) {
  const placed = (plate.sites || []).slice()
    .sort((a, b) => a.row - b.row || a.col - b.col);
  const names = placed.map((s) => siteLabelText(s.name)).join(", ");
  const rows = [
    ["Sites", plate.P + " · stage arrangement " + plate.cols + " × " + plate.rows +
      (names ? " · " + names : "")],
    ["Z", plate.Z + " planes" +
      (plate.zStepUm ? " · d = " + rawValueLabel(Number(plate.zStepUm)) + " µm" : "") +
      " · home " + (Number(plate.zHome) + 1)],
  ];
  const times = plate.timesMs || [];
  const period = platePeriodMs();
  rows.push(["Time", plate.T + " frames" +
    (times.length ? " · " + fmtClock(times[times.length - 1]) : "") +
    (period ? " · every " + fmtPeriod(period) : "")]);
  if (plate.exposureMs !== null && plate.exposureMs !== undefined) {
    rows.push(["Exposure", rawValueLabel(Number(plate.exposureMs)) + " ms"]);
  }
  return rows;
}

function platePlaneLabel() {
  const f = activeFrameParams();
  return f ? "T" + f.t + " · P" + f.p + " · Z" + f.z : platePlaneNote();
}

function currentFocusSummary() {
  const pl = state.plate;
  const sites = pl.focus === null ? state.info.plate.sites.map((s) => s.i) : [pl.focus];
  return window.Nd2PlateUI.focusSummary(pl.focusMap, pl.t, sites, pl.z, state.info.plate.Z, pl.auto);
}

function plateZText() {
  const pl = state.plate;
  const info = state.info.plate;
  const summary = currentFocusSummary();
  const range = summary.min === summary.max ? String(summary.min + 1) : (summary.min + 1) + "–" + (summary.max + 1);
  return "Z " + range + "/" + info.Z + (pl.auto
    ? " · AF " + summary.ready + "/" + summary.total + " ready" : "");
}

function platePlaneNote() {
  const pl = state.plate;
  const info = state.info.plate;
  const bits = [];
  if (pl.focus !== null && info.sites[pl.focus]) bits.push(siteLabelText(info.sites[pl.focus].name));
  bits.push("t " + (pl.t + 1) + "/" + info.T);
  bits.push(plateZText());
  return bits.join(" · ");
}

function plateFrameUrl(t, p, z, k) {
  const q = renderParams(new URLSearchParams({ k: k || state.plate.k }));
  return "api/plate/frame/" + t + "/" + p + "/" + z + ".jpg?" + q.toString();
}

function plateCellZoom() {
  // CSS px per image px of one grid cell; null before the first layout
  const pl = state.plate;
  return pl && pl.cellW ? pl.cellW / state.info.plate.frameW : null;
}

function fmtHm(ms) {
  // "6:30": hours and minutes, the way a scrubber reads
  const total = Math.max(0, Math.round(Number(ms || 0) / 60000));
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h + ":" + String(m).padStart(2, "0");
}

function fmtPeriodWords(ms) {
  const total = Math.max(0, Math.round(Number(ms || 0) / 60000));
  if (total < 60) return total + " min";
  const h = Math.floor(total / 60);
  const m = total % 60;
  return m ? h + " h " + m + " min" : h + " h";
}

function plateArrange() {
  // rows and columns of the grid. The stage arrangement puts dilutions in
  // rows; transposed, the conditions become the rows, which reads better
  // in a landscape window and is the default. The choice is remembered.
  const pl = state.plate;
  const info = state.info.plate;
  const tr = pl.transposed;
  pl.rows = Math.max(1, tr ? info.cols : info.rows);
  pl.cols = Math.max(1, tr ? info.rows : info.cols);
  pl.placed = (info.sites || [])
    .map((s) => ({ ...s, row: tr ? s.col : s.row, col: tr ? s.row : s.col }))
    .sort((a, b) => a.row - b.row || a.col - b.col);
  const assay = pl.placed.length > 0 &&
    pl.placed.every((s) => { const l = siteLabel(s.name); return l.exp && l.cond; });
  const plateUI = window.Nd2PlateUI;
  pl.wellHeaders = plateUI && typeof plateUI.wellHeaders === "function"
    ? plateUI.wellHeaders(pl.placed, pl.rows, pl.cols)
    : null;
  pl.headerKind = pl.wellHeaders ? "well" : assay ? "assay" : null;
  pl.structured = !!pl.headerKind;
  if (pl.view.siteLabels === null) pl.view.siteLabels = pl.headerKind !== "well";
}

function dilNode(l) {
  const span = document.createElement("span");
  span.textContent = l.dil;
  if (l.exp) {
    const sup = document.createElement("sup");
    sup.textContent = l.exp;
    span.append(sup);
  }
  return span;
}

function placePlate() {
  const pl = state.plate;
  const block = $("plate-block");
  block.style.setProperty("--cols", pl.cols);
  block.style.setProperty("--rows", pl.rows);
  const grid = $("plate-grid");
  grid.setAttribute("aria-rowcount", String(pl.rows));
  grid.setAttribute("aria-colcount", String(pl.cols));
  const rows = Array.from({ length: pl.rows }, (_, i) => {
    const row = document.createElement("div");
    row.className = "plate-accessible-row";
    row.setAttribute("role", "row");
    row.setAttribute("aria-rowindex", String(i + 1));
    return row;
  });
  if (!pl.placed.some((s) => s.i === pl.gridActive)) pl.gridActive = pl.placed[0]?.i;
  pl.placed.forEach((s) => {
    const el = pl.gridEls[s.i];
    el.style.gridRow = s.row + 1;
    el.style.gridColumn = s.col + 1;
    el.setAttribute("role", "gridcell");
    el.setAttribute("aria-rowindex", String(s.row + 1));
    el.setAttribute("aria-colindex", String(s.col + 1));
    el.tabIndex = s.i === pl.gridActive ? 0 : -1;
    rows[s.row].append(el);
  });
  grid.replaceChildren(...rows);
  const rowHeads = $("plate-row-heads");
  const colHeads = $("plate-col-heads");
  rowHeads.replaceChildren();
  colHeads.replaceChildren();
  block.classList.toggle("headed", pl.structured);
  if (pl.headerKind === "well") {
    pl.wellHeaders.rows.forEach((label) => {
      const head = document.createElement("span");
      head.textContent = label;
      rowHeads.append(head);
    });
    pl.wellHeaders.cols.forEach((label) => {
      const head = document.createElement("span");
      head.textContent = label;
      colHeads.append(head);
    });
  } else if (pl.headerKind === "assay") {
    for (let r = 0; r < pl.rows; r++) {
      const s = pl.placed.find((x) => x.row === r);
      const l = siteLabel(s ? s.name : "");
      const head = document.createElement("span");
      if (pl.transposed) head.textContent = l.cond; else head.append(dilNode(l));
      rowHeads.append(head);
    }
    for (let c = 0; c < pl.cols; c++) {
      const s = pl.placed.find((x) => x.col === c);
      const l = siteLabel(s ? s.name : "");
      const head = document.createElement("span");
      if (pl.transposed) head.append(dilNode(l)); else head.textContent = l.cond;
      colHeads.append(head);
    }
  }
  const btn = $("plate-transpose");
  btn.classList.toggle("on", pl.transposed);
  btn.setAttribute("aria-pressed", String(pl.transposed));
}

function positionPlateViewMenu() {
  const btn = $("tb-plate-view");
  const menu = $("plate-view-menu");
  if (!btn || !menu || menu.hidden) return;
  const r = btn.getBoundingClientRect();
  const width = menu.offsetWidth || 184;
  const height = menu.offsetHeight || 96;
  menu.style.left = Math.max(8, Math.min(window.innerWidth - width - 8, r.right - width)) + "px";
  menu.style.top = Math.max(8, Math.min(window.innerHeight - height - 8, r.bottom + 6)) + "px";
}

function setPlateViewMenuOpen(open) {
  const btn = $("tb-plate-view");
  const menu = $("plate-view-menu");
  if (!btn || !menu) return;
  const shown = !!open && !btn.hidden;
  menu.hidden = !shown;
  btn.classList.toggle("active", shown);
  btn.setAttribute("aria-expanded", String(shown));
  if (shown) {
    positionPlateViewMenu();
    const items = [...menu.querySelectorAll("[data-plate-view]")];
    items.forEach((item, i) => { item.tabIndex = i === 0 ? 0 : -1; });
    const first = items[0];
    if (first) first.focus({ preventScroll: true });
  }
}

function renderPlateViewMenu() {
  const pl = state.plate;
  if (!pl) return;
  $("plate-view-menu").querySelectorAll("[data-plate-view]").forEach((item) => {
    item.setAttribute("aria-checked", String(!!pl.view[item.dataset.plateView]));
  });
}

function applyPlateView(relayout = true) {
  const pl = state.plate;
  if (!pl) return;
  const wrap = $("stage-wrap");
  wrap.classList.toggle("plate-labels-hidden", !pl.view.siteLabels);
  wrap.classList.toggle("plate-time-hidden", !pl.view.timeline);
  wrap.classList.toggle("plate-z-hidden", !pl.view.zAxis);
  $("time-line").hidden = !pl.view.timeline;
  $("z-slider").hidden = !pl.view.zAxis;
  renderPlateViewMenu();
  if (relayout) layoutPlate();
}

function wirePlateViewMenu() {
  const pl = state.plate;
  const btn = $("tb-plate-view");
  const menu = $("plate-view-menu");
  if (!pl || !btn || !menu) return;
  btn.hidden = false;
  btn.onclick = () => setPlateViewMenuOpen(menu.hidden);
  menu.querySelectorAll("[data-plate-view]").forEach((item) => {
    item.onclick = () => {
      const key = item.dataset.plateView;
      pl.view[key] = !pl.view[key];
      applyPlateView();
    };
  });
  menu.addEventListener("keydown", (ev) => {
    const items = [...menu.querySelectorAll("[data-plate-view]")];
    const current = Math.max(0, items.indexOf(document.activeElement));
    let next = null;
    if (ev.key === "ArrowDown") next = (current + 1) % items.length;
    else if (ev.key === "ArrowUp") next = (current + items.length - 1) % items.length;
    else if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = items.length - 1;
    if (next === null) return;
    ev.preventDefault();
    items.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; });
    items[next].focus({ preventScroll: true });
  });
  menu.addEventListener("focusout", (ev) => {
    if (!menu.contains(ev.relatedTarget) && ev.relatedTarget !== btn) {
      setPlateViewMenuOpen(false);
    }
  });
  document.addEventListener("pointerdown", (ev) => {
    if (!menu.hidden && !menu.contains(ev.target) && ev.target !== btn && !btn.contains(ev.target)) {
      setPlateViewMenuOpen(false);
    }
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape" || menu.hidden) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    setPlateViewMenuOpen(false);
    btn.focus({ preventScroll: true });
  });
  window.addEventListener("resize", positionPlateViewMenu);
  window.addEventListener("blur", () => setPlateViewMenuOpen(false));
  renderPlateViewMenu();
}

function buildPlate() {
  const pl = state.plate;
  wireNativeGestureScope();
  if (!pl) return;
  const info = state.info.plate;
  const wrap = $("stage-wrap");
  wrap.classList.add(pl.focus === null ? "plate-grid" : "plate-focus");
  wrap.classList.toggle("plate-single", info.P === 1);

  try {
    const saved = localStorage.getItem("nd2wsi.plate.transposed");
    pl.transposed = saved === null ? true : saved === "1";
  } catch (_) { pl.transposed = true; }
  try {
    pl.auto = info.Z > 1 && localStorage.getItem("nd2wsi.plate.autofocus") === "1";
  } catch (_) { pl.auto = false; }
  plateArrange();
  wirePlateViewMenu();
  applyPlateView(false);

  const grid = $("plate-grid");
  const strip = $("plate-strip");
  pl.gridEls = [];
  pl.stripEls = [];
  (info.sites || []).forEach((site) => {
    const cell = makeSiteEl(site);
    grid.append(cell);
    pl.gridEls[site.i] = cell;
    const small = makeSiteEl(site);
    strip.append(small);
    pl.stripEls[site.i] = small;
  });
  placePlate();
  grid.addEventListener("keydown", (ev) => {
    if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.isComposing) return;
    const cell = ev.target.closest(".site");
    if (!cell) return;
    const next = window.Nd2PlateUI.nextGridSite(pl.placed, Number(cell.dataset.p), ev.key);
    if (next === null) return;
    ev.preventDefault();
    ev.stopPropagation();
    pl.gridActive = next;
    pl.gridEls.forEach((el, p) => { el.tabIndex = p === next ? 0 : -1; });
    pl.gridEls[next].focus({ preventScroll: true });
  });
  $("plate-back").onclick = () => setPlateFocus(null);
  $("plate-transpose").onclick = () => {
    pl.transposed = !pl.transposed;
    try { localStorage.setItem("nd2wsi.plate.transposed", pl.transposed ? "1" : "0"); } catch (_) { /* private mode */ }
    plateArrange();
    placePlate();
    layoutPlate();
  };

  buildZSlider();
  buildTimeLine();
  wirePlateWheel();
  wirePlateKeys();
  $("t-auto").addEventListener("click", () => setPlateAuto(!state.plate.auto));
  renderPlateAuto();
  if (info.Z > 1) loadPlateFocus();

  window.addEventListener("resize", layoutPlate);
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => layoutPlate()).observe($("plate"));
  }
  layoutPlate();
  paintPlate();
  renderZSlider();
  renderTimeLine();
  $("plane-note").textContent = platePlaneNote();
}

function makeSiteEl(site) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = "site pending";
  el.dataset.p = site.i;
  el.title = siteLabelText(site.name);
  el.setAttribute("aria-label", siteLabelText(site.name));
  const img = document.createElement("img");
  img.alt = "";
  img.draggable = false;
  img.addEventListener("load", () => markFrameLoaded(img));
  img.addEventListener("error", () => {
    // a frame the server could not read would otherwise leave the cell
    // in its placeholder for good, with nothing said
    const cell = img.closest(".site");
    if (!cell || img.dataset.frame !== cell.dataset.shown) return;
    cell.classList.remove("pending");
    cell.classList.add("failed");
    showToast("A frame of " + siteLabelText(site.name) + " could not be read");
  });
  const pill = document.createElement("span");
  pill.className = "pill";
  fillSitePill(pill, site.name);
  el.append(img, pill);
  el.addEventListener("click", () => setPlateFocus(Number(el.dataset.p)));
  return el;
}

function markFrameLoaded(img) {
  // the cell drops its placeholder on the first frame; a time tick fills in
  // once every site's frame at that t and z has arrived
  const pl = state.plate;
  const cell = img.closest(".site");
  if (cell) cell.classList.remove("pending");
  const key = img.dataset.frame;
  const tz = img.dataset.group;
  if (!key || !tz) return;
  // the set counts sites, not frames: with autofocus a site whose plane
  // moved would otherwise count twice and fill the band on its own
  const seen = (pl.loaded.get(tz) || new Set());
  seen.add(key.split("/")[1]);
  pl.loaded.set(tz, seen);
  if (seen.size >= state.info.plate.P && tz.endsWith("/" + plateGroupKey())) renderTimeLine();
}

function layoutPlate() {
  const pl = state.plate;
  if (!pl) return;
  const info = state.info.plate;
  const stage = $("stage-wrap").getBoundingClientRect();
  const r = $("plate").getBoundingClientRect();
  if (!r.width || !r.height) return;
  const gap = 12;
  // The first column holds either headings or the 28 px transpose button;
  // the first row always holds that button. Account for the actual chrome so
  // a dense 16 x 16 grid never clips at the bottom.
  const leadingW = (pl.structured ? 44 : 28) + gap;
  const topChromeH = 28 + 10;
  const sliderW = pl.view.zAxis ? 44 + gap : 0;
  const aspect = info.frameW && info.frameH ? info.frameW / info.frameH : 1;
  const availW = r.width - leadingW - sliderW;
  const availH = r.height - topChromeH;
  const cellW = Math.floor(Math.min(
    (availW - gap * (pl.cols - 1)) / pl.cols,
    ((availH - gap * (pl.rows - 1)) / pl.rows) * aspect
  ));
  const cellH = Math.floor(cellW / aspect);
  if (cellW < 8 || cellH < 8) return;
  pl.cellW = cellW;
  pl.cellH = cellH;
  const block = $("plate-block");
  block.style.setProperty("--cell-w", cellW + "px");
  block.style.setProperty("--cell-h", cellH + "px");
  const gridW = cellW * pl.cols + gap * (pl.cols - 1);
  const gridH = cellH * pl.rows + gap * (pl.rows - 1);
  block.style.setProperty("--grid-h", gridH + "px");
  const capsule = Math.max(420, Math.min(stage.width - 32, Math.max(gridW + 140, 760)));
  $("plate-stage").style.setProperty("--capsule-w", capsule + "px");
  const k = cellW <= 260 ? 8 : 4;
  if (k !== pl.k) {
    pl.k = k;
    paintPlate();
  }
  renderZSlider();
  if (pl.focus === null) updateReadout();
}

function paintPlate() {
  // the 8x reduction comes from the store beside the file, so a scrub
  // shows every site at once; a wide cell then gets the sharper 4x pass
  // once the hand rests, the way a photo app sharpens after a swipe
  const pl = state.plate;
  if (!pl || !pl.gridEls) return;
  const sites = state.info.plate.sites || [];
  const quick = pl.k === 8 ? null : 8;
  const group = pl.t + "/" + plateGroupKey();
  sites.forEach((site) => {
    const z = plateZFor(site.i);
    const ready = window.Nd2PlateUI.focusReady(pl.focusMap, pl.t, site.i);
    const detail = siteLabelText(site.name) + " · Z " + (z + 1) + "/" + state.info.plate.Z
      + (pl.auto ? ready ? " · Autofocus" : " · Manual, autofocus pending" : "");
    [pl.gridEls[site.i], pl.stripEls[site.i]].forEach((cell) => {
      cell.setAttribute("aria-label", detail);
      cell.title = detail;
    });
    const url = plateFrameUrl(pl.t, site.i, z, quick || pl.k);
    // only the set on screen is loaded: the grid in the overview, the strip
    // in focus. Loading both doubled every step's requests, half of them
    // into elements the stylesheet has hidden
    const shown = pl.focus === null ? pl.gridEls[site.i] : pl.stripEls[site.i];
    const img = shown.querySelector("img");
    img.dataset.frame = pl.t + "/" + site.i + "/" + z;
    img.dataset.group = group;
    shown.dataset.shown = img.dataset.frame;
    shown.classList.remove("failed");
    if (img.getAttribute("src") !== url) img.src = url;
    pl.stripEls[site.i].classList.toggle("current", pl.focus === site.i);
    pl.gridEls[site.i].classList.toggle("current", pl.focus === site.i);
  });
  clearTimeout(pl.sharpenTimer);
  pl.sharpenTimer = null;
  if (quick && pl.focus === null) {
    const t = pl.t, z = pl.z, auto = pl.auto;
    pl.sharpenTimer = setTimeout(() => {
      pl.sharpenTimer = null;
      if (pl.t !== t || pl.z !== z || pl.auto !== auto || pl.focus !== null) return;
      sites.forEach((site) => {
        const img = pl.gridEls[site.i].querySelector("img");
        const url = plateFrameUrl(t, site.i, plateZFor(site.i), pl.k);
        if (img.getAttribute("src") !== url) img.src = url;
      });
    }, 350);
  }
}

function plateFrameChanged() {
  // every t, z or focus change: frames, readouts, histogram, status bar
  invalidatePixelProbe(true);
  if (state.roi) {
    if (frameOwnsRoi()) updateScaleStrip();
    else clearRoi();
  }
  refreshTiles();
  renderZSlider();
  renderTimeLine();
  refreshHistograms();
  syncFrameScopedControls();
  $("plane-note").textContent = platePlaneNote();
  if (state.inspect && !state.windows.info.isHidden()) renderSlideInspector(state.inspect);
}

function setPlateFocus(p) {
  const pl = state.plate;
  const info = state.info.plate;
  const numeric = p === null || p === undefined ? null : Number(p);
  if (numeric !== null && !Number.isInteger(numeric)) return;
  const next = info.P === 1
    ? 0
    : numeric === null ? null : clamp(numeric, 0, info.P - 1);
  if (next === pl.focus) return;
  // an open text edit belongs to the site being left, so it is committed
  // first. The debounced save is then called off, because it would fire
  // after the switch and write these marks into the next site's sidecar
  if (state.editingId) closeEditor(true);
  if (!annotationStore.saveDeparting()) return;
  if (state.tool) setTool(state.tool); // every tool belongs to the site being left
  if (state.roi) clearRoi();
  annotationStore.resetOwner();
  state.viewportRelay.contextChanging = true;
  pl.focus = next;
  refreshSpatialContext(); // invalidate old P commands before any asynchronous work
  const wrap = $("stage-wrap");
  wrap.classList.toggle("plate-grid", next === null);
  wrap.classList.toggle("plate-focus", next !== null);
  if (next !== null) {
    if (!pl.everFocused) {
      pl.everFocused = true;
      state.viewer.viewport.goHome(true);
    }
    if (state.viewer.navigator && typeof state.viewer.navigator.update === "function") {
      state.viewer.navigator.update(state.viewer.viewport);
    }
  } else {
    setPlatePlaying(false);
    layoutPlate();
  }
  loadAnnotations(next);
  if (pl.resetWheel) pl.resetWheel(); // the two levels do not share a stroke
  paintPlate(); // the set that just became visible carries older frames
  plateFrameChanged();
  updateReadout();
  state.viewportRelay.contextChanging = false;
  postSpatialReadiness();
}

const NATIVE_GESTURE_EXCLUSIONS = "#time-line, #plate-strip, #plate-back, .mac-window, #plate-view-menu, #ann-editor, #ann-recovery, #z-slider, #plate-transpose, #zoom-cluster";

function wireNativeGestureScope() {
  let pending = false;
  let previous = "";
  let closed = false;
  const rect = (el) => {
    if (!el || !el.getClientRects().length) return null;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return null;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 ? { left: r.left, top: r.top, right: r.right, bottom: r.bottom } : null;
  };
  const report = () => {
    if (pending || closed) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (closed) return;
      const selectors = NATIVE_GESTURE_EXCLUSIONS + (state.plate?.focus !== null ? ", #plate-block" : "");
      const stageRect = rect($("stage-wrap"));
      const channelBody = rect(document.querySelector("#win-channels .win-body"));
      const lutRects = channelBody ? [...document.querySelectorAll(".lut-canvas")].map(rect)
        .filter(Boolean).map((r) => window.Nd2NativeScope.rect({
          left: Math.max(r.left, channelBody.left), top: Math.max(r.top, channelBody.top),
          right: Math.min(r.right, channelBody.right), bottom: Math.min(r.bottom, channelBody.bottom),
        })).filter(Boolean) : [];
      const plateEnabled = !!state.plate && Number(state.info?.plate?.T) > 1;
      let exclusions = plateEnabled ? [] : window.Nd2NativeScope.subtractRects(stageRect, lutRects);
      for (const element of document.querySelectorAll(selectors)) {
        const bounds = rect(element);
        if (!bounds) continue;
        exclusions.push(...(element.id === "win-channels"
          ? window.Nd2NativeScope.subtractRects(bounds, lutRects) : [bounds]));
      }
      const payload = {
        nd2wsi: "native-gesture-scope", version: VIEWPORT_PROTOCOL_VERSION,
        enabled: plateEnabled || lutRects.length > 0, include: stageRect,
        exclude: exclusions,
      };
      const key = JSON.stringify(payload);
      if (key !== previous) { previous = key; window.parent.postMessage(payload, location.origin); }
    });
  };
  // Inspector moves and View/focus changes are DOM style/class changes.
  const observer = new MutationObserver(report);
  const observed = [...document.querySelectorAll(NATIVE_GESTURE_EXCLUSIONS + ", #stage-wrap, #plate, #plate-block")];
  observed.forEach((el) => observer.observe(el, { attributes: true, attributeFilter: ["style", "class", "hidden"] }));
  const sizes = typeof ResizeObserver === "function" ? new ResizeObserver(report) : null;
  observed.forEach((el) => sizes?.observe(el));
  window.addEventListener("resize", report);
  document.addEventListener("scroll", report, true);
  window.addEventListener("pagehide", () => {
    closed = true;
    state.plate?.focusRequest?.abort();
    observer.disconnect();
    sizes?.disconnect();
    window.removeEventListener("resize", report);
    document.removeEventListener("scroll", report, true);
    window.parent.postMessage({ nd2wsi: "native-gesture-scope", version: VIEWPORT_PROTOCOL_VERSION, enabled: false }, location.origin);
  }, { once: true });
  report();
}

function wirePlateWheel() {
  // two levels. Over the grid a vertical scroll turns the z knob and a
  // horizontal swipe scrubs time. Over a focused site the vertical scroll
  // belongs to the deep zoom, a horizontal swipe still scrubs time, and
  // option with a vertical scroll moves z.
  const pl = state.plate;
  const GestureSession = window.Nd2AxisLatch && window.Nd2AxisLatch.WheelGestureSession;
  if (!GestureSession) {
    console.error("Plate gestures are unavailable: axis latch did not load");
    return;
  }
  const gridGesture = new GestureSession({ mode: "grid", idleMs: 100 });
  const focusGesture = new GestureSession({ mode: "focus", idleMs: 100 });
  // Native horizontal input has already passed an AppKit axis latch. Give its
  // first movement a short threshold so a deliberate, modest swipe advances
  // immediately instead of needing a full scroll-wheel notch.
  const nativeGridGesture = new GestureSession({ mode: "grid", idleMs: 100, threshold: 0.05, timeStartStep: 0.05 });
  const nativeFocusGesture = new GestureSession({ mode: "focus", idleMs: 100, threshold: 0.05, timeStartStep: 0.05 });
  const handle = (input, gesture, ev = null) => {
    const result = gesture.feed({
      deltaX: input.deltaX,
      deltaY: input.deltaY,
      deltaMode: input.deltaMode,
      pagePixels: $("stage-wrap").clientHeight || window.innerHeight || 800,
      altKey: input.altKey,
      at: performance.now(),
    });
    if (result.timeSteps) setPlateT(pl.t + result.timeSteps);
    if (result.zSteps) stepPlateZ(result.zSteps);
    if (result.consume && ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    return result.consume;
  };
  const route = (target, input, gestures, ev = null) => {
    if (!(target instanceof Element) || !$("stage-wrap").contains(target)) return false;
    if (target.closest(NATIVE_GESTURE_EXCLUSIONS)) return false;
    if (pl.focus === null) return handle(input, gestures.grid, ev);
    if (target.closest("#plate-block")) return false;
    return handle(input, gestures.focus, ev);
  };
  pl.resetWheel = () => {
    gridGesture.reset();
    focusGesture.reset();
    nativeGridGesture.reset();
    nativeFocusGesture.reset();
  };
  $("stage-wrap").addEventListener("wheel", (ev) => {
    route(ev.target, ev, { grid: gridGesture, focus: focusGesture }, ev);
  }, { passive: false, capture: true });

  const onNativeTrackpad = (ev) => {
    if (ev.origin !== location.origin || ev.source !== window.parent) return;
    const data = ev.data;
    if (!data || data.nd2wsi !== "native-trackpad" || data.version !== VIEWPORT_PROTOCOL_VERSION) return;
    const x = Number(data.clientX);
    const y = Number(data.clientY);
    const target = Number.isFinite(x) && Number.isFinite(y)
      ? document.elementFromPoint(x, y)
      : $("plate");
    if (data.gestureStart) {
      nativeGridGesture.reset();
      nativeFocusGesture.reset();
    }
    route(target, {
      deltaX: Number(data.deltaX) || 0,
      deltaY: 0,
      deltaMode: 0,
      altKey: !!data.altKey,
    }, { grid: nativeGridGesture, focus: nativeFocusGesture });
  };
  window.addEventListener("message", onNativeTrackpad);
  window.addEventListener("pagehide", () => {
    window.removeEventListener("message", onNativeTrackpad);
  }, { once: true });
}

function wirePlateKeys() {
  // arrows step z and t, space plays, digits open a site. Capture phase so
  // OpenSeadragon's own arrow panning never sees the keys; text fields, the
  // command keys, alignment and a linked compare (whose arrows nudge) keep
  // their meaning.
  window.addEventListener("keydown", (ev) => {
    const pl = state.plate;
    if (!pl) return;
    const shortcuts = window.Nd2ShortcutRouter;
    if (shortcuts && shortcuts.isTypingEvent(ev)) return;
    if (!shortcuts && /^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    if (ev.target.closest?.("#tb-plate-view, #plate-view-menu")) return;
    if (ev.target.closest?.("#plate-grid .site, #t-track, #z-knob")) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (state.landmark.active) return;
    const compare = state.viewportRelay.compare;
    if (compare && compare.enabled && compare.frameLinked) return;
    const info = state.info.plate;
    let handled = true;
    if (ev.key === "ArrowUp" && info.Z > 1) stepPlateZ(1);
    else if (ev.key === "ArrowDown" && info.Z > 1) stepPlateZ(-1);
    else if (ev.key === "ArrowLeft" && info.T > 1) setPlateT(pl.t - 1);
    else if (ev.key === "ArrowRight" && info.T > 1) setPlateT(pl.t + 1);
    else if (ev.key === " " && info.T > 1) {
      // a focused transport button would fire its own click on the keyup
      // and undo the toggle, so the key takes the focus away first
      if (ev.target && ev.target !== document.body && typeof ev.target.blur === "function") {
        ev.target.blur();
      }
      if (!ev.repeat) setPlatePlaying(!pl.playing);
    }
    else if ((shortcuts ? shortcuts.letterCode(ev) === "KeyF" : ev.code === "KeyF") &&
        !ev.shiftKey && info.Z > 1) {
      if (!ev.repeat && !$("t-auto").disabled) setPlateAuto(!pl.auto);
    }
    else if (/^[1-9]$/.test(ev.key) && !ev.shiftKey) {
      const site = pl.placed[Number(ev.key) - 1];
      if (site) setPlateFocus(site.i);
      else handled = false;
    } else handled = false;
    if (handled) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }, true);
}

/* ---- keys / misc ---------------------------------------------------------- */

function wireDragForward() {
  // a Finder drag that starts over the slide lands in this iframe's
  // document; hand it to the shell, whose overlay (and the app's Python
  // drop handler) can actually open the file
  window.addEventListener("dragenter", (ev) => {
    const types = ev.dataTransfer ? Array.from(ev.dataTransfer.types || []) : [];
    if (types.includes("Files") && window.parent !== window) {
      window.parent.postMessage({ nd2wsi: "file-drag" }, location.origin);
    }
  });
  window.addEventListener("dragover", (ev) => ev.preventDefault());
  window.addEventListener("drop", (ev) => ev.preventDefault());
  document.addEventListener("dragover", (ev) => ev.preventDefault(), true);
  document.addEventListener("drop", (ev) => ev.preventDefault(), true);
}

function applyOrientationShortcut(action) {
  if (!["rotate-right", "flip-horizontal"].includes(action) || state.quitPreparing) return false;
  refreshSpatialContext();
  const relay = state.viewportRelay;
  if (relay.compare?.enabled && window.parent !== window) {
    if (!spatialPaneReady()) {
      showToast("Focus a ready site in every linked pane before changing orientation");
      return false;
    }
    // The shell owns alignment and the Active linked slide selection. Never
    // rotate this viewport behind a committed fit or independently of its pair.
    window.parent.postMessage({
      nd2wsi: "compare-orientation-shortcut", version: VIEWPORT_PROTOCOL_VERSION,
      sid: currentSlideSid(), action, ...spatialIdentity(),
    }, location.origin);
    return true;
  }
  if (!relay.commandGate.spatialContext || !spatialImageReady()) {
    showToast(state.plate?.focus === null
      ? "Open a site before rotating or flipping the image"
      : "Wait for the image to finish opening");
    return false;
  }
  if (action === "flip-horizontal") relay.displayFlipped = !relay.displayFlipped;
  else {
    // OSD displays F * R(theta): a clockwise screen turn reverses the
    // rotation increment when the viewport is already reflected.
    relay.displayRotation = normalizedRotation(relay.displayRotation +
      (relay.displayFlipped ? -90 : 90));
  }
  return applyDesiredDisplayTransform();
}

function togglePanelForShortcut(panel) {
  if (state.quitPreparing) return false;
  if (panel === "info") {
    const inspector = state.windows?.info;
    if (!inspector) return false;
    if (inspector.isHidden()) inspector.open();
    else inspector.close(false);
    return true;
  }
  const buttons = { channels: "tb-channels", region: "tb-region", annot: "tb-annot" };
  const button = Object.hasOwn(buttons, panel) ? $(buttons[panel]) : null;
  if (!button || button.disabled) return false;
  button.click();
  return true;
}

function wireKeys() {
  window.addEventListener("keydown", (ev) => {
    const shortcuts = window.Nd2ShortcutRouter;
    if (ev.defaultPrevented) return;
    if (shortcuts && !shortcuts.isMac() && shortcuts.isNewWindowShortcut(ev) &&
        window.parent !== window && window.parent.pywebview?.api?.new_window) {
      const role = shortcuts.newWindowRoleForEvent(ev);
      ev.preventDefault(); ev.stopPropagation();
      if (role) window.parent.postMessage({ nd2wsi: "new-window-shortcut",
        version: VIEWPORT_PROTOCOL_VERSION, sid: currentSlideSid(), role,
        ...spatialIdentity() }, location.origin);
      return;
    }
    if (shortcuts && shortcuts.isTypingEvent(ev)) return;
    if (!shortcuts && /^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    if (ev.target.closest?.("#tb-plate-view, #plate-view-menu")) return;
    const command = ev.metaKey || ev.ctrlKey;
    const letterCode = shortcuts ? shortcuts.letterCode(ev) : ev.code;
    if (shortcuts?.isAstromapShortcut(ev)) {
      ev.preventDefault();
      ev.stopPropagation();
      if (!ev.repeat) requestAstromap();
      return;
    }
    if (shortcuts?.isOrientationShortcut(ev)) {
      const action = shortcuts.orientationForEvent(ev);
      ev.preventDefault(); // reserve Reload/Find, including held-key repeats
      ev.stopPropagation();
      if (action) applyOrientationShortcut(action);
      return;
    }
    if (command && !ev.shiftKey && ev.code === "Backslash") {
      if (window.parent !== window && !ev.repeat) {
        ev.preventDefault();
        window.parent.postMessage(
          { nd2wsi: "compare-toggle", version: VIEWPORT_PROTOCOL_VERSION },
          location.origin
        );
      }
      return;
    }
    if (command && ev.shiftKey && letterCode === "KeyE") {
      ev.preventDefault();
      $("ann-geojson").click();
      return;
    }
    const tabIndex = shortcuts ? shortcuts.tabIndexForEvent(ev) : null;
    if (tabIndex !== null) {
      // ⌘1 to ⌘9 pick a tab; the shell owns the tabs
      if (window.parent !== window && tabIndex < state.tabCount) {
        ev.preventDefault();
        window.parent.postMessage(
          { nd2wsi: "tab-select", version: VIEWPORT_PROTOCOL_VERSION, index: tabIndex },
          location.origin
        );
      }
      return;
    }
    const panel = shortcuts ? shortcuts.panelForEvent(ev) : null;
    if (command) {
      if (shortcuts?.isPanelShortcut(ev)) {
        ev.preventDefault();
        ev.stopPropagation();
        if (panel) togglePanelForShortcut(panel);
      }
      return; // preserve native copy/select-all when the router yields ownership
    }
    // Own these letters before the target OSD canvas sees them. In particular
    // OSD's R rotates, A pans and F flips before a bubble listener can react.
    // Plain C/E/A toggle panels before OSD can use A to pan. R remains
    // reserved so it cannot unexpectedly rotate the image.
    // Repeats/Shift variants stay reserved but do not repeatedly toggle tools.
    const ownedLetter = !ev.altKey &&
      ["KeyC", "KeyR", "KeyA", "KeyE", "KeyV", "KeyM", "KeyP", "KeyB", "KeyI", "KeyF"].includes(letterCode);
    if (ownedLetter) {
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.repeat || ev.shiftKey) return;
      if (panel) {
        togglePanelForShortcut(panel);
      } else if (letterCode === "KeyV") { if (state.roi) setTool("move"); }
      else if (letterCode === "KeyM") setTool("measure");
      else if (letterCode === "KeyP") setTool("pin");
      else if (letterCode === "KeyB") setTool("box");
      else if (letterCode === "KeyI") togglePixelInspector();
      // Plain F is reserved for plate autofocus, handled by wirePlateKeys in
      // an earlier capture listener. It must never fall through to OSD flip.
      return;
    }
    const plain = !ev.altKey && !ev.shiftKey && !ev.repeat;
    if (plain && letterCode === "KeyL" && window.parent !== window) {
      if (!ev.repeat) {
        ev.preventDefault();
        ev.stopPropagation();
        if (!spatialPaneReady()) return;
        window.parent.postMessage(
          { nd2wsi: "compare-link-toggle", version: VIEWPORT_PROTOCOL_VERSION,
            sid: currentSlideSid(), ...spatialIdentity() },
          location.origin
        );
      }
    }
    else if (ev.key === "Escape") {
      if (!$("ann-editor").hidden) closeEditor(false);
      else if (state.tool) setTool(state.tool); // toggles off
      else if (state.plate && state.plate.focus !== null) setPlateFocus(null);
    } else if (plain && ["Digit0", "Numpad0"].includes(ev.code)) {
      ev.preventDefault();
      ev.stopPropagation();
      state.viewer.viewport.goHome();
    }
  }, true);
}

function elementPoint(ev, el) {
  const r = el.getBoundingClientRect();
  return { x: ev.clientX - r.left, y: ev.clientY - r.top };
}
function fmtUm(um) {
  if (um >= 10000) return (um / 1000).toFixed(2) + " mm";
  if (um >= 100) return Math.round(um) + " µm";
  return um.toFixed(1) + " µm";
}
function fmtBytes(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + u[i];
}
function dtypeBytes(dt) {
  const m = /(\d+)$/.exec(dt);
  return m ? Math.max(1, parseInt(m[1], 10) / 8) : 1;
}

let toastTimer = null;
function showToast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3600);
}
function debounceToast(msg) {
  let last = 0;
  return () => {
    const now = Date.now();
    if (now - last > 5000) { last = now; showToast(msg); }
  };
}
