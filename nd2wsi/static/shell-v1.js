"use strict";

const $ = (id) => document.getElementById(id);
if (typeof crypto !== "undefined" && typeof crypto.randomUUID !== "function" &&
    typeof crypto.getRandomValues === "function") {
  crypto.randomUUID = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
}
const frames = new Map();
const readyFrames = new Set();
const shadingStates = new Map();
const shadingTokens = new Map();
const astromapStates = new Map();
let shadingCommandSeq = 0;
let shadingConfirmation = null;
let slides = [];
let active = null;
const busyTabs = [];
let busyTab = null;
let busyTimer = null;
let toastTimer = null;
let quitPreparation = null;
let quitPreparationRequestId = "";
const quitPreparedPanes = new Map(); // sid -> most recent full preparation id
// An update stopped this window's server after its panes saved. Nothing
// edited from then on could be saved, so the panes stay frozen for good.
let quitTornDown = false;
const pairPicker = { open: false, mode: "start", replaceSid: null };
const slacsDiscovery = {
  snapshot: null, offeredSession: null, shownSessions: new Set(),
  running: false, epoch: 0, timer: null, pending: null,
};

/* Script errors in the shell and its slide panes are kept in the desktop app's
   session log. Reporting never throws, sends at most 20 reports a minute and
   holds a few until the pywebview bridge is ready; a browser shell drops them.
   Errors from the scripts loaded before this one were held by
   early-errors-v1.js and are relayed first. */
const clientErrors = { sent: [], pending: [], limit: 20, periodMs: 60000, maxPending: 10 };

function clientErrorText(value, limit) {
  let text;
  try {
    text = typeof value === "string" ? value : value === undefined || value === null ? "" : String(value);
  } catch (_error) {
    text = "[unprintable]";
  }
  return text.length > limit ? text.slice(0, limit) : text;
}

function describeClientError(kind, error, event = null) {
  try {
    const where = event?.filename ? `${event.filename}:${event.lineno || 0}:${event.colno || 0}` : "";
    return { kind, message: event?.message || error?.message || error, where,
      stack: typeof error?.stack === "string" ? error.stack : "" };
  } catch (_error) {
    return { kind, message: "[unreadable error]", where: "", stack: "" };
  }
}

function sendClientError(payload) {
  const api = window.pywebview?.api;
  if (typeof api?.log_client_event !== "function") return false;
  Promise.resolve(api.log_client_event(payload)).catch(() => {});
  return true;
}

function relayClientError(report) {
  try {
    const now = Date.now();
    while (clientErrors.sent.length && now - clientErrors.sent[0] >= clientErrors.periodMs) clientErrors.sent.shift();
    if (clientErrors.sent.length >= clientErrors.limit) return false;
    clientErrors.sent.push(now);
    const payload = {
      source: report.source === "pane" ? "pane" : "shell",
      kind: clientErrorText(report.kind || "error", 64),
      message: clientErrorText(report.message, 2000),
      where: clientErrorText(report.where, 1000),
      stack: clientErrorText(report.stack, 8000),
    };
    if (!sendClientError(payload) && clientErrors.pending.length < clientErrors.maxPending) {
      clientErrors.pending.push(payload);
    }
    return true;
  } catch (_error) {
    return false;
  }
}

window.addEventListener("error", (event) => {
  relayClientError({ source: "shell", ...describeClientError("error", event.error, event) });
});
window.addEventListener("unhandledrejection", (event) => {
  relayClientError({ source: "shell", ...describeClientError("unhandledrejection", event.reason) });
});
for (const early of window.Nd2EarlyErrors?.drain() || []) {
  relayClientError({ source: "shell", ...describeClientError(early.kind, early.error, early.event) });
}
window.addEventListener("pywebviewready", () => {
  for (const payload of clientErrors.pending.splice(0)) {
    try {
      sendClientError(payload);
    } catch (_error) {
      // The session log is best effort; the shell keeps running.
    }
  }
});

let metalOpening = false;
window.nd2OpenActiveInMetal = async function (retry = false) {
  if (metalOpening) return {ok:false, message:"A new window is already opening"};
  const button = document.getElementById("open-metal-window");
  const status = document.getElementById("window-action-status");
  metalOpening = true;
  if (button) button.disabled = true;
  try {
    const frame = frames.get(active);
    if (!frame || !readyFrames.has(active) || !window.pywebview?.api?.open_in_metal)
      throw new Error("Open a slide before choosing Metal");
    const display = frame.contentWindow.nd2CaptureViewState();
    const result = await window.pywebview.api.open_in_metal(active, display, retry === true);
    if (!result?.ok) throw new Error(result?.message || "Window could not be opened");
    if (status) status.textContent = "New window started; this window and its annotations are unchanged";
    return result;
  } catch (error) {
    const message = `Could not open in Metal: ${error.message || error}`;
    if (status) status.textContent = message;
    if (typeof showError === "function") showError(message);
    return {ok:false, message};
  } finally {
    metalOpening = false;
    if (button) button.disabled = false;
  }
};

const Align = window.nd2wsiAlign;
const ShortcutRouter = window.Nd2ShortcutRouter;
ShortcutRouter?.localizeLabels(document);
const NativeScope = window.Nd2NativeScope;
const VIEWPORT_PROTOCOL_VERSION = 2;
const VIEWPORT_THROTTLE_MS = 48;
const MAX_GROUP = 4; // the anchor and up to three linked slides
const LANDMARKS_NEEDED = 4;

/* A link group has one anchor and up to three members. Each member carries
   its own similarity transform from anchor space to member space, either
   a user-selected orientation (matched centers, rotations and reflections) or a
   least-squares fit through the landmarks the user placed. */
const compare = {
  enabled: false,
  anchorSid: null,
  members: [], // linked slides other than the anchor, in display order
  orientationSid: null, // explicit target; never change every member at once
  toolsVisible: false, // toolbox visibility is independent of group/link state
  toolbarHeight: 0,
  linked: true,
  linkMode: "scale-frame",
  split: 50,
  mru: [],
  states: new Map(),
  pairs: new Map(), // member sid -> { mode, orientation, transform, fit, landmarks }
  anchorLandmarks: [],
  memory: new Map(), // "anchor|member" -> pair snapshot for this session
  landmark: { active: false, edit: null },
  groupSessionId: null,
  groupEpoch: 0,
  committedRevision: 0,
  committedGroup: null,
  anchorSet: { id: null, revision: 0, points: [] },
  retiredInstances: new Map(),
  commandSeq: 0,
  requestSeq: 0,
  pendingRequest: null,
  pendingNudge: null,
  nudgeQueue: [],
  layoutRequestTimer: null,
  routeTimers: new Map(),
  routeLatest: new Map(),
};

const ORIENTATION_ACTIONS = {
  "compare-flip-horizontal": "flip-horizontal",
  "compare-flip-vertical": "flip-vertical",
  "compare-rotate-left": "rotate-left",
  "compare-rotate-right": "rotate-right",
  "compare-transpose": "transpose",
  "compare-orientation-reset": "reset",
};

const compareEngine = window.ND2ShellCompare.createCompare({
  compare,
  pairPicker,
  frames,
  getSlides: () => slides,
  getActive: () => active,
  setActive: (sid) => { active = sid; },
  Align,
  ShortcutRouter,
  ORIENTATION_ACTIONS,
  VIEWPORT_PROTOCOL_VERSION,
  VIEWPORT_THROTTLE_MS,
  MAX_GROUP,
  LANDMARKS_NEEDED,
  $,
  document,
  window,
  crypto: typeof crypto !== "undefined" ? crypto : undefined,
  setTimeout: (...args) => setTimeout(...args),
  clearTimeout: (...args) => clearTimeout(...args),
  requestAnimationFrame: (...args) => requestAnimationFrame(...args),
  groupSids: () => groupSids(),
  inGroup: (sid) => inGroup(sid),
  slideName: (sid) => slideName(sid),
  rememberSlide: (sid) => rememberSlide(sid),
  ensureFrame: (sid) => ensureFrame(sid),
  applyFrameLayout: () => applyFrameLayout(),
  render: () => render(),
  stopCompare: () => stopCompare(),
  postToSlide: (sid, message) => postToSlide(sid, message),
  showError: (message) => showError(message),
  scheduleNativeGestureScopes: () => scheduleNativeGestureScopes(),
  refreshShellToolButtons: () => refreshShellToolButtons(),
  finitePoint: (value, positive) => finitePoint(value, positive),
  localIdentity: (st) => localIdentity(st),
  sameIdentity: (a, b) => sameIdentity(a, b),
});
const {
  syncCompareToolbarSpace, syncCompareToolsVisibility, setCompareToolsVisible,
  currentEnvelope, spatialGroupReady, invalidateSpatialWork, applyDisplayTransforms,
  clearDisplayTransforms, rememberAlignment, requestGroup, requestGroupSoon,
  receiveViewportState, sendLandmarkMode, startLandmarks, validLandmarkMessage,
  commitLandmarkEdit, finishLandmarks, clearAlignment, fitPair, receiveLandmarkPoints,
  broadcastCompareState, nudgeAlignment, updateCompareControls, pairPickerIsOpen,
  closePairPicker, renderPairPicker, openPicker, startCompare, removeMember,
  toggleViewLink, setLinkMenuVisible, setLinkMode, changeOrientation,
  swapComparedSlides, wireCompareInteractions,
} = compareEngine;
Object.assign(window, compareEngine);

