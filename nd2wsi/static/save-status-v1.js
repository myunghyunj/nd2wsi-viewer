/* Auto-save status for the viewer pane: one tracker merges the save events of
   the named persistence channels (annotation sidecars, LUT settings) into the
   state and tooltip text of the status-bar icon. Pure apart from the injected
   clock and interval timers; no DOM or viewer dependencies. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2SaveStatus = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // "idle" is a channel with nothing pending (an edit reverted to the saved
  // values); it keeps the time of its last successful save.
  const STATES = ["idle", "dirty", "saving", "saved", "failed", "conflict", "unavailable"];
  // most urgent first; an unavailable channel never takes part
  const PRECEDENCE = ["conflict", "failed", "saving", "dirty", "saved"];
  const TEXT = {
    conflict: "Not saved · conflict, see Annotations",
    saving: "Saving…",
    dirty: "Unsaved changes…",
  };

  function savedText(ageMs) {
    const seconds = Math.max(0, Math.floor(ageMs / 1000));
    if (seconds < 60) return "Auto-saved " + seconds + " s ago";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return "Auto-saved " + minutes + " min ago";
    return "Auto-saved " + Math.floor(minutes / 60) + " h ago";
  }

  function failedText(retryAt, nowMs, messages) {
    let text = "Not saved";
    if (retryAt !== null) {
      text += " · retrying in " + Math.max(0, Math.ceil((retryAt - nowMs) / 1000)) + " s";
    }
    return messages.length ? text + "\n" + messages.join("\n") : text;
  }

  function createTracker({
    now = () => Date.now(),
    setInterval: setTicker = (fn, ms) => setInterval(fn, ms),
    clearInterval: clearTicker = (id) => clearInterval(id),
    onRender = () => {},
    tickMs = 1000,
  } = {}) {
    const channels = new Map(); // name -> {state, at, message, savedAt}
    let retryAt = null, ticker = null, disposed = false, last = null;

    function channel(name) {
      let entry = channels.get(name);
      if (!entry) {
        entry = { state: "idle", at: null, message: null, savedAt: null };
        channels.set(name, entry);
      }
      return entry;
    }

    /* ---- events ------------------------------------------------------------ */

    function report(name, event = {}) {
      if (disposed) return;
      if (!STATES.includes(event.state)) throw new Error("unknown save state: " + event.state);
      const entry = channel(name);
      entry.state = event.state;
      entry.at = Number.isFinite(event.at) ? event.at : now();
      entry.message = typeof event.message === "string" && event.message ? event.message : null;
      if (event.state === "saved") entry.savedAt = entry.at;
      render();
    }

    // The periodic retry tells the tracker when it will next run, so the
    // failed text can count down to it.
    function scheduleRetry(delayMs) {
      retryAt = now() + Math.max(0, Number(delayMs) || 0);
      render();
      return retryAt;
    }

    function setRetryAt(value) {
      retryAt = Number.isFinite(value) ? value : null;
      render();
    }

    /* ---- description ------------------------------------------------------- */

    function describe(nowMs = now()) {
      let rank = PRECEDENCE.length, savedAt = null;
      const details = [], failures = [];
      for (const entry of channels.values()) {
        if (entry.state === "unavailable") {
          if (entry.message) details.push(entry.message);
          continue;
        }
        if (entry.savedAt !== null && (savedAt === null || entry.savedAt > savedAt)) savedAt = entry.savedAt;
        const effective = entry.state === "idle" || entry.state === "saved"
          ? (entry.savedAt === null ? null : "saved")
          : entry.state;
        if (!effective) continue;
        if (effective === "failed" && entry.message) failures.push(entry.message);
        // a pending edit that cannot be saved as it is (an invalid LUT) says why
        if (effective === "dirty" && entry.message) details.push(entry.message);
        rank = Math.min(rank, PRECEDENCE.indexOf(effective));
      }
      const state = rank < PRECEDENCE.length ? PRECEDENCE[rank] : "idle";
      const text = state === "idle" ? ""
        : state === "saved" ? savedText(nowMs - savedAt)
        : state === "failed" ? failedText(retryAt, nowMs, failures)
        : TEXT[state];
      return {
        state,
        text,
        visible: state !== "idle",
        details: details.length ? details.join("\n") : null,
        savedAt,
        retryAt,
      };
    }

    /* ---- ticker ------------------------------------------------------------ */

    function render() {
      if (disposed) return;
      const status = describe(now());
      if (last && last.state === status.state && last.text === status.text &&
          last.details === status.details) return;
      last = status;
      onRender(status);
    }

    function start() {
      if (disposed || ticker !== null) return;
      ticker = setTicker(render, tickMs);
      render();
    }

    function stop() {
      if (ticker !== null) clearTicker(ticker);
      ticker = null;
    }

    function dispose() {
      stop();
      disposed = true;
    }

    return {
      report,
      scheduleRetry,
      setRetryAt,
      describe,
      current: () => last || describe(now()),
      render,
      start,
      stop,
      dispose,
      isRunning: () => ticker !== null,
    };
  }

  return { createTracker, savedText, STATES };
});
