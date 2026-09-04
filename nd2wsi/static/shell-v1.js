"use strict";

const $ = (id) => document.getElementById(id);
const frames = new Map();
const readyFrames = new Set();
let slides = [];
let active = null;
let busyTab = null;
let busyTimer = null;
let toastTimer = null;
let quitPreparation = null;
const pairPicker = { open: false, mode: "start", replaceSid: null };

const Align = window.nd2wsiAlign;
const VIEWPORT_PROTOCOL_VERSION = 2;
const VIEWPORT_THROTTLE_MS = 48;
const MAX_GROUP = 4; // the anchor and up to three linked slides
const LANDMARKS_NEEDED = 4;

/* A link group has one anchor and up to three members. Each member carries
   its own similarity transform from anchor space to member space, either
   the format-based default (matched centers, optional 180° or mirror) or a
   least-squares fit through the landmarks the user placed. */
const compare = {
  enabled: false,
  anchorSid: null,
  members: [], // linked slides other than the anchor, in display order
  linked: true,
  split: 50,
  mru: [],
  states: new Map(),
  pairs: new Map(), // member sid -> { mode, orientation, transform, fit, landmarks }
  anchorLandmarks: [],
  memory: new Map(), // "anchor|member" -> pair snapshot for this session
  landmark: { active: false, before: null },
  commandSeq: 0,
  requestSeq: 0,
  pendingRequest: null,
  layoutRequestTimer: null,
  routeTimers: new Map(),
  routeLatest: new Map(),
};

function showError(message) {
  const toast = $("shell-toast");
  toast.textContent = String(message || "Unknown error");
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 4200);
}

window.nd2wsiUpdateNotice = showError;

// pywebview's WKWebView can consume a horizontal NSEvent in its native
// scroll view without emitting a DOM wheel event. The macOS bridge calls this
// function only after the physical gesture has latched horizontally. Route it
// to the iframe under the pointer (or the front tab as a conservative fallback).
window.nd2wsiNativeTrackpad = (input) => {
  const x = Number(input?.clientX);
  const y = Number(input?.clientY);
  let frame = Number.isFinite(x) && Number.isFinite(y)
    ? document.elementFromPoint(x, y)?.closest?.("iframe")
    : null;
  if (!frame || !frames.has(frame.dataset.sid)) frame = frames.get(active);
  if (!frame?.contentWindow) return false;
  const rect = frame.getBoundingClientRect();
  frame.contentWindow.postMessage({
    nd2wsi: "native-trackpad",
    version: VIEWPORT_PROTOCOL_VERSION,
    deltaX: Number(input?.deltaX) || 0,
    gestureStart: !!input?.gestureStart,
    clientX: Number.isFinite(x) ? x - rect.left : rect.width / 2,
    clientY: Number.isFinite(y) ? y - rect.top : rect.height / 2,
    altKey: !!input?.altKey,
  }, location.origin);
  return frame.dataset.sid || true;
};

async function refreshUpdaterButton() {
  const button = $("update-check");
  const api = window.pywebview?.api;
  if (!api?.update_status) return;
  try {
    const status = await api.update_status();
    button.disabled = !status.available;
    button.title = status.available
      ? `Check for Updates… · version ${status.version}`
      : "Updates are unavailable in this build";
  } catch (_) {
    button.disabled = true;
  }
}

$("update-check").addEventListener("click", async () => {
  const button = $("update-check");
  const api = window.pywebview?.api;
  if (!api?.check_for_updates) return;
  button.disabled = true;
  button.classList.add("checking");
  button.setAttribute("aria-busy", "true");
  try {
    const result = await api.check_for_updates();
    if (!result.ok) showError(result.message || "Could not open the update window");
  } catch (error) {
    showError(`Could not check for updates: ${error}`);
  } finally {
    button.classList.remove("checking");
    button.removeAttribute("aria-busy");
    setTimeout(refreshUpdaterButton, 400);
  }
});

function applyShellTheme(theme) {
  document.documentElement.classList.toggle("light", theme === "light");
}

// the shell chrome always mirrors the front tab; with no tab open it
// stays on the app's own dark ground

function markNativeChrome() {
  // inside the packaged app the title bar is hidden and the traffic
  // lights float over the tab strip, which needs room and a drag handle
  document.documentElement.classList.add("native-chrome");
}
if (window.pywebview !== undefined) markNativeChrome();
window.addEventListener("pywebviewready", () => {
  markNativeChrome();
  // The shown callback which installs Sparkle and this page load may race by
  // a few milliseconds, so refresh once immediately and once after startup.
  refreshUpdaterButton();
  setTimeout(refreshUpdaterButton, 600);
});

// The window has no title bar of its own, so the tab strip plays the part:
// a double-click on its empty space zooms the window, as a title bar
// would. Tabs, buttons, and the picker keep their double-clicks. In a
// plain browser there is no window to zoom, and nothing happens.
function selectTabByIndex(index) {
  // ⌘1 to ⌘9: the nth open slide, in tab order
  const slide = slides[index];
  if (slide && slide.sid !== active) activate(slide.sid);
}
window.addEventListener("keydown", (event) => {
  if (!(event.metaKey || event.ctrlKey) || event.shiftKey || event.altKey) return;
  if (!/^[1-9]$/.test(event.key)) return;
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
  event.preventDefault();
  selectTabByIndex(Number(event.key) - 1);
});

function requestWindowZoom() {
  const api = window.pywebview?.api;
  if (api?.title_bar_double_click) api.title_bar_double_click();
}
// Both presses must land on the strip's empty space (the drag regions):
// closing a tab rebuilds the strip under the pointer, so the second click
// of a double-click on a close button would otherwise count as bare.
const bareStrip = (target) => target instanceof Element && target.classList.contains("pywebview-drag-region");
let stripPresses = [];
$("tabbar").addEventListener("pointerdown", (event) => {
  stripPresses = [...stripPresses.slice(-1), { at: event.timeStamp, bare: bareStrip(event.target) }];
});
$("tabbar").addEventListener("dblclick", (event) => {
  if (!bareStrip(event.target)) return;
  const recent = stripPresses.filter((press) => event.timeStamp - press.at < 1000);
  if (recent.length === 2 && !recent.every((press) => press.bare)) return;
  event.preventDefault();
  requestWindowZoom();
});

function groupSids() {
  return compare.enabled ? [compare.anchorSid, ...compare.members] : [];
}

function inGroup(sid) {
  return compare.enabled && Boolean(sid) && groupSids().includes(sid);
}

function slideName(sid) {
  return slides.find((slide) => slide.sid === sid)?.name || "Slide";
}

function render() {
  const bar = $("tabbar");
  bar.querySelectorAll(".tab").forEach((tab) => tab.remove());
  const plus = $("newtab");

  function makeTab(slide, busy) {
    const tab = document.createElement("div");
    const classes = ["tab"];
    if (busy) classes.push("busy");
    else {
      if (slide.sid === active) classes.push("active");
      if (inGroup(slide.sid)) classes.push("compare-member");
    }
    tab.className = classes.join(" ");
    tab.title = slide.name;

    const dot = document.createElement("span");
    dot.className = "dot";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = slide.name;
    tab.append(dot, name);

    if (busy) {
      const progress = document.createElement("div");
      progress.className = "tprog";
      const fill = document.createElement("span");
      if (slide.pct > 0) fill.style.width = `${slide.pct}%`;
      else fill.className = "indet";
      progress.append(fill);
      tab.append(progress);
    } else {
      const close = document.createElement("button");
      close.className = "x";
      close.type = "button";
      close.textContent = "×";
      close.title = "Close tab";
      close.onclick = (event) => {
        event.stopPropagation();
        closeTab(slide.sid);
      };
      tab.append(close);
      tab.onclick = () => activate(slide.sid);
    }
    bar.insertBefore(tab, plus);
  }

  slides.forEach((slide) => makeTab(slide, false));
  if (busyTab) makeTab(busyTab, true);
  $("empty").hidden = slides.length > 0 || Boolean(busyTab);
  const compareToggle = $("compare-toggle");
  compareToggle.disabled = slides.length < 2;
  compareToggle.classList.toggle("active", compare.enabled);
  compareToggle.setAttribute("aria-pressed", String(compare.enabled));
  compareToggle.setAttribute("aria-expanded", String(pairPicker.open && pairPicker.mode === "start"));
  if (compare.enabled) {
    document.title = `${groupSids().map(slideName).join(" ↔ ")} — nd2wsi-viewer`;
  } else {
    document.title = active
      ? `${(slides.find((slide) => slide.sid === active) || {}).name || "Slide"} — nd2wsi-viewer`
      : "nd2wsi-viewer";
  }
}

