"use strict";

/* Planar similarity transforms for linked slides.

   A transform maps anchor-space points to member-space points:

       [x']   [a  b] [x]   [tx]
       [y'] = [c  d] [y] + [ty]

   Every transform here is a similarity with an optional reflection, so the
   matrix is s * R(theta) * F where F mirrors x when the pair is reflected.
   Image coordinates have y pointing down, and theta is measured the way
   OpenSeadragon measures rotation, clockwise on screen. */

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.nd2wsiAlign = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const EPS = 1e-12;

  function identity() {
    return { a: 1, b: 0, c: 0, d: 1, tx: 0, ty: 0 };
  }

  function fromOrientation(sx, sy) {
    // the pre-landmark model: independent sign flips on each axis
    return { a: sx < 0 ? -1 : 1, b: 0, c: 0, d: sy < 0 ? -1 : 1, tx: 0, ty: 0 };
  }

  function apply(t, p) {
    return { x: t.a * p.x + t.b * p.y + t.tx, y: t.c * p.x + t.d * p.y + t.ty };
  }

  function applyLinear(t, v) {
    return { x: t.a * v.x + t.b * v.y, y: t.c * v.x + t.d * v.y };
  }

  function det(t) {
    return t.a * t.d - t.b * t.c;
  }

  function invert(t) {
    const dt = det(t);
    if (Math.abs(dt) < EPS) return null;
    const a = t.d / dt, b = -t.b / dt, c = -t.c / dt, d = t.a / dt;
    return { a, b, c, d, tx: -(a * t.tx + b * t.ty), ty: -(c * t.tx + d * t.ty) };
  }

  function compose(outer, inner) {
    // compose(outer, inner)(p) = outer(inner(p))
    return {
      a: outer.a * inner.a + outer.b * inner.c,
      b: outer.a * inner.b + outer.b * inner.d,
      c: outer.c * inner.a + outer.d * inner.c,
      d: outer.c * inner.b + outer.d * inner.d,
      tx: outer.a * inner.tx + outer.b * inner.ty + outer.tx,
      ty: outer.c * inner.tx + outer.d * inner.ty + outer.ty,
    };
  }

  function scale(t) {
    return Math.sqrt(Math.abs(det(t)));
  }

  function mirrored(t) {
    return det(t) < 0;
  }

  function angleDeg(t) {
    // s * R(theta) * F: the second column is untouched by F, so it carries
    // the rotation alone: (b, d) = s * (-sin, cos)
    return (Math.atan2(-t.b, t.d) * 180) / Math.PI;
  }

  function withTranslation(t, tx, ty) {
    return { a: t.a, b: t.b, c: t.c, d: t.d, tx, ty };
  }

  function translationMatching(t, anchorPoint, memberPoint) {
    // keep the linear part, choose the translation that sends anchorPoint
    // onto memberPoint
    const moved = applyLinear(t, anchorPoint);
    return withTranslation(t, memberPoint.x - moved.x, memberPoint.y - moved.y);
  }

  function leftMultiply(t, m) {
    // m applied after t, translation kept as is; callers re-anchor it
    return {
      a: m.a * t.a + m.b * t.c,
      b: m.a * t.b + m.b * t.d,
      c: m.c * t.a + m.d * t.c,
      d: m.c * t.b + m.d * t.d,
      tx: t.tx,
      ty: t.ty,
    };
  }

  function rmsError(t, from, to) {
    let sum = 0;
    for (let i = 0; i < from.length; i += 1) {
      const p = apply(t, from[i]);
      sum += (p.x - to[i].x) ** 2 + (p.y - to[i].y) ** 2;
    }
    return Math.sqrt(sum / Math.max(1, from.length));
  }

  function fitOne(from, to, reflect) {
    const n = from.length;
    let mx = 0, my = 0, ux = 0, uy = 0;
    for (let i = 0; i < n; i += 1) {
      mx += from[i].x; my += from[i].y; ux += to[i].x; uy += to[i].y;
    }
    mx /= n; my /= n; ux /= n; uy /= n;
    // reflect the source in x first when asked, then fit a rotation + scale
    let sxx = 0, sxy = 0, spp = 0;
    for (let i = 0; i < n; i += 1) {
      const px = (from[i].x - mx) * (reflect ? -1 : 1);
      const py = from[i].y - my;
      const qx = to[i].x - ux;
      const qy = to[i].y - uy;
      sxx += px * qx + py * qy;
      sxy += px * qy - py * qx;
      spp += px * px + py * py;
    }
    if (spp < EPS) return null;
    const cos = sxx / spp;   // s * cos(theta)
    const sin = sxy / spp;   // s * sin(theta)
    const fx = reflect ? -1 : 1;
    const linear = { a: cos * fx, b: -sin, c: sin * fx, d: cos, tx: 0, ty: 0 };
    const t = translationMatching(linear, { x: mx, y: my }, { x: ux, y: uy });
    return { transform: t, rms: rmsError(t, from, to), reflected: reflect };
  }

  function fitSimilarity(from, to, options) {
    // least-squares similarity from paired points; needs at least two pairs
    const opts = options || {};
    if (!Array.isArray(from) || !Array.isArray(to)) return null;
    const n = Math.min(from.length, to.length);
    if (n < 2) return null;
    const src = from.slice(0, n), dst = to.slice(0, n);
    for (let i = 0; i < n; i += 1) {
      const p = src[i], q = dst[i];
      if (![p.x, p.y, q.x, q.y].every(Number.isFinite)) return null;
    }
    const candidates = [];
    if (opts.reflection !== true) candidates.push(fitOne(src, dst, false));
    if (opts.reflection !== false) candidates.push(fitOne(src, dst, true));
    const valid = candidates.filter((c) => c && Number.isFinite(c.rms) && scale(c.transform) > EPS);
    if (!valid.length) return null;
    valid.sort((p, q) => p.rms - q.rms);
    const best = valid[0];
    return {
      transform: best.transform,
      rms: best.rms,
      reflected: best.reflected,
      pairs: n,
      scale: scale(best.transform),
      angleDeg: angleDeg(best.transform),
    };
  }

  return {
    identity,
    fromOrientation,
    apply,
    applyLinear,
    invert,
    compose,
    leftMultiply,
    scale,
    mirrored,
    angleDeg,
    withTranslation,
    translationMatching,
    rmsError,
    fitSimilarity,
  };
});
