/* Frame-owned histogram and pixel requests; no DOM or viewer dependencies. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2FrameData = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function createHistogramController({
    state, readContext, matchesResponse, appendFrameParams, isAbortError,
    fetch, onHistograms, onClear, onStatus = () => {},
    setTimer = setTimeout, clearTimer = clearTimeout,
  }) {
    const retryDelays = [500, 1500, 4000];
    let revision = 0, disposed = false;
    const ownsContext = (context, requestRevision) => !disposed &&
      revision === requestRevision && readContext()?.key === context.key;

    function requestHistograms(context, attempt, requestRevision) {
      if (!ownsContext(context, requestRevision)) return;
      const q = appendFrameParams(new URLSearchParams(), context.frame).toString();
      const ticket = state.requests.begin(context.key);
      return fetch("api/histogram" + (q ? "?" + q : ""), {
        cache: "no-store",
        signal: ticket.signal,
      })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
        .then((data) => {
          if (
            !state.requests.isCurrent(ticket, context.key) ||
            !ownsContext(context, requestRevision)
          ) return;
          if (!matchesResponse(data, context) || !Array.isArray(data.channels)) {
            throw new Error("Histogram response did not match the current frame");
          }
          onHistograms(data.channels);
          onStatus("ready");
        })
        .catch((error) => {
          if (
            !state.requests.isCurrent(ticket, context.key) ||
            !ownsContext(context, requestRevision) ||
            isAbortError(error)
          ) return;
          onClear(false); // manual LUT controls remain usable
          if (attempt < retryDelays.length) {
            onStatus("retrying");
            state.timer = setTimer(() => {
              state.timer = null;
              requestHistograms(context, attempt + 1, requestRevision);
            }, retryDelays[attempt]);
          } else {
            onStatus("failed"); // the UI offers an explicit retry after backoff
          }
        })
        .finally(() => state.requests.finish(ticket));
    }

    function scheduleHistograms(delay) {
      if (disposed) return;
      const requestRevision = ++revision;
      clearTimer(state.timer);
      state.timer = null;
      state.requests.invalidate();
      const context = readContext();
      onClear(!context);
      if (!context) return;
      onStatus("loading");
      const run = () => {
        state.timer = null;
        requestHistograms(context, 0, requestRevision);
      };
      if (delay > 0) state.timer = setTimer(run, delay);
      else run();
    }

    function dispose() {
      disposed = true;
      revision += 1;
      clearTimer(state.timer);
      state.timer = null;
      state.requests.invalidate();
    }

    return { schedule: scheduleHistograms, dispose };
  }

  function createPixelProbeController({
    state, readContext, matchesResponse, appendFrameParams, isAbortError,
    fetch, onRender, onClearCursor, now = () => Date.now(),
    setTimer = setTimeout, clearTimer = clearTimeout,
  }) {
    function queuePixelProbe(x, y) {
      const context = readContext();
      if (!context) {
        invalidatePixelProbe(false);
        return;
      }
      state.queued = { x, y, context };
      pumpPixelProbe();
    }

    function invalidatePixelProbe(requeue = true) {
      clearTimer(state.timer);
      state.timer = null;
      state.requests.invalidate();
      state.inFlight = null;
      state.queued = null;
      state.result = null;
      state.resultKey = null;
      state.failed = false;
      state.retryAfter = 0;
      const context = readContext();
      if (!context) {
        state.cursor = null;
        onClearCursor();
      }
      onRender();
      if (requeue && context && state.cursor) {
        queuePixelProbe(state.cursor.x, state.cursor.y);
      }
    }

    function pumpPixelProbe() {
      if (state.inFlight || state.timer || !state.queued) return;
      const time = now();
      const wait = Math.max(
        0,
        100 - (time - state.lastStarted),
        state.retryAfter - time
      );
      state.timer = setTimer(() => {
        state.timer = null;
        if (!state.queued) return;
        const requested = state.queued;
        state.queued = null;
        const current = readContext();
        if (!current || current.key !== requested.context.key) return;
        const ticket = state.requests.begin(requested.context.key);
        state.inFlight = ticket;
        state.lastStarted = now();
        const query = appendFrameParams(
          new URLSearchParams({ x: requested.x, y: requested.y }),
          requested.context.frame
        );
        fetch("api/pixel?" + query.toString(), { cache: "no-store", signal: ticket.signal })
          .then((r) => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
          .then((data) => {
            const cursor = state.cursor;
            if (
              !state.requests.isCurrent(ticket, requested.context.key) ||
              !matchesResponse(data, requested.context) ||
              !cursor || cursor.x !== requested.x || cursor.y !== requested.y
            ) return;
            state.result = data;
            state.resultKey = requested.context.key;
            state.failed = false;
            state.retryAfter = 0;
            onRender();
          })
          .catch((error) => {
            const cursor = state.cursor;
            if (
              !state.requests.isCurrent(ticket, requested.context.key) ||
              isAbortError(error) ||
              !cursor || cursor.x !== requested.x || cursor.y !== requested.y
            ) return;
            state.failed = true;
            state.retryAfter = now() + 2500;
            onRender();
          })
          .finally(() => {
            state.requests.finish(ticket);
            if (state.inFlight === ticket) state.inFlight = null;
            if (state.queued) pumpPixelProbe();
          });
      }, wait);
    }

    return { queue: queuePixelProbe, invalidate: invalidatePixelProbe };
  }

  return { createHistogramController, createPixelProbeController };
});