function rememberSlide(sid) {
  if (!sid) return;
  compare.mru = compare.mru.filter((item) => item !== sid);
  compare.mru.push(sid);
}

function ensureFrame(sid) {
  if (!sid || frames.has(sid)) return frames.get(sid);
  const frame = document.createElement("iframe");
  frame.dataset.sid = sid;
  frame.src = `s/${sid}/`;
  frame.title = slideName(sid);
  frame.addEventListener("load", () => paneCameUp(sid));
  frames.set(sid, frame);
  $("frames").append(frame);
  return frame;
}

function paneCameUp(sid) {
  // a pane loaded or reloaded: give it everything the group knows
  if (!inGroup(sid)) return;
  applyDisplayTransform(sid);
  broadcastCompareState();
  if (compare.landmark.active) sendLandmarkMode(sid, true);
  if (compare.linked || compare.pendingRequest) {
    requestGroupSoon(compare.pendingRequest?.kind || "sync");
  }
}

function applyFrameLayout() {
  document.documentElement.style.setProperty("--compare-split", `${compare.split}%`);
  const group = groupSids();
  const divider = $("compare-divider");
  const twoUp = compare.enabled && group.length === 2;
  divider.hidden = !twoUp;
  divider.setAttribute("aria-hidden", String(!twoUp));
  $("compare-controls").hidden = !compare.enabled;
  for (const [key, frame] of frames) {
    const index = group.indexOf(key);
    frame.classList.toggle("active", !compare.enabled && key === active);
    frame.classList.toggle("compare-cell", index >= 0);
    frame.style.left = frame.style.top = frame.style.width = frame.style.height = "";
    if (index < 0) continue;
    let cell;
    if (group.length === 2) {
      cell = index === 0
        ? { left: 0, top: 0, width: compare.split, height: 100 }
        : { left: compare.split, top: 0, width: 100 - compare.split, height: 100 };
    } else if (group.length === 3) {
      cell = { left: (index * 100) / 3, top: 0, width: 100 / 3, height: 100 };
    } else {
      cell = { left: (index % 2) * 50, top: Math.floor(index / 2) * 50, width: 50, height: 50 };
    }
    frame.style.left = `${cell.left}%`;
    frame.style.top = `${cell.top}%`;
    frame.style.width = `${cell.width}%`;
    frame.style.height = `${cell.height}%`;
  }
}

function activate(sid) {
  closePairPicker(false);
  if (compare.enabled && sid && !inGroup(sid)) stopCompare();
  active = sid;
  rememberSlide(sid);
  ensureFrame(sid);
  applyFrameLayout();
  const selected = frames.get(active);
  if (selected?.contentWindow) {
    selected.contentWindow.postMessage({ nd2wsi: "theme-request" }, location.origin);
  }
  render();
}

function refresh(selectSid) {
  return fetch("api/slides", { cache: "no-store" })
    .then((response) => response.json())
    .then((data) => {
      slides = data.slides || [];
      const openSids = new Set(slides.map((slide) => slide.sid));
      for (const [key, frame] of [...frames]) {
        if (!openSids.has(key)) {
          frame.remove();
          frames.delete(key);
          readyFrames.delete(key);
          compare.states.delete(key);
        }
      }
      compare.mru = compare.mru.filter((sid) => openSids.has(sid));
      if (compare.enabled) {
        if (!openSids.has(compare.anchorSid)) stopCompare();
        else {
          for (const sid of [...compare.members]) {
            if (!openSids.has(sid)) removeMember(sid, false);
          }
        }
      }
      if (pairPicker.open) {
        if (slides.length < 2) closePairPicker(false);
        else renderPairPicker();
      }
      if (selectSid) activate(selectSid);
      else if (!slides.some((slide) => slide.sid === active)) {
        activate(slides.length ? slides[slides.length - 1].sid : null);
      } else {
        applyFrameLayout();
        updateCompareControls();
        render();
      }
    })
    .catch((error) => showError(`Could not refresh slides: ${error}`));
}

function openMany(paths) {
  return paths.reduce((chain, path) => chain.then(() => openPath(path)), Promise.resolve());
}

function openPath(path) {
  const job = Math.random().toString(36).slice(2, 10);
  const name = path.split("/").pop();
  busyTab = { name: `Opening ${name}…`, job, pct: null };
  render();

  clearInterval(busyTimer);
  busyTimer = setInterval(() => {
    fetch(`api/roi/progress?job=${job}`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data) => {
        if (!busyTab || data.state !== "converting") return;
        busyTab.pct = data.pct || 0;
        busyTab.name = busyTab.pct > 0
          ? `Converting ${name} ${busyTab.pct} %`
          : `Preparing ${name}…`;
        render();
      })
      .catch(() => {});
  }, 400);

  return fetch("api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, job }),
  })
    .then((response) => response.json())
    .then((data) => {
      clearInterval(busyTimer);
      busyTab = null;
      if (data.error) {
        render();
        showError(data.error);
        return;
      }
      return refresh(data.sid);
    })
    .catch((error) => {
      clearInterval(busyTimer);
      busyTab = null;
      render();
      showError(`Open failed: ${error}`);
    });
}

function closeTab(sid) {
  fetch("api/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sid }),
  })
    .then((response) => response.json())
    .then((data) => data.error ? showError(data.error) : refresh())
    .catch((error) => showError(`Close failed: ${error}`));
}

/* ---- pane messages ------------------------------------------------------- */

function frameSidForSource(source) {
  for (const [sid, frame] of frames) {
    if (frame.contentWindow === source) return sid;
  }
  return null;
}

function postToSlide(sid, message) {
  const frame = frames.get(sid);
  if (!frame?.contentWindow) return false;
  frame.contentWindow.postMessage(message, location.origin);
  return true;
}

function finishQuitPreparation(result) {
  const pending = quitPreparation;
  if (!pending) return;
  quitPreparation = null;
  clearTimeout(pending.timer);
  pending.resolve(result);
}

window.nd2wsiPrepareForUpdate = (requestId) => {
  const id = String(requestId || "");
  if (!id) return Promise.resolve({ ok: false, error: "missing update request" });
  if (quitPreparation) {
    finishQuitPreparation({ ok: false, error: "update preparation restarted" });
  }
  document.documentElement.classList.add("preparing-update");
  if (busyTab) {
    return Promise.resolve({ ok: false, error: "a slide is still opening" });
  }
  const targets = new Set([...readyFrames].filter((sid) => frames.has(sid)));
  if (!targets.size) return Promise.resolve({ ok: true, panes: 0 });

  return new Promise((resolve) => {
    const pending = {
      requestId: id,
      targets,
      seen: new Set(),
      errors: [],
      resolve,
      timer: null,
    };
    pending.timer = setTimeout(() => {
      if (quitPreparation !== pending) return;
      const missing = [...targets].filter((sid) => !pending.seen.has(sid));
      finishQuitPreparation({
        ok: false,
        error: `save confirmation timed out for ${missing.length} pane(s)`,
      });
    }, 8000);
    quitPreparation = pending;
    for (const sid of targets) {
      postToSlide(sid, {
        nd2wsi: "prepare-quit",
        version: VIEWPORT_PROTOCOL_VERSION,
        requestId: id,
      });
    }
  });
};

function finitePoint(value, positive = false) {
  if (!value || !Number.isFinite(Number(value.x)) || !Number.isFinite(Number(value.y))) {
    return null;
  }
  const point = { x: Number(value.x), y: Number(value.y) };
  if (positive && (point.x <= 0 || point.y <= 0)) return null;
  return point;
}

