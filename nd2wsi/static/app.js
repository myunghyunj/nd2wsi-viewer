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
  const res = await fetch("/api/info");
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
  $("plane-note").textContent = planeNote(info);

  const dpr = window.devicePixelRatio || 1;
  if (dpr > 1) {
    $("hw-cell").hidden = false;
    $("hw-val").textContent = "Retina " + (Math.round(dpr * 10) / 10) + "×";
  }

  buildWindows();
  buildChannelPanel();
  buildLevelLamps();
  buildViewer();
  wireTools();
  wireKeys();
  loadAnnotations();
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
      return "/api/tile/" + lv[l].path + "/" + x + "/" + y + ".jpg" + q;
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
    ctx.strokeStyle = "rgba(255,255,255,0.12)";
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
    ctx.strokeStyle = "rgba(255,255,255,0.30)";
    ctx.setLineDash([2, 3]);
    for (const v of [cur.lo, cur.hi]) {
      ctx.beginPath();
      ctx.moveTo(vx(v) + 0.5, PLOT.y0);
      ctx.lineTo(vx(v) + 0.5, PLOT.y1);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    // mapping curve: flat-left, gamma ramp, flat-right
    ctx.strokeStyle = "rgba(255,255,255,0.85)";
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
    ctx.fillStyle = "#1e1e20";
    ctx.fill();
    ctx.strokeStyle = "rgba(255,255,255,0.85)";
    ctx.stroke();
    // lo/hi triangles along the top edge
    triangle(vx(cur.lo), "#111214", "rgba(255,255,255,0.55)");
    triangle(vx(cur.hi), "rgba(255,255,255,0.92)", "rgba(0,0,0,0.6)");
    // labels — SF Mono ramp
    ctx.font = "9px ui-monospace, 'SF Mono', Menlo, monospace";
    ctx.fillStyle = "rgba(255,255,255,0.55)";
    ctx.textAlign = "left";
    ctx.fillText(fmtInt(cur.lo), PLOT.x0, 9);
    ctx.textAlign = "right";
    ctx.fillText(fmtInt(cur.hi), PLOT.x1, 9);
    ctx.textAlign = "center";
    ctx.fillText("G: " + cur.gamma.toFixed(2), (PLOT.x0 + PLOT.x1) / 2, 9);
    ctx.fillStyle = "rgba(255,255,255,0.28)";
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
  fetch("/api/histogram")
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
  const [py, px] = state.info.pixelSizeUm;
  $("pos-um").textContent = fmtUm(img.x * px) + " , " + fmtUm(img.y * py);
  $("pos-px").textContent = fmtInt(img.x) + " , " + fmtInt(img.y) + " px";
}

function updateScalebar(iz) {
  const px = state.info.pixelSizeUm[1];
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
  measure: "tool-measure",
  pin: "tool-pin",
  box: "tool-box",
};