function setShellToolsExpanded(expanded) {
  const tools = $("shell-tools"), toggle = $("shell-tools-toggle");
  const show = expanded === true;
  const focusedInside = tools.contains(document.activeElement);
  tools.hidden = !show;
  toggle.setAttribute("aria-expanded", String(show));
  toggle.title = show ? "Hide slide tools" : "Show slide tools";
  toggle.setAttribute("aria-label", toggle.title);
  if (!show && pairPicker.open) closePairPicker(false);
  if (!show && focusedInside) toggle.focus({ preventScroll: true });
  return show;
}

function refreshShellToolButtons() {
  const state = compare.states.get(active);
  const ready = readyFrames.has(active) && !!state?.paneInstanceId && !quitPreparation && !pairPicker.open;
  const info = $("info-toggle");
  info.disabled = !ready;
  info.classList.toggle("active", ready && !!state.infoOpen);
  info.setAttribute("aria-pressed", String(ready && !!state.infoOpen));
  const cloud = $("translucent-toggle");
  const native = typeof window.pywebview?.api?.open_translucent_viewer === "function";
  cloud.disabled = !native || !ready || !state.imageReady || state.plateGrid ||
    !state.translucentAvailable || state.translucentOpening;
  cloud.title = !native ? "The translucent viewer is available in the desktop app" :
    !ready ? "Open an image to use the translucent viewer" : state.plateGrid ?
    "Choose a site before opening the translucent viewer" : !state.translucentAvailable ?
    "Open an image to use the translucent viewer" : state.translucentOpening ?
    "Opening translucent viewer…" : "Open translucent viewer · Right-click its image to load an AstroMapper JPG and TXT pair";
}

function openActiveTranslucent() {
  refreshShellToolButtons();
  if ($("translucent-toggle").disabled) return false;
  const sid = active, identity = localIdentity(compare.states.get(sid));
  return postToSlide(sid, { nd2wsi: "pane-translucent-open", version: VIEWPORT_PROTOCOL_VERSION,
    sid, ...identity });
}

let shellToolsUserToggled = false;
let shellToolsPreference = (() => {
  try {
    const saved = localStorage.getItem("nd2wsi.shellToolsExpanded");
    if (saved === "true") return true;
    if (saved === "false") return false;
  } catch (_) { /* ignore */ }
  return null;
})();

function hydrateShellUiSettings() {
  if (typeof fetch !== "function") return;
  fetch("api/ui-settings", { cache: "no-store" })
    .then((res) => (res && res.ok && typeof res.json === "function" ? res.json() : null))
    .then((data) => {
      if (shellToolsUserToggled || !data) return;
      const root = data.settings && typeof data.settings === "object" ? data.settings : data;
      if (typeof root?.shellToolsExpanded === "boolean") {
        shellToolsPreference = root.shellToolsExpanded;
        try { localStorage.setItem("nd2wsi.shellToolsExpanded", String(root.shellToolsExpanded)); } catch (_) {}
        setShellToolsExpanded(root.shellToolsExpanded);
      }
    })
    .catch(() => {});
}

$("shell-tools-toggle").onclick = () => {
  const next = $("shell-tools").hidden;
  shellToolsUserToggled = true;
  shellToolsPreference = next;
  try { localStorage.setItem("nd2wsi.shellToolsExpanded", String(next)); } catch (_) {}
  setShellToolsExpanded(next);
  if (typeof fetch === "function") {
    fetch("api/ui-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shellToolsExpanded: next }),
    }).catch(() => {});
  }
};
$("translucent-toggle").onclick = () => openActiveTranslucent();
$("info-toggle").onclick = () => panelShortcut("info");
setShellToolsExpanded(shellToolsPreference !== null ? shellToolsPreference : Number(window.innerWidth) >= 900);
hydrateShellUiSettings();
window.addEventListener("pywebviewready", refreshShellToolButtons);

function refreshSlacsOffer() {
  const offer = $("slacs-offer");
  const visible = !!slacsDiscovery.snapshot &&
    slacsDiscovery.offeredSession === slacsDiscovery.snapshot.session_id;
  offer.hidden = !visible;
  if (!visible) return;
  refreshShellToolButtons();
  const cloud = $("translucent-toggle");
  const state = compare.states.get(active);
  const needsImage = !readyFrames.has(active) || !state?.imageReady || !state.translucentAvailable;
  $("slacs-offer-open").disabled = cloud.disabled;
  $("slacs-offer-message").textContent = cloud.disabled ?
    (needsImage ?
      "Open an image with +, then choose Open SLACS view." : cloud.title) :
    "Sniper Cell Sorter is running. Open a translucent view to set up stage following.";
}

function receiveSlacsDiscovery(snapshot) {
  const session = snapshot?.available === true && snapshot.status === "running" &&
    typeof snapshot.session_id === "string" && snapshot.session_id.trim() ? snapshot.session_id : null;
  slacsDiscovery.snapshot = session ? { available: true, status: "running", session_id: session } : null;
  if (!session) slacsDiscovery.offeredSession = null;
  // The backend grants each detected process to one viewer window (offer: true);
  // every other window stays quiet, so the offer appears once across windows.
  else if (snapshot.offer === true && !slacsDiscovery.shownSessions.has(session)) {
    slacsDiscovery.shownSessions.add(session);
    slacsDiscovery.offeredSession = session;
  } else if (slacsDiscovery.offeredSession !== session) {
    slacsDiscovery.offeredSession = null;
  }
  refreshSlacsOffer();
}

function dismissSlacsOffer() {
  slacsDiscovery.offeredSession = null;
  refreshSlacsOffer();
}

function openSlacsOffer() {
  if (!slacsDiscovery.snapshot ||
      slacsDiscovery.offeredSession !== slacsDiscovery.snapshot.session_id) return false;
  if (!openActiveTranslucent()) {
    refreshSlacsOffer();
    return false;
  }
  dismissSlacsOffer();
  return true;
}

// Only the window the user is looking at may claim the single offer for a process.
function slacsMayClaimOffer() {
  return document.visibilityState !== "hidden" &&
    (typeof document.hasFocus !== "function" || document.hasFocus());
}

function pollSlacsDiscovery(epoch) {
  const current = () => slacsDiscovery.running && slacsDiscovery.epoch === epoch;
  if (!current()) return Promise.resolve();
  return new Promise(resolve => {
    const request = { done: false, timeout: null, cancel: () => finish(null) };
    const finish = snapshot => {
      if (request.done) return;
      request.done = true;
      clearTimeout(request.timeout);
      if (slacsDiscovery.pending === request) slacsDiscovery.pending = null;
      if (current()) {
        receiveSlacsDiscovery(snapshot);
        slacsDiscovery.timer = setTimeout(() => pollSlacsDiscovery(epoch), 1000);
      }
      resolve();
    };
    slacsDiscovery.pending = request;
    request.timeout = setTimeout(request.cancel, 3000);
    Promise.resolve().then(() => window.pywebview?.api?.slacs_discovery_state?.(slacsMayClaimOffer()))
      .then(finish, request.cancel);
  });
}

function startSlacsDiscovery() {
  if (slacsDiscovery.running || typeof window.pywebview?.api?.slacs_discovery_state !== "function") return;
  slacsDiscovery.running = true;
  return pollSlacsDiscovery(++slacsDiscovery.epoch);
}

function stopSlacsDiscovery() {
  slacsDiscovery.running = false;
  slacsDiscovery.epoch++;
  clearTimeout(slacsDiscovery.timer);
  slacsDiscovery.timer = null;
  slacsDiscovery.pending?.cancel();
  receiveSlacsDiscovery(null);
}

