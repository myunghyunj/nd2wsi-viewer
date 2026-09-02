"use strict";

const $ = (id) => document.getElementById(id);
const frames = new Map();
let slides = [];
let active = null;
let busyTab = null;
let busyTimer = null;
let toastTimer = null;
const pairPicker = { open: false, leftSid: null };

const VIEWPORT_PROTOCOL_VERSION = 1;
const VIEWPORT_THROTTLE_MS = 48;
const compare = {
  enabled: false,
  leftSid: null,
  rightSid: null,
  linked: true,
  split: 50,
  mru: [],
  states: new Map(),
  offset: { mode: null, x: 0, y: 0 },
  orientation: { x: 1, y: 1 },
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
window.addEventListener("pywebviewready", markNativeChrome);

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
      if (compare.enabled && [compare.leftSid, compare.rightSid].includes(slide.sid)) {
        classes.push("compare-member");
      }
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
  compareToggle.setAttribute("aria-expanded", String(pairPicker.open));
  if (compare.enabled) {
    const leftName = slides.find((slide) => slide.sid === compare.leftSid)?.name || "Slide";
    const rightName = slides.find((slide) => slide.sid === compare.rightSid)?.name || "Slide";
    document.title = `${leftName} ↔ ${rightName} — nd2wsi-viewer`;
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
  frame.src = `s/${sid}/`;
  frame.title = slides.find((slide) => slide.sid === sid)?.name || "Slide";
  frame.addEventListener("load", () => {
    if (compare.enabled && [compare.leftSid, compare.rightSid].includes(sid)) {
      applyDisplayTransform(sid);
      if (compare.linked || compare.pendingRequest) {
        requestPairSoon(
          compare.pendingRequest?.kind || (compare.offset.mode ? "sync" : "initial")
        );
      }
    }
  });
  frames.set(sid, frame);
  $("frames").append(frame);
  return frame;
}

function applyFrameLayout() {
  document.documentElement.style.setProperty("--compare-split", `${compare.split}%`);
  const divider = $("compare-divider");
  divider.hidden = !compare.enabled;
  divider.setAttribute("aria-hidden", String(!compare.enabled));
  for (const [key, frame] of frames) {
    frame.classList.toggle("active", !compare.enabled && key === active);
    frame.classList.toggle("compare-left", compare.enabled && key === compare.leftSid);
    frame.classList.toggle("compare-right", compare.enabled && key === compare.rightSid);
  }
}

function activate(sid) {
  closePairPicker(false);
  if (compare.enabled && sid && ![compare.leftSid, compare.rightSid].includes(sid)) {
    stopCompare();
  }
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
          compare.states.delete(key);
        }
      }
      compare.mru = compare.mru.filter((sid) => openSids.has(sid));
      if (pairPicker.open) {
        if (!openSids.has(pairPicker.leftSid) || slides.length < 2) {
          closePairPicker(false);
        } else {
          renderPairPicker();
        }
      }
      if (compare.enabled &&
          (!openSids.has(compare.leftSid) || !openSids.has(compare.rightSid))) {
        stopCompare();
      }
      if (selectSid) activate(selectSid);
      else if (!slides.some((slide) => slide.sid === active)) {
        activate(slides.length ? slides[slides.length - 1].sid : null);
      } else {
        applyFrameLayout();
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
  return {
    sid,
    seq: Number.isFinite(Number(data.seq)) ? Number(data.seq) : 0,
    reason: String(data.reason || "user"),
    requestId: data.requestId == null ? null : String(data.requestId),
    echoOf: data.echoOf == null ? null : String(data.echoOf),
    centerPx,
    spanPx,
    imagePx,
    pixelSizeUm,
  };
}

function mappingMode(left, right) {
  return left?.pixelSizeUm && right?.pixelSizeUm ? "physical" : "normalized";
}

function centerInSpace(view, mode) {
  if (mode === "physical") {
    return {
      x: view.centerPx.x * view.pixelSizeUm.x,
      y: view.centerPx.y * view.pixelSizeUm.y,
    };
  }
  return {
    x: view.centerPx.x / view.imagePx.x,
    y: view.centerPx.y / view.imagePx.y,
  };
}

function spanInSpace(view, mode) {
  if (mode === "physical") {
    return {
      x: view.spanPx.x * view.pixelSizeUm.x,
      y: view.spanPx.y * view.pixelSizeUm.y,
    };
  }
  return {
    x: view.spanPx.x / view.imagePx.x,
    y: view.spanPx.y / view.imagePx.y,
  };
}

