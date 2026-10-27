/* Script errors raised before app.js (in a pane) or shell-v1.js (in the shell)
   installs its session-log relay: a controller that fails while it loads, for
   example on an older WebView2. Loaded first in index.html and shell.html; it
   holds at most ten reports until that relay takes them with drain(), which
   also removes these listeners. Holding never throws. */
(function (root) {
  "use strict";
  const held = [];
  const maxHeld = 10;

  function hold(kind, error, event) {
    try {
      if (held.length >= maxHeld) return;
      held.push({
        kind,
        error,
        event: event ? { message: event.message, filename: event.filename, lineno: event.lineno,
          colno: event.colno } : null,
      });
    } catch (_error) {
      // Best effort: an unreadable event is not worth a second error.
    }
  }

  const onError = (event) => hold("error", event?.error, event);
  const onRejection = (event) => hold("unhandledrejection", event?.reason, null);
  root.addEventListener("error", onError);
  root.addEventListener("unhandledrejection", onRejection);

  root.Nd2EarlyErrors = {
    drain() {
      root.removeEventListener?.("error", onError);
      root.removeEventListener?.("unhandledrejection", onRejection);
      return held.splice(0);
    },
  };
})(window);