function normalizeViewportState(data, sid) {
  const centerPx = finitePoint(data.centerPx);
  const spanPx = finitePoint(data.spanPx, true);
  const imagePx = finitePoint(data.imagePx, true);
  if (!centerPx || !spanPx || !imagePx) return null;
  let pixelSizeUm = data.pixelSizeUm;
  if (Array.isArray(pixelSizeUm)) {
    pixelSizeUm = { y: Number(pixelSizeUm[0]), x: Number(pixelSizeUm[1]) };
  } else {
    pixelSizeUm = finitePoint(pixelSizeUm, true);
  }
  if (!pixelSizeUm || pixelSizeUm.x <= 0 || pixelSizeUm.y <= 0) pixelSizeUm = null;
  const containerPx = finitePoint(data.containerPx, true) || { x: 1, y: 1 };
  return {
    sid,
    seq: Number.isFinite(Number(data.seq)) ? Number(data.seq) : 0,
    reason: String(data.reason || "user"),
    requestId: data.requestId == null ? null : String(data.requestId),
    echoOf: data.echoOf == null ? null : String(data.echoOf),
    centerPx,
    spanPx,
    imagePx,
    containerPx,
    pixelSizeUm,
  };
}

/* ---- spaces and transforms ------------------------------------------------
   A pair maps in micrometers when both slides are calibrated and in
   normalized image coordinates otherwise. Anchor pixels are the hub every
   relay passes through, so members with different modes still agree. */

function mappingMode(a, b) {
  return a?.pixelSizeUm && b?.pixelSizeUm ? "physical" : "normalized";
}

function pxToSpace(point, st, mode) {
  return mode === "physical"
    ? { x: point.x * st.pixelSizeUm.x, y: point.y * st.pixelSizeUm.y }
    : { x: point.x / st.imagePx.x, y: point.y / st.imagePx.y };
}

function spaceToPx(point, st, mode) {
  return mode === "physical"
    ? { x: point.x / st.pixelSizeUm.x, y: point.y / st.pixelSizeUm.y }
    : { x: point.x * st.imagePx.x, y: point.y * st.imagePx.y };
}

function widthPxToSpace(width, st, mode) {
  return mode === "physical" ? width * st.pixelSizeUm.x : width / st.imagePx.x;
}

function widthSpaceToPx(width, st, mode) {
  return mode === "physical" ? width / st.pixelSizeUm.x : width * st.imagePx.x;
}

function imageCenterSpace(st, mode) {
  return pxToSpace({ x: st.imagePx.x / 2, y: st.imagePx.y / 2 }, st, mode);
}

function slideFormat(sid) {
  const name = String(slideName(sid)).toLowerCase();
  if (name.endsWith(".svs")) return "svs";
  if (name.endsWith(".nd2")) return "nd2";
  return null;
}

function automaticOrientation(anchorSid, memberSid) {
  // this lab's scanners run their axes in opposite directions
  const formats = new Set([slideFormat(anchorSid), slideFormat(memberSid)]);
  return formats.has("svs") && formats.has("nd2") ? { x: -1, y: -1 } : { x: 1, y: 1 };
}

function newPair(anchorSid, memberSid) {
  return {
    mode: null,
    orientation: automaticOrientation(anchorSid, memberSid),
    transform: null,
    fit: null,
    landmarks: [],
  };
}

function defaultTransform(pair, anchorState, memberState, mode) {
  const linear = Align.fromOrientation(pair.orientation.x, pair.orientation.y);
  return Align.translationMatching(
    linear, imageCenterSpace(anchorState, mode), imageCenterSpace(memberState, mode)
  );
}

function ensurePairTransform(sid) {
  const pair = compare.pairs.get(sid);
  const anchorState = compare.states.get(compare.anchorSid);
  const memberState = compare.states.get(sid);
  if (!pair || !anchorState || !memberState) return null;
  const mode = mappingMode(anchorState, memberState);
  if (pair.transform && pair.mode === mode) return pair;
  pair.mode = mode;
  if (pair.fit && pair.landmarks.length >= 2) {
    fitPair(sid, false);
    if (pair.transform) return pair;
  }
  pair.transform = defaultTransform(pair, anchorState, memberState, mode);
  pair.fit = null;
  return pair;
}

function displayTransformFor(sid) {
  if (!compare.enabled || sid === compare.anchorSid) return { degrees: 0, flipped: false };
  const pair = compare.pairs.get(sid);
  if (!pair) return { degrees: 0, flipped: false };
  const linear = pair.transform || Align.fromOrientation(pair.orientation.x, pair.orientation.y);
  // the member holds the anchor turned by theta, so its pane turns back
  return { degrees: -Align.angleDeg(linear), flipped: Align.mirrored(linear) };
}

function applyDisplayTransform(sid) {
  if (!sid) return;
  postToSlide(sid, {
    nd2wsi: "display-transform",
    version: VIEWPORT_PROTOCOL_VERSION,
    ...displayTransformFor(sid),
  });
}

function applyDisplayTransforms() {
  for (const sid of groupSids()) applyDisplayTransform(sid);
}

function clearDisplayTransforms(sids) {
  for (const sid of new Set(sids.filter(Boolean))) {
    postToSlide(sid, {
      nd2wsi: "display-transform",
      version: VIEWPORT_PROTOCOL_VERSION,
      degrees: 0,
      flipped: false,
    });
  }
}

function rematchTranslation(sid) {
  // keep the pair's rotation, scale and mirror; move its translation so the
  // two panes as they stand now correspond
  const pair = ensurePairTransform(sid);
  const anchorState = compare.states.get(compare.anchorSid);
  const memberState = compare.states.get(sid);
  if (!pair || !anchorState || !memberState) return false;
  pair.transform = Align.translationMatching(
    pair.transform,
    pxToSpace(anchorState.centerPx, anchorState, pair.mode),
    pxToSpace(memberState.centerPx, memberState, pair.mode)
  );
  return true;
}

function recaptureAll() {
  let any = false;
  for (const sid of compare.members) any = rematchTranslation(sid) || any;
  return any;
}

function pairKey(anchorSid, memberSid) {
  return `${anchorSid}|${memberSid}`;
}

function clonePoints(points) {
  return (points || []).map((p) => ({ x: p.x, y: p.y }));
}

function rememberAlignment() {
  if (!compare.enabled) return;
  for (const sid of compare.members) {
    const pair = compare.pairs.get(sid);
    if (!pair || !pair.transform) continue;
    compare.memory.set(pairKey(compare.anchorSid, sid), {
      mode: pair.mode,
      orientation: { ...pair.orientation },
      transform: { ...pair.transform },
      fit: pair.fit ? { ...pair.fit } : null,
      landmarks: clonePoints(pair.landmarks),
      anchorLandmarks: clonePoints(compare.anchorLandmarks),
    });
  }
}

function restoreAlignment(anchorSid, memberSid, pair) {
  const direct = compare.memory.get(pairKey(anchorSid, memberSid));
  if (direct) {
    Object.assign(pair, {
      mode: direct.mode,
      orientation: { ...direct.orientation },
      transform: { ...direct.transform },
      fit: direct.fit ? { ...direct.fit } : null,
      landmarks: clonePoints(direct.landmarks),
    });
    if (!compare.anchorLandmarks.length) compare.anchorLandmarks = clonePoints(direct.anchorLandmarks);
    return true;
  }
  const reversed = compare.memory.get(pairKey(memberSid, anchorSid));
  if (reversed) {
    const inverse = Align.invert(reversed.transform);
    if (!inverse) return false;
    Object.assign(pair, {
      mode: reversed.mode,
      orientation: { ...reversed.orientation },
      transform: inverse,
      fit: reversed.fit ? { ...reversed.fit, angleDeg: -reversed.fit.angleDeg, scale: 1 / reversed.fit.scale } : null,
      landmarks: clonePoints(reversed.anchorLandmarks),
    });
    if (!compare.anchorLandmarks.length) compare.anchorLandmarks = clonePoints(reversed.landmarks);
    return true;
  }
  return false;
}

/* ---- relay ---------------------------------------------------------------- */