function imageCenterInSpace(view, mode) {
  if (mode === "physical") {
    return {
      x: view.imagePx.x * view.pixelSizeUm.x / 2,
      y: view.imagePx.y * view.pixelSizeUm.y / 2,
    };
  }
  return { x: 0.5, y: 0.5 };
}

function pointFromSpace(point, target, mode) {
  if (mode === "physical") {
    return {
      x: point.x / target.pixelSizeUm.x,
      y: point.y / target.pixelSizeUm.y,
    };
  }
  return { x: point.x * target.imagePx.x, y: point.y * target.imagePx.y };
}

function slideFormat(sid) {
  const name = String(
    slides.find((slide) => slide.sid === sid)?.name || ""
  ).toLowerCase();
  if (name.endsWith(".svs")) return "svs";
  if (name.endsWith(".nd2")) return "nd2";
  return null;
}

function automaticOrientation(leftSid, rightSid) {
  const formats = new Set([slideFormat(leftSid), slideFormat(rightSid)]);
  return formats.has("svs") && formats.has("nd2")
    ? { x: -1, y: -1 }
    : { x: 1, y: 1 };
}

function halfTurnActive() {
  return compare.orientation.y < 0;
}

function mirrorActive() {
  return compare.orientation.x !== compare.orientation.y;
}

function orientationStatusNote() {
  const { x, y } = compare.orientation;
  if (x > 0 && y > 0) return "";
  if (x < 0 && y > 0) return " · Horizontal mirror";
  if (x < 0 && y < 0) return " · 180°";
  return " · Vertical mirror";
}

function mixedSvsNd2Pair() {
  const formats = new Set([slideFormat(compare.leftSid), slideFormat(compare.rightSid)]);
  return formats.has("svs") && formats.has("nd2");
}

function movingDisplaySid() {
  if (mixedSvsNd2Pair()) {
    return slideFormat(compare.leftSid) === "nd2"
      ? compare.leftSid
      : compare.rightSid;
  }
  // For a same-format/unknown pair, the right pane is the moving image and
  // the left pane remains the visual reference.
  return compare.rightSid;
}

function displayTransformFor(sid) {
  if (!compare.enabled || sid !== movingDisplaySid()) {
    return { degrees: 0, flipped: false };
  }
  return {
    degrees: halfTurnActive() ? 180 : 0,
    flipped: mirrorActive(),
  };
}

function applyDisplayTransform(sid) {
  if (!sid) return;
  const transform = displayTransformFor(sid);
  postToSlide(sid, {
    nd2wsi: "display-transform",
    version: VIEWPORT_PROTOCOL_VERSION,
    ...transform,
  });
}

