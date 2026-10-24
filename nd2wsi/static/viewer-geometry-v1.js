/* Pure viewer geometry shared by the main pane and the translucent window:
   tile sources, flip-aware element/image point conversion and centre-only
   camera moves. Functions only; callers pass their own viewport, info and OSD. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2ViewerGeometry = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // info: { width, height, tileSize, levels: [{ width, height, path }, ...] }
  // with server level 0 = full resolution. query is the tile URL suffix
  // including its leading "?" (or ""): a string, or a zero-argument function
  // read at every tile request so a tile refresh (LUT, channel switch, frame
  // change) picks up the current settings without reopening the source.
  function makeTileSource({ info, OSD, baseUrl = "", query = "" }) {
    // server levels: index 0 = full resolution; OSD wants level 0 = smallest.
    const levels = info.levels.slice().reverse();
    const suffix = typeof query === "function" ? query : () => query;
    return {
      width: info.width,
      height: info.height,
      tileSize: info.tileSize,
      tileOverlap: 0,
      minLevel: 0,
      maxLevel: levels.length - 1,
      getLevelScale: function (l) {
        return levels[l].width / info.width;
      },
      getNumTiles: function (l) {
        // exact integer tile counts (float drift in scale*width could otherwise
        // request one tile past the edge on odd-sized levels)
        return new OSD.Point(
          Math.ceil(levels[l].width / info.tileSize),
          Math.ceil(levels[l].height / info.tileSize)
        );
      },
      getTileUrl: function (l, x, y) {
        return baseUrl + "api/tile/" + levels[l].path + "/" + x + "/" + y + ".jpg" + suffix();
      },
    };
  }

  // A viewer-element point as OSD's conversions expect it: OSD leaves its
  // horizontal display flip out of element/image conversions, so mirror x
  // first. The viewport may be absent before the viewer exists.
  function flipAwareElementPoint(viewport, point, OSD) {
    const p = new OSD.Point(Number(point.x), Number(point.y));
    if (viewport?.getFlip()) {
      p.x = viewport.getContainerSize().x - p.x;
    }
    return p;
  }

  function viewerElementToImagePoint(viewport, point, OSD) {
    return viewport.viewerElementToImageCoordinates(
      flipAwareElementPoint(viewport, point, OSD)
    );
  }

  // OSD includes rotation in this conversion; the horizontal flip is applied
  // separately, the inverse of flipAwareElementPoint.
  function imageToViewerElementPoint(viewport, point, OSD) {
    const p = viewport.imageToViewerElementCoordinates(
      new OSD.Point(Number(point.x), Number(point.y))
    );
    if (viewport.getFlip()) {
      p.x = viewport.getContainerSize().x - p.x;
    }
    return p;
  }

  // The four corners of an image-space rectangle ({ x, y, width, height } or
  // { x, y, w, h }) as element points, clockwise from the top-left corner.
  function rectCornersToElementPoints(viewport, rect, OSD) {
    const width = rect.width ?? rect.w, height = rect.height ?? rect.h;
    return [
      [rect.x, rect.y], [rect.x + width, rect.y],
      [rect.x + width, rect.y + height], [rect.x, rect.y + height],
    ].map(([x, y]) => imageToViewerElementPoint(viewport, { x, y }, OSD));
  }

  // Move the camera so imagePoint is centred while keeping the displayed scale,
  // even if a previous zoom is still animating: only the centre changes, both
  // immediately. With OSD the point is handed over as an OSD.Point, otherwise
  // as (x, y); OpenSeadragon accepts both forms.
  function centerOnPreservingZoom(viewport, imagePoint, OSD) {
    const center = OSD
      ? viewport.imageToViewportCoordinates(new OSD.Point(imagePoint.x, imagePoint.y))
      : viewport.imageToViewportCoordinates(imagePoint.x, imagePoint.y);
    viewport.zoomTo(viewport.getZoom(true), null, true);
    viewport.panTo(center, true);
    return center;
  }

  return {
    makeTileSource, flipAwareElementPoint, viewerElementToImagePoint,
    imageToViewerElementPoint, rectCornersToElementPoints, centerOnPreservingZoom,
  };
});
