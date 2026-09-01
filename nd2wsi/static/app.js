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
  wireTheme();
  wireTrash();
  wireDragForward();
  buildChannelPanel();
  buildLevelLamps();
  buildViewer();
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

function fillContextBadges(info) {
  const name = String(info.name || "").toLowerCase();
  $("source-badge").textContent = name.endsWith(".svs")
    ? "SVS"
    : name.endsWith(".nd2")
      ? "ND2"
      : "OME-ZARR";

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

function tileQuery() {
  const q = new URLSearchParams();
  if (state.channels.length !== state.info.channels.length)
    q.set("c", state.channels.join(","));
  const win = lutParam();
  if (win) q.set("win", win);
  // the cache generation makes tile URLs immutable: the browser may keep
  // them, and a rebuilt cache changes the URLs instead of serving stale
  if (state.info.generation) q.set("g", state.info.generation);
  const s = q.toString();
  return s ? "?" + s : "";
}

function makeTileSource() {
  const info = state.info;
  // server levels: index 0 = full resolution; OSD wants level 0 = smallest.
  const lv = info.levels.slice().reverse();
  const q = tileQuery();
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
      return "api/tile/" + lv[l].path + "/" + x + "/" + y + ".jpg" + q;
    },
  };
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
    navigatorDisplayRegionColor: "#ffffff",
    animationTime: 0.6,
    springStiffness: 8,
    zoomPerScroll: 1.4,
    minZoomImageRatio: 0.7,
    // hardware-matched overzoom: cap at ~3 DEVICE pixels per image pixel,
    // whatever the display density (OSD works in CSS pixels here)
    maxZoomPixelRatio: Math.max(1.25, 3 / (window.devicePixelRatio || 1)),
    imageSmoothingEnabled: true,
    crossOriginPolicy: false,
    drawer: "canvas",
  });
  state.viewer = viewer;

  viewer.addHandler("open", () => {
    $("boot").style.display = "none";
    updateReadout();
    restoreRoiOverlay();
  });
  viewer.addHandler("animation", updateReadout);
  viewer.addHandler("animation-finish", updateReadout);
  viewer.addHandler("update-viewport", renderAnnotations);
  viewer.addHandler("open", renderAnnotations);
  viewer.addHandler("open-failed", () => showToast("could not open tile source"));
  viewer.addHandler("tile-load-failed", debounceToast("some tiles failed to load"));

  const stage = $("stage");
  stage.addEventListener("mousemove", (ev) => {
    const pt = elementPoint(ev, stage);
    const img = viewer.viewport.viewerElementToImageCoordinates(
      new OpenSeadragon.Point(pt.x, pt.y)
    );
    updateCursor(img);
  });
  stage.addEventListener("mouseleave", () => updateCursor(null));

  $("zoom-in").onclick = () => viewer.viewport.zoomBy(1.6).applyConstraints();
  $("zoom-out").onclick = () => viewer.viewport.zoomBy(1 / 1.6).applyConstraints();
  $("zoom-fit").onclick = () => viewer.viewport.goHome();
}

function reopenPreservingView() {
  const viewer = state.viewer;
  const bounds = viewer.viewport.getBounds();
  viewer.addOnceHandler("open", () => {
    viewer.viewport.fitBounds(bounds, true);
    restoreRoiOverlay();
  });
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
    auto.title = "Auto-adjust window (0.1–99.9 percentile)";
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
      reopenPreservingView();
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

const applyLuts = debounce(() => reopenPreservingView(), 250);

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
  let vmax = Math.max(def.hi * 2, 1); // provisional until the histogram loads
  let bins = null;

  const vx = (v) => PLOT.x0 + (Math.max(0, Math.min(v, vmax)) / vmax) * (PLOT.x1 - PLOT.x0);
  const xv = (x) =>
    (Math.max(0, Math.min(1, (x - PLOT.x0) / (PLOT.x1 - PLOT.x0)))) * vmax;
  const curveY = (t) =>
    PLOT.y1 - Math.pow(Math.max(0, Math.min(1, t)), 1 / cur.gamma) * (PLOT.y1 - PLOT.y0);
  const knobPos = () => ({
    x: vx(cur.lo + (Math.max(cur.hi, cur.lo + 1) - cur.lo) * 0.5),
    y: curveY(Math.pow(0.5, 1 / cur.gamma)),
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
    ctx.fillText("0", PLOT.x0, H - 3);
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
      vmax = hg.vmax;
      draw();
    },
    reset() {
      setLut({ ...def });
    },
    auto() {
      // NIS-style auto scale: stretch the window to the 0.1–99.9 percentile
      // of the histogram; gamma is left as set
      if (!bins) return;
      const total = bins.reduce((a, b) => a + b, 0);
      if (!total) return;
      const bw = vmax / bins.length;
      let acc = 0;
      let loB = 0;
      for (let b = 0; b < bins.length; b++) {
        acc += bins[b];
        if (acc >= total * 0.001) { loB = b; break; }
      }
      acc = 0;
      let hiB = bins.length - 1;
      for (let b = 0; b < bins.length; b++) {
        acc += bins[b];
        if (acc >= total * 0.999) { hiB = b; break; }
      }
      setLut({ lo: loB * bw, hi: Math.max((hiB + 1) * bw, loB * bw + 1), gamma: cur.gamma });
    },
  };
  state.lutWidgets[i] = widget;
  relayout(state.windows.channels.bodyWidth());
  return wrap;
}

