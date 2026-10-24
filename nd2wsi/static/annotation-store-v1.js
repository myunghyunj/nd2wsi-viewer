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

  // The store keeps its fields on the pane state it is given, so the pane's
  // existing readers (annotation owner checks, file import, the Astromap
  // panel) keep reading state.ann* as before; only the store writes them:
  //   state.annotations            the items; the pane edits this array itself
  //   state.editingId              cleared when another sidecar is loaded
  //   state.annSite, annReady, annPath, annDirty, annRevision, annContext
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

    function scheduleSave() {
      clearTimer(timer);
      timer = setTimer(() => { timer = null; save(); }, debounceMs);
    }

    function cancelScheduledSave() {
      clearTimer(timer);
      timer = null;
    }

    function markDirty() {
      state.annRevision += 1;
      state.annDirty = true;
      state.annAcceptedDrafts?.clear();
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
      const payload = { items: state.annotations };
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

    function recoveryDrafts() {
      const entries = failedSaves();
      if (!entries.length || entries.some((entry) => !entry.conflict || !entry.draftPath ||
          entry.draftBody !== entry.body)) return null;
      if (state.annDirty) {
        const current = saveEntry();
        if (!current || !entries.some((entry) => entry.url === current.url &&
            entry.draftBody === current.body)) return null;
      }
      return entries;
    }

    function draftAccepted(entry) {
      return !!(entry?.conflict && entry.draftPath && entry.draftBody === entry.body &&
        state.annAcceptedDrafts?.get(entry.url) === entry.draftBody);
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

    function queueSave(entry) {
      if (!entry || !entry.url) {
        return Promise.resolve({ ok: false, error: "choose a plate site first" });
      }
      const run = state.annSaveTail.then(async () => {
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
          const response = await fetch(entry.url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: entry.body,
          });
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
          return { ok: true };
        } catch (error) {
          const failed = state.annFailedSaves.get(entry.url);
          if (!failed || failed.revision <= entry.revision) {
            state.annFailedSaves.set(entry.url, entry);
          }
          if (state.annContext === entry.context) {
            state.annDirty = true;
            onStatus("Save failed: " + error.message);
          }
          onRecoveryNotice();
          return { ok: false, error: error.message };
        }
      });
      // Always resolve the tail so one failed request cannot suppress a newer
      // captured snapshot.  Callers inspect the individual result instead.
      state.annSaveTail = run.then(() => undefined);
      return run;
    }

    function save(site) {
      if (isLocked()) {
        onStatus("Not saved: annotation is locked in this degraded view");
        return Promise.resolve({ ok: false, error: "annotation is locked" });
      }
      const entry = saveEntry(site);
      if (!entry) {
        onStatus("Not saved: choose a plate site first");
        return Promise.resolve({ ok: false, error: "choose a plate site first" });
      }
      return queueSave(entry);
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
        if (!draftAccepted(entry) && (!entry.conflict || !entry.draftPath ||
            entry.draftBody !== entry.body)) await queueSave(entry);
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
      state.annPath = null;
      state.annServerRevision = null;
      state.annotations = [];
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
          state.annotations = Array.isArray(d.items) ? d.items : [];
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
              : state.annotations.length
              ? "Loaded " + state.annotations.length + " from " + basename(d.path)
              : "Sidecar: " + basename(d.path)
          );
          onRecoveryNotice();
          onRender();
          onScope();
        })
        .catch(() => {
          if (!stillCurrent()) return;
          state.annReady = false;
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

  return { createStore };
});
