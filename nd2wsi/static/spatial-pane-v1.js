/* Pane-owned lifetime boundary for asynchronous Compare commands. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2SpatialPane = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function spatialContext(frameContext, isPlate) {
    if (!frameContext) return null;
    const site = isPlate ? frameContext.frame?.p : null;
    if (isPlate && !Number.isInteger(site)) return null;
    const sourceId = frameContext.sourceId;
    const sourceGeneration = String(frameContext.generation || "");
    const kind = isPlate ? "plate" : "slide";
    return {
      key: JSON.stringify([sourceId, sourceGeneration, kind, site]),
      kind, sourceId, sourceGeneration, site,
    };
  }

  class PaneCommandGate {
    constructor(paneInstanceId) {
      this.paneInstanceId = paneInstanceId;
      this.contextEpoch = 0;
      this.spatialContext = null;
      this.contextInitialized = false;
      this.groupSessionId = null;
      this.groupEpoch = -1;
      this.enabled = false;
      this.spatialEnabled = false;
      this.retiredSessions = new Set();
      this.latestCommandSeq = -1;
      this.pendingViewportCommand = null;
    }

    setContext(context) {
      const key = context?.key ?? null;
      if (this.contextInitialized && key === (this.spatialContext?.key ?? null)) return false;
      this.contextInitialized = true;
      this.contextEpoch += 1;
      this.spatialContext = context;
      this.pendingViewportCommand = null;
      this.latestCommandSeq = -1;
      return true;
    }

    envelope() {
      return {
        groupSessionId: this.groupSessionId,
        groupEpoch: this.groupEpoch,
        paneInstanceId: this.paneInstanceId,
        contextEpoch: this.contextEpoch,
        spatialContextKey: this.spatialContext?.key ?? null,
      };
    }

    matchesLocal(message) {
      return message?.paneInstanceId === this.paneInstanceId &&
        message.contextEpoch === this.contextEpoch &&
        message.spatialContextKey === (this.spatialContext?.key ?? null);
    }

    bind(message) {
      if (!this.matchesLocal(message) || typeof message.groupSessionId !== "string" ||
          !message.groupSessionId || !Number.isSafeInteger(message.groupEpoch) ||
          message.groupEpoch < 0 || this.retiredSessions.has(message.groupSessionId)) return false;
      const sameSession = message.groupSessionId === this.groupSessionId;
      if (sameSession && message.groupEpoch < this.groupEpoch) return false;
      if (!sameSession || message.groupEpoch !== this.groupEpoch || !message.enabled) {
        this.pendingViewportCommand = null;
        this.latestCommandSeq = -1;
      }
      if (!sameSession && this.groupSessionId) this.retiredSessions.add(this.groupSessionId);
      this.groupSessionId = message.groupSessionId;
      this.groupEpoch = message.groupEpoch;
      this.enabled = Boolean(message.enabled);
      this.spatialEnabled = message.spatialEnabled === true;
      if (!this.spatialEnabled) this.pendingViewportCommand = null;
      return true;
    }

    valid(message) {
      return this.enabled && this.spatialEnabled && Boolean(this.spatialContext) && this.matchesLocal(message) &&
        message.groupSessionId === this.groupSessionId && message.groupEpoch === this.groupEpoch;
    }

    receive(message) {
      if (!this.valid(message) || !Number.isSafeInteger(message.commandSeq) ||
          message.commandSeq <= this.latestCommandSeq) return false;
      this.latestCommandSeq = message.commandSeq;
      this.pendingViewportCommand = message;
      return true;
    }

    current(message) {
      return this.valid(message) && Number.isSafeInteger(message.commandSeq) &&
        message.commandSeq === this.latestCommandSeq;
    }

    takeReady(imageReady) {
      const message = this.pendingViewportCommand;
      if (!imageReady || !message) return null;
      this.pendingViewportCommand = null;
      return this.current(message) ? message : null;
    }
  }

  return { spatialContext, PaneCommandGate };
});
