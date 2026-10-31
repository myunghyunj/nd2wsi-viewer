/* Screen-fixed laser calibration. All reported points use original image pixels. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./viewer-geometry-v1.js"));
  else root.Nd2LaserTarget = factory(root.Nd2ViewerGeometry);
})(typeof globalThis !== "undefined" ? globalThis : this, function (geometry) {
  "use strict";
  const DEFAULT = { offset_css: [0, 0], side_um: 18, rotation_deg: 45, window: null, dpr: null };
  const vector = (v, n, limit) => Array.isArray(v) && v.length === n && v.every(x => Number.isFinite(x) && Math.abs(x) <= limit);
  function validate(value) {
    const v = value || {};
    const calibrated = vector(v.window, 4, 1e6) && v.window.slice(2).every(x => x > 0 && x <= 16384) &&
      Number.isFinite(v.dpr) && v.dpr >= .1 && v.dpr <= 16;
    return { offset_css: vector(v.offset_css, 2, 16384) ? v.offset_css.slice() : [0, 0],
      side_um: Number.isFinite(v.side_um) ? Math.max(2, Math.min(200, v.side_um)) : 18,
      rotation_deg: Number.isFinite(v.rotation_deg) ? ((v.rotation_deg % 90) + 90) % 90 : 45,
      window: calibrated ? v.window.slice() : null, dpr: calibrated ? v.dpr : null };
  }
  const isCalibrated = (target, frame, dpr) => !!target.window && !!frame &&
    target.dpr === dpr && target.window.every((v, i) => v === frame[i]);
  function shape(viewport, OSD, target, pixelSize) {
    const size = viewport.getContainerSize(), center = { x: size.x / 2 + target.offset_css[0], y: size.y / 2 + target.offset_css[1] };
    const angle = target.rotation_deg * Math.PI / 180, c = Math.cos(angle), s = Math.sin(angle);
    const point = p => geometry.viewerElementToImagePoint(viewport, p, OSD);
    const image = point(center);
    // Calibrate both screen axes through the same flip-aware projection as the corners.
    const length = (dx, dy) => {
      const p = point({ x: center.x + dx, y: center.y + dy });
      return Math.hypot((p.x - image.x) * pixelSize.x, (p.y - image.y) * pixelSize.y);
    };
    const rx = target.side_um / length(c, s) / 2, ry = target.side_um / length(-s, c) / 2;
    if (![rx, ry].every(x => Number.isFinite(x) && x > 0)) throw new Error("Invalid laser target scale");
    const corners = [[-1,-1], [1,-1], [1,1], [-1,1]].map(([x,y]) =>
      ({ x: center.x + x * rx * c - y * ry * s, y: center.y + x * rx * s + y * ry * c }));
    return { center, image, rx, ry, c, s, corners, imageCorners: corners.map(point) };
  }
  function inside(box, x, y, band = 0) {
    const dx = x - box.center.x, dy = y - box.center.y;
    return Math.abs(dx * box.c + dy * box.s) <= box.rx + band &&
      Math.abs(-dx * box.s + dy * box.c) <= box.ry + band;
  }
  function mount({ win, doc, viewer, OSD, pixelSize, state, api, frame, changed, message }) {
    let target = validate(state.target), calibrating = false, press = null, gesture = null, saveQueue = Promise.resolve();
    const layer = doc.getElementById("laser-target"), capture = doc.getElementById("laser-target-capture");
    const button = doc.getElementById("laser-target-set"), label = doc.getElementById("laser-target-label");
    const status = doc.getElementById("laser-target-status");
    const visible = () => state.ready && state.slacs.available === true && !!pixelSize;
    const editable = () => visible() && !state.locked && !state.pending;
    const dpr = () => Number.isFinite(win.devicePixelRatio) ? win.devicePixelRatio : 1;
    const box = () => shape(viewer.viewport, OSD, target, pixelSize);
    const svg = (name, attributes, parent = layer) => {
      const element = doc.createElementNS("http://www.w3.org/2000/svg", name);
      for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
      parent.appendChild(element); return element;
    };
    function render() {
      if (!visible()) { calibrating = false; press = null; capture.hidden = true; }
      layer.hidden = !visible();
      layer.style.display = visible() ? "" : "none";
      button.disabled = !editable();
      button.title = !pixelSize ? "A calibrated image pixel size is needed" : !state.slacs.available ?
        "SLACS is not detected" : state.locked ? "Unlock to set the laser target" : "Click the burn mark to set the laser target";
      label.textContent = "Target " + Math.round(target.side_um * 10) / 10 + " µm · " + target.rotation_deg + "°";
      status.textContent = calibrating ? "Click the burn mark · Esc cancels" :
        isCalibrated(target, frame(), dpr()) ? "Calibrated" : target.window ?
        "Recalibrate: window moved or resized" : "Uncalibrated";
      layer.replaceChildren();
      if (!visible()) return;
      const b = box(), size = viewer.viewport.getContainerSize();
      layer.setAttribute("viewBox", "0 0 " + size.x + " " + size.y);
      const reticle = svg("g", { class: "laser-reticle", transform: `translate(${b.center.x} ${b.center.y})` });
      svg("circle", { cx: 0, cy: 0, r: 7 }, reticle);
      // No line or fill passes through the centre pixel, including at high DPR.
      for (const [x1,y1,x2,y2] of [[-11,0,-4,0],[4,0,11,0],[0,-11,0,-4],[0,4,0,11]])
        svg("line", { x1, y1, x2, y2 }, reticle);
      if (state.locked) return;
      const points = b.corners.map(p => p.x + "," + p.y).join(" ");
      svg("polygon", { points, class: "laser-target-outline", "data-calibrated": String(isCalibrated(target, frame(), dpr())) });
      svg("polygon", { points, class: "laser-target-hit" });
      b.corners.forEach((p, i) => svg("circle", { cx: p.x, cy: p.y, r: 5, class: "laser-target-handle", "data-corner": i }));
    }
    function notify() { state.target = validate(target); render(); changed(); }
    function save(calibrate = false) {
      const snapshot = validate(target), zoom = viewer.viewport.viewportToImageZoom(viewer.viewport.getZoom(true));
      // Serialize gestures. A slow disk write cannot restore an older calibration over a newer one.
      saveQueue = saveQueue.catch(() => {}).then(async () => {
        const result = await api.save_laser_target?.({ target: snapshot, calibrate, zoom });
        if (result?.ok === false || result?.saved === false) message(result?.message || "Laser target could not be remembered");
      }).catch(error => message(error.message || "Laser target could not be remembered"));
      return saveQueue;
    }
    function cancel() {
      calibrating = false; press = null; capture.hidden = true;
      if (gesture) { target = gesture.original; gesture = null; }
      notify();
    }
    function begin() {
      if (!editable()) return false;
      calibrating = true; capture.hidden = false; render(); return true;
    }
    button.addEventListener("click", begin);
    capture.addEventListener("pointerdown", event => {
      event.preventDefault(); event.stopImmediatePropagation();
      if (event.button === 0 && calibrating && editable()) {
        press = { x: event.clientX, y: event.clientY, id: event.pointerId, max: 0 };
        capture.setPointerCapture?.(event.pointerId);
      }
    });
    capture.addEventListener("pointermove", event => {
      event.preventDefault(); event.stopImmediatePropagation();
      if (press) press.max = Math.max(press.max, Math.hypot(event.clientX - press.x, event.clientY - press.y));
    });
    capture.addEventListener("pointerup", event => {
      event.preventDefault(); event.stopImmediatePropagation();
      const p = press; press = null;
      if (!p || event.pointerId !== p.id || !calibrating || !editable()) return;
      capture.releasePointerCapture?.(event.pointerId);
      if (Math.max(p.max, Math.hypot(event.clientX - p.x, event.clientY - p.y)) >= 4) return;
      const size = viewer.viewport.getContainerSize();
      target = { ...target, offset_css: [event.clientX - size.x / 2, event.clientY - size.y / 2], window: frame(), dpr: dpr() };
      calibrating = false; capture.hidden = true; notify(); save(true);
    });
    capture.addEventListener("pointercancel", cancel);
    capture.addEventListener("wheel", event => { event.preventDefault(); event.stopImmediatePropagation(); }, { passive: false });
    layer.addEventListener("pointerdown", event => {
      if (!editable() || event.button !== 0) return;
      // The polygon's 8 px hit band consumes edge clicks; its interior has no hit target.
      event.preventDefault(); event.stopImmediatePropagation();
      if (event.target?.getAttribute?.("data-corner") === null) return;
      gesture = { original: validate(target), box: box(), id: event.pointerId };
      layer.setPointerCapture?.(event.pointerId);
    });
    layer.addEventListener("pointermove", event => {
      if (!gesture || !editable()) return;
      event.preventDefault(); event.stopImmediatePropagation();
      const b = gesture.box, dx = event.clientX - b.center.x, dy = event.clientY - b.center.y;
      const factor = Math.max(Math.abs(dx * b.c + dy * b.s) / b.rx, Math.abs(-dx * b.s + dy * b.c) / b.ry);
      target.side_um = Math.max(2, Math.min(200, gesture.original.side_um * factor)); notify();
    });
    layer.addEventListener("pointerup", event => {
      if (!gesture || event.pointerId !== gesture.id) return;
      event.preventDefault(); event.stopImmediatePropagation(); gesture = null;
      layer.releasePointerCapture?.(event.pointerId); save();
    });
    layer.addEventListener("pointercancel", cancel);
    function contextmenu(event) {
      if (!editable() || !inside(box(), event.clientX, event.clientY)) return false;
      event.preventDefault(); event.stopImmediatePropagation();
      target.rotation_deg = (target.rotation_deg + (event.shiftKey ? 75 : 15)) % 90;
      notify(); save(); return true;
    }
    layer.addEventListener("contextmenu", contextmenu);
    function report() {
      if (!visible()) return { target_px: null, target_offset_css: target.offset_css.slice(), target_side_um: target.side_um,
        target_rotation_deg: target.rotation_deg, target_corners_px: null, target_calibrated: false };
      const b = box();
      return { target_px: [b.image.x, b.image.y], target_offset_css: target.offset_css.slice(), target_side_um: target.side_um,
        target_rotation_deg: target.rotation_deg, target_corners_px: b.imageCorners.map(p => [p.x, p.y]),
        target_calibrated: isCalibrated(target, frame(), dpr()) };
    }
    return { render, report, cancel, begin, contextmenu, get calibrating() { return calibrating; }, get target() { return validate(target); } };
  }
  return { DEFAULT, validate, isCalibrated, shape, inside, mount };
});
