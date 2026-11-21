/* ROI export panel state and pixel/micrometer dimension input controller.
   No DOM side effects at module evaluation time. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2RoiExport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function createController({
    state, $, document, activeFrameContext, frameOwnsRoi, pixelSize,
    dtypeBytes, fmtBytes, drawRoiOverlay, updateScaleStrip,
    syncFrameScopedControls, refreshAstromapPanel, showToast,
  }) {
    function updateRoiPanel() {
      refreshAstromapPanel();
      const r = state.roi;
      if (!r) return;
      const ps = pixelSize();
      const put = (id, v) => {
        const el = $(id);
        if (document.activeElement !== el) el.value = v; // don't clobber typing
      };
      put("roi-px-w", Math.round(r.w));
      put("roi-px-h", Math.round(r.h));
      if (ps) {
        put("roi-um-w", (r.w * ps[1]).toFixed(1));
        put("roi-um-h", (r.h * ps[0]).toFixed(1));
      } else {
        for (const id of ["roi-um-w", "roi-um-h"]) {
          $(id).value = "";
          $(id).disabled = true;
          $(id).title = "Calibration unavailable — the file carries no pixel size";
        }
      }
      const itemsize = dtypeBytes(state.info.dtype);
      const nCh = state.info.rgb ? 3 : state.channels.length;
      $("roi-bytes").textContent = "≈ " + fmtBytes(r.w * r.h * nCh * itemsize);
      updateScaleStrip();
    }

    function applyRoi(x, y, w, h) {
      const context = activeFrameContext();
      if (!context) {
        showToast("Choose a site before marking a region");
        return false;
      }
      state.roi = { x, y, w, h };
      state.roiSite = context.frame ? context.frame.p : null;
      state.renderScale = null; // a new region starts at the finest fitting scale
      drawRoiOverlay();
      updateRoiPanel();
      $("roi-detail").classList.add("active");
      $("roi-hint").style.display = "none";
      $("roi-move").disabled = false;
      syncFrameScopedControls();
      state.windows.region.fitContent(); // saved heights must not hide the exports
      return true;
    }

    /* The calibration for the µm fields is the file's own: ND2 voxel_size() /
       Aperio MPP, carried through the store as pixelSizeUm. Typing a size keeps
       the region's top-left corner, clamps to the slide, and syncs both rows. */
    function applyRoiDims(unit) {
      const r = state.roi;
      if (!r || !frameOwnsRoi()) return;
      const info = state.info;
      const ps = pixelSize();
      if (unit === "um" && !ps) return; // no physical entry without calibration
      const [py, px] = ps || [1, 1];
      const num = (id) => parseFloat(String($(id).value).replace(/[,\s]/g, ""));
      let w;
      let h;
      if (unit === "um") {
        w = num("roi-um-w") / px;
        h = num("roi-um-h") / py;
      } else {
        w = num("roi-px-w");
        h = num("roi-px-h");
      }
      if (!isFinite(w) || w < 4) w = r.w;
      if (!isFinite(h) || h < 4) h = r.h;
      w = Math.round(Math.min(w, info.width));
      h = Math.round(Math.min(h, info.height));
      // keep the requested size: slide the origin back if the box would overflow
      r.x = Math.min(r.x, info.width - w);
      r.y = Math.min(r.y, info.height - h);
      r.w = w;
      r.h = h;
      drawRoiOverlay();
      updateRoiPanel();
    }

    function wireRoiDims() {
      for (const [id, unit] of [
        ["roi-px-w", "px"], ["roi-px-h", "px"],
        ["roi-um-w", "um"], ["roi-um-h", "um"],
      ]) {
        const el = $(id);
        el.addEventListener("change", () => applyRoiDims(unit));
        el.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter") { ev.preventDefault(); el.blur(); }
          ev.stopPropagation();
        });
        el.addEventListener("focus", () => el.select());
      }
    }

    return {
      applyRoi,
      updateRoiPanel,
      applyRoiDims,
      wireRoiDims,
    };
  }

  return { createController };
});