function anchorViewFromSource(sourceSid, source) {
  // where the source pane's field sits in anchor pixels
  if (sourceSid === compare.anchorSid) {
    return { centerPx: source.centerPx, widthPx: source.spanPx.x };
  }
  const pair = ensurePairTransform(sourceSid);
  const anchorState = compare.states.get(compare.anchorSid);
  if (!pair || !anchorState) return null;
  const inverse = Align.invert(pair.transform);
  if (!inverse) return null;
  const centerSpace = Align.apply(inverse, pxToSpace(source.centerPx, source, pair.mode));
  const widthSpace = widthPxToSpace(source.spanPx.x, source, pair.mode) / Align.scale(pair.transform);
  return {
    centerPx: spaceToPx(centerSpace, anchorState, pair.mode),
    widthPx: widthSpaceToPx(widthSpace, anchorState, pair.mode),
  };
}

function targetViewFromAnchor(targetSid, anchorView) {
  const targetState = compare.states.get(targetSid);
  if (!targetState) return null;
  if (targetSid === compare.anchorSid) {
    return { centerPx: anchorView.centerPx, spanX: anchorView.widthPx, state: targetState };
  }
  const pair = ensurePairTransform(targetSid);
  const anchorState = compare.states.get(compare.anchorSid);
  if (!pair || !anchorState) return null;
  const centerSpace = Align.apply(pair.transform, pxToSpace(anchorView.centerPx, anchorState, pair.mode));
  const widthSpace = widthPxToSpace(anchorView.widthPx, anchorState, pair.mode) * Align.scale(pair.transform);
  return {
    centerPx: spaceToPx(centerSpace, targetState, pair.mode),
    spanX: widthSpaceToPx(widthSpace, targetState, pair.mode),
    state: targetState,
  };
}

function forwardViewport(sourceSid, source) {
  if (!compare.enabled || !compare.linked || compare.pendingRequest) return;
  if (!inGroup(sourceSid)) return;
  const anchorView = anchorViewFromSource(sourceSid, source);
  if (!anchorView) return;
  for (const targetSid of groupSids()) {
    if (targetSid === sourceSid) continue;
    const view = targetViewFromAnchor(targetSid, anchorView);
    if (!view || !Number.isFinite(view.spanX) || view.spanX <= 0) continue;
    const aspect = view.state.containerPx.y / view.state.containerPx.x;
    const commandId = `shell-vp-${++compare.commandSeq}`;
    postToSlide(targetSid, {
      nd2wsi: "viewport-apply",
      version: VIEWPORT_PROTOCOL_VERSION,
      commandId,
      sourceSid,
      sourceSeq: source.seq,
      centerPx: view.centerPx,
      spanPx: { x: view.spanX, y: view.spanX * aspect },
      animate: false,
    });
  }
}

function scheduleViewportRoute(sourceSid, state) {
  compare.routeLatest.set(sourceSid, state);
  if (compare.routeTimers.has(sourceSid)) return;
  const timer = setTimeout(() => {
    compare.routeTimers.delete(sourceSid);
    const latest = compare.routeLatest.get(sourceSid);
    compare.routeLatest.delete(sourceSid);
    if (latest) forwardViewport(sourceSid, latest);
  }, VIEWPORT_THROTTLE_MS);
  compare.routeTimers.set(sourceSid, timer);
}

function clearViewportRoutes() {
  for (const timer of compare.routeTimers.values()) clearTimeout(timer);
  compare.routeTimers.clear();
  compare.routeLatest.clear();
}

function clearPendingRequest() {
  if (compare.pendingRequest?.timer) clearTimeout(compare.pendingRequest.timer);
  compare.pendingRequest = null;
  clearTimeout(compare.layoutRequestTimer);
  compare.layoutRequestTimer = null;
}

function syncFromAnchor() {
  const anchorState = compare.states.get(compare.anchorSid);
  if (anchorState && compare.linked) forwardViewport(compare.anchorSid, anchorState);
}

function requestGroup(kind) {
  // ask every pane where it stands, then act once all have answered
  if (!compare.enabled) return;
  clearViewportRoutes();
  clearPendingRequest();
  const requestId = `shell-request-${++compare.requestSeq}`;
  const pending = { requestId, kind, seen: new Set(), timer: null };
  pending.timer = setTimeout(() => {
    if (compare.pendingRequest !== pending) return;
    clearPendingRequest();
    updateCompareControls();
    if (kind !== "sync") showError("Could not read every linked view; nothing was changed");
  }, 2500);
  compare.pendingRequest = pending;
  updateCompareControls();
  for (const sid of groupSids()) {
    postToSlide(sid, {
      nd2wsi: "viewport-request",
      version: VIEWPORT_PROTOCOL_VERSION,
      requestId,
    });
  }
}

function requestGroupSoon(kind) {
  clearTimeout(compare.layoutRequestTimer);
  compare.layoutRequestTimer = setTimeout(() => {
    compare.layoutRequestTimer = null;
    if (compare.enabled) requestGroup(kind);
  }, 80);
}

function finishGroupRequest(pending) {
  if (compare.pendingRequest !== pending) return;
  clearPendingRequest();
  const ready = groupSids().every((sid) => compare.states.has(sid));
  if (!ready) {
    updateCompareControls();
    return;
  }
  for (const sid of compare.members) ensurePairTransform(sid);
  if (pending.kind === "capture") {
    recaptureAll();
    compare.linked = true;
  } else if (pending.kind === "rotate-180" || pending.kind === "mirror") {
    for (const sid of compare.members) {
      const pair = compare.pairs.get(sid);
      if (!pair || pair.fit) continue; // a landmark fit already decided orientation
      if (pending.kind === "rotate-180") {
        pair.orientation = { x: -pair.orientation.x, y: -pair.orientation.y };
      } else {
        pair.orientation = { x: -pair.orientation.x, y: pair.orientation.y };
      }
      const anchorState = compare.states.get(compare.anchorSid);
      const memberState = compare.states.get(sid);
      pair.transform = defaultTransform(pair, anchorState, memberState, pair.mode);
      rematchTranslation(sid);
    }
    applyDisplayTransforms();
  } else if (pending.kind === "clear") {
    for (const sid of compare.members) {
      const pair = compare.pairs.get(sid);
      const anchorState = compare.states.get(compare.anchorSid);
      const memberState = compare.states.get(sid);
      pair.orientation = automaticOrientation(compare.anchorSid, sid);
      pair.fit = null;
      pair.landmarks = [];
      pair.transform = defaultTransform(pair, anchorState, memberState, pair.mode);
    }
    compare.anchorLandmarks = [];
    compare.linked = true;
    applyDisplayTransforms();
    for (const sid of groupSids()) sendLandmarkMode(sid, compare.landmark.active, { clear: true });
  }
  syncFromAnchor();
  updateCompareControls();
}

function receiveViewportState(data, sid) {
  if (data.version !== VIEWPORT_PROTOCOL_VERSION) return;
  const state = normalizeViewportState(data, sid);
  if (!state) return;
  const previous = compare.states.get(sid);
  if (!state.requestId && state.seq && previous?.seq && state.seq <= previous.seq) return;
  compare.states.set(sid, state);
  const pending = compare.pendingRequest;
  if (pending && state.requestId === pending.requestId && inGroup(sid)) {
    pending.seen.add(sid);
    if (pending.seen.size === groupSids().length) finishGroupRequest(pending);
  }
  if (!compare.enabled || !compare.linked || compare.pendingRequest ||
      state.reason !== "user" || state.echoOf) return;
  if (!inGroup(sid)) return;
  if (data.nudge === true) {
    // one pane moved on its own; its pairs absorb the difference, which is
    // how serial sections get matched at high zoom
    clearViewportRoutes();
    const changed = sid === compare.anchorSid ? recaptureAll() : rematchTranslation(sid);
    if (changed) updateCompareControls();
    return;
  }
  scheduleViewportRoute(sid, state);
}

/* ---- landmarks ------------------------------------------------------------ */

function sendLandmarkMode(sid, active, extra = {}) {
  const points = sid === compare.anchorSid
    ? compare.anchorLandmarks
    : compare.pairs.get(sid)?.landmarks || [];
  postToSlide(sid, {
    nd2wsi: "landmark-mode",
    version: VIEWPORT_PROTOCOL_VERSION,
    active: Boolean(active),
    needed: LANDMARKS_NEEDED,
    points: extra.clear ? [] : clonePoints(points),
    ...extra,
  });
}

