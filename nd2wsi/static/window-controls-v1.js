/* Native-window controls. Tabs, navigation and annotations stay in this window. */
(function (root, factory) {
  const controls = factory();
  if (typeof module === "object" && module.exports) module.exports = controls;
  if (!root || !root.document) return;
  root.Nd2WindowControls = controls;
  const attach = () => controls.mount(root.document, root.pywebview?.api, (message) => {
    if (typeof root.showError === "function") root.showError(message);
  }).catch(() => {});
  root.addEventListener("pywebviewready", attach);
  attach();
})(typeof window === "undefined" ? null : window, function () {
  async function mount(doc, api, onError = () => {}) {
    const menu = doc.getElementById("window-menu");
    if (!menu || !api?.window_context || !api?.new_window) return null;
    const context = await api.window_context();
    if (!context?.new_window_supported || !["user", "agent"].includes(context.role)) return null;
    const label = doc.getElementById("window-identity");
    const status = doc.getElementById("window-action-status");
    const buttons = [doc.getElementById("new-user-window"), doc.getElementById("new-agent-window")];
    label.textContent = `${context.role === "agent" ? "Agent" : "User"} · ${context.id.slice(0, 6)}`;
    label.title = context.role === "agent"
      ? "Separate window · isolated annotation snapshots"
      : "Separate window · shared annotations with conflict checks";
    menu.dataset.role = context.role;
    menu.dataset.sessionId = context.id;
    menu.hidden = false;
    const metalButton = doc.getElementById("open-metal-window");
    if (metalButton && context.metal_opt_in_supported && api.open_in_metal) {
      metalButton.hidden = false;
      metalButton.onclick = () => doc.defaultView?.nd2OpenActiveInMetal?.();
    }
    if (menu.dataset.wired) return context;
    menu.dataset.wired = "true";
    let opening = false;
    buttons.forEach((button, index) => {
      button.onclick = async () => {
        if (opening) return;
        opening = true;
        buttons.forEach((item) => { item.disabled = true; });
        status.textContent = "Opening a separate window…";
        try {
          const role = index === 0 ? "user" : "agent";
          const result = await api.new_window(role);
          if (!result?.ok) throw new Error(result?.error || result?.message || "Window could not be opened");
          status.textContent = "New window process started";
          menu.open = false;
        } catch (error) {
          status.textContent = `Could not open window: ${error.message || error}`;
          onError(status.textContent);
        } finally {
          opening = false;
          buttons.forEach((item) => { item.disabled = false; });
        }
      };
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      menu.open = false;
      label.focus();
      event.stopPropagation();
    });
    return context;
  }
  return { mount };
});