function bindSlacsDiscovery() {
  $("slacs-offer-open").onclick = openSlacsOffer;
  $("slacs-offer-dismiss").onclick = dismissSlacsOffer;
  window.addEventListener("pywebviewready", startSlacsDiscovery);
  window.addEventListener("pageshow", startSlacsDiscovery);
  window.addEventListener("pagehide", stopSlacsDiscovery);
  refreshSlacsOffer();
  startSlacsDiscovery();
}

function refreshAstromapButton() {
  const button = $("astromap-toggle");
  if (!button) return;
  const status = astromapStates.get(active);
  const open = status?.paneInstanceId === compare.states.get(active)?.paneInstanceId && !!status?.open;
  button.hidden = false;
  button.disabled = !readyFrames.has(active) || !!quitPreparation;
  button.classList.toggle("active", open);
  button.setAttribute("aria-pressed", String(open));
}

function activateAstromap() {
  if (!readyFrames.has(active) || quitPreparation || pairPicker.open) return false;
  const identity = localIdentity(compare.states.get(active));
  if (!identity?.paneInstanceId) return false;
  refreshAstromapButton();
  return postToSlide(active, { nd2wsi: "astromap-toggle", version: VIEWPORT_PROTOCOL_VERSION,
    sid: active, ...identity, open: false });
}

function receiveAstromapShortcut(senderSid, data) {
  if (senderSid !== active || data.sid !== senderSid || !readyFrames.has(senderSid) ||
      !sameIdentity(localIdentity(compare.states.get(senderSid)), data)) return false;
  return activateAstromap();
}

function receiveAstromapState(senderSid, data) {
  if (data.sid !== senderSid || data.paneInstanceId !== compare.states.get(senderSid)?.paneInstanceId) return;
  astromapStates.set(senderSid, { open: !!data.open, paneInstanceId: data.paneInstanceId });
  if (senderSid === active) refreshAstromapButton();
}

$("astromap-toggle").onclick = () => {
  activateAstromap();
};

function showError(message) {
  const toast = $("shell-toast");
  toast.textContent = String(message || "Unknown error");
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 4200);
}

window.nd2wsiUpdateNotice = message => {
  if (!window.nd2wsiUpdateCard?.notice(message)) showError(message);
};

// pywebview's WKWebView can consume a horizontal NSEvent in its native
// scroll view without emitting a DOM wheel event. The macOS bridge calls this
// function only after the physical gesture has latched horizontally. Route it
// to the iframe under the pointer (or the front tab as a conservative fallback).
window.nd2wsiNativeTrackpad = (input) => {
  const point = NativeScope?.pointFromInput(input, {
    width: window.innerWidth,
    height: window.innerHeight,
  });
  const captured = typeof input?.target === "string" ? input.target : null;
  let frame = captured ? frames.get(captured) : null;
  if (captured && !frame) return false;
  if (!frame && point) frame = document.elementFromPoint(point.x, point.y)?.closest?.("iframe");
  if (!frame || !frames.has(frame.dataset.sid)) frame = frames.get(active);
  if (!frame?.contentWindow) return false;
  const rect = frame.getBoundingClientRect();
  const local = NativeScope?.pointInFrame(point || {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  }, {
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
    clientWidth: frame.clientWidth,
    clientHeight: frame.clientHeight,
  });
  frame.contentWindow.postMessage({
    nd2wsi: "native-trackpad",
    version: VIEWPORT_PROTOCOL_VERSION,
    deltaX: Number(input?.deltaX) || 0,
    gestureStart: !!input?.gestureStart,
    clientX: local ? local.x : frame.clientWidth / 2,
    clientY: local ? local.y : frame.clientHeight / 2,
    altKey: !!input?.altKey,
  }, location.origin);
  return frame.dataset.sid || true;
};

function refreshShadingButton() {
  if (shadingConfirmation && !shadingConfirmationMatches()) closeShadingConfirmation(false);
  if (shadingConfirmation) refreshShadingConfirmation();
  const button = $("shading-correction");
  if (!button) return;
  const status = shadingStates.get(active);
  const view = window.Nd2ShadingCorrection.describe(status);
  button.disabled = view.disabled || !readyFrames.has(active) || Boolean(quitPreparation) || Boolean(status?.blocked);
  button.classList.toggle("active", Boolean(status?.applied));
  button.classList.toggle("preparing", view.busy);
  button.setAttribute("aria-pressed", String(Boolean(status?.applied)));
  button.setAttribute("aria-busy", String(view.busy));
  button.setAttribute("aria-label", view.label);
  button.title = view.label;
  $("shading-status").textContent = view.label;
  const progress = window.Nd2ShadingCorrection.progress(status);
  const outcome = status?.outcome;
  $("shading-progress").hidden = !progress.visible && !outcome;
  $("shading-progress").setAttribute("aria-label", outcome ? "Shading Correction result" : "Shading Correction progress");
  $("shading-pct").hidden = !!outcome;
  $("shading-progress-bar").hidden = !!outcome;
  $("shading-cancel").textContent = outcome ? "Dismiss" : "Cancel";
  if (outcome) {
    $("shading-progress-title").textContent = outcome.title;
    $("shading-stage").textContent = outcome.reason;
    $("shading-eta").textContent = outcome.note;
    $("shading-cancel").disabled = !readyFrames.has(active);
  } else if (progress.visible) {
    $("shading-progress-title").textContent = progress.title;
    $("shading-stage").textContent = progress.stage;
    $("shading-pct").textContent = progress.pct + "%";
    $("shading-progress-bar").value = progress.pct;
    $("shading-eta").textContent = progress.eta;
    $("shading-cancel").disabled = button.disabled || !progress.canCancel;
  }
}

function dismissShadingOutcome() {
  const sid = active, status = shadingStates.get(sid);
  if (!status?.outcome || !readyFrames.has(sid)) return false;
  return postToSlide(sid, { nd2wsi: "shading-command", version: VIEWPORT_PROTOCOL_VERSION, action: "dismiss",
    sid, shellToken: shadingTokens.get(sid), paneInstanceId: status.paneInstanceId,
    contextKey: status.contextKey, commandSeq: ++shadingCommandSeq });
}

function requestShadingState(sid, renew = true) {
  const token = (!renew && shadingTokens.get(sid)) || crypto.randomUUID();
  shadingTokens.set(sid, token);
  if (renew) shadingStates.delete(sid);
  postToSlide(sid, { nd2wsi: "shading-status-request", version: VIEWPORT_PROTOCOL_VERSION,
    sid, shellToken: token });
  refreshShadingButton();
}

function receiveShadingState(sid, data) {
  if (data.sid !== sid || typeof data.shellToken !== "string" || !data.shellToken ||
      data.shellToken !== shadingTokens.get(sid) || typeof data.paneInstanceId !== "string" ||
      !data.paneInstanceId || !Number.isSafeInteger(data.seq)) return false;
  const old = shadingStates.get(sid);
  if (old && (old.paneInstanceId !== data.paneInstanceId || old.seq >= data.seq)) return false;
  shadingStates.set(sid, data);
  if (sid === active) refreshShadingButton();
  return true;
}

function activateShadingCorrection() {
  const sid = active, status = shadingStates.get(sid);
  if (!status || !readyFrames.has(sid) || quitPreparation || status.blocked ||
      window.Nd2ShadingCorrection.describe(status).disabled) return false;
  const action = window.Nd2ShadingCorrection.nextAction(status);
  if (!action) return false;
  if (action !== "cancel") {
    if (shadingConfirmation && shadingConfirmationMatches()) return true;
    closeShadingConfirmation(false);
    shadingConfirmation = { sid, shellToken: shadingTokens.get(sid),
      paneInstanceId: status.paneInstanceId, contextKey: status.contextKey,
      generation: status.generation, state: status.state, action,
      ...(action === "start" ? { method: "bffree" } : {}) };
    const revoke = action === "revoke";
    $("shading-confirm").classList.toggle("revoke", revoke);
    $("shading-confirm-title").textContent = revoke ? "Revoke Shading Correction?" : "Shading Correction";
    $("shading-confirm-continue").textContent = revoke ? "Revoke" : "Continue";
    refreshShadingConfirmation();
    $("shading-confirm").showModal();
    $("shading-confirm-cancel").focus({ preventScroll: true });
    return true;
  }
  return postToSlide(sid, { nd2wsi: "shading-command", version: VIEWPORT_PROTOCOL_VERSION, action,
    sid, shellToken: shadingTokens.get(sid), paneInstanceId: status.paneInstanceId,
    contextKey: status.contextKey, commandSeq: ++shadingCommandSeq });
}

