/* Pure geometry helpers for the native macOS trackpad bridge. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2NativeScope = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const finite = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };

  function rect(value) {
    if (!value || typeof value !== "object") return null;
    const left = finite(value.left);
    const top = finite(value.top);
    const right = finite(value.right);
    const bottom = finite(value.bottom);
    if ([left, top, right, bottom].some((item) => item === null)) return null;
    if (right <= left || bottom <= top) return null;
    return { left, top, right, bottom };
  }

  const clampUnit = (value) => Math.max(0, Math.min(1, value));

  function normalizeRect(value, viewportValue) {
    const source = rect(value);
    const viewportWidth = finite(viewportValue?.width);
    const viewportHeight = finite(viewportValue?.height);
    if (!source || !viewportWidth || !viewportHeight) return null;
    return rect({
      left: clampUnit(source.left / viewportWidth),
      top: clampUnit(source.top / viewportHeight),
      right: clampUnit(source.right / viewportWidth),
      bottom: clampUnit(source.bottom / viewportHeight),
    });
  }

  function mapRect(localValue, frameValue, viewportValue) {
    const local = rect(localValue);
    const frame = rect(frameValue);
    const viewportWidth = finite(viewportValue?.width);
    const viewportHeight = finite(viewportValue?.height);
    const frameWidth = finite(frameValue?.clientWidth) || (frame && frame.right - frame.left);
    const frameHeight = finite(frameValue?.clientHeight) || (frame && frame.bottom - frame.top);
    if (!local || !frame || !viewportWidth || !viewportHeight || !frameWidth || !frameHeight) {
      return null;
    }
    const scaleX = (frame.right - frame.left) / frameWidth;
    const scaleY = (frame.bottom - frame.top) / frameHeight;
    const mapped = {
      left: clampUnit((frame.left + local.left * scaleX) / viewportWidth),
      top: clampUnit((frame.top + local.top * scaleY) / viewportHeight),
      right: clampUnit((frame.left + local.right * scaleX) / viewportWidth),
      bottom: clampUnit((frame.top + local.bottom * scaleY) / viewportHeight),
    };
    return rect(mapped);
  }

  function buildScopes(entries, viewport) {
    if (!Array.isArray(entries)) return [];
    const scopes = [];
    for (const entry of entries) {
      if (!entry?.visible || typeof entry.target !== "string" || !entry.target) continue;
      const include = mapRect(entry.include, entry.frame, viewport);
      if (!include) continue;
      const exclude = (Array.isArray(entry.exclude) ? entry.exclude : [])
        .map((item) => mapRect(item, entry.frame, viewport))
        .filter(Boolean);
      scopes.push({ target: entry.target, include, exclude });
    }
    return scopes;
  }

  function pointFromInput(input, viewport) {
    const viewportWidth = finite(viewport?.width);
    const viewportHeight = finite(viewport?.height);
    if (!viewportWidth || !viewportHeight) return null;
    const ratioX = finite(input?.clientXRatio);
    const ratioY = finite(input?.clientYRatio);
    const clientX = ratioX === null ? finite(input?.clientX) : ratioX * viewportWidth;
    const clientY = ratioY === null ? finite(input?.clientY) : ratioY * viewportHeight;
    if (clientX === null || clientY === null) return null;
    return { x: clientX, y: clientY };
  }

  function pointInFrame(point, frameValue) {
    const frame = rect(frameValue);
    const x = finite(point?.x);
    const y = finite(point?.y);
    if (!frame || x === null || y === null) return null;
    const frameWidth = finite(frameValue?.clientWidth) || frame.right - frame.left;
    const frameHeight = finite(frameValue?.clientHeight) || frame.bottom - frame.top;
    if (!frameWidth || !frameHeight) return null;
    return {
      x: (x - frame.left) * frameWidth / (frame.right - frame.left),
      y: (y - frame.top) * frameHeight / (frame.bottom - frame.top),
    };
  }

  return { rect, normalizeRect, mapRect, buildScopes, pointFromInput, pointInFrame };
});