function setTool(tool) {
  state.tool = state.tool === tool ? null : tool;
  const on = state.tool;
  if (on === "roi" && state.windows.region.isHidden()) state.windows.region.open();
  for (const [t, id] of Object.entries(TOOL_BTN)) {
    $(id).classList.toggle("armed", on === t);
  }
  $("roi-toggle").textContent = on === "roi" ? "Drag to Mark…" : "Select Region";
  $("stage").classList.toggle("selecting", !!on);
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
  $("tool-measure").onclick = () => setTool("measure");
  $("tool-pin").onclick = () => setTool("pin");
  $("tool-box").onclick = () => setTool("box");

  stage.addEventListener("pointerdown", (ev) => {
    if (!state.tool || ev.button !== 0) return;
    start = elementPoint(ev, stage);
    startImg = imgPoint(start);
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
    if (state.tool === "roi" || state.tool === "box") {
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

    if (tool === "roi") {
      finishSelection(start, end);
    } else if (tool === "measure") {
      state.tempLine = null;
      if (moved >= 6) {
        addAnnotation({
          type: "line",
          x1: startImg.x, y1: startImg.y, x2: endImg.x, y2: endImg.y,
          text: "",
        });
        // stay armed: rulers are usually taken in series
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
  fetch("/api/annotations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: state.annotations }),
  })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
    .then((d) => setAnnStatus("Saved · " + basename(d.path)))
    .catch((e) => setAnnStatus("Save failed: " + e.message));
}

function loadAnnotations() {
  fetch("/api/annotations")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
    .then((d) => {
      state.annotations = Array.isArray(d.items) ? d.items : [];
      state.annPath = d.path;
      setAnnStatus(
        state.annotations.length
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

function basename(p) {
  return p ? String(p).split("/").pop() : "";
}

function lineLengthUm(a) {
  const [py, px] = state.info.pixelSizeUm;
  return Math.hypot((a.x2 - a.x1) * px, (a.y2 - a.y1) * py);
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
    const label = fmtUm(lineLengthUm(a)) + (a.text ? " · " + a.text : "");
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
      [JSON.stringify({ format: "nd2wsi-annotations/1", source: state.info.name, items: state.annotations }, null, 1)],
      { type: "application/json" }
    );
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = state.info.name.replace(/\.nd2$/i, "") + ".annotations.json";
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
        const items = Array.isArray(data) ? data : data.items;
        if (!Array.isArray(items)) throw new Error("no items array");
        state.annotations = items;
        annotationsChanged();
        setAnnStatus("Opened " + f.name + " (" + items.length + ")");
      } catch (e) {
        showToast("could not read annotations: " + e.message);
      }
      $("ann-file").value = "";
    });
  });
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
    meta.textContent = a.type === "line" ? fmtUm(lineLengthUm(a)) : "";
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
  state.roi = { x: Math.round(x0), y: Math.round(y0), w, h };
  drawRoiOverlay();
  updateRoiPanel();
  $("roi-detail").classList.add("active");
  $("roi-hint").style.display = "none";
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
}

function updateRoiPanel() {
  const r = state.roi;
  if (!r) return;
  const info = state.info;
  $("roi-px").textContent = fmtInt(r.w) + " × " + fmtInt(r.h);
  const [py, px] = info.pixelSizeUm;
  const wUm = fmtUm(r.w * px);
  const hUm = fmtUm(r.h * py);
  const [wNum, wUnit] = wUm.split(" ");
  const [hNum, hUnit] = hUm.split(" ");
  $("roi-um").textContent =
    wUnit === hUnit ? wNum + " × " + hNum + " " + wUnit : wUm + " × " + hUm;
  const itemsize = dtypeBytes(info.dtype);
  const nCh = info.rgb ? 3 : state.channels.length;
  $("roi-bytes").textContent = "≈ " + fmtBytes(r.w * r.h * nCh * itemsize);
}

function downloadRoi(fmt) {
  const r = state.roi;
  if (!r) return;
  // exports are always native resolution (level 0)
  const q = new URLSearchParams({
    level: 0,
    x: r.x,
    y: r.y,
    w: r.w,
    h: r.h,
    format: fmt,
  });
  const all = state.channels.length === state.info.channels.length;
  if (!all) q.set("c", state.channels.join(","));
  if (fmt === "png" || fmt === "jpg") {
    const win = lutParam();
    if (win) q.set("win", win); // rendered exports match the screen LUTs
    const mpx = (q.get("w") * q.get("h")) / 1e6;
    if (mpx > state.info.maxRenderMpx) {
      showToast(
        "rendered export capped at " + state.info.maxRenderMpx +
        " MPx — use ND2/TIFF (they stream any size)"
      );
      return;
    }
  }
  const a = document.createElement("a");
  a.href = "/api/roi?" + q.toString();
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
  showToast("export started — check your downloads");
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

  let st = { rect: opts.def(), collapsed: false, hidden: false, zoomRestore: null };
  try {
    // geometry and collapse persist; "closed" is session-only, so panels
    // always come back on the next launch (macOS palette behavior)
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
    isHidden: () => st.hidden,
    bodyWidth: () => Math.max(180, (body.clientWidth || st.rect.w - 2) - 24),
  };
  apply();
  clampToStage();
  return api;
}

/* ---- keys / misc ---------------------------------------------------------- */

function wireKeys() {
  window.addEventListener("keydown", (ev) => {
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    const k = ev.key.toLowerCase();
    if (k === "r") setTool("roi");
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
