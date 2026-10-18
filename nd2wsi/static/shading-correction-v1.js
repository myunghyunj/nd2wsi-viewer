/* Persistent pyramid correction; application and revocation require confirmation. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2ShadingCorrection = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const working = new Set(["preparing", "fitting", "preparing_display", "rebuilding", "publishing"]);
  const terminal = new Set(["idle", "ready", "unchanged", "unsupported", "cancelled", "failed"]);

  function nextAction(status) {
    if (!status || status.state === "checking" || status.pending || status.refreshing || status.view_stale) return null;
    if (working.has(status.state)) return status.can_cancel === true ? "cancel" : null;
    if (!status.supported || (status.state === "unchanged" && !status.applied)) return null;
    return status.applied ? "revoke" : "start";
  }

  function describe(status) {
    if (!status) return { disabled: true, busy: false, label: "Open a slide to use Shading Correction" };
    const reason = status.reason ? ": " + status.reason : "";
    if (status.state === "checking") return { disabled: true, busy: true, label: "Checking Shading Correction availability" };
    if (status.refreshing) return { disabled: true, busy: true, label: "Loading the rebuilt pyramid" };
    if (status.view_stale) return { disabled: true, busy: false, label: "The pyramid changed; refreshing the view" + reason };
    if (status.pending === "cancel") return { disabled: true, busy: true, label: "Cancelling pyramid rebuild" };
    if (status.pending) return { disabled: true, busy: true, label: status.operation === "revoke"
      ? "Starting original pyramid rebuild" : "Starting Shading Correction" };
    if (working.has(status.state)) return {
      disabled: nextAction(status) === null, busy: true,
      label: (status.operation === "revoke" ? "Rebuilding original pyramid " : "Applying Shading Correction ") +
        Math.round(Number(status.pct) || 0) + "%" + (status.can_cancel ? " · Click to cancel" : " · Saving"),
    };
    if (status.state === "unchanged" && !status.applied) return {
      disabled: true, busy: false, label: "Shading Correction: image unchanged" + reason,
    };
    if (!status.supported) return { disabled: true, busy: false, label: "Shading Correction unavailable" + reason };
    if (status.state === "failed") return {
      disabled: false, busy: false,
      label: (status.operation === "revoke" ? "Revoke Shading Correction failed" : "Shading Correction failed") + reason + " · Click to retry",
    };
    if (status.state === "cancelled") return { disabled: false, busy: false,
      label: "Pyramid rebuild cancelled · " + (status.applied ? "Shading Correction remains applied" : "Click to retry") };
    return { disabled: false, busy: false, label: status.applied
      ? "Shading Correction applied · Revoke…" : "Apply Shading Correction" };
  }

  function progress(status) {
    const visible = !!status && (working.has(status.state) || !!status.pending || status.refreshing === true);
    const revoke = status?.operation === "revoke";
    const stage = status?.refreshing ? "Loading rebuilt pyramid" : status?.pending === "cancel" ? "Cancelling…" : {
      preparing: "Preparing image", fitting: "Estimating illumination",
      preparing_display: "Building corrected pyramid", rebuilding: "Rebuilding original pyramid",
      publishing: "Saving rebuilt pyramid",
    }[status?.state] || (revoke ? "Rebuilding original pyramid" : "Preparing image");
    let eta = "Estimating time…";
    const seconds = status?.eta_seconds;
    if (seconds !== null && seconds !== undefined && Number.isFinite(Number(seconds)) && Number(seconds) >= 0) {
      const rounded = Math.max(1, Math.ceil(Number(seconds) / 5) * 5);
      const duration = rounded < 60 ? rounded + " sec" : rounded < 3600
        ? Math.ceil(rounded / 60) + " min" : Math.ceil(rounded / 3600) + " hr";
      eta = Number(seconds) < 1 ? "Finishing…" : "About " + duration + " remaining";
    }
    return { visible, title: revoke ? "Revoke Shading Correction" : "Shading Correction", stage,
      canCancel: status?.can_cancel === true && !status?.pending && !status?.refreshing,
      pct: Math.round(Math.max(0, Math.min(100, Number(status?.pct) || 0))), eta };
  }

  function estimateLabel(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds)) || Number(seconds) < 0) {
      return "Est. time: unavailable until preparation starts";
    }
    return "Est. time: about " + Math.max(1, Math.ceil(Number(seconds) / 60)) + " min";
  }

  function createController(options) {
    const fetcher = options.fetch;
    const schedule = options.setTimer || setTimeout;
    const unschedule = options.clearTimer || clearTimeout;
    let context = null, status = initial(), initiatedOperation = null, initiatedGeneration = null, outcome = null;
    let sequence = 0, timer = null, abort = null, disposed = false, refreshing = false, refreshNeeded = false, lastError = null;

    function initial() {
      return { state: "checking", supported: false, applied: false, operation: null,
        reason: "", pct: 0, can_cancel: false, channels: [], generation: null };
    }
    function sameSource(a, b) {
      return !!a && !!b && a.sourceId === b.sourceId && JSON.stringify(a.frame) === JSON.stringify(b.frame);
    }
    function current(captured = context) {
      const now = options.readContext();
      return !disposed && !!now && !!captured && now.key === captured.key;
    }
    function snapshot() {
      return { ...status, channels: [...status.channels], applied: status.applied === true, refreshing,
        outcome: outcome ? { ...outcome } : null,
        view_stale: refreshNeeded || (!!status.generation && status.generation !== context?.generation),
        contextKey: context?.key || null, generation: status.generation || context?.generation || "" };
    }
    function emit() { options.onState?.(snapshot()); }
    function stopRequest() {
      sequence += 1;
      if (timer !== null) unschedule(timer);
      timer = null;
      abort?.abort();
      abort = null;
    }
    function schedulePoll() {
      if (disposed || timer !== null || !(working.has(status.state) || status.supported || status.applied)) return;
      const delay = working.has(status.state) ? 750 : 5000;
      timer = schedule(() => { timer = null; request("status"); }, delay);
    }
    function reportError(message) {
      if (message === lastError) return;
      lastError = message;
      options.onError?.("Shading Correction: " + message);
    }
    function fail(error) {
      initiatedOperation = null;
      initiatedGeneration = null;
      refreshing = false;
      status = { ...status, state: "failed", pending: null, can_cancel: false, reason: String(error.message || error) };
      emit();
      reportError(status.reason);
      schedulePoll();
    }
    async function accept(payload, captured, ticket) {
      if (!payload || typeof payload.generation !== "string" || !payload.generation) throw new Error("Invalid pyramid generation");
      if (!working.has(payload.state) && !terminal.has(payload.state)) throw new Error("Invalid preparation status");
      const initiatedHere = initiatedOperation;
      const generationHere = initiatedGeneration;
      const channels = Array.isArray(payload.channels) ? payload.channels.filter(value => typeof value === "string") : [];
      status = {
        ...payload, pending: null, channels, supported: payload.supported === true,
        applied: payload.applied === true, can_cancel: payload.can_cancel === true,
        operation: ["apply", "revoke"].includes(payload.operation) ? payload.operation : null,
        reason: typeof payload.reason === "string" ? payload.reason : "",
        pct: Math.max(0, Math.min(100, Number(payload.pct) || 0)),
      };
      if (payload.generation !== context.generation || refreshNeeded) {
        refreshNeeded = true;
        refreshing = true;
        emit();
        if (typeof options.onGeneration !== "function") throw new Error("The rebuilt pyramid could not be loaded");
        await options.onGeneration(payload.generation, captured);
        const now = options.readContext();
        if (disposed || ticket !== sequence || !sameSource(captured, now)) return false;
        if (now.generation !== payload.generation) throw new Error("The rebuilt pyramid could not be loaded");
        context = now;
        refreshing = false;
        refreshNeeded = false;
      }
      if (disposed || ticket !== sequence) return false;
      if (outcome && (status.applied || status.generation !== outcome.generation || working.has(status.state))) outcome = null;
      if (initiatedHere === "apply" && !status.applied && status.generation === generationHere &&
          ["unchanged", "unsupported", "failed"].includes(status.state)) {
        outcome = {
          title: "Shading Correction was not applied",
          reason: status.reason || "A usable correction could not be verified for this scan.",
          note: "The original pyramid is unchanged.",
          generation: status.generation,
        };
      }
      if (!working.has(status.state)) {
        initiatedOperation = null;
        initiatedGeneration = null;
      }
      emit();
      if (status.state === "failed") {
        const reason = status.reason || "Pyramid rebuild failed";
        if (outcome) lastError = reason;
        else reportError(reason);
      } else lastError = null;
      schedulePoll();
      return true;
    }
    async function request(action) {
      if (disposed || !current()) return false;
      stopRequest();
      const captured = context, ticket = sequence;
      abort = typeof AbortController === "function" ? new AbortController() : null;
      const settings = { cache: "no-store", signal: abort?.signal };
      if (action !== "status") Object.assign(settings, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ generation: captured.generation }),
      });
      try {
        const response = await fetcher("api/shading/" + action, settings);
        let payload;
        try { payload = await response.json(); } catch (_) { payload = null; }
        if (ticket !== sequence || !current(captured)) return false;
        if (!response.ok && response.status === 409 && action !== "status") {
          // Another window may have committed since the confirmation opened.
          // Refresh the committed state without replaying or changing the action.
          initiatedOperation = null;
          initiatedGeneration = null;
          await request("status");
          return false;
        }
        if (!response.ok) throw new Error(payload?.error || payload?.reason || "Request failed (HTTP " + response.status + ")");
        return await accept(payload, captured, ticket);
      } catch (error) {
        if (ticket !== sequence || disposed || !sameSource(captured, options.readContext()) || error?.name === "AbortError") return false;
        // Metadata may already be current when reopening its tiles fails.
        // Keep polling that context, but block mutations until display retry succeeds.
        if (refreshing) context = options.readContext();
        fail(error);
        return false;
      } finally {
        if (ticket === sequence) abort = null;
      }
    }
    function load() {
      if (disposed) return Promise.resolve(false);
      stopRequest();
      context = options.readContext();
      initiatedOperation = null;
      initiatedGeneration = null;
      outcome = null;
      refreshing = false;
      refreshNeeded = false;
      lastError = null;
      status = context ? initial() : {
        ...initial(), state: "unsupported", reason: "Choose a slide or site first",
      };
      emit();
      return context ? request("status") : Promise.resolve(false);
    }
    function syncContext() {
      const now = options.readContext();
      if (refreshing && sameSource(context, now)) return;
      if ((now?.key || null) !== (context?.key || null)) load();
      else emit();
    }
    function activate(action, expectedKey, confirmed = false) {
      if (disposed || !current() || (expectedKey && expectedKey !== context.key) || options.isBlocked?.()) return Promise.resolve(false);
      if (action !== nextAction(snapshot())) return Promise.resolve(false);
      if (action === "cancel") {
        initiatedOperation = null;
        initiatedGeneration = null;
        status = { ...status, pending: "cancel" };
      } else {
        if (!confirmed) return Promise.resolve(false);
        initiatedOperation = action === "revoke" ? "revoke" : "apply";
        initiatedGeneration = context.generation;
        outcome = null;
        status = { ...status, state: action === "revoke" ? "rebuilding" : "preparing",
          operation: initiatedOperation, pending: action, pct: 0, reason: "", can_cancel: false };
      }
      emit();
      return request(action);
    }
    function dismissOutcome(expectedKey) {
      if (disposed || !current() || expectedKey !== context.key || !outcome) return false;
      outcome = null;
      emit();
      return true;
    }
    function dispose() { stopRequest(); disposed = true; }
    return { load, syncContext, activate, dismissOutcome, snapshot, dispose };
  }

  return { createController, nextAction, describe, progress, estimateLabel };
});