function refreshShadingConfirmation() {
  const pending = shadingConfirmation;
  if (!pending) return;
  const revoke = pending.action === "revoke", status = shadingStates.get(pending.sid);
  $("shading-confirm-persistence").textContent = revoke
    ? "Rebuild the original pyramid from the source ND2."
    : "Applying correction rebuilds and saves this slide's display pyramid. Original measurements stay unchanged.";
  $("shading-confirm-estimate").textContent = window.Nd2ShadingCorrection.estimateLabel(status.estimated_seconds);
  $("shading-confirm-continue").disabled = !revoke &&
    !window.Nd2ShadingCorrection.methodSupport(status).supported;
}

function shadingConfirmationMatches() {
  const pending = shadingConfirmation;
  if (!pending || pending.sid !== active || !readyFrames.has(active) || quitPreparation) return false;
  const status = shadingStates.get(active);
  return !!status && !status.blocked && status.state === pending.state &&
    window.Nd2ShadingCorrection.nextAction(status) === pending.action &&
    status.paneInstanceId === pending.paneInstanceId && status.contextKey === pending.contextKey &&
    status.generation === pending.generation && shadingTokens.get(active) === pending.shellToken &&
    !window.Nd2ShadingCorrection.describe(status).disabled;
}

function closeShadingConfirmation(restoreFocus = true) {
  const pending = shadingConfirmation;
  shadingConfirmation = null;
  const dialog = $("shading-confirm");
  if (dialog.open) dialog.close();
  if (restoreFocus && pending?.sid === active) $("shading-correction").focus({ preventScroll: true });
}

function continueShadingConfirmation() {
  if (!shadingConfirmationMatches()) { closeShadingConfirmation(false); return false; }
  const pending = shadingConfirmation;
  if (pending.action === "start" &&
      !window.Nd2ShadingCorrection.methodSupport(shadingStates.get(active)).supported) {
    refreshShadingConfirmation();
    return false;
  }
  const selection = pending.action === "start" ? { method: "bffree" } : {};
  const { method, ...captured } = pending;
  closeShadingConfirmation();
  return postToSlide(pending.sid, { nd2wsi: "shading-command", version: VIEWPORT_PROTOCOL_VERSION,
    ...captured, ...selection, confirmed: true, commandSeq: ++shadingCommandSeq });
}

function dismissShadingConfirmation(event) {
  if (event.type === "cancel") {
    event.preventDefault();
    closeShadingConfirmation();
  } else if (event.target === $("shading-confirm")) {
    const box = event.target.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) {
      closeShadingConfirmation();
    }
  }
}

$("shading-correction").addEventListener("click", activateShadingCorrection);
$("shading-cancel").addEventListener("click", () => {
  if (shadingStates.get(active)?.outcome) dismissShadingOutcome();
  else activateShadingCorrection();
});
$("shading-confirm-continue").addEventListener("click", continueShadingConfirmation);
$("shading-confirm-cancel").addEventListener("click", () => closeShadingConfirmation());
$("shading-confirm").addEventListener("cancel", dismissShadingConfirmation);
$("shading-confirm").addEventListener("click", dismissShadingConfirmation);
$("shading-confirm").addEventListener("close", () => closeShadingConfirmation(false));

function applyShellTheme(theme) {
  document.documentElement.classList.toggle("light", theme === "light");
}

// the shell chrome always mirrors the front tab; with no tab open it
// stays on the app's own dark ground

function markNativeChrome() {
  // inside the packaged app the title bar is hidden and the traffic
  // lights float over the tab strip, which needs room and a drag handle
  document.documentElement.classList.toggle("native-chrome", ShortcutRouter?.isMac() ?? false);
}
if (window.pywebview !== undefined) markNativeChrome();
window.addEventListener("pywebviewready", () => {
  markNativeChrome();
  const api = window.pywebview?.api;
  if (api?.begin_native_gesture_scope_session) {
    Promise.resolve(api.begin_native_gesture_scope_session())
      .then((result) => {
        nativeGestureScopeToken = typeof result?.token === "string" ? result.token : null;
        nativeGestureScopeRevision = 0;
        scheduleNativeGestureScopes();
      })
      .catch(() => { nativeGestureScopeToken = null; });
  }
});

// The window has no title bar of its own, so the tab strip plays the part:
// a double-click on its empty space zooms the window, as a title bar
// would. Tabs, buttons, and the picker keep their double-clicks. In a
// plain browser there is no window to zoom, and nothing happens.
function selectTabByIndex(index) {
  // ⌘1 to ⌘9: the nth open slide, in tab order
  const slide = slides[index];
  if (!slide) return false;
  if (slide.sid !== active) activate(slide.sid);
  return true;
}
window.addEventListener("keydown", (event) => {
  if ($("shading-confirm")?.open) return;
  const index = ShortcutRouter ? ShortcutRouter.tabIndexForEvent(event) : null;
  if (index === null || !slides[index]) return;
  event.preventDefault();
  selectTabByIndex(index);
});

function requestWindowZoom() {
  const api = window.pywebview?.api;
  if (api?.title_bar_double_click) api.title_bar_double_click();
}
// Both presses must land on the strip's empty space (the drag regions):
// closing a tab rebuilds the strip under the pointer, so the second click
// of a double-click on a close button would otherwise count as bare.
const bareStrip = (target) => target instanceof Element && target.classList.contains("pywebview-drag-region");
const nativeWindowDrag = () => window.pywebview?.platform === "cocoa"
  && typeof window.pywebview?.api?.start_window_drag === "function";
let stripPresses = [];
$("tabbar").addEventListener("pointerdown", (event) => {
  stripPresses = [...stripPresses.slice(-1), { at: event.timeStamp, bare: bareStrip(event.target) }];
});
$("tabbar").addEventListener("mousedown", (event) => {
  if (!nativeWindowDrag() || !event.isTrusted || event.button !== 0 || !bareStrip(event.target)) return;
  const recent = stripPresses.filter((press) => event.timeStamp - press.at < 1000);
  // A close button can disappear under the pointer between the two presses.
  // Do not reinterpret that second click as a title-bar double-click.
  if (event.detail === 2 && recent.length === 2 && !recent.every((press) => press.bare)) return;
  event.preventDefault();
  Promise.resolve(window.pywebview.api.start_window_drag()).catch(() => {});
});
$("tabbar").addEventListener("dblclick", (event) => {
  if (nativeWindowDrag()) return; // AppKit uses the original native click count.
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

function updateBusyTabNode(entry) {
  if (!entry?._tabEl) return false;
  entry._tabEl.title = entry.name;
  if (entry._nameEl) entry._nameEl.textContent = entry.name;
  if (entry._fillEl) {
    if (entry.pct > 0) {
      entry._fillEl.classList.remove("indet");
      entry._fillEl.style.width = `${entry.pct}%`;
    } else {
      entry._fillEl.style.width = "";
      entry._fillEl.classList.add("indet");
    }
  }
  return true;
}

function render() {
  refreshShellToolButtons();
  refreshShadingButton();
  refreshAstromapButton();
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
      slide._tabEl = tab;
      slide._nameEl = name;
      slide._fillEl = fill;
    } else {
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(slide.sid === active));
      tab.tabIndex = slide.sid === active ? 0 : -1;
      const close = document.createElement("button");
      close.className = "x";
      close.type = "button";
      close.textContent = "×";
      close.title = "Close tab";
      close.setAttribute("aria-label", `Close ${slide.name}`);
      close.onclick = (event) => {
        event.stopPropagation();
        closeTab(slide.sid);
      };
      tab.append(close);
      tab.onclick = () => activate(slide.sid);
      tab.addEventListener("keydown", (event) => {
        if (event.target !== tab) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate(slide.sid);
        }
      });
    }
    bar.insertBefore(tab, plus);
  }

  slides.forEach((slide) => makeTab(slide, false));
  if (busyTabs.length) {
    busyTabs.forEach((entry) => makeTab(entry, true));
  } else if (busyTab) {
    makeTab(busyTab, true);
  }
  $("empty").hidden = slides.length > 0 || busyTabs.length > 0 || Boolean(busyTab);
  const compareToggle = $("compare-toggle");
  compareToggle.disabled = slides.length < 2;
  compareToggle.classList.toggle("active", compare.enabled);
  compareToggle.setAttribute("aria-pressed", String(compare.enabled));
  compareToggle.setAttribute("aria-expanded", String(pairPicker.open && pairPicker.mode === "start"));
  compareToggle.setAttribute("aria-label", compare.enabled ? "Stop comparing slides" : "Compare slides");
  compareToggle.title = ShortcutRouter.formatShortcutText(
    compare.enabled ? "Stop comparing slides (⌘\\)" : "Compare slides (⌘\\)"
  );
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
  frame.addEventListener("load", () => { requestShadingState(sid); paneCameUp(sid); });
  frames.set(sid, frame);
  $("frames").append(frame);
  return frame;
}

