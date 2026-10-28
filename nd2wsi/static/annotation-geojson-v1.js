/* QuPath-compatible image-pixel GeoJSON. Geometry is never rescaled or clipped. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2AnnotationGeoJSON = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MAX_BYTES = 8_000_000, MAX_FEATURES = 10000, MAX_POINTS = 250000;
  const clone = value => JSON.parse(JSON.stringify(value));
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const name = properties => properties?.name == null ? "" : String(properties.name);
  const samePoint = (a, b) => a[0] === b[0] && a[1] === b[1];

  function isGeoJSON(data) {
    return data?.type === "FeatureCollection" || data?.type === "Feature" ||
      (Array.isArray(data) && data.length > 0 && data.some(item => item?.type === "Feature"));
  }

  function color(properties) {
    const value = properties?.color ?? properties?.colorRGB ?? properties?.classification?.color ?? properties?.classification?.colorRGB;
    if (typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value)) return value.toLowerCase();
    const rgb = Number.isInteger(value) ? [(value >>> 16) & 255, (value >>> 8) & 255, value & 255]
      : Array.isArray(value) && (value.length === 3 || value.length === 4) ? value.slice(0, 3) : null;
    return rgb?.every(v => Number.isInteger(v) && v >= 0 && v <= 255)
      ? "#" + rgb.map(v => v.toString(16).padStart(2, "0")).join("") : null;
  }

  function validateGeometry(geometry, dimensions, budget) {
    if (!object(geometry)) throw new Error("missing GeoJSON geometry");
    if (geometry.isEllipse === true) throw new Error("QuPath ellipse annotations are not supported; export them as polygons first");
    if (geometry.crs != null) throw new Error("use image pixels, not a geographic coordinate reference system");
    if (geometry.plane != null) {
      const plane = geometry.plane;
      if (!object(plane) || ![plane.c ?? -1, plane.z ?? 0, plane.t ?? 0].every(Number.isInteger) ||
          (plane.c ?? -1) !== -1 || (plane.z ?? 0) !== 0 || (plane.t ?? 0) !== 0) {
        throw new Error("plane-specific GeoJSON annotations are not supported");
      }
    }
    const point = p => {
      if (!Array.isArray(p) || p.length !== 2 || !p.every(Number.isFinite)) {
        throw new Error("coordinates must contain exactly two finite image-pixel numbers");
      }
      if (p[0] < 0 || p[1] < 0 || p[0] > dimensions.width || p[1] > dimensions.height) {
        throw new Error("coordinates are outside this image; use the matching full-resolution image");
      }
      if (++budget.points > MAX_POINTS) throw new Error("GeoJSON exceeds 250000 coordinate points");
    };
    const list = (values, minimum, visit) => {
      if (!Array.isArray(values) || values.length < minimum) throw new Error("empty or incomplete GeoJSON geometry");
      values.forEach(visit);
    };
    const line = values => list(values, 2, point);
    const ring = values => {
      list(values, 4, point);
      if (!samePoint(values[0], values[values.length - 1])) throw new Error("polygon rings must be closed");
    };
    const polygon = values => list(values, 1, ring);
    switch (geometry.type) {
      case "Point": point(geometry.coordinates); break;
      case "MultiPoint": list(geometry.coordinates, 1, point); break;
      case "LineString": line(geometry.coordinates); break;
      case "MultiLineString": list(geometry.coordinates, 1, line); break;
      case "Polygon": polygon(geometry.coordinates); break;
      case "MultiPolygon": list(geometry.coordinates, 1, polygon); break;
      default: throw new Error("unsupported GeoJSON geometry: " + String(geometry.type));
    }
  }

  function visitPoints(geometry, visit) {
    const depth = { Point: 0, MultiPoint: 1, LineString: 1, MultiLineString: 2, Polygon: 2, MultiPolygon: 3 }[geometry.type];
    if (depth === undefined) throw new Error("unsupported GeoJSON geometry");
    const walk = (values, level) => level === 0 ? visit(values) : values.forEach(value => walk(value, level - 1));
    walk(geometry.coordinates, depth);
  }

  function bounds(geometry) {
    let x = Infinity, y = Infinity, right = -Infinity, bottom = -Infinity;
    visitPoints(geometry, p => { x = Math.min(x, p[0]); y = Math.min(y, p[1]);
      right = Math.max(right, p[0]); bottom = Math.max(bottom, p[1]); });
    return { x, y, w: right - x, h: bottom - y };
  }

  function nativeShape(geometry) {
    const c = geometry.coordinates;
    if (geometry.type === "Point") return { type: "pin", x: c[0], y: c[1] };
    if (geometry.type === "LineString" && c.length === 2) {
      return { type: "line", x1: c[0][0], y1: c[0][1], x2: c[1][0], y2: c[1][1] };
    }
    if (geometry.type === "Polygon" && c.length === 1 && c[0].length === 5) {
      const r = bounds(geometry), points = c[0].slice(0, 4);
      if (r.w > 0 && r.h > 0 && new Set(points.map(p => p.join(","))).size === 4 &&
          points.every((p, i) => (p[0] === r.x || p[0] === r.x + r.w) &&
            (p[1] === r.y || p[1] === r.y + r.h) &&
            (p[0] === points[(i + 1) % 4][0] || p[1] === points[(i + 1) % 4][1]))) {
        return { type: "box", ...r };
      }
    }
    return { type: "geojson" };
  }

  function parse(data, dimensions) {
    if (!dimensions || ![dimensions.width, dimensions.height].every(v => Number.isFinite(v) && v > 0)) {
      throw new Error("image dimensions are unavailable");
    }
    if (data?.crs != null || (data?.coordinate_space && data.coordinate_space !== "level-0-pixels")) {
      throw new Error("GeoJSON must use full-resolution image-pixel coordinates");
    }
    const features = data?.type === "FeatureCollection" ? data.features : data?.type === "Feature" ? [data] : data;
    if (!Array.isArray(features) || !features.length) throw new Error("GeoJSON contains no annotations");
    if (features.length > MAX_FEATURES) throw new Error("Import at most 10000 GeoJSON features");
    const budget = { points: 0 };
    return features.map((feature, index) => {
      try {
        if (!object(feature) || feature.type !== "Feature") throw new Error("expected a GeoJSON Feature");
        if (feature.properties != null && !object(feature.properties)) throw new Error("invalid feature properties");
        if (feature.crs != null) throw new Error("geographic coordinate reference systems are not supported");
        validateGeometry(feature.geometry, dimensions, budget);
        const item = { ...nativeShape(feature.geometry), text: name(feature.properties), geojson: clone(feature) };
        const selectedColor = color(feature.properties);
        if (selectedColor) item.color = selectedColor;
        return item;
      } catch (error) { throw new Error("Feature " + (index + 1) + ": " + error.message); }
    });
  }

  function featureForAnnotation(item) {
    if (!item?.geojson) return null;
    const feature = clone(item.geojson), original = nativeShape(feature.geometry);
    // Preserve vertex order, holes and foreign members unless native geometry
    // was explicitly edited. Display rotation/flip never reaches these fields.
    if (original.type !== "geojson" && Object.entries(original).some(([key, value]) => item[key] !== value)) {
      if (item.type === "pin") feature.geometry.coordinates = [item.x, item.y];
      else if (item.type === "line") feature.geometry.coordinates = [[item.x1, item.y1], [item.x2, item.y2]];
      else if (item.type === "box") feature.geometry.coordinates = [[[item.x, item.y], [item.x + item.w, item.y],
        [item.x + item.w, item.y + item.h], [item.x, item.y + item.h], [item.x, item.y]]];
      delete feature.bbox;
      delete feature.geometry.bbox;
    }
    if (String(item.text ?? "") !== name(feature.properties)) {
      feature.properties = { ...feature.properties, name: String(item.text ?? "") };
    }
    if (/^#[0-9a-f]{6}$/i.test(item.color || "") && item.color.toLowerCase() !== color(feature.properties)) {
      feature.properties = { ...feature.properties,
        color: [1, 3, 5].map(start => parseInt(item.color.slice(start, start + 2), 16)) };
    }
    return feature;
  }

  function displayParts(geometry, project) {
    const result = { paths: [], points: [] };
    const point = p => {
      const q = project(p[0], p[1]);
      if (!Number.isFinite(q.x) || !Number.isFinite(q.y)) throw new Error("invalid display coordinates");
      return q;
    };
    const path = (points, close) => points.map((p, i) => {
      const q = point(p); return (i ? "L" : "M") + q.x + " " + q.y;
    }).join(" ") + (close ? " Z" : "");
    const line = values => result.paths.push({ d: path(values, false), closed: false });
    const polygon = rings => result.paths.push({ d: rings.map(ring => path(ring, true)).join(" "), closed: true });
    switch (geometry.type) {
      case "Point": result.points.push(point(geometry.coordinates)); break;
      case "MultiPoint": geometry.coordinates.forEach(p => result.points.push(point(p))); break;
      case "LineString": line(geometry.coordinates); break;
      case "MultiLineString": geometry.coordinates.forEach(line); break;
      case "Polygon": polygon(geometry.coordinates); break;
      case "MultiPolygon": geometry.coordinates.forEach(polygon); break;
      default: throw new Error("unsupported GeoJSON geometry");
    }
    return result;
  }

  // displayParts in image pixels plus the bounds, for the viewer's image-space
  // layer. Computed once per geometry object: geometry is never edited in place
  // (parse and featureForAnnotation work on clones).
  const imagePartsCache = new WeakMap();
  function imageParts(geometry) {
    let parts = imagePartsCache.get(geometry);
    if (!parts) {
      parts = { ...displayParts(geometry, (x, y) => ({ x, y })), bounds: bounds(geometry) };
      imagePartsCache.set(geometry, parts);
    }
    return parts;
  }

  function label(item) {
    const classification = item.geojson?.properties?.classification;
    return item.text || classification?.name || (Array.isArray(classification?.names) ? classification.names.join(" / ") : "") ||
      item.geojson?.geometry?.type || "GeoJSON";
  }

  return { MAX_BYTES, MAX_FEATURES, MAX_POINTS, isGeoJSON, parse, color, bounds, featureForAnnotation, displayParts, imageParts, label };
});
