/* Native-window controls. Tabs, navigation and annotations stay in this window. */
(function (root, factory) {
  const controls = factory();
  if (typeof module === "object" && module.exports) module.exports = controls;
  if (!root || !root.document) return;
  root.Nd2WindowControls = controls;
  const attach = () => controls.mount(root.document, root.pywebview?.api, (message) => {
    if (typeof root.showError === "function") root.showError(message);
    else if (typeof root.setStatus === "function") root.setStatus(message);
  }).catch(() => {});
  root.addEventListener("pywebviewready", attach);
  attach();
})(typeof window === "undefined" ? null : window, function () {
  const mounts = new WeakMap();
  const controllers = new WeakMap();

  function canOpenWindow(doc) {
    return controllers.has(doc);
  }

  function requestWindow(doc, role = "user") {
    const controller = controllers.get(doc);
    if (!controller || !["user", "agent"].includes(role)) return Promise.resolve(false);
    return controller.open(role);
  }

  function mount(doc, api, onError = () => {}) {
    if (!api?.window_context || !api?.new_window) return Promise.resolve(null);
    if (mounts.has(doc)) return mounts.get(doc);
    const pending = initialize(doc, api, onError).catch(error => {
      mounts.delete(doc);
      throw error;
    });
    mounts.set(doc, pending);
    return pending;
  }

  async function initialize(doc, api, onError) {
    const context = await api.window_context();
    if (!context?.new_window_supported || !["user", "agent"].includes(context.role)) return null;
    const menu = doc.getElementById("window-menu");
    const label = doc.getElementById("window-identity");
    const status = doc.getElementById("window-action-status") || doc.getElementById("status");
    const buttons = [doc.getElementById("new-user-window"), doc.getElementById("new-agent-window")];
    if (menu && label) {
      label.textContent = `${context.role === "agent" ? "Agent" : "User"} · ${context.id.slice(0, 6)}`;
      label.title = context.role === "agent"
        ? "Separate window · isolated annotation snapshots"
        : "Separate window · shared annotations with conflict checks";
      menu.dataset.role = context.role;
      menu.dataset.sessionId = context.id;
      menu.dataset.wired = "true";
      // Keep role/session metadata and native File-menu actions intact while
      // showing an identity badge only where Agent-window isolation matters.
      menu.hidden = context.role !== "agent";
      const metalButton = doc.getElementById("open-metal-window");
      if (metalButton && context.metal_opt_in_supported && api.open_in_metal) {
        metalButton.hidden = false;
        metalButton.onclick = () => doc.defaultView?.nd2OpenActiveInMetal?.();
      }
      menu.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        menu.open = false;
        label.focus();
        event.stopPropagation();
      });
    }
    let opening = false;
    const controller = {
      async open(role) {
        if (opening) return false;
        opening = true;
        buttons.forEach(button => { if (button) button.disabled = true; });
        if (status) status.textContent = "Opening a separate window…";
        try {
          const result = await api.new_window(role);
          if (!result?.ok) throw new Error(result?.error || result?.message || "Window could not be opened");
          if (status) status.textContent = "New window process started";
          if (menu) menu.open = false;
          return true;
        } catch (error) {
          const message = `Could not open window: ${error.message || error}`;
          if (status) status.textContent = message;
          onError(message);
          return false;
        } finally {
          opening = false;
          buttons.forEach(button => { if (button) button.disabled = false; });
        }
      },
    };
    controllers.set(doc, controller);
    buttons.forEach((button, index) => {
      if (button) button.onclick = () => controller.open(index === 0 ? "user" : "agent");
    });
    const win = doc.defaultView;
    const shortcuts = win?.Nd2ShortcutRouter;
    // AppKit owns macOS key equivalents, including editable controls. Only
    // Windows needs a DOM handler, in both the empty entry page and the shell.
    if (shortcuts && !shortcuts.isMac()) win.addEventListener("keydown", event => {
      if (!shortcuts.isNewWindowShortcut(event)) return;
      const role = shortcuts.newWindowRoleForEvent(event);
      event.preventDefault();
      event.stopPropagation();
      if (role) controller.open(role);
    }, true);
    return context;
  }
  return { mount, canOpenWindow, requestWindow };
});