function sendTabShortcutState(sid) {
  const frame = frames.get(sid);
  if (!frame?.contentWindow) return;
  frame.contentWindow.postMessage({
    nd2wsi: "tab-shortcut-state",
    version: VIEWPORT_PROTOCOL_VERSION,
    count: slides.length,
  }, location.origin);
}

function broadcastTabShortcutState() {
  for (const sid of frames.keys()) sendTabShortcutState(sid);
}

function focusActivePane(deferred = false) {
  if (typeof document.hasFocus === "function" && !document.hasFocus()) return false;
  const frame = frames.get(active);
  if (!frame || !readyFrames.has(active) || pairPicker.open || quitPreparation) return false;
  const focused = document.activeElement;
  // Already inside the selected pane: leave its annotation editor/controls alone.
  if (focused === frame) return true;
  if (ShortcutRouter.isFocusControl(focused)) return false;
  const otherPane = [...frames.values()].find((item) => item === focused);
  if (otherPane && compare.enabled && inGroup(otherPane.dataset.sid)) return false;
  // A late ready message must not take focus from a control the user selected
  // while the image loaded. A hidden old iframe can safely yield focus.
  if (deferred && focused && focused !== document.body &&
      focused !== document.documentElement && !otherPane) return false;
  frame.focus({preventScroll: true});
  frame.contentWindow?.focus();
  return true;
}

function paneCameUp(sid) {
  sendTabShortcutState(sid);
  refreshShellToolButtons();
  refreshShadingButton();
  refreshAstromapButton();
  if (sid === active) focusActivePane(true);
  scheduleNativeGestureScopes();
  if (!inGroup(sid)) return;
  broadcastCompareState();
  if (!compare.pendingRequest && spatialGroupReady("sync")) {
    applyDisplayTransforms();
    if (compare.landmark.active) sendLandmarkMode(sid, true);
    requestGroupSoon("sync");
  }
}

function applyFrameLayout() {
  document.documentElement.style.setProperty("--compare-split", `${compare.split}%`);
  const group = groupSids();
  const divider = $("compare-divider");
  const twoUp = compare.enabled && group.length === 2;
  divider.hidden = !twoUp;
  divider.setAttribute("aria-hidden", String(!twoUp));
  for (const [key, frame] of frames) {
    const index = group.indexOf(key);
    const shown = !compare.enabled && key === active;
    frame.classList.toggle("active", shown);
    frame.classList.toggle("compare-cell", index >= 0);
    // Tell a pane when it is hidden or shown again, so it can pause work
    // nobody sees (plate playback). Only changes are posted; a pane that is
    // still loading counts itself visible and cannot be played while hidden.
    const visible = shown || index >= 0;
    if (frame.dataset.paneVisible !== String(visible)) {
      frame.dataset.paneVisible = String(visible);
      frame.contentWindow?.postMessage({
        nd2wsi: "pane-visibility", version: VIEWPORT_PROTOCOL_VERSION, visible,
      }, location.origin);
    }
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
  syncCompareToolsVisibility();
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
  } else if (!active) {
    applyShellTheme("dark");
  }
  render();
  focusActivePane();
}

// Browser mode: the panes tear their save tracking down on pagehide, so a
// window restored from the back/forward cache loads afresh, every pane with it.
function reloadIfRestoredFromCache(event) {
  if (event.persisted) location.reload();
}
window.addEventListener("pageshow", reloadIfRestoredFromCache);

function refresh(selectSid, beforeActivate) {
  const seq = (refresh._seq = (refresh._seq || 0) + 1);
  return fetch("api/slides", { cache: "no-store" })
    .then(async (response) => {
      if (response && response.ok === false) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!data || !Array.isArray(data.slides)) {
        throw new Error("Invalid slide list payload");
      }
      return data;
    })
    .then((data) => {
      if (seq !== refresh._seq) return;
      slides = data.slides;
      if (typeof beforeActivate === "function") beforeActivate();
      const openSids = new Set(slides.map((slide) => slide.sid));
      for (const [key, frame] of [...frames]) {
        if (!openSids.has(key)) {
          frame.remove();
          frames.delete(key);
          forgetClosedPane(key);
          nativeGesturePaneScopes.delete(key);
          readyFrames.delete(key);
          shadingStates.delete(key);
          astromapStates.delete(key);
          shadingTokens.delete(key);
          compare.states.delete(key);
        }
      }
      compare.mru = compare.mru.filter((sid) => openSids.has(sid));
      broadcastTabShortcutState();
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
      if (selectSid && openSids.has(selectSid)) activate(selectSid);
      else if (!slides.some((slide) => slide.sid === active)) {
        activate(slides.length ? slides[slides.length - 1].sid : null);
      } else {
        applyFrameLayout();
        updateCompareControls();
        render();
      }
    })
    .catch((error) => {
      if (seq !== refresh._seq) return;
      showError(`Could not refresh slides: ${error}`);
    });
}

function openMany(paths) {
  if (quitTornDown && window.pywebview?.api?.open_paths) {
    return window.pywebview.api
      .open_paths(paths)
      .then(async (url) => {
        if (url) {
          if (window.nd2wsiUpdateCard?.handoff) {
            await window.nd2wsiUpdateCard.handoff();
          }
          location.replace(url);
          return;
        }
        const status = await Promise.resolve(window.pywebview.api.status?.()).catch(() => "");
        if (status) showError(status);
      })
      .catch((error) => showError(`Open failed: ${error.message || error}`));
  }
  return paths.reduce((chain, path) => chain.then(() => openPath(path)), Promise.resolve());
}

function finishBusyEntry(entry) {
  clearInterval(entry.timer);
  const idx = busyTabs.indexOf(entry);
  if (idx !== -1) busyTabs.splice(idx, 1);
  busyTab = busyTabs[busyTabs.length - 1] || null;
  busyTimer = busyTab ? busyTab.timer : null;
}

function openPath(path) {
  const job = Math.random().toString(36).slice(2, 10);
  const name = String(path).split(/[\\/]/).pop();
  const entry = { name: `Opening ${name}…`, job, pct: null, timer: null };
  busyTabs.push(entry);
  busyTab = entry;
  render();

  entry.timer = setInterval(() => {
    fetch(`api/roi/progress?job=${job}`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data) => {
        if (!busyTabs.includes(entry) || data.state !== "converting") return;
        entry.pct = data.pct || 0;
        entry.name = entry.pct > 0
          ? `Converting ${name} ${entry.pct} %`
          : `Preparing ${name}…`;
        if (!updateBusyTabNode(entry)) render();
      })
      .catch(() => {});
  }, 400);
  busyTimer = entry.timer;

  return fetch("api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, job }),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.error) {
        finishBusyEntry(entry);
        render();
        showError(data.error);
        return;
      }
      return refresh(data.sid, () => finishBusyEntry(entry)).finally(() => {
        finishBusyEntry(entry);
      });
    })
    .catch((error) => {
      finishBusyEntry(entry);
      render();
      showError(`Open failed: ${error}`);
    });
}

let tabClosePending = false;

