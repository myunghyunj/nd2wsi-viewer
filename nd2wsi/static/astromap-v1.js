/* AstroMapper-compatible coordinates.
   No pixels live in this module: annotations stay in source image coordinates. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2Astromap = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function normalizeRect(rect, width, height) {
    if (!rect || ![width, height, rect.x, rect.y, rect.w, rect.h].every(Number.isFinite) ||
        width <= 0 || height <= 0 || rect.w <= 0 || rect.h <= 0) return null;
    const x = Math.max(0, Math.min(width, Math.floor(rect.x)));
    const y = Math.max(0, Math.min(height, Math.floor(rect.y)));
    const right = Math.max(0, Math.min(width, Math.ceil(rect.x + rect.w)));
    const bottom = Math.max(0, Math.min(height, Math.ceil(rect.y + rect.h)));
    return right > x && bottom > y ? { x, y, w: right - x, h: bottom - y } : null;
  }

  function overlaps(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  function selectedRois(annotations) {
    return annotations.filter((a) => a.type === "box" && a.astromap === true)
      .map((a, i) => ({ ...a, note: a.text || String(i + 1) }))
      .filter((a) => a.astromapEnabled !== false)
      .map((a) => ({ x: a.x, y: a.y, w: a.w, h: a.h, note: a.note, enabled: true }));
  }

  function exportSize(width, height) {
    if (![width, height].every(Number.isFinite) || width <= 0 || height <= 0) return null;
    const major = Math.max(width, height);
    if (major <= 8000) return { width, height };
    // Multiply first so the major axis stays exactly 8000, even when its
    // reciprocal cannot be represented precisely by a floating-point number.
    return { width: Math.max(1, Math.floor(width * 8000 / major)),
      height: Math.max(1, Math.floor(height * 8000 / major)) };
  }

  function scaledCoordinates(rect, sourceW, sourceH, targetW, targetH) {
    const clip = (value, size) => Math.max(0, Math.min(size - 1, Math.floor(value)));
    return {
      x1: clip(rect.x * targetW / sourceW, targetW),
      y1: clip(rect.y * targetH / sourceH, targetH),
      x2: clip((rect.x + rect.w - 1) * targetW / sourceW, targetW),
      y2: clip((rect.y + rect.h - 1) * targetH / sourceH, targetH),
    };
  }

  return { normalizeRect, overlaps, selectedRois, exportSize, scaledCoordinates };
});
