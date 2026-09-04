/* Explicit frame targeting and latest-request ownership, without a DOM. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2LatestRequest = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function plateFrame(plate, zFor, backing) {
    if (!plate || typeof plate !== "object") return null;
    const focus = plate.focus === null || plate.focus === undefined
      ? null
      : Number(plate.focus);
    if (focus === null && !backing) return null;
    const p = focus === null ? 0 : focus;
    const t = Number(plate.t);
    const z = Number(typeof zFor === "function" ? zFor(p) : plate.z);
    if (![t, p, z].every(Number.isInteger)) return null;
    return { t, p, z };
  }

  function appendFrameParams(params, frame) {
    if (!params || !frame) return params;
    params.set("t", String(frame.t));
    params.set("p", String(frame.p));
    params.set("z", String(frame.z));
    return params;
  }

  function frameOwnsSite(frame, site) {
    if (!frame || site === null || site === undefined || site === "") return false;
    return !!(
      frame.p !== null && frame.p !== undefined && frame.p !== "" &&
      Number.isInteger(Number(frame.p)) &&
      Number.isInteger(Number(site)) && Number(frame.p) === Number(site)
    );
  }

  function identityKey(sourceId, generation, frame) {
    const f = frame
      ? [Number(frame.t), Number(frame.p), Number(frame.z)]
      : null;
    return JSON.stringify([String(sourceId || ""), String(generation || ""), f]);
  }

  function responseIdentityMatches(payload, context) {
    if (!payload || !context || !("generation" in payload) || !("frame" in payload)) {
      return false;
    }
    return identityKey(context.sourceId, payload.generation, payload.frame)
      === context.key;
  }

  function isAbortError(error) {
    return !!error && (error.name === "AbortError" || error.code === 20);
  }

  class LatestRequestGate {
    constructor(AbortControllerClass) {
      this._AbortController = AbortControllerClass === undefined
        ? (typeof AbortController === "function" ? AbortController : null)
        : AbortControllerClass;
      this._seq = 0;
      this._active = null;
    }

    begin(key) {
      this.invalidate();
      const controller = this._AbortController ? new this._AbortController() : null;
      const ticket = {
        seq: ++this._seq,
        key: String(key),
        controller,
        signal: controller ? controller.signal : undefined,
      };
      this._active = ticket;
      return ticket;
    }

    invalidate() {
      const previous = this._active;
      this._active = null;
      this._seq += 1;
      if (previous && previous.controller) {
        try { previous.controller.abort(); } catch (_) { /* optional */ }
      }
    }

    isCurrent(ticket, key) {
      return !!(
        ticket && this._active === ticket && ticket.seq === this._seq &&
        (key === undefined || ticket.key === String(key))
      );
    }

    finish(ticket) {
      if (!this.isCurrent(ticket)) return false;
      this._active = null;
      return true;
    }
  }

  return {
    plateFrame,
    appendFrameParams,
    frameOwnsSite,
    identityKey,
    responseIdentityMatches,
    isAbortError,
    LatestRequestGate,
  };
});
