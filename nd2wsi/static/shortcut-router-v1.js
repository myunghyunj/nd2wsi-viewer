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
  const ORIENTATION_BY_CODE = Object.freeze({
    KeyR: "rotate-right",
    KeyF: "flip-horizontal",
  });
  const EDITABLE_SELECTOR = [
    "select",
    "textarea",
    '[contenteditable]:not([contenteditable="false"])',
    '[role="textbox"]',
  ].join(",");
  const FOCUS_CONTROL_SELECTOR = "input," + EDITABLE_SELECTOR;

  function isMac(platform) {
    if (platform === undefined) {
      const nav = typeof navigator === "undefined" ? null : navigator;
      platform = nav?.userAgentData?.platform || nav?.platform || "MacIntel";
    }
    return /mac|iphone|ipad|ipod/i.test(String(platform));
  }

  function commandPressed(event, platform) {
    if (!event) return false;
    return isMac(platform) ? !!event.metaKey && !event.ctrlKey : !!event.ctrlKey && !event.metaKey;
  }

  function formatShortcutText(text, platform) {
    if (isMac(platform)) return text;
    return String(text).replaceAll("⌘", "Ctrl+").replaceAll("⌥", "Alt+")
      .replaceAll("⇧", "Shift+").replaceAll("Option-drag", "Alt-drag");
  }

  function localizeLabels(document) {
    document.documentElement.classList.toggle("platform-windows", !isMac());
    for (const element of document.querySelectorAll("[title]")) {
      element.title = formatShortcutText(element.title);
    }
  }

  function targetElement(target) {
    if (target && typeof target.closest === "function") return target;
    const parent = target && target.parentElement;
    return parent && typeof parent.closest === "function" ? parent : null;
  }

  function isTypingEvent(event) {
    if (!event || event.isComposing || event.keyCode === 229) return true;
    // Shadow-root events can be retargeted to a non-editable host. Keep the
    // original input's typing/native key ownership even across that boundary.
    const targets = typeof event.composedPath === "function" ? event.composedPath() : [];
    return [event.target, ...targets].some((target) => {
      if (!target) return false;
      const tag = String(target.tagName || "").toUpperCase();
      const element = targetElement(target);
      const input = tag === "INPUT" ? target : element?.closest("input");
      if (input) {
        // Checkbox focus does not own letters such as C. Space still belongs
        // to its native toggle, including on a plate where Space means play.
        if (String(input.type || "text").toLowerCase() !== "checkbox") return true;
        if (event.key === " " || event.key === "Spacebar" ||
            event.code === "Space" || event.keyCode === 32) return true;
      }
      return tag === "SELECT" || tag === "TEXTAREA" || target.isContentEditable === true ||
        Boolean(element && element.closest(EDITABLE_SELECTOR));
    });
  }

  function isFocusControl(target) {
    if (!target) return false;
    const tag = String(target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return true;
    if (target.isContentEditable === true) return true;
    const element = targetElement(target);
    return Boolean(element && element.closest(FOCUS_CONTROL_SELECTOR));
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

  function plainLetterForEvent(event) {
    if (
      isBlocked(event) || event.repeat || event.metaKey || event.ctrlKey ||
      event.altKey || event.shiftKey
    ) return null;
    return letterCode(event) || null;
  }

  function panelForEvent(event) {
    return PANEL_BY_CODE[plainLetterForEvent(event)] || null;
  }

  function isOrientationShortcut(event, platform) {
    if (
      isBlocked(event) || !commandPressed(event, platform) ||
      event.altKey || event.shiftKey
    ) return false;
    return Boolean(ORIENTATION_BY_CODE[letterCode(event)]);
  }

  function orientationForEvent(event, platform) {
    if (!isOrientationShortcut(event, platform) || event.repeat) return null;
    return ORIENTATION_BY_CODE[letterCode(event)];
  }

  function tabIndexForEvent(event, platform) {
    if (
      isBlocked(event) || event.repeat || !commandPressed(event, platform) ||
      event.altKey || event.shiftKey
    ) return null;
    const match = /^(?:Digit|Numpad)([1-9])$/.exec(String(event.code || ""));
    return match ? Number(match[1]) - 1 : null;
  }

  return {
    isTypingEvent, isFocusControl, letterCode, plainLetterForEvent, panelForEvent, isOrientationShortcut,
    orientationForEvent, tabIndexForEvent, isMac, commandPressed,
    formatShortcutText, localizeLabels,
  };
});
