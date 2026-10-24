/* Per-image display LUTs only. Source pixels and other viewer state are untouched. */
(function (root, factory) {
  if (typeof module === "object" && module.exports)
    module.exports = factory(require("./lut-transfer-v1.js"));
  else root.Nd2LutPersistence = factory(root.Nd2LutTransfer);
})(typeof globalThis !== "undefined" ? globalThis : this, function (defaultTransfer) {
  "use strict";

  const FORMAT = "nd2wsi-lut-settings/1";
  const MAX_CHANNELS = 512;
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  // The Fetch spec lets a page keep at most 64 KiB of keepalive request bodies
  // in flight; a larger keepalive request rejects at once. The same limit as
  // annotation-store-v1.js, with headroom for both requests of one pagehide;
  // a LUT body (at most 512 channels) never reaches it, the rule just stays in
  // one shape.
  const KEEPALIVE_BODY_LIMIT = 60 * 1024;
  const keepaliveFits = body => new TextEncoder().encode(body).length <= KEEPALIVE_BODY_LIMIT;

  function createController({ info, readLuts, fetch, reportError = () => {},
    onSaveState = () => {}, now = () => Date.now(),
    setTimeout: setTimer = setTimeout, clearTimeout: clearTimer = clearTimeout,
    transfer = defaultTransfer, endpoint = "api/lut-settings" } = {}) {
    let status = "unloaded", disposed = false;
    let timer = null, loading = null, saving = null, loadError = null;
    let revision = 0, savedRevision = 0;
    let pendingBody = null, pendingError = null, lastSavedBody = null, lastWarning = null;
    const dirty = () => revision !== savedRevision;
    const copy = luts => luts.map(lut => ({ ...lut }));

    function warn(message) {
      if (disposed || message === lastWarning) return;
      lastWarning = message;
      try { reportError(message); } catch (_) { /* A notification cannot break persistence. */ }
    }

    // Structured transitions for the pane's auto-save indicator; "idle" means
    // nothing is pending any more (an edit returned to the saved values).
    function emit(saveState, message) {
      if (disposed) return;
      const event = { channel: "lut", state: saveState, at: now() };
      if (message) event.message = String(message);
      try { onSaveState(event); } catch (_) { /* An indicator cannot break persistence. */ }
    }

    function metadata() {
      if (!transfer || typeof transfer.currentLut !== "function" ||
          typeof transfer.fitsChannel !== "function" || !record(info) ||
          !Array.isArray(info.channels) || !info.channels.length ||
          info.channels.length > MAX_CHANNELS ||
          typeof info.dtype !== "string" || !info.dtype.trim())
        throw new Error("LUT channel metadata is unavailable or invalid.");
      for (const channel of info.channels) {
        if (!record(channel) || !record(channel.window) ||
            !transfer.fitsChannel(channel, transfer.currentLut(channel, null), info.dtype))
          throw new Error("LUT channel defaults are invalid.");
      }
      return info.channels;
    }

    function checkedLuts(values, allowDefaults) {
      const channels = metadata();
      if (!Array.isArray(values) || values.length !== channels.length)
        throw new Error("Saved LUT settings do not match this image's channels.");
      const result = [];
      for (let index = 0; index < channels.length; index++) {
        const value = values[index];
        if (!(allowDefaults && value == null) && (!record(value) ||
            Object.keys(value).length !== 3 ||
            !["lo", "hi", "gamma"].every(key => Object.hasOwn(value, key))))
          throw new Error("LUT settings must contain only minimum, maximum and gamma.");
        const lut = transfer.currentLut(channels[index], value);
        if (!transfer.fitsChannel(channels[index], lut, info.dtype))
          throw new Error("LUT values are invalid for this image's pixel type.");
        result.push(lut);
      }
      return result;
    }

    const encode = luts => JSON.stringify({ format: FORMAT, luts });
    function cancelTimer() {
      if (timer !== null) clearTimer(timer);
      timer = null;
    }

    function schedule() {
      cancelTimer();
      if (disposed || status === "unavailable" || !dirty() || pendingError) return;
      timer = setTimer(() => {
        timer = null;
        flush().catch(() => {}); // flush reports the failure and retains dirty state.
      }, 250);
    }

    async function responseJson(response) {
      if (!response || response.ok !== true || typeof response.json !== "function")
        throw new Error("The LUT settings request failed.");
      const payload = await response.json();
      if (!record(payload) || payload.ok !== true)
        throw new Error("The LUT settings request was not accepted.");
      return payload;
    }

    async function load() {
      if (disposed || status === "unavailable" || status === "ready") return null;
      if (loading) return loading.then(value => value && copy(value));
      loading = (async () => {
        // Assign the shared promise before even a synchronous fetch failure can
        // run finally; a later edit must be able to retry that failed read.
        await Promise.resolve();
        try {
          const payload = await responseJson(await fetch(endpoint, { cache: "no-store" }));
          if (disposed) return null;
          if (payload.available === false) {
            status = "unavailable";
            savedRevision = revision;
            cancelTimer();
            if (typeof payload.warning === "string" && payload.warning) warn(payload.warning);
            emit("unavailable", typeof payload.warning === "string" && payload.warning
              ? payload.warning : "LUT settings are not saved for this source.");
            return null;
          }
          metadata();
          if (payload.available !== true || !Object.hasOwn(payload, "settings"))
            throw new Error("The saved LUT settings response is invalid.");
          let restored = null;
          if (payload.settings !== null) {
            const settings = payload.settings;
            if (!record(settings) || settings.format !== FORMAT ||
                Object.keys(settings).length !== 2 || !Object.hasOwn(settings, "luts"))
              throw new Error("The saved LUT settings format is invalid.");
            restored = checkedLuts(settings.luts, false);
          }
          status = "ready";
          loadError = null;
          lastWarning = null;
          lastSavedBody = restored === null ? null : encode(restored);
          if (typeof payload.warning === "string" && payload.warning) warn(payload.warning);
          // A retry after editing must establish safe access, not restore stale
          // values over the user's new changes. Only initial load returns values.
          return revision === 0 ? restored : null;
        } catch (error) {
          if (disposed) return null;
          status = "failed";
          loadError = new Error("Saved LUT settings could not be read. " + error.message);
          warn(loadError.message + " No saved values were applied; saving is blocked until reading succeeds.");
          return null;
        } finally {
          loading = null;
        }
      })();
      return loading.then(value => value && copy(value));
    }

    function changed() {
      if (disposed || status === "unavailable") return;
      let body;
      try {
        body = encode(checkedLuts(readLuts(), true));
      } catch (error) {
        revision++;
        pendingBody = null;
        pendingError = error;
        cancelTimer();
        warn("LUT changes are not saved. " + error.message);
        // unsaved, but not a transient failure: nothing retries it until the
        // next valid edit, so the indicator shows it as pending, not failed
        emit("dirty", error.message);
        return;
      }
      if (body === pendingBody && !pendingError) {
        schedule();
        emit(dirty() ? "dirty" : "idle");
        return;
      }
      revision++;
      pendingBody = body;
      pendingError = null;
      if (!saving && body === lastSavedBody) savedRevision = revision;
      schedule();
      emit(dirty() ? "dirty" : "idle");
    }

    async function saveLatest({ keepalive = false } = {}) {
      let posted = false;
      try {
        if (status !== "ready") await load();
        if (status === "unavailable") return;
        if (status !== "ready") throw loadError || new Error("LUT settings are not available for saving.");
        while (dirty() && !disposed) {
          if (pendingError) throw pendingError;
          const body = pendingBody, writingRevision = revision;
          if (body !== lastSavedBody) {
            emit("saving");
            const init = { method: "POST", cache: "no-store",
              headers: { "Content-Type": "application/json" }, body };
            // pagehide: the browser keeps the request alive after the page is
            // gone, for a body within the keepalive budget
            if (keepalive && keepaliveFits(body)) init.keepalive = true;
            await responseJson(await fetch(endpoint, init));
            lastSavedBody = body;
            posted = true;
          }
          savedRevision = writingRevision;
          lastWarning = null;
          if (posted) emit("saved");
          // A completed older request never clears a newer dirty snapshot.
        }
        if (dirty()) throw new Error("LUT changes remain unsaved after the controller was disposed.");
        if (!posted) emit("idle"); // the pending edit matched the saved values
      } catch (error) {
        warn("LUT changes are not saved. " + error.message);
        emit(error === pendingError ? "dirty" : "failed", error.message);
        throw error;
      }
    }

    function flush(options = {}) {
      cancelTimer();
      if (!dirty() || status === "unavailable") return Promise.resolve();
      if (disposed) return Promise.reject(new Error("LUT changes remain unsaved after the controller was disposed."));
      if (!saving) {
        saving = saveLatest(options).then(value => { saving = null; return value; },
          error => { saving = null; throw error; });
      }
      return saving.then(() => dirty() ? flush(options) : undefined);
    }

    function dispose() {
      disposed = true;
      cancelTimer();
    }

    // Read-only views for the pane's periodic retry and pagehide flush: a
    // validation failure (pendingError) is not retried, only a new edit is.
    const isDirty = () => !disposed && status !== "unavailable" && dirty() && !pendingError;

    return { load, changed, flush, dispose, isDirty, status: () => status };
  }

  return { createController, FORMAT, KEEPALIVE_BODY_LIMIT };
});