function snapshotAlignment() {
  return {
    anchorLandmarks: clonePoints(compare.anchorLandmarks),
    linked: compare.linked,
    pairs: new Map([...compare.pairs].map(([sid, pair]) => [sid, {
      mode: pair.mode,
      orientation: { ...pair.orientation },
      transform: pair.transform ? { ...pair.transform } : null,
      fit: pair.fit ? { ...pair.fit } : null,
      landmarks: clonePoints(pair.landmarks),
    }])),
  };
}

function restoreSnapshot(snapshot) {
  compare.anchorLandmarks = clonePoints(snapshot.anchorLandmarks);
  compare.linked = snapshot.linked;
  for (const [sid, saved] of snapshot.pairs) {
    const pair = compare.pairs.get(sid);
    if (!pair) continue;
    Object.assign(pair, {
      mode: saved.mode,
      orientation: { ...saved.orientation },
      transform: saved.transform ? { ...saved.transform } : null,
      fit: saved.fit ? { ...saved.fit } : null,
      landmarks: clonePoints(saved.landmarks),
    });
  }
}

function startLandmarks() {
  if (!compare.enabled || compare.pendingRequest || compare.landmark.active) return;
  closePairPicker(false);
  compare.landmark.active = true;
  compare.landmark.before = snapshotAlignment();
  for (const sid of groupSids()) sendLandmarkMode(sid, true);
  updateCompareControls();
}

function finishLandmarks(keep) {
  if (!compare.landmark.active) return;
  if (!keep && compare.landmark.before) restoreSnapshot(compare.landmark.before);
  compare.landmark.active = false;
  compare.landmark.before = null;
  for (const sid of groupSids()) sendLandmarkMode(sid, false);
  applyDisplayTransforms();
  rememberAlignment();
  syncFromAnchor();
  updateCompareControls();
}

function clearAlignment() {
  if (!compare.enabled || compare.pendingRequest) return;
  requestGroup("clear");
}

function fitPair(sid, announce = true) {
  const pair = compare.pairs.get(sid);
  const anchorState = compare.states.get(compare.anchorSid);
  const memberState = compare.states.get(sid);
  if (!pair || !anchorState || !memberState) return false;
  const n = Math.min(compare.anchorLandmarks.length, pair.landmarks.length);
  if (n < 2) return false;
  const mode = mappingMode(anchorState, memberState);
  const from = compare.anchorLandmarks.slice(0, n).map((p) => pxToSpace(p, anchorState, mode));
  const to = pair.landmarks.slice(0, n).map((p) => pxToSpace(p, memberState, mode));
  const fit = Align.fitSimilarity(from, to);
  if (!fit) return false;
  pair.mode = mode;
  pair.transform = fit.transform;
  pair.fit = {
    pairs: fit.pairs,
    rms: fit.rms,
    reflected: fit.reflected,
    angleDeg: fit.angleDeg,
    scale: fit.scale,
  };
  if (announce) {
    applyDisplayTransform(sid);
    syncFromAnchor();
  }
  return true;
}

function receiveLandmarkPoints(sid, data) {
  if (!compare.landmark.active || !inGroup(sid)) return;
  const points = (Array.isArray(data.points) ? data.points : [])
    .map((p) => ({ x: Number(p.x), y: Number(p.y) }))
    .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))
    .slice(0, LANDMARKS_NEEDED);
  if (sid === compare.anchorSid) {
    compare.anchorLandmarks = points;
    for (const member of compare.members) fitPair(member);
  } else {
    const pair = compare.pairs.get(sid);
    if (!pair) return;
    pair.landmarks = points;
    fitPair(sid);
  }
  updateCompareControls();
}

/* ---- controls --------------------------------------------------------------- */

function formatDeltaUm(value) {
  const sign = value < 0 ? "−" : "+";
  const abs = Math.abs(value);
  if (abs >= 1000) return `${sign}${(abs / 1000).toFixed(2)} mm`;
  return `${sign}${abs.toFixed(abs >= 100 ? 0 : 1)} µm`;
}

function formatRms(pair) {
  if (!pair.fit) return "";
  if (pair.mode === "physical") {
    const v = pair.fit.rms;
    return v >= 1000 ? `${(v / 1000).toFixed(2)} mm` : `${v.toFixed(v >= 100 ? 0 : 1)} µm`;
  }
  return `${(pair.fit.rms * 100).toFixed(2)}%`;
}

function alignmentDeltaLabel() {
  // for a single pair without a fit: the hand-tuned part beyond matched centers
  if (compare.members.length !== 1) return "";
  const sid = compare.members[0];
  const pair = compare.pairs.get(sid);
  const anchorState = compare.states.get(compare.anchorSid);
  const memberState = compare.states.get(sid);
  if (!pair || !pair.transform || pair.fit || !anchorState || !memberState) return "";
  const base = defaultTransform(pair, anchorState, memberState, pair.mode);
  const dx = pair.transform.tx - base.tx;
  const dy = pair.transform.ty - base.ty;
  if (pair.mode === "physical") {
    if (Math.abs(dx) < 0.05 && Math.abs(dy) < 0.05) return "";
    return `Δ ${formatDeltaUm(dx)}, ${formatDeltaUm(dy)}`;
  }
  if (Math.abs(dx) < 0.0005 && Math.abs(dy) < 0.0005) return "";
  const pct = (value) => `${value < 0 ? "−" : "+"}${(Math.abs(value) * 100).toFixed(1)}%`;
  return `Δ ${pct(dx)}, ${pct(dy)}`;
}

function movingSids() {
  return compare.members;
}

function broadcastCompareState() {
  const pending = Boolean(compare.pendingRequest);
  for (const sid of frames.keys()) {
    const member = inGroup(sid);
    postToSlide(sid, {
      nd2wsi: "compare-state",
      version: VIEWPORT_PROTOCOL_VERSION,
      enabled: member,
      linked: member && compare.linked && !pending,
      moving: member && sid !== compare.anchorSid,
      role: member && sid === compare.anchorSid ? "anchor" : "member",
    });
  }
}

function nudgeAlignment(dxPx, dyPx, fromSid) {
  if (!compare.enabled || !compare.linked || compare.pendingRequest) return;
  const dx = Number(dxPx), dy = Number(dyPx);
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return;
  clearViewportRoutes();
  // arrows nudge the pane they were pressed in when it is a member, and the
  // first member when pressed in the anchor
  const target = fromSid && fromSid !== compare.anchorSid ? fromSid : compare.members[0];
  if (!target) return;
  postToSlide(target, {
    nd2wsi: "viewport-nudge",
    version: VIEWPORT_PROTOCOL_VERSION,
    dxPx: Math.max(-200, Math.min(200, dx)),
    dyPx: Math.max(-200, Math.min(200, dy)),
  });
}

function orientationNote(pair) {
  if (pair.fit) {
    const parts = [`${pair.fit.pairs} pts`];
    if (pair.fit.pairs >= 3) parts.push(`rms ${formatRms(pair)}`);
    const deg = Math.round(pair.fit.angleDeg);
    if (Math.abs(deg) >= 1) parts.push(`${deg}°`);
    if (pair.fit.reflected) parts.push("mirror");
    return parts.join(" · ");
  }
  const { x, y } = pair.orientation;
  if (x > 0 && y > 0) return "";
  if (x < 0 && y > 0) return "mirror";
  if (x < 0 && y < 0) return "180°";
  return "vertical mirror";
}

function renderChips() {
  const chips = $("compare-chips");
  chips.replaceChildren();
  const group = groupSids();
  group.forEach((sid, index) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "compare-chip" + (index === 0 ? " anchor" : "");
    chip.dataset.sid = sid;
    const pair = index === 0 ? null : compare.pairs.get(sid);
    const note = pair ? orientationNote(pair) : "";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = slideName(sid);
    chip.append(name);
    if (note) {
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = note;
      chip.append(meta);
    }
    chip.title = index === 0
      ? `${slideName(sid)} is the anchor. Every other slide follows it.`
      : `${slideName(sid)}${note ? " · " + note : ""}. Click to link a different slide in its place.`;
    chip.disabled = index === 0 || Boolean(compare.pendingRequest) || compare.landmark.active;
    chip.onclick = () => openPicker("replace", sid);
    if (index > 0) {
      const remove = document.createElement("span");
      remove.className = "remove";
      remove.textContent = "×";
      remove.title = `Unlink ${slideName(sid)}`;
      remove.setAttribute("role", "button");
      remove.onclick = (event) => {
        event.stopPropagation();
        if (!compare.landmark.active) removeMember(sid, true);
      };
      chip.append(remove);
    }
    chips.append(chip);
  });
}

