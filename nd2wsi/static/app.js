/* nd2wsi viewer ------------------------------------------------------------ */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  info: null,
  viewer: null,
  channels: [], // enabled channel indices
  luts: [], // per channel {lo, hi, gamma}; null while at store defaults
  lutWidgets: [], // canvas LUT widgets, aligned with luts
  roi: null, // {x, y, w, h} in level-0 pixels
  roiOverlayEl: null,
  tool: null, // null | "roi" | "measure" | "pin" | "box"
  renderScale: null, // chosen PNG/JPEG downsample; null = finest that fits
  annotations: [], // {id, type: "line"|"pin"|"box", ...image coords, text}
  annPath: null, // sidecar file, reported by the server
  editingId: null,
  tempLine: null, // live ruler while dragging
  windows: null, // floating mac-window controllers
  inspect: null, // extended slide metadata, loaded when Slide Info first opens
  inspectLoading: false,
  plate: null, // {t, z, focus, playing, fps, k, ...} for a time series of sites
  annDirty: false, // an edit is waiting for the debounced save
  annRevision: 0,
  annContext: 0, // changes when a plate switches to another site's sidecar
  annSaveTail: Promise.resolve(),
  annFailedSaves: new Map(), // URL -> latest captured payload that still needs retry
  quitPreparing: false,
  annLoadSeq: 0, // only the newest sidecar load may install its items
  tabCount: 0, // supplied by the same-origin shell for conditional Cmd+digit handling
  pixel: {
    hudVisible: false,
    cursor: null, // latest displayed-base coordinate and element position
    result: null,
    queued: null,
    inFlight: false,
    timer: null,
    lastStarted: 0,
    retryAfter: 0,
    failed: false,
  },
  viewportRelay: {
    seq: 0,
    timer: null,
    suppressUntil: 0,
    reopening: false,
    displayRotation: 0,
    displayFlipped: false,
    transformWaitingForOpen: false,
    altHeld: false, // Option held: a drag adjusts the alignment instead of both views
    compare: null, // what the shell says about this pane's part in Compare
  },
  landmark: {
    active: false, // the shell asked for alignment points on this pane
    points: [], // level-0 image coordinates, in placement order
    needed: 4,
    clickToZoom: null, // OpenSeadragon setting restored when the mode ends
  },
};

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

  if (info.kind === "plate" && info.plate) {
    state.plate = {
      t: 0,
      z: clamp(Number(info.plate.zHome) || 0, 0, Math.max(0, info.plate.Z - 1)),
      focus: null, // site index p while one site fills the stage
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
  wireTrash();
  wireDragForward();
  buildChannelPanel();
  buildLevelLamps();
  buildViewer();
  buildPlate();
  if (state.plate) pollPlateStatus();
  wireCompareRelay();
  wireTools();
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
  return state.info.channels
    .map((ch, i) => {
      const l = luts[i];
      if (!l) return "";
      const g = Math.abs(l.gamma - 1) > 0.005 ? ":" + l.gamma.toFixed(2) : "";
      return Math.round(l.lo) + ":" + Math.round(l.hi) + g;
    })
    .join(",");
}

function renderParams(q) {
  // the channel set, the LUT windows and the cache generation, shared by
  // tiles, plate frames and rendered exports
  if (state.channels.length !== state.info.channels.length)
    q.set("c", state.channels.join(","));
  const win = lutParam();
  if (win) q.set("win", win);
  // the cache generation makes tile URLs immutable: the browser may keep
  // them, and a rebuilt cache changes the URLs instead of serving stale
  if (state.info.generation) q.set("g", state.info.generation);
  return q;
}

function plateFrameParams() {
  // the frame the stage shows: current time, the focused site (else the
  // first) and its plane; null for an ordinary slide
  const pl = state.plate;
  if (!pl) return null;
  const p = pl.focus === null ? 0 : pl.focus;
  return { t: pl.t, p, z: plateZFor(p) };
}

function plateZFor(p) {
  // the plane a site shows: the one the user set, or with autofocus on the
  // sharpest plane measured for that site at this time point
  const pl = state.plate;
  if (!pl) return 0;
  if (!pl.auto || !pl.focusMap) return pl.z;
  const row = pl.focusMap.best[pl.t];
  const z = row ? row[p] : null;
  return Number.isInteger(z) ? clamp(z, 0, state.info.plate.Z - 1) : pl.z;
}

function plateGroupKey() {
  // frames of one time point group under the plane they share; with
  // autofocus the sites differ, so they group under the mode instead
  const pl = state.plate;
  return pl.auto ? "a" : String(pl.z);
}

function withPlateParams(q) {
  const f = plateFrameParams();
  if (f) {
    q.set("t", f.t);
    q.set("p", f.p);
    q.set("z", f.z);
  }
  return q;
}

function tileQuery() {
  const q = withPlateParams(renderParams(new URLSearchParams()));
  const s = q.toString();
  return s ? "?" + s : "";
}

function makeTileSource() {
  const info = state.info;
  // server levels: index 0 = full resolution; OSD wants level 0 = smallest.
  const lv = info.levels.slice().reverse();
  return {
    width: info.width,
    height: info.height,
    tileSize: info.tileSize,
    tileOverlap: 0,
    minLevel: 0,
    maxLevel: lv.length - 1,
    getLevelScale: function (l) {
      return lv[l].width / info.width;
    },
    getNumTiles: function (l) {
      // exact integer tile counts (float drift in scale*width could otherwise
      // request one tile past the edge on odd-sized levels)
      return new OpenSeadragon.Point(
        Math.ceil(lv[l].width / info.tileSize),
        Math.ceil(lv[l].height / info.tileSize)
      );
    },
    getTileUrl: function (l, x, y) {
      // read the query at request time so a tile refresh (LUT, channel
      // switch, frame change) picks up the current settings without a reopen
      return "api/tile/" + lv[l].path + "/" + x + "/" + y + ".jpg" + tileQuery();
    },
  };
}

function flipAwareViewerElementPoint(point) {
  const p = new OpenSeadragon.Point(Number(point.x), Number(point.y));
  const viewport = state.viewer?.viewport;
  if (viewport?.getFlip()) {
    p.x = viewport.getContainerSize().x - p.x;
  }
  return p;
}

function viewerElementToImagePoint(point) {
  return state.viewer.viewport.viewerElementToImageCoordinates(
    flipAwareViewerElementPoint(point)
  );
}

function imageToViewerElementPoint(point) {
  const viewport = state.viewer.viewport;
  const p = viewport.imageToViewerElementCoordinates(
    new OpenSeadragon.Point(Number(point.x), Number(point.y))
  );
  if (viewport.getFlip()) {
    p.x = viewport.getContainerSize().x - p.x;
  }
  return p;
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

  viewer.addHandler("open", () => {
    applyDesiredDisplayTransform(false);
    $("boot").style.display = "none";
    updateReadout();
    restoreRoiOverlay();
    if (!state.viewportRelay.reopening) postViewportState("user");
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
    renderAnnotations();
    scheduleViewportState();
  });
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
  }, true);
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

