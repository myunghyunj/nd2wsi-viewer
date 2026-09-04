/* Pure keyboard-shortcut classification shared by the pane and tab shell. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2ShortcutRouter = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PANEL_BY_CODE = Object.freeze({
    KeyC: "channels",
    KeyR: "region",
    KeyA: "annot",
  });
  const EDITABLE_SELECTOR = [
    "input",
    "select",
    "textarea",
    '[contenteditable]:not([contenteditable="false"])',
    '[role="textbox"]',
  ].join(",");

  function targetElement(target) {
    if (target && typeof target.closest === "function") return target;
    const parent = target && target.parentElement;
    return parent && typeof parent.closest === "function" ? parent : null;
  }

  function isTypingEvent(event) {
    if (!event || event.isComposing || event.keyCode === 229) return true;
    const target = event.target;
    if (!target) return false;
    const tag = String(target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return true;
    if (target.isContentEditable === true) return true;
    const element = targetElement(target);
    return Boolean(element && element.closest(EDITABLE_SELECTOR));
  }

  function isBlocked(event) {
    return !event || event.defaultPrevented || isTypingEvent(event);
  }

  function letterCode(event) {
    if (!event) return "";
    const key = String(event.key || "");
    if (/^[A-Za-z]$/.test(key)) return `Key${key.toUpperCase()}`;
    const code = String(event.code || "");
    return /^Key[A-Z]$/.test(code) ? code : "";
  }

  function panelForEvent(event) {
    if (
      isBlocked(event) || event.repeat || event.metaKey || event.ctrlKey ||
      event.altKey || event.shiftKey
    ) return null;
    return PANEL_BY_CODE[letterCode(event)] || null;
  }

  function tabIndexForEvent(event) {
    if (
      isBlocked(event) || event.repeat || !event.metaKey || event.ctrlKey ||
      event.altKey || event.shiftKey
    ) return null;
    const match = /^(?:Digit|Numpad)([1-9])$/.exec(String(event.code || ""));
    return match ? Number(match[1]) - 1 : null;
  }

  return { isTypingEvent, letterCode, panelForEvent, tabIndexForEvent };
});