function renderLandmarkPanel() {
  const panel = $("compare-landmarks");
  const active = compare.landmark.active;
  panel.hidden = !active;
  if (!active) return;
  const rows = $("compare-landmark-rows");
  rows.replaceChildren();
  const anchorCount = compare.anchorLandmarks.length;
  const line = (label, text, cls) => {
    const row = document.createElement("div");
    row.className = "landmark-row" + (cls ? " " + cls : "");
    const a = document.createElement("span");
    a.className = "name";
    a.textContent = label;
    const b = document.createElement("span");
    b.className = "state";
    b.textContent = text;
    row.append(a, b);
    rows.append(row);
  };
  line(slideName(compare.anchorSid), `${anchorCount} of ${LANDMARKS_NEEDED}`, anchorCount >= LANDMARKS_NEEDED ? "done" : "");
  for (const sid of compare.members) {
    const pair = compare.pairs.get(sid);
    const n = pair?.landmarks.length || 0;
    const fitText = pair?.fit
      ? ` · fit ${pair.fit.pairs} pts${pair.fit.pairs >= 3 ? ` · rms ${formatRms(pair)}` : ""}`
      : "";
    line(slideName(sid), `${n} of ${LANDMARKS_NEEDED}${fitText}`, n >= LANDMARKS_NEEDED && pair?.fit ? "done" : "");
  }
  const every = anchorCount >= LANDMARKS_NEEDED &&
    compare.members.every((sid) => (compare.pairs.get(sid)?.landmarks.length || 0) >= LANDMARKS_NEEDED);
  $("compare-landmark-hint").textContent = every
    ? "All points placed. Check the fit, drag a marker to refine, then Done."
    : "Click the same four structures on every slide, in the same order. Pan and zoom freely while placing.";
}

function updateCompareControls() {
  $("compare-controls").hidden = !compare.enabled;
  if (!compare.enabled) {
    renderLandmarkPanel();
    broadcastCompareState();
    return;
  }
  const pending = Boolean(compare.pendingRequest);
  const landmarking = compare.landmark.active;
  const link = $("compare-link");
  link.classList.toggle("linked", compare.linked && !pending);
  link.classList.toggle("pending", pending);
  link.disabled = pending;
  link.setAttribute("aria-pressed", String(compare.linked && !pending));
  link.setAttribute("aria-label", compare.linked ? "Unlink views" : "Relink and capture alignment");
  link.title = compare.linked
    ? "Unlink, move any slide, then relink (L). While linked, arrow keys and Option-drag nudge the alignment."
    : "Relink and keep the current positions as the alignment (L)";

  const members = compare.members.map((sid) => compare.pairs.get(sid)).filter(Boolean);
  const anyUnfitted = members.some((pair) => !pair.fit);
  const rotate = $("compare-rotate");
  const mirror = $("compare-mirror");
  rotate.disabled = pending || landmarking || !anyUnfitted;
  mirror.disabled = pending || landmarking || !anyUnfitted;
  const halfTurn = members.some((pair) => !pair.fit && pair.orientation.y < 0);
  const mirrored = members.some((pair) => !pair.fit && pair.orientation.x !== pair.orientation.y);
  rotate.classList.toggle("rotated", halfTurn);
  rotate.setAttribute("aria-pressed", String(halfTurn));
  mirror.classList.toggle("mirrored", mirrored);
  mirror.setAttribute("aria-pressed", String(mirrored));
  rotate.title = anyUnfitted
    ? "Toggle the 180° scan orientation of the linked slides"
    : "Orientation comes from the landmark fit. Clear the points to set it by hand.";
  mirror.title = anyUnfitted
    ? "Mirror the linked slides left-to-right"
    : "Orientation comes from the landmark fit. Clear the points to set it by hand.";

  $("compare-swap").disabled = pending || landmarking || compare.members.length !== 1;
  const add = $("compare-add");
  const others = slides.filter((slide) => !inGroup(slide.sid));
  add.disabled = pending || landmarking || groupSids().length >= MAX_GROUP || !others.length;
  add.title = groupSids().length >= MAX_GROUP
    ? `Up to ${MAX_GROUP} slides can be linked`
    : others.length ? "Link another open slide" : "Open another slide to link it";
  const align = $("compare-align");
  align.disabled = pending;
  align.classList.toggle("active", landmarking);
  align.setAttribute("aria-pressed", String(landmarking));

  const modes = new Set(members.map((pair) => pair.mode).filter(Boolean));
  let status;
  if (pending) status = "Reading every view…";
  else if (!compare.linked) status = "Unlinked · move any view";
  else if (!members.length || !members.every((pair) => pair.transform)) status = "Waiting for views";
  else status = modes.has("normalized") ? "Linked · relative" : "Linked · µm";
  $("compare-status").textContent = status;

  const delta = compare.linked && !pending ? alignmentDeltaLabel() : "";
  const deltaEl = $("compare-delta");
  deltaEl.textContent = delta;
  deltaEl.hidden = !delta;
  deltaEl.title = delta
    ? "Hand-tuned offset beyond matched centers. Arrow keys move it one screen pixel, Shift ten, Option-drag moves it freely."
    : "";

  renderChips();
  renderLandmarkPanel();
  rememberAlignment();
  broadcastCompareState();
}

/* ---- picker --------------------------------------------------------------- */

function mostRecentOther(exclude) {
  const open = new Set(slides.map((slide) => slide.sid));
  for (let i = compare.mru.length - 1; i >= 0; i -= 1) {
    if (!exclude.includes(compare.mru[i]) && open.has(compare.mru[i])) return compare.mru[i];
  }
  for (let i = slides.length - 1; i >= 0; i -= 1) {
    if (!exclude.includes(slides[i].sid)) return slides[i].sid;
  }
  return null;
}

function pairPickerIsOpen() {
  return pairPicker.open && !$("compare-picker").hidden;
}

function closePairPicker(restoreFocus = true) {
  const wasOpen = pairPicker.open;
  const mode = pairPicker.mode;
  pairPicker.open = false;
  pairPicker.mode = "start";
  pairPicker.replaceSid = null;
  $("compare-picker").hidden = true;
  $("compare-toggle").setAttribute("aria-expanded", "false");
  $("compare-add").setAttribute("aria-expanded", "false");
  if (restoreFocus && wasOpen) $(mode === "start" ? "compare-toggle" : "compare-add").focus();
}

function slideTypeLabel(slide) {
  const match = String(slide?.name || "").match(/\.([^.]+)$/);
  return match ? match[1].toUpperCase() : "SLIDE";
}

function slideSizeLabel(slide) {
  const width = Number(slide?.width);
  const height = Number(slide?.height);
  if (!Number.isFinite(width) || !Number.isFinite(height)) return slideTypeLabel(slide);
  return `${slideTypeLabel(slide)} · ${Math.round(width).toLocaleString()} × ${Math.round(height).toLocaleString()}`;
}

function pickerChoices() {
  const anchor = pairPicker.mode === "start" ? (active || slides[slides.length - 1]?.sid) : compare.anchorSid;
  const taken = pairPicker.mode === "start" ? [anchor] : groupSids().filter((sid) => sid !== pairPicker.replaceSid);
  return { anchor, choices: slides.filter((slide) => !taken.includes(slide.sid)) };
}

