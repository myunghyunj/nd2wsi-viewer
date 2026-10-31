/* Annotation sidecar persistence: the debounced save, one serialized save
   queue per pane, revision/context guards, failed-save recovery drafts and
   plate-site owner transitions; no DOM or viewer dependencies. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2AnnotationStore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function basename(p) {
    return p ? String(p).split("/").pop() : "";
  }

  // The Fetch spec lets a page keep at most 64 KiB of keepalive request bodies
  // in flight; a larger keepalive request rejects at once, which would record
  // a bogus failed entry. Stay under it with headroom for the LUT request the
  // same pagehide sends (lut-persistence-v1.js keeps the same limit); a bigger
  // snapshot goes out as a plain request, whose failed entry, if any, waits
  // for the next edit or minute retry.
  const KEEPALIVE_BODY_LIMIT = 60 * 1024;

  function keepaliveFits(body) {
    return new TextEncoder().encode(body).length <= KEEPALIVE_BODY_LIMIT;
  }

  // Items arrive from files other tools wrote and from sidecars on disk.
  // Every item the pane renders needs a known type, finite coordinates and a
  // unique id. A bad color, text or id is repaired on a copy (sound items are
  // returned as they are). Opening a file refuses the whole set otherwise, so
  // a failed open changes nothing; a sidecar keeps such items aside instead
  // (splitItems), because an older version may have saved them there.
  const HEX_COLOR = /^#[0-9a-f]{6}$/i;
  const COORDINATES = { pin: ["x", "y"], box: ["x", "y", "w", "h"], line: ["x1", "y1", "x2", "y2"] };
  const GEOMETRY_DEPTH = { Point: 0, MultiPoint: 1, LineString: 1, MultiLineString: 2, Polygon: 2, MultiPolygon: 3 };

  function finitePoints(values, depth) {
    if (!Array.isArray(values)) return false;
    if (depth === 0) return values.length >= 2 && Number.isFinite(values[0]) && Number.isFinite(values[1]);
    return values.length > 0 && values.every((value) => finitePoints(value, depth - 1));
  }

  function itemProblem(item) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return "not an annotation object";
    if (item.type === "geojson") {
      const geometry = item.geojson && item.geojson.geometry;
      const depth = geometry ? GEOMETRY_DEPTH[geometry.type] : undefined;
      return depth !== undefined && finitePoints(geometry.coordinates, depth) ? null : "invalid geometry";
    }
    const keys = Object.prototype.hasOwnProperty.call(COORDINATES, item.type) ? COORDINATES[item.type] : null;
    if (!keys) return "unknown type " + String(item.type);
    return keys.every((key) => Number.isFinite(item[key])) ? null : "coordinates must be finite numbers";
  }

  function normalizeItems(items, makeId, reserved = []) {
    if (!Array.isArray(items)) throw new Error("no items array");
    const used = new Set(reserved);
    return items.map((item, index) => {
      const problem = itemProblem(item);
      if (problem) throw new Error("annotation " + (index + 1) + ": " + problem);
      const badColor = "color" in item && !(typeof item.color === "string" && HEX_COLOR.test(item.color));
      const badText = item.text != null && typeof item.text !== "string";
      const badId = typeof item.id !== "string" || !item.id || used.has(item.id);
      let result = item;
      if (badColor || badText || badId) {
        result = { ...item };
        if (badColor) delete result.color;
        if (badText) result.text = String(item.text);
        if (badId) do { result.id = makeId(); } while (used.has(result.id));
      }
      used.add(result.id);
      return result;
    });
  }

  // A sidecar's items the pane cannot draw are not shown or edited, and are
  // written back unchanged with the drawable ones on every save. A sidecar
  // without an items array is refused, never read as an empty one.
  function splitItems(items, makeId) {
    if (!Array.isArray(items)) throw new Error("no items array");
    const drawable = [], foreign = [];
    for (const item of items) (itemProblem(item) ? foreign : drawable).push(item);
    const reserved = foreign.map((item) => item && item.id).filter((id) => typeof id === "string");
    return { items: normalizeItems(drawable, makeId, reserved), foreign };
  }

  // The store keeps its fields on the pane state it is given, so the pane's
  // existing readers (annotation owner checks, file import, the Astromap
  // panel) keep reading state.ann* as before; only the store writes them:
  //   state.annotations            the items; the pane edits this array itself
  //   state.editingId              cleared when another sidecar is loaded
  //   state.annSite, annReady, annLoadFailed, annPath, annDirty, annRevision, annContext
  //   state.annForeignItems        sidecar items the pane cannot draw, kept for saving
  //   state.annServerRevision, annServerRevisions, annAcceptedDrafts
  //   state.annSaveTail, annFailedSaves, annLoadSeq
  // It reads state.plate, state.info (plate.P, notes) and state.quitPreparing.
  //
  // Invariants (see docs/architecture.md):
  // - saves for one pane are serialized on state.annSaveTail in the order
  //   they were captured; the tail always resolves so one failed request
  //   cannot suppress a newer snapshot
  // - a load waits for the tail, so A -> B -> A cannot GET A before the
  //   captured A save reached disk; an older load or finalizer cannot
  //   install over a newer one (annLoadSeq, annContext, annRevision guards)
  // - a 409 conflict is never overwritten or merged: the snapshot stays in
  //   annFailedSaves, is shown again when the user returns to that site, and
  //   only an explicit acceptRecoveryDrafts() lets the pane close with it
  function createStore({
    state,
    fetch,
    isLocked = () => false,
    activeFrame = () => null,
    onStatus = () => {},
    onRender = () => {},
    onScope = () => {},
    onEditorReset = () => {},
    onRecoveryNotice = () => {},
    onSaveState = () => {},
    now = () => Date.now(),
    setTimer = setTimeout,
    clearTimer = clearTimeout,
    debounceMs = 800,
  }) {
    if (!(state.annFailedSaves instanceof Map)) state.annFailedSaves = new Map();
    if (!state.annSaveTail) state.annSaveTail = Promise.resolve();
    for (const field of ["annRevision", "annContext", "annLoadSeq"]) {
      if (typeof state[field] !== "number") state[field] = 0;
    }

    /* ---- debounced save ---------------------------------------------------- */

    // the timer argument is deliberately not forwarded: save() with no site
    // saves the current owner
    let timer = null;
    // the snapshot last handed to the queue, so the minute retry can tell a
    // save that is still in flight (or was queued meanwhile) from one it owes
    let queuedRevision = null, queuedContext = null;

    // Nothing is marked dirty, scheduled or sent for an owner whose sidecar
    // failed to load or was refused (normalizeItems): the pane's list is then
    // empty, and saving it would replace the sidecar on disk.
    function scheduleSave() {
      if (state.annLoadFailed) return;
      clearTimer(timer);
      timer = setTimer(() => { timer = null; save(); }, debounceMs);
    }

    function cancelScheduledSave() {
      clearTimer(timer);
      timer = null;
    }

    function markDirty() {
      if (state.annLoadFailed) return;
      state.annRevision += 1;
      state.annDirty = true;
      state.annAcceptedDrafts?.clear();
      emitSaveState("dirty");
    }

    /* ---- save events ------------------------------------------------------- */

    // Structured transitions for the pane's auto-save indicator. The onStatus
    // strings stay the Annotations panel's text; this adds, never replaces.
    function emitSaveState(saveState, extra) {
      try {
        onSaveState({ channel: "annotations", state: saveState, at: now(), ...extra });
      } catch (_) { /* an indicator failure cannot break persistence */ }
    }

    // The channel after a request settled: an older site's conflict or failed
    // snapshot still outranks a clean reply for the current one.
    function settledSaveState() {
      const entries = failedSaves();
      if (entries.some((entry) => entry.conflict)) return "conflict";
      if (entries.length) return "failed";
      return state.annDirty ? "dirty" : "saved";
    }

    function emitSettledSaveState(message) {
      const settled = settledSaveState();
      const detail = message !== undefined ? message
        : failedSaves().find((entry) => entry.conflict === (settled === "conflict"))?.error;
      emitSaveState(settled, {
        conflict: settled === "conflict",
        ...(detail ? { message: detail } : {}),
      });
    }

    /* ---- site routing ------------------------------------------------------ */

    function normalizeAnnotationSite(site) {
      if (
        site === null || site === undefined ||
        (typeof site === "string" && site.trim() === "")
      ) return null;
      const p = Number(site);
      if (!Number.isInteger(p) || p < 0 || p >= state.info.plate.P) return null;
      return p;
    }

    function urlFor(site) {
      // a plate keeps one sidecar per site, shared across time and z; the
      // coordinates inside are level-0 pixels of the frame, as for any slide
      if (!state.plate) return "api/annotations";
      const p = normalizeAnnotationSite(site === undefined ? state.annSite : site);
      if (p === null) return null;
      return "api/annotations?p=" + p;
    }

    function saveEntry(site) {
      const url = urlFor(site);
      if (!url) return null;
      const foreign = state.annForeignItems || [];
      const payload = { items: foreign.length ? state.annotations.concat(foreign) : state.annotations,
        ...(state.annLaserState ? { slacs: state.annLaserState } : {}) };
      if (typeof state.annServerRevision === "string") {
        payload.expected_revision = state.annServerRevision;
      }
      return {
        url,
        body: JSON.stringify(payload),
        revision: state.annRevision,
        context: state.annContext,
      };
    }

    /* ---- recovery drafts --------------------------------------------------- */

    function failedSaves() {
      return [...state.annFailedSaves.values()];
    }

    // A 409 whose recovery draft already holds this exact body: sending the
    // body again would only make the server write another draft. The minute
    // retry and the pagehide flush skip the current snapshot on this one rule
    // (the quit flush adds the user's explicit acceptance, see draftAccepted);
    // a newer edit changes the body and goes out as usual.
    function draftHolds(entry, body) {
      return !!(entry?.conflict && entry.draftPath && entry.draftBody === body);
    }

    function recoveryDrafts() {
      const entries = failedSaves();
      if (!entries.length || entries.some((entry) => !draftHolds(entry, entry.body))) return null;
      if (state.annDirty) {
        const current = saveEntry();
        if (!current || !entries.some((entry) => entry.url === current.url &&
            entry.draftBody === current.body)) return null;
      }
      return entries;
    }

    function draftAccepted(entry) {
      return draftHolds(entry, entry.body) && state.annAcceptedDrafts?.get(entry.url) === entry.draftBody;
    }

    function acceptRecoveryDrafts() {
      if (state.quitPreparing) return false;
      const entries = recoveryDrafts();
      if (!entries) {
        onRecoveryNotice();
        return false;
      }
      state.annAcceptedDrafts = new Map(entries.map((entry) => [entry.url, entry.draftBody]));
      onRecoveryNotice();
      return true;
    }

    /* ---- save queue -------------------------------------------------------- */

    function queueSave(entry, { keepalive = false } = {}) {
      if (!entry || !entry.url) {
        return Promise.resolve({ ok: false, error: "choose a plate site first" });
      }
      queuedRevision = entry.revision;
      queuedContext = entry.context;
      const run = state.annSaveTail.then(async () => {
        emitSaveState("saving");
        try {
          // A newer local snapshot may have queued behind our own successful
          // save. Advance only from this window's committed revision, never a
          // revision reported by a conflicting writer.
          const payload = JSON.parse(entry.body);
          const revisions = state.annServerRevisions || (state.annServerRevisions = new Map());
          if (typeof payload.expected_revision === "string" && revisions.has(entry.url)) {
            payload.expected_revision = revisions.get(entry.url);
          }
          entry.body = JSON.stringify(payload);
          // A previous draft cannot authorize a later failed or changed request.
          entry.conflict = false;
          entry.draftPath = null;
          entry.draftBody = null;
          entry.error = null;
          const init = {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: entry.body,
          };
          // pagehide: the browser keeps the request alive after the page is
          // gone, for a body within the keepalive budget
          if (keepalive && keepaliveFits(entry.body)) init.keepalive = true;
          const response = await fetch(entry.url, init);
          if (response.status === 409) {
            let conflict = {};
            try { conflict = await response.json(); } catch (_) { /* keep local draft */ }
            entry.conflict = true;
            entry.draftPath = typeof conflict.draft_path === "string" && !conflict.draft_error
              ? conflict.draft_path : null;
            entry.draftBody = entry.draftPath ? entry.body : null;
            throw new Error("Conflict: another window changed this sidecar. Your edits remain unsaved; no overwrite or merge." +
              (entry.draftPath ? " Recovery draft: " + entry.draftPath : " Keep this window open and export your annotations."));
          }
          if (!response.ok) throw new Error("HTTP " + response.status);
          const data = await response.json();
          if (typeof data.revision === "string") {
            revisions.set(entry.url, data.revision);
            if (state.annContext === entry.context) state.annServerRevision = data.revision;
          }
          const failed = state.annFailedSaves.get(entry.url);
          if (!failed || failed.revision <= entry.revision) {
            state.annFailedSaves.delete(entry.url);
          }
          if (state.annContext === entry.context && state.annRevision === entry.revision) {
            state.annDirty = false;
          }
          if (state.annContext === entry.context) {
            onStatus("Saved · " + basename(data.path));
          }
          onRecoveryNotice();
          emitSaveState("saved");
          if (settledSaveState() !== "saved") emitSettledSaveState();
          return { ok: true };
        } catch (error) {
          entry.error = error.message;
          const failed = state.annFailedSaves.get(entry.url);
          if (!failed || failed.revision <= entry.revision) {
            state.annFailedSaves.set(entry.url, entry);
          }
          if (state.annContext === entry.context) {
            state.annDirty = true;
            onStatus("Save failed: " + error.message);
          }
          onRecoveryNotice();
          emitSettledSaveState(error.message);
          return { ok: false, error: error.message };
        }
      });
      // Always resolve the tail so one failed request cannot suppress a newer
      // captured snapshot.  Callers inspect the individual result instead.
      state.annSaveTail = run.then(() => undefined);
      return run;
    }

    function save(site, options = {}) {
      if (isLocked()) {
        onStatus("Not saved: annotation is locked in this degraded view");
        return Promise.resolve({ ok: false, error: "annotation is locked" });
      }
      if (state.annLoadFailed) {
        onStatus("Not saved: this image's annotations did not load");
        return Promise.resolve({ ok: false, error: "annotations did not load" });
      }
      const entry = saveEntry(site);
      if (!entry) {
        onStatus("Not saved: choose a plate site first");
        return Promise.resolve({ ok: false, error: "choose a plate site first" });
      }
      return queueSave(entry, options);
    }

    // The once-a-minute safety net: re-queue transient failures (any site)
    // and a dirty snapshot whose debounce is not pending. A 409 whose recovery
    // draft already holds the snapshot is never retried here; only the user's
    // explicit recovery-draft acceptance resolves it (a 409 that got no draft
    // is sent again, so the server can try the draft once more). Locked panes
    // and a missing plate site are skipped like save(), silently: this path
    // owns no status text. An owner whose sidecar did not load has no
    // snapshot to send; other owners' failed requests are still retried.
    async function retryFailed(options = {}) {
      if (isLocked()) return { ok: false, error: "annotation is locked", retried: 0 };
      // Once the debounce fired, the dirty flag stays set until the request
      // succeeds: wait for the tail, as flushForUpdate does, so a slow request
      // is not queued a second time. A save queued meanwhile, behind the tail
      // this waited for, shows as the current snapshot already being queued.
      await state.annSaveTail;
      const runs = [], superseded = new Set();
      const queued = state.annRevision === queuedRevision && state.annContext === queuedContext;
      if (!state.annLoadFailed && state.annDirty && timer === null && !queued) {
        const current = saveEntry();
        if (current && !draftHolds(state.annFailedSaves.get(current.url), current.body)) {
          // one request per sidecar: the current snapshot replaces an older
          // failed one for the same site
          superseded.add(current.url);
          runs.push(queueSave(current, options));
        }
      }
      for (const entry of failedSaves()) {
        if (draftHolds(entry, entry.body) || superseded.has(entry.url)) continue;
        runs.push(queueSave(entry, options));
      }
      const results = await Promise.all(runs);
      return { ok: results.every((result) => result.ok), retried: results.length };
    }

    // Browser mode's tab close or reload (app.js, pagehide): hand the pending
    // snapshot to the browser with keepalive. Nothing is sent when the site's
    // recovery draft already holds this body, and the debounce is called off
    // only when a save is issued. Locked panes and a missing site are skipped
    // silently, as in retryFailed(), and so is an owner whose sidecar did not
    // load.
    function flushOnPagehide({ keepalive = true } = {}) {
      if (isLocked()) return Promise.resolve({ ok: false, error: "annotation is locked", sent: false });
      const current = !state.annLoadFailed && (timer !== null || state.annDirty) ? saveEntry() : null;
      if (!current || draftHolds(state.annFailedSaves.get(current.url), current.body)) {
        return Promise.resolve({ ok: true, sent: false });
      }
      cancelScheduledSave();
      return queueSave(current, { keepalive }).then((result) => ({ ...result, sent: true }));
    }

    // Save-before-update/close: every dirty snapshot of every site must reach
    // disk, or be an explicitly accepted recovery draft, before this resolves.
    // The caller marks the pane as preparing to quit before calling this.
    async function flushForUpdate() {
      cancelScheduledSave();
      await state.annSaveTail;

      if (state.annDirty) {
        const current = saveEntry();
        const failed = current && state.annFailedSaves.get(current.url);
        if (!failed || failed.body !== current.body || !draftAccepted(failed)) {
          await save();
        }
      }
      await state.annSaveTail;

      // Capture every older plate site's failed snapshot too. Do not short-circuit
      // at the first conflict, leaving another site's unsaved marks unchecked.
      for (const entry of failedSaves()) {
        if (!draftHolds(entry, entry.body)) await queueSave(entry);
      }
      await state.annSaveTail;
      if (state.annDirty || state.annFailedSaves.size) {
        const drafts = recoveryDrafts();
        if (!drafts || !drafts.every(draftAccepted)) {
          onRecoveryNotice();
          throw new Error(drafts
            ? "Annotations conflict. Review the recovery notice and choose Keep recovery drafts for closing, then close again."
            : "Some annotations are not safely preserved. Keep this window open and retry saving or export your annotations.");
        }
      }
    }

    /* ---- load and owner transitions ---------------------------------------- */

    function load(site) {
      // switching sites twice in a row leaves two loads in flight; the older
      // one must not install its items over the newer site's
      const seq = ++state.annLoadSeq;
      const frame = activeFrame();
      const targetSite = state.plate
        ? normalizeAnnotationSite(site === undefined ? (frame ? frame.p : null) : site)
        : null;
      const context = state.annContext;
      const revision = state.annRevision;
      const url = urlFor(targetSite);
      state.annSite = Number.isInteger(targetSite) ? targetSite : null;
      state.annReady = false;
      state.annLoadFailed = false;
      state.annPath = null;
      state.annServerRevision = null;
      state.annotations = [];
      state.annForeignItems = [];
      state.annLaserState = null;
      state.editingId = null;
      onEditorReset();
      onRender();
      onScope();
      if (!url) {
        onStatus(state.plate ? "Choose a site to view its annotations" : "");
        return Promise.resolve();
      }
      const stillCurrent = () => {
        const currentFrame = activeFrame();
        return !!(
          seq === state.annLoadSeq && context === state.annContext &&
          (!state.plate || (currentFrame && currentFrame.p === targetSite && state.annSite === targetSite))
        );
      };
      // A fast A -> B -> A transition must not GET A before the captured A save
      // reaches disk, or the UI would reinstall the older sidecar contents.
      return state.annSaveTail.then(() => {
        if (!stillCurrent()) return null;
        const failed = state.annFailedSaves.get(url);
        if (failed) {
          // Never replace a failed in-memory snapshot with the older sidecar on
          // disk when the user returns to this site. The next edit, site change,
          // or update preparation will retry this exact recovered snapshot.
          const captured = JSON.parse(failed.body);
          if (captured && Array.isArray(captured.items)) {
            return { data: captured, recovered: true, conflict: !!failed.conflict, draftPath: failed.draftPath };
          }
        }
        return fetch(url).then((r) => r.ok
          ? r.json().then((data) => ({ data, recovered: false }))
          : Promise.reject(new Error("HTTP " + r.status)));
      })
        .then((result) => {
          if (result === null || !stillCurrent() || revision !== state.annRevision) return;
          const { data: d, recovered, conflict, draftPath } = result;
          let loadedIds = 0;
          // A recovered draft is this pane's own snapshot: its drawable items
          // come first and its kept-aside ones last, so splitting it again
          // leaves the saved body exactly as it was.
          const { items, foreign } = splitItems(d && d.items,
            () => "a" + now().toString(36) + "s" + loadedIds++);
          state.annotations = items;
          state.annForeignItems = foreign;
          state.annLaserState = d.slacs || null;
          state.annPath = recovered ? null : d.path;
          state.annReady = true;
          state.annDirty = recovered;
          const rev = recovered ? d.expected_revision : d.revision;
          state.annServerRevision = typeof rev === "string" ? rev : null;
          const revisions = state.annServerRevisions || (state.annServerRevisions = new Map());
          if (!recovered && typeof rev === "string") revisions.set(url, rev);
          // a skipped legacy-sidecar import must not be silent: the server
          // leaves a note explaining why old annotations are not shown here
          const skipped = ((state.info && state.info.notes) || []).find((n) =>
            n.includes("annotation sidecar")
          );
          onStatus(
            conflict
              ? "Conflict: unsaved annotations recovered; no overwrite or merge." + (draftPath ? " Recovery draft: " + draftPath : " Export these annotations before closing.")
              : recovered
              ? "Recovered unsaved annotations · save will retry"
              : skipped
              ? skipped
              : (state.annotations.length
                ? "Loaded " + state.annotations.length + " from " + basename(d.path)
                : "Sidecar: " + basename(d.path)) +
                (foreign.length ? " · " + foreign.length + " item(s) this viewer cannot draw are kept unchanged" : "")
          );
          onRecoveryNotice();
          onRender();
          onScope();
        })
        .catch(() => {
          if (!stillCurrent()) return;
          // The list was emptied when this load started: an edit or debounce
          // left from while it loaded would save that over the sidecar.
          cancelScheduledSave();
          state.annDirty = false;
          state.annReady = false;
          state.annLoadFailed = true;
          onStatus("Could not load annotations for this site");
          onScope();
        });
    }

    // Leaving a plate site: the debounced save is called off, because it
    // would fire after the switch and write these marks into the next site's
    // sidecar, and the departing site's marks are captured now instead.
    // Returns false when dirty marks have no known owner; the caller must
    // then abandon the switch.
    function saveDeparting() {
      const departingSite = state.annSite;
      cancelScheduledSave();
      if (state.annDirty) {
        if (!Number.isInteger(departingSite)) {
          onStatus("Not saved: annotation owner is unknown");
          return false;
        }
        save(departingSite);
      }
      return true;
    }

    // A new context: replies to the departing site's saves can no longer
    // touch the dirty flag, status or server revision of the pane.
    function resetOwner() {
      state.annContext += 1;
      state.annDirty = false;
      state.annReady = false;
      state.annSite = null;
    }

    // The whole transition in one call: save departing, reset, load next.
    // The pane's focus change performs the same three steps explicitly, with
    // its own tool and selection teardown between the capture and the reset.
    function switchOwner(site) {
      if (!saveDeparting()) return Promise.resolve(false);
      resetOwner();
      return load(site).then(() => true);
    }

    return {
      // routing
      normalizeSite: normalizeAnnotationSite,
      urlFor,
      saveEntry,
      // edits and saves
      markDirty,
      scheduleSave,
      cancelScheduledSave,
      queueSave,
      save,
      retryFailed,
      flushOnPagehide,
      flushForUpdate,
      // loads and owner transitions
      load,
      saveDeparting,
      resetOwner,
      switchOwner,
      // recovery drafts
      failedSaves,
      recoveryDrafts,
      draftAccepted,
      acceptRecoveryDrafts,
      // read-only views for the UI
      isDirty: () => !!state.annDirty,
      isReady: () => !!state.annReady,
      site: () => state.annSite,
      path: () => state.annPath,
      revision: () => state.annRevision,
      context: () => state.annContext,
      serverRevision: () => state.annServerRevision,
      tail: () => state.annSaveTail,
    };
  }

  return { createStore, normalizeItems, splitItems, KEEPALIVE_BODY_LIMIT };
});
