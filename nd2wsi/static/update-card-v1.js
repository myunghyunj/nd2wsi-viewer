/* Update text is always plain text. Native replies are scoped to one prompt. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.nd2wsiUpdateCard = factory().mount(root, root.document);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  function mount(win, doc) {
    const element = doc.getElementById("update-notice");
    const title = doc.getElementById("update-notice-title"), body = doc.getElementById("update-notice-body");
    const meter = doc.getElementById("update-notice-progress"), detail = doc.getElementById("update-notice-detail");
    const actions = doc.getElementById("update-notice-actions"), notes = doc.getElementById("update-notice-notes");
    // getRandomValues is available on pywebview's about:blank landing page;
    // randomUUID is secure-context-only and is absent there on WKWebView.
    const identifier = () => Array.from(win.crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, "0")).join("");
    const pageId = identifier();
    let model = null, busy = false;
    const bridge = () => win.pywebview?.api;
    const text = (value, max = 600) => typeof value === "string" ? value.slice(0, max) : "";
    const number = value => Number.isFinite(value) && value > 0 ? value : 0;
    function dismiss(token) {
      if (model && token !== model.token) return false;
      model = null; element.hidden = true; busy = false; return true;
    }
    async function act(action) {
      if (!model || busy) return;
      const current = model;
      const samePrompt = () => model && model.token === current.token && model.prompt === current.prompt;
      if (current.manual && (action === "close" || action === "later")) { dismiss(current.token); return; }
      if (current.manual && action === "retry") { await check(); return; }
      busy = true; render();
      try {
        let result;
        if (current.manual && action === "download") result = await bridge()?.open_update_download(current.token);
        else if (current.manual && action === "notes") result = await bridge()?.open_update_notes(current.token);
        else result = await bridge()?.update_card_action({ token: current.token, prompt: current.prompt, action });
        if (!result?.ok) throw new Error(result?.message || "The update action is no longer available.");
        if (current.manual && action === "download") dismiss(current.token);
        else if (!current.manual && action !== "notes" && samePrompt()) {
          model.actions = []; model.consumed = true;
        }
      } catch (error) {
        if (samePrompt()) body.textContent = text(error.message);
      } finally {
        if (samePrompt()) { busy = false; renderActions(); }
      }
    }
    function renderActions() {
      actions.replaceChildren();
      if (!model) return;
      const labels = { later: model.on_quit ? "Install on quit" : "Later", download: model.manual ? "Open download" : "Download",
        install: "Install and relaunch", cancel: "Cancel", close: "Close", retry: "Try again" };
      for (const action of model.actions || []) {
        if (!labels[action]) continue;
        const button = doc.createElement("button"); button.type = "button";
        button.textContent = labels[action]; button.disabled = busy;
        if (["download", "install", "retry"].includes(action)) button.className = "update-primary";
        button.addEventListener("click", () => act(action)); actions.appendChild(button);
      }
      notes.disabled = busy;
    }
    function render() {
      if (!model) return;
      title.textContent = text(model.title, 100);
      body.textContent = (model.state === "available" && model.version ? "nd2wsi-viewer " + text(model.version, 80) + ". " : "") + text(model.message);
      notes.hidden = !model.notes;
      const downloading = model.state === "downloading";
      const fraction = downloading ? (number(model.total) ? number(model.received) / number(model.total) : null) : model.progress;
      meter.hidden = !downloading && !["waiting", "checking"].includes(model.state);
      if (Number.isFinite(fraction)) meter.value = Math.min(1, Math.max(0, fraction));
      else meter.removeAttribute("value");
      detail.hidden = !downloading;
      detail.textContent = downloading ? (number(model.received) / 1e6).toFixed(1) + " MB" +
        (number(model.total) ? " of " + (number(model.total) / 1e6).toFixed(1) + " MB" : " downloaded") : "";
      renderActions(); element.hidden = false;
    }
    function show(value) {
      if (!element || !value || typeof value.token !== "string" || typeof value.prompt !== "string" ||
          !["available", "downloading", "waiting", "ready", "error", "up_to_date", "checking"].includes(value.state)) return false;
      const samePrompt = model && model.token === value.token && model.prompt === value.prompt;
      const notice = samePrompt && model.notice ? {
        notice: true, state: model.state, title: model.title, message: model.message, actions: model.actions, progress: undefined,
      } : {};
      const answered = samePrompt && model.consumed ? { consumed: true, actions: [] } : {};
      model = { ...value, ...notice, ...answered }; busy = !!samePrompt && busy; render(); return true;
    }
    function manual(result) {
      const available = result?.ok && result.available;
      const token = result?.token || identifier();
      const shown = show({ token, prompt: identifier(), manual: true,
        state: available ? "available" : result?.ok ? "up_to_date" : "error",
        title: available ? "Update available" : result?.ok ? "No update available" : "Could not check for updates",
        version: result?.version, message: result?.message || "Please try again.", notes: available,
        actions: available ? ["later", "download"] : result?.ok ? [] : ["close", "retry"] });
      if (result?.ok && !available) win.setTimeout(() => dismiss(token), 5000);
      return shown;
    }
    async function check() {
      try {
        const result = await bridge()?.check_for_updates(false);
        if (result?.mode === "manual-download" || result?.ok === false) manual(result);
        return result;
      } catch (error) { manual({ ok: false, message: error.message }); return { ok: false }; }
    }
    function notice(message) {
      if (!model || model.manual) return false;
      model = { ...model, notice: true, state: "waiting", title: "Waiting to install", message: text(message), actions: [], progress: undefined };
      render(); return true;
    }
    notes.addEventListener("click", () => act("notes"));
    // A reload or native close also ends the Python session via native events;
    // this bridge covers pagehide while those events are still queued.
    win.addEventListener("pagehide", () => {
      if (model && !model.manual) bridge()?.update_card_closed?.(model.token)?.catch?.(() => {});
    });
    return { ready: doc.documentElement.dataset.updateCardShell === "true", pageId, show, dismiss, manual, check, notice };
  }
  return { mount };
});