function renderPairPicker() {
  const { anchor, choices } = pickerChoices();
  if (!pairPicker.open || !anchor || !choices.length ||
      (pairPicker.mode !== "start" && !compare.enabled)) {
    closePairPicker(false);
    return;
  }
  $("compare-picker-title").textContent = pairPicker.mode === "start"
    ? "Choose the slide to link"
    : pairPicker.mode === "add"
      ? "Link another slide"
      : `Replace ${slideName(pairPicker.replaceSid)}`;
  $("compare-picker-current-name").textContent = slideName(anchor);
  const list = $("compare-picker-list");
  const previousOptions = [...list.querySelectorAll(".compare-picker-option")];
  const focusedIndex = previousOptions.indexOf(document.activeElement);
  const focusedSid = focusedIndex >= 0 ? previousOptions[focusedIndex].dataset.sid : null;
  list.replaceChildren();
  for (const slide of choices) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "compare-picker-option";
    option.setAttribute("role", "menuitem");
    option.dataset.sid = slide.sid;
    const current = pairPicker.mode === "replace" && slide.sid === pairPicker.replaceSid;
    option.classList.toggle("current", current);
    option.title = current ? `${slide.name} is linked now` : `Link ${slideName(anchor)} with ${slide.name}`;
    if (current) option.setAttribute("aria-current", "true");
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = slide.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = current ? "Linked now" : slideSizeLabel(slide);
    option.append(dot, name, meta);
    option.onclick = () => choosePickerSlide(slide.sid);
    list.append(option);
  }
  if (focusedIndex >= 0) {
    const options = [...list.querySelectorAll(".compare-picker-option")];
    const sameSlide = options.find((option) => option.dataset.sid === focusedSid);
    (sameSlide || options[Math.min(focusedIndex, options.length - 1)])?.focus();
  }
}

function openPicker(mode, replaceSid = null) {
  // The user always names the slide to link. With two slides open the list
  // has one entry, and it is still a choice rather than an assignment.
  if (mode !== "start" && (!compare.enabled || compare.pendingRequest || compare.landmark.active)) return;
  if (pairPickerIsOpen() && pairPicker.mode === mode && pairPicker.replaceSid === replaceSid) {
    closePairPicker();
    return;
  }
  pairPicker.open = true;
  pairPicker.mode = mode;
  pairPicker.replaceSid = replaceSid;
  $("compare-picker").hidden = false;
  $(mode === "start" ? "compare-toggle" : "compare-add").setAttribute("aria-expanded", "true");
  renderPairPicker();
  if (!pairPicker.open) return;
  const suggestedSid = mode === "replace" ? replaceSid : mostRecentOther(groupSids().length ? groupSids() : [pickerChoices().anchor]);
  requestAnimationFrame(() => {
    if (!pairPickerIsOpen()) return;
    const options = [...$("compare-picker-list").querySelectorAll(".compare-picker-option")];
    const suggested = options.find((option) => option.dataset.sid === suggestedSid);
    (suggested || options[0])?.focus();
  });
}

function startCompare() {
  if (compare.enabled) return;
  const anchor = active || slides[slides.length - 1]?.sid;
  if (!anchor || slides.length < 2) {
    showError("Open two slides before starting Compare");
    return;
  }
  openPicker("start");
}

function choosePickerSlide(sid) {
  const mode = pairPicker.mode;
  const replaceSid = pairPicker.replaceSid;
  const { anchor } = pickerChoices();
  closePairPicker();
  const openSids = new Set(slides.map((slide) => slide.sid));
  if (!sid || !openSids.has(sid) || sid === anchor) {
    showError("That slide is no longer open. Choose another slide to link.");
    return;
  }
  if (mode === "start") startGroup(anchor, sid);
  else if (mode === "add") addMember(sid);
  else if (mode === "replace") replaceMember(replaceSid, sid);
}

/* ---- group membership ------------------------------------------------------ */

function attachMember(sid) {
  const pair = newPair(compare.anchorSid, sid);
  restoreAlignment(compare.anchorSid, sid, pair);
  compare.pairs.set(sid, pair);
  compare.members.push(sid);
  rememberSlide(sid);
  ensureFrame(sid);
}

function startGroup(anchorSid, memberSid) {
  if (compare.enabled || !anchorSid || !memberSid || anchorSid === memberSid) return;
  compare.enabled = true;
  compare.anchorSid = anchorSid;
  compare.members = [];
  compare.pairs = new Map();
  compare.anchorLandmarks = [];
  compare.linked = true;
  active = anchorSid;
  rememberSlide(anchorSid);
  ensureFrame(anchorSid);
  attachMember(memberSid);
  applyFrameLayout();
  applyDisplayTransforms();
  updateCompareControls();
  render();
  requestGroupSoon("sync");
}

function addMember(sid) {
  if (!compare.enabled || inGroup(sid) || groupSids().length >= MAX_GROUP) return;
  clearPendingRequest();
  clearViewportRoutes();
  attachMember(sid);
  applyFrameLayout();
  applyDisplayTransforms();
  updateCompareControls();
  render();
  requestGroupSoon("sync");
}

function removeMember(sid, andRender) {
  if (!compare.enabled || !compare.members.includes(sid)) return;
  rememberAlignment();
  clearPendingRequest();
  clearViewportRoutes();
  compare.members = compare.members.filter((item) => item !== sid);
  compare.pairs.delete(sid);
  clearDisplayTransforms([sid]);
  sendLandmarkMode(sid, false, { clear: true });
  if (!compare.members.length) {
    stopCompare();
    return;
  }
  applyFrameLayout();
  updateCompareControls();
  if (andRender) render();
  requestGroupSoon("sync");
}

function replaceMember(oldSid, newSid) {
  if (!compare.enabled || !compare.members.includes(oldSid) || inGroup(newSid)) return;
  rememberAlignment();
  clearPendingRequest();
  clearViewportRoutes();
  const index = compare.members.indexOf(oldSid);
  compare.pairs.delete(oldSid);
  clearDisplayTransforms([oldSid]);
  sendLandmarkMode(oldSid, false, { clear: true });
  compare.members.splice(index, 1);
  const pair = newPair(compare.anchorSid, newSid);
  restoreAlignment(compare.anchorSid, newSid, pair);
  compare.pairs.set(newSid, pair);
  compare.members.splice(index, 0, newSid);
  rememberSlide(newSid);
  ensureFrame(newSid);
  applyFrameLayout();
  applyDisplayTransforms();
  updateCompareControls();
  render();
  requestGroupSoon("sync");
}

function stopCompare() {
  closePairPicker(false);
  if (!compare.enabled) return;
  if (compare.landmark.active) finishLandmarks(true);
  rememberAlignment();
  const sids = groupSids();
  clearPendingRequest();
  clearViewportRoutes();
  clearDisplayTransforms(sids);
  for (const sid of sids) sendLandmarkMode(sid, false, { clear: true });
  compare.enabled = false;
  compare.anchorSid = null;
  compare.members = [];
  compare.pairs = new Map();
  compare.anchorLandmarks = [];
  compare.linked = true;
  applyFrameLayout();
  updateCompareControls();
  render();
}

function toggleCompare() {
  if (compare.enabled) stopCompare();
  else if (pairPickerIsOpen()) closePairPicker();
  else startCompare();
}

function toggleViewLink() {
  if (!compare.enabled || compare.pendingRequest) return;
  if (compare.linked) {
    compare.linked = false;
    clearViewportRoutes();
    updateCompareControls();
  } else {
    requestGroup("capture");
  }
}

function toggleHalfTurn() {
  if (!compare.enabled || compare.pendingRequest || compare.landmark.active) return;
  requestGroup("rotate-180");
}

function toggleMirror() {
  if (!compare.enabled || compare.pendingRequest || compare.landmark.active) return;
  requestGroup("mirror");
}

function swapComparedSlides() {
  // only meaningful for one pair: the member becomes the anchor
  if (!compare.enabled || compare.members.length !== 1 || compare.landmark.active) return;
  rememberAlignment();
  closePairPicker(false);
  clearPendingRequest();
  clearViewportRoutes();
  const oldAnchor = compare.anchorSid;
  const oldMember = compare.members[0];
  const old = compare.pairs.get(oldMember);
  const pair = newPair(oldMember, oldAnchor);
  if (old?.transform) {
    const inverse = Align.invert(old.transform);
    if (inverse) {
      pair.mode = old.mode;
      pair.orientation = { ...old.orientation };
      pair.transform = inverse;
      pair.fit = old.fit ? { ...old.fit, angleDeg: -old.fit.angleDeg, scale: 1 / old.fit.scale } : null;
    }
  }
  pair.landmarks = clonePoints(compare.anchorLandmarks);
  compare.anchorLandmarks = clonePoints(old?.landmarks || []);
  compare.anchorSid = oldMember;
  compare.members = [oldAnchor];
  compare.pairs = new Map([[oldAnchor, pair]]);
  active = compare.anchorSid;
  applyFrameLayout();
  applyDisplayTransforms();
  updateCompareControls();
  render();
  requestGroupSoon("sync");
}

