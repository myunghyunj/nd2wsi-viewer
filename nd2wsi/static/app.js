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
  selecting: false,
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

  document.title = info.name + " — nd2wsi";
  $("file-name").textContent = info.name;
  $("file-dims").textContent =
    fmtInt(info.width) + " × " + fmtInt(info.height) + " px · " + info.dtype +
    (info.rgb ? " · RGB" : " · " + info.channels.length + " ch");
  $("plane-note").textContent = planeNote(info);

  buildChannelPanel();
  buildLevelLamps();
  buildViewer();
  wireRoi();
  wireKeys();
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
    animationTime: 0.6,
    springStiffness: 8,
    zoomPerScroll: 1.4,
    minZoomImageRatio: 0.7,
    maxZoomPixelRatio: 3,
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

function buildChannelPanel() {
  const info = state.info;
  if (info.rgb) return; // RGB slides render as-is
  $("channels-section").hidden = false;
  const list = $("channel-list");
  info.channels.forEach((ch, i) => {
    const row = document.createElement("label");
    row.className = "channel-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    if (info.channels.length < 2) cb.style.display = "none";
    cb.onchange = () => {
      const on = new Set(state.channels);
      cb.checked ? on.add(i) : on.delete(i);
      if (!on.size) {
        cb.checked = true; // keep at least one channel lit
        return;
      }
      state.channels = [...on].sort((a, b) => a - b);
      reopenPreservingView();
    };
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = "#" + ch.color;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = ch.label || "channel " + i;
    const win = document.createElement("span");
    win.className = "win";
    win.textContent = fmtInt(ch.window.start) + "–" + fmtInt(ch.window.end);
    const reset = document.createElement("button");
    reset.className = "lut-reset";
    reset.type = "button";
    reset.title = "Reset window and gamma";
    reset.textContent = "↺";
    reset.addEventListener("click", (ev) => {
      ev.preventDefault();
      state.lutWidgets[i].reset();
    });
    row.append(cb, sw, name, win, reset);
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

const LUT_W = 208;
const LUT_H = 84;
const PLOT = { x0: 4, x1: 204, y0: 14, y1: 66 }; // curve/histogram area

const applyLuts = debounce(() => reopenPreservingView(), 250);

function buildLutRow(i, ch, winLabel) {
  const wrap = document.createElement("div");
  wrap.className = "lut";
  const canvas = document.createElement("canvas");
  canvas.className = "lut-canvas";
  const dpr = window.devicePixelRatio || 1;
  canvas.width = LUT_W * dpr;
  canvas.height = LUT_H * dpr;
  canvas.style.width = LUT_W + "px";
  canvas.style.height = LUT_H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  wrap.append(canvas);

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
    ctx.clearRect(0, 0, LUT_W, LUT_H);
    // frame + baseline
    ctx.strokeStyle = "#262b31";
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
    ctx.strokeStyle = "#566070";
    ctx.setLineDash([2, 3]);
    for (const v of [cur.lo, cur.hi]) {
      ctx.beginPath();
      ctx.moveTo(vx(v) + 0.5, PLOT.y0);
      ctx.lineTo(vx(v) + 0.5, PLOT.y1);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    // mapping curve: flat-left, gamma ramp, flat-right
    ctx.strokeStyle = "#dbe1e8";
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
    ctx.fillStyle = "#16191d";
    ctx.fill();
    ctx.strokeStyle = "#dbe1e8";
    ctx.stroke();
    // lo/hi triangles along the top edge
    triangle(vx(cur.lo), "#0a0c0e", "#8b94a1");
    triangle(vx(cur.hi), "#dbe1e8", "#0a0c0e");
    // labels
    ctx.font = "9px ui-monospace, Menlo, monospace";
    ctx.fillStyle = "#8b94a1";
    ctx.textAlign = "left";
    ctx.fillText(fmtInt(cur.lo), PLOT.x0, 9);
    ctx.textAlign = "right";
    ctx.fillText(fmtInt(cur.hi), PLOT.x1, 9);
    ctx.textAlign = "center";
    ctx.fillText("G: " + cur.gamma.toFixed(2), (PLOT.x0 + PLOT.x1) / 2, 9);
    ctx.fillStyle = "#566070";
    ctx.textAlign = "left";
    ctx.fillText("0", PLOT.x0, LUT_H - 3);
    ctx.textAlign = "right";
    ctx.fillText(fmtInt(vmax), PLOT.x1, LUT_H - 3);
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
    setHistogram(hg) {
      bins = hg.bins;
      vmax = hg.vmax;
      draw();
    },
    reset() {
      setLut({ ...def });
    },
  };
  state.lutWidgets[i] = widget;
  draw();
  return wrap;
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
  return vp.viewportToImageZoom(vp.getZoom(true)); // screen px per image px
}

function activeLevel() {
  // the coarsest level whose scale still meets the current sampling density
  const iz = Math.min(currentImageZoom(), 1);
  const levels = state.info.levels; // 0 = full res
  for (let k = levels.length - 1; k >= 0; k--) {
    if (1 / levels[k].downsample >= iz) return k;
  }
  return 0;
}

function updateReadout() {
  const iz = currentImageZoom();
  $("zoom-val").textContent = (iz * 100).toFixed(iz >= 0.1 ? 0 : 1) + " %";
  const act = activeLevel();
  state.info.levels.forEach((lv, k) => {
    $("lamp-" + lv.path).classList.toggle("lit", k === act);
  });
  updateScalebar(iz);
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

/* ---- ROI selection -------------------------------------------------------- */

function wireRoi() {
  const stage = $("stage");
  const rubber = $("rubber");
  let start = null;

  $("roi-toggle").onclick = () => setSelecting(!state.selecting);

  stage.addEventListener("pointerdown", (ev) => {
    if (!state.selecting || ev.button !== 0) return;
    start = elementPoint(ev, stage);
    rubber.style.display = "block";
    positionRubber(start, start);
    try { stage.setPointerCapture(ev.pointerId); } catch (_) { /* optional */ }
    ev.preventDefault();
    ev.stopPropagation();
  }, true);

  stage.addEventListener("pointermove", (ev) => {
    if (!state.selecting || !start) return;
    positionRubber(start, elementPoint(ev, stage));
    ev.preventDefault();
    ev.stopPropagation();
  }, true);

  stage.addEventListener("pointerup", (ev) => {
    if (!state.selecting || !start) return;
    const end = elementPoint(ev, stage);
    rubber.style.display = "none";
    finishSelection(start, end);
    start = null;
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
}

function setSelecting(on) {
  state.selecting = on;
  $("stage").classList.toggle("selecting", on);
  $("roi-toggle").classList.toggle("armed", on);
  $("roi-toggle").textContent = on ? "Drag to mark …" : "Select region";
  state.viewer.setMouseNavEnabled(!on);
  if (!on) $("rubber").style.display = "none";
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
  setSelecting(false);
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

/* ---- keys / misc ---------------------------------------------------------- */

function wireKeys() {
  window.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
    if (ev.key === "r" || ev.key === "R") setSelecting(!state.selecting);
    else if (ev.key === "Escape") { setSelecting(false); }
    else if (ev.key === "0") state.viewer.viewport.goHome();
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
