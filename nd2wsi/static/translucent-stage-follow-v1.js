/* SLACS stage-follow controller, overlay view reporting, laser-shot annotations,
   and client error forwarding for the translucent tracing window. */
(function (root, factory) {
  const api = factory(
    typeof module === "object" && module.exports
      ? require("./viewer-geometry-v1.js")
      : root.Nd2ViewerGeometry
  );
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2TranslucentStageFollow = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (geometry) {
  "use strict";

  // The per-key stage step, kept for this app run. SLACS jogs the stage by its
  // Roughness in micrometres (1 Roughness = 1 µm per press). By default Follow
  // reads Roughness from SLACS and this step is only the fallback while it
  // cannot be read; with reading turned off every press moves by this step.
  const STEP_STORAGE_KEY = "nd2wsi.stageFollow.stepUm";
  const STEP_AUTO_STORAGE_KEY = "nd2wsi.stageFollow.stepAuto";
  const DEFAULT_STEP_UM = 10;
  const MIN_STEP_UM = 0.001;
  const MAX_STEP_UM = 10000;
  // The current view is reported at most this often, for SLACS laser-shot records.
  const VIEW_REPORT_MS = 200;
  // Follow's two flips are remembered per file format by the app
  // (follow_flips.py), never in this page's storage: its origin changes with
  // every launch. "other" names every format not listed here.
  const FOLLOW_FORMAT_LABELS = {
    nd2: "ND2", czi: "CZI", lif: "LIF", ometiff: "OME-TIFF", tiff: "TIFF", qptiff: "QPTIFF",
    svs: "SVS", sdpc: "SDPC", jpeg: "JPEG", png: "PNG"
  };

  function followFormat(value) {
    return typeof value === "string" && Object.prototype.hasOwnProperty.call(FOLLOW_FORMAT_LABELS, value) ?
      value : "other";
  }

  function flipHintText(sourceFormat) {
    const normalized = followFormat(sourceFormat);
    return "Reverses Follow's movement only, never the image. Remembered for " +
      (normalized === "other" ? "other image types" : FOLLOW_FORMAT_LABELS[normalized] + " files") + " on this PC.";
  }

  function clampStep(value) {
    return Number.isFinite(value) && value >= MIN_STEP_UM && value <= MAX_STEP_UM ? value : null;
  }

  function loadStep(win) {
    try {
      const stored = clampStep(Number(win?.localStorage?.getItem(STEP_STORAGE_KEY)));
      if (stored) return stored;
    } catch (_) { /* private mode or blocked storage: fall back to the default */ }
    return DEFAULT_STEP_UM;
  }

  // Reading Roughness from SLACS is on unless this viewer turned it off.
  function loadStepAuto(win) {
    try {
      if (!win?.localStorage) return true;
      return win.localStorage.getItem(STEP_AUTO_STORAGE_KEY) !== "false";
    } catch (_) { return true; }
  }

  function stagePixelSize(info) {
    const size = info?.pixelSizeUm; // Metadata is [Y, X], not [X, Y].
    return Array.isArray(size) && size.length === 2 && size.every(value => Number.isFinite(value) && value > 0)
      ? { x: size[1], y: size[0] } : null;
  }

  // Quarter turns are exact, as in OSD's Point.rotate, so 90° views stay exact.
  function rotationTerms(degrees) {
    const turn = ((degrees % 360) + 360) % 360;
    if (turn === 0) return { cos: 1, sin: 0 };
    if (turn === 90) return { cos: 0, sin: 1 };
    if (turn === 180) return { cos: -1, sin: 0 };
    if (turn === 270) return { cos: 0, sin: -1 };
    const radians = degrees * Math.PI / 180;
    return { cos: Math.cos(radians), sin: Math.sin(radians) };
  }

  // A Follow displacement [dx, dy] in micrometres moves the view centre dx to
  // the right and dy down on screen, as displayed: the overlay moves in the
  // directions the user sees, whatever rotation or flip the main viewer handed
  // over. Returns the canonical image-pixel move of the centre. Screen axes are
  // converted as OpenSeadragon 5.0.1 converts a mouse drag: its point
  // conversions undo the rotation but not the flip, so x is negated on a
  // flipped viewport first. Each screen axis then spans its micrometres at the
  // image's pixel size along that direction, which differs per axis when the
  // pixels are not square.
  function screenImageDelta(displacementUm, pixelSize, rotationDegrees, flipped) {
    const { cos, sin } = rotationTerms(rotationDegrees);
    const sign = flipped ? -1 : 1;
    // Unit image-pixel directions of screen right and screen down.
    const axes = [{ x: sign * cos, y: -sign * sin }, { x: sin, y: cos }];
    let x = 0, y = 0;
    axes.forEach((axis, index) => {
      const um = displacementUm[index];
      if (!um) return;
      const umPerPixel = Math.hypot(axis.x * pixelSize.x, axis.y * pixelSize.y);
      x += um * axis.x / umPerPixel;
      y += um * axis.y / umPerPixel;
    });
    return { x, y };
  }

  // What the key follower's snapshot says the next W/A/S/D press moves by while
  // Follow reads Roughness from SLACS: the last good Roughness (kept while SLACS
  // is busy), or the fallback step while none can be read. Image pixels are at
  // the overlay's own calibration; Mirror shows which keys SLACS reverses.
  function slacsRoughnessText(keys, fallbackUm, pixelSize) {
    if (!keys || typeof keys !== "object") return "SLACS Roughness is read while Follow runs";
    const micrometres = value => String(Math.round(value * 1000) / 1000) + " µm";
    const valid = value => typeof value === "number" && Number.isFinite(value) && value > 0 && value <= MAX_STEP_UM;
    const roughness = valid(keys.roughness_um) ? keys.roughness_um : null;
    const fallback = valid(keys.step_um) ? keys.step_um : fallbackUm;
    if (keys.slacs_busy === true) {
      return roughness !== null ? "SLACS busy — last Roughness " + micrometres(roughness) :
        "SLACS busy — using " + micrometres(fallback);
    }
    if (roughness === null || keys.controls_ok !== true) {
      return "SLACS Roughness unreadable — using " + micrometres(roughness ?? fallback);
    }
    const flag = value => value === true ? "on" : value === false ? "off" : "unknown";
    const pixels = !pixelSize ? "" : pixelSize.x === pixelSize.y ?
      " = " + (roughness / pixelSize.x).toFixed(1) + " px" :
      " = X " + (roughness / pixelSize.x).toFixed(1) + " · Y " + (roughness / pixelSize.y).toFixed(1) + " px";
    return "SLACS Roughness: " + micrometres(roughness) + pixels +
      " · Mirror H " + flag(keys.mirror_h) + ", V " + flag(keys.mirror_v);
  }

  function createLaserAnnotations({ win, doc, viewer, OSD, state, initialItems, isDisposed }) {
    const laserItems = new Map();
    const laserLayer = doc.getElementById("laser-annotations");
    const hydrate = items => {
      laserItems.clear();
      if (!Array.isArray(items)) return;
      for (const item of items) {
        if (item?.slacs?.kind === "laser_shot" && typeof item.id === "string") {
          laserItems.set(item.id, item);
        }
      }
    };
    hydrate(initialItems);
    const render = () => {
      if (!laserLayer) return;
      laserLayer.replaceChildren();
      if (!state.ready) return;
      const size = viewer.viewport.getContainerSize();
      laserLayer.setAttribute("viewBox", "0 0 " + size.x + " " + size.y);
      for (const item of laserItems.values()) {
        const ring = item.type === "box" ? [[item.x, item.y], [item.x + item.w, item.y],
          [item.x + item.w, item.y + item.h], [item.x, item.y + item.h]] : item.geojson?.geometry?.coordinates?.[0];
        if (!Array.isArray(ring) || !ring.every(p => Array.isArray(p) && p.length >= 2 && p.slice(0, 2).every(Number.isFinite))) continue;
        const points = ring.map(([x, y]) => geometry.imageToViewerElementPoint(viewer.viewport, { x, y }, OSD));
        const polygon = doc.createElementNS("http://www.w3.org/2000/svg", "polygon");
        polygon.setAttribute("points", points.map(p => p.x + "," + p.y).join(" "));
        laserLayer.appendChild(polygon);
        const label = doc.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", Math.min(...points.map(p => p.x)));
        label.setAttribute("y", Math.min(...points.map(p => p.y)) - 5);
        label.textContent = String(item.text) + " · " + item.slacs.shots + (item.slacs.shots === 1 ? " shot" : " shots");
        laserLayer.appendChild(label);
      }
    };
    win.nd2TranslucentLaserItem = item => {
      if (isDisposed() || item?.slacs?.kind !== "laser_shot" || typeof item.id !== "string") return false;
      laserItems.set(item.id, item);
      render();
      return true;
    };
    return { hydrate, render };
  }

  function createViewReporter({ win, viewer, state, api, getTarget, isDisposed }) {
    // The app keeps the current view so each SLACS laser shot (",") and next
    // well (".") it logs carries the image-pixel centre of this window, with
    // the Follow progress already shown. Reports are never logged themselves.
    let timer = null, inFlight = false, again = false, reportedFrame = "", reportedDpr = null;
    const finiteValues = values => values.every(Number.isFinite) ? values : null;
    const windowFrame = () => finiteValues([win.screenX, win.screenY, win.innerWidth, win.innerHeight]);
    const overlayView = () => {
      const viewport = viewer.viewport;
      const center = typeof viewport.viewportToImageCoordinates === "function"
        ? viewport.viewportToImageCoordinates(viewport.getCenter(true))
        : viewport.getCenter(true);
      const size = viewport.getContainerSize();
      const follow = state.follow;
      const following = follow.enabled && !!follow.sessionId;
      return {
        version: 1,
        center_px: [center.x, center.y],
        zoom: typeof viewport.viewportToImageZoom === "function"
          ? viewport.viewportToImageZoom(viewport.getZoom(true))
          : viewport.getZoom(true),
        rotation: viewport.getRotation(),
        flip: viewport.getFlip(),
        locked: state.locked,
        follow_session: following ? follow.sessionId : null,
        follow_seq: following ? follow.seq : null,
        follow_displacement_um: following ? follow.displacement.slice() : null,
        container: finiteValues([size.x, size.y]),
        window: windowFrame(),
        ...(getTarget()?.report?.() || {}),
      };
    };
    const reportView = async () => {
      if (isDisposed() || !state.ready || typeof api.report_overlay_view !== "function") return false;
      if (timer !== null && typeof win.clearTimeout === "function") {
        win.clearTimeout(timer);
        timer = null;
      }
      // One request in flight; a change meanwhile is sent once it returns.
      if (inFlight) { again = true; return false; }
      inFlight = true;
      try {
        const view = overlayView();
        const ok = (await api.report_overlay_view(view))?.ok === true;
        if (ok) { reportedFrame = JSON.stringify(view.window); reportedDpr = win.devicePixelRatio; }
        return ok;
      } catch (_) { return false; }
      finally {
        inFlight = false;
        if (again) { again = false; scheduleViewReport(); }
      }
    };
    // A trailing throttle: the report sent when it fires describes the latest view.
    const scheduleViewReport = () => {
      if (isDisposed() || timer !== null || typeof api.report_overlay_view !== "function" ||
          typeof win.setTimeout !== "function") return;
      timer = win.setTimeout(() => { timer = null; reportView(); }, VIEW_REPORT_MS);
    };
    // Moving the window changes no camera, and a native drag ends outside the
    // page, so the 1 s discovery tick also reports a window frame that changed.
    const reportMovedWindow = () => {
      getTarget()?.render?.();
      if (state.ready && (JSON.stringify(windowFrame()) !== reportedFrame || reportedDpr !== win.devicePixelRatio)) {
        scheduleViewReport();
      }
    };
    const dispose = () => {
      if (timer !== null && typeof win.clearTimeout === "function") win.clearTimeout(timer);
      timer = null;
    };
    return { windowFrame, reportView, scheduleViewReport, reportMovedWindow, dispose };
  }

  function attachClientErrorForwarder(win, api) {
    // Forward uncaught overlay errors to the session log. Rate limited, and it
    // must never throw from inside an error handler.
    let clientLogCount = 0, clientLogWindowStart = 0;
    const forwardClientError = payload => {
      try {
        if (typeof api.log_client_event !== "function") return;
        const now = win.performance?.now ? win.performance.now() : Date.now();
        if (now - clientLogWindowStart > 10000) { clientLogWindowStart = now; clientLogCount = 0; }
        if (clientLogCount >= 20) return;
        clientLogCount += 1;
        const result = api.log_client_event(payload);
        if (result && typeof result.catch === "function") result.catch(() => {});
      } catch (_) { /* logging must not surface a new error */ }
    };
    win.addEventListener("error", event => forwardClientError({
      source: "translucent", kind: "error",
      message: String(event?.message || event?.error || "error"),
      where: event?.filename ? event.filename + ":" + event.lineno + ":" + event.colno : "",
      stack: event?.error?.stack ? String(event.error.stack) : "",
    }));
    win.addEventListener("unhandledrejection", event => {
      const reason = event?.reason;
      forwardClientError({
        source: "translucent", kind: "unhandledrejection",
        message: String(reason?.message ?? reason ?? "unhandledrejection"),
        stack: reason?.stack ? String(reason.stack) : "",
      });
    });
    return forwardClientError;
  }

  function mount(deps) {
    const {
      win, doc, api, OSD, viewer, state, pixelSize, sourceFormat, target,
      getLockEpoch, isDisposed, paintLock, renderOverlays, reportView,
      onFollowSeq, onControlsChanged, requireSuccess,
    } = deps;
    const followButton = doc.getElementById("stage-follow");
    const followControl = doc.getElementById("stage-follow-control");
    const followDisconnected = doc.getElementById("stage-follow-disconnected");
    const followStatus = doc.getElementById("stage-follow-status");
    const followOptions = doc.getElementById("stage-follow-options");
    const followSettings = doc.getElementById("stage-follow-settings");
    const followSave = doc.getElementById("stage-follow-save");
    const followStep = doc.getElementById("stage-follow-step");
    const followStepAuto = doc.getElementById("stage-follow-step-auto");
    const followStepLabel = doc.getElementById("stage-follow-step-label");
    const followStepHint = doc.getElementById("stage-follow-step-hint");
    const followRoughness = doc.getElementById("stage-follow-roughness");
    const followCount = doc.getElementById("stage-follow-count");
    const followMessage = doc.getElementById("stage-follow-message");
    const followDelta = doc.getElementById("stage-follow-delta");
    const followScale = doc.getElementById("stage-follow-scale");
    const flipBoxes = {
      y: doc.getElementById("stage-follow-flip-ud"),
      x: doc.getElementById("stage-follow-flip-lr"),
    };
    const followFlipHint = doc.getElementById("stage-follow-flip-hint");
    let flipHint = flipHintText(sourceFormat);

    const follow = state.follow = {
      enabled: false, pending: false, status: "off", sessionId: "",
      seq: 0, displacement: [0, 0], lastDelta: { x: 0, y: 0 },
      message: "", keys: null, slacsSession: "",
    };
    state.slacs = { available: false, session_id: null, status: "checking" };

    let followEpoch = 0, followTimer = null, followInFlight = false;
    let discoveryTask = null, discoveryGeneration = 0, disconnected = "";
    let followLifecycle = Promise.resolve();

    const setFormat = nextFormat => { flipHint = flipHintText(nextFormat); };

    const paintFollow = () => {
      if (isDisposed()) return;
      const prevControlHidden = followControl?.hidden;
      const prevSettingsHidden = followSettings?.hidden;
      const prevDisconnectedHidden = followDisconnected?.hidden;
      const prevRoughnessHidden = followRoughness?.hidden;
      const prevFlipHintHidden = followFlipHint?.hidden;
      // Follow appears only while SLACS is detected. The loss notice is its own
      // element, so it stays visible after the control hides.
      const detected = state.slacs.available === true;
      if (followControl) followControl.hidden = !detected;
      target.render();
      if (!detected) {
        if (followSettings) followSettings.hidden = true;
        followOptions?.setAttribute("aria-expanded", "false");
      }
      if (followDisconnected) {
        followDisconnected.hidden = !disconnected;
        followDisconnected.textContent = disconnected;
      }
      const available = typeof api.start_stage_follow === "function" &&
        typeof api.stage_follow_state === "function" &&
        typeof api.stop_stage_follow === "function";
      if (followButton) {
        followButton.disabled = follow.pending || !state.ready || !pixelSize || !available || !detected;
        followButton.setAttribute("aria-pressed", String(follow.enabled));
        followButton.title = !pixelSize ? "Follow needs a calibrated image pixel size" :
          !available ? "Stage follow is unavailable" :
          !detected ? "SLACS is not detected" :
          follow.enabled ? "Stop following SLACS keys" : "Follow SLACS W/A/S/D keys";
      }
      // Unlocked and SLACS losing the foreground are hints that never stop Follow.
      // A pause stops polling, so it must not hide behind either stale hint.
      const live = follow.enabled && ["waiting", "following"].includes(follow.status);
      const unlocked = live && !state.locked;
      const notInFront = live && !!follow.keys && follow.keys.slacs_in_front === false;
      if (followStatus) {
        followStatus.textContent = follow.pending ? "Connecting…" :
          live && follow.laserDelivery === "delivery_disabled" ? "Laser boxes paused: viewer busy" :
          unlocked ? "Unlocked: align by hand, lock to follow" : notInFront ? "SLACS not in front" :
          follow.status === "following" ? "Following SLACS keys" :
          follow.status === "waiting" ? "Waiting for W/A/S/D in SLACS" :
          follow.status === "paused" ? "Paused" : "Off";
        followStatus.dataset.status = follow.pending ? follow.status : unlocked ? "unlocked" :
          notInFront ? "not-in-front" : follow.status;
        followStatus.title = follow.laserDelivery === "delivery_disabled" ?
          "Shots are being skipped. Laser boxes resume when the viewer responds." : follow.message;
      }
      if (followStep) {
        followStep.value = String(state.stepUm);
        // The step is fixed for a run: changing it mid-follow would desync counts.
        followStep.disabled = follow.enabled || follow.pending;
      }
      if (followStepAuto) {
        followStepAuto.checked = state.stepAuto;
        followStepAuto.disabled = follow.enabled || follow.pending;  // fixed for a run, like the step
      }
      if (followStepLabel) followStepLabel.textContent = state.stepAuto ? "Fallback" : "Step";
      if (followStepHint) {
        followStepHint.textContent = state.stepAuto ?
          "Each press moves by SLACS's Roughness (1 Roughness = 1 µm), and SLACS's Mirror Horizontal and Mirror Vertical reverse W/S and A/D here too. Counts W/A/S/D only while SLACS is in front." :
          "Set this to SLACS's Roughness (1 Roughness = 1 µm per press). Counts W/A/S/D only while SLACS is in front.";
      }
      if (followStepLabel) followStepLabel.title = followStepHint?.textContent || "";
      if (followRoughness) {
        followRoughness.hidden = !state.stepAuto;
        followRoughness.title = state.stepAuto ? slacsRoughnessText(follow.keys, state.stepUm, pixelSize) : "";
        followRoughness.textContent = followRoughness.title.replace("SLACS Roughness: ", "").replace(" = ", " · ")
          .replace(" · Mirror H ", " · Mirror ").replace(", V ", "/");
      }
      if (followCount) {
        const counted = follow.keys && Number.isFinite(follow.keys.counted) ? follow.keys.counted : 0;
        followCount.textContent = "Key presses: " + counted;
      }
      // Unlike the step, a flip applies from the next press: it stays live while
      // following and while locked.
      const flipAvailable = typeof api.set_stage_follow_flips === "function";
      for (const axis of ["x", "y"]) {
        if (!flipBoxes[axis]) continue;
        flipBoxes[axis].checked = state.followFlips[axis];
        flipBoxes[axis].disabled = state.flipPending || !flipAvailable;
      }
      if (followFlipHint) {
        followFlipHint.textContent = state.flipMessage || flipHint;
        followFlipHint.hidden = !state.flipMessage;
      }
      for (const input of Object.values(flipBoxes)) if (input) input.title = state.flipMessage || flipHint;
      if (followSave) {
        followSave.disabled = !follow.enabled || follow.pending || typeof api.save_stage_follow_connection !== "function";
      }
      if (followMessage) {
        followMessage.textContent = follow.message ||
          (!pixelSize ? "A calibrated pixel size is needed." : "Commanded motion · moves in on-screen directions");
      }
      if (followDelta) {
        followDelta.textContent = "Last image move: " +
          follow.lastDelta.x.toFixed(3) + ", " + follow.lastDelta.y.toFixed(3) + " px";
      }
      if (followScale) {
        followScale.textContent = pixelSize ?
          "Pixel size: X " + pixelSize.x.toFixed(3) + " · Y " + pixelSize.y.toFixed(3) + " µm/px" : "Pixel size unavailable";
      }
      if (state.locked && !state.pending && (
          followControl?.hidden !== prevControlHidden ||
          followSettings?.hidden !== prevSettingsHidden ||
          followDisconnected?.hidden !== prevDisconnectedHidden ||
          followRoughness?.hidden !== prevRoughnessHidden ||
          followFlipHint?.hidden !== prevFlipHintHidden)) {
        onControlsChanged?.();
      }
    };

    const clearFollowTimer = () => {
      if (followTimer !== null && typeof win.clearTimeout === "function") win.clearTimeout(followTimer);
      followTimer = null;
    };

    const pauseFollow = message => {
      clearFollowTimer();
      follow.status = "paused";
      follow.message = message + " Turn Follow off and on to reconnect.";
      paintFollow();
    };

    const consumeStageState = (result, mayMove, { report = true } = {}) => {
      if (!follow.enabled || follow.status === "paused" || isDisposed()) return false;
      if (result?.enabled !== true || result.session_id !== follow.sessionId) {
        pauseFollow("Stage connection changed."); return false;
      }
      if (result.status === "paused") { pauseFollow(result.message || "Stage updates stopped."); return false; }
      const cumulative = result.displacement_um;
      if (!["waiting", "following"].includes(result.status) || !Number.isSafeInteger(result.seq) || result.seq < follow.seq ||
          !Array.isArray(cumulative) || cumulative.length !== 2 || !cumulative.every(Number.isFinite)) {
        pauseFollow("Stage update could not be read."); return false;
      }
      const delta = [cumulative[0] - follow.displacement[0], cumulative[1] - follow.displacement[1]];
      if (result.seq === follow.seq && (delta[0] || delta[1])) {
        pauseFollow("Stage update changed unexpectedly."); return false;
      }
      // Only a locked overlay follows. While unlocked the user aligns by hand:
      // the reply is still consumed below, so presses made meanwhile are
      // discarded rather than replayed after locking. mayMove is decided per
      // request (pollStageFollow), because a reply carries presses counted
      // before it was sent.
      if (mayMove && state.locked && !state.pending && result.status === "following" && (delta[0] || delta[1])) {
        // Orientation is the main viewer's, shown unchanged; the target rotation
        // is the one OSD's own drag conversion uses.
        const imageDelta = screenImageDelta(delta, pixelSize, viewer.viewport.getRotation(),
          viewer.viewport.getFlip());
        const center = viewer.viewport.viewportToImageCoordinates(viewer.viewport.getCenter(true));
        const nextCenter = { x: center.x + imageDelta.x, y: center.y + imageDelta.y };
        if (![nextCenter.x, nextCenter.y, imageDelta.x, imageDelta.y].every(Number.isFinite)) {
          pauseFollow("Stage movement is outside the image coordinate range."); return false;
        }
        // Add to the current camera, so following continues from wherever the
        // user left the view while unlocked.
        geometry.centerOnPreservingZoom(viewer.viewport, nextCenter, OSD);
        viewer.forceRedraw();
        renderOverlays();
        follow.lastDelta = imageDelta;
      }
      const seqChanged = result.seq !== follow.seq;
      follow.displacement = cumulative.slice();
      follow.seq = result.seq;
      follow.status = result.status;
      follow.laserDelivery = result.laser_delivery || "ready";
      follow.message = "";
      follow.keys = result.keys && typeof result.keys === "object" ? result.keys : null;
      paintFollow();
      onFollowSeq(seqChanged, report);
      return true;
    };

    const scheduleStagePoll = () => {
      clearFollowTimer();
      if (typeof win.setTimeout === "function") followTimer = win.setTimeout(pollStageFollow, 50);
    };

    const pollStageFollow = async () => {
      if (isDisposed() || !follow.enabled || follow.status === "paused" || followInFlight) return;
      clearFollowTimer();
      const epoch = followEpoch;
      // The lock state when the request leaves decides, not when the reply
      // lands: a reply requested while unlocked or mid-lock, or answered after
      // the lock changed, carries presses made while not locked and never moves.
      const lockAtRequest = state.locked && !state.pending ? getLockEpoch() : -1;
      followInFlight = true;
      const watchdog = typeof win.setTimeout === "function" ? win.setTimeout(() => {
        if (epoch === followEpoch && follow.enabled && !isDisposed()) pauseFollow("Stage connection is not responding.");
      }, 3000) : null;
      try {
        const result = await api.stage_follow_state();
        if (epoch === followEpoch && !isDisposed()) consumeStageState(result, lockAtRequest === getLockEpoch());
      } catch (_) {
        if (epoch === followEpoch && follow.enabled && !isDisposed()) pauseFollow("Stage connection was lost.");
      } finally {
        if (watchdog !== null && typeof win.clearTimeout === "function") win.clearTimeout(watchdog);
        followInFlight = false;
        if (!isDisposed() && follow.enabled && follow.status !== "paused") scheduleStagePoll();
      }
    };

    const startStageFollow = async () => {
      target.cancel();
      // A discovery reply still on its way decides; Follow needs a detected SLACS.
      if (!state.slacs.available && discoveryTask) await discoveryTask;
      if (isDisposed() || follow.enabled || follow.pending || !state.ready || !pixelSize || !state.slacs.available ||
          typeof api.start_stage_follow !== "function" || typeof api.stage_follow_state !== "function" ||
          typeof api.stop_stage_follow !== "function") return false;
      const epoch = ++followEpoch;
      disconnected = "";
      // Every Follow is bound to the SLACS process detected now; its loss or
      // replacement stops it (pollSlacsDiscovery, revokeStageFollow).
      follow.slacsSession = state.slacs.session_id;
      follow.pending = true; follow.message = ""; paintFollow();
      // Serialize native starts/stops, including a close during a pending start.
      const task = followLifecycle.catch(() => {}).then(async () => {
        if (epoch !== followEpoch || isDisposed()) return false;
        // Observe SLACS's W/A/S/D keys, moving by SLACS's Roughness when it is read
        // (the step is then the fallback), or by the step alone.
        const result = requireSuccess(await api.start_stage_follow({
          source: "keys", step_um: state.stepUm,
          step_source: state.stepAuto ? "slacs" : "manual",
        }), "Could not connect to the stage");
        if (epoch !== followEpoch || isDisposed()) return false;
        if (typeof result?.session_id !== "string" || !result.session_id) throw new Error("Stage session is unavailable");
        follow.enabled = true; follow.sessionId = result.session_id; follow.status = "waiting";
        follow.seq = 0; follow.displacement = [0, 0]; follow.lastDelta = { x: 0, y: 0 }; follow.keys = null;
        scheduleStagePoll();
        reportView();
        return true;
      }).catch(error => {
        if (epoch === followEpoch && !isDisposed()) {
          follow.status = "off";
          follow.message = error.message || "Could not connect to the stage";
        }
        return false;
      }).finally(() => {
        if (epoch === followEpoch && !isDisposed()) { follow.pending = false; paintLock(); }
      });
      followLifecycle = task;
      return task;
    };

    const stopStageFollow = () => {
      const wasActive = follow.enabled || follow.pending;
      const epoch = ++followEpoch;
      clearFollowTimer();
      follow.enabled = false; follow.status = "off"; follow.sessionId = ""; follow.message = ""; follow.keys = null;
      follow.slacsSession = "";
      follow.pending = wasActive;
      if (!isDisposed()) paintLock();
      if (!wasActive) return Promise.resolve(true);
      reportView();
      const task = followLifecycle.catch(() => {}).then(async () => {
        requireSuccess(await api.stop_stage_follow(), "Could not stop stage follow");
        return true;
      }).catch(error => {
        if (epoch === followEpoch && !isDisposed()) follow.message = error.message || "Could not stop stage follow";
        return false;
      }).finally(() => {
        if (epoch === followEpoch && !isDisposed()) { follow.pending = false; paintLock(); }
      });
      followLifecycle = task;
      return task;
    };

    const pollSlacsDiscovery = () => {
      if (isDisposed()) return Promise.resolve(false);
      if (discoveryTask) return discoveryTask;
      const generation = ++discoveryGeneration;
      const task = (async () => {
        let watchdog;
        let next = { available: false, session_id: null, status: "error" };
        try {
          if (typeof api.slacs_discovery_state !== "function") throw new Error("Discovery unavailable");
          const pending = Promise.resolve(api.slacs_discovery_state());
          const value = typeof win.setTimeout === "function"
            ? await Promise.race([pending, new Promise((_, reject) => {
                watchdog = win.setTimeout(() => reject(new Error("Discovery timed out")), 3000);
              })])
            : await pending;
          if (value?.available === true && value.status === "running" &&
              typeof value.session_id === "string" && value.session_id) {
            next = { available: true, session_id: value.session_id, status: String(value.status || "running") };
          } else {
            next.status = String(value?.status || "not_running");
          }
        } catch (_) { /* Fail closed if process metadata is unavailable. */ }
        finally {
          if (watchdog !== undefined && typeof win.clearTimeout === "function") win.clearTimeout(watchdog);
        }
        if (isDisposed() || generation !== discoveryGeneration) return false;
        state.slacs = next;
        if (follow.slacsSession && (!next.available || next.session_id !== follow.slacsSession) &&
            (follow.enabled || follow.pending)) {
          stopStageFollow();
          follow.status = "paused";
          disconnected = "SLACS program stopped or changed. Follow stopped; reconnect manually.";
        }
        paintFollow();
        return next.available;
      })();
      discoveryTask = task;
      task.finally(() => { if (discoveryTask === task) discoveryTask = null; });
      return task;
    };

    // The backend pushes this as soon as discovery reports the program lost or
    // replaced. stopStageFollow bumps followEpoch, so a follow reply that arrives
    // after the revocation, even one computed before the loss, never moves the image.
    const revokeStageFollow = revocation => {
      if (isDisposed() || (!follow.enabled && !follow.pending)) return false;
      const sessionId = typeof revocation?.session_id === "string" ? revocation.session_id : "";
      if (sessionId && follow.sessionId && sessionId !== follow.sessionId) return false;
      stopStageFollow();
      follow.status = "paused";
      disconnected = typeof revocation?.message === "string" && revocation.message ? revocation.message :
        "SLACS program stopped or changed. Follow stopped; reconnect manually.";
      // Forget the lost detection; a discovery reply already in flight is from before the loss.
      state.slacs = { available: false, session_id: null, status: "checking" };
      discoveryTask = null;
      paintFollow();
      pollSlacsDiscovery();
      return true;
    };
    win.nd2TranslucentRevokeFollow = revokeStageFollow;

    followButton?.addEventListener("click", () => {
      followButton.blur?.();
      return follow.enabled ? stopStageFollow() : startStageFollow();
    });
    followOptions?.addEventListener("click", () => {
      if (followSettings) followSettings.hidden = !followSettings.hidden;
      followOptions.setAttribute("aria-expanded", String(!followSettings?.hidden));
      followOptions.blur?.();
      if (state.locked && !state.pending) onControlsChanged?.();
    });

    const setStepUm = value => {
      const next = clampStep(value);
      if (next === null || follow.enabled || follow.pending) { paintFollow(); return false; }
      state.stepUm = next;
      try { win.localStorage?.setItem(STEP_STORAGE_KEY, String(next)); }
      catch (_) { /* storage may be unavailable; keep the value for this session */ }
      paintFollow();
      return true;
    };
    followStep?.addEventListener("change", () => setStepUm(Number(followStep.value)));

    const setStepAuto = value => {
      if (typeof value !== "boolean" || follow.enabled || follow.pending) { paintFollow(); return false; }
      state.stepAuto = value;
      try { win.localStorage?.setItem(STEP_AUTO_STORAGE_KEY, String(value)); }
      catch (_) { /* storage may be unavailable; keep the choice for this session */ }
      paintFollow();
      return true;
    };
    followStepAuto?.addEventListener("change", () => {
      followStepAuto.blur?.();
      return setStepAuto(followStepAuto.checked === true);
    });

    // Flip up/down reverses W/S (flip_y) and Flip left/right reverses A/D
    // (flip_x) for Follow's key source, from the next press on. The app applies
    // and remembers them; this page never turns or flips the image for them.
    const setFollowFlips = async ({ x, y } = {}) => {
      if (state.flipPending || isDisposed() || typeof x !== "boolean" || typeof y !== "boolean" ||
          typeof api.set_stage_follow_flips !== "function") { paintFollow(); return false; }
      const previous = state.followFlips;
      if (x === previous.x && y === previous.y) { paintFollow(); return true; }
      state.followFlips = { x, y };
      state.flipPending = true;
      paintFollow();
      try {
        const result = requireSuccess(await api.set_stage_follow_flips({ flip_x: x, flip_y: y }),
          "Could not change Follow's direction");
        state.followFlips = {
          x: typeof result?.flip_x === "boolean" ? result.flip_x : x,
          y: typeof result?.flip_y === "boolean" ? result.flip_y : y,
        };
        state.flipMessage = typeof result?.message === "string" ? result.message : "";
        return true;
      } catch (error) {
        state.followFlips = previous;
        state.flipMessage = error.message || "Could not change Follow's direction";
        return false;
      } finally {
        state.flipPending = false;
        paintFollow();
      }
    };
    flipBoxes.y?.addEventListener("change", () => {
      flipBoxes.y.blur?.();
      return setFollowFlips({ x: state.followFlips.x, y: flipBoxes.y.checked === true });
    });
    flipBoxes.x?.addEventListener("change", () => {
      flipBoxes.x.blur?.();
      return setFollowFlips({ x: flipBoxes.x.checked === true, y: state.followFlips.y });
    });

    followSave?.addEventListener("click", async () => {
      followSave.blur?.();
      if (!follow.enabled || follow.pending) return;
      const epoch = followEpoch;
      try {
        const result = requireSuccess(await api.save_stage_follow_connection(), "Could not save the connection");
        if (epoch === followEpoch && !isDisposed() && !result?.cancelled) {
          follow.message = "Connection saved. Open it in the stage sender.";
          paintFollow();
        }
      } catch (error) {
        if (epoch === followEpoch && !isDisposed()) {
          follow.message = error.message || "Could not save the connection";
          paintFollow();
        }
      }
    });

    for (const control of [followControl, followSettings]) {
      if (!control) continue;
      for (const name of ["pointerdown", "pointermove", "pointerup", "pointercancel", "wheel"]) {
        control.addEventListener(name, event => event.stopPropagation());
      }
    }

    const followDetails = typeof followSettings?.querySelectorAll === "function"
      ? Array.from(followSettings.querySelectorAll("details"))
      : (typeof doc?.querySelectorAll === "function"
        ? Array.from(doc.querySelectorAll("#stage-follow-settings details"))
        : []);
    const onFollowDetailsToggle = () => {
      if (state.locked && !state.pending) onControlsChanged?.();
    };
    for (const details of followDetails) {
      details?.addEventListener?.("toggle", onFollowDetailsToggle);
    }

    const dispose = () => {
      stopStageFollow();
      discoveryGeneration++;
      for (const details of followDetails) {
        details?.removeEventListener?.("toggle", onFollowDetailsToggle);
      }
    };

    return {
      paintFollow, pauseFollow, consumeStageState, pollStageFollow,
      startStageFollow, stopStageFollow, pollSlacsDiscovery, revokeStageFollow,
      setStepUm, setStepAuto, setFollowFlips, setFormat, dispose,
    };
  }

  return {
    STEP_STORAGE_KEY, STEP_AUTO_STORAGE_KEY, DEFAULT_STEP_UM, MIN_STEP_UM, MAX_STEP_UM,
    FOLLOW_FORMAT_LABELS, followFormat, flipHintText, clampStep, loadStep, loadStepAuto,
    stagePixelSize, rotationTerms, screenImageDelta, slacsRoughnessText,
    createLaserAnnotations, createViewReporter, attachClientErrorForwarder, mount,
  };
});
