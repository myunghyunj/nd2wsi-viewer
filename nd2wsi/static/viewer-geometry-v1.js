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

  // Reset an OpenSeadragon TiledImage in place so the next draw rebuilds tile
  // records through getTileUrl without reopening the source. Emptying
  // tilesMatrix forces fresh tile URLs; clearing imageLoader (when requested)
  // drops queued unstarted jobs from superseded display revisions; calling
  // setClip with the unchanged clip schedules the tile pass.
  function resetTiledImage(item, options = {}) {
    if (!item || typeof item.reset !== "function") return false;
    if (
      options &&
      options.clearImageLoader === true &&
      typeof item.viewer?.imageLoader?.clear === "function"
    ) {
      item.viewer.imageLoader.clear();
    }
    item.reset();
    if (item.tilesMatrix) item.tilesMatrix = {};
    if (
      typeof item._setFullyLoaded === "function" &&
      typeof item.getDrawArea === "function" &&
      item.getDrawArea()
    ) {
      item._setFullyLoaded(false);
    }
    if (typeof item.setClip === "function" && typeof item.getClip === "function") {
      item.setClip(item.getClip());
    }
    return true;
  }

  // Canonical ?-less query string so internal display-query comparisons stay
  // independent of leading "?" or encoding variants.
  function normalizeQuery(raw) {
    let text = "";
    try {
      text = raw ? String(raw) : "";
    } catch (_) {
      return "";
    }
    const body = text.replace(/^\?/, "");
    if (!body) return "";
    return typeof URLSearchParams === "function"
      ? new URLSearchParams(body).toString()
      : body;
  }

  // Identify whether a tile belongs to the currently active reset generation.
  // Prefers exact object identity in item.tilesMatrix (handles A -> B -> A
  // and post-reset stale tiles even when URLs collide), falling back to
  // normalized URL query matching for minimal mocks without tilesMatrix.
  function isActiveTile(item, tile, activeQuery) {
    if (
      item &&
      item.tilesMatrix &&
      tile &&
      Number.isInteger(tile.level) &&
      Number.isInteger(tile.x) &&
      Number.isInteger(tile.y)
    ) {
      return item.tilesMatrix[tile.level]?.[tile.x]?.[tile.y] === tile;
    }
    if (typeof tile?.getUrl === "function") {
      const url = String(tile.getUrl() || "");
      const qIdx = url.indexOf("?");
      const urlQuery = qIdx >= 0 ? url.slice(qIdx + 1) : "";
      return normalizeQuery(urlQuery) === normalizeQuery(activeQuery);
    }
    return true;
  }

  // Paint-gated tile refresh controller shared by the main viewer and the
  // translucent overlay. Holds at most one reset in flight at a time, coalesces
  // newer display queries until full visible coverage paints (or the progress
  // watchdog expires), and suppresses obsolete tile-load failures.
  function createTileRefreshGate({
    getItem,
    getViewer,
    onBeforeReset,
    onPaintComplete,
    onActiveTileError,
    setTimer,
    clearTimer,
    stallTimeoutMs = 2000,
  } = {}) {
    const timeoutMs =
      Number.isFinite(stallTimeoutMs) && stallTimeoutMs > 0 ? stallTimeoutMs : 2000;
    let activeQuery = "";
    let paintedQuery = null;
    let pendingQuery = null;
    let rendering = false;
    let stallTimer = null;
    let disposed = false;

    function currentItem() {
      return typeof getItem === "function" ? getItem() : null;
    }

    function currentViewer() {
      return typeof getViewer === "function" ? getViewer() : null;
    }

    function clearWatchdog() {
      if (stallTimer !== null) {
        const id = stallTimer;
        stallTimer = null;
        if (typeof clearTimer === "function") clearTimer(id);
      }
    }

    function armWatchdog() {
      clearWatchdog();
      if (disposed || !rendering || typeof setTimer !== "function") return;
      stallTimer = setTimer(() => {
        stallTimer = null;
        if (disposed || !rendering) return;
        rendering = false;
        // Keep paintedQuery = null so an A -> B -> A revisit still resets.
        onPaintComplete?.(null);
        if (!disposed && !rendering && pendingQuery !== null) {
          const next = pendingQuery;
          pendingQuery = null;
          beginReset(next);
        }
      }, timeoutMs);
    }

    function completePaint() {
      clearWatchdog();
      rendering = false;
      paintedQuery = activeQuery;
      onPaintComplete?.(paintedQuery);
      if (!disposed && !rendering && pendingQuery !== null) {
        const next = pendingQuery;
        pendingQuery = null;
        if (next !== paintedQuery) {
          beginReset(next);
        }
      }
    }

    function beginReset(next) {
      if (disposed) return false;
      clearWatchdog();
      activeQuery = next;
      pendingQuery = null;
      paintedQuery = null;
      const item = currentItem();
      onBeforeReset?.(item);
      if (disposed) return false;
      if (!item || typeof item.reset !== "function") {
        rendering = false;
        paintedQuery = next;
        currentViewer()?.forceRedraw?.();
        onPaintComplete?.(next);
        if (!disposed && !rendering && pendingQuery !== null) {
          const queued = pendingQuery;
          pendingQuery = null;
          if (queued !== paintedQuery) beginReset(queued);
        }
        return true;
      }
      const offscreen = typeof item.getDrawArea === "function" && !item.getDrawArea();
      if (offscreen) {
        rendering = false;
        resetTiledImage(item, { clearImageLoader: true });
        currentViewer()?.forceRedraw?.();
        paintedQuery = next;
        onPaintComplete?.(next);
        if (!disposed && !rendering && pendingQuery !== null) {
          const queued = pendingQuery;
          pendingQuery = null;
          if (queued !== paintedQuery) beginReset(queued);
        }
        return true;
      }
      rendering = true;
      resetTiledImage(item, { clearImageLoader: true });
      currentViewer()?.forceRedraw?.();
      if (rendering && !disposed && stallTimer === null) {
        armWatchdog();
      }
      return true;
    }

    function resetState(query) {
      clearWatchdog();
      activeQuery = normalizeQuery(query);
      paintedQuery = activeQuery;
      pendingQuery = null;
      rendering = false;
    }

    function init(initialQuery) {
      resetState(initialQuery);
    }

    function getActiveQuery() {
      return activeQuery;
    }

    function isRendering() {
      return rendering;
    }

    function request(nextQuery, options) {
      if (disposed) return false;
      const force = Boolean(options && options.force);
      const next = normalizeQuery(nextQuery);
      if (!rendering && !force && next === paintedQuery) {
        activeQuery = next;
        return false;
      }
      if (rendering) {
        pendingQuery = next;
        return true;
      }
      beginReset(next);
      return true;
    }

    function onTileLoaded(event) {
      if (disposed || !rendering) return;
      const item = currentItem();
      if (event?.tiledImage && event.tiledImage !== item) return;
      if (isActiveTile(item, event?.tile, activeQuery)) {
        armWatchdog();
      }
    }

    function onTiledImageDrawn(event) {
      if (disposed) return;
      const item = currentItem();
      if (event?.tiledImage && event.tiledImage !== item) return;
      const hasActiveTile =
        !Array.isArray(event?.tiles) ||
        event.tiles.some((tile) => isActiveTile(item, tile, activeQuery));
      if (rendering) {
        const fullyLoaded =
          typeof item?.getFullyLoaded !== "function" || Boolean(item.getFullyLoaded());
        if (hasActiveTile && fullyLoaded) {
          completePaint();
        }
        return;
      }
      if (paintedQuery === null && hasActiveTile && item?.getFullyLoaded?.()) {
        paintedQuery = activeQuery;
        onPaintComplete?.(activeQuery);
      }
    }

    function onFullyLoadedChange(event) {
      if (disposed || !rendering || event?.fullyLoaded !== true) return;
      const item = currentItem();
      if (event?.tiledImage && event.tiledImage !== item) return;
      if (typeof item?.getFullyLoaded !== "function" || item.getFullyLoaded() !== false) {
        completePaint();
      }
    }

    function onTileLoadFailed(event) {
      if (disposed) return false;
      const item = currentItem();
      if (event?.tiledImage && event.tiledImage !== item) return false;
      if (!isActiveTile(item, event?.tile, activeQuery)) return false;
      onActiveTileError?.(event);
      clearWatchdog();
      rendering = false;
      paintedQuery = null;
      if (!disposed && pendingQuery !== null) {
        const next = pendingQuery;
        pendingQuery = null;
        beginReset(next);
      }
      return true;
    }

    function dispose() {
      disposed = true;
      rendering = false;
      pendingQuery = null;
      clearWatchdog();
    }

    return {
      init,
      getActiveQuery,
      isRendering,
      request,
      onTileLoaded,
      onTiledImageDrawn,
      onFullyLoadedChange,
      onTileLoadFailed,
      resetState,
      dispose,
    };
  }

  return {
    makeTileSource, flipAwareElementPoint, viewerElementToImagePoint,
    imageToViewerElementPoint, rectCornersToElementPoints, centerOnPreservingZoom,
    resetTiledImage, normalizeQuery, isActiveTile, createTileRefreshGate,
  };
});
