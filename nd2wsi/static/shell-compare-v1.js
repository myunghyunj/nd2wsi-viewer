/* Compare group engine: spatial identity, coordinate transforms, viewport relay,
 * atomic landmark alignment, nudge queue, compare controls, pair picker, and
 * split-divider / menu interactions for the desktop shell. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ND2ShellCompare = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const DEFAULT_ORIENTATION_ACTIONS = {
    "compare-flip-horizontal": "flip-horizontal",
    "compare-flip-vertical": "flip-vertical",
    "compare-rotate-left": "rotate-left",
    "compare-rotate-right": "rotate-right",
    "compare-transpose": "transpose",
    "compare-orientation-reset": "reset",
  };

  function createCompare(deps) {
    const {
      compare,
      pairPicker,
      frames,
      getSlides,
      getActive,
      setActive,
      Align,
      ShortcutRouter,
      ORIENTATION_ACTIONS = DEFAULT_ORIENTATION_ACTIONS,
      VIEWPORT_PROTOCOL_VERSION = 2,
      VIEWPORT_THROTTLE_MS = 48,
      MAX_GROUP = 4,
      LANDMARKS_NEEDED = 4,
      $,
      document = globalThis.document,
      window = globalThis.window,
      crypto = globalThis.crypto,
      setTimeout = (...args) => globalThis.setTimeout(...args),
      clearTimeout = (...args) => globalThis.clearTimeout(...args),
      requestAnimationFrame = (...args) => globalThis.requestAnimationFrame(...args),
      groupSids,
      inGroup,
      slideName,
      rememberSlide,
      ensureFrame,
      applyFrameLayout,
      render,
      stopCompare,
      postToSlide,
      showError,
      scheduleNativeGestureScopes,
      refreshShellToolButtons = () => {},
      finitePoint,
      localIdentity,
      sameIdentity,
    } = deps;
    const api = {};

    function syncCompareToolbarSpace() {
      const controls = $("compare-controls");
      const edit = compare.landmark.edit;
      const width = window.innerWidth;
      if (!edit || edit.toolbarLock?.width !== width) {
        controls.style.height = "";
        controls.style.overflowY = "";
      }
      let height = 0;
      if (compare.enabled && compare.toolsVisible) {
        if (edit && edit.toolbarLock?.width === width) height = edit.toolbarLock.height;
        else {
          height = Math.ceil(controls.getBoundingClientRect().height) + 20;
          if (edit) edit.toolbarLock = {width, height};
        }
        if (edit) {
          controls.style.height = (height - 20) + "px";
          controls.style.overflowY = "auto";
        }
      }
      if (compare.toolbarHeight === height) return;
      compare.toolbarHeight = height;
      document.documentElement.style.setProperty("--compare-toolbar-height", `${height}px`);
      if (compare.enabled) api.requestGroupSoon("sync");
    }

    function syncCompareToolsVisibility() {
      const visible = compare.enabled && compare.toolsVisible;
      $("compare-controls").hidden = !visible;
      const toggle = $("compare-tools-toggle");
      toggle.hidden = !compare.enabled;
      toggle.classList.toggle("active", visible);
      toggle.setAttribute("aria-expanded", String(visible));
      const label = visible ? "Hide comparison tools" : "Show comparison tools";
      toggle.setAttribute("aria-label", label);
      toggle.title = !visible && compare.landmark.active
        ? `${label} — alignment in progress` : label;
      syncCompareToolbarSpace();
      scheduleNativeGestureScopes();
    }

    function setCompareToolsVisible(visible) {
      if (!compare.enabled) return;
      api.closePairPicker(false);
      compare.toolsVisible = Boolean(visible);
      syncCompareToolsVisibility();
      // Hiding tools must not clear the group, transforms, in-flight operations,
      // or a landmark edit. Keep a keyboard-accessible way to return to those tools.
      $(compare.toolsVisible ? "compare-close" : "compare-tools-toggle").focus();
    }

    function normalizeViewportState(data, sid) {
      const centerPx = finitePoint(data.centerPx);
      const spanPx = finitePoint(data.spanPx, true);
      const imagePx = finitePoint(data.imagePx, true);
      if (typeof data.paneInstanceId !== "string" || !Number.isSafeInteger(data.contextEpoch)) return null;
      if (data.imageReady === true && (!centerPx || !spanPx || !imagePx)) return null;
      const pixelSizeUm = Array.isArray(data.pixelSizeUm)
        ? finitePoint({x: data.pixelSizeUm[1], y: data.pixelSizeUm[0]}, true)
        : finitePoint(data.pixelSizeUm, true);
      return {
        sid, seq: Number(data.seq) || 0, reason: String(data.reason || "user"),
        requestId: data.requestId == null ? null : String(data.requestId),
        echoOf: data.echoOf == null ? null : String(data.echoOf),
        centerPx, spanPx, imagePx, pixelSizeUm,
        physicalScale: data.physicalScale ? {...data.physicalScale} : null,
        containerPx: finitePoint(data.containerPx, true) || {x: 1, y: 1},
        plateGrid: data.plateGrid === true, imageReady: data.imageReady === true,
        translucentAvailable: data.translucentAvailable === true,
        translucentOpening: data.translucentOpening === true, infoOpen: data.infoOpen === true,
        paneInstanceId: data.paneInstanceId, contextEpoch: data.contextEpoch,
        spatialContext: data.spatialContext?.key ? {...data.spatialContext} : null,
        groupSessionId: data.groupSessionId, groupEpoch: data.groupEpoch,
      };
    }

    function uniqueId() {
      return crypto.randomUUID();
    }

    function cloneValue(value) {
      return value == null ? value : JSON.parse(JSON.stringify(value));
    }

    function spatialEnvelope(sid) {
      const identity = localIdentity(compare.states.get(sid));
      return identity && compare.groupSessionId ? {
        ...identity, groupSessionId: compare.groupSessionId, groupEpoch: compare.groupEpoch,
      } : null;
    }

    function currentEnvelope(data, sid) {
      return sameIdentity(data, localIdentity(compare.states.get(sid))) &&
        data.groupSessionId === compare.groupSessionId && data.groupEpoch === compare.groupEpoch;
    }

    function sendSpatial(sid, message) {
      const envelope = spatialEnvelope(sid);
      if (!envelope || !compare.states.get(sid)?.spatialContext) return false;
      const commandSeq = ++compare.commandSeq;
      return postToSlide(sid, {
        ...message, ...envelope, version: VIEWPORT_PROTOCOL_VERSION,
        commandSeq, commandId: "shell-spatial-" + commandSeq,
      });
    }

    function spatialGroupReady(operation = "spatial") {
      if (!compare.enabled || !compare.members.length) return false;
      for (const sid of groupSids()) {
        const st = compare.states.get(sid);
        if (!st?.imageReady || st.plateGrid || !st.spatialContext?.key ||
            !finitePoint(st.imagePx, true) || !finitePoint(st.spanPx, true)) return false;
      }
      // Clear/remove/reset still require a real site, but can repair an unsupported fit.
      if (["orientation", "remove-fit", "relative", "edit"].includes(operation)) return true;
      return compare.members.every((sid) => {
        const pair = ensurePairTransform(sid);
        return pair && rendererForPair(sid, pair).supported;
      });
    }

    function spatialPauseReason() {
      if (groupSids().some((sid) => compare.states.get(sid)?.plateGrid)) return "Paused · focus a site in every plate";
      if (!groupSids().every((sid) => compare.states.get(sid)?.imageReady &&
          compare.states.get(sid)?.spatialContext)) return "Waiting for current views";
      if (!spatialGroupReady()) return "Paused · anisotropic mapping unsupported; choose Relative explicitly";
      return "";
    }

    function physicalScaleFor(st) {
      if (!finitePoint(st?.pixelSizeUm, true)) return null;
      const value = st.physicalScale;
      if (!value || ![value.x, value.y].every((v) => Number.isFinite(v) && v > 0) ||
          !Number.isFinite(value.cosine) || Math.abs(value.cosine) > 1.000001) return null;
      return value;
    }

    function scaleLinkIssue(mode = compare.linkMode) {
      if (!spatialGroupReady("edit")) return "Focus a ready image in every view";
      const scales = groupSids().map((sid) => physicalScaleFor(compare.states.get(sid)));
      if (scales.some((s) => !s)) return "Pixel calibration is needed in every view";
      const first = scales[0];
      if (scales.some((s) => Math.abs(s.y / s.x - first.y / first.x) > 0.001 ||
          Math.abs(s.cosine - first.cosine) > 0.001)) {
        return "Pixel shape or orientation differs; equal physical scale is unavailable";
      }
      if (mode === "scale-frame" && !spatialGroupReady()) return spatialPauseReason();
      const low = Math.max(...scales.map((s) => Number.isFinite(s.min) && s.min > 0 ? s.min : 0));
      const high = Math.min(...scales.map((s) => Number.isFinite(s.max) && s.max > 0 ? s.max : Infinity));
      return low > high ? "These views have no shared zoom range" : "";
    }

    function linkedScale(source) {
      const value = physicalScaleFor(source);
      if (!value || scaleLinkIssue()) return null;
      const scales = groupSids().map((sid) => physicalScaleFor(compare.states.get(sid)));
      const low = Math.max(...scales.map((s) => Number.isFinite(s.min) && s.min > 0 ? s.min : 0));
      const high = Math.min(...scales.map((s) => Number.isFinite(s.max) && s.max > 0 ? s.max : Infinity));
      return Math.max(low, Math.min(high, value.x));
    }

    function invalidateSpatialWork() {
      clearNudgeCommands();
      clearPendingRequest();
      clearViewportRoutes();
      compare.groupEpoch += 1;
      compare.landmark = {active: false, edit: null};
      broadcastCompareState();
    }

    function committedMutation() {
      compare.committedRevision += 1;
      compare.committedGroup = snapshotAlignment();
      rememberAlignment();
    }

    function adoptSpatialIdentity(sid, st) {
      const previous = compare.states.get(sid);
      const retired = compare.retiredInstances.get(sid) || new Set();
      if (retired.has(st.paneInstanceId)) return false;
      if (previous?.paneInstanceId === st.paneInstanceId && st.contextEpoch < previous.contextEpoch) return false;
      const changed = previous && !sameIdentity(localIdentity(previous), localIdentity(st));
      if (changed && inGroup(sid)) {
        // Save only committed geometry under the OLD site identity, before replacement.
        rememberAlignment();
        invalidateSpatialWork();
      }
      if (previous && previous.paneInstanceId !== st.paneInstanceId) {
        retired.add(previous.paneInstanceId);
        compare.retiredInstances.set(sid, retired);
      }
      compare.states.set(sid, st);
      if (changed && inGroup(sid)) {
        compare.anchorLandmarks = [];
        compare.anchorSet = {id: uniqueId(), revision: 0, points: []};
        compare.pairs = new Map(compare.members.map((member) => [member, newPair()]));
        for (const member of compare.members) restoreAlignment(compare.anchorSid, member, compare.pairs.get(member));
        compare.committedRevision += 1;
        compare.committedGroup = snapshotAlignment();
      }
      return true;
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
        // One unit is an image width on BOTH axes. A unit-square mapping would
        // distort nonsquare, uncalibrated images when rotated by 90 degrees.
        : { x: point.x / st.imagePx.x, y: point.y / st.imagePx.x };
    }

    function spaceToPx(point, st, mode) {
      return mode === "physical"
        ? { x: point.x / st.pixelSizeUm.x, y: point.y / st.pixelSizeUm.y }
        : { x: point.x * st.imagePx.x, y: point.y * st.imagePx.x };
    }

    function imageCenterSpace(st, mode) {
      return pxToSpace({ x: st.imagePx.x / 2, y: st.imagePx.y / 2 }, st, mode);
    }

    function newPair() {
      return {
        mode: null, forceRelative: false, orientation: Align.identity(), transform: null,
        fitTransform: null, manualOffset: {x: 0, y: 0}, fit: null, currentRms: null,
        landmarks: [], landmarkSet: {id: uniqueId(), revision: 0, points: []},
        provenance: null, provenanceMismatch: false,
      };
    }

    function defaultTransform(pair, anchorState, memberState, mode) {
      const linear = pair.orientation;
      return Align.translationMatching(
        linear, imageCenterSpace(anchorState, mode), imageCenterSpace(memberState, mode)
      );
    }

    function ensurePairTransform(sid) {
      const pair = compare.pairs.get(sid);
      const anchorState = compare.states.get(compare.anchorSid);
      const memberState = compare.states.get(sid);
      if (!pair || !anchorState?.imageReady || !memberState?.imageReady ||
          !anchorState.spatialContext || !memberState.spatialContext) return null;
      const mode = pair.forceRelative ? "normalized" : mappingMode(anchorState, memberState);
      if (pair.transform && pair.mode === mode) return pair;
      pair.mode = mode;
      pair.transform = defaultTransform(pair, anchorState, memberState, mode);
      pair.fit = null;
      pair.fitTransform = null;
      pair.provenance = null;
      pair.manualOffset = {x: 0, y: 0};
      return pair;
    }

    function pixelScale(st, mode) {
      return mode === "physical" ? st.pixelSizeUm : {x: 1 / st.imagePx.x, y: 1 / st.imagePx.x};
    }

    function pixelTransform(sid, pair) {
      const a = compare.states.get(compare.anchorSid), b = compare.states.get(sid);
      return a && b && pair?.transform ? Align.pixelMapping(
        pair.transform, pixelScale(a, pair.mode), pixelScale(b, pair.mode)
      ) : null;
    }

    function rendererForPair(sid, pair) {
      return Align.rendererPose(pixelTransform(sid, pair));
    }

    function updatePairResidual(pair) {
      const provenance = pair.provenance;
      if (!pair.fitTransform || !provenance || pair.provenanceMismatch) {
        pair.currentRms = null;
        return;
      }
      pair.currentRms = Align.residual(pair.transform, provenance.from, provenance.to);
      pair.manualOffset = {
        x: pair.transform.tx - pair.fitTransform.tx,
        y: pair.transform.ty - pair.fitTransform.ty,
      };
    }

    function displayTransformFor(sid) {
      if (!compare.enabled || sid === compare.anchorSid) return {degrees: 0, flipped: false};
      const pair = ensurePairTransform(sid);
      const renderer = rendererForPair(sid, pair);
      return renderer.supported ? renderer.pose : null;
    }

    function applyDisplayTransform(sid) {
      if (!sid || !spatialGroupReady()) return;
      const pose = displayTransformFor(sid);
      if (pose) sendSpatial(sid, {nd2wsi: "display-transform", ...pose});
    }

    function applyDisplayTransforms() {
      if (compare.linked && compare.linkMode === "scale") return;
      for (const sid of groupSids()) applyDisplayTransform(sid);
    }

    function clearDisplayTransforms(sids) {
      // A lifecycle reset, not an unscoped display command. A grid may reset safely.
      for (const sid of new Set(sids.filter(Boolean))) {
        const envelope = spatialEnvelope(sid);
        if (envelope) postToSlide(sid, {
          nd2wsi: "compare-state", version: VIEWPORT_PROTOCOL_VERSION,
          ...envelope, enabled: false, linked: false, moving: false,
        });
      }
    }

    function rematchTranslation(sid, states = compare.states) {
      if (!spatialGroupReady()) return false;
      const pair = ensurePairTransform(sid);
      const a = states.get(compare.anchorSid), b = states.get(sid);
      if (!pair || !a || !b) return false;
      pair.transform = Align.translationMatching(
        pair.transform, pxToSpace(a.centerPx, a, pair.mode), pxToSpace(b.centerPx, b, pair.mode)
      );
      updatePairResidual(pair);
      if (!pair.fitTransform) {
        const base = defaultTransform(pair, a, b, pair.mode);
        pair.manualOffset = {x: pair.transform.tx - base.tx, y: pair.transform.ty - base.ty};
      }
      return true;
    }

    function recaptureAll(states = compare.states) {
      let any = false;
      for (const sid of compare.members) any = rematchTranslation(sid, states) || any;
      return any;
    }

    function pairKey(anchorSid, memberSid) {
      const a = compare.states.get(anchorSid)?.spatialContext?.key;
      const b = compare.states.get(memberSid)?.spatialContext?.key;
      return a && b ? JSON.stringify([a, b]) : null;
    }

    function clonePoints(points) {
      return cloneValue(points || []);
    }

    function rememberAlignment() {
      if (!compare.enabled) return;
      for (const sid of compare.members) {
        const pair = compare.pairs.get(sid), key = pairKey(compare.anchorSid, sid);
        if (!key || !pair?.transform) continue;
        compare.memory.set(key, {
          pair: cloneValue(pair), anchorSet: cloneValue(compare.anchorSet),
          anchorContext: compare.states.get(compare.anchorSid).spatialContext.key,
          memberContext: compare.states.get(sid).spatialContext.key,
        });
      }
    }

    function restoreAlignment(anchorSid, memberSid, pair) {
      const key = pairKey(anchorSid, memberSid);
      if (!key) return false;
      const direct = compare.memory.get(key);
      const reversed = compare.memory.get(pairKey(memberSid, anchorSid));
      if (!direct && !reversed) return false;
      const saved = direct || reversed;
      const next = direct ? cloneValue(saved.pair) : reversePair(saved.pair, saved.anchorSet);
      if (!next) return false;
      const anchorSet = direct ? cloneValue(saved.anchorSet) : cloneValue(saved.pair.landmarkSet);
      if (!compare.anchorSet.points.length) {
        compare.anchorSet = anchorSet;
        compare.anchorLandmarks = clonePoints(anchorSet.points);
      }
      next.provenanceMismatch = Boolean(next.provenance && (
        next.provenance.anchorSetId !== compare.anchorSet.id ||
        next.provenance.anchorRevision !== compare.anchorSet.revision
      ));
      Object.assign(pair, next);
      updatePairResidual(pair);
      return true;
    }

    function reversePair(old, oldAnchorSet) {
      const transform = Align.invert(old.transform);
      if (!transform) return null;
      const pair = cloneValue(old);
      pair.orientation = Align.invert(old.orientation);
      pair.transform = transform;
      pair.fitTransform = old.fitTransform ? Align.invert(old.fitTransform) : null;
      if (!pair.fitTransform) {
        const offset = Align.applyLinear(transform, old.manualOffset || {x: 0, y: 0});
        pair.manualOffset = {x: -offset.x, y: -offset.y};
      }
      pair.landmarkSet = cloneValue(oldAnchorSet);
      pair.landmarks = clonePoints(oldAnchorSet.points);
      if (old.provenance && pair.fitTransform) {
        pair.provenance = {
          ...cloneValue(old.provenance),
          from: clonePoints(old.provenance.to), to: clonePoints(old.provenance.from),
          anchorContext: old.provenance.memberContext, memberContext: old.provenance.anchorContext,
          anchorSetId: old.landmarkSet.id, anchorRevision: old.landmarkSet.revision,
          memberSetId: oldAnchorSet.id, memberRevision: oldAnchorSet.revision,
          anchorPointIds: cloneValue(old.provenance.memberPointIds),
          memberPointIds: cloneValue(old.provenance.anchorPointIds),
        };
        pair.fit = {
          ...pair.fit, transform: cloneValue(pair.fitTransform),
          rms: Align.residual(pair.fitTransform, pair.provenance.from, pair.provenance.to),
          scale: Align.scale(pair.fitTransform), angleDeg: Align.angleDeg(pair.fitTransform),
        };
      }
      updatePairResidual(pair);
      return pair;
    }

    /* ---- relay ---------------------------------------------------------------- */

    function anchorViewFromSource(sourceSid, source) {
      if (sourceSid === compare.anchorSid) return {centerPx: source.centerPx, widthPx: source.spanPx.x};
      const pair = ensurePairTransform(sourceSid);
      const renderer = rendererForPair(sourceSid, pair);
      const inverse = Align.invert(pixelTransform(sourceSid, pair));
      if (!renderer.supported || !inverse) return null;
      return {centerPx: Align.apply(inverse, source.centerPx), widthPx: source.spanPx.x / renderer.scale};
    }

    function targetViewFromAnchor(targetSid, anchorView) {
      const targetState = compare.states.get(targetSid);
      if (!targetState) return null;
      if (targetSid === compare.anchorSid) return {
        centerPx: anchorView.centerPx, spanX: anchorView.widthPx, state: targetState,
      };
      const pair = ensurePairTransform(targetSid);
      const renderer = rendererForPair(targetSid, pair);
      if (!renderer.supported) return null;
      return {
        centerPx: Align.apply(pixelTransform(targetSid, pair), anchorView.centerPx),
        spanX: anchorView.widthPx * renderer.scale, state: targetState,
      };
    }

    function forwardViewport(sourceSid, source) {
      if (!compare.linked || compare.pendingRequest || compare.landmark.active ||
          !inGroup(sourceSid) || scaleLinkIssue() ||
          !sameIdentity(localIdentity(source), localIdentity(compare.states.get(sourceSid)))) return;
      const scaleOnly = compare.linkMode === "scale";
      const umPerCss = linkedScale(source);
      const anchorView = scaleOnly ? null : anchorViewFromSource(sourceSid, source);
      if (!umPerCss || (!scaleOnly && !anchorView)) return;
      for (const targetSid of groupSids()) {
        if (compare.pendingNudge?.sid === targetSid) continue;
        const target = compare.states.get(targetSid), scale = physicalScaleFor(target);
        const changingScale = Math.abs(scale.x / umPerCss - 1) > 1e-6;
        if (targetSid === sourceSid && !changingScale) continue;
        if (scaleOnly && !changingScale) continue;
        const view = scaleOnly || targetSid === sourceSid
          ? {centerPx: target.centerPx, state: target}
          : targetViewFromAnchor(targetSid, anchorView);
        if (view) view.spanX = target.spanPx.x * umPerCss / scale.x;
        if (!view || !Number.isFinite(view.spanX) || view.spanX <= 0) continue;
        sendSpatial(targetSid, {
          nd2wsi: "viewport-apply", sourceSid, sourceSeq: source.seq,
          scaleOnly: scaleOnly || targetSid === sourceSid,
          centerPx: view.centerPx,
          spanPx: {x: view.spanX, y: view.spanX * view.state.containerPx.y / view.state.containerPx.x},
          animate: false,
        });
      }
    }

    function scheduleViewportRoute(sourceSid, state) {
      if (scaleLinkIssue()) return;
      compare.routeLatest.set(sourceSid, state);
      if (compare.routeTimers.has(sourceSid)) return;
      const epoch = compare.groupEpoch, session = compare.groupSessionId;
      const timer = setTimeout(() => {
        if (compare.routeTimers.get(sourceSid) !== timer) return;
        compare.routeTimers.delete(sourceSid);
        const latest = compare.routeLatest.get(sourceSid);
        compare.routeLatest.delete(sourceSid);
        if (latest && epoch === compare.groupEpoch && session === compare.groupSessionId) {
          forwardViewport(sourceSid, latest);
        }
      }, VIEWPORT_THROTTLE_MS);
      compare.routeTimers.set(sourceSid, timer);
    }

    function clearViewportRoutes() {
      for (const timer of compare.routeTimers.values()) clearTimeout(timer);
      compare.routeTimers.clear();
      compare.routeLatest.clear();
    }

    function clearPendingRequest(preserveLayout = false) {
      if (compare.pendingRequest?.timer) clearTimeout(compare.pendingRequest.timer);
      compare.pendingRequest = null;
      if (!preserveLayout) {
        clearTimeout(compare.layoutRequestTimer);
        compare.layoutRequestTimer = null;
      }
    }

    function syncFromAnchor() {
      const anchorState = compare.states.get(compare.anchorSid);
      if (anchorState && compare.linked) forwardViewport(compare.anchorSid, anchorState);
    }

    function requestGroup(kind, details = {}) {
      if (compare.pendingNudge) {
        if (kind === "sync") api.requestGroupSoon("sync");
        return false;
      }
      if (!spatialGroupReady(kind === "scale-link" || (kind === "sync" && compare.linkMode === "scale")
          ? "edit" : kind) || (compare.landmark.active && kind !== "sync")) return false;
      if (compare.pendingRequest) {
        if (kind === "sync") api.requestGroupSoon("sync");
        return false;
      }
      clearViewportRoutes();
      const requestId = "shell-request-" + (++compare.requestSeq);
      const expectedTargets = Object.freeze([...groupSids()]);
      const contexts = new Map(expectedTargets.map((sid) => [sid, spatialEnvelope(sid)]));
      const pending = {
        ...details, requestId, kind, expectedTargets, contexts, responses: new Map(),
        groupSessionId: compare.groupSessionId, groupEpoch: compare.groupEpoch,
        editId: compare.landmark.edit?.editId ?? null,
        committedRevision: compare.committedRevision, timer: null,
      };
      pending.timer = setTimeout(() => {
        if (compare.pendingRequest !== pending) return;
        clearPendingRequest(true);
        api.updateCompareControls();
        if (kind !== "sync") showError("Could not read every current linked view; nothing was changed");
      }, 2500);
      compare.pendingRequest = pending;
      broadcastCompareState();
      for (const sid of expectedTargets) sendSpatial(sid, {nd2wsi: "viewport-request", requestId});
      api.updateCompareControls();
      return true;
    }

    function transactionCurrent(pending) {
      return pending && compare.pendingRequest === pending &&
        pending.groupSessionId === compare.groupSessionId && pending.groupEpoch === compare.groupEpoch &&
        pending.committedRevision === compare.committedRevision &&
        pending.editId === (compare.landmark.edit?.editId ?? null) &&
        pending.expectedTargets.length === groupSids().length &&
        pending.expectedTargets.every((sid) => inGroup(sid) &&
          sameIdentity(pending.contexts.get(sid), localIdentity(compare.states.get(sid)))) &&
        spatialGroupReady(pending.kind === "scale-link" ||
          (pending.kind === "sync" && compare.linkMode === "scale") ? "edit" : pending.kind);
    }

    function requestGroupSoon(kind) {
      clearTimeout(compare.layoutRequestTimer);
      const epoch = compare.groupEpoch, session = compare.groupSessionId;
      const timer = setTimeout(() => {
        if (compare.layoutRequestTimer !== timer) return;
        compare.layoutRequestTimer = null;
        if (compare.enabled && epoch === compare.groupEpoch && session === compare.groupSessionId) api.requestGroup(kind);
      }, 80);
      compare.layoutRequestTimer = timer;
    }

    function finishGroupRequest(pending) {
      if (!transactionCurrent(pending) ||
          !pending.expectedTargets.every((sid) => pending.responses.has(sid))) return false;
      const captured = pending.responses;
      clearPendingRequest(true);
      let changed = false;
      if (pending.kind === "capture") {
        changed = recaptureAll(captured);
        compare.linked = true;
      } else if (pending.kind === "scale-link") {
        compare.linked = true;
      } else if (["orientation", "remove-fit", "relative"].includes(pending.kind)) {
        const sid = pending.targetSid, previous = compare.pairs.get(sid);
        if (!compare.members.includes(sid) || !previous || compare.landmark.active) return false;
        const pair = cloneValue(previous);
        const a = captured.get(compare.anchorSid), b = captured.get(sid);
        if (pending.kind === "orientation") {
          if (pair.fit) return false;
          const next = Align.reorient(pair.orientation, pending.action);
          if (!next) return false;
          pair.orientation = next;
        } else {
          pair.fit = null; pair.fitTransform = null; pair.provenance = null;
          pair.currentRms = null; pair.provenanceMismatch = false;
          pair.manualOffset = {x: 0, y: 0};
          if (pending.kind === "relative") {
            pair.forceRelative = Boolean(pending.relative);
            pair.mode = pair.forceRelative ? "normalized" : mappingMode(a, b);
          }
        }
        pair.transform = Align.translationMatching(
          pair.orientation, pxToSpace(a.centerPx, a, pair.mode), pxToSpace(b.centerPx, b, pair.mode)
        );
        if (!rendererForPair(sid, pair).supported) {
          showError("This physical mapping needs anisotropic rendering. Select Relative to use display-only alignment.");
          api.updateCompareControls();
          return false;
        }
        compare.pairs.set(sid, pair);
        changed = true;
      }
      if (changed) committedMutation();
      // Transaction snapshots never overwrite newer unsolicited viewport states.
      for (const [sid, st] of captured) {
        if (st.seq >= (compare.states.get(sid)?.seq || 0)) compare.states.set(sid, st);
      }
      broadcastCompareState();
      if (["orientation", "remove-fit", "relative"].includes(pending.kind)) applyDisplayTransform(pending.targetSid);
      else if (pending.kind !== "scale-link") applyDisplayTransforms();
      if (["sync", "capture", "scale-link"].includes(pending.kind)) api.syncFromAnchor();
      api.updateCompareControls();
      return true;
    }

    function receiveViewportState(data, sid) {
      if (data.version !== VIEWPORT_PROTOCOL_VERSION) return;
      const state = normalizeViewportState(data, sid);
      if (!state) return;
      if (data.nudgeCommandId) return receiveNudgeReply(data, state, sid);
      if (data.nudge === true) return receiveDragNudge(data, state, sid);
      if (state.requestId) {
        // A cancelled/unknown request must never become a user pan or mutate global state.
        const pending = compare.pendingRequest;
        if (!transactionCurrent(pending) || state.requestId !== pending.requestId ||
            !pending.expectedTargets.includes(sid) || !currentEnvelope(data, sid) ||
            !sameIdentity(localIdentity(state), pending.contexts.get(sid)) || !state.imageReady || state.plateGrid) return;
        pending.responses.set(sid, state);
        if (pending.responses.size === pending.expectedTargets.length) finishGroupRequest(pending);
        return;
      }
      const previous = compare.states.get(sid);
      const same = sameIdentity(localIdentity(previous), localIdentity(state));
      if (same && state.seq <= previous.seq) return false;
      // Only readiness may introduce a new pane/context. Ordinary late packets cannot revive it.
      if (data.nd2wsi !== "viewport-ready") {
        if (!same || (inGroup(sid) && !currentEnvelope(data, sid))) return;
        compare.states.set(sid, state);
        if (sid === getActive()) refreshShellToolButtons();
      } else {
        if (!adoptSpatialIdentity(sid, state)) return;
        if (inGroup(sid) && previous?.imageReady && !state.imageReady) {
          clearPendingRequest();
          clearNudgeCommands();
          clearViewportRoutes();
        }
        broadcastCompareState();
        if (inGroup(sid)) {
          for (const member of compare.members) ensurePairTransform(member);
          applyDisplayTransforms();
          api.updateCompareControls();
        }
        return true;
      }
      if (inGroup(sid) && ["transform", "apply"].includes(state.reason)) api.updateCompareControls();
      if (!compare.linked || compare.pendingRequest || compare.landmark.active ||
          !inGroup(sid) || scaleLinkIssue() || state.echoOf) return;
      if (state.reason === "transform") { api.syncFromAnchor(); return; }
      if (state.reason !== "user") return;
      scheduleViewportRoute(sid, state);
    }

    /* ---- landmarks ------------------------------------------------------------ */

    function sendLandmarkMode(sid, active, extra = {}) {
      const edit = compare.landmark.edit;
      const points = sid === compare.anchorSid
        ? edit?.anchorSet.points || compare.anchorLandmarks
        : edit?.pairs.get(sid)?.landmarks || compare.pairs.get(sid)?.landmarks || [];
      sendSpatial(sid, {
        nd2wsi: "landmark-mode", active: Boolean(active), needed: LANDMARKS_NEEDED,
        points: clonePoints(points), editId: edit?.editId ?? null,
        editRevision: edit?.editRevision ?? 0, ...extra,
      });
    }

    function snapshotAlignment() {
      return {
        anchorSet: cloneValue(compare.anchorSet), anchorLandmarks: clonePoints(compare.anchorLandmarks),
        linked: compare.linked, linkMode: compare.linkMode,
        pairs: new Map([...compare.pairs].map(([sid, pair]) => [sid, cloneValue(pair)])),
      };
    }

    function restoreSnapshot(snapshot) {
      compare.anchorSet = cloneValue(snapshot.anchorSet);
      compare.anchorLandmarks = clonePoints(snapshot.anchorLandmarks);
      compare.linked = snapshot.linked;
      compare.linkMode = snapshot.linkMode || "scale-frame";
      compare.pairs = new Map([...snapshot.pairs].map(([sid, pair]) => [sid, cloneValue(pair)]));
    }

    function startLandmarks() {
      if (compare.pendingNudge) return;
      if (!spatialGroupReady("edit") || (compare.pendingRequest && compare.pendingRequest.kind !== "sync") ||
          compare.landmark.active) return;
      clearPendingRequest();
      clearViewportRoutes();
      api.closePairPicker(false);
      const before = snapshotAlignment();
      for (const pair of before.pairs.values()) {
        if (pair.provenanceMismatch) {
          pair.landmarks = [];
          pair.landmarkSet = {id: uniqueId(), revision: 0, points: []};
        }
      }
      compare.landmark = {active: true, edit: {
        editId: uniqueId(), editRevision: 0, baseCommittedRevision: compare.committedRevision,
        groupSessionId: compare.groupSessionId, groupEpoch: compare.groupEpoch,
        expectedTargets: [...groupSids()],
        contexts: new Map(groupSids().map((sid) => [sid, spatialEnvelope(sid)])),
        anchorSet: cloneValue(compare.anchorSet), pairs: before.pairs,
        candidates: new Map(), pointRevisions: new Map(), reflectionPolicy: "keep",
      }};
      for (const sid of compare.members) fitPair(sid);
      broadcastCompareState();
      for (const sid of groupSids()) sendLandmarkMode(sid, true);
      api.updateCompareControls();
    }

    function validLandmarkMessage(sid, data) {
      const edit = compare.landmark.edit;
      return compare.landmark.active && edit && inGroup(sid) && currentEnvelope(data, sid) &&
        data.editId === edit.editId && data.editRevision === edit.editRevision;
    }

    function landmarkCommitReady(edit = compare.landmark.edit) {
      if (!edit || !compare.landmark.active || !spatialGroupReady("edit") ||
          (compare.pendingRequest && compare.pendingRequest.kind !== "sync") ||
          edit.baseCommittedRevision !== compare.committedRevision ||
          edit.groupSessionId !== compare.groupSessionId || edit.groupEpoch !== compare.groupEpoch ||
          edit.expectedTargets.length !== groupSids().length ||
          !edit.expectedTargets.every((sid) => inGroup(sid) &&
            sameIdentity(edit.contexts.get(sid), localIdentity(compare.states.get(sid))))) return false;
      return compare.members.every((sid) => {
        const pair = edit.pairs.get(sid), candidate = edit.candidates.get(sid);
        return candidate?.status === "valid" && candidate.editRevision === edit.editRevision &&
          candidate.anchorRevision === edit.anchorSet.revision &&
          candidate.memberRevision === pair.landmarkSet.revision &&
          candidate.policy === edit.reflectionPolicy && candidate.renderer.supported;
      });
    }

    function commitLandmarkEdit(editId) {
      const edit = compare.landmark.edit;
      if (edit?.editId !== editId || !landmarkCommitReady(edit)) return false;
      // Validate and construct every member before the one synchronous publication.
      const nextPairs = new Map();
      for (const sid of compare.members) {
        const pair = cloneValue(edit.pairs.get(sid)), candidate = edit.candidates.get(sid);
        pair.fit = cloneValue(candidate.fit);
        pair.fitTransform = cloneValue(candidate.fit.transform);
        pair.transform = cloneValue(pair.fitTransform);
        pair.mode = candidate.mode;
        pair.manualOffset = {x: 0, y: 0};
        pair.provenanceMismatch = false;
        pair.provenance = {
          anchorContext: edit.contexts.get(compare.anchorSid).spatialContextKey,
          memberContext: edit.contexts.get(sid).spatialContextKey,
          anchorSetId: edit.anchorSet.id, anchorRevision: edit.anchorSet.revision,
          memberSetId: pair.landmarkSet.id, memberRevision: pair.landmarkSet.revision,
          anchorPointIds: edit.anchorSet.points.map((p) => p.id),
          memberPointIds: pair.landmarks.map((p) => p.id),
          reflectionPolicy: edit.reflectionPolicy,
          from: clonePoints(candidate.from), to: clonePoints(candidate.to),
        };
        updatePairResidual(pair);
        nextPairs.set(sid, pair);
      }
      const next = {
        anchorSet: cloneValue(edit.anchorSet), anchorLandmarks: clonePoints(edit.anchorSet.points),
        linked: compare.linked, linkMode: compare.linkMode, pairs: nextPairs,
      };
      clearPendingRequest();
      compare.committedGroup = next;
      restoreSnapshot(next);
      compare.committedRevision += 1;
      compare.landmark = {active: false, edit: null};
      compare.groupEpoch += 1; // invalidate already queued draft/display messages
      broadcastCompareState();
      for (const sid of groupSids()) sendLandmarkMode(sid, false);
      rememberAlignment();
      applyDisplayTransforms();
      api.syncFromAnchor();
      api.updateCompareControls();
      return true;
    }

    function finishLandmarks(keep) {
      if (!compare.landmark.active) return false;
      if (keep) return commitLandmarkEdit(compare.landmark.edit.editId);
      // Drafts never touched committed geometry. Invalidate callbacks BEFORE restoring display.
      invalidateSpatialWork();
      for (const sid of groupSids()) sendLandmarkMode(sid, false);
      applyDisplayTransforms();
      api.syncFromAnchor();
      api.updateCompareControls();
      return true;
    }

    function clearAlignment() {
      const edit = compare.landmark.edit;
      if (!edit || !spatialGroupReady("edit")) return;
      edit.editRevision += 1;
      edit.anchorSet = {id: uniqueId(), revision: edit.anchorSet.revision + 1, points: []};
      for (const pair of edit.pairs.values()) {
        pair.landmarks = [];
        pair.landmarkSet = {id: uniqueId(), revision: pair.landmarkSet.revision + 1, points: []};
      }
      edit.candidates.clear();
      for (const sid of groupSids()) sendLandmarkMode(sid, true, {clear: true});
      api.updateCompareControls();
    }

    function fitPair(sid) {
      const edit = compare.landmark.edit, pair = edit?.pairs.get(sid);
      const a = compare.states.get(compare.anchorSid), b = compare.states.get(sid);
      if (!edit || !pair || !a || !b) return false;
      edit.candidates.delete(sid); // never leave a last-successful fit marked current
      const mode = pair.forceRelative ? "normalized" : mappingMode(a, b);
      const from = edit.anchorSet.points.map((p) => pxToSpace(p, a, mode));
      const to = pair.landmarks.map((p) => pxToSpace(p, b, mode));
      const reflected = pair.fit ? pair.fit.reflected : Align.mirrored(pair.orientation);
      const candidate = Align.fitCandidate(from, to, {
        reflection: edit.reflectionPolicy, reflected, minPoints: LANDMARKS_NEEDED,
        sourceBounds: {width: a.imagePx.x * pixelScale(a, mode).x,
          height: a.imagePx.y * pixelScale(a, mode).y},
        targetBounds: {width: b.imagePx.x * pixelScale(b, mode).x,
          height: b.imagePx.y * pixelScale(b, mode).y},
      });
      const renderer = candidate.fit ? rendererForPair(sid, {mode, transform: candidate.fit.transform}) : {supported: false};
      if (candidate.status === "valid" && !renderer.supported) {
        candidate.status = "invalid";
        candidate.reason = "Physical mapping is not a renderer-supported pixel similarity";
      }
      edit.candidates.set(sid, {
        ...candidate, renderer, mode, from, to, editRevision: edit.editRevision,
        anchorRevision: edit.anchorSet.revision, memberRevision: pair.landmarkSet.revision,
        policy: edit.reflectionPolicy,
      });
      return candidate.status === "valid";
    }

    function receiveLandmarkPoints(sid, data) {
      if (!validLandmarkMessage(sid, data) || !spatialGroupReady("edit")) return;
      const edit = compare.landmark.edit;
      if (!Number.isSafeInteger(data.pointRevision) ||
          data.pointRevision <= (edit.pointRevisions.get(sid) ?? -1)) return;
      const raw = data.points;
      if (!Array.isArray(raw) || raw.length > LANDMARKS_NEEDED ||
          raw.some((p) => !finitePoint(p) || typeof p.id !== "string") ||
          new Set(raw.map((p) => p.id)).size !== raw.length) return;
      edit.pointRevisions.set(sid, data.pointRevision);
      const points = clonePoints(raw);
      if (sid === compare.anchorSid) {
        edit.anchorSet.points = points;
        edit.anchorSet.revision += 1;
      } else {
        const pair = edit.pairs.get(sid);
        pair.landmarks = points;
        pair.landmarkSet.points = clonePoints(points);
        pair.landmarkSet.revision += 1;
      }
      // Re-evaluate all members against the shared anchor revision, without moving any view.
      for (const member of compare.members) fitPair(member);
      api.updateCompareControls();
    }

    /* ---- controls --------------------------------------------------------------- */

    function formatDeltaUm(value) {
      const sign = value < 0 ? "−" : "+";
      const abs = Math.abs(value);
      if (abs >= 1000) return `${sign}${(abs / 1000).toFixed(2)} mm`;
      return `${sign}${abs.toFixed(abs >= 100 ? 0 : 1)} µm`;
    }

    function formatRms(pair, value = pair.fit?.rms) {
      if (!Number.isFinite(value)) return "—";
      if (pair.mode === "physical") return value >= 1000
        ? (value / 1000).toFixed(2) + " mm" : value.toFixed(value >= 100 ? 0 : 1) + " µm";
      return (value * 100).toFixed(2) + "%";
    }

    function alignmentDeltaLabel() {
      const sid = compare.orientationSid, pair = compare.pairs.get(sid);
      const a = compare.states.get(compare.anchorSid), b = compare.states.get(sid);
      if (!pair?.transform || !a?.imageReady || !b?.imageReady || !a.spatialContext || !b.spatialContext) return "";
      const base = pair.fitTransform || defaultTransform(pair, a, b, pair.mode);
      const dx = pair.transform.tx - base.tx, dy = pair.transform.ty - base.ty;
      if (pair.mode === "physical") return "Δ " + formatDeltaUm(dx) + ", " + formatDeltaUm(dy);
      return "Δ " + (dx * 100).toFixed(2) + "%, " + (dy * 100).toFixed(2) + "%";
    }

    function broadcastCompareState() {
      const ready = spatialGroupReady("edit") && !scaleLinkIssue(), pending = Boolean(compare.pendingRequest);
      for (const sid of frames.keys()) {
        const envelope = spatialEnvelope(sid);
        if (!envelope) continue;
        const member = inGroup(sid);
        postToSlide(sid, {
          nd2wsi: "compare-state", version: VIEWPORT_PROTOCOL_VERSION, ...envelope,
          enabled: member, spatialReady: member && ready,
          committedRevision: compare.committedRevision, nudgeBusy: Boolean(compare.pendingNudge),
          spatialEnabled: member && spatialGroupReady("edit"),
          linked: member && ready && compare.linked && !pending && !compare.landmark.active,
          linkMode: compare.linkMode,
          frameLinked: member && ready && compare.linked && compare.linkMode === "scale-frame" &&
            !pending && !compare.landmark.active,
          moving: member && sid !== compare.anchorSid,
          role: member && sid === compare.anchorSid ? "anchor" : "member",
        });
      }
    }

    function nudgeAlignment(dxPx, dyPx, fromSid) {
      if (compare.linkMode !== "scale-frame") return;
      if (compare.pendingRequest?.kind === "sync") clearPendingRequest(true);
      if (!compare.linked || compare.pendingRequest || compare.landmark.active || !spatialGroupReady()) return;
      const dx = Number(dxPx), dy = Number(dyPx);
      if (!Number.isFinite(dx) || !Number.isFinite(dy) || (!dx && !dy)) return;
      const target = fromSid && fromSid !== compare.anchorSid ? fromSid : compare.orientationSid;
      if (!compare.members.includes(target)) return;
      clearViewportRoutes();
      compare.nudgeQueue.push({
        sid: target, context: spatialEnvelope(target),
        dxPx: Math.max(-200, Math.min(200, dx)), dyPx: Math.max(-200, Math.min(200, dy)),
      });
      dispatchNextNudge();
    }

    function clearNudgeCommands() {
      if (compare.pendingNudge?.timer) clearTimeout(compare.pendingNudge.timer);
      compare.pendingNudge = null;
      compare.nudgeQueue = [];
    }

    function dispatchNextNudge() {
      if (compare.pendingNudge) return;
      const item = compare.nudgeQueue.shift();
      if (!item) return;
      if (!currentEnvelope(item.context, item.sid) || !compare.members.includes(item.sid) ||
          !compare.linked || compare.pendingRequest || compare.landmark.active || !spatialGroupReady()) {
        clearNudgeCommands();
        return;
      }
      const pending = {
        ...item, committedRevision: compare.committedRevision,
        commandSeq: compare.commandSeq + 1, commandId: "shell-spatial-" + (compare.commandSeq + 1),
      };
      compare.pendingNudge = pending;
      pending.timer = setTimeout(() => {
        if (compare.pendingNudge !== pending) return;
        clearNudgeCommands();
        api.syncFromAnchor();
        api.updateCompareControls();
      }, 2500);
      const sent = sendSpatial(item.sid, {
        nd2wsi: "viewport-nudge", dxPx: item.dxPx, dyPx: item.dyPx,
        committedRevision: pending.committedRevision,
      });
      if (!sent) clearNudgeCommands();
      api.updateCompareControls();
    }

    function applyNudgeDelta(sid, deltaPx) {
      const delta = finitePoint(deltaPx);
      if (!delta || !spatialGroupReady()) return false;
      if (sid === compare.anchorSid) {
        // Move the reference picture without moving its partners. Express the
        // opposite anchor-space displacement through each pair's linear map.
        for (const member of compare.members) {
          const pair = ensurePairTransform(member);
          const d = pxToSpace(delta, compare.states.get(sid), pair.mode);
          const tx = -(pair.transform.a * d.x + pair.transform.b * d.y);
          const ty = -(pair.transform.c * d.x + pair.transform.d * d.y);
          pair.transform.tx += tx;
          pair.transform.ty += ty;
          if (!pair.fitTransform) pair.manualOffset = {
            x: (pair.manualOffset?.x || 0) + tx, y: (pair.manualOffset?.y || 0) + ty,
          };
          updatePairResidual(pair);
        }
        return true;
      }
      if (!compare.members.includes(sid)) return false;
      const pair = ensurePairTransform(sid);
      const d = pxToSpace(delta, compare.states.get(sid), pair.mode);
      pair.transform.tx += d.x;
      pair.transform.ty += d.y;
      if (!pair.fitTransform) pair.manualOffset = {
        x: (pair.manualOffset?.x || 0) + d.x, y: (pair.manualOffset?.y || 0) + d.y,
      };
      updatePairResidual(pair);
      return true;
    }

    function receiveNudgeReply(data, snapshot, sid) {
      const pending = compare.pendingNudge;
      if (!pending || pending.sid !== sid || data.nudgeCommandId !== pending.commandId ||
          data.commandSeq !== pending.commandSeq || !currentEnvelope(data, sid) ||
          !sameIdentity(localIdentity(snapshot), pending.context)) return false;
      if (data.committedRevision !== pending.committedRevision ||
          compare.committedRevision !== pending.committedRevision || !snapshot.imageReady ||
          !spatialGroupReady() || !finitePoint(data.nudgeDeltaPx)) {
        clearNudgeCommands(); api.syncFromAnchor(); api.updateCompareControls();
        return false;
      }
      clearTimeout(pending.timer);
      compare.pendingNudge = null;
      if (snapshot.seq > (compare.states.get(sid)?.seq ?? -1)) compare.states.set(sid, snapshot);
      if (applyNudgeDelta(sid, data.nudgeDeltaPx)) committedMutation();
      forwardViewport(sid, compare.states.get(sid)); // the just-adjusted pane owns its current zoom
      dispatchNextNudge();
      api.updateCompareControls();
      return true;
    }

    function receiveDragNudge(data, snapshot, sid) {
      if (compare.linkMode !== "scale-frame") return false;
      if (data.nudgeSource !== "drag" || typeof data.dragId !== "string" ||
          !currentEnvelope(data, sid) || !inGroup(sid) || !snapshot.imageReady ||
          snapshot.seq <= (compare.states.get(sid)?.seq ?? -1)) return false;
      if (data.nudgeCancelled || data.committedRevision !== compare.committedRevision ||
          compare.pendingNudge || compare.pendingRequest || compare.landmark.active ||
          !compare.linked || !spatialGroupReady() || !finitePoint(data.nudgeDeltaPx)) {
        api.syncFromAnchor();
        return false;
      }
      clearViewportRoutes();
      compare.states.set(sid, snapshot);
      if (applyNudgeDelta(sid, data.nudgeDeltaPx)) committedMutation();
      forwardViewport(sid, snapshot);
      api.updateCompareControls();
      return true;
    }

    function orientationNote(pair) {
      if (pair.provenanceMismatch) return "Stored transform · Landmark set mismatch";
      if (pair.fit) return [
        pair.fit.pairs + " pts", "Fit RMS " + formatRms(pair),
        "Current RMS " + formatRms(pair, pair.currentRms),
        Math.round(pair.fit.angleDeg) + "°", pair.fit.reflected ? "mirror" : "",
      ].filter(Boolean).join(" · ");
      const pose = Align.displayPose(pair.orientation);
      const degrees = ((Math.round(pose.degrees) % 360) + 360) % 360;
      return [degrees ? degrees + "°" : "", pose.flipped ? "mirrored" : ""].filter(Boolean).join(" · ");
    }

    function orientationNeedsFocusedSite(sid) {
      return !compare.members.includes(sid) || !spatialGroupReady("orientation");
    }

    function updateOrientationControls() {
      if (!compare.members.includes(compare.orientationSid)) compare.orientationSid = compare.members[0] || null;
      const select = $("compare-orientation-target");
      const key = JSON.stringify(compare.members.map((sid) => [sid, slideName(sid)]));
      if (select.dataset.optionsKey !== key) {
        select.replaceChildren(...compare.members.map((sid) => {
          const option = document.createElement("option");
          option.value = sid; option.textContent = slideName(sid);
          return option;
        }));
        select.dataset.optionsKey = key;
      }
      select.value = compare.orientationSid || "";
      select.title = slideName(compare.orientationSid);
      select.disabled = Boolean(compare.pendingRequest || compare.pendingNudge) || compare.landmark.active;
      const pair = compare.pairs.get(compare.orientationSid);
      const paused = !spatialGroupReady("orientation");
      const disabled = select.disabled || !pair || Boolean(pair.fit) || paused;
      for (const id of Object.keys(ORIENTATION_ACTIONS)) $(id).disabled = disabled;
      $("compare-remove-fit").disabled = select.disabled || paused || !pair?.fit;
      $("compare-mapping").disabled = select.disabled || paused;
      $("compare-mapping").value = pair?.forceRelative ? "relative" : "physical";
      $("compare-orientation-state").textContent = pair ? orientationNote(pair) || "Original" : "";
      $("compare-orientation-hint").textContent = paused ? spatialPauseReason()
        : pair?.fit ? "Fit protected · Remove Fit retains manual orientation"
        : compare.landmark.active ? "Draft only · Done applies all fits together"
        : "Reference stays fixed · center preserved · display only";
    }

    function renderChips() {
      const chips = $("compare-chips");
      chips.replaceChildren();
      const group = groupSids();
      group.forEach((sid, index) => {
        const chip = document.createElement("div");
        chip.className = "compare-chip" + (index === 0 ? " anchor" : "");
        chip.dataset.sid = sid;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "compare-chip-btn";
        const pair = index === 0 ? null : compare.pairs.get(sid);
        const note = pair ? orientationNote(pair) : "";
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = slideName(sid);
        btn.append(name);
        if (note) {
          const meta = document.createElement("span");
          meta.className = "meta";
          // Full residuals belong in the active-slide inspector and tooltip;
          // never let them displace the filename in the compact group chip.
          meta.textContent = pair?.fit
            ? Math.round(pair.fit.angleDeg) + "°" + (pair.fit.reflected ? " · mirror" : "")
            : note;
          btn.append(meta);
        }
        btn.title = index === 0
          ? `${slideName(sid)} is the anchor. Every other slide follows it.`
          : `${slideName(sid)}${note ? " · " + note : ""}. Click to link a different slide in its place.`;
        btn.disabled = index === 0 || Boolean(compare.pendingRequest) || compare.landmark.active;
        btn.onclick = () => openPicker("replace", sid);
        chip.append(btn);
        if (index > 0) {
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "remove";
          remove.textContent = "×";
          remove.title = `Unlink ${slideName(sid)}`;
          remove.setAttribute("aria-label", `Unlink ${slideName(sid)}`);
          remove.disabled = Boolean(compare.landmark.active);
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
      const panel = $("compare-landmarks"), edit = compare.landmark.edit;
      panel.hidden = !compare.landmark.active;
      if (!compare.landmark.active || !edit) return;
      const rows = $("compare-landmark-rows");
      rows.replaceChildren();
      function line(label, text, valid) {
        const row = document.createElement("div");
        row.className = "landmark-row" + (valid ? " done" : "");
        const a = document.createElement("span"), b = document.createElement("span");
        a.className = "name"; a.textContent = label;
        b.className = "state"; b.textContent = text;
        row.append(a, b); rows.append(row);
      }
      line(slideName(compare.anchorSid), edit.anchorSet.points.length + " of " + LANDMARKS_NEEDED,
        edit.anchorSet.points.length === LANDMARKS_NEEDED);
      for (const sid of compare.members) {
        const candidate = edit.candidates.get(sid), pair = edit.pairs.get(sid);
        const valid = candidate?.status === "valid";
        const detail = valid
          ? "Candidate RMS " + formatRms({mode: candidate.mode}, candidate.fit.rms)
          : "Draft incomplete · Previous alignment shown · " + (candidate?.reason || "no points");
        line(slideName(sid), pair.landmarks.length + " of " + LANDMARKS_NEEDED + " · " + detail +
          (candidate?.warnings?.length ? " · " + candidate.warnings.join("; ") : ""), valid);
      }
      $("compare-landmark-done").disabled = !landmarkCommitReady();
      $("compare-mirror-policy").value = edit.reflectionPolicy;
      $("compare-landmark-hint").textContent = landmarkCommitReady()
        ? "Done applies every candidate atomically. Low RMS does not establish same-cell registration."
        : "Place the same four structures in order. Views keep the committed alignment until Done.";
    }

    function updateCompareControls() {
      // Materialize the edit panel before reserving its fixed canvas boundary.
      api.renderLandmarkPanel();
      syncCompareToolsVisibility();
      if (!compare.enabled) {
        broadcastCompareState();
        return;
      }
      const pending = Boolean(compare.pendingRequest || compare.pendingNudge);
      const landmarking = compare.landmark.active;
      const link = $("compare-link");
      link.classList.toggle("linked", compare.linked && !pending);
      link.classList.toggle("scale-only", compare.linked && compare.linkMode === "scale");
      link.classList.toggle("pending", pending);
      link.disabled = landmarking;
      link.setAttribute("aria-pressed", String(compare.linked && !pending));
      link.setAttribute("aria-label", "Choose linking: " +
        (!compare.linked ? "Off" : compare.linkMode === "scale" ? "Link scale" : "Link scale + frame"));
      link.title = "Choose Off, Link scale, or Link scale + frame (L)";
      updateLinkModeMenu();

      const members = compare.members.map((sid) => compare.pairs.get(sid)).filter(Boolean);
      api.updateOrientationControls();

      $("compare-swap").disabled = pending || landmarking || compare.members.length !== 1;
      const add = $("compare-add");
      const others = getSlides().filter((slide) => !inGroup(slide.sid));
      add.disabled = pending || landmarking || groupSids().length >= MAX_GROUP || !others.length;
      add.title = groupSids().length >= MAX_GROUP
        ? `Up to ${MAX_GROUP} slides can be linked`
        : others.length ? "Link another open slide" : "Open another slide to link it";
      const align = $("compare-align");
      align.disabled = Boolean(compare.pendingNudge) ||
        (compare.pendingRequest && compare.pendingRequest.kind !== "sync") || !spatialGroupReady("edit");
      align.classList.toggle("active", landmarking);
      align.setAttribute("aria-pressed", String(landmarking));

      let status;
      if (compare.pendingNudge) status = "Adjusting alignment…";
      else if (pending) status = "Reading every view…";
      else if (!compare.linked) status = "Unlinked · move any view";
      else if (scaleLinkIssue()) status = "Paused · " + scaleLinkIssue();
      else if (!members.length) status = "Waiting for views";
      else status = compare.linkMode === "scale" ? "Scale linked · µm" : "Scale + frame linked · µm";
      $("compare-status").textContent = status;

      const delta = compare.linked && compare.linkMode === "scale-frame" && !pending ? alignmentDeltaLabel() : "";
      const deltaEl = $("compare-delta");
      deltaEl.textContent = delta;
      deltaEl.hidden = !delta;
      deltaEl.title = ShortcutRouter.formatShortcutText(delta
        ? "Hand-tuned offset beyond matched centers. Arrow keys move it one screen pixel, Shift ten. Shift-drag or Option-drag adjusts one view."
        : "");

      api.renderChips();
      syncCompareToolbarSpace();
      broadcastCompareState();
    }

    /* ---- picker --------------------------------------------------------------- */

    function mostRecentOther(exclude) {
      const openSlides = getSlides();
      const open = new Set(openSlides.map((slide) => slide.sid));
      for (let i = compare.mru.length - 1; i >= 0; i -= 1) {
        if (!exclude.includes(compare.mru[i]) && open.has(compare.mru[i])) return compare.mru[i];
      }
      for (let i = openSlides.length - 1; i >= 0; i -= 1) {
        if (!exclude.includes(openSlides[i].sid)) return openSlides[i].sid;
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
      const openSlides = getSlides();
      const anchor = pairPicker.mode === "start" ? (getActive() || openSlides[openSlides.length - 1]?.sid) : compare.anchorSid;
      const taken = pairPicker.mode === "start" ? [anchor] : groupSids().filter((sid) => sid !== pairPicker.replaceSid);
      return { anchor, choices: openSlides.filter((slide) => !taken.includes(slide.sid)) };
    }

    function renderPairPicker() {
      const { anchor, choices } = pickerChoices();
      if (!pairPicker.open || !anchor || !choices.length ||
          (pairPicker.mode !== "start" && !compare.enabled)) {
        api.closePairPicker(false);
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
        api.closePairPicker();
        return;
      }
      pairPicker.open = true;
      pairPicker.mode = mode;
      pairPicker.replaceSid = replaceSid;
      $("compare-picker").hidden = false;
      $(mode === "start" ? "compare-toggle" : "compare-add").setAttribute("aria-expanded", "true");
      api.renderPairPicker();
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
      const openSlides = getSlides();
      const anchor = getActive() || openSlides[openSlides.length - 1]?.sid;
      if (!anchor || openSlides.length < 2) {
        showError("Open two slides before starting Compare");
        return;
      }
      openPicker("start");
    }

    function choosePickerSlide(sid) {
      const mode = pairPicker.mode;
      const replaceSid = pairPicker.replaceSid;
      const { anchor } = pickerChoices();
      api.closePairPicker();
      const openSids = new Set(getSlides().map((slide) => slide.sid));
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
      const pair = newPair();
      restoreAlignment(compare.anchorSid, sid, pair);
      compare.pairs.set(sid, pair);
      compare.members.push(sid);
      rememberSlide(sid);
      ensureFrame(sid);
    }

    function startGroup(anchorSid, memberSid) {
      if (compare.enabled || !anchorSid || !memberSid || anchorSid === memberSid) return;
      compare.groupSessionId = uniqueId();
      compare.groupEpoch += 1;
      compare.committedRevision = 0;
      compare.enabled = true; compare.toolsVisible = true;
      compare.anchorSid = anchorSid; compare.orientationSid = memberSid;
      compare.members = []; compare.pairs = new Map(); compare.anchorLandmarks = [];
      compare.anchorSet = {id: uniqueId(), revision: 0, points: []};
      compare.landmark = {active: false, edit: null};
      compare.linked = false; compare.linkMode = "scale"; setActive(anchorSid);
      rememberSlide(anchorSid); ensureFrame(anchorSid); attachMember(memberSid);
      applyFrameLayout();
      broadcastCompareState();
      applyDisplayTransforms();
      compare.committedGroup = snapshotAlignment();
      api.updateCompareControls(); render(); api.requestGroupSoon("sync");
      setLinkMenuVisible(true);
    }

    function addMember(sid) {
      if (!compare.enabled || inGroup(sid) || groupSids().length >= MAX_GROUP) return;
      invalidateSpatialWork();
      attachMember(sid);
      applyFrameLayout(); broadcastCompareState(); applyDisplayTransforms();
      committedMutation();
      api.updateCompareControls(); render(); api.requestGroupSoon("sync");
    }

    function removeMember(sid, andRender) {
      if (!compare.enabled || !compare.members.includes(sid)) return;
      rememberAlignment();
      invalidateSpatialWork();
      clearDisplayTransforms([sid]);
      compare.members = compare.members.filter((item) => item !== sid);
      compare.pairs.delete(sid);
      if (!compare.members.length) { stopCompare(); return; }
      committedMutation();
      applyFrameLayout(); api.updateCompareControls();
      if (andRender) render();
      api.requestGroupSoon("sync");
    }

    function replaceMember(oldSid, newSid) {
      if (!compare.enabled || !compare.members.includes(oldSid) || inGroup(newSid)) return;
      rememberAlignment(); invalidateSpatialWork(); clearDisplayTransforms([oldSid]);
      const index = compare.members.indexOf(oldSid);
      compare.pairs.delete(oldSid);
      const pair = newPair();
      restoreAlignment(compare.anchorSid, newSid, pair);
      compare.pairs.set(newSid, pair); compare.members.splice(index, 1, newSid);
      rememberSlide(newSid); ensureFrame(newSid);
      committedMutation();
      applyFrameLayout(); broadcastCompareState(); applyDisplayTransforms();
      api.updateCompareControls(); render(); api.requestGroupSoon("sync");
    }

    function toggleViewLink() {
      if (!compare.enabled || compare.landmark.active) return;
      setLinkMenuVisible($("compare-link-menu").hidden);
    }

    function updateLinkModeMenu() {
      const current = compare.linked ? compare.linkMode : "off";
      for (const mode of ["off", "scale", "scale-frame"]) {
        const button = $("compare-link-" + mode);
        const issue = mode === "off" ? "" : scaleLinkIssue(mode);
        button.disabled = Boolean(compare.landmark.active || (mode !== "off" &&
          (issue || compare.pendingRequest || compare.pendingNudge)));
        button.setAttribute("aria-checked", String(mode === current));
        button.title = issue;
      }
      $("compare-link-hint").textContent = scaleLinkIssue("scale") ||
        "Match physical magnification using each image’s pixel calibration.";
    }

    function setLinkMenuVisible(visible, focus = true) {
      const menu = $("compare-link-menu");
      menu.hidden = !visible || !compare.enabled;
      $("compare-link").setAttribute("aria-expanded", String(!menu.hidden));
      if (!menu.hidden) updateLinkModeMenu();
      syncCompareToolbarSpace();
      if (focus) (menu.hidden ? $("compare-link") : $("compare-link-off")).focus();
    }

    function setLinkMode(mode) {
      if (!compare.enabled || !["off", "scale", "scale-frame"].includes(mode) || compare.landmark.active) return;
      if (mode !== "off") {
        const issue = scaleLinkIssue(mode);
        if (issue || compare.pendingRequest || compare.pendingNudge) {
          if (issue) showError(issue);
          return;
        }
      }
      compare.linked = false;
      invalidateSpatialWork();
      // Closing this panel resizes the panes. Keep its deferred fresh-layout
      // snapshot instead of cancelling it with the previous mode's commands.
      setLinkMenuVisible(false);
      if (mode !== "off") {
        compare.linkMode = mode;
        api.requestGroup(mode === "scale" ? "scale-link" : "capture");
      }
      api.updateCompareControls();
    }

    function changeOrientation(action) {
      if (!compare.enabled || compare.pendingRequest || compare.landmark.active) return;
      const targetSid = compare.orientationSid;
      const pair = compare.pairs.get(targetSid);
      if (!compare.members.includes(targetSid) || !pair || pair.fit || orientationNeedsFocusedSite(targetSid) ||
          !Align.reorient(pair.orientation, action)) return;
      api.requestGroup("orientation", { targetSid, action });
    }

    function swapComparedSlides() {
      if (compare.members.length !== 1 || compare.landmark.active || compare.pendingRequest ||
          !spatialGroupReady()) return;
      rememberAlignment(); api.closePairPicker(false); invalidateSpatialWork();
      const oldAnchor = compare.anchorSid, oldMember = compare.members[0];
      const old = compare.pairs.get(oldMember);
      const pair = reversePair(old, compare.anchorSet);
      if (!pair) return;
      compare.anchorSet = cloneValue(old.landmarkSet);
      compare.anchorLandmarks = clonePoints(compare.anchorSet.points);
      compare.anchorSid = oldMember; compare.members = [oldAnchor]; compare.orientationSid = oldAnchor;
      compare.pairs = new Map([[oldAnchor, pair]]);
      setActive(compare.anchorSid);
      committedMutation();
      applyFrameLayout(); broadcastCompareState(); applyDisplayTransforms();
      api.updateCompareControls(); render(); api.requestGroupSoon("sync");
    }

    function wireCompareInteractions() {
      $("compare-picker").addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          api.closePairPicker();
          return;
        }
        if (event.key === "Tab") {
          api.closePairPicker();
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
        const linkMenu = $("compare-link-menu");
        if (linkMenu && !linkMenu.hidden && !linkMenu.contains(event.target) &&
            !$("compare-link").contains(event.target)) {
          setLinkMenuVisible(false, false);
        }
        if (!pairPickerIsOpen()) return;
        if ($("compare-picker").contains(event.target) || $("compare-toggle").contains(event.target) ||
            $("compare-add").contains(event.target) || $("compare-chips").contains(event.target)) return;
        api.closePairPicker(false);
      }, true);

      const compareDivider = $("compare-divider");
      let dividerPointer = null;
      compareDivider.addEventListener("pointerdown", (event) => {
        if ((event.button != null && event.button !== 0) || !compare.enabled || event.target.closest("button")) return;
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
        if (compare.linked) api.requestGroupSoon("sync");
      }
      compareDivider.addEventListener("pointerup", finishDividerDrag);
      compareDivider.addEventListener("pointercancel", finishDividerDrag);
      compareDivider.addEventListener("lostpointercapture", finishDividerDrag);
    }

    Object.assign(api, {
      syncCompareToolbarSpace, syncCompareToolsVisibility, setCompareToolsVisible, normalizeViewportState,
      uniqueId, cloneValue, spatialEnvelope, currentEnvelope,
      sendSpatial, spatialGroupReady, spatialPauseReason, physicalScaleFor,
      scaleLinkIssue, linkedScale, invalidateSpatialWork, committedMutation,
      adoptSpatialIdentity, mappingMode, pxToSpace, spaceToPx,
      imageCenterSpace, newPair, defaultTransform, ensurePairTransform,
      pixelScale, pixelTransform, rendererForPair, updatePairResidual,
      displayTransformFor, applyDisplayTransform, applyDisplayTransforms, clearDisplayTransforms,
      rematchTranslation, recaptureAll, pairKey, clonePoints,
      rememberAlignment, restoreAlignment, reversePair, anchorViewFromSource,
      targetViewFromAnchor, forwardViewport, scheduleViewportRoute, clearViewportRoutes,
      clearPendingRequest, syncFromAnchor, requestGroup, transactionCurrent,
      requestGroupSoon, finishGroupRequest, receiveViewportState, sendLandmarkMode,
      snapshotAlignment, restoreSnapshot, startLandmarks, validLandmarkMessage,
      landmarkCommitReady, commitLandmarkEdit, finishLandmarks, clearAlignment,
      fitPair, receiveLandmarkPoints, formatDeltaUm, formatRms,
      alignmentDeltaLabel, broadcastCompareState, nudgeAlignment, clearNudgeCommands,
      dispatchNextNudge, applyNudgeDelta, receiveNudgeReply, receiveDragNudge,
      orientationNote, orientationNeedsFocusedSite, updateOrientationControls, renderChips,
      renderLandmarkPanel, updateCompareControls, mostRecentOther, pairPickerIsOpen,
      closePairPicker, slideTypeLabel, slideSizeLabel, pickerChoices,
      renderPairPicker, openPicker, startCompare, choosePickerSlide,
      attachMember, startGroup, addMember, removeMember,
      replaceMember, toggleViewLink, updateLinkModeMenu, setLinkMenuVisible,
      setLinkMode, changeOrientation, swapComparedSlides, wireCompareInteractions,
    });
    return api;
  }

  return {
    ORIENTATION_ACTIONS: DEFAULT_ORIENTATION_ACTIONS,
    createCompare,
  };
});