function relayoutLuts() {
  const w = state.windows.channels.bodyWidth();
  state.lutWidgets.forEach((wd) => wd && wd.relayout(w));
}

function loadHistograms() {
  fetch("api/histogram")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
    .then((d) => {
      d.channels.forEach((hg, i) => {
        if (state.lutWidgets[i]) state.lutWidgets[i].setHistogram(hg);
      });
    })
    .catch(() => {}); // panel still works without histograms
}

function debounce(fn, ms) {
  let t = null;
  return () => {
    clearTimeout(t);
    t = setTimeout(fn, ms);
  };
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

function activeLevel() {
  // the coarsest level whose scale still meets the DEVICE sampling density
  const iz = Math.min(deviceZoom(), 1);
  const levels = state.info.levels; // 0 = full res
  for (let k = levels.length - 1; k >= 0; k--) {
    if (1 / levels[k].downsample >= iz) return k;
  }
  return 0;
}

function updateReadout() {
  const dz = deviceZoom();
  $("zoom-val").textContent =
    (dz * 100).toFixed(dz >= 0.1 ? 0 : 1) + " %" + (Math.abs(dz - 1) < 0.005 ? " · 1:1" : "");
  const act = activeLevel();
  state.info.levels.forEach((lv, k) => {
    $("lamp-" + lv.path).classList.toggle("active", k === act);
  });
  updateScalebar(currentImageZoom());
}

function updateCursor(img) {
  if (!img || img.x < 0 || img.y < 0 || img.x > state.info.width || img.y > state.info.height) {
    $("pos-um").textContent = "–";
    $("pos-px").textContent = "–";
    return;
  }
  const ps = pixelSize();
  $("pos-um").textContent = ps ? fmtUm(img.x * ps[1]) + " , " + fmtUm(img.y * ps[0]) : "uncalibrated";
  $("pos-px").textContent = fmtInt(img.x) + " , " + fmtInt(img.y) + " px";
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
  const p = state.viewer.viewport.viewerElementToImageCoordinates(
    new OpenSeadragon.Point(pt.x, pt.y)
  );
  return {
    x: clamp(p.x, 0, state.info.width),
    y: clamp(p.y, 0, state.info.height),
  };
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
  stage.addEventListener("pointerdown", (ev) => {
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
        const r = {
          x: Math.min(startImg.x, endImg.x),
          y: Math.min(startImg.y, endImg.y),
          w: Math.abs(endImg.x - startImg.x),
          h: Math.abs(endImg.y - startImg.y),
        };
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
  renderAnnotations();
  rebuildAnnList();
  scheduleAnnSave();
}

const scheduleAnnSave = debounce(saveAnnotations, 800);

function saveAnnotations() {
  if (annLocked()) {
    setAnnStatus("Not saved: annotation is locked in this degraded view");
    return;
  }
  fetch("api/annotations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: state.annotations }),
  })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
    .then((d) => setAnnStatus("Saved · " + basename(d.path)))
    .catch((e) => setAnnStatus("Save failed: " + e.message));
}

function loadAnnotations() {
  fetch("api/annotations")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
    .then((d) => {
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
  return state.viewer.viewport.imageToViewerElementCoordinates(
    new OpenSeadragon.Point(x, y)
  );
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
      const p1 = toEl(a.x, a.y);
      const p2 = toEl(a.x + a.w, a.y + a.h);
      const g = svgEl("g", { class: "hit" }, layer);
      svgEl("rect", {
        x: p1.x, y: p1.y, width: Math.max(1, p2.x - p1.x), height: Math.max(1, p2.y - p1.y),
        fill: hexAlpha(annColor(a), 0.08), stroke: annColor(a), "stroke-width": 1.5, rx: 2,
      }, g);
      g.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); openEditor(a.id); });
      chip(layer, p1.x, p1.y - 22, a.text || "Box", a.id);
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
    const blob = new Blob(
      [JSON.stringify(annotationDocument(), null, 1)],
      { type: "application/json" }
    );
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = state.info.name.replace(/\.(nd2|svs)$/i, "") + ".annotations.json";
    a.click();
    URL.revokeObjectURL(a.href);
  };
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
    selection: state.info.selection || {},
    items: state.annotations,
  };
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
    const expected = normalizedSelection(state.info.selection);
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
  const vp = state.viewer.viewport;
  const p1 = vp.viewerElementToImageCoordinates(new OpenSeadragon.Point(a.x, a.y));
  const p2 = vp.viewerElementToImageCoordinates(new OpenSeadragon.Point(b.x, b.y));
  const info = state.info;
  const x0 = clamp(Math.min(p1.x, p2.x), 0, info.width);
  const y0 = clamp(Math.min(p1.y, p2.y), 0, info.height);
  const x1 = clamp(Math.max(p1.x, p2.x), 0, info.width);
  const y1 = clamp(Math.max(p1.y, p2.y), 0, info.height);
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

