/* nd2wsi viewer ------------------------------------------------------------ */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  info: null,
  viewer: null,
  channels: [], // enabled channel indices
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

  document.title = info.name + " — nd2wsi";
  $("file-name").textContent = info.name;
  $("file-dims").textContent =
    fmtInt(info.width) + " × " + fmtInt(info.height) + " px · " + info.dtype +
    (info.rgb ? " · RGB" : " · " + info.channels.length + " ch");
  $("plane-note").textContent = planeNote(info);

  buildChannelPanel();
  buildLevelLamps();
  buildRoiLevelSelect();
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

function tileQuery() {
  const all = state.channels.length === state.info.channels.length;
  return all ? "" : "?c=" + state.channels.join(",");
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
  if (info.rgb || info.channels.length < 2) return;
  $("channels-section").hidden = false;
  const list = $("channel-list");
  info.channels.forEach((ch, i) => {
    const row = document.createElement("label");
    row.className = "channel-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
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
    row.append(cb, sw, name, win);
    list.append(row);
  });
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
  $("roi-dl-tiff").onclick = () => downloadRoi("tiff");
  $("roi-dl-png").onclick = () => downloadRoi("png");
  $("roi-dl-jpg").onclick = () => downloadRoi("jpg");
  $("roi-level").onchange = updateRoiPanel;

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

function buildRoiLevelSelect() {
  const sel = $("roi-level");
  state.info.levels.forEach((lv, k) => {
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent =
      k === 0
        ? "Native resolution (level 0)"
        : "Level " + k + "  ·  1/" + lv.downsample;
    sel.append(opt);
  });
}

function updateRoiPanel() {
  const r = state.roi;
  if (!r) return;
  const info = state.info;
  const k = parseInt($("roi-level").value, 10) || 0;
  const f = info.levels[k].downsample;
  const w = Math.max(1, Math.round(r.w / f));
  const h = Math.max(1, Math.round(r.h / f));
  $("roi-px").textContent = fmtInt(w) + " × " + fmtInt(h);
  const [py, px] = info.pixelSizeUm;
  $("roi-um").textContent = fmtUm(r.w * px) + " × " + fmtUm(r.h * py);
  const itemsize = dtypeBytes(info.dtype);
  const nCh = info.rgb ? 3 : state.channels.length;
  $("roi-bytes").textContent = "≈ " + fmtBytes(w * h * nCh * itemsize);
}

function downloadRoi(fmt) {
  const r = state.roi;
  if (!r) return;
  const k = parseInt($("roi-level").value, 10) || 0;
  const f = state.info.levels[k].downsample;
  const q = new URLSearchParams({
    level: k,
    x: Math.floor(r.x / f),
    y: Math.floor(r.y / f),
    w: Math.max(1, Math.round(r.w / f)),
    h: Math.max(1, Math.round(r.h / f)),
    format: fmt,
  });
  const all = state.channels.length === state.info.channels.length;
  if (!all) q.set("c", state.channels.join(","));
  if (fmt !== "tiff") {
    const mpx = (q.get("w") * q.get("h")) / 1e6;
    if (mpx > state.info.maxRenderMpx) {
      showToast(
        "rendered export capped at " + state.info.maxRenderMpx +
        " MPx — use TIFF (streams any size) or a coarser level"
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
