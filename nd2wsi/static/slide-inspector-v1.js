/* Slide inspector metadata formatting, panel rendering and OS path reveal.
   Pure formatters and a dependency-injected panel controller; no DOM work on load. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2SlideInspector = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function formatMetadataValue(value) {
    if (value === null || value === undefined || value === "") return "Not reported";
    if (typeof value !== "object") return String(value);
    if (Array.isArray(value)) return value.map(formatMetadataValue).join(" · ");
    const parts = Object.entries(value)
      .filter(([, item]) => item !== null && item !== undefined && item !== "")
      .map(([key, item]) => key.replace(/_/g, " ") + " " + formatMetadataValue(item));
    return parts.length ? parts.join(" · ") : "Not reported";
  }

  function calibratedPixelSize(data) {
    const calibration = data && typeof data.calibration === "object" ? data.calibration : {};
    const value = calibration.pixel_size_um || calibration.pixelSizeUm ||
      (data && (data.pixelSizeUm || data.pixel_size_um));
    return Array.isArray(value) && value.length >= 2 ? value.map(Number) : null;
  }

  function calibrationLabel(data, rawValueLabel) {
    const ps = calibratedPixelSize(data);
    if (!ps || !ps.every(Number.isFinite)) return "Unknown";
    const calibration = data && typeof data.calibration === "object" ? data.calibration : {};
    const source = calibration.source || data.calibration_source;
    const fmt = typeof rawValueLabel === "function" ? rawValueLabel : String;
    const value = fmt(ps[1]) + " × " + fmt(ps[0]) + " µm/px";
    return source ? value + " · " + source : value;
  }

  function selectionLabel(selection) {
    const source = selection || {};
    const z = source.z_resolved !== undefined
      ? source.z_resolved
      : source.z === undefined ? "mid" : source.z;
    return "T" + (source.t === undefined ? 0 : source.t) +
      " · P" + (source.p === undefined ? 0 : source.p) +
      " · Z" + z;
  }

  function byteSizeLabel(logical, allocated, fmtBytes) {
    if (logical === null || logical === undefined || !Number.isFinite(Number(logical))) {
      return "Unavailable";
    }
    const fmt = typeof fmtBytes === "function" ? fmtBytes : String;
    let value = "Data " + fmt(Number(logical));
    if (allocated !== null && allocated !== undefined && Number.isFinite(Number(allocated))) {
      value += " · Disk " + fmt(Number(allocated));
    }
    return value;
  }

  function storageLabel(storage) {
    const labels = {
      compact: "Compact cache (source-backed)",
      full: "Portable pyramid",
      direct: "Direct source",
      "overview-degraded": "Overview only (source missing)",
    };
    return labels[storage] || formatMetadataValue(storage);
  }

  function createInspector({
    state, $, document, fetch: fetchFn, rawValueLabel, fmtBytes, fmtInt,
    plateInspectionRows, platePlaneLabel, renderAssociatedImages, copyPlainText,
  }) {
    function slideInspectionRows(data) {
      data = data || {};
      const channels = Array.isArray(data.channels)
        ? data.channels.map((channel, i) => channel.label || channel.name || "C" + i).join(", ")
        : "Not reported";
      const levels = Array.isArray(data.levels)
        ? data.levels.map((level) =>
            "L" + level.path + " " + fmtInt(level.width) + " × " + fmtInt(level.height) +
            " (1/" + rawValueLabel(level.downsample) + ")"
          ).join(" · ")
        : "Not reported";
      const sourcePath = data.source_path || "Unavailable";
      const cachePath = data.cache_path || (
        data.storage !== "direct" && data.container_path ? data.container_path : null
      );
      const plate = data.kind === "plate" && data.plate && typeof data.plate === "object"
        ? data.plate
        : null;
      const rows = [
        ["File", data.name || "Unnamed slide"],
        ...(plate
          ? [["Kind", "Volumetric temporal · T " + plate.T + " × sites " + plate.P + " × Z " + plate.Z]]
          : []),
        ["Dimensions", fmtInt(data.width || 0) + " × " + fmtInt(data.height || 0) + " px" +
          (plate ? " per frame" : "")],
        ["Pixel type", (data.dtype || "Unknown") + (data.rgb ? " · RGB" : "")],
        ["Channels", data.rgb ? "RGB" : channels],
        ...(plate ? plateInspectionRows(plate) : []),
        ["Calibration", calibrationLabel(data, rawValueLabel)],
        ["Objective", formatMetadataValue(data.objective)],
        ["Plane", plate ? platePlaneLabel() : selectionLabel(data.selection)],
        ["Pyramid", levels],
        ["Storage", storageLabel(data.storage)],
        ["Source", sourcePath],
        ["Source size", byteSizeLabel(data.source_bytes, data.source_allocated_bytes, fmtBytes)],
        ["Cache", cachePath || (data.storage === "direct" ? "None (served from the file)" : "Unavailable")],
        ["Cache size", cachePath
          ? byteSizeLabel(data.cache_bytes, data.cache_allocated_bytes, fmtBytes)
          : data.storage === "direct" ? "None" : "Unavailable"],
      ];
      if (data.storage_details && typeof data.storage_details === "object") {
        rows.splice(9, 0, ["Storage details", formatMetadataValue(data.storage_details)]);
      }
      if (Array.isArray(data.associated) && data.associated.length) {
        rows.push(["Associated", data.associated.join(", ")]);
      }
      if (Array.isArray(data.notes) && data.notes.length) {
        rows.push(["Notes", data.notes.join(" · ")]);
      }
      return rows;
    }

    function renderSlideInspector(data, partial = false) {
      const grid = $("info-grid");
      grid.replaceChildren();
      for (const [label, value] of slideInspectionRows(data)) {
        const term = document.createElement("dt");
        const detail = document.createElement("dd");
        term.textContent = label;
        detail.textContent = value;
        if (label === "Plane") {
          detail.dataset.infoField = "plane";
        }
        if (label === "Source" || label === "Cache") {
          detail.classList.add("info-path");
          detail.title = value;
        }
        grid.append(term, detail);
      }
      $("info-content").hidden = false;
      $("info-copy").disabled = false;
      $("info-reveal-source").disabled = partial || !data.source_path || data.source_bytes === null;
      $("info-reveal-cache").disabled = partial || !(
        data.cache_path || (data.storage !== "direct" && data.container_path)
      );
      renderAssociatedImages(partial ? [] : data.associated);
      if (state.windows && state.windows.info) state.windows.info.fitContent();
    }

    function updateSlideInspectorPlane() {
      const grid = $("info-grid");
      const cell = grid?.querySelector?.('[data-info-field="plane"]');
      if (!cell) return false;
      const data = state.inspect || state.info;
      const plate = data?.kind === "plate" && data.plate && typeof data.plate === "object" ? data.plate : null;
      cell.textContent = plate ? platePlaneLabel() : selectionLabel(data?.selection);
      return true;
    }

    async function loadSlideInspector(force = false) {
      if (state.inspectLoading || (state.inspect && !force)) return;
      const expectedGen = state.info?.generation;
      state.inspectLoading = true;
      $("info-loading").hidden = false;
      $("info-loading").textContent = state.inspect ? "Refreshing slide metadata…" : "Loading slide metadata…";
      $("info-error").hidden = true;
      if (!state.inspect) $("info-content").hidden = true;
      try {
        const res = await fetchFn("api/inspect", { cache: "no-store" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        if (!data || typeof data !== "object") throw new Error("invalid response");
        if (expectedGen && state.info?.generation && state.info.generation !== expectedGen) return;
        state.inspect = { ...state.info, ...data };
        renderSlideInspector(state.inspect);
        $("info-status").textContent = "";
      } catch (error) {
        state.inspect = null;
        renderSlideInspector(state.info, true);
        $("info-error").hidden = false;
        $("info-error").querySelector("span").textContent =
          "Detailed metadata is unavailable (" + error.message + ").";
        $("info-status").textContent = "Basic viewer metadata is shown below";
      } finally {
        state.inspectLoading = false;
        $("info-loading").hidden = true;
      }
    }

    async function revealSlidePath(which) {
      const button = $(which === "source" ? "info-reveal-source" : "info-reveal-cache");
      const previous = button.textContent;
      button.disabled = true;
      button.textContent = "Revealing…";
      try {
        const res = await fetchFn("api/reveal", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ which }),
        });
        let data = null;
        try { data = await res.json(); } catch (_) { /* response may be empty */ }
        if (!res.ok || (data && data.error)) {
          throw new Error((data && data.error) || "HTTP " + res.status);
        }
        const label = which === "source" ? "Source" : "Cache";
        $("info-status").textContent = label + " revealed in the file manager";
      } catch (error) {
        $("info-status").textContent = "Could not reveal " + which + ": " + error.message;
      } finally {
        button.textContent = previous;
        const data = state.inspect || {};
        button.disabled = which === "source"
          ? !data.source_path || data.source_bytes === null
          : !(data.cache_path || (data.storage !== "direct" && data.container_path));
      }
    }

    function wireSlideInspector() {
      $("info-retry").onclick = () => loadSlideInspector(true);
      $("info-copy").onclick = () => {
        const data = state.inspect || state.info;
        const text = slideInspectionRows(data)
          .map(([label, value]) => label + ": " + value)
          .join("\n");
        copyPlainText(text)
          .then(() => {
            $("info-status").textContent = "Metadata copied to the clipboard";
          })
          .catch(() => {
            $("info-status").textContent = "Could not copy metadata";
          });
      };
      $("info-reveal-source").onclick = () => revealSlidePath("source");
      $("info-reveal-cache").onclick = () => revealSlidePath("cache");
      $("info-copy").disabled = true;
      $("info-reveal-source").disabled = true;
      $("info-reveal-cache").disabled = true;
    }

    return {
      slideInspectionRows,
      renderSlideInspector,
      updateSlideInspectorPlane,
      loadSlideInspector,
      revealSlidePath,
      wireSlideInspector,
    };
  }

  return {
    formatMetadataValue,
    calibratedPixelSize,
    calibrationLabel,
    selectionLabel,
    byteSizeLabel,
    storageLabel,
    createInspector,
  };
});
