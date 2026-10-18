/* Source-bound preparation; each pane independently chooses RAW or corrected. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2ShadingCorrection = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const working = new Set(["preparing", "fitting", "preparing_display"]);
  const terminal = new Set(["idle", "ready", "unchanged", "unsupported", "cancelled", "failed"]);

  function describe(status) {
    if (!status) return { disabled: true, busy: false, label: "Open a slide to use Shading Correction" };
    const reason = status.reason ? ": " + status.reason : "";
    if (status.state === "checking") return { disabled: true, busy: true, label: "Checking Shading Correction availability" };
    if (status.pending === "cancel") return { disabled: true, busy: true, label: "Cancelling Shading Correction" };
    if (status.pending === "start") return { disabled: true, busy: true, label: "Starting Shading Correction" };
    if (working.has(status.state)) return {
      disabled: false, busy: true,
      label: "Preparing Shading Correction " + Math.round(Number(status.pct) || 0) + "% · Click to cancel",
    };
    if (status.state === "ready") return {
      disabled: false, busy: false,
      label: "Shading Correction " + (status.enabled ? "on · Show original" : "off · Show corrected") +
        (status.channels?.length ? " · " + status.channels.join(", ") : ""),
    };
    if (status.state === "unchanged") return { disabled: true, busy: false, label: "Shading Correction: image unchanged" + reason };
    if (!status.supported && status.state !== "failed") return {
      disabled: true, busy: false, label: "Shading Correction unavailable" + reason,
    };
    if (status.state === "failed") return { disabled: false, busy: false, label: "Shading Correction failed" + reason + " · Click to retry" };
    if (status.state === "cancelled") return { disabled: false, busy: false, label: "Shading Correction cancelled · Click to retry" };
    return { disabled: false, busy: false, label: "Apply Shading Correction" };
  }

  function progress(status) {
    const visible = !!status && (working.has(status.state) || !!status.pending);
    const stage = status?.pending === "cancel" ? "Cancelling…" : {
      preparing: "Preparing image", fitting: "Estimating illumination",
      preparing_display: "Preparing corrected view",
    }[status?.state] || "Preparing image";
    let eta = "Estimating time…";
    const seconds = status?.eta_seconds;
    if (seconds !== null && seconds !== undefined && Number.isFinite(Number(seconds)) && Number(seconds) >= 0) {
      const rounded = Math.max(1, Math.ceil(Number(seconds) / 5) * 5);
      const duration = rounded < 60 ? rounded + " sec" : rounded < 3600
        ? Math.ceil(rounded / 60) + " min" : Math.ceil(rounded / 3600) + " hr";
      eta = Number(seconds) < 1 ? "Finishing…" : "About " + duration + " remaining";
    }
    return { visible, stage, pct: Math.round(Math.max(0, Math.min(100, Number(status?.pct) || 0))), eta };
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
    let context = null, status = initial(), enabled = false, autoEnable = false;
    let sequence = 0, timer = null, abort = null, disposed = false;

    function initial() {
      return { state: "checking", supported: false, reason: "", pct: 0, correction_id: null, channels: [] };
    }
    function current(captured = context) {
      const now = options.readContext();
      return !disposed && !!now && !!captured && now.key === captured.key;
    }
    function snapshot() {
      return { ...status, channels: [...status.channels], enabled: enabled && current(),
        contextKey: context?.key || null, generation: context?.generation || "" };
    }
    function emit() { options.onState?.(snapshot()); }
    function setEnabled(value) {
      const next = Boolean(value);
      if (enabled === next) return;
      enabled = next;
      options.onChange?.(enabled ? status.correction_id : null);
    }
    function stopRequest() {
      sequence += 1;
      if (timer !== null) unschedule(timer);
      timer = null;
      abort?.abort();
      abort = null;
    }
    function schedulePoll() {
      if (disposed || timer !== null || !working.has(status.state)) return;
      timer = schedule(() => { timer = null; request("status"); }, 750);
    }
    function fail(error) {
      autoEnable = false;
      setEnabled(false);
      status = { ...status, state: "failed", pending: null, reason: String(error.message || error), correction_id: null };
      emit();
      options.onError?.("Shading Correction: " + status.reason);
    }
    function accept(payload) {
      if (!payload || payload.generation !== context.generation) {
        throw new Error("The slide changed. Reopen the slide before preparing correction.");
      }
      if (!working.has(payload.state) && !terminal.has(payload.state)) throw new Error("Invalid preparation status");
      const initiatedHere = autoEnable;
      const channels = Array.isArray(payload.channels) ? payload.channels.filter(value => typeof value === "string") : [];
      status = {
        ...payload, pending: null, channels, supported: payload.supported === true,
        reason: typeof payload.reason === "string" ? payload.reason : "",
        pct: Math.max(0, Math.min(100, Number(payload.pct) || 0)),
        correction_id: typeof payload.correction_id === "string" ? payload.correction_id : null,
      };
      if (status.state === "ready" && (!channels.length || !status.correction_id)) {
        status = { ...status, state: "unchanged", correction_id: null };
      }
      if (status.state === "ready" && autoEnable) setEnabled(true);
      else if (status.state !== "ready") setEnabled(false);
      if (!working.has(status.state)) autoEnable = false;
      emit();
      if (status.state === "failed") options.onError?.("Shading Correction: " + (status.reason || "Preparation failed"));
      if (initiatedHere && ["unchanged", "unsupported"].includes(status.state)) {
        const outcome = status.state === "unchanged" ? "image unchanged" : "unavailable for this image";
        options.onNotice?.("Shading Correction: " + outcome + (status.reason ? ". " + status.reason : "."));
      }
      schedulePoll();
    }
    async function request(action) {
      if (disposed || !current()) return false;
      stopRequest();
      const captured = context, ticket = sequence;
      abort = typeof AbortController === "function" ? new AbortController() : null;
      const settings = { cache: "no-store", signal: abort?.signal };
      if (action !== "status") Object.assign(settings, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      try {
        const response = await fetcher("api/shading/" + action, settings);
        let payload;
        try { payload = await response.json(); } catch (_) { payload = null; }
        if (ticket !== sequence || !current(captured)) return false;
        if (!response.ok) throw new Error(payload?.error || payload?.reason || "Request failed (HTTP " + response.status + ")");
        accept(payload);
        return true;
      } catch (error) {
        if (ticket !== sequence || !current(captured) || error?.name === "AbortError") return false;
        fail(error);
        return false;
      } finally {
        if (ticket === sequence) abort = null;
      }
    }
    function load() {
      if (disposed) return Promise.resolve(false);
      stopRequest();
      setEnabled(false);
      context = options.readContext();
      autoEnable = false;
      status = context ? initial() : {
        ...initial(), state: "unsupported", reason: "Choose a slide or site first",
      };
      emit();
      return context ? request("status") : Promise.resolve(false);
    }
    function syncContext() {
      const now = options.readContext();
      if ((now?.key || null) !== (context?.key || null)) load();
      else emit();
    }
    function activate(expectedKey, confirmedStart = false) {
      if (disposed || !current() || (expectedKey && expectedKey !== context.key) || options.isBlocked?.()) return Promise.resolve(false);
      if (describe(snapshot()).disabled) return Promise.resolve(false);
      if (status.state === "ready") {
        setEnabled(!enabled);
        emit();
        return Promise.resolve(true);
      }
      if (working.has(status.state)) {
        autoEnable = false;
        status = { ...status, pending: "cancel" };
        emit();
        return request("cancel");
      }
      if (!confirmedStart) return Promise.resolve(false);
      autoEnable = true;
      status = { ...status, state: "preparing", pending: "start", pct: 0, reason: "" };
      emit();
      return request("start");
    }
    function appendRenderParams(params) {
      if (enabled && current() && status.state === "ready" && status.correction_id) {
        params.set("correction", status.correction_id);
      }
      return params;
    }
    function dispose() { stopRequest(); disposed = true; }
    return { load, syncContext, activate, snapshot, appendRenderParams, dispose };
  }

  return { createController, describe, progress, estimateLabel };
});
