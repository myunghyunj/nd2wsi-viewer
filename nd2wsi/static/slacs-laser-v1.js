/* Pane-owned laser annotations: validation, clipping, numbering and idempotent counts. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2SlacsLaser = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const COLOR = "#a0a6ae", RGB = [160, 166, 174];
  const laser = item => item?.slacs?.kind === "laser_shot";
  const pair = p => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite);
  const integer = n => Number.isSafeInteger(n) && n > 0;
  function area(points) {
    return Math.abs(points.reduce((sum, p, i) => {
      const q = points[(i + 1) % points.length]; return sum + p[0]*q[1] - q[0]*p[1];
    }, 0)) / 2;
  }
  function clip(points, width, height) {
    let ring = points.map(p => p.slice());
    for (const [axis, bound, greater] of [[0,0,true],[0,width,false],[1,0,true],[1,height,false]]) {
      const input = ring; ring = [];
      if (!input.length) break;
      const inside = p => greater ? p[axis] >= bound : p[axis] <= bound;
      let previous = input[input.length - 1];
      for (const current of input) {
        if (inside(current) !== inside(previous)) {
          const t = (bound - previous[axis]) / (current[axis] - previous[axis]);
          const cross = previous.map((v, j) => v + t * (current[j] - v)); cross[axis] = bound;
          ring.push(cross);
        }
        if (inside(current)) ring.push(current);
        previous = current;
      }
    }
    return ring;
  }
  function metadata(value, items = []) {
    const highwater = Number.isSafeInteger(value?.isolation_highwater) && value.isolation_highwater >= 0 ? value.isolation_highwater : 0;
    const presses = {};
    for (const [receiver, press] of Object.entries(value?.presses || {}).slice(-256)) {
      if (/^[a-zA-Z0-9_-]{1,128}$/.test(receiver) && integer(press)) presses[receiver] = press;
    }
    return { isolation_highwater: items.filter(laser).reduce((n, a) => integer(a.slacs.isolation) ? Math.max(n,a.slacs.isolation) : n, highwater), presses };
  }
  const shotCount = item => (item.slacs.shots || 1) + ((item.slacs.shots || 1) === 1 ? " shot" : " shots");
  const label = (item, count = false) => String(item.text || "isolation #" + item.slacs.isolation) +
    (count ? " · " + shotCount(item) : "");
  function listLabel(item) {
    const date = new Date(item.slacs.at), time = Number.isFinite(date.getTime()) ?
      String(date.getHours()).padStart(2,"0") + ":" + String(date.getMinutes()).padStart(2,"0") : "";
    return label(item) + " — " + shotCount(item) + (time ? " · " + time : "");
  }
  function receive(input, owner) {
    const result = { token: input?.token, press: input?.press, decision: "skipped", reason: "invalid", id: null,
      target_px: null, box: null, distance_px: null };
    const skip = reason => ({ ...result, reason });
    if (!input || typeof input !== "object" || !integer(input.press) ||
        typeof input.receiver_id !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(input.receiver_id) ||
        typeof input.token !== "string" || input.token.length > 128 ||
        !pair(input.target_px) || !Array.isArray(input.target_corners_px) || input.target_corners_px.length !== 4 ||
        !input.target_corners_px.every(pair) || !pair(input.pixel_size_um) || !input.pixel_size_um.every(v => v > 0) ||
        !Number.isFinite(input.target_side_um) || input.target_side_um < 2 || input.target_side_um > 200 ||
        !Number.isFinite(input.target_rotation_deg) || typeof input.target_calibrated !== "boolean" ||
        typeof input.at !== "string" || input.at.length > 64 || !Number.isFinite(Date.parse(input.at))) return result;
    result.target_px = input.target_px.slice();
    if (input.sid !== owner.sid) return skip("wrong_slide");
    if (!owner.ready) return skip("owner_not_ready");
    if (owner.locked) return skip("annotations_locked");
    if (owner.loadFailed) return skip("load_failed");
    if (owner.quitPreparing) return skip("quit_preparing");
    if (input.g !== owner.g) return skip("generation_changed");
    if (input.p !== owner.p) return skip("site_changed");
    const { width, height, items } = owner;
    if (!(width > 0 && height > 0) || !Array.isArray(items)) return skip("owner_not_ready");
    const [x,y] = input.target_px;
    if (x < 0 || y < 0 || x > width || y > height) return skip("outside_image");
    const ring = clip(input.target_corners_px, width, height), originalArea = area(input.target_corners_px);
    if (!(originalArea > 0) || ring.length < 3 || area(ring) < originalArea * .25) return skip("clipped_area");
    const meta = metadata(owner.meta, items), receiver = input.receiver_id;
    const id = "slacs-laser-" + receiver + "-" + input.press;
    result.id = id;
    if ((meta.presses[receiver] || 0) >= input.press || items.some(a => a.id === id ||
        (laser(a) && (a.slacs.presses?.[receiver] || 0) >= input.press))) return skip("duplicate_press");
    // Use the last numbered item, never the centre of a clipped or rotated bounding box.
    const previous = items.filter(a => laser(a) && pair(a.slacs.target_px)).reduce((last,a) =>
      !last || (a.slacs.isolation || 0) > (last.slacs.isolation || 0) ? a : last, null);
    const distance = previous ? Math.hypot(x - previous.slacs.target_px[0], y - previous.slacs.target_px[1]) : null;
    const halfSide = input.target_side_um / Math.min(...input.pixel_size_um) / 2;
    result.distance_px = distance;
    meta.presses[receiver] = input.press;
    // Bound receiver history without ever truncating this session's idempotency.
    meta.presses = Object.fromEntries(Object.entries(meta.presses).slice(-256));
    if (previous && distance < halfSide) {
      const updated = { ...previous, slacs: { ...previous.slacs, shots: (previous.slacs.shots || 1) + 1,
        last_at: input.at, presses: { ...previous.slacs.presses, [receiver]: input.press } } };
      owner.meta = meta;
      owner.update(previous, updated); // only laser metadata changes; a user's geometry/text stays intact
      return { ...result, decision: "updated", reason: "same_burst", id: updated.id, item: updated };
    }
    const isolation = meta.isolation_highwater + 1;
    if (!Number.isSafeInteger(isolation)) return skip("number_limit");
    meta.isolation_highwater = isolation;
    const text = "isolation #" + isolation + (input.target_calibrated ? "" : "_uncalibrated");
    const slacs = { kind: "laser_shot", version: 1, isolation, press: input.press, target_px: input.target_px.slice(),
      side_um: input.target_side_um, rotation_deg: input.target_rotation_deg, calibrated: input.target_calibrated,
      at: input.at, last_at: input.at, slacs_session: input.slacs_session, receiver_id: receiver,
      shots: 1, presses: { [receiver]: input.press } };
    const bounds = { x: Math.min(...ring.map(p=>p[0])), y: Math.min(...ring.map(p=>p[1])) };
    bounds.w = Math.max(...ring.map(p=>p[0])) - bounds.x; bounds.h = Math.max(...ring.map(p=>p[1])) - bounds.y;
    const axisAligned = ring.length === 4 && ring.every((p,i) => {
      const q = ring[(i+1)%ring.length]; return Math.abs(p[0]-q[0]) < 1e-7 || Math.abs(p[1]-q[1]) < 1e-7;
    });
    const properties = { classification: { name: "SLACS laser", color: RGB.slice() }, color: RGB.slice(), name: text };
    const item = { id, type: axisAligned ? "box" : "geojson", color: COLOR, text, slacs,
      ...(axisAligned ? bounds : {}),
      geojson: { type: "Feature", geometry: { type: "Polygon", coordinates: [[...ring, ring[0].slice()]] }, properties } };
    owner.meta = meta;
    const added = owner.add(item);
    return { ...result, decision: "added", reason: "new_burst", box: bounds, item: added || item };
  }
  return { COLOR, RGB, laser, area, clip, metadata, label, listLabel, receive };
});
