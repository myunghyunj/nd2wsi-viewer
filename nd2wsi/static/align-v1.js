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
  // Dimensionless numerical policies, not visual/biological accuracy limits.
  // Covariance eigenvalue separation below this rank threshold cannot reliably
  // distinguish mirror parity; small image coverage is only an advisory below.
  const GEOMETRY_RANK_TOLERANCE = 1e-10;
  const DUPLICATE_TOLERANCE = 64 * Number.EPSILON;
  // The renderer supports a true uniform similarity, not general affine. This
  // roundoff allowance is 1e-10 relative Gram error, not a 1% anisotropy budget.
  const RENDERER_SIMILARITY_TOLERANCE = 1e-10;

  function identity() {
    return { a: 1, b: 0, c: 0, d: 1, tx: 0, ty: 0 };
  }

  function fromOrientation(sx, sy) {
    // the pre-landmark model: independent sign flips on each axis
    return { a: sx < 0 ? -1 : 1, b: 0, c: 0, d: sy < 0 ? -1 : 1, tx: 0, ty: 0 };
  }

  function screenOperation(action) {
    // Exact D4 operations in image/screen coordinates, where y points down.
    // A positive quarter turn is therefore clockwise on screen.
    if (action === "rotate-right") {
      return { a: 0, b: -1, c: 1, d: 0, tx: 0, ty: 0 };
    }
    if (action === "rotate-left") {
      return { a: 0, b: 1, c: -1, d: 0, tx: 0, ty: 0 };
    }
    if (action === "flip-horizontal") {
      return { a: -1, b: 0, c: 0, d: 1, tx: 0, ty: 0 };
    }
    if (action === "flip-vertical") {
      return { a: 1, b: 0, c: 0, d: -1, tx: 0, ty: 0 };
    }
    if (action === "transpose") {
      return { a: 0, b: 1, c: 1, d: 0, tx: 0, ty: 0 };
    }
    return null;
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

  function displayPose(t) {
    // OpenSeadragon presents a flipped viewport as F * R(degrees). For an
    // anchor-to-member transform R(theta) * F, this pose is its orientation
    // inverse F * R(-theta), including reflected quarter turns.
    return { degrees: -angleDeg(t), flipped: mirrored(t) };
  }

  function reorient(linear, action) {
    if (action === "reset") return identity();
    if (!linear) return null;
    const screen = screenOperation(action);
    if (!screen) return null;
    const inverse = invert(screen);
    return inverse ? compose(linear, inverse) : null;
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
    if (!finiteTransform(t) || !validPairs(from, to) || !from.length) return null;
    let sum = 0;
    for (let i = 0; i < from.length; i += 1) {
      const p = apply(t, from[i]);
      sum += (p.x - to[i].x) ** 2 + (p.y - to[i].y) ** 2;
    }
    const rms = Math.sqrt(sum / from.length);
    return Number.isFinite(rms) ? rms : null;
  }

  function finiteTransform(t) {
    return Boolean(t && [t.a, t.b, t.c, t.d, t.tx, t.ty].every(Number.isFinite));
  }

  function validPairs(from, to) {
    return Array.isArray(from) && Array.isArray(to) && from.length === to.length &&
      from.every((p, i) => p && to[i] &&
        [p.x, p.y, to[i].x, to[i].y].every(Number.isFinite));
  }

  function pointGeometry(points) {
    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    const minX = Math.min(...xs), minY = Math.min(...ys);
    const width = Math.max(...xs) - minX, height = Math.max(...ys) - minY;
    const extent = Math.max(width, height);
    if (!(extent > 0) || !Number.isFinite(extent)) return {valid: false, reason: "coincident-landmarks"};
    // Center relative to a nearby origin before summing. A local landmark set
    // stays numerically meaningful even far from the image-coordinate origin.
    const ox = minX + width / 2, oy = minY + height / 2;
    const mx = ox + points.reduce((sum, p) => sum + (p.x - ox) / points.length, 0);
    const my = oy + points.reduce((sum, p) => sum + (p.y - oy) / points.length, 0);
    const centered = points.map((p) => ({x: (p.x - mx) / extent, y: (p.y - my) / extent}));
    for (let i = 0; i < centered.length; i += 1) {
      for (let j = 0; j < i; j += 1) {
        if (Math.hypot(centered[i].x - centered[j].x, centered[i].y - centered[j].y) <= DUPLICATE_TOLERANCE) {
          return {valid: false, reason: "duplicate-landmarks"};
        }
      }
    }
    let xx = 0, xy = 0, yy = 0;
    for (const p of centered) {
      xx += p.x * p.x; xy += p.x * p.y; yy += p.y * p.y;
    }
    const trace = xx + yy;
    const rankRatio = Math.max(0, (xx * yy - xy * xy) / (trace * trace));
    return {
      valid: Number.isFinite(rankRatio),
      reason: "unstable-landmarks",
      center: {x: mx, y: my}, centered, extent, width, height, rankRatio,
      noncollinear: rankRatio > GEOMETRY_RANK_TOLERANCE,
    };
  }

  function fitOne(from, to, reflect, source, target) {
    const n = from.length;
    // reflect the source in x first when asked, then fit a rotation + scale
    let sxx = 0, sxy = 0, spp = 0;
    for (let i = 0; i < n; i += 1) {
      const px = source.centered[i].x * (reflect ? -1 : 1);
      const py = source.centered[i].y;
      const qx = target.centered[i].x;
      const qy = target.centered[i].y;
      sxx += px * qx + py * qy;
      sxy += px * qy - py * qx;
      spp += px * px + py * py;
    }
    if (spp < EPS) return null;
    const factor = target.extent / source.extent;
    const cos = sxx / spp * factor;   // s * cos(theta)
    const sin = sxy / spp * factor;   // s * sin(theta)
    const fx = reflect ? -1 : 1;
    const linear = { a: cos * fx, b: -sin, c: sin * fx, d: cos, tx: 0, ty: 0 };
    const t = translationMatching(linear, source.center, target.center);
    return { transform: t, rms: rmsError(t, from, to), reflected: reflect };
  }

  function fitSimilarity(from, to, options) {
    // Legacy boolean reflection remains supported: true forces reflected,
    // false forces unreflected, absent infers it from noncollinear landmarks.
    // Explicit keep policy fixes parity only, never angle or scale.
    const opts = options || {};
    if (!validPairs(from, to) || from.length < 2) return null;
    const source = pointGeometry(from), target = pointGeometry(to);
    if (!source.valid || !target.valid) return null;
    const parity = reflectionPolicy(opts);
    if (!parity || (parity.infer && (!source.noncollinear || !target.noncollinear))) return null;
    const candidates = [];
    if (parity.infer || !parity.reflected) candidates.push(fitOne(from, to, false, source, target));
    if (parity.infer || parity.reflected) candidates.push(fitOne(from, to, true, source, target));
    const valid = candidates.filter((c) => c && c.rms !== null && Number.isFinite(c.rms) &&
      finiteTransform(c.transform) && scale(c.transform) > EPS);
    if (!valid.length) return null;
    valid.sort((p, q) => p.rms - q.rms);
    const best = valid[0];
    return {
      transform: best.transform,
      rms: best.rms,
      reflected: best.reflected,
      pairs: from.length,
      scale: scale(best.transform),
      angleDeg: angleDeg(best.transform),
    };
  }

  function reflectionPolicy(options) {
    if (options.reflection === "keep") return {infer: false, reflected: options.reflected === true};
    if (typeof options.reflection === "boolean") return {infer: false, reflected: options.reflection};
    if (options.reflection === undefined || options.reflection === "infer") return {infer: true};
    return null;
  }

  function fitCandidate(from, to, options) {
    const opts = {reflection: "keep", minPoints: 4, ...(options || {})};
    const result = {status: "incomplete", reason: "insufficient-landmarks", fit: null, warnings: []};
    if (!Array.isArray(from) || !Array.isArray(to)) return result;
    if (from.length !== to.length) return {...result, reason: "unpaired-landmarks"};
    const minPoints = Number.isInteger(opts.minPoints) ? Math.max(2, opts.minPoints) : 4;
    if (from.length < minPoints) return result;
    if (!validPairs(from, to)) return {...result, status: "degenerate", reason: "nonfinite-landmarks"};
    const source = pointGeometry(from), target = pointGeometry(to);
    if (!source.valid || !target.valid) {
      return {...result, status: "degenerate", reason: !source.valid ? source.reason : target.reason};
    }
    const parity = reflectionPolicy(opts);
    if (!parity) return {...result, status: "degenerate", reason: "invalid-reflection-policy"};
    if (parity.infer && (!source.noncollinear || !target.noncollinear)) {
      return {...result, status: "degenerate", reason: "mirror-needs-noncollinear-landmarks"};
    }
    const fit = fitSimilarity(from, to, opts);
    if (!fit) return {...result, status: "degenerate", reason: "singular-fit"};
    // Coverage is a user-advisory policy, independent of rank and fit validity.
    // Optional bounds must be expressed in the same units as their landmarks.
    const coverage = {};
    for (const [name, geometry, bounds] of [
      ["source", source, opts.sourceBounds], ["target", target, opts.targetBounds],
    ]) {
      if (bounds && bounds.width > 0 && bounds.height > 0 &&
          Number.isFinite(bounds.width) && Number.isFinite(bounds.height)) {
        coverage[name] = geometry.width / bounds.width * (geometry.height / bounds.height);
        if (coverage[name] < 0.01) result.warnings.push(`${name}-local-coverage`);
      }
    }
    return {...result, status: "valid", reason: null, fit, coverage};
  }

  function pixelMapping(transform, anchorScale, memberScale) {
    // D_member^-1 * T * D_anchor. The translation is in member physical
    // coordinates, so divide it by the corresponding member pixel size only.
    if (!finiteTransform(transform) || !anchorScale || !memberScale ||
        ![anchorScale.x, anchorScale.y, memberScale.x, memberScale.y]
          .every((value) => Number.isFinite(value) && value > 0)) return null;
    const mapped = {
      a: transform.a * anchorScale.x / memberScale.x,
      b: transform.b * anchorScale.y / memberScale.x,
      c: transform.c * anchorScale.x / memberScale.y,
      d: transform.d * anchorScale.y / memberScale.y,
      tx: transform.tx / memberScale.x,
      ty: transform.ty / memberScale.y,
    };
    return finiteTransform(mapped) ? mapped : null;
  }

  function rendererPose(mapping, options) {
    const requested = options && options.relativeTolerance;
    const tolerance = Number.isFinite(requested) && requested >= 0 && requested <= 1e-8 ?
      requested : RENDERER_SIMILARITY_TOLERANCE;
    const result = {supported: false, pose: null, scale: null, error: null, tolerance};
    if (!finiteTransform(mapping)) return {...result, reason: "invalid-pixel-mapping"};
    // Normalize before M^T M to keep the test scale independent and avoid
    // squaring overflow. Equal eigenvalues mean no shear/nonuniform scale.
    const norm = Math.max(Math.abs(mapping.a), Math.abs(mapping.b), Math.abs(mapping.c), Math.abs(mapping.d));
    if (!(norm > 0)) return {...result, reason: "singular-pixel-mapping"};
    const a = mapping.a / norm, b = mapping.b / norm;
    const c = mapping.c / norm, d = mapping.d / norm;
    const g11 = a * a + c * c, g22 = b * b + d * d, g12 = a * b + c * d;
    const lambda = (g11 + g22) / 2;
    const error = Math.hypot((g11 - g22) / 2, g12) / lambda;
    const pixelScale = norm * Math.sqrt(lambda);
    if (!Number.isFinite(error) || !(pixelScale > 0) || !Number.isFinite(pixelScale)) {
      return {...result, reason: "singular-pixel-mapping"};
    }
    if (error > tolerance) return {...result, error, reason: "nonuniform-pixel-mapping"};
    // The unit-scale matrix avoids determinant under/overflow when extracting
    // reflection parity; presentation depends on orientation, not magnitude.
    const pose = displayPose({a, b, c, d, tx: 0, ty: 0});
    return {supported: true, pose, scale: pixelScale, error, tolerance, reason: null};
  }

  return {
    identity,
    fromOrientation,
    screenOperation,
    apply,
    applyLinear,
    invert,
    compose,
    leftMultiply,
    scale,
    mirrored,
    angleDeg,
    displayPose,
    reorient,
    withTranslation,
    translationMatching,
    rmsError,
    residual: rmsError,
    fitSimilarity,
    fitCandidate,
    pixelMapping,
    rendererPose,
    RENDERER_SIMILARITY_TOLERANCE,
    GEOMETRY_RANK_TOLERANCE,
  };
});