function reopenPreservingView() {
  const viewer = state.viewer;
  const bounds = viewer.viewport.getBounds();
  clearTimeout(state.viewportRelay.timer);
  state.viewportRelay.timer = null;
  state.viewportRelay.reopening = true;
  state.viewportRelay.suppressUntil = Number.POSITIVE_INFINITY;
  const cleanup = () => {
    viewer.removeHandler("open", restored);
    viewer.removeHandler("open-failed", failed);
  };
  const restored = () => {
    cleanup();
    applyDesiredDisplayTransform(false);
    viewer.viewport.fitBounds(bounds, true);
    restoreRoiOverlay();
    state.viewportRelay.reopening = false;
    state.viewportRelay.suppressUntil = Date.now() + 180;
    postViewportState("restore");
  };
  const failed = () => {
    cleanup();
    state.viewportRelay.reopening = false;
    state.viewportRelay.suppressUntil = 0;
  };
  viewer.addHandler("open", restored);
  viewer.addHandler("open-failed", failed);
  viewer.open(makeTileSource());
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
  if (info.rgb) {
    // RGB slides render as-is: no channel window at all
    state.windows.channels.close(true);
    $("tb-channels").disabled = true;
    $("tb-channels").style.opacity = 0.4;
    return;
  }
  const list = $("channel-list");
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
    auto.title = "Auto-adjust window (background peak–99.9 percentile; bright-background fallback)";
    auto.textContent = "Auto";
    auto.addEventListener("click", (ev) => {
      ev.preventDefault();
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
    const toggle = macSwitch(true, (next) => {
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
}

/* ---- per-channel LUT (NIS-style histogram + window/gamma curve) ------------
   Each channel gets a small canvas: the intensity histogram filled in the
   channel color, a black (low) and white (high) triangle to drag the display
   window, and a knob on the mapping curve that drags vertically to set gamma
   -- the layout NIS-Elements' LUTs panel uses.  Shift-drag applies to all
   channels. */

const applyLuts = debounce(() => refreshTiles(), 250);

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
  const wrap = document.createElement("div");
  wrap.className = "lut";
  const canvas = document.createElement("canvas");
  canvas.className = "lut-canvas";
  const ctx = canvas.getContext("2d");
  wrap.append(canvas);

  // size follows the Channels window; relayout() re-derives everything
  let W = 208;
  let H = 84;
  let PLOT = { x0: 4, x1: W - 4, y0: 14, y1: H - 18 };

  function relayout(cssW) {
    W = Math.max(180, Math.round(cssW));
    H = Math.round(Math.max(74, Math.min(150, W * 0.4)));
    PLOT = { x0: 4, x1: W - 4, y0: 14, y1: H - 18 };
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  const def = { lo: ch.window.start, hi: ch.window.end, gamma: 1 };
  const cur = { ...def };
  let vmin = Math.min(Number(ch.window.min) || 0, def.lo);
  let vmax = Math.max(
    def.hi,
    vmin + 2 * Math.max(def.hi - vmin, 1)
  ); // provisional until the histogram loads
  let bins = null;

  const vx = (v) => PLOT.x0 +
    clamp((v - vmin) / Math.max(vmax - vmin, 1e-9), 0, 1) * (PLOT.x1 - PLOT.x0);
  const xv = (x) =>
    vmin + clamp((x - PLOT.x0) / (PLOT.x1 - PLOT.x0), 0, 1) * (vmax - vmin);
  const curveY = (t) =>
    PLOT.y1 - Math.pow(Math.max(0, Math.min(1, t)), 1 / cur.gamma) * (PLOT.y1 - PLOT.y0);
  const knobPos = () => ({
    x: vx(cur.lo + (Math.max(cur.hi, cur.lo + 1) - cur.lo) * 0.5),
    y: curveY(0.5),
  });

  function draw() {
    const w = PLOT.x1 - PLOT.x0;
    const h = PLOT.y1 - PLOT.y0;
    ctx.clearRect(0, 0, W, H);
    // frame
    ctx.strokeStyle = inkColor(0.12);
    ctx.lineWidth = 1;
    ctx.strokeRect(PLOT.x0 - 0.5, PLOT.y0 - 0.5, w + 1, h + 1);
    // histogram (sqrt-scaled so tissue signal shows over background counts)
    if (bins) {
      const peak = Math.sqrt(Math.max(...bins, 1));
      ctx.beginPath();
      ctx.moveTo(PLOT.x0, PLOT.y1);
      for (let b = 0; b < bins.length; b++) {
        const x = PLOT.x0 + (w * (b + 0.5)) / bins.length;
        ctx.lineTo(x, PLOT.y1 - (Math.sqrt(bins[b]) / peak) * (h - 2));
      }
      ctx.lineTo(PLOT.x1, PLOT.y1);
      ctx.closePath();
      ctx.fillStyle = "#" + ch.color + "55";
      ctx.fill();
      ctx.strokeStyle = "#" + ch.color + "cc";
      ctx.stroke();
    }
    // window guides
    ctx.strokeStyle = inkColor(0.30);
    ctx.setLineDash([2, 3]);
    for (const v of [cur.lo, cur.hi]) {
      ctx.beginPath();
      ctx.moveTo(vx(v) + 0.5, PLOT.y0);
      ctx.lineTo(vx(v) + 0.5, PLOT.y1);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    // mapping curve: flat-left, gamma ramp, flat-right
    ctx.strokeStyle = inkColor(0.85);
    ctx.beginPath();
    ctx.moveTo(PLOT.x0, PLOT.y1);
    ctx.lineTo(vx(cur.lo), PLOT.y1);
    const steps = 40;
    for (let s = 0; s <= steps; s++) {
      const t = s / steps;
      ctx.lineTo(vx(cur.lo + (cur.hi - cur.lo) * t), curveY(t));
    }
    ctx.lineTo(PLOT.x1, PLOT.y0);
    ctx.stroke();
    // gamma knob
    const k = knobPos();
    ctx.beginPath();
    ctx.arc(k.x, k.y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = currentTheme() === "light" ? "#f6f6f8" : "#1e1e20";
    ctx.fill();
    ctx.strokeStyle = inkColor(0.85);
    ctx.stroke();
    // lo/hi triangles along the top edge
    triangle(vx(cur.lo), currentTheme() === "light" ? "#3a3a3c" : "#111214", inkColor(0.55));
    triangle(vx(cur.hi), currentTheme() === "light" ? "#ffffff" : "rgba(255,255,255,0.92)", "rgba(0,0,0,0.6)");
    // labels — SF Mono ramp
    ctx.font = "9px ui-monospace, 'SF Mono', Menlo, monospace";
    ctx.fillStyle = inkColor(0.55);
    ctx.textAlign = "left";
    ctx.fillText(fmtInt(cur.lo), PLOT.x0, 9);
    ctx.textAlign = "right";
    ctx.fillText(fmtInt(cur.hi), PLOT.x1, 9);
    ctx.textAlign = "center";
    ctx.fillText("G: " + cur.gamma.toFixed(2), (PLOT.x0 + PLOT.x1) / 2, 9);
    ctx.fillStyle = inkColor(0.28);
    ctx.textAlign = "left";
    ctx.fillText(fmtInt(vmin), PLOT.x0, H - 3);
    ctx.textAlign = "right";
    ctx.fillText(fmtInt(vmax), PLOT.x1, H - 3);
    winLabel.textContent = fmtInt(cur.lo) + "–" + fmtInt(cur.hi);
  }

  function triangle(x, fill, stroke) {
    ctx.beginPath();
    ctx.moveTo(x - 5, PLOT.y0 - 10);
    ctx.lineTo(x + 5, PLOT.y0 - 10);
    ctx.lineTo(x, PLOT.y0 - 1);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = stroke;
    ctx.stroke();
  }

  function isDefault(l) {
    return (
      Math.abs(l.lo - def.lo) < 0.5 &&
      Math.abs(l.hi - def.hi) < 0.5 &&
      Math.abs(l.gamma - 1) < 0.005
    );
  }
  function setLut(l) {
    cur.lo = l.lo;
    cur.hi = Math.max(l.hi, l.lo + 1);
    cur.gamma = Math.max(0.25, Math.min(4, l.gamma));
    draw();
    state.luts[i] = isDefault(cur) ? null : { ...cur };
    applyLuts(); // shared debounce: one tile reload even for shift-drags
  }

  // dragging: lo/hi triangles (horizontal), gamma knob (vertical)
  let mode = null;
  const pt = (ev) => {
    const r = canvas.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  };
  canvas.addEventListener("pointerdown", (ev) => {
    const p = pt(ev);
    const k = knobPos();
    if (Math.hypot(p.x - k.x, p.y - k.y) < 9) mode = "gamma";
    else if (Math.abs(p.x - vx(cur.hi)) < Math.abs(p.x - vx(cur.lo))) mode = "hi";
    else mode = "lo";
    canvas.setPointerCapture(ev.pointerId);
    drag(ev);
    ev.preventDefault();
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (mode) drag(ev);
  });
  canvas.addEventListener("pointerup", () => (mode = null));

  function drag(ev) {
    const p = pt(ev);
    const next = { ...cur };
    if (mode === "gamma") {
      const frac = (PLOT.y1 - Math.max(PLOT.y0, Math.min(PLOT.y1, p.y))) /
        (PLOT.y1 - PLOT.y0);
      // knob height = 0.5^(1/gamma)  =>  gamma = ln(0.5)/ln(frac)
      const f = Math.max(0.02, Math.min(0.98, frac));
      next.gamma = Math.max(0.25, Math.min(4, Math.log(0.5) / Math.log(f)));
    } else {
      const v = xv(p.x);
      if (mode === "lo") next.lo = Math.min(v, cur.hi - 1);
      else next.hi = Math.max(v, cur.lo + 1);
    }
    if (ev.shiftKey) {
      state.lutWidgets.forEach((wd) => wd.setLut(next));
    } else {
      setLut(next);
    }
  }

  const widget = {
    setLut,
    relayout,
    setHistogram(hg) {
      bins = hg.bins;
      vmin = Number.isFinite(Number(hg.vmin)) ? Number(hg.vmin) : 0;
      const reportedMax = Number(hg.vmax);
      vmax = Math.max(Number.isFinite(reportedMax) ? reportedMax : vmin + 1, vmin + 1);
      draw();
    },
    reset() {
      setLut({ ...def });
    },
    auto() {
      const window = autoWindowFromHistogram(bins, vmin, vmax);
      if (window) setLut({ ...window, gamma: cur.gamma });
    },
  };
  state.lutWidgets[i] = widget;
  relayout(state.windows.channels.bodyWidth());
  return wrap;
}

function autoWindowFromHistogram(bins, vmin, vmax, rightPeakFraction = 0.70) {
  if (!Array.isArray(bins) || !bins.length || !(vmax > vmin)) return null;
  const total = bins.reduce((sum, count) => sum + Number(count || 0), 0);
  if (!(total > 0)) return null;
  const bw = (vmax - vmin) / bins.length;
  let acc = 0;
  let fallbackBin = -1;
  let highBin = bins.length - 1;
  let modeBin = 0;
  for (let b = 0; b < bins.length; b++) {
    if (Number(bins[b] || 0) > Number(bins[modeBin] || 0)) modeBin = b;
    acc += Number(bins[b] || 0);
    if (acc >= total * 0.001 && fallbackBin < 0) fallbackBin = b;
  }
  acc = 0;
  for (let b = 0; b < bins.length; b++) {
    acc += Number(bins[b] || 0);
    if (acc >= total * 0.999) { highBin = b; break; }
  }
  const fallback = vmin + Math.max(0, fallbackBin) * bw;
  const high = Math.max(vmin + (highBin + 1) * bw, fallback + 1);
  const mode = vmin + modeBin * bw;
  const low = mode >= fallback + rightPeakFraction * (high - fallback)
    ? fallback
    : mode;
  return { lo: low, hi: Math.max(high, low + 1) };
}

function relayoutLuts() {
  const w = state.windows.channels.bodyWidth();
  state.lutWidgets.forEach((wd) => wd && wd.relayout(w));
}

function loadHistograms() {
  const q = withPlateParams(new URLSearchParams()).toString();
  fetch("api/histogram" + (q ? "?" + q : ""))
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
    .then((d) => {
      d.channels.forEach((hg, i) => {
        if (state.lutWidgets[i]) state.lutWidgets[i].setHistogram(hg);
      });
    })
    .catch(() => {}); // panel still works without histograms
}

// a plate frame change refetches the histogram of the frame on the stage
const refreshHistograms = debounce(loadHistograms, 300);

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
    state.pixel.queued = null;
    if (state.pixel.timer && !state.pixel.inFlight) {
      clearTimeout(state.pixel.timer);
      state.pixel.timer = null;
    }
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
  return !!(
    c && result &&
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
  state.pixel.queued = { x, y };
  pumpPixelProbe();
}

function pumpPixelProbe() {
  if (state.pixel.inFlight || state.pixel.timer || !state.pixel.queued) return;
  const now = Date.now();
  const wait = Math.max(
    0,
    100 - (now - state.pixel.lastStarted),
    state.pixel.retryAfter - now
  );
  state.pixel.timer = setTimeout(() => {
    state.pixel.timer = null;
    if (!state.pixel.queued) return;
    const requested = state.pixel.queued;
    state.pixel.queued = null;
    state.pixel.inFlight = true;
    state.pixel.lastStarted = Date.now();
    const query = withPlateParams(new URLSearchParams({ x: requested.x, y: requested.y }));
    fetch("api/pixel?" + query.toString(), { cache: "no-store" })
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
      .then((data) => {
        state.pixel.result = data;
        state.pixel.failed = false;
        state.pixel.retryAfter = 0;
        renderPixelInspector();
      })
      .catch(() => {
        state.pixel.failed = true;
        state.pixel.retryAfter = Date.now() + 2500;
        renderPixelInspector();
      })
      .finally(() => {
        state.pixel.inFlight = false;
        if (state.pixel.queued) pumpPixelProbe();
      });
  }, wait);
}

function togglePixelInspector() {
  state.pixel.hudVisible = !state.pixel.hudVisible;
  renderPixelInspector();
  if (state.pixel.hudVisible && !state.pixel.cursor) {
    showToast("Pixel Inspector on — move over the slide");
  }
}

function updateScalebar(iz) {
  if (!pixelSize()) {
    $("scalebar").style.display = "none";
    $("scalebar-label").textContent = "";
    return;
  }
  const px = pixelSize()[1];
  const umPerScreenPx = px / iz;
  const target = 110 * umPerScreenPx; // aim for a ~110 px bar
  const nice = niceLength(target);
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
};

function annLocked() {
  // a degraded overview's base level is level 1: drawing here would save
  // half-scale coordinates into the slide's level-0 sidecar
  return state.info && state.info.storage === "overview-degraded";
}

function setTool(tool) {
  if (annLocked() && (tool === "measure" || tool === "pin" || tool === "box")) {
    setAnnStatus("Annotation is locked while the source file is missing");
    return;
  }
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
  state.viewer.setMouseNavEnabled(!on);
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
  // the image-space box covering a screen rectangle, whatever the rotation
  const corners = [
    imgPoint({ x: a.x, y: a.y }), imgPoint({ x: b.x, y: a.y }),
    imgPoint({ x: b.x, y: b.y }), imgPoint({ x: a.x, y: b.y }),
  ];
  const xs = corners.map((c) => c.x), ys = corners.map((c) => c.y);
  const x = Math.min(...xs), y = Math.min(...ys);
  return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
}

function wireTools() {
  const stage = $("stage");
  const rubber = $("rubber");
  let start = null; // element coords
  let startImg = null;

  $("roi-toggle").onclick = () => setTool("roi");
  $("roi-move").onclick = () => {
    if (state.roi) setTool("move");
  };
  $("tool-measure").onclick = () => setTool("measure");
  $("tool-pin").onclick = () => setTool("pin");
  $("tool-box").onclick = () => setTool("box");

  let moveFrom = null; // roi origin at drag start (move mode)
  let landmarkStart = null; // a click, not a drag, places an alignment point
  stage.addEventListener("pointerdown", (ev) => {
    if (state.landmark.active && !state.tool && ev.button === 0) {
      landmarkStart = elementPoint(ev, stage);
      return; // OpenSeadragon keeps the drag, so the user can still pan
    }
    if (!state.tool || ev.button !== 0) return;
    if (state.tool === "move" && !state.roi) return;
    start = elementPoint(ev, stage);
    startImg = imgPoint(start);
    if (state.tool === "move") {
      moveFrom = { x: state.roi.x, y: state.roi.y };
      stage.classList.add("grabbing");
    }
    if (state.tool === "roi" || state.tool === "box") {
      rubber.style.display = "block";
      positionRubber(start, start);
    }
    try { stage.setPointerCapture(ev.pointerId); } catch (_) { /* optional */ }
    ev.preventDefault();
    ev.stopPropagation();
  }, true);

  stage.addEventListener("pointermove", (ev) => {
    if (!state.tool || !start) return;
    const cur = elementPoint(ev, stage);
    if (state.tool === "move" && moveFrom) {
      const p = imgPoint(cur);
      const r = state.roi;
      r.x = Math.round(clamp(moveFrom.x + (p.x - startImg.x), 0, state.info.width - r.w));
      r.y = Math.round(clamp(moveFrom.y + (p.y - startImg.y), 0, state.info.height - r.h));
      moveRoiOverlay();
    } else if (state.tool === "roi" || state.tool === "box") {
      positionRubber(start, cur);
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
    if (!state.tool || !start) return;
    const end = elementPoint(ev, stage);
    const endImg = imgPoint(end);
    rubber.style.display = "none";
    const tool = state.tool;
    const moved = Math.hypot(end.x - start.x, end.y - start.y);

    if (tool === "move") {
      moveFrom = null;
      stage.classList.remove("grabbing");
      updateRoiPanel(); // position changed; size and readouts stay
    } else if (tool === "roi") {
      finishSelection(start, end);
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
    } else if (tool === "box") {
      if (moved >= 6) {
        const r = imageBoundsOfScreenRect(start, end);
        const item = addAnnotation({ type: "box", ...r, text: "" });
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
    start = null;
    startImg = null;
    ev.preventDefault();
    ev.stopPropagation();
  }, true);

  $("roi-clear").onclick = clearRoi;
  $("roi-full").onclick = () => applyRoi(0, 0, state.info.width, state.info.height);
  $("roi-dl-nd2").onclick = () => downloadRoi("nd2");
  $("roi-dl-tiff").onclick = () => downloadRoi("tiff");
  $("roi-dl-png").onclick = () => downloadRoi("png");
  $("roi-dl-jpg").onclick = () => downloadRoi("jpg");
  if (state.info.nd2Export === false) {
    const b = $("roi-dl-nd2");
    b.disabled = true;
    b.title =
      "ND2 export needs the limnd2 package on the server — " +
      "pip install --index-url https://pypi.laboratory-imaging.com/simple limnd2";
  }

  function positionRubber(a, b) {
    const r = rectFrom(a, b);
    const wrap = $("stage-wrap").getBoundingClientRect();
    const st = stage.getBoundingClientRect();
    rubber.style.left = r.x + (st.left - wrap.left) + "px";
    rubber.style.top = r.y + (st.top - wrap.top) + "px";
    rubber.style.width = r.w + "px";
    rubber.style.height = r.h + "px";
  }

  wireRoiDims();
  wireAnnotationPanel();
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
  state.annotations = state.annotations.filter((a) => a.id !== id);
  if (state.editingId === id) closeEditor();
  annotationsChanged();
}

function annotationsChanged() {
  if (state.quitPreparing) return;
  state.annRevision += 1;
  state.annDirty = true;
  renderAnnotations();
  rebuildAnnList();
  scheduleAnnSave();
}

const scheduleAnnSave = debounce(saveAnnotations, 800);

function annotationsUrl(site) {
  // a plate keeps one sidecar per site, shared across time and z; the
  // coordinates inside are level-0 pixels of the frame, as for any slide
  if (!state.plate) return "api/annotations";
  const p = site === undefined ? plateFrameParams().p : site;
  return "api/annotations?p=" + p;
}

function annotationSaveEntry(site) {
  return {
    url: annotationsUrl(site),
    body: JSON.stringify({ items: state.annotations }),
    revision: state.annRevision,
    context: state.annContext,
  };
}

function queueAnnotationSave(entry) {
  const run = state.annSaveTail.then(async () => {
    try {
      const response = await fetch(entry.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: entry.body,
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      const failed = state.annFailedSaves.get(entry.url);
      if (!failed || failed.revision <= entry.revision) {
        state.annFailedSaves.delete(entry.url);
      }
      if (state.annContext === entry.context && state.annRevision === entry.revision) {
        state.annDirty = false;
      }
      setAnnStatus("Saved · " + basename(data.path));
      return { ok: true };
    } catch (error) {
      const failed = state.annFailedSaves.get(entry.url);
      if (!failed || failed.revision <= entry.revision) {
        state.annFailedSaves.set(entry.url, entry);
      }
      setAnnStatus("Save failed: " + error.message);
      return { ok: false, error: error.message };
    }
  });
  // Always resolve the tail so one failed request cannot suppress a newer
  // captured snapshot.  Callers inspect the individual result instead.
  state.annSaveTail = run.then(() => undefined);
  return run;
}

function saveAnnotations(site) {
  if (annLocked()) {
    setAnnStatus("Not saved: annotation is locked in this degraded view");
    return Promise.resolve({ ok: false, error: "annotation is locked" });
  }
  return queueAnnotationSave(annotationSaveEntry(site));
}

async function flushAnnotationsForUpdate() {
  if (!$('ann-editor').hidden) closeEditor(true);
  state.quitPreparing = true;
  scheduleAnnSave.cancel();

  if (state.annDirty) {
    const saved = await saveAnnotations();
    if (!saved.ok) throw new Error(saved.error || "annotation save failed");
  }
  await state.annSaveTail;

  // A plate may have changed sites after queueing a save. Retry the exact
  // URL and payload captured for every older site rather than writing the
  // current site's marks into it.
  for (const entry of [...state.annFailedSaves.values()]) {
    const retried = await queueAnnotationSave(entry);
    if (!retried.ok) throw new Error(retried.error || "annotation retry failed");
  }
  await state.annSaveTail;
  if (state.annDirty || state.annFailedSaves.size) {
    throw new Error("annotations are still waiting to be saved");
  }
}

async function acknowledgeUpdatePreparation(requestId) {
  let reply;
  try {
    await flushAnnotationsForUpdate();
    reply = { ok: true };
  } catch (error) {
    reply = { ok: false, error: String(error.message || error).slice(0, 240) };
  }
  window.parent.postMessage({
    nd2wsi: "quit-ready",
    version: VIEWPORT_PROTOCOL_VERSION,
    requestId,
    sid: currentSlideSid(),
    ...reply,
  }, location.origin);
}

function loadAnnotations() {
  // switching sites twice in a row leaves two loads in flight; the older
  // one must not install its items over the newer site's
  const seq = ++state.annLoadSeq;
  fetch(annotationsUrl())
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
    .then((d) => {
      if (seq !== state.annLoadSeq) return;
      state.annotations = Array.isArray(d.items) ? d.items : [];
      state.annPath = d.path;
      // a skipped legacy-sidecar import must not be silent: the server
      // leaves a note explaining why old annotations are not shown here
      const skipped = ((state.info && state.info.notes) || []).find((n) =>
        n.includes("annotation sidecar")
      );
      setAnnStatus(
        skipped
          ? skipped
          : state.annotations.length
          ? "Loaded " + state.annotations.length + " from " + basename(d.path)
          : "Sidecar: " + basename(d.path)
      );
      renderAnnotations();
      rebuildAnnList();
    })
    .catch(() => setAnnStatus(""));
}

function setAnnStatus(msg) {
  $("ann-status").textContent = msg || "";
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
  const g = svgEl("g", { class: id ? "hit" : "" }, parent);
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
  if (id) g.addEventListener("pointerdown", (ev) => {
    ev.stopPropagation();
    openEditor(id);
  });
  return g;
}

function renderAnnotations() {
  const layer = $("ann-layer");
  if (!state.viewer) return;
  layer.replaceChildren();

  const drawLine = (a, isTemp) => {
    const col = annColor(a);
    const p1 = toEl(a.x1, a.y1);
    const p2 = toEl(a.x2, a.y2);
    const g = svgEl("g", {}, layer);
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
    chip(layer, mx, my, label, isTemp ? null : a.id);
  };

  for (const a of state.annotations) {
    if (a.type === "line") drawLine(a, false);
    else if (a.type === "box") {
      const corners = [
        toEl(a.x, a.y), toEl(a.x + a.w, a.y), toEl(a.x + a.w, a.y + a.h), toEl(a.x, a.y + a.h),
      ];
      const left = Math.min(...corners.map((c) => c.x));
      const top = Math.min(...corners.map((c) => c.y));
      const g = svgEl("g", { class: "hit" }, layer);
      svgEl("polygon", {
        points: corners.map((c) => c.x + "," + c.y).join(" "),
        fill: hexAlpha(annColor(a), 0.08), stroke: annColor(a), "stroke-width": 1.5,
        "stroke-linejoin": "round",
      }, g);
      g.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); openEditor(a.id); });
      chip(layer, left, top - 22, a.text || "Box", a.id);
    } else if (a.type === "pin") {
      const p = toEl(a.x, a.y);
      const g = svgEl("g", { class: "hit" }, layer);
      svgEl("circle", { cx: p.x, cy: p.y, r: 6, fill: annColor(a), stroke: "#fff", "stroke-width": 1.5 }, g);
      svgEl("circle", { cx: p.x, cy: p.y, r: 1.8, fill: "#fff" }, g);
      g.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); openEditor(a.id); });
      if (a.text) chip(layer, p.x + 10, p.y - 9, a.text, a.id);
    }
  }
  if (state.tempLine) drawLine({ ...state.tempLine, text: "" }, true);
  renderLandmarks(layer);
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
  if (window.parent === window) return;
  window.parent.postMessage({
    nd2wsi: "landmark-points",
    version: VIEWPORT_PROTOCOL_VERSION,
    sid: currentSlideSid(),
    points: state.landmark.points.map((p) => ({ x: p.x, y: p.y })),
  }, location.origin);
}

function setLandmarkMode(message) {
  const lm = state.landmark;
  const viewer = state.viewer;
  const active = Boolean(message.active);
  if (Array.isArray(message.points)) {
    lm.points = message.points
      .map((p) => ({ x: Number(p.x), y: Number(p.y) }))
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
  const img = imgPoint(screenPoint);
  if (lm.points.length < lm.needed) {
    lm.points.push({ x: img.x, y: img.y });
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
    lm.points[nearest] = { x: img.x, y: img.y };
  }
  renderAnnotations();
  updateLandmarkHint();
  postLandmarkPoints();
}

function undoLandmark() {
  const lm = state.landmark;
  if (!lm.points.length) return;
  lm.points.pop();
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
    downloadJsonFile(
      annotationDocument(),
      state.info.name.replace(/\.(nd2|svs)$/i, "") + ".annotations.json",
      "application/json"
    );
  };
  $("ann-geojson").onclick = exportAnnotationsGeoJSON;
  $("ann-open").onclick = () => $("ann-file").click();
  $("ann-file").addEventListener("change", () => {
    const f = $("ann-file").files[0];
    if (!f) return;
    f.text().then((txt) => {
      try {
        const data = JSON.parse(txt);
        const { items, verified } = annotationItemsForThisSlide(data);
        state.annotations = items;
        annotationsChanged();
        setAnnStatus(
          "Opened " + f.name + " (" + items.length + ")" +
          (verified ? "" : " · legacy file, source not verified")
        );
      } catch (e) {
        showToast("could not open annotations: " + e.message);
      }
      $("ann-file").value = "";
    });
  });
}

function downloadJsonFile(data, filename, mime) {
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
  const doc = annotationsToGeoJSON(state.annotations);
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
  if (state.plate) return { p: plateFrameParams().p };
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

function annotationItemsForThisSlide(data) {
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
    const expected = normalizedSelection(currentSelection());
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

function finishSelection(a, b) {
  const box = imageBoundsOfScreenRect(a, b);
  const info = state.info;
  const x0 = clamp(box.x, 0, info.width);
  const y0 = clamp(box.y, 0, info.height);
  const x1 = clamp(box.x + box.w, 0, info.width);
  const y1 = clamp(box.y + box.h, 0, info.height);
  const w = Math.round(x1 - x0);
  const h = Math.round(y1 - y0);
  setTool(null);
  if (w < 4 || h < 4) return; // treat as an aborted drag
  applyRoi(Math.round(x0), Math.round(y0), w, h);
}

function applyRoi(x, y, w, h) {
  state.roi = { x, y, w, h };
  state.renderScale = null; // a new region starts at the finest fitting scale
  drawRoiOverlay();
  updateRoiPanel();
  $("roi-detail").classList.add("active");
  $("roi-hint").style.display = "none";
  $("roi-move").disabled = false;
  state.windows.region.fitContent(); // saved heights must not hide the exports
}

function drawRoiOverlay() {
  const viewer = state.viewer;
  if (state.roiOverlayEl) {
    viewer.removeOverlay(state.roiOverlayEl);
    state.roiOverlayEl = null;
  }
  if (!state.roi) return;
  const el = document.createElement("div");
  el.className = "roi-overlay";
  const r = state.roi;
  viewer.addOverlay({
    element: el,
    location: viewer.viewport.imageToViewportRectangle(
      new OpenSeadragon.Rect(r.x, r.y, r.w, r.h)
    ),
  });
  state.roiOverlayEl = el;
}

function moveRoiOverlay() {
  const r = state.roi;
  if (!r || !state.roiOverlayEl) return drawRoiOverlay();
  state.viewer.updateOverlay(
    state.roiOverlayEl,
    state.viewer.viewport.imageToViewportRectangle(
      new OpenSeadragon.Rect(r.x, r.y, r.w, r.h)
    )
  );
}

function restoreRoiOverlay() {
  if (state.roi) drawRoiOverlay();
}

function clearRoi() {
  state.roi = null;
  if (state.roiOverlayEl) {
    state.viewer.removeOverlay(state.roiOverlayEl);
    state.roiOverlayEl = null;
  }
  $("roi-detail").classList.remove("active");
  $("roi-hint").style.display = "";
  $("roi-move").disabled = true;
  if (state.tool === "move") setTool("move"); // toggles the mode off
}

function updateRoiPanel() {
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

/* ---- render scale: how PNG and JPEG leave -------------------------------
   Each option is one pyramid level. The strip shows the same field at every
   scale so the pixel cost is visible, the dims line shows what the file
   will hold, and the size is measured from one real 512 px sample at that
   level rather than guessed. ND2 and TIFF ignore all of this and stream at
   native resolution. */
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
  const strip = $("roi-scales");
  const dims = $("roi-scale-dims");
  const r = state.roi;
  const opts = scaleOptions();
  const seq = ++scaleSeq;
  strip.innerHTML = "";
  if (!r || !opts.length) {
    dims.textContent = "–";
    return;
  }
  const sel = roiScale();
  const win = lutParam();
  let winQ = win ? "&win=" + encodeURIComponent(win) : "";
  if (state.channels.length !== state.info.channels.length) {
    winQ += "&c=" + state.channels.join(",");
  }
  const frame = plateFrameParams();
  if (frame) winQ += "&t=" + frame.t + "&p=" + frame.p + "&z=" + frame.z;
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
  note.textContent = "ND2 and TIFF stay native.";
  estimateRenderBytes(sel, winQ).then((t) => {
    if (seq === scaleSeq && t) {
      note.textContent = t + " · ND2 and TIFF stay native.";
    }
  });
}

function levelPathFor(d) {
  const lv = state.info.levels.find((l) => l.downsample === d);
  return lv ? lv.path : 0;
}

const sizeSamples = new Map(); // sid-stable cache: level+fmt+win -> bytes/px
async function estimateRenderBytes(o, winQ) {
  const r = state.roi;
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
  if (!r) return;
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

function downloadRoi(fmt) {
  const r = state.roi;
  if (!r) return;
  // ND2 and TIFF stream raw pixels at native resolution; a rendered PNG or
  // JPEG too big for one image drops to the pyramid level that fits, which
  // is how the whole slide leaves as one picture
  let level = 0;
  let d = 1;
  if (fmt === "png" || fmt === "jpg") {
    const sel = roiScale();
    if (!sel) {
      showToast(
        "rendered export capped at " + state.info.maxRenderMpx +
        " MPx — use ND2/TIFF (they stream any size)"
      );
      return;
    }
    d = sel.d;
    level = state.info.levels.findIndex((l) => l.downsample === d);
  }
  const q = new URLSearchParams({
    level,
    x: Math.floor(r.x / d),
    y: Math.floor(r.y / d),
    w: Math.max(1, Math.floor(r.w / d)),
    h: Math.max(1, Math.floor(r.h / d)),
    format: fmt,
  });
  const all = state.channels.length === state.info.channels.length;
  if (!all) q.set("c", state.channels.join(","));
  if (fmt === "png" || fmt === "jpg") {
    const win = lutParam();
    if (win) q.set("win", win); // rendered exports match the screen LUTs
  }
  withPlateParams(q); // a plate exports the frame on the stage
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
  for (const name of ["thumbnail", "label", "macro"]) {
    if (!available.has(name)) continue;
    const figure = document.createElement("figure");
    const image = document.createElement("img");
    const caption = document.createElement("figcaption");
    image.alt = name[0].toUpperCase() + name.slice(1) + " associated image";
    image.loading = "lazy";
    image.src = "api/associated/" + name + ".jpg" +
      (state.info.generation ? "?g=" + encodeURIComponent(state.info.generation) : "");
    caption.textContent = name[0].toUpperCase() + name.slice(1);
    image.addEventListener("error", () => {
      figure.remove();
      if (!container.children.length) section.hidden = true;
    });
    figure.append(image, caption);
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
    $("info-status").textContent = label + " revealed in Finder";
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
  const degrees = Number(message.degrees);
  if (!Number.isFinite(degrees) || typeof message.flipped !== "boolean") return;
  state.viewportRelay.displayRotation = normalizedRotation(degrees);
  state.viewportRelay.displayFlipped = message.flipped;
  if (applyDesiredDisplayTransform()) return;
  if (state.viewportRelay.transformWaitingForOpen) return;
  state.viewportRelay.transformWaitingForOpen = true;
  state.viewer.addOnceHandler("open", () => {
    state.viewportRelay.transformWaitingForOpen = false;
    applyDesiredDisplayTransform();
  });
}

function currentSlideSid() {
  const match = location.pathname.match(/\/s\/([0-9a-f]{8})\//);
  return match ? match[1] : null;
}

function viewportSnapshot() {
  const viewer = state.viewer;
  if (!viewer || !viewer.viewport || !viewer.world || !viewer.world.getItemCount()) return null;
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
  if (!numbers.every(Number.isFinite)) return null;
  const pixelSize = state.info.pixelSizeUm;
  const calibrated = Array.isArray(pixelSize) && pixelSize.length >= 2 &&
    Number(pixelSize[0]) > 0 && Number(pixelSize[1]) > 0;
  return {
    centerPx: {
      x: (topLeft.x + bottomRight.x) / 2,
      y: (topLeft.y + bottomRight.y) / 2,
    },
    spanPx: {
      x: Math.max(1e-6, Math.abs(bottomRight.x - topLeft.x)),
      y: Math.max(1e-6, Math.abs(bottomRight.y - topLeft.y)),
    },
    imagePx: { x: state.info.width, y: state.info.height },
    containerPx: (() => {
      const size = viewer.viewport.getContainerSize();
      return { x: Math.max(1, size.x), y: Math.max(1, size.y) };
    })(),
    pixelSizeUm: calibrated
      ? { x: Number(pixelSize[1]), y: Number(pixelSize[0]) }
      : null,
  };
}

function postViewportState(reason, extra = {}) {
  if (window.parent === window) return;
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
  if (state.viewportRelay.timer) return;
  state.viewportRelay.timer = setTimeout(() => {
    state.viewportRelay.timer = null;
    if (Date.now() < state.viewportRelay.suppressUntil) return;
    // An Option-drag moves only this pane. The shell reads the resulting
    // relative position as the new alignment instead of steering the partner.
    postViewportState("user", state.viewportRelay.altHeld ? { nudge: true } : {});
  }, VIEWPORT_EMIT_MS);
}

function nudgeView(dxPx, dyPx) {
  // Move this pane's picture by a screen-pixel delta, whatever its display
  // rotation or mirror, then report the move as an alignment nudge.
  const viewer = state.viewer;
  if (!viewer?.viewport || !viewer.world || !viewer.world.getItemCount()) return;
  const dx = Number(dxPx), dy = Number(dyPx);
  if (!Number.isFinite(dx) || !Number.isFinite(dy) || (!dx && !dy)) return;
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
  postViewportState("user", { nudge: true });
}

function finiteViewportPoint(value, positive = false) {
  if (!value || !Number.isFinite(Number(value.x)) || !Number.isFinite(Number(value.y))) {
    return null;
  }
  const point = { x: Number(value.x), y: Number(value.y) };
  return positive && (point.x <= 0 || point.y <= 0) ? null : point;
}

function applyLinkedViewport(message) {
  const center = finiteViewportPoint(message.centerPx);
  const span = finiteViewportPoint(message.spanPx, true);
  const commandId = String(message.commandId || "");
  if (!center || !span || !commandId) return;

  const apply = () => {
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
    const screenPerImage = viewport.getContainerSize().x / span.x;
    viewport.zoomTo(viewport.imageToViewportZoom(screenPerImage), null, true);
    viewport.panTo(
      viewport.imageToViewportCoordinates(new OpenSeadragon.Point(center.x, center.y)),
      true
    );
    viewport.applyConstraints(true);
    postViewportState("apply", { echoOf: commandId });
  };

  if (state.viewer.world && state.viewer.world.getItemCount()) apply();
  else state.viewer.addOnceHandler("open", apply);
}

function wireCompareRelay() {
  window.addEventListener("message", (event) => {
    if (
      window.parent === window || event.source !== window.parent ||
      event.origin !== location.origin || !event.data ||
      event.data.version !== VIEWPORT_PROTOCOL_VERSION
    ) return;
    if (event.data.nd2wsi === "prepare-quit") {
      const requestId = String(event.data.requestId || "");
      if (requestId) acknowledgeUpdatePreparation(requestId);
    } else if (event.data.nd2wsi === "viewport-request") {
      const requestId = String(event.data.requestId || "");
      if (!requestId) return;
      const reply = () => postViewportState("request", { requestId });
      if (state.viewer.world && state.viewer.world.getItemCount()) reply();
      else state.viewer.addOnceHandler("open", reply);
    } else if (event.data.nd2wsi === "viewport-apply") {
      applyLinkedViewport(event.data);
    } else if (event.data.nd2wsi === "viewport-nudge") {
      nudgeView(event.data.dxPx, event.data.dyPx);
    } else if (event.data.nd2wsi === "display-transform") {
      applyDisplayTransform(event.data);
    } else if (event.data.nd2wsi === "compare-state") {
      state.viewportRelay.compare = {
        enabled: Boolean(event.data.enabled),
        linked: Boolean(event.data.linked),
        moving: Boolean(event.data.moving),
        role: event.data.role === "anchor" ? "anchor" : "member",
      };
      if (!event.data.enabled && state.landmark.active) setLandmarkMode({ active: false, clear: true });
    } else if (event.data.nd2wsi === "landmark-mode") {
      setLandmarkMode(event.data);
    } else if (event.data.nd2wsi === "tab-shortcut-state") {
      state.tabCount = Math.max(0, Math.floor(Number(event.data.count) || 0));
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
    if (!state.landmark.active || window.parent === window) return;
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
    window.parent.postMessage({ nd2wsi: kind, version: VIEWPORT_PROTOCOL_VERSION }, location.origin);
  }, true);
  // Arrow keys nudge the alignment while linked. This runs in the capture
  // phase so OpenSeadragon's own arrow-key panning never sees the event.
  window.addEventListener("keydown", (ev) => {
    const compare = state.viewportRelay.compare;
    if (window.parent === window || !compare?.enabled || !compare.linked) return;
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
    window.parent.postMessage({
      nd2wsi: "compare-nudge",
      version: VIEWPORT_PROTOCOL_VERSION,
      dxPx: delta[0],
      dyPx: delta[1],
    }, location.origin);
  }, true);
  if (window.parent !== window) {
    window.parent.postMessage({
      nd2wsi: "viewport-ready",
      version: VIEWPORT_PROTOCOL_VERSION,
      sid: currentSlideSid(),
    }, location.origin);
  }
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

/* ---- cache trashcan --------------------------------------------------------
   Deletes this slide's pyramid store on disk (annotations stay). The tab
   closes because the tiles are gone; the slide re-converts on next open. */

function wireTrash() {
  const btn = $("tb-trash");
  const pop = $("trash-confirm");
  if (state.info && !state.info.trashable) {
    btn.hidden = true; // direct source or user-owned portable store
    return;
  }
  const hide = () => { pop.hidden = true; };
  btn.onclick = () => {
    if (pop.hidden) setPlateViewMenuOpen(false);
    pop.hidden = !pop.hidden;
  };
  $("trash-cancel").onclick = hide;
  document.addEventListener("pointerdown", (ev) => {
    if (!pop.hidden && !pop.contains(ev.target) && ev.target !== btn && !btn.contains(ev.target)) hide();
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") hide();
  });
  $("trash-go").onclick = () => {
    const m = location.pathname.match(/\/s\/([0-9a-f]{8})\//);
    if (!m) { hide(); showToast("cannot resolve slide id"); return; }
    const go = $("trash-go");
    const label = go.textContent;
    go.disabled = true;
    // a store is hundreds of thousands of small files, and on a USB disk
    // that takes minutes, so the button carries the count while it runs
    const job = Math.random().toString(36).slice(2, 10);
    const timer = setInterval(() => {
      fetch("api/roi/progress?job=" + job, { cache: "no-store" })
        .then((r) => r.json())
        .then((d) => {
          if (d.state === "deleting") go.textContent = "Deleting… " + (d.pct || 0) + " %";
        })
        .catch(() => {});
    }, 400);
    const stop = () => { clearInterval(timer); go.textContent = label; };
    fetch("../../api/trash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: m[1], job }),
    })
      .then((r) => r.json())
      .then((d) => {
        stop();
        if (d.error) throw new Error(d.error);
        showToast("cache deleted — freed " + fmtBytes(d.freed || 0));
        setTimeout(() => {
          if (window.parent !== window) {
            window.parent.postMessage({ nd2wsi: "slide-trashed" }, location.origin);
          } else {
            location.href = "../../";
          }
        }, 900);
      })
      .catch((e) => {
        stop();
        go.disabled = false;
        hide();
        showToast("could not delete cache: " + e.message);
      });
  };
}

/* ---- floating mac windows --------------------------------------------------
   Each panel is a small macOS-style window over the slide: draggable by its
   title bar, resizable from every edge and corner, with working traffic
   lights (close / minimize-to-titlebar / zoom) and remembered geometry. */

let winZ = 30;

function buildWindows() {
  const stage = $("stage-wrap");
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
      toolbarBtn: $("tb-info"),
      startClosed: true,
      onOpen: () => loadSlideInspector(),
    }),
  };
  window.addEventListener("resize", () => {
    Object.values(state.windows).forEach((w) => w.clampToStage());
  });
}

function makeMacWindow(el, opts) {
  const stage = $("stage-wrap");
  const body = el.querySelector(".win-body");
  const titlebar = el.querySelector(".win-titlebar");
  const store = "nd2wsi.win." + opts.key;

  let st = {
    rect: opts.def(),
    collapsed: false,
    hidden: !!opts.startClosed,
    zoomRestore: null,
  };
  try {
    // geometry and collapse persist; "closed" is session-only, so panels
    // come back in their opening state on the next launch
    const saved = JSON.parse(localStorage.getItem(store) || "null");
    if (saved && saved.rect) {
      st.rect = saved.rect;
      st.collapsed = !!saved.collapsed;
    }
  } catch (_) { /* private mode etc. */ }

  function persist() {
    try {
      localStorage.setItem(
        store,
        JSON.stringify({ rect: st.rect, collapsed: st.collapsed })
      );
    } catch (_) { /* ignore */ }
  }

  function apply() {
    el.style.left = st.rect.x + "px";
    el.style.top = st.rect.y + "px";
    el.style.width = st.rect.w + "px";
    if (st.collapsed || st.rect.h == null) {
      el.style.height = "";
      el.style.maxHeight = Math.max(120, stage.clientHeight - st.rect.y - 12) + "px";
    } else {
      el.style.height = st.rect.h + "px";
      el.style.maxHeight = "";
    }
    el.classList.toggle("collapsed", st.collapsed);
    el.classList.toggle("hidden", st.hidden);
    if (opts.toolbarBtn) opts.toolbarBtn.classList.toggle("active", !st.hidden);
  }

  function clampToStage() {
    const sw = stage.clientWidth;
    const sh = stage.clientHeight;
    st.rect.w = Math.min(Math.max(st.rect.w, opts.minW), opts.maxW, sw - 20);
    st.rect.x = clamp(st.rect.x, 6 - st.rect.w + 60, sw - 60);
    st.rect.y = clamp(st.rect.y, 6, Math.max(6, sh - 34));
    apply();
  }

  function focus() {
    document.querySelectorAll(".mac-window").forEach((w) => w.classList.remove("focused"));
    el.classList.add("focused");
    el.style.zIndex = ++winZ;
  }

  el.addEventListener("pointerdown", focus);

  // -- dragging by the title bar
  let dragFrom = null;
  titlebar.addEventListener("pointerdown", (ev) => {
    if (ev.target.closest(".tl")) return;
    dragFrom = { px: ev.clientX, py: ev.clientY, x: st.rect.x, y: st.rect.y };
    el.classList.add("dragging");
    titlebar.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  titlebar.addEventListener("pointermove", (ev) => {
    if (!dragFrom) return;
    st.rect.x = dragFrom.x + (ev.clientX - dragFrom.px);
    st.rect.y = dragFrom.y + (ev.clientY - dragFrom.py);
    clampToStage();
  });
  titlebar.addEventListener("pointerup", () => {
    dragFrom = null;
    el.classList.remove("dragging");
    persist();
  });
  titlebar.addEventListener("dblclick", (ev) => {
    if (ev.target.closest(".tl")) return;
    setCollapsed(!st.collapsed);
  });

  // -- resizing from edges and corners
  for (const dir of ["n", "s", "e", "w", "ne", "nw", "se", "sw"]) {
    const h = document.createElement("div");
    h.className = "rz rz-" + dir;
    el.append(h);
    let from = null;
    h.addEventListener("pointerdown", (ev) => {
      from = {
        px: ev.clientX,
        py: ev.clientY,
        x: st.rect.x,
        y: st.rect.y,
        w: st.rect.w,
        h: st.rect.h != null ? st.rect.h : el.offsetHeight,
      };
      el.classList.add("resizing");
      focus();
      h.setPointerCapture(ev.pointerId);
      ev.preventDefault();
      ev.stopPropagation();
    });
    h.addEventListener("pointermove", (ev) => {
      if (!from) return;
      const dx = ev.clientX - from.px;
      const dy = ev.clientY - from.py;
      const r = { ...st.rect, h: from.h };
      if (dir.includes("e")) r.w = from.w + dx;
      if (dir.includes("s")) r.h = from.h + dy;
      if (dir.includes("w")) { r.w = from.w - dx; r.x = from.x + dx; }
      if (dir.includes("n")) { r.h = from.h - dy; r.y = from.y + dy; }
      if (r.w < opts.minW) { if (dir.includes("w")) r.x -= opts.minW - r.w; r.w = opts.minW; }
      if (r.w > opts.maxW) { if (dir.includes("w")) r.x += r.w - opts.maxW; r.w = opts.maxW; }
      if (r.h < opts.minH) { if (dir.includes("n")) r.y -= opts.minH - r.h; r.h = opts.minH; }
      st.rect = r;
      apply();
      if (opts.onResize) opts.onResize();
    });
    h.addEventListener("pointerup", () => {
      from = null;
      el.classList.remove("resizing");
      persist();
    });
  }

  // -- traffic lights
  function setCollapsed(on) {
    st.collapsed = on;
    apply();
    persist();
  }
  function close(silent) {
    st.hidden = true;
    apply();
    if (!silent) persist();
  }
  function open() {
    st.hidden = false;
    st.collapsed = false;
    apply();
    clampToStage();
    focus();
    persist();
    if (opts.onResize) opts.onResize();
    if (opts.onOpen) opts.onOpen();
  }
  el.querySelector(".tl-close").addEventListener("click", () => close(false));
  el.querySelector(".tl-min").addEventListener("click", () => setCollapsed(!st.collapsed));
  el.querySelector(".tl-zoom").addEventListener("click", () => {
    if (st.zoomRestore) {
      st.rect = st.zoomRestore;
      st.zoomRestore = null;
    } else {
      st.zoomRestore = { ...st.rect };
      st.rect = { ...st.rect, w: opts.zoomW, h: null };
    }
    st.collapsed = false;
    clampToStage();
    persist();
    if (opts.onResize) opts.onResize();
  });

  if (opts.toolbarBtn) {
    opts.toolbarBtn.addEventListener("click", () => (st.hidden ? open() : close(false)));
  }

  const api = {
    el,
    open,
    close,
    clampToStage,
    fitContent() {
      st.rect.h = null;
      st.collapsed = false;
      apply();
      persist();
    },
    isHidden: () => st.hidden,
    bodyWidth: () => Math.max(180, (body.clientWidth || st.rect.w - 2) - 24),
  };
  apply();
  clampToStage();
  return api;
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
  const f = plateFrameParams();
  return "T" + f.t + " · P" + f.p + " · Z" + f.z;
}

function platePlaneNote() {
  const pl = state.plate;
  const info = state.info.plate;
  const bits = [];
  if (pl.focus !== null && info.sites[pl.focus]) bits.push(siteLabelText(info.sites[pl.focus].name));
  bits.push("t " + (pl.t + 1) + "/" + info.T);
  bits.push("z " + (plateZFor(pl.focus === null ? 0 : pl.focus) + 1) + "/" + info.Z + (pl.auto ? " auto" : ""));
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
  pl.placed.forEach((s) => {
    const el = pl.gridEls[s.i];
    el.style.gridRow = s.row + 1;
    el.style.gridColumn = s.col + 1;
  });
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
    const trash = $("trash-confirm");
    if (trash) trash.hidden = true;
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
  if (!pl) return;
  const info = state.info.plate;
  const wrap = $("stage-wrap");
  wrap.classList.add("plate-grid");

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
  refreshTiles();
  renderZSlider();
  renderTimeLine();
  refreshHistograms();
  $("plane-note").textContent = platePlaneNote();
  if (state.inspect && !state.windows.info.isHidden()) renderSlideInspector(state.inspect);
}

function setPlateZ(z) {
  // reaching for the plane is how the user takes the wheel back from
  // autofocus, so this turns it off
  const pl = state.plate;
  const next = clamp(Math.round(z), 0, state.info.plate.Z - 1);
  const wasAuto = pl.auto;
  if (next === pl.z && !wasAuto) return;
  pl.auto = false;
  if (wasAuto) {
    try { localStorage.setItem("nd2wsi.plate.autofocus", "0"); } catch (_) { /* private mode */ }
  }
  pl.z = next;
  if (wasAuto) {
    renderPlateAuto();
    paintPlate();
  }
  plateFrameChanged();
}

function stepPlateZ(delta) {
  // with autofocus on, the plane on screen is the site's own, so a step
  // moves from there rather than from the plane the user last set by hand
  const pl = state.plate;
  const from = pl.auto ? plateZFor(pl.focus === null ? 0 : pl.focus) : pl.z;
  setPlateZ(from + delta);
}

function setPlateAuto(on) {
  const pl = state.plate;
  const next = state.info.plate.Z > 1 && !!on;
  if (next === pl.auto) return;
  pl.auto = next;
  try { localStorage.setItem("nd2wsi.plate.autofocus", next ? "1" : "0"); } catch (_) { /* private mode */ }
  renderPlateAuto();
  paintPlate();
  plateFrameChanged();
}

function setPlateT(t) {
  const pl = state.plate;
  const next = clamp(Math.round(t), 0, state.info.plate.T - 1);
  if (next === pl.t) return;
  pl.t = next;
  plateFrameChanged();
}

function setPlateFocus(p) {
  const pl = state.plate;
  const info = state.info.plate;
  const next = p === null || p === undefined ? null : clamp(Number(p), 0, info.P - 1);
  if (next === pl.focus) return;
  const before = plateFrameParams().p;
  // an open text edit belongs to the site being left, so it is committed
  // first. The debounced save is then called off, because it would fire
  // after the switch and write these marks into the next site's sidecar
  if (state.editingId) closeEditor(true);
  if (state.annDirty) {
    scheduleAnnSave.cancel();
    saveAnnotations(before);
  }
  state.annContext += 1;
  state.annDirty = false;
  pl.focus = next;
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
    if (state.tool) setTool(state.tool); // hand the mouse back for the next focus
    setPlatePlaying(false);
    layoutPlate();
  }
  if (plateFrameParams().p !== before) {
    state.annotations = [];
    renderAnnotations();
    rebuildAnnList();
    loadAnnotations();
  }
  if (pl.resetWheel) pl.resetWheel(); // the two levels do not share a stroke
  paintPlate(); // the set that just became visible carries older frames
  plateFrameChanged();
  updateReadout();
}

function setPlatePlaying(on) {
  const pl = state.plate;
  pl.playing = state.info.plate.T > 1 && !!on;
  const btn = $("t-play");
  btn.classList.toggle("on", pl.playing);
  btn.title = pl.playing ? "Pause (space)" : "Play (space)";
  btn.innerHTML = pl.playing
    ? '<svg viewBox="0 0 16 16"><path d="M3.5 2.5h3v11h-3zM9.5 2.5h3v11h-3z"/></svg>'
    : '<svg viewBox="0 0 16 16"><path d="M4 2.2v11.6c0 .6.6.9 1.1.6l8.6-5.8c.5-.3.5-1 0-1.3L5.1 1.6C4.6 1.3 4 1.6 4 2.2z"/></svg>';
  clearInterval(pl.timer);
  pl.timer = null;
  if (pl.playing) {
    pl.timer = setInterval(() => {
      const T = state.info.plate.T;
      setPlateT(pl.t + 1 >= T ? 0 : pl.t + 1);
    }, 1000 / pl.fps);
  }
}

/* z slider: a track that fills up to the knob, a tick per plane, the home
   plane marked, a value capsule beside the knob */

function zSliderPct(z) {
  const Z = state.info.plate.Z;
  return Z > 1 ? (1 - z / (Z - 1)) * 100 : 50;
}

function buildZSlider() {
  const pl = state.plate;
  const info = state.info.plate;
  const ticks = $("z-ticks");
  pl.zTickEls = [];
  for (let i = 0; i < info.Z; i++) {
    const tick = document.createElement("div");
    tick.className = "ztick" + (i === info.zHome ? " home" : "");
    tick.style.top = zSliderPct(i) + "%";
    ticks.append(tick);
    pl.zTickEls.push(tick);
  }
  const slider = $("z-slider");
  const track = $("z-track");
  const knob = $("z-knob");
  const zFromY = (y) => {
    const r = track.getBoundingClientRect();
    if (!r.height) return;
    const f = clamp((y - r.top) / r.height, 0, 1);
    setPlateZ(Math.round((1 - f) * (info.Z - 1)));
  };
  let drag = false;
  knob.addEventListener("pointerdown", (ev) => {
    drag = true;
    try { knob.setPointerCapture(ev.pointerId); } catch (_) { /* optional */ }
    ev.preventDefault();
  });
  knob.addEventListener("pointermove", (ev) => { if (drag) zFromY(ev.clientY); });
  knob.addEventListener("pointerup", () => { drag = false; });
  knob.addEventListener("pointercancel", () => { drag = false; });
  slider.addEventListener("pointerdown", (ev) => {
    if (ev.target !== knob && ev.target.id !== "z-label") zFromY(ev.clientY);
  });
  knob.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowUp") { stepPlateZ(1); ev.preventDefault(); ev.stopPropagation(); }
    else if (ev.key === "ArrowDown") { stepPlateZ(-1); ev.preventDefault(); ev.stopPropagation(); }
  });
  knob.setAttribute("aria-valuemin", "1");
  knob.setAttribute("aria-valuemax", String(info.Z));
}

function renderZSlider() {
  const pl = state.plate;
  const info = state.info.plate;
  if (!pl.zTickEls) return;
  // with autofocus on, the slider reports the plane of the site in view
  const shownZ = pl.auto ? plateZFor(pl.focus === null ? 0 : pl.focus) : pl.z;
  const pct = zSliderPct(shownZ);
  // the track is inset 10 px inside the slider, so the knob and the label
  // follow the track's own extent
  const slider = $("z-slider");
  const track = $("z-track");
  const sr = slider.getBoundingClientRect();
  const tr = track.getBoundingClientRect();
  const top = sr.height ? (tr.top - sr.top) + (pct / 100) * tr.height : 0;
  pl.zTickEls.forEach((tick, i) => {
    tick.classList.toggle("on", i === shownZ);
    tick.style.top = (sr.height ? (tr.top - sr.top) + (zSliderPct(i) / 100) * tr.height - 10 : 0) + "px";
  });
  const knob = $("z-knob");
  knob.style.top = top + "px";
  knob.setAttribute("aria-valuenow", String(shownZ + 1));
  $("z-slider").classList.toggle("auto", !!pl.auto);
  $("z-fill").style.height = ((1 - pct / 100) * (tr.height || 0)) + "px";
  const label = $("z-label");
  label.style.top = top + "px";
  label.replaceChildren();
  label.append((shownZ + 1) + " of " + info.Z);
  const dim = document.createElement("span");
  dim.className = "dim";
  let text = "";
  if (Number(info.zStepUm) > 0) {
    const d = (shownZ - info.zHome) * Number(info.zStepUm);
    text += " · " + (d < 0 ? "−" : "+") + rawValueLabel(Math.abs(d)) + " µm";
  }
  if (pl.auto) text += " · auto";
  else if (shownZ === info.zHome) text += " · home";
  dim.textContent = text;
  label.append(dim);
}

/* the transport capsule: buttons, a scrubber with ticks at the real frame
   times and a cached band, the clock, the speed */

function timeSpanMs() {
  const times = state.info.plate.timesMs || [];
  return times.length ? Math.max(0, times[times.length - 1] - times[0]) : 0;
}

function timePct(t) {
  const times = state.info.plate.timesMs || [];
  const span = timeSpanMs();
  if (!span || !times.length) return 0;
  return ((times[t] - times[0]) / span) * 100;
}

function buildTimeLine() {
  const pl = state.plate;
  const info = state.info.plate;
  const ticks = $("t-ticks");
  const cached = $("t-cached");
  pl.tickEls = [];
  pl.cachedEls = [];
  const showTicks = info.T <= 120;
  for (let i = 0; i < info.T; i++) {
    if (showTicks) {
      const tick = document.createElement("div");
      tick.className = "ttick";
      tick.style.left = timePct(i) + "%";
      ticks.append(tick);
      pl.tickEls.push(tick);
    }
    const band = document.createElement("span");
    const from = timePct(i);
    const to = i + 1 < info.T ? timePct(i + 1) : 100;
    band.style.left = from + "%";
    band.style.width = Math.max(0, to - from) + "%";
    band.hidden = true;
    cached.append(band);
    pl.cachedEls.push(band);
  }
  // hour labels every 6 h on a long run, every 30 min on a short one
  const labels = $("t-labels");
  const spanMin = timeSpanMs() / 60000;
  if (spanMin > 0) {
    const stepMin = spanMin > 120 ? 360 : 30;
    for (let m = 0; m <= spanMin + 1e-6; m += stepMin) {
      const lab = document.createElement("div");
      const last = m + stepMin > spanMin;
      lab.className = "tlabel" + (m === 0 ? " first" : last ? " last" : "");
      lab.style.left = (m / spanMin) * 100 + "%";
      lab.textContent = m === 0 ? "0" : fmtTickLabel(m);
      labels.append(lab);
    }
  }
  const track = $("t-track");
  const tFromX = (x) => {
    const r = track.getBoundingClientRect();
    if (!r.width) return;
    const times = info.timesMs || [];
    const target = times[0] + clamp((x - r.left) / r.width, 0, 1) * timeSpanMs();
    let best = 0;
    for (let i = 1; i < info.T; i++) {
      if (Math.abs(times[i] - target) < Math.abs(times[best] - target)) best = i;
    }
    setPlateT(best);
  };
  let drag = false;
  track.addEventListener("pointerdown", (ev) => {
    drag = true;
    try { track.setPointerCapture(ev.pointerId); } catch (_) { /* optional */ }
    tFromX(ev.clientX);
    ev.preventDefault();
  });
  track.addEventListener("pointermove", (ev) => { if (drag) tFromX(ev.clientX); });
  track.addEventListener("pointerup", () => { drag = false; });
  track.addEventListener("pointercancel", () => { drag = false; });

  $("t-first").onclick = () => setPlateT(0);
  $("t-last").onclick = () => setPlateT(info.T - 1);
  $("t-prev").onclick = () => setPlateT(pl.t - 1);
  $("t-next").onclick = () => setPlateT(pl.t + 1);
  $("t-play").onclick = () => setPlatePlaying(!pl.playing);
  const speed = $("t-speed");
  speed.querySelectorAll("button").forEach((b) => {
    b.disabled = info.T <= 1;
    b.onclick = () => {
      speed.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      pl.fps = Number(b.dataset.fps) || 8;
      if (pl.playing) setPlatePlaying(true);
    };
  });
  $("t-play").disabled = info.T <= 1;
}

function renderPlateAuto() {
  const pl = state.plate;
  const btn = $("t-auto");
  if (!pl || !btn) return;
  const measured = pl.focusMap ? Number(pl.focusMap.measured) || 0 : 0;
  btn.classList.toggle("on", !!pl.auto);
  btn.setAttribute("aria-pressed", pl.auto ? "true" : "false");
  btn.disabled = state.info.plate.Z <= 1 || measured === 0;
  btn.title = state.info.plate.Z <= 1
    ? "Autofocus needs more than one Z plane"
    : measured === 0
    ? "Autofocus becomes ready as the store measures the planes"
    : pl.auto
      ? "Autofocus on: every site shows its sharpest plane (F)"
      : "Show every site at its sharpest plane (F)";
}

function loadPlateFocus() {
  // the sharpest plane of every site at every time point, measured from the
  // store's own reductions. An extra, so a failure only leaves it off
  const pl = state.plate;
  if (!pl) return;
  fetch("api/plate/focus", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
    .then((map) => {
      if (!state.plate || !map || !Array.isArray(map.best)) return;
      // the map keeps growing while the planes are measured, so a repaint
      // only happens when the planes on screen actually move; reloading the
      // deep-zoom stage on every poll would blank it on a two second beat
      const site = pl.focus === null ? 0 : pl.focus;
      const wasZ = pl.auto ? plateZFor(site) : null;
      const wasRow = pl.auto && pl.focusMap ? String(pl.focusMap.best[pl.t]) : null;
      pl.focusMap = map;
      renderPlateAuto();
      if (!pl.auto || String(map.best[pl.t]) === wasRow) return;
      paintPlate();
      if (plateZFor(site) !== wasZ) plateFrameChanged();
      else { renderZSlider(); renderTimeLine(); }
    })
    .catch(() => {});
}

function pollPlateStatus() {
  // the store beside the file fills in the background; a band under the
  // scrubber shows which time points are in, and the status bar counts,
  // so the series can be scrubbed without touching the ND2 once it is done
  const pl = state.plate;
  if (!pl || pl.statusTimer) return;
  const cell = $("plate-cache-cell");
  const ask = () => {
    fetch("api/plate/status", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
      .then((d) => {
        pl.storePerT = Array.isArray(d.perT) ? d.perT : null;
        pl.storeDone = Number(d.done) || 0;
        pl.storeTotal = Number(d.total) || 0;
        renderTimeLine();
        const done = pl.storeTotal && pl.storeDone >= pl.storeTotal;
        if (cell) {
          cell.hidden = done || !pl.storeTotal;
          $("plate-cache-val").textContent = pl.storeTotal
            ? Math.floor((pl.storeDone / pl.storeTotal) * 100) + " % · " + fmtInt(pl.storeDone) + " of " + fmtInt(pl.storeTotal)
            : "";
        }
        const map = pl.focusMap;
        const focusNeeded = state.info.plate.Z > 1;
        const measured = map && map.measured >= map.total;
        if (focusNeeded && !measured) loadPlateFocus();
        if (done && (!focusNeeded || measured)) {
          clearInterval(pl.statusTimer);
          pl.statusTimer = null;
        }
      })
      .catch(() => {});
  };
  ask();
  pl.statusTimer = setInterval(ask, 2000);
  window.addEventListener("pagehide", () => { clearInterval(pl.statusTimer); pl.statusTimer = null; }, { once: true });
}

function renderTimeLine() {
  const pl = state.plate;
  const info = state.info.plate;
  const times = info.timesMs || [];
  const perFrame = (info.P || 1) * (info.Z || 1);
  (pl.tickEls || []).forEach((tick, i) => tick.classList.toggle("on", i === pl.t));
  (pl.cachedEls || []).forEach((band, i) => {
    const seen = pl.loaded.get(i + "/" + plateGroupKey());
    const stored = pl.storePerT && pl.storePerT[i] >= perFrame;
    band.hidden = !(stored || !!(seen && seen.size >= info.P));
  });
  const pct = timePct(pl.t);
  $("t-playhead").style.left = pct + "%";
  $("t-fill").style.width = pct + "%";
  $("t-read-clock").textContent = fmtHm(times.length ? times[pl.t] - times[0] : 0);
  const period = platePeriodMs();
  $("t-read-frame").textContent = "frame " + (pl.t + 1) + " of " + info.T +
    (period ? " · every " + fmtPeriodWords(period) : "");
  $("t-prev").disabled = pl.t === 0;
  $("t-first").disabled = pl.t === 0;
  $("t-next").disabled = pl.t >= info.T - 1;
  $("t-last").disabled = pl.t >= info.T - 1;
  $("t-play").disabled = info.T <= 1;
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
    if (target.closest("#time-line, #plate-strip, #plate-back, .mac-window")) return false;
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
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (state.landmark.active) return;
    const compare = state.viewportRelay.compare;
    if (compare && compare.enabled && compare.linked) return;
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
    else if ((ev.key === "f" || ev.key === "F") && info.Z > 1) {
      if (!$("t-auto").disabled) setPlateAuto(!pl.auto);
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

function wireKeys() {
  window.addEventListener("keydown", (ev) => {
    const shortcuts = window.Nd2ShortcutRouter;
    if (shortcuts && shortcuts.isTypingEvent(ev)) return;
    if (!shortcuts && /^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    if (ev.target.closest?.("#tb-plate-view, #plate-view-menu")) return;
    const command = ev.metaKey || ev.ctrlKey;
    const letterCode = shortcuts ? shortcuts.letterCode(ev) : ev.code;
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
    if (command && !ev.shiftKey && letterCode === "KeyI") {
      ev.preventDefault();
      $("tb-info").click();
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
    if (command) return;  // leave other shortcuts alone
    const panel = shortcuts ? shortcuts.panelForEvent(ev) : null;
    if (panel) {
      const btn = $({ channels: "tb-channels", region: "tb-region", annot: "tb-annot" }[panel]);
      if (btn && !btn.disabled) {
        ev.preventDefault();
        btn.click();
      }
      return;
    }
    const plain = !ev.altKey && !ev.shiftKey && !ev.repeat;
    if (plain && letterCode === "KeyV") { if (state.roi) setTool("move"); }
    else if (plain && letterCode === "KeyM") setTool("measure");
    else if (plain && letterCode === "KeyP") setTool("pin");
    else if (plain && letterCode === "KeyB") setTool("box");
    else if (plain && letterCode === "KeyI") togglePixelInspector();
    else if (plain && letterCode === "KeyL" && window.parent !== window) {
      if (!ev.repeat) {
        ev.preventDefault();
        window.parent.postMessage(
          { nd2wsi: "compare-link-toggle", version: VIEWPORT_PROTOCOL_VERSION },
          location.origin
        );
      }
    }
    else if (ev.key === "Escape") {
      if (!$("ann-editor").hidden) closeEditor(false);
      else if (state.tool) setTool(state.tool); // toggles off
      else if (state.plate && state.plate.focus !== null) setPlateFocus(null);
    } else if (plain && ["Digit0", "Numpad0"].includes(ev.code)) state.viewer.viewport.goHome();
  });
}

function elementPoint(ev, el) {
  const r = el.getBoundingClientRect();
  return { x: ev.clientX - r.left, y: ev.clientY - r.top };
}
function rectFrom(a, b) {
  return {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    w: Math.abs(a.x - b.x),
    h: Math.abs(a.y - b.y),
  };
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const fmtInt = (v) => Math.round(v).toLocaleString("en-US");
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
