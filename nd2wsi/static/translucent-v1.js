/* Independent tiled tracing window with live source display sync. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(
      require("./viewer-geometry-v1.js"),
      require("./laser-target-v1.js"),
      require("./translucent-stage-follow-v1.js")
    );
  } else {
    const api = factory(root.Nd2ViewerGeometry, root.Nd2LaserTarget, root.Nd2TranslucentStageFollow);
    root.Nd2Translucent = api;
    api.boot(root);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (geometry, laserTargetArg, stageFollowArg) {
  "use strict";

  const laserTarget = (laserTargetArg && typeof laserTargetArg.validate === "function" &&
    typeof laserTargetArg.mount === "function") ? laserTargetArg : {
    validate: value => value || null,
    mount: () => ({ render() {}, report() { return {}; }, cancel() {}, begin() { return false; },
      contextmenu() { return false; }, calibrating: false, target: null }),
  };

  const stageFollow = (stageFollowArg && typeof stageFollowArg.mount === "function") ? stageFollowArg : {
    followFormat: () => "other", stagePixelSize: () => null, screenImageDelta: () => ({ x: 0, y: 0 }),
    slacsRoughnessText: () => "SLACS Roughness is read while Follow runs", loadStep: () => 10, loadStepAuto: () => true,
    createLaserAnnotations: () => ({ hydrate() {}, render() {} }),
    createViewReporter: () => ({ windowFrame: () => null, reportView: async () => false,
      scheduleViewReport() {}, reportMovedWindow() {}, dispose() {} }),
    attachClientErrorForwarder: () => () => {},
    mount: ({ state }) => {
      state.follow = { enabled: false, pending: false, status: "off", sessionId: "",
        seq: 0, displacement: [0, 0], lastDelta: { x: 0, y: 0 }, message: "", keys: null, slacsSession: "" };
      state.slacs = { available: false, session_id: null, status: "checking" };
      return { paintFollow() {}, pauseFollow() {}, consumeStageState: () => false,
        pollStageFollow: async () => {}, startStageFollow: async () => false,
        stopStageFollow: () => Promise.resolve(true), pollSlacsDiscovery: async () => false,
        revokeStageFollow: () => false, setStepUm: () => false, setStepAuto: () => false,
        setFollowFlips: async () => false, setFormat() {}, dispose() {} };
    },
  };

  // Visible controls that remain clickable while the tracing window is locked.
  const LOCKED_INTERACTIVE_IDS = [
    "overlay-lock", "overlay-close", "stage-follow-control", "stage-follow-settings",
    "opacity-control", "roi-controls", "roi-import-review",
  ];

  const ALLOWED_QUERY_KEYS = ["c", "win", "col", "g", "t", "p", "z"];
  const BASE_QUERY_KEYS = ["g", "t", "p", "z"];
  const MAX_QUERY_LENGTH = 16384;
  const HEX6_RE = /^[0-9a-fA-F]{6}$/;
  const EXPIRED_STATUS = "The source image changed. Close and reopen the translucent viewer.";
  const SYNC_ERROR_STATUS = "Could not sync contrast settings.";
  const TILE_LOAD_FAILED_STATUS = "Some image tiles could not load. Close and reopen the translucent viewer.";

  function validateQueryParams(rawQuery, info = null) {
    if (typeof rawQuery !== "string" || rawQuery.length > MAX_QUERY_LENGTH) {
      throw new Error("Invalid image display settings");
    }
    const query = new URLSearchParams(rawQuery.replace(/^\?/, ""));
    for (const key of query.keys()) {
      if (!ALLOWED_QUERY_KEYS.includes(key) || query.getAll(key).length !== 1) {
        throw new Error("Invalid image display settings");
      }
    }
    const col = query.get("col");
    if (col !== null) {
      const tokens = col.split(",");
      if (!tokens.length || !tokens.every(tok => tok === "" || HEX6_RE.test(tok))) {
        throw new Error("Invalid image display settings");
      }
      if (Array.isArray(info?.channels) && tokens.length > info.channels.length) {
        throw new Error("Invalid image display settings");
      }
    }
    const winParam = query.get("win");
    if (winParam !== null && Array.isArray(info?.channels)) {
      const slots = winParam.split(",");
      if (slots.length > info.channels.length) {
        throw new Error("Invalid image display settings");
      }
    }
    return query;
  }

  function validateMergedQuery(rawQuery, baseQuery, info) {
    const params = validateQueryParams(rawQuery, info);
    const baseParams = new URLSearchParams(String(baseQuery || "").replace(/^\?/, ""));
    for (const key of BASE_QUERY_KEYS) {
      if (params.get(key) !== baseParams.get(key)) {
        throw new Error("Invalid image display settings");
      }
    }
    return geometry.normalizeQuery(rawQuery);
  }

  function validateContext(value, pageUrl) {
    if (!value || value.version !== 1 || !Array.isArray(value.center) ||
        value.center.length !== 2 || !value.center.every(Number.isFinite) ||
        !Number.isFinite(value.zoom) || value.zoom <= 0 ||
        !Number.isFinite(value.rotation ?? 0)) throw new Error("Invalid image handoff");
    const slide = new URL(value.slide_url, pageUrl);
    if (slide.origin !== new URL(pageUrl).origin || slide.search || slide.hash ||
        !/^(\/[A-Za-z0-9._~%-]+)?\/s\/[a-zA-Z0-9_-]+\/$/.test(slide.pathname)) {
      throw new Error("The source must belong to this viewer");
    }
    if (value.query != null && typeof value.query !== "string") {
      throw new Error("Invalid image display settings");
    }
    const rawQuery = String(value.query || "");
    validateQueryParams(rawQuery, null);
    return { ...value, slide_url: slide.href, query: geometry.normalizeQuery(rawQuery),
      opacity: Math.max(.1, Math.min(1, Number.isFinite(value.opacity) ? value.opacity : .55)),
      // An older context without these starts unflipped and names no format.
      source_format: stageFollow.followFormat(value.source_format),
      laser_target: laserTarget.validate(value.laser_target),
      follow_flips: { flip_x: value.follow_flips?.flip_x === true, flip_y: value.follow_flips?.flip_y === true } };
  }

  function makeTileSource(info, context, OSD, getQuery = null) {
    if (!Number.isFinite(info.width) || info.width <= 0 ||
        !Number.isFinite(info.height) || info.height <= 0 || !info.levels?.length) {
      throw new Error("Open an image to use the translucent viewer");
    }
    const initialQuery = validateMergedQuery(String(context.query || ""), String(context.query || ""), info);
    const requestedGeneration = new URLSearchParams(initialQuery).get("g");
    if (requestedGeneration && requestedGeneration !== info.generation) {
      throw new Error("The source changed. Reopen the translucent viewer.");
    }
    const query = typeof getQuery === "function"
      ? () => {
          const q = getQuery();
          return q ? "?" + q : "";
        }
      : (initialQuery ? "?" + initialQuery : "");
    return geometry.makeTileSource({ info, OSD, baseUrl: context.slide_url, query });
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

  function wheelZoomFactor(event, pageHeight) {
    if (!event) return 1;
    const dy = Number(event.deltaY) || 0;
    const dx = Number(event.deltaX) || 0;
    const raw = (event.shiftKey && dy === 0 && dx !== 0) ? dx : dy;
    if (!Number.isFinite(raw) || raw === 0) return 1;
    const mode = event.deltaMode || 0;
    let deltaPx = raw;
    if (mode === 1) deltaPx = raw * (100 / 3);
    else if (mode === 2) deltaPx = raw * Math.max((Number.isFinite(pageHeight) ? pageHeight : 600) || 600, 100);
    deltaPx = Math.max(-300, Math.min(300, deltaPx));
    if (event.ctrlKey && mode === 0 && Math.abs(deltaPx) < 50) {
      return Math.exp(Math.max(-0.25, Math.min(0.25, -deltaPx * 0.005)));
    }
    const k = event.altKey ? 0.00002 : event.shiftKey ? 0.0001 : 0.0005;
    return Math.exp(-deltaPx * k);
  }

  function formatZoomValue(z) {
    if (!Number.isFinite(z) || z <= 0) return "";
    return z < 0.01 ? z.toFixed(4) : z.toFixed(3);
  }

  function formatZoomLabel(z) {
    const value = formatZoomValue(z);
    return value ? value + "×" : "—";
  }

  function parseZoomInput(raw) {
    if (typeof raw === "number") return Number.isFinite(raw) && raw > 0 ? raw : null;
    if (typeof raw !== "string") return null;
    const trimmed = raw.trim();
    const match = /^([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([xX×%])?$/.exec(trimmed);
    if (!match) return null;
    const base = Number(match[1]);
    const value = match[2] === "%" ? base / 100 : base;
    return Number.isFinite(value) && value > 0 ? value : null;
  }

  function mount(options) {
    const { window: win, document: doc, api, OSD, context, info } = options;
    const activeContext = { ...context };
    const importer = options.importer || win.Nd2AstromapImport;
    const stage = doc.getElementById("translucent-stage");
    const backdrop = doc.getElementById("translucent-backdrop");
    const overlay = doc.getElementById("overlay");
    const lock = doc.getElementById("overlay-lock");
    const status = doc.getElementById("overlay-status");
    const previousRoi = doc.getElementById("roi-previous");
    const nextRoi = doc.getElementById("roi-next");
    const roiIndex = doc.getElementById("roi-index");
    const roiLayer = doc.getElementById("roi-overlay");
    const importReview = doc.getElementById("roi-import-review");
    const importSummary = doc.getElementById("roi-import-summary");
    const importIssues = doc.getElementById("roi-import-issues");
    const importRepair = doc.getElementById("roi-import-repair");
    const opacityControl = doc.getElementById("opacity-control");
    const opacitySlider = doc.getElementById("overlay-opacity");
    const opacityValue = doc.getElementById("opacity-value");
    const windowControls = doc.getElementById("window-controls");
    const zoomControl = doc.getElementById("zoom-control");
    const zoomBadge = doc.getElementById("zoom-badge");
    const zoomPopover = doc.getElementById("zoom-popover");
    const zoomForm = doc.getElementById("zoom-form");
    const zoomInput = doc.getElementById("zoom-input");
    const zoomApply = doc.getElementById("zoom-apply");
    const zoomStepButtons = Array.from(
      zoomPopover?.querySelectorAll?.("[data-zoom-step]") ||
      doc.querySelectorAll?.("#zoom-popover [data-zoom-step]") || []
    );
    const followControl = doc.getElementById("stage-follow-control");
    const followSettings = doc.getElementById("stage-follow-settings");
    const laserTargetCapture = doc.getElementById("laser-target-capture");
    if (zoomPopover) zoomPopover.hidden = true;
    const sourceFormat = stageFollow.followFormat(activeContext.source_format);
    const pixelSize = stageFollow.stagePixelSize(info);
    const validOpacity = value => Number.isFinite(value) && value >= .1 && value <= 1;
    const initialOpacity = validOpacity(activeContext.opacity) ? activeContext.opacity : .55;
    const initialQuery = validateMergedQuery(String(activeContext.query || ""), String(activeContext.query || ""), info);
    const baseQuery = initialQuery;
    const state = { locked: activeContext.locked === true, pending: false, gesture: null, ready: false,
      rois: [], roiIndex: -1, roiName: "", loadingRois: false,
      stepUm: stageFollow.loadStep(win), stepAuto: stageFollow.loadStepAuto(win),
      importResult: null, importReview: null, importApplied: false,
      opacity: initialOpacity, confirmedOpacity: initialOpacity, zoomPopoverOpen: false,
      query: initialQuery,
      launchToken: typeof activeContext.launch_token === "string" ? activeContext.launch_token : "",
      displayRevision: Number.isInteger(activeContext.display_revision) && activeContext.display_revision >= 0
        ? activeContext.display_revision : 0,
      syncError: "",
      expired: false,
      // Follow's flips as the app last confirmed them (remembered for this format).
      followFlips: { x: activeContext.follow_flips?.flip_x === true, y: activeContext.follow_flips?.flip_y === true },
      flipPending: false, flipMessage: "", target: laserTarget.validate(activeContext.laser_target),
      follow: { enabled: false, pending: false, status: "off", sessionId: "",
        seq: 0, displacement: [0, 0], lastDelta: { x: 0, y: 0 }, message: "", keys: null, slacsSession: "" },
      slacs: { available: false, session_id: null, status: "checking" } };
    let disposed = false, lockEpoch = 0, discoveryInterval = null;
    const showStatus = message => { status.textContent = message; status.hidden = !message; };
    const clearSyncStatus = () => {
      if (state.syncError && status.textContent === state.syncError) showStatus("");
      state.syncError = "";
    };
    let opacityTask = null, opacityError = "";
    const paintOpacity = () => {
      const percent = Math.round(state.opacity * 100);
      opacitySlider.value = String(percent);
      opacitySlider.setAttribute("aria-valuetext", percent + "%");
      opacitySlider.disabled = state.locked || state.pending;
      opacityValue.textContent = percent + "%";
      stage.style.opacity = activeContext.opacity_mode === "native" ? "1" : String(state.opacity);
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
    let maxZoomRatio = Math.max(16, activeContext.zoom || 1);
    const setTimer = typeof win?.setTimeout === "function"
      ? win.setTimeout.bind(win)
      : (typeof setTimeout === "function" ? setTimeout : null);
    const clearTimer = typeof win?.clearTimeout === "function"
      ? win.clearTimeout.bind(win)
      : (typeof clearTimeout === "function" ? clearTimeout : null);
    let viewer = null;
    let backdropVisible = false;
    let lastViewportPose = null;
    let lastViewSignature = null;
    let displayLoopStarted = false;
    let backoffTimer = null;
    let cancelBackoff = null;
    const readViewportPose = () => {
      const vp = viewer?.viewport;
      if (!vp) return null;
      const c = vp.getCenter?.(true);
      const z = vp.getZoom?.(true);
      const r = typeof vp.getRotation === "function" ? vp.getRotation(true) : 0;
      const f = typeof vp.getFlip === "function" ? Boolean(vp.getFlip()) : false;
      const s = vp.getContainerSize?.();
      return { cx: c?.x, cy: c?.y, z, r, f, sx: s?.x, sy: s?.y };
    };
    const poseChanged = (a, b) => {
      if (!a || !b) return a !== b;
      return a.cx !== b.cx || a.cy !== b.cy || a.z !== b.z ||
        a.r !== b.r || a.f !== b.f || a.sx !== b.sx || a.sy !== b.sy;
    };
    const readViewSignature = (pose = readViewportPose()) => {
      const follow = state.follow;
      const tgt = state.target;
      const disp = Array.isArray(follow?.displacement) ? follow.displacement : null;
      const off = Array.isArray(tgt?.offset_css) ? tgt.offset_css : null;
      return {
        pose,
        followEnabled: Boolean(follow?.enabled),
        followSession: follow?.sessionId || "",
        followSeq: follow?.seq ?? 0,
        followDx: disp ? disp[0] : 0,
        followDy: disp ? disp[1] : 0,
        targetCalibrated: Boolean(tgt?.calibrated),
        targetOx: off ? off[0] : null,
        targetOy: off ? off[1] : null,
        targetSide: tgt?.side_um ?? null,
        targetRot: tgt?.rotation_deg ?? null,
      };
    };
    const viewSignatureChanged = (a, b) => {
      if (!a || !b) return a !== b;
      return poseChanged(a.pose, b.pose) ||
        a.followEnabled !== b.followEnabled ||
        a.followSession !== b.followSession ||
        a.followSeq !== b.followSeq ||
        a.followDx !== b.followDx ||
        a.followDy !== b.followDy ||
        a.targetCalibrated !== b.targetCalibrated ||
        a.targetOx !== b.targetOx ||
        a.targetOy !== b.targetOy ||
        a.targetSide !== b.targetSide ||
        a.targetRot !== b.targetRot;
    };
    const syncViewSignature = () => {
      const pose = readViewportPose();
      lastViewportPose = pose;
      lastViewSignature = readViewSignature(pose);
    };
    const clearBackdrop = () => {
      if (backdrop) {
        backdrop.hidden = true;
        if (backdropVisible) {
          const ctx = typeof backdrop.getContext === "function" ? backdrop.getContext("2d") : null;
          if (ctx && backdrop.width > 0 && backdrop.height > 0) {
            ctx.clearRect(0, 0, backdrop.width, backdrop.height);
          }
        }
      }
      backdropVisible = false;
    };
    const captureBackdrop = (_item, meta) => {
      if (!backdrop) return false;
      if (backdropVisible) {
        if (poseChanged(lastViewportPose, readViewportPose())) {
          clearBackdrop();
          if (meta?.hadPainted === false) return false;
        } else {
          return true;
        }
      } else if (meta?.hadPainted === false) {
        return false;
      }
      const ctx = typeof backdrop.getContext === "function" ? backdrop.getContext("2d") : null;
      const src = viewer?.drawer?.canvas;
      if (!ctx || !src || !(src.width > 0 && src.height > 0)) return false;
      try {
        if (backdrop.width !== src.width) backdrop.width = src.width;
        if (backdrop.height !== src.height) backdrop.height = src.height;
        ctx.clearRect(0, 0, backdrop.width, backdrop.height);
        ctx.drawImage(src, 0, 0);
        backdrop.hidden = false;
        backdropVisible = true;
        lastViewportPose = readViewportPose();
        return true;
      } catch (_) {
        backdropVisible = true;
        clearBackdrop();
        return false;
      }
    };
    const displayGate = geometry.createTileRefreshGate({
      getItem: () => viewer?.world?.getItemAt?.(0),
      getViewer: () => viewer,
      onBeforeReset: captureBackdrop,
      onPaintComplete: (painted, meta) => {
        if (painted === null || meta?.timedOut) return;
        clearBackdrop();
        clearSyncStatus();
        if (status.textContent === TILE_LOAD_FAILED_STATUS) showStatus("");
      },
      onActiveTileError: () => {
        clearBackdrop();
        showStatus(TILE_LOAD_FAILED_STATUS);
      },
      setTimer,
      clearTimer,
      stallTimeoutMs: 2000,
    });
    displayGate.init(initialQuery);
    viewer = OSD({
      element: stage,
      tileSources: makeTileSource(info, activeContext, OSD, () => displayGate.getActiveQuery()),
      showNavigationControl: false, showNavigator: false, showSequenceControl: false,
      prefixUrl: "", drawer: "canvas", crossOriginPolicy: false,
      autoResize: false, preserveImageSizeOnResize: true,
      animationTime: .15, springStiffness: 12, zoomPerScroll: 1.2,
      minZoomImageRatio: .05, maxZoomPixelRatio: maxZoomRatio,
      visibilityRatio: 0, constrainDuringPan: false,
      imageSmoothingEnabled: true, imageLoaderLimit: 6, maxTilesPerFrame: 6, maxImageCacheCount: 120,
      gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: false, scrollToZoom: false },
      gestureSettingsTouch: { pinchToZoom: true, flickEnabled: false, clickToZoom: false, dblClickToZoom: false },
    });
    const sleepBackoff = ms => {
      if (typeof setTimer !== "function") return null;
      return new Promise(resolve => {
        let settled = false;
        const finish = () => {
          if (settled) return;
          settled = true;
          if (backoffTimer !== null && typeof clearTimer === "function") clearTimer(backoffTimer);
          backoffTimer = null;
          cancelBackoff = null;
          resolve();
        };
        cancelBackoff = finish;
        backoffTimer = setTimer(finish, ms);
      });
    };
    const applyDisplaySnapshot = snap => {
      if (disposed || state.expired || !snap || snap.ok === false) return false;
      if (state.launchToken && snap.launch_token && snap.launch_token !== state.launchToken) return false;
      if (snap.expired === true) {
        state.expired = true;
        clearBackdrop();
        state.syncError = "";
        showStatus(EXPIRED_STATUS);
        return false;
      }
      if (!Number.isInteger(snap.revision) || snap.revision <= state.displayRevision) return false;
      let nextQuery;
      try {
        nextQuery = validateMergedQuery(snap.query, baseQuery, info);
      } catch (_) {
        return false;
      }
      state.displayRevision = snap.revision;
      state.query = nextQuery;
      clearSyncStatus();
      if (state.ready) displayGate.request(nextQuery);
      else displayGate.resetState(nextQuery);
      return true;
    };
    const startDisplayLoop = () => {
      if (disposed || displayLoopStarted) return;
      displayLoopStarted = true;
      const hasStatePull = typeof api?.translucent_display_state === "function";
      const hasWait = Boolean(state.launchToken) && typeof api?.wait_translucent_display === "function";
      if (!hasStatePull && !hasWait) return;
      Promise.resolve().then(async () => {
        if (disposed) return;
        if (hasStatePull) {
          try {
            const initialSnap = await api.translucent_display_state();
            if (disposed || initialSnap?.closed) return;
            if (initialSnap?.ok) {
              if (initialSnap.expired !== true) clearSyncStatus();
              applyDisplaySnapshot(initialSnap);
            }
          } catch (_) {
            // Transient initial pull errors fall through to the long-poll loop.
          }
        }
        if (!hasWait) return;
        let consecutiveErrors = 0;
        while (!disposed && !state.expired) {
          let res;
          try {
            res = await api.wait_translucent_display(state.launchToken, state.displayRevision, 25000);
          } catch (_) {
            res = null;
          }
          if (disposed || res?.closed) break;
          if (res?.ok) {
            consecutiveErrors = 0;
            if (res.expired === true) {
              applyDisplaySnapshot(res);
              continue;
            }
            clearSyncStatus();
            if (!res.unchanged) applyDisplaySnapshot(res);
            continue;
          }
          consecutiveErrors += 1;
          if (consecutiveErrors === 1) continue;
          state.syncError = SYNC_ERROR_STATUS;
          showStatus(state.syncError);
          const wait = sleepBackoff(1000);
          if (!wait) break;
          await wait;
        }
      });
    };
    // On a focused canvas OSD's own keys rotate (R), flip (F), pan (W/A/S/D,
    // arrows) or zoom (+/-, 0). This window shows exactly the main viewer's
    // orientation and is aligned by hand, so none of them may act here.
    viewer.addHandler("canvas-key", event => { event.preventDefaultAction = true; });
    const laserAnnotations = stageFollow.createLaserAnnotations({
      win, doc, viewer, OSD, state, initialItems: activeContext.laser_items, isDisposed: () => disposed,
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
    const renderOverlays = () => { renderRoiBoxes(); laserAnnotations.render(); };
    let target = null;
    const viewReporter = stageFollow.createViewReporter({
      win, viewer, state, api, getTarget: () => target, isDisposed: () => disposed,
    });
    const { windowFrame, reportView: rawReportView, scheduleViewReport: rawScheduleViewReport, reportMovedWindow } = viewReporter;
    const reportView = (...args) => { syncViewSignature(); return rawReportView(...args); };
    const scheduleViewReport = (...args) => { syncViewSignature(); return rawScheduleViewReport(...args); };
    target = laserTarget.mount({
      win, doc, viewer, OSD, pixelSize, state, api,
      frame: windowFrame, changed: scheduleViewReport, message: showStatus,
    });

    const currentImageZoom = () => {
      const vp = viewer.viewport;
      const raw = vp.getZoom(true);
      return typeof vp.viewportToImageZoom === "function" ? vp.viewportToImageZoom(raw) : raw;
    };
    const zoomBounds = () => {
      const vp = viewer.viewport;
      let minZ = 1e-4, maxZ = maxZoomRatio;
      const hasConvert = typeof vp.viewportToImageZoom === "function";
      if (typeof vp.getMinZoom === "function") {
        const rawMin = vp.getMinZoom();
        const candidate = hasConvert ? vp.viewportToImageZoom(rawMin) : rawMin;
        if (Number.isFinite(candidate) && candidate > 0) minZ = candidate;
      } else {
        const size = vp.getContainerSize?.();
        if (size?.x > 0 && size?.y > 0 && info.width > 0 && info.height > 0) {
          minZ = Math.max(1e-6, 0.05 * Math.min(size.x / info.width, size.y / info.height));
        }
      }
      if (typeof vp.getMaxZoom === "function") {
        const rawMax = vp.getMaxZoom();
        const candidate = hasConvert ? vp.viewportToImageZoom(rawMax) : rawMax;
        if (Number.isFinite(candidate) && candidate > minZ) maxZ = candidate;
      }
      if (Number.isFinite(activeContext.zoom) && activeContext.zoom > 0) {
        minZ = Math.min(minZ, activeContext.zoom);
        maxZ = Math.max(maxZ, activeContext.zoom);
      }
      return { min: Math.max(1e-6, minZ), max: Math.max(maxZ, minZ) };
    };
    const paintZoom = () => {
      const z = state.ready ? currentImageZoom() : activeContext.zoom;
      const disabled = state.locked || state.pending || !state.ready;
      if (zoomBadge) {
        zoomBadge.textContent = formatZoomLabel(z);
        zoomBadge.disabled = disabled;
        zoomBadge.setAttribute("aria-expanded", String(state.zoomPopoverOpen));
      }
      if (zoomInput) zoomInput.disabled = disabled;
      if (zoomApply) zoomApply.disabled = disabled;
      for (const button of zoomStepButtons) button.disabled = disabled;
    };
    const closeZoomPopover = ({ restoreFocus = false } = {}) => {
      const wasOpen = state.zoomPopoverOpen;
      state.zoomPopoverOpen = false;
      if (zoomPopover) zoomPopover.hidden = true;
      if (zoomControl?.dataset) zoomControl.dataset.open = "false";
      if (zoomBadge) zoomBadge.setAttribute("aria-expanded", "false");
      zoomInput?.removeAttribute?.("aria-invalid");
      if (wasOpen && restoreFocus && zoomBadge && !zoomBadge.disabled) zoomBadge.focus?.();
      return wasOpen;
    };
    const openZoomPopover = () => {
      if (!zoomPopover || !zoomBadge || state.locked || state.pending || !state.ready) return false;
      if (target.calibrating) target.cancel();
      state.zoomPopoverOpen = true;
      zoomPopover.hidden = false;
      if (zoomControl?.dataset) zoomControl.dataset.open = "true";
      zoomBadge.setAttribute("aria-expanded", "true");
      if (zoomInput) {
        zoomInput.value = formatZoomValue(currentImageZoom());
        zoomInput.removeAttribute?.("aria-invalid");
        zoomInput.focus?.();
        zoomInput.select?.();
      }
      return true;
    };
    const applyImageZoom = (targetImageZoom, refPoint = null) => {
      if (!state.ready || state.locked || state.pending) return false;
      if (!Number.isFinite(targetImageZoom) || targetImageZoom <= 0) return false;
      const vp = viewer.viewport;
      const { min, max } = zoomBounds();
      const clamped = Math.max(min, Math.min(max, targetImageZoom));
      const vpZoom = typeof vp.imageToViewportZoom === "function" ? vp.imageToViewportZoom(clamped) : clamped;
      vp.zoomTo(vpZoom, refPoint || null, true);
      viewer.forceRedraw?.();
      renderOverlays();
      target.render();
      paintZoom();
      reportView();
      return true;
    };
    const stepZoom = delta => {
      if (!state.ready || state.locked || state.pending || !Number.isFinite(delta) || delta === 0) return false;
      const current = currentImageZoom();
      if (!Number.isFinite(current) || current <= 0) return false;
      const { min, max } = zoomBounds();
      const next = Math.max(min, Math.min(max, Math.round((current + delta) * 1e6) / 1e6));
      const applied = applyImageZoom(next, viewer.viewport.getCenter(true));
      if (applied && zoomInput) {
        zoomInput.value = formatZoomValue(currentImageZoom());
        zoomInput.removeAttribute?.("aria-invalid");
      }
      return applied;
    };
    const submitZoomForm = event => {
      event?.preventDefault?.();
      if (state.locked || state.pending || !state.ready) { closeZoomPopover(); return false; }
      const parsed = parseZoomInput(zoomInput?.value ?? "");
      if (parsed === null) {
        zoomInput?.setAttribute?.("aria-invalid", "true");
        zoomInput?.focus?.();
        zoomInput?.select?.();
        return false;
      }
      zoomInput?.removeAttribute?.("aria-invalid");
      const applied = applyImageZoom(parsed, viewer.viewport.getCenter(true));
      if (applied) closeZoomPopover();
      return applied;
    };
    if (zoomControl) {
      for (const name of ["pointerdown", "pointermove", "pointerup", "pointercancel"]) {
        zoomControl.addEventListener(name, event => event.stopPropagation());
      }
      zoomControl.addEventListener("wheel", event => { event.preventDefault(); event.stopPropagation(); }, { passive: false });
    }
    zoomBadge?.addEventListener("click", () => { if (state.zoomPopoverOpen) closeZoomPopover(); else openZoomPopover(); });
    zoomForm?.addEventListener("submit", submitZoomForm);
    if (zoomInput) {
      zoomInput.addEventListener("keydown", event => {
        if (event.altKey || event.ctrlKey || event.metaKey) return;
        if (event.key === "Escape" && state.zoomPopoverOpen) {
          event.preventDefault?.(); event.stopPropagation?.();
          closeZoomPopover({ restoreFocus: true });
          return;
        }
        if (event.key === "ArrowUp" || event.key === "ArrowDown") {
          event.preventDefault(); event.stopPropagation?.();
          stepZoom((event.shiftKey ? 0.01 : 0.001) * (event.key === "ArrowUp" ? 1 : -1));
        }
      });
      zoomInput.addEventListener("input", () => { zoomInput.removeAttribute?.("aria-invalid"); });
    }
    for (const button of zoomStepButtons) {
      button.addEventListener("click", () => {
        stepZoom(Number(button.dataset?.zoomStep ?? button.getAttribute?.("data-zoom-step")));
      });
    }
    const isExcludedWheelTarget = el => {
      if (!el) return false;
      for (const node of [zoomPopover, zoomControl, windowControls, opacityControl, importReview,
        followControl, followSettings, laserTargetCapture]) {
        if (node && (el === node || node.contains?.(el))) return true;
      }
      const hit = el.closest?.(
        "#zoom-popover, #zoom-control, #window-controls, #opacity-control, #roi-import-review, #stage-follow-control, #stage-follow-settings, #laser-target-capture"
      );
      return Boolean(hit && typeof hit === "object" && (!hit.id || [
        "zoom-popover", "zoom-control", "window-controls", "opacity-control",
        "roi-import-review", "stage-follow-control", "stage-follow-settings", "laser-target-capture",
      ].includes(hit.id)));
    };
    stage.addEventListener("wheel", event => {
      if (isExcludedWheelTarget(event.target)) return;
      if (state.locked || state.pending || !state.ready) return;
      event.preventDefault?.();
      const factor = wheelZoomFactor(event, stage.clientHeight || win.innerHeight || 600);
      if (!Number.isFinite(factor) || factor === 1) return;
      const current = currentImageZoom();
      if (!Number.isFinite(current) || current <= 0) return;
      const vp = viewer.viewport;
      let refPoint = null;
      if (typeof vp.pointFromPixel === "function" && Number.isFinite(event.clientX) && Number.isFinite(event.clientY)) {
        const rect = typeof stage.getBoundingClientRect === "function" ? stage.getBoundingClientRect() : { left: 0, top: 0 };
        const elemPt = geometry.flipAwareElementPoint(vp, { x: event.clientX - rect.left, y: event.clientY - rect.top }, OSD);
        refPoint = vp.pointFromPixel(elemPt, true);
      }
      applyImageZoom(current * factor, refPoint);
    }, { passive: false });

    const collectLockedInteractiveRects = () => {
      const rects = [];
      for (const id of LOCKED_INTERACTIVE_IDS) {
        const el = doc.getElementById(id);
        if (!el || el.hidden || el.style?.display === "none" || typeof el.getBoundingClientRect !== "function") continue;
        const rect = el.getBoundingClientRect();
        const left = Number(rect?.left ?? rect?.x), top = Number(rect?.top ?? rect?.y);
        const width = Number(rect?.width ?? (Number(rect?.right) - left));
        const height = Number(rect?.height ?? (Number(rect?.bottom) - top));
        if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) continue;
        rects.push({ x: left, y: top, width, height });
      }
      return rects;
    };
    const reportLockedInteractiveRects = () => {
      if (disposed || typeof api.set_locked_interactive_rects !== "function") return false;
      const rects = state.locked ? collectLockedInteractiveRects() : [];
      try {
        const result = api.set_locked_interactive_rects(rects);
        if (result && typeof result.catch === "function") result.catch(() => {});
        return true;
      } catch (_) { return false; }
    };

    const paintRois = () => {
      const hasRois = state.rois.length > 0;
      doc.getElementById("roi-controls").hidden = !hasRois;
      overlay.dataset.hasRois = String(hasRois);
      const disabled = state.locked || state.pending || state.loadingRois || !state.ready;
      previousRoi.disabled = disabled || state.roiIndex <= 0;
      nextRoi.disabled = disabled || state.rois.length === 0 || state.roiIndex >= state.rois.length - 1;
      roiIndex.disabled = disabled;
      importRepair.disabled = disabled || !state.importResult;
      roiIndex.textContent = state.rois.length === 0 ? "—" :
        (state.roiIndex < 0 ? "—" : state.roiIndex + 1) + "/" + state.rois.length;
      const label = state.rois[state.roiIndex]?.label;
      roiIndex.title = (state.roiName ? state.roiName + (label ? " · " + label : "") + " · " : "") + "Open AstroMapper JPG and TXT pair";
      roiIndex.setAttribute("aria-label", state.roiIndex < 0 ? "Open AstroMapper JPG and TXT pair" :
        "ROI " + (state.roiIndex + 1) + " of " + state.rois.length + ". Open AstroMapper JPG and TXT pair");
      renderRoiBoxes();
    };
    const paintImportReview = () => {
      const review = state.importReview;
      importReview.hidden = !review?.issues.length;
      importRepair.hidden = !review?.issues.some(issue => issue.repairable && !issue.repaired);
      importSummary.textContent = review ?
        `${state.importApplied ? "Imported" : "Ready"} ${review.count} ROIs · ${review.skippedCount} skipped · ${review.repairedCount} repaired` : "";
      importIssues.textContent = review ? [
        state.importResult?.name || "ROI TXT",
        "Original TXT unchanged. Skipped rows are not shown on the image.",
        ...review.issues.map(issue => `${issue.repaired ? "Repaired" : "Skipped"}: ${issue.message}\n${issue.raw}`),
      ].join("\n\n") : "";
    };
    let stageFollowCtrl = null;
    const paintLock = () => {
      if (state.locked) closeZoomPopover();
      overlay.dataset.locked = String(state.locked);
      lock.setAttribute("aria-pressed", String(state.locked));
      const follow = state.follow;
      const lockLabel = follow.enabled ? (state.locked ? "Unlock to align by hand" : "Lock to follow SLACS keys") :
        (state.locked ? "Unlock image and window" : "Lock image and window");
      lock.setAttribute("aria-label", lockLabel);
      lock.title = lockLabel + " (L)" + (follow.enabled ? " · Key presses move the image only while locked" : "");
      lock.disabled = state.pending;
      viewer.setMouseNavEnabled(!state.locked && !state.pending);
      paintOpacity();
      paintRois();
      paintZoom();
      stageFollowCtrl?.paintFollow();
    };
    // Also report while unlocked so the app knows which presses the view already shows.
    // When locked, report immediately so laser shots right after a jog do not wait on the 200 ms throttle.
    const onFollowSeq = (seqChanged, report) => {
      if (seqChanged && report) {
        if (state.locked) reportView();
        else scheduleViewReport();
      }
    };
    stageFollowCtrl = stageFollow.mount({
      win, doc, api, OSD, viewer, state, pixelSize, sourceFormat, target,
      getLockEpoch: () => lockEpoch, isDisposed: () => disposed,
      paintLock, renderOverlays, reportView, onFollowSeq,
      onControlsChanged: () => { if (state.locked && !state.pending) reportLockedInteractiveRects(); },
      requireSuccess,
    });
    const { consumeStageState, pollStageFollow, startStageFollow, stopStageFollow,
      pollSlacsDiscovery, revokeStageFollow, setStepUm, setStepAuto, setFollowFlips } = stageFollowCtrl;

    const cancelGesture = () => {
      if (state.gesture) state.gesture.cancelled = true;
      state.gesture = null;
    };
    const toggleLock = async () => {
      if (state.pending) return;
      const previous = state.locked;
      state.pending = true;
      state.locked = !previous;
      lockEpoch++;
      cancelGesture();
      closeZoomPopover();
      target.cancel();
      if (state.ready) restoreCamera(viewer, camera(viewer)); // stop any in-flight pan/zoom
      paintLock();
      try {
        // Finish the last opacity update before the native lock freezes it.
        if (opacityTask) await opacityTask;
        const result = requireSuccess(await api.set_translucent_locked(state.locked), "Could not change the lock");
        if (typeof result?.locked === "boolean") state.locked = result.locked;
        if (state.locked && state.follow.enabled && result?.stage_follow?.enabled) {
          consumeStageState(result.stage_follow, false, { report: false });
        }
      } catch (error) {
        state.locked = previous;
        showStatus(error.message || "Could not change the lock");
      } finally { state.pending = false; lockEpoch++; paintLock(); reportLockedInteractiveRects(); reportView(); }
    };
    const close = async () => {
      closeZoomPopover();
      target.cancel();
      stopStageFollow();
      try { requireSuccess(await api.close_translucent(), "Could not close this window"); }
      catch (error) { showStatus(error.message || "Could not close this window"); }
    };
    lock.addEventListener("click", () => { lock.blur?.(); return toggleLock(); });
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
    const setRois = (result, { pan = true, index = 0, initial = false, repairZeroArea = false } = {}) => {
      if ((!initial && (state.locked || state.pending)) || !state.ready) return false;
      if (result?.cancelled) return false;
      try {
        requireSuccess(result, "Could not load the AstroMapper JPG and TXT pair");
        // Keep the source TXT and every rejected row available for review.
        // Mapping still validates the whole-image relationship before replacing
        // a working list; coordinate corrections require the explicit button.
        let rois;
        if (typeof result?.text === "string") {
          // A new selection must never leave a repair action for the old TXT,
          // even when the new file fails before any row can be reviewed.
          state.importResult = null;
          state.importReview = null;
          state.importApplied = false;
          paintImportReview();
          paintRois();
          if (!importer?.review || !importer?.mapToSource) throw new Error("AstroMapper import is unavailable");
          const review = importer.review(result.text, result.coordinateSize, { repairZeroArea });
          state.importResult = { ...result, coordinateSize: { ...result.coordinateSize } };
          state.importReview = review;
          state.importApplied = false;
          paintImportReview();
          paintRois();
          if (!review.count) throw new Error("No valid ROIs to import. Review the skipped rows.");
          rois = importedRois({ rois: importer.mapToSource(review.rows, result.coordinateSize,
            { width: info.width, height: info.height }) }, info, importer);
        } else {
          rois = importedRois(result, info, importer);
        }
        const selected = Number.isInteger(index) && index >= 0 && index < rois.length ? index : -1;
        if (pan && selected >= 0) focusRoi(viewer, rois[selected], info);
        state.rois = rois;
        state.roiName = typeof result.name === "string" ? result.name : "";
        state.roiIndex = selected;
        if (typeof result?.text !== "string") {
          state.importResult = null;
          state.importReview = null;
        }
        state.importApplied = true;
        paintImportReview();
        paintRois();
        showStatus("");
        return true;
      } catch (error) { showStatus(error.message); return false; }
    };
    const repairRois = () => {
      if (state.locked || state.pending || state.loadingRois || !state.importResult ||
          !state.importReview?.issues.some(issue => issue.repairable && !issue.repaired)) return false;
      const selected = state.importApplied ? state.rois[state.roiIndex]?.index : undefined;
      const success = setRois(state.importResult, { pan: false, index: -1, repairZeroArea: true });
      if (success && selected !== undefined) {
        state.roiIndex = state.rois.findIndex(rect => rect.index === selected);
        paintRois();
      }
      return success;
    };
    importRepair.addEventListener("click", repairRois);
    for (const name of ["pointerdown", "pointermove", "pointerup", "pointercancel", "wheel"])
      importReview.addEventListener(name, event => event.stopPropagation());
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
      if (target.contextmenu(event)) return;
      event.preventDefault();
      chooseRois();
    });
    win.nd2TranslucentSetRois = setRois;
    win.nd2TranslucentFocusRoi = rect => {
      if (!state.ready || state.locked || state.pending) return false;
      try { return focusRoi(viewer, rect, info); }
      catch (error) { showStatus(error.message); return false; }
    };
    const applyContext = nextContext => {
      if (disposed || !nextContext || typeof nextContext !== "object") return false;
      const wasLocked = state.locked;
      const hasZoom = Number.isFinite(nextContext.zoom) && nextContext.zoom > 0;
      const hasCenter = Array.isArray(nextContext.center) && nextContext.center.length === 2 &&
        nextContext.center.every(Number.isFinite);
      if (hasZoom) { activeContext.zoom = nextContext.zoom; maxZoomRatio = Math.max(maxZoomRatio, nextContext.zoom); }
      if (hasCenter) activeContext.center = nextContext.center.slice();
      if (Number.isFinite(nextContext.rotation)) activeContext.rotation = nextContext.rotation;
      if (typeof nextContext.flip === "boolean") activeContext.flip = nextContext.flip;
      if (validOpacity(nextContext.opacity)) {
        activeContext.opacity = state.opacity = state.confirmedOpacity = nextContext.opacity;
      }
      if (typeof nextContext.opacity_mode === "string") activeContext.opacity_mode = nextContext.opacity_mode;
      if (typeof nextContext.locked === "boolean") state.locked = nextContext.locked;
      if (typeof nextContext.source_format === "string") {
        activeContext.source_format = stageFollow.followFormat(nextContext.source_format);
        stageFollowCtrl.setFormat(activeContext.source_format);
      }
      if (nextContext.follow_flips && typeof nextContext.follow_flips === "object") {
        state.followFlips = { x: nextContext.follow_flips.flip_x === true, y: nextContext.follow_flips.flip_y === true };
      }
      if (Array.isArray(nextContext.laser_items)) laserAnnotations.hydrate(nextContext.laser_items);
      if (state.ready) {
        const vp = viewer.viewport;
        if (Number.isFinite(nextContext.rotation)) vp.setRotation(nextContext.rotation, true);
        if (typeof nextContext.flip === "boolean") vp.setFlip(nextContext.flip);
        if (hasCenter || hasZoom) {
          const saved = camera(viewer);
          const nextCenter = hasCenter ? vp.imageToViewportCoordinates(new OSD.Point(...nextContext.center)) : saved.center;
          restoreCamera(viewer, { zoom: hasZoom ? nextContext.zoom : saved.zoom, center: nextCenter });
        }
        if (hasCenter || hasZoom || Number.isFinite(nextContext.rotation) || typeof nextContext.flip === "boolean") {
          clearBackdrop();
          lastViewportPose = readViewportPose();
          lastViewSignature = readViewSignature(lastViewportPose);
        }
        if (Array.isArray(nextContext.rois)) {
          if (nextContext.rois.length) {
            setRois({ rois: nextContext.rois, name: nextContext.roiName || "" },
              { pan: false, index: nextContext.roiIndex ?? -1, initial: true });
          } else {
            state.rois = []; state.roiIndex = -1; state.roiName = "";
            state.importResult = state.importReview = null; state.importApplied = false;
            paintImportReview();
          }
        }
        renderOverlays();
        target.render();
      }
      paintLock();
      if (state.locked || wasLocked) reportLockedInteractiveRects();
      if (state.ready) reportView();
      return true;
    };
    win.nd2TranslucentApplyContext = applyContext;

    const boundFullyLoadedItems = new Set();
    const onFullyLoadedChange = event => {
      if (!disposed) displayGate.onFullyLoadedChange(event);
    };
    const bindItemFullyLoaded = item => {
      if (!item || boundFullyLoadedItems.has(item) || typeof item.addHandler !== "function") return;
      item.addHandler("fully-loaded-change", onFullyLoadedChange);
      boundFullyLoadedItems.add(item);
    };
    const unbindItemFullyLoaded = item => {
      if (!item || !boundFullyLoadedItems.has(item)) return;
      item.removeHandler?.("fully-loaded-change", onFullyLoadedChange);
      boundFullyLoadedItems.delete(item);
    };
    const onWorldAddItem = event => {
      if (!disposed) bindItemFullyLoaded(event?.item);
    };
    const onWorldRemoveItem = event => {
      unbindItemFullyLoaded(event?.item);
    };
    viewer.world?.addHandler?.("add-item", onWorldAddItem);
    viewer.world?.addHandler?.("remove-item", onWorldRemoveItem);
    bindItemFullyLoaded(viewer.world?.getItemAt?.(0));

    viewer.addHandler("open", () => {
      bindItemFullyLoaded(viewer.world?.getItemAt?.(0));
      const viewport = viewer.viewport;
      // Exactly the main viewer's orientation; this window has no orientation
      // settings of its own (an older handoff's mirror_scan is ignored, and
      // Follow's flips reverse its movement only).
      viewport.setRotation(activeContext.rotation || 0, true);
      viewport.setFlip(activeContext.flip === true);
      restoreCamera(viewer, { zoom: activeContext.zoom,
        center: viewport.imageToViewportCoordinates(new OSD.Point(...activeContext.center)) });
      state.ready = true;
      lastViewportPose = readViewportPose();
      lastViewSignature = readViewSignature(lastViewportPose);
      showStatus("");
      paintRois();
      paintZoom();
      stageFollowCtrl.paintFollow();
      resize();
      reportView();
      if (Array.isArray(activeContext.rois) && activeContext.rois.length) {
        setRois({ rois: activeContext.rois, name: activeContext.roiName || "" },
          { pan: false, index: activeContext.roiIndex ?? -1, initial: true });
      }
      if (state.locked) reportLockedInteractiveRects();
      startDisplayLoop();
    });
    viewer.addHandler("open-failed", () => {
      if (disposed) return;
      clearBackdrop();
      showStatus("Could not load this image. Close and reopen the translucent viewer.");
    });
    viewer.addHandler("tile-loaded", event => {
      if (!disposed) displayGate.onTileLoaded(event);
    });
    viewer.addHandler("tiled-image-drawn", event => {
      if (!disposed) displayGate.onTiledImageDrawn(event);
    });
    viewer.addHandler("fully-loaded-change", onFullyLoadedChange);
    viewer.addHandler("tile-load-failed", event => {
      if (!disposed) displayGate.onTileLoadFailed(event);
    });
    viewer.addHandler("zoom", paintZoom);
    viewer.addHandler("animation", paintZoom);
    // Diff camera, target, and Follow signature so LUT-only tile redraws never
    // rebuild #laser-annotations or send spurious view reports.
    viewer.addHandler("update-viewport", () => {
      const pose = readViewportPose();
      const moved = poseChanged(lastViewportPose, pose);
      if (backdropVisible && moved) {
        clearBackdrop();
      }
      lastViewportPose = pose;
      renderRoiBoxes();
      paintZoom();
      const sig = readViewSignature(pose);
      const changed = viewSignatureChanged(lastViewSignature, sig);
      lastViewSignature = sig;
      if (changed) {
        laserAnnotations.render();
        target.render();
        scheduleViewReport();
      }
    });
    const resize = () => {
      clearBackdrop();
      if (state.ready) {
        resizePreservingScale(viewer, stage.clientWidth, stage.clientHeight, OSD);
        lastViewportPose = readViewportPose();
        lastViewSignature = readViewSignature(lastViewportPose);
        renderOverlays();
        target.render();
        paintZoom();
        scheduleViewReport();
      }
      if (state.locked) reportLockedInteractiveRects();
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
            const box = resizeGeometry(gesture.width, gesture.height, gesture.edge,
              x - gesture.startX, y - gesture.startY);
            const result = requireSuccess(await api.resize_window(box.width, box.height), "Could not resize this window");
            const width = Number(result?.width) || box.width;
            const height = Number(result?.height) || box.height;
            const dx = gesture.edge.includes("w") ? gesture.appliedWidth - width : 0;
            const dy = gesture.edge.includes("n") ? gesture.appliedHeight - height : 0;
            if (dx || dy) requireSuccess(await api.move_window(dx, dy), "Could not move this window");
            gesture.appliedWidth = width; gesture.appliedHeight = height;
            scheduleViewReport();  // a moved or resized window changes no camera
          } else {
            const dx = x - gesture.appliedX, dy = y - gesture.appliedY;
            if (dx || dy) {
              requireSuccess(await api.move_window(dx, dy), "Could not move this window");
              scheduleViewReport();
            }
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
      if (event.altKey && !event.target?.closest?.("button, input, #opacity-control, #roi-import-review, #zoom-control, #stage-follow-control, #stage-follow-settings, #laser-target, #laser-target-capture, .resize-handle")) {
        startGesture(event);
      }
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

    const closestMatch = (el, selector, ids) => {
      const hit = el?.closest?.(selector);
      return Boolean(hit && typeof hit === "object" && (!hit.id || ids.includes(hit.id)));
    };
    const isInZoomControl = el => !!(el && (el === zoomControl || el === zoomBadge || el === zoomPopover ||
      el === zoomInput || zoomControl?.contains?.(el) || closestMatch(el, "#zoom-control", ["zoom-control"])));
    const isInZoomPopover = el => !!(el && (el === zoomPopover || el === zoomInput ||
      zoomPopover?.contains?.(el) || closestMatch(el, "#zoom-popover", ["zoom-popover"])));
    const isTextInput = el => {
      if (!el) return false;
      const tag = String(el.tagName || "").toUpperCase();
      if (tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable) return true;
      return tag === "INPUT" && !["range", "checkbox", "radio", "button", "submit", "reset"].includes(String(el.type || "text").toLowerCase());
    };
    const onDocPointerDown = event => {
      if (state.zoomPopoverOpen && !isInZoomControl(event.target)) {
        closeZoomPopover();
      }
    };
    doc.addEventListener("pointerdown", onDocPointerDown, true);
    const onDocKeyDown = event => {
      if (event.key === "Escape" && target.calibrating) {
        event.preventDefault(); event.stopImmediatePropagation(); target.cancel(); return;
      }
      if (event.repeat) return;
      if (event.key === "Escape" && !event.altKey && !event.ctrlKey && !event.metaKey) {
        if (state.zoomPopoverOpen) {
          event.preventDefault(); event.stopPropagation?.();
          closeZoomPopover({ restoreFocus: true });
          return;
        }
        event.preventDefault(); close(); return;
      }
      if (event.altKey) return;
      const eventTarget = event.target || doc.activeElement;
      if (isInZoomPopover(eventTarget) || isInZoomPopover(doc.activeElement) || isTextInput(eventTarget)) return;
      if (closestMatch(eventTarget, "#stage-follow-control, #stage-follow-settings",
        ["stage-follow-control", "stage-follow-settings"])) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "o") {
        event.preventDefault(); chooseRois(); return;
      }
      if (event.ctrlKey || event.metaKey) return;
      if (closestMatch(eventTarget, "#opacity-control, #roi-import-review", ["opacity-control", "roi-import-review"]) &&
          /^(Arrow|Home$|End$|Page)/.test(event.key)) return;
      if (event.key.toLowerCase() === "l") { event.preventDefault(); toggleLock(); }
      else if (event.key === "ArrowLeft") { event.preventDefault(); selectRoi(state.roiIndex - 1); }
      else if (event.key === "ArrowRight") { event.preventDefault(); selectRoi(state.roiIndex + 1); }
    };
    doc.addEventListener("keydown", onDocKeyDown, true);
    // Keep the embedded page itself at 100%; zoom belongs to the image.
    const onDocWheel = event => { if (event.ctrlKey || state.locked) event.preventDefault(); };
    doc.addEventListener("wheel", onDocWheel, { passive: false });
    const forwardClientError = stageFollow.attachClientErrorForwarder(win, api);
    win.addEventListener("pagehide", () => {
      disposed = true;
      cancelBackoff?.();
      displayGate.dispose();
      clearBackdrop();
      closeZoomPopover();
      target.cancel();
      stageFollowCtrl.dispose();
      if (discoveryInterval !== null && typeof win.clearInterval === "function") win.clearInterval(discoveryInterval);
      viewReporter.dispose();
      cancelGesture();
      viewer.world?.removeHandler?.("add-item", onWorldAddItem);
      viewer.world?.removeHandler?.("remove-item", onWorldRemoveItem);
      for (const item of Array.from(boundFullyLoadedItems)) {
        unbindItemFullyLoaded(item);
      }
      win.removeEventListener?.("resize", resize);
      doc.removeEventListener?.("pointerdown", onDocPointerDown, true);
      doc.removeEventListener?.("keydown", onDocKeyDown, true);
      doc.removeEventListener?.("wheel", onDocWheel, { passive: false });
      observer?.disconnect();
      viewer.destroy();
    }, { once: true });
    pollSlacsDiscovery();
    if (typeof win.setInterval === "function") {
      discoveryInterval = win.setInterval(() => { reportMovedWindow(); pollSlacsDiscovery(); }, 1000);
    }
    return { viewer, state, toggleLock, close, resize, setRois, selectRoi, setOpacity, repairRois,
      startStageFollow, stopStageFollow, pollStageFollow, pollSlacsDiscovery, revokeStageFollow,
      setStepUm, setStepAuto, setFollowFlips, reportView, forwardClientError, target,
      openZoomPopover, closeZoomPopover, setZoom: applyImageZoom, stepZoom,
      applyContext, reportLockedInteractiveRects, applyDisplaySnapshot, displayGate };
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
        const closeOnFail = () => win.pywebview?.api?.close_translucent?.();
        doc.getElementById("overlay-close").onclick = closeOnFail;
        doc.addEventListener("keydown", event => {
          if (event.key === "Escape") { event.preventDefault(); closeOnFail(); }
        }, true);
      }
    };
    if (win.pywebview?.api?.translucent_context) start();
    else win.addEventListener("pywebviewready", start, { once: true });
  }

  return { validateContext, validateMergedQuery, makeTileSource, camera, restoreCamera, resizePreservingScale,
    resizeGeometry, roiBounds, importedRois, focusRoi, roiElementPoints,
    stagePixelSize: stageFollow.stagePixelSize,
    screenImageDelta: stageFollow.screenImageDelta,
    slacsRoughnessText: stageFollow.slacsRoughnessText,
    wheelZoomFactor, formatZoomLabel, parseZoomInput, mount, boot };
});