async function closeTab(sid) {
  if (!slides.some((slide) => slide.sid === sid)) return false;
  if (tabClosePending || quitPreparation || quitPreparationRequestId) {
    showError("Finish the current save or close operation before closing another tab.");
    return false;
  }
  if (frames.has(sid) && !readyFrames.has(sid)) {
    showError("This slide is still loading. Wait before closing its tab.");
    return false;
  }
  const requestId = `tab-close-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  tabClosePending = true;
  try {
    const api = window.pywebview?.api;
    if (api?.update_block_reason) {
      const reason = await api.update_block_reason();
      if (reason) throw new Error(reason);
    }
    // An update or window close may have started preparing while the native
    // check was answering; restarting it here would unlock its panes.
    if (quitPreparation || quitPreparationRequestId) {
      throw new Error("Finish the current save or close operation before closing another tab.");
    }
    // Removing an iframe would discard its editor, debounce and failed-save
    // snapshots. Reuse the acknowledged flush, including older plate sites,
    // and keep every local pane locked until the server accepts the close.
    const prepared = await window.nd2wsiPrepareForUpdate(requestId);
    if (!prepared?.ok) throw new Error(prepared?.error || "Annotations were not saved");
    if (quitPreparationRequestId !== requestId) {
      throw new Error("Close was superseded by another save operation");
    }
    const response = await fetch("api/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid }),
    });
    const data = await response.json();
    if (!response.ok || data.error || data.ok !== true) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    if (api?.close_translucent_viewer) {
      await Promise.resolve(api.close_translucent_viewer(sid)).catch(() => {});
    }
    await refresh();
    return true;
  } catch (error) {
    showError(`Close cancelled: ${error.message || error}`);
    return false;
  } finally {
    // Also runs after conflicts, export refusal, timeout or network failure.
    // Cancellation is request-scoped; it cannot unlock a later preparation.
    window.nd2wsiCancelUpdate(requestId);
    tabClosePending = false;
  }
}

/* ---- pane messages ------------------------------------------------------- */

function frameSidForSource(source) {
  for (const [sid, frame] of frames) {
    if (frame.contentWindow === source) return sid;
  }
  return null;
}

const nativeGesturePaneScopes = new Map();
let nativeGestureScopeRevision = 0;
let nativeGestureScopeFrame = null;
let nativeGestureScopeToken = null;

function receiveNativeGestureScope(sid, data) {
  if (!sid || !NativeScope) return;
  if (!data.enabled) {
    nativeGesturePaneScopes.delete(sid);
    scheduleNativeGestureScopes();
    return;
  }
  const include = NativeScope.rect(data.include);
  if (!include) {
    nativeGesturePaneScopes.delete(sid);
    scheduleNativeGestureScopes();
    return;
  }
  const exclude = (Array.isArray(data.exclude) ? data.exclude : [])
    .map((item) => NativeScope.rect(item))
    .filter(Boolean);
  nativeGesturePaneScopes.set(sid, { include, exclude });
  scheduleNativeGestureScopes();
}

function scheduleNativeGestureScopes() {
  if (nativeGestureScopeFrame !== null) return;
  nativeGestureScopeFrame = requestAnimationFrame(() => {
    nativeGestureScopeFrame = null;
    publishNativeGestureScopes();
  });
}

function publishNativeGestureScopes() {
  const api = window.pywebview?.api;
  if (!api?.set_native_gesture_scopes || !NativeScope || !nativeGestureScopeToken) return;
  const entries = [...nativeGesturePaneScopes.entries()].reverse().map(([sid, scope]) => {
    const frame = frames.get(sid);
    if (!frame) return { target: sid, visible: false };
    const bounds = frame.getBoundingClientRect();
    const style = getComputedStyle(frame);
    return {
      target: sid,
      visible: bounds.width > 0 && bounds.height > 0 &&
        style.display !== "none" && style.visibility !== "hidden",
      frame: {
        left: bounds.left,
        top: bounds.top,
        right: bounds.right,
        bottom: bounds.bottom,
        clientWidth: frame.clientWidth,
        clientHeight: frame.clientHeight,
      },
      include: scope.include,
      exclude: scope.exclude,
    };
  });
  const viewport = {
    width: window.innerWidth,
    height: window.innerHeight,
  };
  let scopes = NativeScope.buildScopes(entries, viewport);
  const shellBlockers = ["compare-controls", "compare-divider", "compare-picker", "dropzone", "shading-progress"]
    .map((id) => $(id))
    .filter((element) => {
      if (!element || element.hidden) return false;
      const style = getComputedStyle(element);
      const bounds = element.getBoundingClientRect();
      return bounds.width > 0 && bounds.height > 0 &&
        style.display !== "none" && style.visibility !== "hidden";
    })
    .map((element) => NativeScope.normalizeRect(element.getBoundingClientRect(), viewport))
    .filter(Boolean);
  scopes = scopes.map((scope) => ({
    ...scope,
    exclude: [...scope.exclude, ...shellBlockers],
  }));
  if (document.documentElement.classList.contains("preparing-update") || $("shading-confirm")?.open) scopes = [];
  const revision = ++nativeGestureScopeRevision;
  Promise.resolve(api.set_native_gesture_scopes({
    token: nativeGestureScopeToken,
    revision,
    scopes,
  })).catch(() => {});
}

window.addEventListener("resize", scheduleNativeGestureScopes);
const nativeGestureShellBlockers = ["compare-controls", "compare-divider", "compare-picker", "dropzone", "shading-progress", "shading-confirm"]
  .map((id) => $(id))
  .filter(Boolean);
if (typeof MutationObserver === "function") {
  const blockerObserver = new MutationObserver(scheduleNativeGestureScopes);
  nativeGestureShellBlockers.forEach((element) => blockerObserver.observe(
    element,
    { attributes: true, attributeFilter: ["class", "hidden", "style", "open"] }
  ));
}
if (typeof ResizeObserver === "function") {
  const blockerSizes = new ResizeObserver(() => {
    syncCompareToolbarSpace();
    scheduleNativeGestureScopes();
  });
  nativeGestureShellBlockers.forEach((element) => blockerSizes.observe(element));
}

window.nd2wsiSlacsLaserBox = box => {
  if (!box || typeof box.sid !== "string" || quitPreparation) return false;
  return postToSlide(box.sid, { nd2wsi: "slacs-laser-box", version: VIEWPORT_PROTOCOL_VERSION, sid: box.sid, box });
};

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

// Every pane still targeted has answered: the preparation succeeds unless
// one of them refused.
function settleQuitPreparation(pending) {
  if (pending !== quitPreparation || pending.seen.size !== pending.targets.size) return;
  finishQuitPreparation(pending.errors.length
    ? { ok: false, error: pending.errors.join("; ") }
    : { ok: true, panes: pending.targets.size });
}

// refresh() removed a pane whose slide the server closed. It holds nothing
// left to save and can no longer answer, so a preparation that targeted it,
// one that started while its tab was closing, stops waiting for it.
function forgetClosedPane(sid) {
  quitPreparedPanes.delete(sid);
  const pending = quitPreparation;
  if (!pending || !pending.targets.delete(sid)) return;
  pending.seen.delete(sid);
  settleQuitPreparation(pending);
}

window.nd2wsiPrepareForUpdate = (requestId) => {
  const id = String(requestId || "");
  if (!id) return Promise.resolve({ ok: false, error: "missing update request" });
  if (quitPreparation) {
    finishQuitPreparation({ ok: false, error: "update preparation restarted" });
  }
  quitPreparationRequestId = id;
  document.documentElement.classList.add("preparing-update");
  scheduleNativeGestureScopes();
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
      // A pane that never confirmed has not answered: native preparation
      // keeps it frozen between retries, and a repeated close may offer to
      // close without confirmed saves.
      finishQuitPreparation({
        ok: false,
        unanswered: true,
        error: `save confirmation timed out for ${missing.length} pane(s)`,
      });
    }, 8000);
    quitPreparation = pending;
    for (const sid of targets) {
      quitPreparedPanes.set(sid, id);
      postToSlide(sid, {
        nd2wsi: "prepare-quit",
        version: VIEWPORT_PROTOCOL_VERSION,
        requestId: id,
      });
    }
  });
};

window.nd2wsiCancelUpdate = (requestId, afterTeardown = false) => {
  const id = String(requestId || "");
  if (!id || id !== quitPreparationRequestId) return false;
  quitPreparationRequestId = "";
  finishQuitPreparation({ ok: false, cancelled: true, error: "update cancelled" });
  // After teardown the panes and tab strip stay locked, also when a later
  // preparation (a tab close) is cancelled; the window still closes, without
  // asking the panes again.
  if (afterTeardown === true) quitTornDown = true;
  if (quitTornDown) {
    showError("Update stopped after saving annotations and releasing images. Reopen the slide or restart the app.");
    return true;
  }
  // A retry can fail before contacting panes (for example while a new slide
  // is opening), so retain each earlier pane's exact request id until abort.
  for (const [sid, paneRequestId] of quitPreparedPanes) {
    postToSlide(sid, {
      nd2wsi: "cancel-quit", version: VIEWPORT_PROTOCOL_VERSION,
      requestId: paneRequestId,
    });
  }
  quitPreparedPanes.clear();
  document.documentElement.classList.remove("preparing-update");
  scheduleNativeGestureScopes();
  return true;
};

function finitePoint(value, positive = false) {
  if (!value || !Number.isFinite(Number(value.x)) || !Number.isFinite(Number(value.y))) {
    return null;
  }
  const point = { x: Number(value.x), y: Number(value.y) };
  if (positive && (point.x <= 0 || point.y <= 0)) return null;
  return point;
}

function localIdentity(st) {
  return st ? {
    paneInstanceId: st.paneInstanceId, contextEpoch: st.contextEpoch,
    spatialContextKey: st.spatialContext?.key ?? null,
  } : null;
}

function sameIdentity(a, b) {
  return Boolean(a && b && a.paneInstanceId === b.paneInstanceId &&
    a.contextEpoch === b.contextEpoch && a.spatialContextKey === b.spatialContextKey);
}

function stopCompare() {
  closePairPicker(false);
  setLinkMenuVisible(false, false);
  if (!compare.enabled) return;
  const sids = groupSids();
  rememberAlignment(); // committed only; never finishLandmarks(true) in teardown
  invalidateSpatialWork();
  clearDisplayTransforms(sids);
  compare.enabled = false; compare.toolsVisible = false; compare.anchorSid = null;
  compare.members = []; compare.pairs = new Map(); compare.anchorLandmarks = [];
  compare.anchorSet = {id: null, revision: 0, points: []};
  compare.linked = true;
  applyFrameLayout(); updateCompareControls(); render();
  focusActivePane();
}

function toggleCompare() {
  if (compare.enabled) stopCompare();
  else if (pairPickerIsOpen()) closePairPicker();
  else startCompare();
}

function receiveNewWindowShortcut(senderSid, data) {
  if (quitPreparation || !readyFrames.has(senderSid) || data.sid !== senderSid ||
      !["user", "agent"].includes(data.role) ||
      !sameIdentity(localIdentity(compare.states.get(senderSid)), data)) return false;
  const controls = window.Nd2WindowControls;
  if (!controls?.canOpenWindow(document) || ShortcutRouter.isMac()) return false;
  controls.requestWindow(document, data.role);
  return true;
}

function panelShortcut(panel) {
  if (quitPreparation || pairPicker.open ||
      !["channels", "region", "annot", "info", "shortcuts"].includes(panel)) return false;
  const st = compare.states.get(active);
  if (!readyFrames.has(active) || !st?.paneInstanceId) return false;
  // Pane key events stay local to their iframe. Only keys owned by the shell
  // (tab strip/toolbar focus) need forwarding to the active tab, once.
  return postToSlide(active, {
    nd2wsi: "pane-panel-shortcut", version: VIEWPORT_PROTOCOL_VERSION,
    panel, ...localIdentity(st),
  });
}

function receiveSceneSelected(senderSid, data) {
  if (!senderSid || quitPreparation || !data || typeof data.sid !== "string" ||
      !/^[A-Za-z0-9_-]{1,64}$/.test(data.sid)) {
    if (senderSid && typeof postToSlide === "function") {
      postToSlide(senderSid, {
        nd2wsi: "scene-select-rejected",
        version: VIEWPORT_PROTOCOL_VERSION,
        sid: senderSid,
      });
    }
    return false;
  }
  const newSid = data.sid;
  const newName = typeof data.name === "string" && data.name ? data.name : null;
  const closeSenderTranslucent = () => {
    const nativeApi = typeof window !== "undefined" ? window.pywebview?.api : null;
    if (nativeApi?.close_translucent_viewer) {
      return Promise.resolve(nativeApi.close_translucent_viewer(senderSid)).catch(() => {});
    }
    return null;
  };
  if (newSid === senderSid) {
    const existing = slides.find((s) => s.sid === senderSid);
    if (existing && newName) existing.name = newName;
    closeSenderTranslucent();
    readyFrames.delete(senderSid);
    frames.get(senderSid)?.contentWindow?.location?.reload?.();
    render();
    return true;
  }
  const oldIndex = slides.findIndex((s) => s.sid === senderSid);
  if (typeof fetch === "function") {
    fetch("api/slides", { cache: "no-store" })
      .then((res) => (res && res.ok ? res.json() : null))
      .then((listing) => {
        const listed = Array.isArray(listing?.slides) ? listing.slides : [];
        const hasOld = listed.some((s) => s.sid === senderSid);
        const hasNew = listed.some((s) => s.sid === newSid);
        const doRefresh = () => refresh(newSid, () => {
          if (oldIndex >= 0) {
            const newIdx = slides.findIndex((s) => s.sid === newSid);
            if (newIdx >= 0 && newIdx !== oldIndex) {
              const [moved] = slides.splice(newIdx, 1);
              if (newName) moved.name = newName;
              slides.splice(Math.min(oldIndex, slides.length), 0, moved);
            }
          }
        });
        const finishRetarget = () => {
          const closing = closeSenderTranslucent();
          return closing ? closing.then(doRefresh) : doRefresh();
        };
        if (hasOld && hasNew) {
          return fetch("api/close", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ sid: senderSid }),
          }).catch(() => {}).then(finishRetarget);
        }
        return finishRetarget();
      })
      .catch(() => refresh(newSid));
  } else {
    closeSenderTranslucent();
    if (oldIndex >= 0) {
      slides[oldIndex] = { ...slides[oldIndex], sid: newSid, name: newName || slides[oldIndex].name };
    }
    activate(newSid);
  }
  return true;
}

function orientationShortcut(action) {
  if (!["rotate-right", "flip-horizontal"].includes(action) || quitPreparation) return false;
  if (compare.enabled) {
    if (compare.landmark.active || compare.pendingRequest || compare.pendingNudge) return false;
    if (compare.pairs.get(compare.orientationSid)?.fit) {
      showError("Fit protected — use Remove Fit before changing orientation");
      return false;
    }
    if (!spatialGroupReady("orientation")) {
      showError("Focus a ready site in every linked pane before changing orientation");
      return false;
    }
    changeOrientation(action);
    return Boolean(compare.pendingRequest);
  }
  const st = compare.states.get(active);
  if (!st?.imageReady || !st.spatialContext || st.plateGrid) {
    showError("Open a ready slide or focused site before changing orientation");
    return false;
  }
  return postToSlide(active, {
    nd2wsi: "pane-orientation-shortcut", version: VIEWPORT_PROTOCOL_VERSION,
    action, ...localIdentity(st),
  });
}

$("compare-toggle").onclick = toggleCompare;
$("compare-tools-toggle").onclick = () => setCompareToolsVisible(!compare.toolsVisible);
$("compare-picker-close").onclick = () => closePairPicker();
$("compare-add").onclick = () => openPicker("add");
$("compare-link").onclick = toggleViewLink;
for (const mode of ["off", "scale", "scale-frame"]) {
  $("compare-link-" + mode).onclick = () => setLinkMode(mode);
}
$("compare-link-menu").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault(); event.stopPropagation(); setLinkMenuVisible(false); return;
  }
  if (event.key === "Tab") { setLinkMenuVisible(false, false); return; }
  const options = ["off", "scale", "scale-frame"].map((mode) => $("compare-link-" + mode))
    .filter((button) => !button.disabled);
  const index = options.indexOf(document.activeElement);
  const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
  if (step && options.length) {
    event.preventDefault(); options[(index + step + options.length) % options.length].focus();
  }
});
$("compare-swap").onclick = swapComparedSlides;
for (const [id, action] of Object.entries(ORIENTATION_ACTIONS)) {
  $(id).onclick = () => changeOrientation(action);
}
$("compare-orientation-target").onchange = (event) => {
  if (compare.pendingRequest || compare.landmark.active) return;
  const sid = event.target.value;
  if (!compare.members.includes(sid)) return;
  compare.orientationSid = sid;
  updateCompareControls();
};
$("compare-align").onclick = () => (compare.landmark.active ? finishLandmarks(true) : startLandmarks());
$("compare-close").onclick = () => setCompareToolsVisible(false);
$("compare-landmark-clear").onclick = clearAlignment;
$("compare-landmark-done").onclick = () => finishLandmarks(true);
$("compare-landmark-cancel").onclick = () => finishLandmarks(false);
$("compare-remove-fit").onclick = () => requestGroup("remove-fit", {targetSid: compare.orientationSid});
$("compare-mapping").onchange = (event) => requestGroup("relative", {
  targetSid: compare.orientationSid, relative: event.target.value === "relative",
});
$("compare-mirror-policy").onchange = (event) => {
  const edit = compare.landmark.edit;
  if (!edit || !["keep", "infer"].includes(event.target.value)) return;
  edit.reflectionPolicy = event.target.value;
  edit.editRevision += 1;
  for (const sid of compare.members) fitPair(sid);
  for (const sid of groupSids()) sendLandmarkMode(sid, true);
  updateCompareControls();
};

window.addEventListener("keydown", (event) => {
  if ($("shading-confirm")?.open) return;
  if (event.key === "Escape" && !$("dropzone")?.hidden) {
    event.preventDefault();
    $("dropzone").hidden = true;
    return;
  }
  if (ShortcutRouter?.isShortcutHelpEvent?.(event)) {
    event.preventDefault();
    event.stopPropagation();
    panelShortcut("shortcuts");
    return;
  }
  if (ShortcutRouter?.isAstromapShortcut(event)) {
    event.preventDefault();
    event.stopPropagation();
    if (!event.repeat) activateAstromap();
    return;
  }
  if (ShortcutRouter?.isPanelShortcut(event)) {
    const panel = ShortcutRouter.panelForEvent(event);
    event.preventDefault();
    event.stopPropagation();
    if (panel) panelShortcut(panel);
    return;
  }
  if (ShortcutRouter?.isOrientationShortcut(event)) {
    const action = ShortcutRouter.orientationForEvent(event);
    event.preventDefault();
    event.stopPropagation();
    if (action) orientationShortcut(action);
    return;
  }
  if (event.repeat) return;
  if (event.key === "Escape" && !$("compare-link-menu").hidden) {
    event.preventDefault();
    setLinkMenuVisible(false);
  } else if (event.key === "Escape" && pairPickerIsOpen()) {
    event.preventDefault();
    closePairPicker();
  } else if (event.key === "Escape" && compare.landmark.active) {
    event.preventDefault();
    finishLandmarks(false);
  } else if (event.key === "Enter" && compare.landmark.active &&
      !event.target.closest?.("input, textarea, select, button")) {
    event.preventDefault();
    commitLandmarkEdit(compare.landmark.edit.editId);
  } else if ((event.metaKey || event.ctrlKey) && event.code === "Backslash") {
    event.preventDefault();
    toggleCompare();
  } else if (compare.enabled && ShortcutRouter?.plainLetterForEvent(event) === "KeyL") {
    event.preventDefault();
    toggleViewLink();
  }
}, true);

wireCompareInteractions();

$("newtab").onclick = () => {
  if (window.pywebview?.api?.pick_paths) {
    window.pywebview.api.pick_paths().then((paths) => {
      if (paths?.length) openMany(paths);
    });
    return;
  }
  const path = prompt("Path to a slide (.nd2, .czi, .lif, .tif/.ome.tif, .svs, .sdpc, .nd2svs, .jpg, .png):");
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
zone.addEventListener("pointerdown", () => {
  if (!zone.hidden) zone.hidden = true;
});
window.addEventListener("dragend", () => { zone.hidden = true; });
window.addEventListener("blur", () => { zone.hidden = true; });
zone.addEventListener("drop", (event) => {
  event.preventDefault();
  zone.hidden = true;
  if (window.pywebview) return;
  const paths = Array.from(event.dataTransfer.files)
    .map((file) => file.pywebviewFullPath || file.path)
    .filter(Boolean);
  if (paths.length) openMany(paths);
  else showError("The browser hid the dropped file paths. Use + or the desktop app.");
});
window.addEventListener("drop", (event) => event.preventDefault());
document.addEventListener("dragover", (event) => event.preventDefault(), true);
document.addEventListener("drop", (event) => event.preventDefault(), true);

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || !event.data) return;
  const senderSid = frameSidForSource(event.source);
  const kind = event.data.nd2wsi;
  const versioned = event.data.version === VIEWPORT_PROTOCOL_VERSION;
  if (kind === "new-window-shortcut") {
    if (!senderSid || !versioned) return;
    receiveNewWindowShortcut(senderSid, event.data);
  } else if (kind === "astromap-shortcut") {
    if (!senderSid || !versioned) return;
    receiveAstromapShortcut(senderSid, event.data);
  } else if (kind === "astromap-state") {
    if (!senderSid || !versioned) return;
    receiveAstromapState(senderSid, event.data);
  } else if (kind === "shading-state") {
    if (!senderSid || !versioned) return;
    receiveShadingState(senderSid, event.data);
  } else if (kind === "quit-ready") {
    const pending = quitPreparation;
    if (!pending || !senderSid || !versioned || !pending.targets.has(senderSid)) return;
    if (event.data.sid !== senderSid || event.data.requestId !== pending.requestId) return;
    if (pending.seen.has(senderSid)) return;
    pending.seen.add(senderSid);
    if (!event.data.ok) {
      pending.errors.push(`${slideName(senderSid)}: ${event.data.error || "save failed"}`);
    }
    settleQuitPreparation(pending);
  } else if (kind === "native-gesture-scope") {
    if (!senderSid || !versioned) return;
    receiveNativeGestureScope(senderSid, event.data);
  } else if (kind === "viewport-ready") {
    if (!senderSid || !versioned || (event.data.sid && event.data.sid !== senderSid)) return;
    if (!receiveViewportState(event.data, senderSid)) return;
    readyFrames.add(senderSid);
    // iframe load can precede the pane's awaited metadata/LUT initialization.
    // A validated ready message proves its command listener now exists; retry
    // the possibly lost load-time handshake without resetting a live toggle.
    if (!shadingStates.has(senderSid)) requestShadingState(senderSid, false);
    paneCameUp(senderSid);
  } else if (kind === "viewport-state") {
    if (!senderSid || (event.data.sid && event.data.sid !== senderSid)) return;
    receiveViewportState(event.data, senderSid);
  } else if (kind === "compare-toggle") {
    if (!senderSid || !versioned) return;
    toggleCompare();
  } else if (kind === "compare-link-toggle") {
    if (!senderSid || !versioned) return;
    if (currentEnvelope(event.data, senderSid)) toggleViewLink();
  } else if (kind === "compare-nudge") {
    if (!senderSid || !versioned || !inGroup(senderSid)) return;
    if (currentEnvelope(event.data, senderSid)) nudgeAlignment(event.data.dxPx, event.data.dyPx, senderSid);
  } else if (kind === "compare-orientation-shortcut") {
    if (!senderSid || !versioned || !inGroup(senderSid) || event.data.sid !== senderSid) return;
    if (currentEnvelope(event.data, senderSid)) orientationShortcut(event.data.action);
  } else if (kind === "landmark-points") {
    if (!senderSid || !versioned || (event.data.sid && event.data.sid !== senderSid)) return;
    receiveLandmarkPoints(senderSid, event.data);
  } else if (kind === "landmark-done") {
    if (!senderSid || !versioned || !inGroup(senderSid)) return;
    if (validLandmarkMessage(senderSid, event.data)) commitLandmarkEdit(event.data.editId);
  } else if (kind === "landmark-cancel") {
    if (!senderSid || !versioned || !inGroup(senderSid)) return;
    if (validLandmarkMessage(senderSid, event.data)) finishLandmarks(false);
  } else if (kind === "tab-select") {
    if (!senderSid || !versioned) return;
    selectTabByIndex(Number(event.data.index));
  } else if (kind === "scene-selected") {
    if (!senderSid || !versioned) return;
    receiveSceneSelected(senderSid, event.data);
  } else if (kind === "window-zoom") {
    // a double-click on a slide's own toolbar, relayed from its frame
    if (!senderSid || !versioned) return;
    requestWindowZoom();
  } else if (kind === "file-drag") {
    if (senderSid) zone.hidden = false;
  } else if (kind === "client-error") {
    if (!senderSid) return;
    relayClientError({ source: "pane", kind: event.data.kind, message: event.data.message,
      where: event.data.where, stack: event.data.stack });
  } else if (kind === "theme") {
    // only the front tab colors the chrome; each tab keeps its own look
    const front = frames.get(active);
    if (front && event.source === front.contentWindow) {
      applyShellTheme(event.data.theme);
    }
  }
});

refresh();
bindSlacsDiscovery();


function showTranslucentWarning() {
  $("translucent-warning").hidden = false;
}
function hideTranslucentWarning() {
  $("translucent-warning").hidden = true;
}
$("translucent-warning-ok").addEventListener("click", hideTranslucentWarning);
window.addEventListener("message", event => {
  const d = event.data;
  if (event.origin !== location.origin || d?.version !== VIEWPORT_PROTOCOL_VERSION || d.nd2wsi !== "translucent-refused") return;
  if (frames.get(d.sid)?.contentWindow !== event.source) return;
  showTranslucentWarning();
});
