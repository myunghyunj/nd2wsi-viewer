/* Independent tiled tracing window. Source display is a read-only snapshot. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./viewer-geometry-v1.js"));
  } else {
    const api = factory(root.Nd2ViewerGeometry);
    root.Nd2Translucent = api;
    api.boot(root);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (geometry) {
  "use strict";

  // The per-key stage step, remembered per viewer. SLACS jogs the stage by its
  // Roughness in micrometres; this should match it (1 Roughness ≈ 1 µm).
  const STEP_STORAGE_KEY = "nd2wsi.stageFollow.stepUm";
  const DEFAULT_STEP_UM = 10;
  const MIN_STEP_UM = 0.001;
  const MAX_STEP_UM = 10000;

  function validateContext(value, pageUrl) {
    if (!value || value.version !== 1 || !Array.isArray(value.center) ||
        value.center.length !== 2 || !value.center.every(Number.isFinite) ||
        !Number.isFinite(value.zoom) || value.zoom <= 0 ||
        !Number.isFinite(value.rotation ?? 0)) throw new Error("Invalid image handoff");
    const slide = new URL(value.slide_url, pageUrl);
    if (slide.origin !== new URL(pageUrl).origin || !/\/s\/[a-zA-Z0-9_-]+\/$/.test(slide.pathname)) {
      throw new Error("The source must belong to this viewer");
    }
    const query = new URLSearchParams(String(value.query || "").replace(/^\?/, ""));
    for (const key of query.keys()) {
      if (!["c", "win", "g", "t", "p", "z"].includes(key) || query.getAll(key).length !== 1) {
        throw new Error("Invalid image display settings");
      }
    }
    return { ...value, slide_url: slide.href, query: query.toString(),
      opacity: Math.max(.1, Math.min(1, Number.isFinite(value.opacity) ? value.opacity : .55)) };
  }

  function makeTileSource(info, context, OSD) {
    if ((info.rgb && !info.raster) || !Number.isFinite(info.width) || info.width <= 0 ||
        !Number.isFinite(info.height) || info.height <= 0 || !info.levels?.length) {
      throw new Error("Open a fluorescence image to use the translucent viewer");
    }
    const requestedGeneration = new URLSearchParams(context.query).get("g");
    if (requestedGeneration && requestedGeneration !== info.generation) {
      throw new Error("The source changed. Reopen the translucent viewer.");
    }
    // This window shows one fixed rendering, so the tile query never changes.
    return geometry.makeTileSource({ info, OSD, baseUrl: context.slide_url,
      query: context.query ? "?" + context.query : "" });
  }

  function camera(viewer) {
    const viewport = viewer.viewport;
    return { center: viewport.getCenter(true),
      zoom: viewport.viewportToImageZoom(viewport.getZoom(true)) };
  }

  function restoreCamera(viewer, saved) {
    const viewport = viewer.viewport;
    viewport.zoomTo(viewport.imageToViewportZoom(saved.zoom), null, true);
    viewport.panTo(saved.center, true);
    viewer.forceRedraw();
  }

  function resizePreservingScale(viewer, width, height, OSD) {
    if (!(width > 0 && height > 0)) return;
    const previous = viewer.viewport.getContainerSize();
    if (previous.x === width && previous.y === height) return;
    // Viewport.resize/fitBounds alone can refit to the new aspect ratio.
    // Explicitly restore CSS pixels per image pixel and the image-space center.
    const saved = camera(viewer);
    viewer.viewport.resize(new OSD.Point(width, height), true);
    restoreCamera(viewer, saved);
  }

  function resizeGeometry(width, height, edge, dx, dy) {
    const clamp = (value, min) => Math.max(min, Math.min(8192, value));
    const nextWidth = clamp(width + (edge.includes("e") ? dx : edge.includes("w") ? -dx : 0), 220);
    const nextHeight = clamp(height + (edge.includes("s") ? dy : edge.includes("n") ? -dy : 0), 160);
    return { width: nextWidth, height: nextHeight,
      left: edge.includes("w") ? width - nextWidth : 0,
      top: edge.includes("n") ? height - nextHeight : 0 };
  }

  function roiBounds(rect, info) {
    if (!rect || ![rect.x, rect.y, rect.w, rect.h].every(Number.isFinite) ||
        rect.x < 0 || rect.y < 0 || rect.w <= 0 || rect.h <= 0 ||
        rect.x + rect.w > info.width + .001 || rect.y + rect.h > info.height + .001) {
      throw new Error("An ROI is outside this image or has invalid coordinates");
    }
    return { x: rect.x, y: rect.y, width: rect.w, height: rect.h };
  }

  function importedRois(result, info, importer) {
    let rois;
    if (typeof result?.text === "string") {
      if (!importer?.parse || !importer?.mapToSource) throw new Error("AstroMapper import is unavailable");
      rois = importer.mapToSource(importer.parse(result.text, result.coordinateSize).rows, result.coordinateSize,
        { width: info.width, height: info.height });
    } else {
      rois = result?.rois;
    }
    if (!Array.isArray(rois) || rois.length === 0) throw new Error("This text file contains no ROIs");
    if (rois.length > (importer?.MAX_ROIS || 2000)) throw new Error("Import at most 2000 regions");
    return rois.map(rect => {
      roiBounds(rect, info);
      return { x: rect.x, y: rect.y, w: rect.w, h: rect.h,
        index: rect.astromapImportIndex ?? rect.index,
        label: typeof rect.text === "string" ? rect.text : typeof rect.label === "string" ? rect.label : "" };
    });
  }

  function focusRoi(viewer, rect, info, locked = false) {
    if (locked) return false;
    const bounds = roiBounds(rect, info);
    // AstroMapper rectangles identify a location, often only a few pixels wide.
    // Re-fitting them would lose the user's tracing scale; only the center moves.
    geometry.centerOnPreservingZoom(viewer.viewport,
      { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 });
    viewer.forceRedraw();
    return true;
  }

  function roiElementPoints(viewer, rect, info, OSD) {
    const bounds = roiBounds(rect, info);
    // The main viewer's projection: OSD includes rotation in this conversion;
    // its horizontal display flip is applied separately.
    return geometry.rectCornersToElementPoints(viewer.viewport, bounds, OSD).map(point => {
      if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error("Invalid ROI display coordinates");
      return { x: point.x, y: point.y };
    });
  }

  function requireSuccess(result, message) {
    if (result === false || result?.ok === false) throw new Error(result?.error || result?.message || message);
    return result;
  }

  function stagePixelSize(info) {
    const size = info.pixelSizeUm; // Metadata is [Y, X], not [X, Y].
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

  function mount(options) {
    const { window: win, document: doc, api, OSD, context, info } = options;
    const importer = options.importer || win.Nd2AstromapImport;
    const stage = doc.getElementById("translucent-stage");
    const overlay = doc.getElementById("overlay");
    const lock = doc.getElementById("overlay-lock");
    const status = doc.getElementById("overlay-status");
    const previousRoi = doc.getElementById("roi-previous");
    const nextRoi = doc.getElementById("roi-next");
    const roiIndex = doc.getElementById("roi-index");
    const roiLayer = doc.getElementById("roi-overlay");
    const opacityControl = doc.getElementById("opacity-control");
    const opacitySlider = doc.getElementById("overlay-opacity");
    const opacityValue = doc.getElementById("opacity-value");
    const followButton = doc.getElementById("stage-follow");
    const followControl = doc.getElementById("stage-follow-control");
    const followDisconnected = doc.getElementById("stage-follow-disconnected");
    const followStatus = doc.getElementById("stage-follow-status");
    const followSettings = doc.getElementById("stage-follow-settings");
    const followSave = doc.getElementById("stage-follow-save");
    const followStep = doc.getElementById("stage-follow-step");
    const followCount = doc.getElementById("stage-follow-count");
    const pixelSize = stagePixelSize(info);
    const validOpacity = value => Number.isFinite(value) && value >= .1 && value <= 1;
    const initialOpacity = validOpacity(context.opacity) ? context.opacity : .55;
    const clampStep = value =>
      Number.isFinite(value) && value >= MIN_STEP_UM && value <= MAX_STEP_UM ? value : null;
    const loadStep = () => {
      try { const stored = clampStep(Number(win.localStorage.getItem(STEP_STORAGE_KEY))); if (stored) return stored; }
      catch (_) { /* private mode or blocked storage: fall back to the default */ }
      return DEFAULT_STEP_UM;
    };
    const state = { locked: context.locked === true, pending: false, gesture: null, ready: false,
      rois: [], roiIndex: -1, roiName: "", loadingRois: false, stepUm: loadStep(),
      opacity: initialOpacity, confirmedOpacity: initialOpacity };
    const follow = state.follow = { enabled: false, pending: false, status: "off", sessionId: "",
      seq: 0, displacement: [0, 0], lastDelta: { x: 0, y: 0 }, message: "", keys: null, slacsSession: "" };
    state.slacs = { available: false, session_id: null, status: "checking" };
    let followEpoch = 0, followTimer = null, followInFlight = false, disposed = false;
    let discoveryTask = null, discoveryInterval = null, discoveryGeneration = 0, disconnected = "";
    let followLifecycle = Promise.resolve();
    const showStatus = message => { status.textContent = message; status.hidden = !message; };
    let opacityTask = null, opacityError = "";
    const paintOpacity = () => {
      const percent = Math.round(state.opacity * 100);
      opacitySlider.value = String(percent);
      opacitySlider.setAttribute("aria-valuetext", percent + "%");
      opacitySlider.disabled = state.locked || state.pending;
      opacityValue.textContent = percent + "%";
      stage.style.opacity = context.opacity_mode === "native" ? "1" : String(state.opacity);
    };
    const setOpacity = value => {
      if (state.locked || state.pending || !validOpacity(value)) {
        paintOpacity();
        return Promise.resolve(false);
      }
      state.opacity = value;
      paintOpacity();
      if (opacityTask) return opacityTask;
      // A drag can emit input faster than the native bridge responds. Keep one
      // request in flight and send only the latest remaining selection next.
      opacityTask = Promise.resolve().then(async () => {
        try {
          while (true) {
            const requested = state.opacity;
            const result = await api.set_translucent_opacity(requested);
            if (result?.ok === false && validOpacity(result.opacity)) state.confirmedOpacity = result.opacity;
            requireSuccess(result, "Could not change the opacity");
            state.confirmedOpacity = validOpacity(result?.opacity) ? result.opacity : requested;
            if (state.opacity === requested) {
              state.opacity = state.confirmedOpacity;
              paintOpacity();
              if (opacityError && status.textContent === opacityError) showStatus("");
              opacityError = "";
              return true;
            }
          }
        } catch (error) {
          state.opacity = state.confirmedOpacity;
          paintOpacity();
          opacityError = error.message || "Could not change the opacity";
          showStatus(opacityError);
          return false;
        } finally { opacityTask = null; }
      });
      return opacityTask;
    };
    opacitySlider.addEventListener("input", () => setOpacity(Number(opacitySlider.value) / 100));
    for (const name of ["pointerdown", "pointermove", "pointerup", "pointercancel"]) {
      opacityControl.addEventListener(name, event => event.stopPropagation());
    }
    opacityControl.addEventListener("wheel", event => {
      event.preventDefault(); event.stopPropagation();
    }, { passive: false });
    paintOpacity();
    const viewer = OSD({
      element: stage, tileSources: makeTileSource(info, context, OSD),
      showNavigationControl: false, showNavigator: false, showSequenceControl: false,
      prefixUrl: "", drawer: "canvas", crossOriginPolicy: false,
      autoResize: false, preserveImageSizeOnResize: true,
      animationTime: .15, springStiffness: 12, zoomPerScroll: 1.2,
      minZoomImageRatio: .05, maxZoomPixelRatio: Math.max(8, context.zoom),
      visibilityRatio: 0, constrainDuringPan: false,
      imageSmoothingEnabled: true, imageLoaderLimit: 6, maxImageCacheCount: 120,
      gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: false, scrollToZoom: true },
      gestureSettingsTouch: { pinchToZoom: true, flickEnabled: false, clickToZoom: false, dblClickToZoom: false },
    });
    let drawnRois = null, roiBoxes = [];
    const renderRoiBoxes = () => {
      if (!state.ready) return;
      if (drawnRois !== state.rois) {
        roiLayer.replaceChildren();
        roiBoxes = state.rois.map((rect, index) => {
          const box = doc.createElementNS("http://www.w3.org/2000/svg", "polygon");
          box.setAttribute("class", "roi-box");
          box.setAttribute("data-roi-index", String(index));
          roiLayer.appendChild(box);
          return box;
        });
        drawnRois = state.rois;
      }
      const size = viewer.viewport.getContainerSize();
      roiLayer.setAttribute("viewBox", "0 0 " + size.x + " " + size.y);
      roiBoxes.forEach((box, index) => {
        box.setAttribute("data-selected", String(index === state.roiIndex));
        try {
          const points = roiElementPoints(viewer, state.rois[index], info, OSD);
          box.setAttribute("points", points.map(point => point.x + "," + point.y).join(" "));
          box.style.display = "";
        } catch (_) { box.style.display = "none"; }
      });
      // Keep the selected outline visible when source regions overlap.
      const selected = roiBoxes[state.roiIndex];
      if (selected && roiLayer.lastElementChild !== selected) roiLayer.appendChild(selected);
    };
    const paintRois = () => {
      const hasRois = state.rois.length > 0;
      doc.getElementById("roi-controls").hidden = !hasRois;
      overlay.dataset.hasRois = String(hasRois);
      const disabled = state.locked || state.pending || state.loadingRois || !state.ready;
      previousRoi.disabled = disabled || state.roiIndex <= 0;
      nextRoi.disabled = disabled || state.rois.length === 0 || state.roiIndex >= state.rois.length - 1;
      roiIndex.disabled = disabled;
      roiIndex.textContent = state.rois.length === 0 ? "—" :
        (state.roiIndex < 0 ? "—" : state.roiIndex + 1) + "/" + state.rois.length;
      const label = state.rois[state.roiIndex]?.label;
      roiIndex.title = (state.roiName ? state.roiName + (label ? " · " + label : "") + " · " : "") + "Open AstroMapper JPG and TXT pair";
      roiIndex.setAttribute("aria-label", state.roiIndex < 0 ? "Open AstroMapper JPG and TXT pair" :
        "ROI " + (state.roiIndex + 1) + " of " + state.rois.length + ". Open AstroMapper JPG and TXT pair");
      renderRoiBoxes();
    };
    const paintLock = () => {
      overlay.dataset.locked = String(state.locked);
      lock.setAttribute("aria-pressed", String(state.locked));
      const lockLabel = follow.enabled ? (state.locked ? "Unlock to align by hand" : "Lock to follow SLACS keys") :
        (state.locked ? "Unlock image and window" : "Lock image and window");
      lock.setAttribute("aria-label", lockLabel);
      lock.title = lockLabel + " (L)" + (follow.enabled ? " · Key presses move the image only while locked" : "");
      lock.disabled = state.pending;
      viewer.setMouseNavEnabled(!state.locked && !state.pending);
      paintOpacity();
      paintRois();
      paintFollow();
    };
    const paintFollow = () => {
      if (disposed) return;
      // Detection adds the main-window offer and stops a Follow bound to a lost
      // SLACS process, but never hides or disables Follow itself.
      followDisconnected.hidden = !disconnected;
      followDisconnected.textContent = disconnected;
      const available = typeof api.start_stage_follow === "function" && typeof api.stage_follow_state === "function" &&
        typeof api.stop_stage_follow === "function";
      followButton.disabled = follow.pending || !state.ready || !pixelSize || !available;
      followButton.setAttribute("aria-pressed", String(follow.enabled));
      followButton.title = !pixelSize ? "Follow needs a calibrated image pixel size" : !available ? "Stage follow is unavailable" :
        follow.enabled ? "Stop following SLACS keys" : "Follow SLACS W/A/S/D keys";
      // Unlocked and SLACS losing the foreground are hints that never stop Follow.
      // A pause stops polling, so it must not hide behind either stale hint.
      const live = follow.enabled && ["waiting", "following"].includes(follow.status);
      const unlocked = live && !state.locked;
      const notInFront = live && !!follow.keys && follow.keys.slacs_in_front === false;
      followStatus.textContent = follow.pending ? "Connecting…" :
        unlocked ? "Unlocked: align by hand, lock to follow" : notInFront ? "SLACS not in front" :
        follow.status === "following" ? "Following SLACS keys" :
        follow.status === "waiting" ? "Waiting for W/A/S/D in SLACS" :
        follow.status === "paused" ? "Paused" : "Off";
      followStatus.dataset.status = follow.pending ? follow.status : unlocked ? "unlocked" :
        notInFront ? "not-in-front" : follow.status;
      followStatus.title = follow.message;
      if (followStep) {
        followStep.value = String(state.stepUm);
        // The step is fixed for a run: changing it mid-follow would desync counts.
        followStep.disabled = follow.enabled || follow.pending;
      }
      if (followCount) {
        const counted = follow.keys && Number.isFinite(follow.keys.counted) ? follow.keys.counted : 0;
        followCount.textContent = "Key presses: " + counted;
      }
      followSave.disabled = !follow.enabled || follow.pending || typeof api.save_stage_follow_connection !== "function";
      doc.getElementById("stage-follow-message").textContent = follow.message ||
        (!pixelSize ? "A calibrated pixel size is needed." : "Commanded motion · moves in on-screen directions");
      doc.getElementById("stage-follow-delta").textContent = "Last image move: " +
        follow.lastDelta.x.toFixed(3) + ", " + follow.lastDelta.y.toFixed(3) + " px";
      doc.getElementById("stage-follow-scale").textContent = pixelSize ?
        "Pixel size: X " + pixelSize.x.toFixed(3) + " · Y " + pixelSize.y.toFixed(3) + " µm/px" : "Pixel size unavailable";
    };
    const clearFollowTimer = () => {
      if (followTimer !== null) win.clearTimeout(followTimer);
      followTimer = null;
    };
    const pauseFollow = message => {
      clearFollowTimer();
      follow.status = "paused";
      follow.message = message + " Turn Follow off and on to reconnect.";
      paintFollow();
    };
    const consumeStageState = result => {
      if (!follow.enabled || follow.status === "paused" || disposed) return false;
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
      // discarded rather than replayed after locking.
      if (state.locked && result.status === "following" && (delta[0] || delta[1])) {
        // Orientation is the main viewer's, shown unchanged; the target rotation
        // is the one OSD's own drag conversion uses.
        const imageDelta = screenImageDelta(delta, pixelSize, viewer.viewport.getRotation(),
          viewer.viewport.getFlip());
        const center = viewer.viewport.viewportToImageCoordinates(viewer.viewport.getCenter(true));
        const target = { x: center.x + imageDelta.x, y: center.y + imageDelta.y };
        if (![target.x, target.y, imageDelta.x, imageDelta.y].every(Number.isFinite)) {
          pauseFollow("Stage movement is outside the image coordinate range."); return false;
        }
        // Add to the current camera, so following continues from wherever the
        // user left the view while unlocked.
        geometry.centerOnPreservingZoom(viewer.viewport, target, OSD);
        viewer.forceRedraw();
        renderRoiBoxes();
        follow.lastDelta = imageDelta;
      }
      follow.displacement = cumulative.slice();
      follow.seq = result.seq;
      follow.status = result.status;
      follow.message = "";
      follow.keys = result.keys && typeof result.keys === "object" ? result.keys : null;
      paintFollow();
      return true;
    };
    const pollStageFollow = async () => {
      if (disposed || !follow.enabled || follow.status === "paused" || followInFlight) return;
      clearFollowTimer();
      const epoch = followEpoch;
      followInFlight = true;
      const watchdog = win.setTimeout(() => {
        if (epoch === followEpoch && follow.enabled && !disposed) pauseFollow("Stage connection is not responding.");
      }, 3000);
      try {
        const result = await api.stage_follow_state();
        if (epoch === followEpoch && !disposed) consumeStageState(result);
      } catch (_) {
        if (epoch === followEpoch && follow.enabled && !disposed) pauseFollow("Stage connection was lost.");
      } finally {
        win.clearTimeout(watchdog);
        followInFlight = false;
        if (!disposed && follow.enabled && follow.status !== "paused") scheduleStagePoll();
      }
    };
    const scheduleStagePoll = () => {
      clearFollowTimer();
      followTimer = win.setTimeout(pollStageFollow, 50);
    };
    const startStageFollow = async () => {
      if (disposed || follow.enabled || follow.pending || !state.ready || !pixelSize ||
          typeof api.start_stage_follow !== "function" || typeof api.stage_follow_state !== "function" ||
          typeof api.stop_stage_follow !== "function") return false;
      const epoch = ++followEpoch;
      disconnected = "";
      // Bound to the detected SLACS process only if one is detected now.
      follow.slacsSession = state.slacs.available ? state.slacs.session_id : "";
      follow.pending = true; follow.message = ""; paintFollow();
      // Serialize native starts/stops, including a close during a pending start.
      const task = followLifecycle.catch(() => {}).then(async () => {
        if (epoch !== followEpoch || disposed) return false;
        // Observe SLACS's W/A/S/D keys; step must match SLACS's Roughness.
        const result = requireSuccess(await api.start_stage_follow({ source: "keys", step_um: state.stepUm }),
          "Could not connect to the stage");
        if (epoch !== followEpoch || disposed) return false;
        if (typeof result?.session_id !== "string" || !result.session_id) throw new Error("Stage session is unavailable");
        follow.enabled = true; follow.sessionId = result.session_id; follow.status = "waiting";
        follow.seq = 0; follow.displacement = [0, 0]; follow.lastDelta = { x: 0, y: 0 }; follow.keys = null;
        scheduleStagePoll();
        return true;
      }).catch(error => {
        if (epoch === followEpoch && !disposed) { follow.status = "off"; follow.message = error.message || "Could not connect to the stage"; }
        return false;
      }).finally(() => {
        if (epoch === followEpoch && !disposed) { follow.pending = false; paintLock(); }
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
      if (!disposed) paintLock();
      if (!wasActive) return Promise.resolve(true);
      const task = followLifecycle.catch(() => {}).then(async () => {
        requireSuccess(await api.stop_stage_follow(), "Could not stop stage follow");
        return true;
      }).catch(error => {
        if (epoch === followEpoch && !disposed) follow.message = error.message || "Could not stop stage follow";
        return false;
      }).finally(() => {
        if (epoch === followEpoch && !disposed) { follow.pending = false; paintLock(); }
      });
      followLifecycle = task;
      return task;
    };
    const pollSlacsDiscovery = () => {
      if (disposed) return Promise.resolve(false);
      if (discoveryTask) return discoveryTask;
      const generation = ++discoveryGeneration;
      const task = (async () => {
        let watchdog;
        let next = { available: false, session_id: null, status: "error" };
        try {
          if (typeof api.slacs_discovery_state !== "function") throw new Error("Discovery unavailable");
          const timeout = new Promise((_, reject) => {
            watchdog = win.setTimeout(() => reject(new Error("Discovery timed out")), 3000);
          });
          const value = await Promise.race([Promise.resolve(api.slacs_discovery_state()), timeout]);
          if (value?.available === true && value.status === "running" && typeof value.session_id === "string" && value.session_id) {
            next = { available: true, session_id: value.session_id, status: String(value.status || "running") };
          } else next.status = String(value?.status || "not_running");
        } catch (_) { /* Fail closed if process metadata is unavailable. */ }
        finally { if (watchdog !== undefined) win.clearTimeout(watchdog); }
        if (disposed || generation !== discoveryGeneration) return false;
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
      if (disposed || (!follow.enabled && !follow.pending)) return false;
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
    followButton.addEventListener("click", () => follow.enabled ? stopStageFollow() : startStageFollow());
    doc.getElementById("stage-follow-options").addEventListener("click", () => {
      followSettings.hidden = !followSettings.hidden;
      doc.getElementById("stage-follow-options").setAttribute("aria-expanded", String(!followSettings.hidden));
    });
    const setStepUm = value => {
      const next = clampStep(value);
      if (next === null || follow.enabled || follow.pending) { paintFollow(); return false; }
      state.stepUm = next;
      try { win.localStorage.setItem(STEP_STORAGE_KEY, String(next)); }
      catch (_) { /* storage may be unavailable; keep the value for this session */ }
      paintFollow();
      return true;
    };
    followStep?.addEventListener("change", () => setStepUm(Number(followStep.value)));
    followSave.addEventListener("click", async () => {
      if (!follow.enabled || follow.pending) return;
      const epoch = followEpoch;
      try {
        const result = requireSuccess(await api.save_stage_follow_connection(), "Could not save the connection");
        if (epoch === followEpoch && !disposed && !result?.cancelled) {
          follow.message = "Connection saved. Open it in the stage sender."; paintFollow();
        }
      } catch (error) {
        if (epoch === followEpoch && !disposed) { follow.message = error.message || "Could not save the connection"; paintFollow(); }
      }
    });
    for (const id of ["stage-follow-control", "stage-follow-settings"]) {
      const control = doc.getElementById(id);
      for (const name of ["pointerdown", "pointermove", "pointerup", "pointercancel", "wheel"]) {
        control.addEventListener(name, event => event.stopPropagation());
      }
    }
    const cancelGesture = () => {
      if (state.gesture) state.gesture.cancelled = true;
      state.gesture = null;
    };
    const toggleLock = async () => {
      if (state.pending) return;
      const previous = state.locked;
      state.pending = true;
      state.locked = !previous;
      cancelGesture();
      if (state.ready) restoreCamera(viewer, camera(viewer)); // stop any in-flight pan/zoom
      paintLock();
      try {
        // Finish the last opacity update before the native lock freezes it.
        if (opacityTask) await opacityTask;
        const result = requireSuccess(await api.set_translucent_locked(state.locked), "Could not change the lock");
        if (typeof result?.locked === "boolean") state.locked = result.locked;
      } catch (error) {
        state.locked = previous;
        showStatus(error.message || "Could not change the lock");
      } finally { state.pending = false; paintLock(); }
    };
    const close = async () => {
      stopStageFollow();
      try { requireSuccess(await api.close_translucent(), "Could not close this window"); }
      catch (error) { showStatus(error.message || "Could not close this window"); }
    };
    lock.addEventListener("click", toggleLock);
    doc.getElementById("overlay-close").addEventListener("click", close);
    paintLock();
    const selectRoi = index => {
      if (!state.ready || state.locked || state.pending || index < 0 || index >= state.rois.length) return false;
      focusRoi(viewer, state.rois[index], info);
      state.roiIndex = index;
      paintRois();
      showStatus("");
      return true;
    };
    const setRois = (result, { pan = true, index = 0, initial = false } = {}) => {
      if ((!initial && (state.locked || state.pending)) || !state.ready) return false;
      if (result?.cancelled) return false;
      try {
        requireSuccess(result, "Could not load the AstroMapper JPG and TXT pair");
        // Parse, map and validate the entire pair before replacing a valid list.
        const rois = importedRois(result, info, importer);
        const selected = Number.isInteger(index) && index >= 0 && index < rois.length ? index : -1;
        if (pan && selected >= 0) focusRoi(viewer, rois[selected], info);
        state.rois = rois;
        state.roiName = typeof result.name === "string" ? result.name : "";
        state.roiIndex = selected;
        paintRois();
        showStatus("");
        return true;
      } catch (error) { showStatus(error.message); return false; }
    };
    const chooseRois = async () => {
      if (state.locked || state.pending || state.loadingRois || !state.ready) return;
      state.loadingRois = true; paintRois();
      try { setRois(await api.choose_roi_file()); }
      catch (error) { showStatus(error.message || "Could not load the AstroMapper JPG and TXT pair"); }
      finally { state.loadingRois = false; paintRois(); }
    };
    previousRoi.addEventListener("click", () => selectRoi(state.roiIndex - 1));
    nextRoi.addEventListener("click", () => selectRoi(state.roiIndex + 1));
    roiIndex.addEventListener("click", chooseRois);
    stage.addEventListener("contextmenu", event => {
      event.preventDefault();
      chooseRois();
    });
    win.nd2TranslucentSetRois = setRois;
    win.nd2TranslucentFocusRoi = rect => {
      if (!state.ready || state.locked || state.pending) return false;
      try { return focusRoi(viewer, rect, info); }
      catch (error) { showStatus(error.message); return false; }
    };

    viewer.addHandler("open", () => {
      const viewport = viewer.viewport;
      // Exactly the main viewer's orientation; this window has no direction
      // settings of its own (an older handoff's mirror_scan is ignored).
      viewport.setRotation(context.rotation || 0, true);
      viewport.setFlip(context.flip === true);
      restoreCamera(viewer, { zoom: context.zoom,
        center: viewport.imageToViewportCoordinates(new OSD.Point(...context.center)) });
      state.ready = true;
      showStatus("");
      paintRois();
      paintFollow();
      resize();
      if (Array.isArray(context.rois) && context.rois.length) {
        setRois({ rois: context.rois, name: context.roiName || "" },
          { pan: false, index: context.roiIndex ?? -1, initial: true });
      }
    });
    viewer.addHandler("open-failed", () => showStatus("Could not load this image. Close and reopen the translucent viewer."));
    viewer.addHandler("tile-load-failed", () => showStatus("Some image tiles could not load. Close and reopen the translucent viewer."));
    viewer.addHandler("update-viewport", renderRoiBoxes);
    const resize = () => {
      if (state.ready) {
        resizePreservingScale(viewer, stage.clientWidth, stage.clientHeight, OSD);
        renderRoiBoxes();
      }
    };
    const observer = win.ResizeObserver ? new win.ResizeObserver(resize) : null;
    observer?.observe(stage);
    win.addEventListener("resize", resize);

    const pumpGeometry = async gesture => {
      if (gesture.running || gesture.nativePending) return;
      gesture.running = true;
      try {
        while (gesture.dirty && !gesture.cancelled && !state.locked && !state.pending) {
          gesture.dirty = false;
          const x = gesture.x, y = gesture.y;
          if (gesture.edge) {
            const target = resizeGeometry(gesture.width, gesture.height, gesture.edge,
              x - gesture.startX, y - gesture.startY);
            const result = requireSuccess(await api.resize_window(target.width, target.height), "Could not resize this window");
            const width = Number(result?.width) || target.width;
            const height = Number(result?.height) || target.height;
            const dx = gesture.edge.includes("w") ? gesture.appliedWidth - width : 0;
            const dy = gesture.edge.includes("n") ? gesture.appliedHeight - height : 0;
            if (dx || dy) requireSuccess(await api.move_window(dx, dy), "Could not move this window");
            gesture.appliedWidth = width; gesture.appliedHeight = height;
          } else {
            const dx = x - gesture.appliedX, dy = y - gesture.appliedY;
            if (dx || dy) requireSuccess(await api.move_window(dx, dy), "Could not move this window");
            gesture.appliedX = x; gesture.appliedY = y;
          }
        }
      } catch (error) { gesture.cancelled = true; showStatus(error.message); }
      finally { gesture.running = false; }
    };
    const startGesture = async (event, edge = "") => {
      if (state.locked || state.pending || event.button !== 0) return;
      event.preventDefault(); event.stopImmediatePropagation();
      cancelGesture();
      const gesture = { edge, startX: event.screenX, startY: event.screenY,
        x: event.screenX, y: event.screenY, appliedX: event.screenX, appliedY: event.screenY,
        width: win.innerWidth, height: win.innerHeight, appliedWidth: win.innerWidth,
        appliedHeight: win.innerHeight, nativePending: !edge, dirty: false };
      state.gesture = gesture;
      overlay.setPointerCapture?.(event.pointerId);
      if (!edge) {
        try {
          const native = await api.start_window_drag();
          if (native === true) { gesture.cancelled = true; if (state.gesture === gesture) state.gesture = null; }
        } catch (_) { /* The incremental bridge is available when native dragging is unavailable. */ }
        gesture.nativePending = false;
        pumpGeometry(gesture);
      }
    };
    doc.getElementById("window-drag").addEventListener("pointerdown", event => startGesture(event));
    overlay.addEventListener("pointerdown", event => {
      if (event.altKey && !event.target.closest("button, input, #opacity-control, #stage-follow-control, #stage-follow-settings, .resize-handle")) startGesture(event);
    }, true);
    for (const handle of doc.querySelectorAll(".resize-handle")) {
      handle.addEventListener("pointerdown", event => startGesture(event, handle.dataset.edge));
    }
    const moveGesture = event => {
      const gesture = state.gesture;
      if (!gesture) return;
      event.preventDefault();
      gesture.x = event.screenX; gesture.y = event.screenY; gesture.dirty = true;
      pumpGeometry(gesture);
    };
    overlay.addEventListener("pointermove", moveGesture);
    overlay.addEventListener("pointerup", event => {
      moveGesture(event);
      state.gesture = null;
      overlay.releasePointerCapture?.(event.pointerId);
    });
    overlay.addEventListener("pointercancel", cancelGesture);
    doc.addEventListener("keydown", event => {
      if (event.repeat || event.altKey) return;
      if (event.target?.closest?.("#stage-follow-control, #stage-follow-settings") && event.key !== "Escape") return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "o") {
        event.preventDefault(); chooseRois(); return;
      }
      if (event.ctrlKey || event.metaKey) return;
      if (event.target?.closest?.("#opacity-control") && /^(Arrow|Home$|End$|Page)/.test(event.key)) return;
      if (event.key === "Escape") { event.preventDefault(); close(); }
      else if (event.key.toLowerCase() === "l") { event.preventDefault(); toggleLock(); }
      else if (event.key === "ArrowLeft") { event.preventDefault(); selectRoi(state.roiIndex - 1); }
      else if (event.key === "ArrowRight") { event.preventDefault(); selectRoi(state.roiIndex + 1); }
    }, true);
    // Keep the embedded page itself at 100%; zoom belongs to the image.
    doc.addEventListener("wheel", event => { if (event.ctrlKey || state.locked) event.preventDefault(); }, { passive: false });
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
    win.addEventListener("error", event => forwardClientError({ source: "translucent", kind: "error",
      message: String(event?.message || event?.error || "error"),
      where: event?.filename ? event.filename + ":" + event.lineno + ":" + event.colno : "",
      stack: event?.error?.stack ? String(event.error.stack) : "" }));
    win.addEventListener("unhandledrejection", event => {
      const reason = event?.reason;
      forwardClientError({ source: "translucent", kind: "unhandledrejection",
        message: String(reason?.message ?? reason ?? "unhandledrejection"),
        stack: reason?.stack ? String(reason.stack) : "" });
    });
    win.addEventListener("pagehide", () => {
      stopStageFollow(); disposed = true;
      discoveryGeneration++;
      if (discoveryInterval !== null) win.clearInterval(discoveryInterval);
      cancelGesture(); observer?.disconnect(); viewer.destroy();
    }, { once: true });
    pollSlacsDiscovery();
    discoveryInterval = win.setInterval(pollSlacsDiscovery, 1000);
    return { viewer, state, toggleLock, close, resize, setRois, selectRoi, setOpacity,
      startStageFollow, stopStageFollow, pollStageFollow, pollSlacsDiscovery, revokeStageFollow,
      setStepUm, forwardClientError };
  }

  async function boot(win) {
    const doc = win.document;
    const status = doc.getElementById("overlay-status");
    const start = async () => {
      if (start.started) return;
      start.started = true;
      try {
        const api = win.pywebview.api;
        const context = validateContext(await api.translucent_context(), win.location.href);
        const response = await win.fetch(context.slide_url + "api/info", { cache: "no-store" });
        if (!response.ok) throw new Error("The source image is no longer available");
        const info = await response.json();
        win.nd2Translucent = mount({ window: win, document: doc, api, OSD: win.OpenSeadragon, context, info });
      } catch (error) {
        status.textContent = error.message || "Could not open the translucent viewer";
        status.hidden = false;
        // Exit stays available even when the source closes before the image opens.
        doc.getElementById("overlay-close").onclick = () => win.pywebview?.api.close_translucent();
      }
    };
    if (win.pywebview?.api?.translucent_context) start();
    else win.addEventListener("pywebviewready", start, { once: true });
  }

  return { validateContext, makeTileSource, camera, restoreCamera, resizePreservingScale,
    resizeGeometry, roiBounds, importedRois, focusRoi, roiElementPoints, stagePixelSize, screenImageDelta, mount, boot };
});