$("compare-toggle").onclick = toggleCompare;
$("compare-picker-close").onclick = () => closePairPicker();
$("compare-add").onclick = () => openPicker("add");
$("compare-link").onclick = toggleViewLink;
$("compare-swap").onclick = swapComparedSlides;
$("compare-rotate").onclick = toggleHalfTurn;
$("compare-mirror").onclick = toggleMirror;
$("compare-align").onclick = () => (compare.landmark.active ? finishLandmarks(true) : startLandmarks());
$("compare-close").onclick = stopCompare;
$("compare-landmark-clear").onclick = clearAlignment;
$("compare-landmark-done").onclick = () => finishLandmarks(true);
$("compare-landmark-cancel").onclick = () => finishLandmarks(false);

window.addEventListener("keydown", (event) => {
  if (event.repeat) return;
  if (event.key === "Escape" && pairPickerIsOpen()) {
    event.preventDefault();
    closePairPicker();
  } else if (event.key === "Escape" && compare.landmark.active) {
    event.preventDefault();
    finishLandmarks(false);
  } else if ((event.metaKey || event.ctrlKey) && event.code === "Backslash") {
    event.preventDefault();
    toggleCompare();
  } else if (compare.enabled && !event.metaKey && !event.ctrlKey && !event.altKey &&
             event.key.toLowerCase() === "l") {
    event.preventDefault();
    toggleViewLink();
  }
});

$("compare-picker").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closePairPicker();
    return;
  }
  if (event.key === "Tab") {
    closePairPicker();
    return;
  }
  const options = [...$("compare-picker-list").querySelectorAll(".compare-picker-option")];
  if (!options.length) return;
  const index = options.indexOf(document.activeElement);
  let next = null;
  if (event.key === "ArrowDown") {
    next = index < 0 ? options[0] : options[(index + 1) % options.length];
  } else if (event.key === "ArrowUp") {
    next = index < 0 ? options[options.length - 1]
      : options[(index - 1 + options.length) % options.length];
  }
  else if (event.key === "Home") next = options[0];
  else if (event.key === "End") next = options[options.length - 1];
  if (next) {
    event.preventDefault();
    next.focus();
  }
});

document.addEventListener("pointerdown", (event) => {
  if (!pairPickerIsOpen()) return;
  if ($("compare-picker").contains(event.target) || $("compare-toggle").contains(event.target) ||
      $("compare-add").contains(event.target) || $("compare-chips").contains(event.target)) return;
  closePairPicker(false);
}, true);

const compareDivider = $("compare-divider");
let dividerPointer = null;
compareDivider.addEventListener("pointerdown", (event) => {
  if (!compare.enabled || event.target.closest("button")) return;
  event.preventDefault();
  dividerPointer = event.pointerId;
  compareDivider.setPointerCapture(event.pointerId);
  compareDivider.classList.add("dragging");
});
compareDivider.addEventListener("pointermove", (event) => {
  if (event.pointerId !== dividerPointer) return;
  const rect = $("frames").getBoundingClientRect();
  const pct = ((event.clientX - rect.left) / Math.max(1, rect.width)) * 100;
  compare.split = Math.max(25, Math.min(75, pct));
  applyFrameLayout();
});
function finishDividerDrag(event) {
  if (event.pointerId !== dividerPointer) return;
  dividerPointer = null;
  compareDivider.classList.remove("dragging");
  if (compare.linked) requestGroupSoon("sync");
}
compareDivider.addEventListener("pointerup", finishDividerDrag);
compareDivider.addEventListener("pointercancel", finishDividerDrag);

$("newtab").onclick = () => {
  if (window.pywebview?.api?.pick_paths) {
    window.pywebview.api.pick_paths().then((paths) => {
      if (paths?.length) openMany(paths);
    });
    return;
  }
  const path = prompt("Path to an ND2, SVS, or OME-Zarr store:");
  if (path?.trim()) openPath(path.trim());
};

const zone = $("dropzone");
window.__pydrop_multi = (paths) => {
  zone.hidden = true;
  openMany(paths);
};
window.__pydrop = (path) => window.__pydrop_multi([path]);
window.addEventListener("dragenter", (event) => {
  const types = event.dataTransfer ? Array.from(event.dataTransfer.types || []) : [];
  if (types.includes("Files")) zone.hidden = false;
});
window.addEventListener("dragover", (event) => event.preventDefault());
zone.addEventListener("dragleave", (event) => {
  if (event.target === zone) zone.hidden = true;
});
zone.addEventListener("drop", (event) => {
  event.preventDefault();
  zone.hidden = true;
  if (window.pywebview) return;
  const paths = Array.from(event.dataTransfer.files)
    .map((file) => file.pywebviewFullPath || file.path)
    .filter(Boolean);
  if (paths.length) openMany(paths);
  else showError("The browser hid the dropped file paths. Use + or the macOS app.");
});
window.addEventListener("drop", (event) => event.preventDefault());
document.addEventListener("dragover", (event) => event.preventDefault(), true);
document.addEventListener("drop", (event) => event.preventDefault(), true);

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || !event.data) return;
  const senderSid = frameSidForSource(event.source);
  const kind = event.data.nd2wsi;
  const versioned = event.data.version === VIEWPORT_PROTOCOL_VERSION;
  if (kind === "quit-ready") {
    const pending = quitPreparation;
    if (!pending || !senderSid || !versioned || !pending.targets.has(senderSid)) return;
    if (event.data.sid !== senderSid || event.data.requestId !== pending.requestId) return;
    if (pending.seen.has(senderSid)) return;
    pending.seen.add(senderSid);
    if (!event.data.ok) {
      pending.errors.push(`${slideName(senderSid)}: ${event.data.error || "save failed"}`);
    }
    if (pending.seen.size === pending.targets.size) {
      finishQuitPreparation(pending.errors.length
        ? { ok: false, error: pending.errors.join("; ") }
        : { ok: true, panes: pending.targets.size });
    }
  } else if (kind === "viewport-ready") {
    if (!senderSid || !versioned || (event.data.sid && event.data.sid !== senderSid)) return;
    readyFrames.add(senderSid);
    paneCameUp(senderSid);
  } else if (kind === "viewport-state") {
    if (!senderSid || (event.data.sid && event.data.sid !== senderSid)) return;
    receiveViewportState(event.data, senderSid);
  } else if (kind === "compare-toggle") {
    if (!senderSid || !versioned) return;
    toggleCompare();
  } else if (kind === "compare-link-toggle") {
    if (!senderSid || !versioned) return;
    toggleViewLink();
  } else if (kind === "compare-nudge") {
    if (!senderSid || !versioned || !inGroup(senderSid)) return;
    nudgeAlignment(event.data.dxPx, event.data.dyPx, senderSid);
  } else if (kind === "landmark-points") {
    if (!senderSid || !versioned || (event.data.sid && event.data.sid !== senderSid)) return;
    receiveLandmarkPoints(senderSid, event.data);
  } else if (kind === "landmark-done") {
    if (!senderSid || !versioned || !inGroup(senderSid)) return;
    finishLandmarks(true);
  } else if (kind === "landmark-cancel") {
    if (!senderSid || !versioned || !inGroup(senderSid)) return;
    finishLandmarks(false);
  } else if (kind === "tab-select") {
    if (!senderSid || !versioned) return;
    selectTabByIndex(Number(event.data.index));
  } else if (kind === "window-zoom") {
    // a double-click on a slide's own toolbar, relayed from its frame
    if (!senderSid || !versioned) return;
    requestWindowZoom();
  } else if (kind === "slide-trashed") {
    if (senderSid) refresh();
  } else if (kind === "file-drag") {
    if (senderSid) zone.hidden = false;
  } else if (kind === "theme") {
    // only the front tab colors the chrome; each tab keeps its own look
    const front = frames.get(active);
    if (front && event.source === front.contentWindow) {
      applyShellTheme(event.data.theme);
    }
  }
});

refresh();