function applyDisplayTransforms() {
  if (!compare.enabled) return;
  applyDisplayTransform(compare.leftSid);
  applyDisplayTransform(compare.rightSid);
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

function defaultAlignmentOffset(left, right, mode) {
  const a = imageCenterInSpace(left, mode);
  const b = imageCenterInSpace(right, mode);
  return {
    mode,
    x: b.x - compare.orientation.x * a.x,
    y: b.y - compare.orientation.y * a.y,
  };
}

function mappedCenter(sourceSid, point) {
  const sign = compare.orientation;
  if (sourceSid === compare.leftSid) {
    return {
      x: sign.x * point.x + compare.offset.x,
      y: sign.y * point.y + compare.offset.y,
    };
  }
  return {
    x: sign.x * (point.x - compare.offset.x),
    y: sign.y * (point.y - compare.offset.y),
  };
}

function forwardViewport(sourceSid, source) {
  if (!compare.enabled || !compare.linked || compare.pendingRequest) return;
  const targetSid = sourceSid === compare.leftSid
    ? compare.rightSid
    : sourceSid === compare.rightSid
      ? compare.leftSid
      : null;
  if (!targetSid) return;
  const target = compare.states.get(targetSid);
  if (!target) return;
  const mode = mappingMode(source, target);
  if (compare.offset.mode !== mode) {
    const sourceCenter = centerInSpace(source, mode);
    const targetCenter = centerInSpace(target, mode);
    const leftCenter = sourceSid === compare.leftSid ? sourceCenter : targetCenter;
    const rightCenter = sourceSid === compare.leftSid ? targetCenter : sourceCenter;
    compare.offset = {
      mode,
      x: rightCenter.x - compare.orientation.x * leftCenter.x,
      y: rightCenter.y - compare.orientation.y * leftCenter.y,
    };
  }
  const center = mappedCenter(sourceSid, centerInSpace(source, mode));
  const span = spanInSpace(source, mode);
  const centerPx = pointFromSpace(center, target, mode);
  const spanPx = pointFromSpace(span, target, mode);
  const commandId = `shell-vp-${++compare.commandSeq}`;
  postToSlide(targetSid, {
    nd2wsi: "viewport-apply",
    version: VIEWPORT_PROTOCOL_VERSION,
    commandId,
    sourceSid,
    sourceSeq: source.seq,
    centerPx,
    spanPx,
    animate: false,
  });
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

function updateCompareControls() {
  const link = $("compare-link");
  const rotate = $("compare-rotate");
  const mirror = $("compare-mirror");
  const pending = Boolean(compare.pendingRequest);
  rotate.disabled = pending;
  mirror.disabled = pending;
  $("compare-swap").disabled = pending;
  $("compare-reset").disabled = pending;
  link.classList.toggle("linked", compare.linked && !pending);
  link.classList.toggle("pending", pending);
  link.setAttribute("aria-pressed", String(compare.linked && !pending));
  link.setAttribute("aria-label", compare.linked ? "Unlink views" : "Relink and capture alignment");
  link.title = compare.linked
    ? "Unlink, align either slide, then relink (L)"
    : "Relink and keep the current manual offset (L)";
  const halfTurn = halfTurnActive();
  const mirrored = mirrorActive();
  rotate.classList.toggle("rotated", halfTurn);
  rotate.setAttribute("aria-pressed", String(halfTurn));
  mirror.classList.toggle("mirrored", mirrored);
  mirror.setAttribute("aria-pressed", String(mirrored));
  const orientationNote = orientationStatusNote();
  if (pending) {
    $("compare-status").textContent = "Reading both views…" + orientationNote;
  } else if (!compare.linked) {
    $("compare-status").textContent = "Unlinked · align either view" + orientationNote;
  } else {
    const left = compare.states.get(compare.leftSid);
    const right = compare.states.get(compare.rightSid);
    const mode = compare.offset.mode || (left && right ? mappingMode(left, right) : null);
    const status = !left || !right
      ? "Approximate alignment · waiting"
      : mode === "physical"
        ? "Approximate alignment · µm"
        : "Approximate alignment · relative";
    $("compare-status").textContent = status + orientationNote;
  }
}

function mostRecentOther(sid) {
  const open = new Set(slides.map((slide) => slide.sid));
  for (let i = compare.mru.length - 1; i >= 0; i -= 1) {
    if (compare.mru[i] !== sid && open.has(compare.mru[i])) return compare.mru[i];
  }
  for (let i = slides.length - 1; i >= 0; i -= 1) {
    if (slides[i].sid !== sid) return slides[i].sid;
  }
  return null;
}

function pairPickerIsOpen() {
  return pairPicker.open && !$("compare-picker").hidden;
}

function closePairPicker(restoreFocus = true) {
  const wasOpen = pairPicker.open;
  pairPicker.open = false;
  pairPicker.leftSid = null;
  $("compare-picker").hidden = true;
  $("compare-toggle").setAttribute("aria-expanded", "false");
  if (restoreFocus && wasOpen) $("compare-toggle").focus();
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

function renderPairPicker() {
  const left = slides.find((slide) => slide.sid === pairPicker.leftSid);
  const choices = slides.filter((slide) => slide.sid !== pairPicker.leftSid);
  if (!pairPicker.open || !left || !choices.length) {
    closePairPicker(false);
    return;
  }

  $("compare-picker-current-name").textContent = left.name;
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
    option.title = `Link ${left.name} with ${slide.name}`;

    const dot = document.createElement("span");
    dot.className = "dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = slide.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = slideSizeLabel(slide);
    option.append(dot, name, meta);
    option.onclick = () => startComparePair(pairPicker.leftSid, slide.sid);
    list.append(option);
  }
  if (focusedIndex >= 0) {
    const options = [...list.querySelectorAll(".compare-picker-option")];
    const sameSlide = options.find((option) => option.dataset.sid === focusedSid);
    (sameSlide || options[Math.min(focusedIndex, options.length - 1)])?.focus();
  }
}

function startCompare() {
  if (compare.enabled) return;
  const leftSid = active || slides[slides.length - 1]?.sid;
  const choices = slides.filter((slide) => slide.sid !== leftSid);
  if (!leftSid || !choices.length) {
    showError("Open two slides before starting Compare");
    return;
  }
  if (choices.length === 1) {
    startComparePair(leftSid, choices[0].sid);
    return;
  }

  pairPicker.open = true;
  pairPicker.leftSid = leftSid;
  $("compare-picker").hidden = false;
  $("compare-toggle").setAttribute("aria-expanded", "true");
  renderPairPicker();
  const suggestedSid = mostRecentOther(leftSid);
  requestAnimationFrame(() => {
    if (!pairPickerIsOpen()) return;
    const options = [...$("compare-picker-list").querySelectorAll(".compare-picker-option")];
    const suggested = options.find((option) => option.dataset.sid === suggestedSid);
    (suggested || options[0])?.focus();
  });
}

function requestPair(kind) {
  if (!compare.enabled) return;
  clearViewportRoutes();
  clearPendingRequest();
  const requestId = `shell-request-${++compare.requestSeq}`;
  const pending = { requestId, kind, seen: new Set(), timer: null };
  pending.timer = setTimeout(() => {
    if (compare.pendingRequest !== pending) return;
    clearPendingRequest();
    updateCompareControls();
    if (
      kind === "capture" || kind === "rotate-180" ||
      kind === "mirror" || kind === "reset"
    ) {
      showError(
        kind === "capture"
          ? "Could not read both views; alignment remains unlinked"
          : kind === "rotate-180"
            ? "Could not read both views; orientation was not changed"
            : kind === "mirror"
              ? "Could not read both views; mirror was not changed"
              : "Could not read both views; alignment was not reset"
      );
    }
  }, 2500);
  compare.pendingRequest = pending;
  updateCompareControls();
  for (const sid of [compare.leftSid, compare.rightSid]) {
    postToSlide(sid, {
      nd2wsi: "viewport-request",
      version: VIEWPORT_PROTOCOL_VERSION,
      requestId,
    });
  }
}

function requestPairSoon(kind) {
  clearTimeout(compare.layoutRequestTimer);
  compare.layoutRequestTimer = setTimeout(() => {
    compare.layoutRequestTimer = null;
    if (compare.enabled) requestPair(kind);
  }, 80);
}

function finishPairRequest(pending) {
  if (compare.pendingRequest !== pending) return;
  clearPendingRequest();
  const left = compare.states.get(compare.leftSid);
  const right = compare.states.get(compare.rightSid);
  if (!left || !right) {
    updateCompareControls();
    return;
  }
  const mode = mappingMode(left, right);
  if (
    pending.kind === "capture" || pending.kind === "rotate-180" ||
    pending.kind === "mirror"
  ) {
    if (pending.kind === "rotate-180") {
      compare.orientation.x *= -1;
      compare.orientation.y *= -1;
    } else if (pending.kind === "mirror") {
      compare.orientation.x *= -1;
    }
    const a = centerInSpace(left, mode);
    const b = centerInSpace(right, mode);
    compare.offset = {
      mode,
      x: b.x - compare.orientation.x * a.x,
      y: b.y - compare.orientation.y * a.y,
    };
    if (pending.kind === "capture") compare.linked = true;
    if (pending.kind === "rotate-180" || pending.kind === "mirror") {
      applyDisplayTransforms();
    }
  } else {
    if (pending.kind === "initial" || pending.kind === "reset") {
      if (pending.kind === "reset") {
        compare.orientation = automaticOrientation(compare.leftSid, compare.rightSid);
        compare.linked = true;
        applyDisplayTransforms();
      }
      compare.offset = defaultAlignmentOffset(left, right, mode);
    } else if (compare.offset.mode !== mode) {
      const a = centerInSpace(left, mode);
      const b = centerInSpace(right, mode);
      compare.offset = {
        mode,
        x: b.x - compare.orientation.x * a.x,
        y: b.y - compare.orientation.y * a.y,
      };
    }
    if (compare.linked) forwardViewport(compare.leftSid, left);
  }
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
  if (pending && state.requestId === pending.requestId &&
      [compare.leftSid, compare.rightSid].includes(sid)) {
    pending.seen.add(sid);
    if (pending.seen.size === 2) finishPairRequest(pending);
  }
  if (!compare.enabled || !compare.linked || compare.pendingRequest ||
      state.reason !== "user" || state.echoOf) return;
  if (![compare.leftSid, compare.rightSid].includes(sid)) return;
  scheduleViewportRoute(sid, state);
}

function startComparePair(leftSid, rightSid) {
  if (compare.enabled) {
    closePairPicker();
    return;
  }
  const openSids = new Set(slides.map((slide) => slide.sid));
  if (!leftSid || !rightSid || leftSid === rightSid ||
      !openSids.has(leftSid) || !openSids.has(rightSid)) {
    closePairPicker();
    showError("That slide is no longer open. Choose another slide to link.");
    return;
  }
  closePairPicker();
  compare.enabled = true;
  compare.leftSid = leftSid;
  compare.rightSid = rightSid;
  compare.linked = true;
  compare.offset = { mode: null, x: 0, y: 0 };
  compare.orientation = automaticOrientation(leftSid, rightSid);
  active = leftSid;
  rememberSlide(rightSid);
  rememberSlide(leftSid);
  ensureFrame(leftSid);
  ensureFrame(rightSid);
  applyFrameLayout();
  applyDisplayTransforms();
  updateCompareControls();
  render();
  requestPairSoon("initial");
}

function stopCompare() {
  closePairPicker(false);
  if (!compare.enabled) return;
  const comparedSids = [compare.leftSid, compare.rightSid];
  clearPendingRequest();
  clearTimeout(compare.layoutRequestTimer);
  compare.layoutRequestTimer = null;
  clearViewportRoutes();
  clearDisplayTransforms(comparedSids);
  compare.enabled = false;
  compare.leftSid = null;
  compare.rightSid = null;
  compare.linked = true;
  compare.offset = { mode: null, x: 0, y: 0 };
  compare.orientation = { x: 1, y: 1 };
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
    requestPair("capture");
  }
}

function resetAlignment() {
  if (!compare.enabled || compare.pendingRequest) return;
  requestPair("reset");
}

function toggleHalfTurn() {
  if (!compare.enabled || compare.pendingRequest) return;
  requestPair("rotate-180");
}

function toggleMirror() {
  if (!compare.enabled || compare.pendingRequest) return;
  requestPair("mirror");
}

function swapComparedSlides() {
  if (!compare.enabled) return;
  clearPendingRequest();
  clearViewportRoutes();
  [compare.leftSid, compare.rightSid] = [compare.rightSid, compare.leftSid];
  compare.offset = {
    mode: compare.offset.mode,
    x: -compare.orientation.x * compare.offset.x,
    y: -compare.orientation.y * compare.offset.y,
  };
  active = compare.leftSid;
  applyFrameLayout();
  applyDisplayTransforms();
  updateCompareControls();
  render();
  if (compare.linked) requestPairSoon("sync");
}

$("compare-toggle").onclick = toggleCompare;
$("compare-picker-close").onclick = () => closePairPicker();
$("compare-link").onclick = toggleViewLink;
$("compare-swap").onclick = swapComparedSlides;
$("compare-rotate").onclick = toggleHalfTurn;
$("compare-mirror").onclick = toggleMirror;
$("compare-reset").onclick = resetAlignment;
$("compare-close").onclick = stopCompare;

window.addEventListener("keydown", (event) => {
  if (event.repeat) return;
  if (event.key === "Escape" && pairPickerIsOpen()) {
    event.preventDefault();
    closePairPicker();
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
  if ($("compare-picker").contains(event.target) || $("compare-toggle").contains(event.target)) return;
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
  document.documentElement.style.setProperty("--compare-split", `${compare.split}%`);
});
function finishDividerDrag(event) {
  if (event.pointerId !== dividerPointer) return;
  dividerPointer = null;
  compareDivider.classList.remove("dragging");
  if (compare.linked) requestPairSoon("sync");
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
  if (event.data.nd2wsi === "viewport-ready") {
    if (!senderSid || event.data.version !== VIEWPORT_PROTOCOL_VERSION ||
        (event.data.sid && event.data.sid !== senderSid)) return;
    if (compare.enabled && [compare.leftSid, compare.rightSid].includes(senderSid)) {
      applyDisplayTransform(senderSid);
      if (compare.linked || compare.pendingRequest) {
        requestPairSoon(
          compare.pendingRequest?.kind || (compare.offset.mode ? "sync" : "initial")
        );
      }
    }
  } else if (event.data.nd2wsi === "viewport-state") {
    if (!senderSid || (event.data.sid && event.data.sid !== senderSid)) return;
    receiveViewportState(event.data, senderSid);
  } else if (event.data.nd2wsi === "compare-toggle") {
    if (!senderSid || event.data.version !== VIEWPORT_PROTOCOL_VERSION) return;
    toggleCompare();
  } else if (event.data.nd2wsi === "compare-link-toggle") {
    if (!senderSid || event.data.version !== VIEWPORT_PROTOCOL_VERSION) return;
    toggleViewLink();
  } else if (event.data.nd2wsi === "slide-trashed") {
    if (senderSid) refresh();
  } else if (event.data.nd2wsi === "file-drag") {
    if (senderSid) zone.hidden = false;
  } else if (event.data.nd2wsi === "theme") {
    // only the front tab colors the chrome; each tab keeps its own look
    const front = frames.get(active);
    if (front && event.source === front.contentWindow) {
      applyShellTheme(event.data.theme);
    }
  }
});

refresh();