/* ---- appearance ------------------------------------------------------------
   Auto follows the system for fluorescence and opens RGB brightfield in light
   mode. An explicit light or dark choice is never overwritten by the slide. */

const THEME_MODES = ["auto", "light", "dark"];
const THEME_MODE_KEY = "nd2wsi.theme.mode";
const LEGACY_THEME_KEY = "nd2wsi.theme";

function currentTheme() {
  return document.documentElement.classList.contains("light") ? "light" : "dark";
}

function savedThemeMode() {
  // the pre-1.1 key recorded every automatic theme flip, not a choice the
  // user made, so upgraders start in Auto rather than pinned to whatever
  // the old build last wrote
  try {
    const mode = localStorage.getItem(THEME_MODE_KEY);
    if (THEME_MODES.includes(mode)) return mode;
  } catch (e) { /* private mode */ }
  return "auto";
}

function resolveTheme(mode) {
  if (mode === "light" || mode === "dark") return mode;
  if (state.info && state.info.rgb) return "light";
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
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

function applyThemeMode(mode, resolved = null, persist = true) {
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

  if (persist) {
    try {
      localStorage.setItem(THEME_MODE_KEY, mode);
      localStorage.setItem(LEGACY_THEME_KEY, theme);
    } catch (e) { /* private mode */ }
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
  let mode = savedThemeMode();
  let theme = applyThemeMode(mode);
  announceTheme(mode, theme);

  $("tb-theme").onclick = () => {
    mode = THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
    theme = applyThemeMode(mode);
    announceTheme(mode, theme);
  };

  const media = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)");
  if (media && media.addEventListener) {
    media.addEventListener("change", () => {
      if (mode !== "auto") return;
      theme = applyThemeMode(mode, null, false);
      announceTheme(mode, theme);
    });
  }

  window.addEventListener("message", (ev) => {
    if (ev.origin !== location.origin || !ev.data) return;
    if (ev.data.nd2wsi === "theme-request") {
      announceTheme(mode, theme);
      return;
    }
    if (ev.data.nd2wsi !== "theme") return;
    mode = THEME_MODES.includes(ev.data.mode) ? ev.data.mode : mode;
    theme = applyThemeMode(mode, ev.data.theme || null);
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
  btn.onclick = () => { pop.hidden = !pop.hidden; };
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
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    const k = ev.key.toLowerCase();
    if (k === "r") setTool("roi");
    else if (k === "v") { if (state.roi) setTool("move"); }
    else if (k === "m") setTool("measure");
    else if (k === "p") setTool("pin");
    else if (k === "b") setTool("box");
    else if (ev.key === "Escape") {
      if (!$("ann-editor").hidden) closeEditor(false);
      else if (state.tool) setTool(state.tool); // toggles off
    } else if (ev.key === "0") state.viewer.viewport.goHome();
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
